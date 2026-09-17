// === SED 分析标签页（详情页内嵌） ===
// 由 detail.js 在切换到「SED 分析」标签时调用 initSedTab(container, tid)，
// 切走或页面重渲染时调用 destroySedTab() 停止轮询、销毁图表。
// 四个区块（设计方案 §2.1）：① SED 构建器（同步 /api/sed/build，含 log-log 预览图
// 与数据表）② 模型拟合面板（按 /api/sed/models schema 动态渲染，提交异步任务）
// ③ 任务与结果（轮询 + 参数表/产物图/下载）④ 时间序列诊断（闭包关系 α–β +
// 伪玻尔兹曼光变）。
import {
  isAuthed, isAdmin, showToast, getLightcurves,
  getSedModels, getSedEpochs, buildSed,
  submitSedJob, getSedJobs, getSedJob, sedJobFileUrl, deleteSedJob, stopSedJob,
  sedClosure, sedClosurePlot, getSedClosureRelations, sedBolometric,
} from '../api.js';
import { chartColors, academicFonts } from '../theme.js';
import { buildSpectralColors, sortBandsByFreq, pointToMJy } from '../bands.js';
import { createYErrBarPlugin } from '../chart_plugins.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { esc, escAttr, sci3, sciFormat, fmtNum } from '../utils.js';

const POLL_INTERVAL = 5000;

let _tid = null;
let _modelsData = null;     // GET /api/sed/models 缓存（与源无关，跨 render 复用）
let _epochs = null;         // GET /api/sed/epochs 结果（按当前 tid）
let _sedResult = null;      // 最近一次 /build 结果
let _jobs = [];
let _pollTimer = null;
let _selectedId = null;     // 当前展开结果的任务 id
let _resultReqId = 0;       // 结果加载请求令牌（竞态防护）
let _closureTable = null;   // 闭包关系系数表缓存
let _closureImgUrl = null;  // closure_plot 的 blob URL（销毁时需 revoke）
let _sedChart = null;
const _sedChartHolder = { chart: null };   // 框选缩放用的当前图表引用
let _lbolChart = null;
const _lbolChartHolder = { chart: null };
let _tbbChart = null;
let _rbbChart = null;
let _lcRaw = [];          // 构建器小光变图原始测光点（未 discard、非上限、time>0）
let _lcChart = null;      // 小光变图实例
let _lcColors = {};       // band → color（小光变图与高亮层共用）
let _selRange = null;     // 当前阴影区 {t1, t2, markT}（t 单位天）
let _dragPx = null;       // 框选拖拽中的像素选区 {x0, x1}
let _buildTimer = null;   // 框选/勾选触发的 SED 重建防抖

// ─── 工具 ───
function fmtTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}

function fmtRuntime(s) {
  if (s == null) return '-';
  if (s < 60) return `${s.toFixed(0)}s`;
  return `${(s / 60).toFixed(1)}min`;
}

function numOrNull(v) {
  if (v == null || v === '') return null;
  const n = parseFloat(v);
  return isFinite(n) ? n : null;
}

const STATUS_BADGE = {
  pending: '<span class="badge bg-secondary"><i class="bi bi-clock"></i> pending</span>',
  running: '<span class="badge bg-primary"><span class="spinner-border spinner-border-sm" style="width:0.7rem;height:0.7rem"></span> running</span>',
  done: '<span class="badge bg-success">done</span>',
  failed: '<span class="badge bg-danger">failed</span>',
  interrupted: '<span class="badge bg-warning text-dark">interrupted</span>',
};

// 文本下载（CSV 导出用）
function downloadText(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// model_name（sed_xxx）→ 显示用模型名
function modelLabel(modelName) {
  if (!modelName) return '-';
  const key = modelName.replace(/^sed_/, '');
  const m = ((_modelsData && _modelsData.models) || []).find(x => x.key === key);
  if (m) return m.label;
  if (_modelsData && _modelsData.series && key === _modelsData.series.key) {
    return _modelsData.series.label;
  }
  return modelName;
}

// log-log 散点图公共 scales 配置
function _logScales(xTitle, yTitle) {
  const cc = chartColors();
  return {
    x: {
      type: 'logarithmic',
      title: { display: true, text: xTitle, color: cc.tick, font: academicFonts().title },
      ticks: { color: cc.tick, font: academicFonts().tick, maxTicksLimit: 8, callback: v => sciFormat(v) },
      grid: { color: cc.gridSoft }, border: { color: cc.tick },
    },
    y: {
      type: 'logarithmic',
      title: { display: true, text: yTitle, color: cc.tick, font: academicFonts().title },
      ticks: { color: cc.tick, font: academicFonts().tick, maxTicksLimit: 8, callback: v => sciFormat(v) },
      grid: { color: cc.gridSoft }, border: { color: cc.tick },
    },
  };
}

function _chartPluginsCfg(tooltipLabel) {
  const cc = chartColors();
  return {
    legend: {
      position: 'bottom',
      labels: {
        color: cc.legend, boxWidth: 12, font: { ...academicFonts().legend, size: 11 },
        filter: item => item.text !== '',
      },
    },
    tooltip: {
      backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
      callbacks: { label: tooltipLabel },
    },
  };
}

// 框选缩放：把选区范围写回 scales 并无动画重绘
function _attachLogDragZoom(holder, canvas) {
  attachDragZoom(holder, canvas, (range) => {
    const ch = holder.chart;
    if (!ch) return;
    ch.options.scales.x.min = range.xmin;
    ch.options.scales.x.max = range.xmax;
    ch.options.scales.y.min = range.ymin;
    ch.options.scales.y.max = range.ymax;
    ch.update('none');
  });
}

// ─── 入口 / 清理 ───
export async function initSedTab(container, tid) {
  destroySedTab();
  _tid = tid;
  _jobs = [];
  _selectedId = null;
  _sedResult = null;
  _epochs = null;
  container.innerHTML = `
    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-layers"></i> SED 构建器</div>
      <div class="card-body">
        <div class="row g-3">
          <div class="col-lg-6">
            <div class="small text-secondary mb-1">光变参考（log-log；在图上横向框选时间窗口 → 自动填 t_sel/Δt 并重建 SED；阴影 = t_sel±Δt）</div>
            <div style="position:relative;height:280px"><canvas id="sedLcChart"></canvas></div>
            <div class="text-secondary mt-1" style="font-size:0.72rem" id="sedLcHint">加载光变数据...</div>
          </div>
          <div class="col-lg-6">
            <div class="row g-2 small">
              <div class="col-4">
                <label class="form-label small mb-1">t_sel（天，相对 t0）</label>
                <input type="number" class="form-control form-control-sm" id="sedTSel" step="any" min="0" placeholder="如 1.0">
              </div>
              <div class="col-4">
                <label class="form-label small mb-1">Δt（天）</label>
                <input type="number" class="form-control form-control-sm" id="sedDt" step="any" min="0" placeholder="默认 0.1·t_sel">
              </div>
              <div class="col-4">
                <label class="form-label small mb-1">建议历元（同窗口 ≥3 波段）</label>
                <select class="form-select form-select-sm" id="sedEpochSel">
                  <option value="">加载中...</option>
                </select>
              </div>
            </div>
            <div class="row g-2 small mt-1 align-items-center">
              <div class="col-auto">
                <span class="small text-secondary me-1">同时化模式</span>
                <div class="form-check form-check-inline">
                  <input class="form-check-input" type="radio" name="sedMode" id="sedModeWindow" value="window" checked>
                  <label class="form-check-label" for="sedModeWindow" title="t_sel±Δt 窗口内直接取点，不插值（余辉文献惯例）">时段取值</label>
                </div>
                <div class="form-check form-check-inline">
                  <input class="form-check-input" type="radio" name="sedMode" id="sedModeInterp" value="interp">
                  <label class="form-check-label" for="sedModeInterp" title="每波段 GP 回归在 t_sel 处内插（SN/TDE 波段错峰时常用；不外推）">GP 内插</label>
                </div>
              </div>
              <div class="col-auto">
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" id="sedUseGext" checked>
                  <label class="form-check-label" for="sedUseGext" title="优先使用已做银河系消光改正的流量列">银消改正列</label>
                </div>
              </div>
              <div class="col-auto">
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" id="sedKCorrect">
                  <label class="form-check-label" for="sedKCorrect" title="z 已知时输出静止系 SED 副本（幂律近似）">k 改正</label>
                </div>
              </div>
            </div>
            <div class="row g-2 small mt-1">
              <div class="col-6">
                <label class="form-label small mb-1">上限点</label>
                <select class="form-select form-select-sm" id="sedUlMode">
                  <option value="exclude" selected>剔除（默认）</option>
                  <option value="include">保留（拟合单侧罚）</option>
                </select>
              </div>
              <div class="col-6">
                <label class="form-label small mb-1">z 覆盖</label>
                <input type="number" class="form-control form-control-sm" id="sedZOverride" step="any" min="0" placeholder="缺省用目录值">
              </div>
            </div>
            <div class="row mt-2">
              <div class="col-12">
                <button class="btn btn-sm btn-primary w-100" id="sedBuildBtn">
                  <i class="bi bi-play-fill"></i> 构建 SED
                </button>
              </div>
            </div>
          </div>
        </div>
        <details open class="mt-3">
          <summary class="small text-secondary" style="cursor:pointer">波段选择（缺省全部；勾选变化即重建）</summary>
          <div class="d-flex align-items-center gap-2 mt-1">
            <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-1" id="sedBandAllBtn" style="font-size:0.72rem">全选</button>
            <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-1" id="sedBandNoneBtn" style="font-size:0.72rem">全不选</button>
            <span class="small text-secondary" id="sedBandCount"></span>
          </div>
          <div id="sedBandPanel" class="border rounded p-2 mt-1"
               style="max-height:180px;overflow-y:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:0.1rem 0.5rem">
            <span class="text-secondary small">建议历元加载后列出可用波段</span>
          </div>
        </details>
        <div id="sedBuildOut" class="mt-3"></div>
      </div>
    </div>

    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-cpu"></i> 模型拟合</div>
      <div class="card-body" id="sedFitBody">
        <div class="text-secondary small"><span class="spinner-border spinner-border-sm"></span> 加载模型清单...</div>
      </div>
    </div>

    <div class="card mb-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-list-task"></i> 拟合任务与结果</span>
        <button class="btn btn-sm btn-outline-secondary" id="sedRefreshBtn" title="刷新任务列表">
          <i class="bi bi-arrow-clockwise"></i>
        </button>
      </div>
      <div class="card-body p-0" id="sedJobsBody">
        <div class="text-secondary small p-3"><span class="spinner-border spinner-border-sm"></span> 加载任务...</div>
      </div>
    </div>
    <div id="sedResultArea" class="mb-3"></div>

    <div class="row g-3">
      <div class="col-lg-6">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-diagram-3"></i> 闭包关系 α–β 诊断</div>
          <div class="card-body">
            <div class="text-secondary small mb-2">
              <i class="bi bi-info-circle"></i> α 为光变衰减指数（Fν∝t^(−α)，可先在光变页用幂律拟合得到）；
              β 为谱指数（Fν∝ν^(−β)，可由上方幂律拟合任务一键填入）。
            </div>
            <div class="row g-2 small">
              <div class="col-6"><label class="form-label small mb-1">α</label>
                <input type="number" class="form-control form-control-sm" id="sedAlpha" step="any" placeholder="如 1.2"></div>
              <div class="col-6"><label class="form-label small mb-1">σ_α<span class="text-secondary">（可空）</span></label>
                <input type="number" class="form-control form-control-sm" id="sedAlphaErr" step="any" min="0" placeholder="可空"></div>
              <div class="col-6"><label class="form-label small mb-1">β</label>
                <input type="number" class="form-control form-control-sm" id="sedBeta" step="any" placeholder="如 0.8"></div>
              <div class="col-6"><label class="form-label small mb-1">σ_β<span class="text-secondary">（可空）</span></label>
                <input type="number" class="form-control form-control-sm" id="sedBetaErr" step="any" min="0" placeholder="可空"></div>
              <div class="col-6"><label class="form-label small mb-1" title="L∝t^q 参数化的能量注入指数；填写后注入闭包关系（Racusin+2009 Table 1 列 c）参与排名，图中以虚线区分">q<span class="text-secondary">（能量注入指数，可空，0≤q&lt;1）</span></label>
                <input type="number" class="form-control form-control-sm" id="sedClosureQ" step="any" min="0" max="1" placeholder="可空"></div>
            </div>
            <button class="btn btn-sm btn-primary mt-2" id="sedClosureBtn">
              <i class="bi bi-play-fill"></i> 诊断
            </button>
            <div id="sedClosureOut" class="mt-3"></div>
          </div>
        </div>
      </div>
      <div class="col-lg-6">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-brightness-high"></i> 伪玻尔兹曼光变</div>
          <div class="card-body">
            <div class="row g-2 small">
              <div class="col-4">
                <label class="form-label small mb-1" title="Lyman+2014 颜色→BC 系数样本">BC 样本</label>
                <select class="form-select form-select-sm" id="sedBcSample">
                  <option value="all" selected>all（全样本）</option>
                  <option value="se">se（SE SNe）</option>
                  <option value="ii">ii（SNe II）</option>
                  <option value="cooling">cooling（SBO 冷却）</option>
                </select>
              </div>
              <div class="col-4">
                <label class="form-label small mb-1">Δt/t（历元窗口）</label>
                <input type="number" class="form-control form-control-sm" id="sedBoloDtFrac" step="any" min="0" value="0.1">
              </div>
              <div class="col-4">
                <label class="form-label small mb-1">z 覆盖</label>
                <input type="number" class="form-control form-control-sm" id="sedBoloZ" step="any" min="0" placeholder="缺省用目录值">
              </div>
            </div>
            <button class="btn btn-sm btn-primary mt-2" id="sedBoloBtn">
              <i class="bi bi-play-fill"></i> 计算
            </button>
            <div class="text-secondary small mt-1" style="font-size:0.72rem">历元自动建议（同窗口 ≥3 波段，上限 30 个）；无红移时光度不可算。</div>
            <div id="sedBoloOut" class="mt-3"></div>
          </div>
        </div>
      </div>
    </div>`;

  document.getElementById('sedBuildBtn').addEventListener('click', doBuild);
  document.getElementById('sedEpochSel').addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (isFinite(v)) {
      document.getElementById('sedTSel').value = String(v);
      _syncSelFromInputs();
      _scheduleBuild();
    }
  });
  document.getElementById('sedRefreshBtn').addEventListener('click', () => refreshJobs());
  document.getElementById('sedClosureBtn').addEventListener('click', doClosure);
  document.getElementById('sedBoloBtn').addEventListener('click', doBolometric);
  // t_sel/Δt 手动输入 → 阴影区同步移动
  for (const id of ['sedTSel', 'sedDt']) {
    document.getElementById(id).addEventListener('input', _syncSelFromInputs);
  }
  // 同时化模式切换 → interp 下阴影内加画 t_sel 实线
  document.querySelectorAll('input[name="sedMode"]').forEach(r =>
    r.addEventListener('change', _syncSelFromInputs));
  // 银消改正列切换 → 重算小光变图 mJy
  document.getElementById('sedUseGext').addEventListener('change', buildMiniLcChart);
  // 波段勾选（事件代理；面板内容随历元加载重建，容器不变）
  document.getElementById('sedBandPanel').addEventListener('change', (e) => {
    if (!e.target.classList.contains('sed-band-cb')) return;
    updateLcHighlight();
    _updateBandButtons();
    if (_clearSedIfNoBand()) return;  // 0 波段：清空 SED 图，不触发重建
    _scheduleBuild();
  });
  document.getElementById('sedBandAllBtn').addEventListener('click', () => _setAllBands(true));
  document.getElementById('sedBandNoneBtn').addEventListener('click', () => _setAllBands(false));

  // 模型清单（模块级缓存）、建议历元、任务列表并行加载
  try {
    if (!_modelsData) _modelsData = await getSedModels();
    renderFitCard();
  } catch (e) {
    const body = document.getElementById('sedFitBody');
    if (body) body.innerHTML = `<div class="text-danger small">模型清单加载失败: ${esc(e.message)}</div>`;
  }
  getSedEpochs(_tid).then(res => {
    if (!_tid) return;
    _epochs = (res && res.epochs) || [];
    renderEpochOptions();
    renderBandPanel();
    updateLcHighlight();
    // 历元晚于模型清单加载时，序列模型的历元勾选列表需补渲染
    const sel = document.getElementById('sedModelSel');
    if (sel && _modelsData && sel.value === (_modelsData.series || {}).key) {
      renderModelConfig();
    }
  }).catch(e => {
    const sel = document.getElementById('sedEpochSel');
    if (sel) sel.innerHTML = `<option value="">历元建议加载失败: ${esc(e.message)}</option>`;
  });
  loadMiniLc();
  refreshJobs();
}

export function destroySedTab() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  if (_buildTimer) { clearTimeout(_buildTimer); _buildTimer = null; }
  for (const k of ['_sedChart', '_lbolChart', '_tbbChart', '_rbbChart', '_lcChart']) {
    const ch = { _sedChart, _lbolChart, _tbbChart, _rbbChart, _lcChart }[k];
    if (ch) { try { ch.destroy(); } catch {} }
  }
  _sedChart = _lbolChart = _tbbChart = _rbbChart = _lcChart = null;
  _sedChartHolder.chart = null;
  _lbolChartHolder.chart = null;
  if (_closureImgUrl) { URL.revokeObjectURL(_closureImgUrl); _closureImgUrl = null; }
  _tid = null;
  _selectedId = null;
  _sedResult = null;
  _epochs = null;
  _lcRaw = [];
  _lcColors = {};
  _selRange = null;
  _dragPx = null;
}

// ─── ① SED 构建器 ───
function renderEpochOptions() {
  const sel = document.getElementById('sedEpochSel');
  if (!sel) return;
  if (!_epochs || !_epochs.length) {
    sel.innerHTML = '<option value="">（无满足条件的历元）</option>';
    return;
  }
  sel.innerHTML = '<option value="">选择建议历元...</option>' + _epochs.map(e =>
    `<option value="${e.t_days}">t=${sci3(e.t_days)} d（${e.n_bands} 波段 / ${e.n_points} 点）</option>`
  ).join('');
}

// 波段勾选面板：建议历元波段的并集，按频率排序
function renderBandPanel() {
  const panel = document.getElementById('sedBandPanel');
  if (!panel) return;
  const bands = new Set();
  for (const e of (_epochs || [])) for (const b of (e.bands || [])) bands.add(b);
  if (!bands.size) {
    panel.innerHTML = '<span class="text-secondary small">无可用波段信息</span>';
    return;
  }
  const sorted = sortBandsByFreq([...bands]);
  panel.innerHTML = sorted.map((b, i) => `
    <div class="form-check form-check-inline small">
      <input class="form-check-input sed-band-cb" type="checkbox" id="sedBand_${i}" value="${escAttr(b)}" checked>
      <label class="form-check-label" for="sedBand_${i}">${esc(b)}</label>
    </div>`).join('');
  _updateBandButtons();
}

// 勾选的波段；面板未渲染或全部勾选时返回 null（= 不限制）
function _checkedBands() {
  const cbs = [...document.querySelectorAll('.sed-band-cb')];
  if (!cbs.length) return null;
  const checked = cbs.filter(c => c.checked).map(c => c.value);
  return checked.length === cbs.length ? null : checked;
}

// 勾选波段数；面板未渲染返回 null（不限制）
function _nCheckedBands() {
  const cbs = [...document.querySelectorAll('.sed-band-cb')];
  if (!cbs.length) return null;
  return cbs.filter(c => c.checked).length;
}

// 波段勾选数为 0 时禁用「构建 SED」与「提交拟合」按钮（title 提示）；
// 同时刷新面板头部的已选计数（如「已选 48/56」）
function _updateBandButtons() {
  const cbs = [...document.querySelectorAll('.sed-band-cb')];
  const countEl = document.getElementById('sedBandCount');
  if (countEl && cbs.length) {
    countEl.textContent = `已选 ${cbs.filter(c => c.checked).length}/${cbs.length}`;
  }
  const none = _nCheckedBands() === 0;
  const buildBtn = document.getElementById('sedBuildBtn');
  if (buildBtn) {
    buildBtn.disabled = none;
    buildBtn.title = none ? '请至少选择一个波段' : '';
  }
  const subBtn = document.getElementById('sedSubmitBtn');
  if (subBtn) {
    subBtn.disabled = none || !isAuthed();
    subBtn.title = none ? '请至少选择一个波段' : '';
  }
}

// 波段勾选数为 0：左图高亮清空、SED 图清空并显示提示文字
function _clearSedIfNoBand() {
  if (_nCheckedBands() !== 0) return false;
  _sedResult = null;
  if (_sedChart) { try { _sedChart.destroy(); } catch {} _sedChart = null; }
  _sedChartHolder.chart = null;
  const out = document.getElementById('sedBuildOut');
  if (out) out.innerHTML = '<div class="text-secondary small">未选择任何波段：请至少勾选一个波段后构建 SED。</div>';
  return true;
}

// 全选 / 全不选
function _setAllBands(on) {
  const cbs = [...document.querySelectorAll('.sed-band-cb')];
  if (!cbs.length) return;
  cbs.forEach(c => { c.checked = on; });
  updateLcHighlight();
  _updateBandButtons();
  if (_clearSedIfNoBand()) return;  // 全不选：不触发重建
  _scheduleBuild();
}

// 框选/勾选变化后的 SED 重建（0.4s 防抖；t_sel 无效时不触发）
function _scheduleBuild() {
  if (_buildTimer) clearTimeout(_buildTimer);
  _buildTimer = setTimeout(() => {
    _buildTimer = null;
    const t = numOrNull(document.getElementById('sedTSel')?.value);
    if (t != null && t > 0 && _tid) doBuild();
  }, 400);
}

// ─── ①a 小光变图（框选取时窗） ───
// 从输入框同步阴影区；interp 模式另在 t_sel 处画实线标记
function _syncSelFromInputs() {
  const t = numOrNull(document.getElementById('sedTSel')?.value);
  let dt = numOrNull(document.getElementById('sedDt')?.value);
  if (t == null || t <= 0) {
    _selRange = null;
  } else {
    if (dt == null || dt <= 0) dt = 0.1 * t;
    const interp = document.querySelector('input[name="sedMode"]:checked')?.value === 'interp';
    _selRange = { t1: Math.max(t - dt, t * 1e-4), t2: t + dt, markT: interp ? t : null };
  }
  if (_lcChart) _lcChart.draw();
}

// afterDraw 画选区阴影（半透明填充 + 虚线边界；interp 时 t_sel 实线）
const _sedSelPlugin = {
  id: 'sedSelRect',
  afterDraw(chart) {
    const a = chart.chartArea;
    if (!a) return;
    const cc = chartColors();
    let x0 = null, x1 = null, xMark = null;
    if (_dragPx) {
      x0 = _dragPx.x0; x1 = _dragPx.x1;
    } else if (_selRange) {
      x0 = chart.scales.x.getPixelForValue(_selRange.t1);
      x1 = chart.scales.x.getPixelForValue(_selRange.t2);
      if (_selRange.markT != null) xMark = chart.scales.x.getPixelForValue(_selRange.markT);
    }
    const ctx = chart.ctx;
    ctx.save();
    if (x0 != null && x1 != null && isFinite(x0) && isFinite(x1)) {
      const xa = Math.max(a.left, Math.min(x0, x1));
      const xb = Math.min(a.right, Math.max(x0, x1));
      if (xb > xa) {
        ctx.fillStyle = cc.selectFill;
        ctx.fillRect(xa, a.top, xb - xa, a.bottom - a.top);
        ctx.strokeStyle = cc.selectStroke;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(xa, a.top, xb - xa, a.bottom - a.top);
      }
    }
    if (xMark != null && isFinite(xMark) && xMark >= a.left && xMark <= a.right) {
      ctx.setLineDash([]);
      ctx.strokeStyle = cc.selectStroke;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(xMark, a.top);
      ctx.lineTo(xMark, a.bottom);
      ctx.stroke();
    }
    ctx.restore();
  },
};

// canvas 上横向框选（只取 x 方向；松手后写回 t_sel/Δt 并防抖重建）
function _attachLcSelect(canvas) {
  if (canvas._sedSelOn) return;
  canvas._sedSelOn = true;
  let startX = null;
  const rel = (e) => {
    const b = canvas.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  };
  canvas.addEventListener('mousedown', (e) => {
    const a = _lcChart && _lcChart.chartArea;
    if (!a) return;
    const p = rel(e);
    if (p.x >= a.left && p.x <= a.right && p.y >= a.top && p.y <= a.bottom) {
      startX = p.x;
      _dragPx = { x0: p.x, x1: p.x };
      e.preventDefault();
    }
  });
  const doc = canvas.ownerDocument;
  // canvas 被 SPA 路由重建移除后，惰性自清理 document 级监听器（同 dragzoom 约定）
  const onMove = (e) => {
    if (!canvas.isConnected) { doc.removeEventListener('mousemove', onMove); return; }
    if (startX == null || !_lcChart) return;
    const a = _lcChart.chartArea;
    _dragPx = { x0: startX, x1: Math.max(a.left, Math.min(a.right, rel(e).x)) };
    _lcChart.draw();
  };
  const onUp = (e) => {
    if (!canvas.isConnected) { doc.removeEventListener('mouseup', onUp); return; }
    if (startX == null || !_lcChart) return;
    const a = _lcChart.chartArea;
    const r = { x0: startX, x1: Math.max(a.left, Math.min(a.right, rel(e).x)) };
    startX = null;
    _dragPx = null;
    _lcChart.draw();
    // 极小范围（<2% 绘图区宽）视为误触
    if (Math.abs(r.x1 - r.x0) < 0.02 * (a.right - a.left)) return;
    const xs = _lcChart.scales.x;
    let t1 = xs.getValueForPixel(Math.min(r.x0, r.x1));   // 反向拖已由 min/max 交换
    let t2 = xs.getValueForPixel(Math.max(r.x0, r.x1));
    if (!(t1 > 0) || !(t2 > t1)) return;
    const tSel = (t1 + t2) / 2, dt = (t2 - t1) / 2;
    const tEl = document.getElementById('sedTSel');
    const dEl = document.getElementById('sedDt');
    if (tEl) tEl.value = String(Number(tSel.toPrecision(4)));
    if (dEl) dEl.value = String(Number(dt.toPrecision(4)));
    _syncSelFromInputs();
    _scheduleBuild();
  };
  doc.addEventListener('mousemove', onMove);
  doc.addEventListener('mouseup', onUp);
}

// 拉取该源全部测光（一次），供小光变图使用
async function loadMiniLc() {
  _lcRaw = [];
  try {
    const lcData = await getLightcurves({ transient_id: _tid, per_page: 9999 });
    if (!_tid) return;
    _lcRaw = (lcData.items || []).filter(p => !p.discard && !p.upperlimit && p.time > 0);
    buildMiniLcChart();
  } catch (e) {
    const hint = document.getElementById('sedLcHint');
    if (hint) hint.textContent = `光变数据加载失败: ${e.message}`;
  }
}

// 小光变图：log-log、按波段分 dataset、buildSpectralColors 着色、无误差棒、图例隐藏
function buildMiniLcChart() {
  if (_lcChart) { try { _lcChart.destroy(); } catch {} _lcChart = null; }
  const canvas = document.getElementById('sedLcChart');
  if (!canvas || typeof Chart === 'undefined') return;
  const useGext = !!document.getElementById('sedUseGext')?.checked;
  const pts = [];
  for (const p of _lcRaw) {
    const conv = pointToMJy(p, useGext);
    if (!conv || !(conv.y > 0) || conv.clipped) continue;
    pts.push({ x: p.time / 86400, y: conv.y, band: p.band });  // time 原始单位为秒 → 天
  }
  const hint = document.getElementById('sedLcHint');
  if (!pts.length) {
    if (hint) hint.textContent = '无可画的光变探测点';
    return;
  }
  const bandNames = sortBandsByFreq([...new Set(pts.map(p => p.band))]);
  _lcColors = buildSpectralColors(bandNames);
  const datasets = bandNames.map(b => ({
    label: b, data: pts.filter(p => p.band === b), showLine: false,
    backgroundColor: _lcColors[b], borderColor: _lcColors[b],
    pointRadius: 2, pointHoverRadius: 4,
  }));
  const cc = chartColors();
  _lcChart = new Chart(canvas.getContext('2d'), {
    type: 'scatter',
    data: { datasets },
    plugins: [_sedSelPlugin],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          filter: item => !item.dataset._hl,
          callbacks: {
            label: (ctx) => {
              const p = ctx.raw;
              return `${p.band}  F=${sci3(p.y)} mJy  t=${fmtNum(p.x, 4)} d`;
            },
          },
        },
      },
      scales: _logScales('t [d]', 'Fν [mJy]'),
    },
  });
  if (hint) hint.textContent = `${pts.length} 探测点 / ${bandNames.length} 波段（上限点不画）`;
  _attachLcSelect(canvas);
  _syncSelFromInputs();
  updateLcHighlight();
}

// 波段勾选高亮层：被勾选波段的点叠加稍大空心圆（不必等 SED 重建）
function updateLcHighlight() {
  if (!_lcChart) return;
  const checked = new Set(
    [...document.querySelectorAll('.sed-band-cb')].filter(c => c.checked).map(c => c.value));
  _lcChart.data.datasets = _lcChart.data.datasets.filter(d => !d._hl);
  for (const d of [..._lcChart.data.datasets]) {
    if (!checked.has(d.label)) continue;
    _lcChart.data.datasets.push({
      _hl: true, label: '', data: d.data, showLine: false,
      pointRadius: 5, pointHoverRadius: 5, borderWidth: 2,
      backgroundColor: 'transparent',
      borderColor: _lcColors[d.label] || d.borderColor,
    });
  }
  _lcChart.update('none');
}

function _collectCommonOpts() {
  return {
    mode: document.querySelector('input[name="sedMode"]:checked')?.value || 'window',
    use_gext: !!document.getElementById('sedUseGext')?.checked,
    upperlimits: document.getElementById('sedUlMode')?.value || 'exclude',
    k_correct: !!document.getElementById('sedKCorrect')?.checked,
    z_override: numOrNull(document.getElementById('sedZOverride')?.value),
    bands: _checkedBands(),
  };
}

async function doBuild() {
  const out = document.getElementById('sedBuildOut');
  const btn = document.getElementById('sedBuildBtn');
  const tSel = numOrNull(document.getElementById('sedTSel')?.value);
  if (tSel == null || tSel <= 0) { showToast('请填写有效的 t_sel（天，>0）', 'warning'); return; }
  const dt = numOrNull(document.getElementById('sedDt')?.value);
  if (dt != null && dt <= 0) { showToast('Δt 必须 > 0', 'warning'); return; }
  const opts = _collectCommonOpts();
  const body = { transient_id: _tid, t_sel: tSel, mode: opts.mode,
                 use_gext: opts.use_gext, upperlimits: opts.upperlimits,
                 k_correct: opts.k_correct };
  if (dt != null) body.dt = dt;
  if (opts.z_override != null) body.z_override = opts.z_override;
  if (opts.bands) body.bands = opts.bands;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 构建中...';
  try {
    _sedResult = await buildSed(body);
    renderSedOutput(_sedResult);
  } catch (e) {
    showToast(`SED 构建失败: ${e.message}`, 'danger');
    if (out) out.innerHTML = `<div class="text-danger small">构建失败: ${esc(e.message)}</div>`;
  } finally {
    if (btn.isConnected) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-play-fill"></i> 构建 SED';
    }
  }
}

// 构建结果：log-log 预览图（全宽）+ meta 信息行 + warnings + 数据表 + CSV 下载
function renderSedOutput(res) {
  const out = document.getElementById('sedBuildOut');
  if (!out) return;
  const meta = res.meta || {};
  const pts = res.points || [];
  const warnings = meta.warnings || [];
  const lamTxt = (meta.lambda_min_a != null && meta.lambda_max_a != null)
    ? `${sci3(meta.lambda_min_a)}–${sci3(meta.lambda_max_a)} Å` : '-';
  const badges = [
    `${meta.n_bands ?? 0} 波段 / ${meta.n_points ?? 0} 点`,
    `λ 覆盖 ${lamTxt}`,
    meta.has_uv ? '含 UV' : '无 UV',
    meta.has_ir ? '含 IR' : '无 IR',
    meta.mode === 'interp' ? 'GP 内插' : '时段取值',
    meta.gext_used ? '已用银消改正列' : '未用银消改正列',
    res.z != null ? `z=${sci3(res.z)}（${esc(res.z_source || '')}）` : '无红移',
    res.k_corrected ? '已 k 改正' : null,
  ].filter(Boolean);

  out.innerHTML = pts.length ? `
    <div class="small text-secondary mb-1">log-log SED 预览（F<sub>ν</sub>，可框选缩放；▼ 为上限点）</div>
    <div style="position:relative;height:340px"><canvas id="sedPreviewChart"></canvas></div>
    <div class="d-flex flex-wrap gap-1 mt-2 mb-2">
      ${badges.map(b => `<span class="badge-tag badge-neutral">${b}</span>`).join('')}
    </div>
    ${warnings.length ? `<div class="alert alert-warning py-2 small">
      <i class="bi bi-exclamation-triangle"></i> ${warnings.map(w => esc(w)).join('<br>')}</div>` : ''}
    <div class="d-flex justify-content-between align-items-center mt-2 mb-1">
      <span class="small text-secondary">SED 数据表（${pts.length} 点）</span>
      <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="sedCsvBtn"><i class="bi bi-download"></i> 下载 CSV</button>
    </div>
    <div class="table-scroll" style="max-height:260px;overflow-y:auto">
      <table class="table table-sm mb-0" style="font-size:0.78rem">
        <thead><tr><th>波段</th><th>λ (Å)</th><th>ν (Hz)</th><th>Fν (mJy)</th><th>σ (mJy)</th><th>t_actual (d)</th><th>标记</th></tr></thead>
        <tbody>
          ${pts.map(p => `<tr>
            <td>${esc(p.band)}</td>
            <td>${sci3(p.wavelength_a)}</td>
            <td>${sci3(p.nu_hz)}</td>
            <td>${sci3(p.f_mjy)}</td>
            <td>${p.is_ul ? '-' : sci3(p.ferr_mjy)}</td>
            <td>${fmtNum(p.t_actual_days, 4)}</td>
            <td>${p.is_ul ? '<span class="badge bg-secondary">上限</span>' : ''}${p.interp ? '<span class="badge bg-primary">插值</span>' : ''}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>` : `
    <div class="d-flex flex-wrap gap-1 mb-2">
      ${badges.map(b => `<span class="badge-tag badge-neutral">${b}</span>`).join('')}
    </div>
    ${warnings.length ? `<div class="alert alert-warning py-2 small">
      <i class="bi bi-exclamation-triangle"></i> ${warnings.map(w => esc(w)).join('<br>')}</div>` : ''}
    <div class="text-secondary small">该历元窗口内无可用数据点，请调整 t_sel / Δt。</div>`;

  const csvBtn = document.getElementById('sedCsvBtn');
  if (csvBtn) csvBtn.addEventListener('click', () => {
    downloadText(`${_tid}_sed_t${res.t_sel}d.csv`, _sedCsvText(res));
  });
  if (pts.length) buildSedPreviewChart(res);
}

// CSV 字段转义（含逗号/引号/换行时按 RFC4180 加引号，引号双写）
function _csvField(v) {
  const s = v == null ? '' : String(v);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// SED 表 CSV 序列化（口径对齐后端 sed_to_csv；元数据以 # 注释行随附）
function _sedCsvText(res) {
  const meta = res.meta || {};
  const lines = [
    '# AJST SED 表',
    `# transient_id=${_tid} t_sel_days=${res.t_sel} dt_days=${res.dt} z=${res.z} z_source=${res.z_source} mode=${meta.mode} k_corrected=${res.k_corrected}`,
    `# lambda_range_a=[${meta.lambda_min_a}, ${meta.lambda_max_a}] has_uv=${meta.has_uv} has_ir=${meta.has_ir} gext_used=${meta.gext_used}`,
    'band,nu_hz,wavelength_a,t_actual_days,f_mjy,ferr_mjy,is_ul,interp',
  ];
  for (const p of (res.points || [])) {
    lines.push([p.band, p.nu_hz, p.wavelength_a, p.t_actual_days,
                p.f_mjy, p.ferr_mjy, p.is_ul, p.interp].map(_csvField).join(','));
  }
  return lines.join('\n') + '\n';
}

// SED 预览图：按波段分 dataset（一个波段一个，含上限点；上限画倒三角、无误差棒）
function buildSedPreviewChart(res) {
  if (_sedChart) { try { _sedChart.destroy(); } catch {} _sedChart = null; }
  const canvas = document.getElementById('sedPreviewChart');
  if (!canvas) return;
  if (typeof Chart === 'undefined') {
    canvas.parentElement.innerHTML = '<div class="text-secondary small">Chart.js 未加载</div>';
    return;
  }
  const pts = (res.points || []).filter(p => p.nu_hz > 0 && p.f_mjy > 0);
  const bandNames = [...new Set(pts.map(p => p.band))];
  const colors = buildSpectralColors(bandNames);
  const datasets = [];
  for (const band of bandNames) {
    const color = colors[band] || '#58a6ff';
    const data = pts.filter(p => p.band === band)
      .map(p => ({ x: p.nu_hz, y: p.f_mjy, err: p.ferr_mjy, band,
                   interp: p.interp, t: p.t_actual_days, isUL: p.is_ul }));
    if (!data.length) continue;
    datasets.push({
      label: band, data, showLine: false,
      borderColor: color,
      backgroundColor: (c) => (c.raw && c.raw.isUL) ? color + '60' : color,
      pointStyle: (c) => (c.raw && c.raw.isUL) ? 'triangle' : 'circle',
      pointRotation: (c) => (c.raw && c.raw.isUL) ? 180 : 0,
      pointRadius: (c) => (c.raw && c.raw.isUL) ? 5 : 4,
      pointHoverRadius: 6,
    });
  }
  const errPlugin = createYErrBarPlugin({
    errOf: (ds, raw) => raw.err,
    skipPoint: raw => raw.isUL,
  });
  const pluginsCfg = _chartPluginsCfg((ctx) => {
    const p = ctx.raw;
    if (p.isUL) return `${p.band} 上限 <${sci3(p.y)} mJy  t=${fmtNum(p.t, 4)} d`;
    const errTxt = p.err != null && p.err > 0 ? `±${sci3(p.err)}` : '';
    return `${p.band}  ν=${sci3(p.x)} Hz  F=${sci3(p.y)}${errTxt} mJy  t=${fmtNum(p.t, 4)} d${p.interp ? '（GP 插值）' : ''}`;
  });
  // 图例点击 ↔ 波段勾选联动（阻止默认的隐藏 dataset 行为，以重建后的数据为准）
  pluginsCfg.legend.onClick = (e, item) => {
    const cb = [...document.querySelectorAll('.sed-band-cb')].find(c => c.value === item.text);
    if (!cb) return;
    cb.checked = !cb.checked;
    updateLcHighlight();
    _updateBandButtons();
    if (_clearSedIfNoBand()) return;  // 0 波段：清空 SED 图，不触发重建
    _scheduleBuild();
  };
  _sedChart = new Chart(canvas.getContext('2d'), {
    type: 'scatter',
    data: { datasets },
    plugins: [errPlugin, dragRectPlugin],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: pluginsCfg,
      scales: _logScales('ν [Hz]', 'Fν [mJy]'),
    },
  });
  _sedChartHolder.chart = _sedChart;
  _attachLogDragZoom(_sedChartHolder, canvas);
}

// ─── ② 模型拟合面板 ───
function renderFitCard() {
  const body = document.getElementById('sedFitBody');
  if (!body || !_modelsData) return;
  const authed = isAuthed();
  const models = _modelsData.models || [];
  const series = _modelsData.series || null;
  body.innerHTML = `
    ${authed ? '' : `<div class="alert alert-warning py-2 small mb-2">
      <i class="bi bi-lock"></i> 未登录：可浏览任务与结果，提交拟合需要先登录。</div>`}
    <div class="row g-2 small">
      <div class="col-12 col-md-6">
        <label class="form-label small mb-1">模型</label>
        <select class="form-select form-select-sm" id="sedModelSel">
          ${models.map(m => `<option value="${escAttr(m.key)}">${esc(m.label)}（${esc(m.key)}）</option>`).join('')}
          ${series ? `<option value="${escAttr(series.key)}">${esc(series.label)}（${esc(series.key)}）</option>` : ''}
        </select>
      </div>
    </div>
    <div id="sedModelCfg" class="mt-2"></div>
    <button class="btn btn-sm btn-primary w-100 mt-2" id="sedSubmitBtn" ${authed ? '' : 'disabled'}>
      <i class="bi bi-play-fill"></i> 提交拟合任务
    </button>`;
  document.getElementById('sedModelSel').addEventListener('change', renderModelConfig);
  document.getElementById('sedSubmitBtn').addEventListener('click', submitJob);
  renderModelConfig();
  _updateBandButtons();  // 提交按钮重建后恢复 0 波段禁用态
}

// 按所选模型的 schema 动态渲染配置区
function renderModelConfig() {
  const cfg = document.getElementById('sedModelCfg');
  if (!cfg || !_modelsData) return;
  const key = document.getElementById('sedModelSel')?.value;
  const dft = _modelsData.defaults || {};
  const laws = _modelsData.laws || [];
  const series = _modelsData.series || {};

  // 多历元黑体序列：历元多选 / auto
  if (key === series.key) {
    const epList = (_epochs || []).map((e, i) => `
      <div class="form-check small">
        <input class="form-check-input sed-series-epoch" type="checkbox" id="sedSeriesEp_${i}" value="${e.t_days}">
        <label class="form-check-label" for="sedSeriesEp_${i}">t=${sci3(e.t_days)} d（${e.n_bands} 波段 / ${e.n_points} 点）</label>
      </div>`).join('');
    cfg.innerHTML = `
      <div class="small">
        <div class="form-check">
          <input class="form-check-input" type="radio" name="sedSeriesMode" id="sedSeriesAuto" value="auto" checked>
          <label class="form-check-label" for="sedSeriesAuto">自动建议历元</label>
        </div>
        <div class="row g-2 ms-3 mb-2" style="max-width:480px">
          <div class="col-4"><label class="small text-secondary mb-0">历元数</label>
            <input type="number" class="form-control form-control-sm" id="sedSeriesNEpochs" value="12" min="1" max="30"></div>
          <div class="col-4"><label class="small text-secondary mb-0">最少波段数</label>
            <input type="number" class="form-control form-control-sm" id="sedSeriesMinBands" value="3" min="1"></div>
          <div class="col-4"><label class="small text-secondary mb-0">Δt/t</label>
            <input type="number" class="form-control form-control-sm" id="sedSeriesDtFrac" value="0.1" step="any" min="0"></div>
        </div>
        <div class="form-check">
          <input class="form-check-input" type="radio" name="sedSeriesMode" id="sedSeriesCustom" value="custom">
          <label class="form-check-label" for="sedSeriesCustom">自定义历元（从建议历元勾选）</label>
        </div>
        <div class="border rounded p-2 ms-3" style="max-height:160px;overflow-y:auto">
          ${epList || '<span class="text-secondary small">建议历元未加载，请用自动模式</span>'}
        </div>
        <div class="row g-2 mt-2" style="max-width:480px">
          <div class="col-6"><label class="small text-secondary mb-0">单历元 nsteps</label>
            <input type="number" class="form-control form-control-sm" id="sedSeriesNsteps" value="2000" min="100" step="100"></div>
          <div class="col-6"><label class="small text-secondary mb-0">单历元 nburn</label>
            <input type="number" class="form-control form-control-sm" id="sedSeriesNburn" value="800" min="0" step="100"></div>
        </div>
      </div>`;
    return;
  }

  const m = (_modelsData.models || []).find(x => x.key === key);
  if (!m) { cfg.innerHTML = ''; return; }
  const params = m.params || [];
  const hasDust = params.some(p => p.name === 'Av');
  const hasRv = params.some(p => p.name === 'Rv');
  const isPl = key.includes('powerlaw');
  cfg.innerHTML = `
    <div class="small">
      <div class="text-secondary mb-1">拟合参数（先验区间由后端固定；历元取①构建器的 t_sel / Δt / 模式 / 波段当前值）：</div>
      <div class="table-scroll mb-2" style="max-height:180px;overflow-y:auto">
        <table class="table table-sm mb-0" style="font-size:0.78rem">
          <thead><tr><th>参数</th><th>说明</th><th>区间</th><th>scale</th></tr></thead>
          <tbody>
            ${params.map(p => `<tr>
              <td class="text-nowrap">${esc(p.name)}${p.unit ? ` (${esc(p.unit)})` : ''}</td>
              <td>${esc(p.desc || '')}</td>
              <td class="text-nowrap">${sci3(p.lo)} – ${sci3(p.hi)}</td>
              <td>${esc(p.scale)}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>
      <div class="row g-2">
        ${hasDust ? `
        <div class="col-6 col-md-4">
          <label class="small text-secondary mb-0">宿主消光律</label>
          <select class="form-select form-select-sm" id="sedLaw">
            ${laws.map(l => `<option value="${escAttr(l.name)}">${esc(l.label)}（R_V≈${sci3(l.rv_default)}${l.has_bump ? '，含 2175Å bump' : ''}）</option>`).join('')}
          </select>
        </div>` : ''}
        ${hasRv ? `
        <div class="col-6 col-md-4">
          <div class="form-check mt-3">
            <input class="form-check-input" type="checkbox" id="sedRvFree">
            <label class="form-check-label" for="sedRvFree" title="自由 R_V 为高级选项（GRB 个体消光曲线可偏离模板）">R_V 自由</label>
          </div>
        </div>
        <div class="col-6 col-md-4">
          <label class="small text-secondary mb-0">固定 R_V</label>
          <input type="number" class="form-control form-control-sm" id="sedRvFixed" step="any" placeholder="缺省用标称值">
        </div>` : ''}
      </div>
      <details class="mt-2">
        <summary class="small text-secondary" style="cursor:pointer">高级：采样与模型细节</summary>
        <div class="row g-2 mt-1" style="max-width:640px">
          <div class="col-6 col-md-3"><label class="small text-secondary mb-0">nsteps</label>
            <input type="number" class="form-control form-control-sm" id="sedNsteps" value="${dft.nsteps ?? 5000}" min="100" step="100"></div>
          <div class="col-6 col-md-3"><label class="small text-secondary mb-0">nburn</label>
            <input type="number" class="form-control form-control-sm" id="sedNburn" value="${dft.nburn ?? 2000}" min="0" step="100"></div>
          ${isPl ? `
          <div class="col-6 col-md-3"><label class="small text-secondary mb-0">ν0 (Hz)</label>
            <input type="number" class="form-control form-control-sm" id="sedNu0" step="any" placeholder="默认 5e14"></div>` : ''}
          <div class="col-12">
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="sedBandpass" checked>
              <label class="form-check-label" for="sedBandpass" title="有透过率曲线缓存的波段用通带综合与数据公平比较">通带综合（bandpass synthesis）</label>
            </div>
          </div>
        </div>
      </details>
    </div>`;
}

async function submitJob() {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  if (_nCheckedBands() === 0) { showToast('请至少选择一个波段', 'warning'); return; }
  const key = document.getElementById('sedModelSel')?.value;
  if (!key) { showToast('模型清单未加载', 'danger'); return; }
  const series = (_modelsData && _modelsData.series) || {};
  const config = { model: key };

  if (key === series.key) {
    const mode = document.querySelector('input[name="sedSeriesMode"]:checked')?.value || 'auto';
    if (mode === 'auto') {
      config.epochs = 'auto';
      const nEp = parseInt(document.getElementById('sedSeriesNEpochs')?.value, 10);
      const minB = parseInt(document.getElementById('sedSeriesMinBands')?.value, 10);
      const dtF = numOrNull(document.getElementById('sedSeriesDtFrac')?.value);
      if (isFinite(nEp) && nEp > 0) config.n_epochs = nEp;
      if (isFinite(minB) && minB > 0) config.min_bands = minB;
      if (dtF != null && dtF > 0) config.dt_frac = dtF;
    } else {
      const eps = [...document.querySelectorAll('.sed-series-epoch')]
        .filter(c => c.checked).map(c => parseFloat(c.value)).filter(isFinite);
      if (!eps.length) { showToast('自定义模式需至少勾选一个历元', 'warning'); return; }
      config.epochs = eps;
    }
    const ns = parseInt(document.getElementById('sedSeriesNsteps')?.value, 10);
    const nb = parseInt(document.getElementById('sedSeriesNburn')?.value, 10);
    if (isFinite(ns)) config.series_nsteps = ns;
    if (isFinite(nb)) config.series_nburn = nb;
    const z = numOrNull(document.getElementById('sedZOverride')?.value);
    if (z != null) config.z_override = z;
  } else {
    const tSel = numOrNull(document.getElementById('sedTSel')?.value);
    if (tSel == null || tSel <= 0) {
      showToast('单历元拟合需要先在①构建器填写有效的 t_sel（天，>0）', 'warning');
      return;
    }
    const opts = _collectCommonOpts();
    config.t_sel = tSel;
    const dt = numOrNull(document.getElementById('sedDt')?.value);
    if (dt != null) config.dt = dt;
    config.mode = opts.mode;
    config.use_gext = opts.use_gext;
    config.upperlimits = opts.upperlimits;
    config.k_correct = opts.k_correct;
    if (opts.z_override != null) config.z_override = opts.z_override;
    if (opts.bands) config.bands = opts.bands;
    const lawEl = document.getElementById('sedLaw');
    if (lawEl) config.law = lawEl.value;
    const rvFree = document.getElementById('sedRvFree');
    if (rvFree) {
      config.rv_free = rvFree.checked;
      const rvFix = numOrNull(document.getElementById('sedRvFixed')?.value);
      if (!rvFree.checked && rvFix != null) config.rv = rvFix;
    }
    const nsteps = parseInt(document.getElementById('sedNsteps')?.value, 10);
    const nburn = parseInt(document.getElementById('sedNburn')?.value, 10);
    if (isFinite(nsteps)) config.nsteps = nsteps;
    if (isFinite(nburn)) config.nburn = nburn;
    const nu0 = numOrNull(document.getElementById('sedNu0')?.value);
    if (nu0 != null && nu0 > 0) config.nu0 = nu0;
    const bp = document.getElementById('sedBandpass');
    if (bp && !bp.checked) config.bandpass = false;
  }

  const btn = document.getElementById('sedSubmitBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 提交中...';
  try {
    const res = await submitSedJob({ transient_id: _tid, config });
    showToast(`SED 拟合任务 #${res.id} 已提交（${esc(res.model_name)}）`, 'success');
    await refreshJobs();
  } catch (e) {
    showToast(`提交失败: ${e.message}`, 'danger');
  } finally {
    if (btn.isConnected) {
      _updateBandButtons();  // 恢复禁用态（含 0 波段禁用）
      btn.innerHTML = '<i class="bi bi-play-fill"></i> 提交拟合任务';
    }
  }
}

// ─── ③ 任务列表 ───
async function refreshJobs() {
  if (!_tid) return;
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  try {
    _jobs = await getSedJobs(_tid);
    if (!_tid) return;  // 等待期间已切走/销毁，丢弃过期结果（与其他 async 一致）
  } catch (e) {
    showToast(`任务列表加载失败: ${e.message}`, 'danger');
    return;
  }
  renderJobs();
  // 选中任务状态有更新时刷新结果区
  if (_selectedId != null) {
    const j = _jobs.find(x => x.id === _selectedId);
    if (!j) {
      _selectedId = null;
      const area = document.getElementById('sedResultArea');
      if (area) area.innerHTML = '';
    } else if (j.status === 'done' || j.status === 'failed') {
      loadResult(j.id);
    }
  }
  // 有 pending/running 任务时轮询
  if (_jobs.some(j => j.status === 'pending' || j.status === 'running')) {
    _pollTimer = setTimeout(refreshJobs, POLL_INTERVAL);
  }
}

function renderJobs() {
  const body = document.getElementById('sedJobsBody');
  if (!body) return;
  if (!_jobs.length) {
    body.innerHTML = '<div class="text-secondary small p-3">暂无 SED 拟合任务</div>';
    return;
  }
  body.innerHTML = `
    <div class="table-scroll" style="max-height:400px;overflow-y:auto">
      <table class="table table-sm table-hover mb-0" style="font-size:0.8rem">
        <thead><tr>
          <th>#</th><th>模型</th><th>历元</th><th>状态</th><th>χ²</th><th>创建时间</th><th></th>
        </tr></thead>
        <tbody>
          ${_jobs.map(j => `
            <tr data-jobid="${j.id}" class="${j.id === _selectedId ? 'table-active' : ''}" style="cursor:pointer">
              <td>${j.id}</td>
              <td class="text-nowrap" title="${escAttr(j.model_name)}">${esc(modelLabel(j.model_name))}</td>
              <td>${j.t_sel != null ? `t=${sci3(j.t_sel)} d` : '多历元'}</td>
              <td>${STATUS_BADGE[j.status] || esc(j.status)}</td>
              <td>${j.chi_squared != null ? sci3(j.chi_squared) : '-'}</td>
              <td class="text-nowrap small">${fmtTime(j.created_at)}</td>
              <td class="text-nowrap">
                ${j.status === 'done' ? `<button class="btn btn-sm btn-outline-primary py-0 px-1 sed-view" data-jobid="${j.id}">查看结果</button>` : ''}
                ${isAuthed() && ['pending', 'running'].includes(j.status)
                  ? `<button class="btn btn-sm btn-outline-warning py-0 px-1 sed-stop" data-jobid="${j.id}">中断</button>` : ''}
                ${isAdmin() && ['done', 'failed', 'interrupted'].includes(j.status)
                  ? `<button class="btn btn-sm btn-outline-danger py-0 px-1 sed-del" data-jobid="${j.id}" title="删除"><i class="bi bi-trash"></i></button>` : ''}
              </td>
            </tr>
            ${['failed', 'interrupted'].includes(j.status) ? `<tr class="sed-err-row" data-jobid="${j.id}" style="display:none"><td colspan="7" class="small text-danger" id="sedErr_${j.id}">加载错误信息...</td></tr>` : ''}
          `).join('')}
        </tbody>
      </table>
    </div>`;

  body.querySelectorAll('.sed-view').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      loadResult(parseInt(btn.dataset.jobid, 10));
    });
  });
  body.querySelectorAll('.sed-stop').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = parseInt(btn.dataset.jobid, 10);
      if (!confirm(`确定中断任务 #${id}？已计算的结果将被放弃`)) return;
      try {
        await stopSedJob(id);
        showToast(`任务 #${id} 已中断`, 'success');
        await refreshJobs();
      } catch (err) {
        showToast(`中断失败: ${err.message}`, 'danger');
      }
    });
  });
  body.querySelectorAll('.sed-del').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = parseInt(btn.dataset.jobid, 10);
      if (!confirm(`删除 SED 拟合任务 #${id} 及其产物？`)) return;
      try {
        await deleteSedJob(id);
        showToast(`任务 #${id} 已删除`, 'success');
        if (_selectedId === id) {
          _selectedId = null;
          const area = document.getElementById('sedResultArea');
          if (area) area.innerHTML = '';
        }
        await refreshJobs();
      } catch (err) {
        showToast(`删除失败: ${err.message}`, 'danger');
      }
    });
  });
  // 行点击：done → 查看结果；failed/interrupted → 展开错误（中断任务显示「用户手动中断」）
  body.querySelectorAll('tr[data-jobid]:not(.sed-err-row)').forEach(row => {
    row.addEventListener('click', async () => {
      const id = parseInt(row.dataset.jobid, 10);
      const job = _jobs.find(j => j.id === id);
      if (!job) return;
      if (job.status === 'done') loadResult(id);
      else if (['failed', 'interrupted'].includes(job.status)) {
        const errRow = body.querySelector(`.sed-err-row[data-jobid="${id}"]`);
        if (!errRow) return;
        const show = errRow.style.display === 'none';
        errRow.style.display = show ? '' : 'none';
        if (show) {
          try {
            const d = await getSedJob(id);
            const cell = document.getElementById(`sedErr_${id}`);
            if (cell) cell.textContent = d.error || '（无错误信息）';
          } catch (e) {
            const cell = document.getElementById(`sedErr_${id}`);
            if (cell) cell.textContent = `错误信息加载失败: ${e.message}`;
          }
        }
      }
    });
  });
}

// ─── ③ 结果展示 ───
async function loadResult(jobId) {
  const area = document.getElementById('sedResultArea');
  if (!area) return;
  const req = ++_resultReqId;
  _selectedId = jobId;
  renderJobs();  // 高亮选中行
  area.innerHTML = `<div class="card"><div class="card-body text-secondary small">
    <span class="spinner-border spinner-border-sm"></span> 加载任务 #${jobId} 结果...</div></div>`;

  let detail;
  try {
    detail = await getSedJob(jobId);
  } catch (e) {
    if (req !== _resultReqId) return;
    area.innerHTML = `<div class="card"><div class="card-body text-danger small">结果加载失败: ${esc(e.message)}</div></div>`;
    return;
  }
  if (req !== _resultReqId) return;  // 已切换到其他任务，丢弃过期结果
  if (detail.model_name === 'sed_blackbody_series') renderSeriesResult(detail);
  else renderSingleResult(detail);
}

// 派生量统一渲染（数值 sci3；布尔 是/否；字符串转义）
function _derivedHtml(derived) {
  const entries = Object.entries(derived || {});
  if (!entries.length) return '';
  const rows = entries.map(([k, v]) => {
    let txt;
    if (v == null) txt = '-';
    else if (typeof v === 'boolean') txt = v ? '是' : '否';
    else if (typeof v === 'number') txt = sci3(v);
    else txt = esc(v);
    return `<span class="badge-tag badge-neutral" title="${escAttr(k)}">${esc(k)} = ${txt}</span>`;
  }).join(' ');
  return `<div class="mb-2"><span class="small text-secondary">派生量：</span><div class="d-flex flex-wrap gap-1 mt-1">${rows}</div></div>`;
}

// 可信度元数据折叠块（§4.4：λ 覆盖 / 消光假设 / z 假设 / 插值窗口）
function _metaHtml(meta) {
  if (!meta) return '';
  const rows = [];
  if (meta.lambda_min_a != null) rows.push(`λ 覆盖 ${sci3(meta.lambda_min_a)}–${sci3(meta.lambda_max_a)} Å`);
  rows.push(meta.has_uv ? '含 UV（<3000 Å）' : '无 UV 覆盖');
  rows.push(meta.has_ir ? '含 IR（>10⁴ Å）' : '无 IR 覆盖');
  if (meta.host_law) rows.push(`宿主消光律 ${esc(meta.host_law)}（R_V: ${esc(String(meta.rv))}）`);
  if (meta.z_assumption) rows.push(`z=${meta.z_assumption.z != null ? sci3(meta.z_assumption.z) : '无'}（${esc(meta.z_assumption.source || '未知来源')}）`);
  if (meta.window_days) rows.push(`取数窗口 [${fmtNum(meta.window_days[0], 4)}, ${fmtNum(meta.window_days[1], 4)}] d`);
  rows.push(meta.mode === 'interp' ? 'GP 内插模式' : '时段取值模式');
  rows.push(meta.gext_used ? '已用银消改正列' : '未用银消改正列');
  if (meta.bandpass_synth != null) rows.push(meta.bandpass_synth ? '通带综合已启用' : '通带综合未启用');
  if (meta.version) rows.push(esc(meta.version));
  return `
    <details class="mt-2">
      <summary class="small text-secondary" style="cursor:pointer">可信度元数据（λ 覆盖 / 消光假设 / z 假设 / 取数窗口）</summary>
      <div class="small mt-1">${rows.map(r => `<span class="badge-tag badge-neutral me-1 mb-1">${r}</span>`).join('')}</div>
    </details>`;
}

// 下载行：按任务实际产物列出
function _downloadsHtml(detail, kinds) {
  const files = detail.files || {};
  const links = kinds.filter(k => files[k]).map(k =>
    `<a class="btn btn-sm btn-outline-secondary" href="${sedJobFileUrl(detail.id, k)}" download>
      <i class="bi bi-download"></i> ${esc(k)}</a>`);
  return links.length
    ? `<div class="mt-3 d-flex flex-wrap gap-2">${links.join('')}</div>` : '';
}

function renderSingleResult(detail) {
  const area = document.getElementById('sedResultArea');
  if (!area) return;
  const ts = Date.now();  // 防缓存
  const params = detail.parameters || {};
  const warnings = detail.warnings || [];
  const files = detail.files || {};
  const stats = [
    ['χ²', detail.chi_squared != null ? sci3(detail.chi_squared) : '-'],
    ['dof', detail.dof ?? '-'],
    ['BIC', detail.bic != null ? sci3(detail.bic) : '-'],
    ['AIC', detail.aic != null ? sci3(detail.aic) : '-'],
    ['耗时', fmtRuntime(detail.runtime_s)],
  ];
  // 幂律系模型允许把 β 一键填入闭包诊断
  const betaName = params.beta ? 'beta' : null;

  area.innerHTML = `
    <div class="card">
      <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
        <span><i class="bi bi-graph-up-arrow"></i> 拟合结果 — 任务 #${detail.id}
          <span class="text-secondary small">${esc(modelLabel(detail.model_name))}
            ${detail.config && detail.config.t_sel != null ? ` · t=${sci3(detail.config.t_sel)} d` : ''}</span></span>
        ${betaName ? `<button class="btn btn-sm btn-outline-primary" id="sedFillBeta">
          <i class="bi bi-box-arrow-down"></i> 填入闭包诊断 β</button>` : ''}
      </div>
      <div class="card-body">
        <div class="d-flex flex-wrap gap-2 mb-2">
          ${stats.map(([k, v]) => `<span class="badge-tag badge-neutral">${k} = ${v}</span>`).join('')}
        </div>
        ${warnings.length ? `<div class="alert alert-warning py-2 small">
          <i class="bi bi-exclamation-triangle"></i> ${warnings.map(w => esc(w)).join('<br>')}</div>` : ''}
        ${_derivedHtml(detail.derived)}
        <div class="row g-3">
          <div class="col-lg-5">
            <div class="table-scroll" style="max-height:340px;overflow-y:auto">
              <table class="table table-sm mb-0" style="font-size:0.8rem">
                <thead><tr><th>参数</th><th>最大似然值 ± 1σ</th></tr></thead>
                <tbody>
                  ${Object.entries(params).map(([name, p]) => `
                    <tr><td>${esc(name)}</td><td>${sci3(p.v)} ± ${sci3(p.err)}</td></tr>`).join('')}
                </tbody>
              </table>
            </div>
            ${_metaHtml(detail.result_meta)}
          </div>
          <div class="col-lg-7">
            <div class="row g-2">
              ${files.sed_png ? `
              <div class="col-md-6 text-center">
                <a href="${sedJobFileUrl(detail.id, 'sed_png')}" target="_blank" title="新标签页查看原图">
                  <img src="${sedJobFileUrl(detail.id, 'sed_png')}?t=${ts}" class="rounded border" alt="SED fit"
                       style="max-height:380px;width:auto;max-width:100%;display:block;margin:0 auto"
                       onerror="this.outerHTML='<div class=\\'text-secondary small\\'>SED 图加载失败</div>'">
                </a>
                <div class="small text-secondary mt-1">SED 拟合图（数据 + 最佳模型 + 68% 可信带 + 残差），点击看原图</div>
              </div>` : ''}
              ${files.corner ? `
              <div class="col-md-6 text-center">
                <a href="${sedJobFileUrl(detail.id, 'corner')}" target="_blank" title="新标签页查看原图">
                  <img src="${sedJobFileUrl(detail.id, 'corner')}?t=${ts}" class="rounded border" alt="corner plot"
                       style="max-height:380px;width:auto;max-width:100%;display:block;margin:0 auto"
                       onerror="this.outerHTML='<div class=\\'text-secondary small\\'>角图加载失败</div>'">
                </a>
                <div class="small text-secondary mt-1">后验角图（corner；β–A_V 简并检查），点击看原图</div>
              </div>` : ''}
            </div>
          </div>
        </div>
        ${_downloadsHtml(detail, ['result', 'sed_csv', 'h5', 'log'])}
      </div>
    </div>`;

  const fillBtn = document.getElementById('sedFillBeta');
  if (fillBtn && betaName) {
    fillBtn.addEventListener('click', () => {
      const p = params[betaName] || {};
      const bEl = document.getElementById('sedBeta');
      const beEl = document.getElementById('sedBetaErr');
      if (bEl && p.v != null) bEl.value = String(p.v);
      if (beEl && p.err != null) beEl.value = String(p.err);
      showToast(`已填入 β=${sci3(p.v)} ± ${sci3(p.err)}（任务 #${detail.id}，${betaName}）`, 'success');
      document.getElementById('sedAlpha')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }
}

// 多历元黑体序列结果：T/R/L(t) 表 + trl.png / series.png
function renderSeriesResult(detail) {
  const area = document.getElementById('sedResultArea');
  if (!area) return;
  const ts = Date.now();
  const warnings = detail.warnings || [];
  const files = detail.files || {};
  const epochs = detail.epochs || [];
  const stats = [
    ['历元（成功/总数）', `${(detail.parameters || {}).n_epochs_done ?? '-'}/${(detail.parameters || {}).n_epochs_total ?? '-'}`],
    ['耗时', fmtRuntime(detail.runtime_s)],
  ];
  area.innerHTML = `
    <div class="card">
      <div class="card-header">
        <i class="bi bi-graph-up-arrow"></i> 拟合结果 — 任务 #${detail.id}
        <span class="text-secondary small">${esc(modelLabel(detail.model_name))}</span>
      </div>
      <div class="card-body">
        <div class="d-flex flex-wrap gap-2 mb-2">
          ${stats.map(([k, v]) => `<span class="badge-tag badge-neutral">${k} = ${v}</span>`).join('')}
        </div>
        ${warnings.length ? `<div class="alert alert-warning py-2 small" style="max-height:160px;overflow-y:auto">
          <i class="bi bi-exclamation-triangle"></i> ${warnings.map(w => esc(w)).join('<br>')}</div>` : ''}
        <div class="row g-3">
          ${files.trl_png ? `
          <div class="col-lg-6 text-center">
            <a href="${sedJobFileUrl(detail.id, 'trl_png')}" target="_blank" title="新标签页查看原图">
              <img src="${sedJobFileUrl(detail.id, 'trl_png')}?t=${ts}" class="rounded border" alt="TRL"
                   style="max-height:420px;width:auto;max-width:100%;display:block;margin:0 auto"
                   onerror="this.outerHTML='<div class=\\'text-secondary small\\'>三联图加载失败</div>'">
            </a>
            <div class="small text-secondary mt-1">T / R / L(t) 三联图，点击看原图</div>
          </div>` : ''}
          ${files.series_png ? `
          <div class="col-lg-6 text-center">
            <a href="${sedJobFileUrl(detail.id, 'series_png')}" target="_blank" title="新标签页查看原图">
              <img src="${sedJobFileUrl(detail.id, 'series_png')}?t=${ts}" class="rounded border" alt="series SED"
                   style="max-height:420px;width:auto;max-width:100%;display:block;margin:0 auto"
                   onerror="this.outerHTML='<div class=\\'text-secondary small\\'>叠图加载失败</div>'">
            </a>
            <div class="small text-secondary mt-1">全历元 SED 叠图（按 log t 着色），点击看原图</div>
          </div>` : ''}
        </div>
        <div class="table-scroll mt-3" style="max-height:300px;overflow-y:auto">
          <table class="table table-sm mb-0" style="font-size:0.78rem">
            <thead><tr><th>t (d)</th><th>T (K)</th><th>R (cm)</th><th>L (erg/s)</th><th>波段数</th><th>λ 覆盖 (Å)</th><th>χ²/dof</th><th>备注</th></tr></thead>
            <tbody>
              ${epochs.map(e => `<tr>
                <td>${sci3(e.t_days)}</td>
                <td>${e.T != null ? `${sci3(e.T)} ± ${sci3(e.T_err)}` : '-'}</td>
                <td>${e.R != null ? `${sci3(e.R)} ± ${sci3(e.R_err)}` : '-'}</td>
                <td>${e.L != null ? sci3(e.L) : '-'}</td>
                <td>${e.n_bands ?? '-'}</td>
                <td>${e.lambda_min_a != null ? `${sci3(e.lambda_min_a)}–${sci3(e.lambda_max_a)}` : '-'}</td>
                <td>${e.chi2 != null ? `${sci3(e.chi2)}/${e.dof ?? '-'}` : '-'}</td>
                <td class="text-danger">${esc(e.error || '')}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
        ${_downloadsHtml(detail, ['result', 'series_csv', 'log'])}
      </div>
    </div>`;
}

// ─── ④a 闭包关系 α–β 诊断 ───
async function doClosure() {
  const out = document.getElementById('sedClosureOut');
  const btn = document.getElementById('sedClosureBtn');
  const alpha = numOrNull(document.getElementById('sedAlpha')?.value);
  const beta = numOrNull(document.getElementById('sedBeta')?.value);
  if (alpha == null || beta == null) { showToast('请填写 α 与 β（数值）', 'warning'); return; }
  // σ 可空：空或非正数 → body 不带该字段（后端按点估计偏差处理）
  const body = { alpha, beta };
  const aErr = numOrNull(document.getElementById('sedAlphaErr')?.value);
  const bErr = numOrNull(document.getElementById('sedBetaErr')?.value);
  if (aErr != null && aErr > 0) body.alpha_err = aErr;
  if (bErr != null && bErr > 0) body.beta_err = bErr;
  // q 可空：填写才传（0≤q<1，注入闭包关系才参与排名）
  const q = numOrNull(document.getElementById('sedClosureQ')?.value);
  if (q != null) {
    if (!(q >= 0 && q < 1)) { showToast('q（能量注入指数）必须满足 0 ≤ q < 1', 'warning'); return; }
    body.q = q;
  }
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 诊断中...';
  try {
    const res = await sedClosure(body);
    // α–β 诊断图（PNG → blob URL；失败不阻断排名表）
    let imgUrl = null;
    try {
      imgUrl = await sedClosurePlot(body);
    } catch (e) {
      console.warn('closure_plot 生成失败:', e);
    }
    if (!document.getElementById('sedClosureOut')) {
      if (imgUrl) URL.revokeObjectURL(imgUrl);
      return;
    }
    if (_closureImgUrl) URL.revokeObjectURL(_closureImgUrl);
    _closureImgUrl = imgUrl;
    renderClosureResult(res, _closureImgUrl);
  } catch (e) {
    showToast(`闭包诊断失败: ${e.message}`, 'danger');
    if (out) out.innerHTML = `<div class="text-danger small">诊断失败: ${esc(e.message)}</div>`;
  } finally {
    if (btn.isConnected) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-play-fill"></i> 诊断';
    }
  }
}

function renderClosureResult(res, imgUrl) {
  const out = document.getElementById('sedClosureOut');
  if (!out) return;
  const ranking = res.ranking || [];
  const best = res.best || {};
  const pc = res.p_candidates || {};
  const inp = res.input || {};
  const note = res.note || (ranking.find(r => r.note) || {}).note || best.note || null;
  const noErrBadge = '<span class="badge bg-secondary ms-1">无误差</span>';
  // 偏差单元格：有 σ 显示 σ 倍数；无 σ 显示点估计 |Δα| + 「无误差」badge
  const devCell = (r) => (r.sigma_dev != null && isFinite(r.sigma_dev))
    ? sci3(r.sigma_dev)
    : `|Δα|=${sci3(r.abs_dev)}${noErrBadge}`;
  const bestDev = (best.sigma_dev != null && isFinite(best.sigma_dev))
    ? `偏差 ${sci3(best.sigma_dev)}σ`
    : `点估计偏差 |Δα|=${sci3(best.abs_dev)}`;
  out.innerHTML = `
    ${note ? `<div class="alert alert-info py-2 small mb-2">
      <i class="bi bi-info-circle"></i> ${esc(note)}</div>` : ''}
    ${best.id ? `<div class="alert alert-success py-2 small">
      <i class="bi bi-check-circle"></i> 最优组合：<b>${esc(best.label)}</b>
      （${bestDev}${best.p != null ? `，p=${sci3(best.p)}` : ''}）</div>` : ''}
    <div class="small mb-2">
      p 候选（由 β=${sci3(inp.beta)} 反解）：
      <span class="badge-tag badge-neutral">ν_m&lt;ν&lt;ν_c：p = 2β+1 = ${sci3(pc.below_nuc)}</span>
      <span class="badge-tag badge-neutral">备选映射：p = 2β+2 = ${sci3(pc.slow_above_nuc)}</span>
    </div>
    <div class="table-scroll" style="max-height:280px;overflow-y:auto">
      <table class="table table-sm mb-0" style="font-size:0.76rem">
        <thead><tr><th>关系</th><th>介质</th><th>冷却</th><th>谱段</th><th>α_pred</th><th>偏差 σ</th><th>p</th></tr></thead>
        <tbody>
          ${ranking.map((r, i) => `<tr class="${i === 0 ? 'table-success' : ''}">
            <td title="${escAttr(r.ref || '')}">${esc(r.label)}${r.injection === true ? ' <span class="badge bg-info text-dark">注入</span>' : ''}</td>
            <td>${esc(r.medium)}</td>
            <td>${esc(r.regime)}</td>
            <td class="text-nowrap">${esc(r.segment)}</td>
            <td>${sci3(r.alpha_pred)}</td>
            <td>${devCell(r)}${r.beta_dev != null ? `<br><span class="text-secondary" title="固定 β 段的 β 偏差">β_dev=${sci3(r.beta_dev)}</span>` : ''}</td>
            <td>${r.p != null ? sci3(r.p) : '-'}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>
    ${imgUrl ? `<div class="small text-secondary mt-2 mb-1">α–β 诊断图（理论线 + 数据点）</div>
      <img src="${imgUrl}" class="img-fluid rounded border" alt="closure plot">` : ''}
    <details class="mt-2">
      <summary class="small text-secondary" style="cursor:pointer">闭包关系系数表（审校用）</summary>
      <div id="sedClosureTable" class="small mt-1 text-secondary">加载中...</div>
    </details>`;
  _fillClosureTable();
}

async function _fillClosureTable() {
  const el = document.getElementById('sedClosureTable');
  if (!el) return;
  try {
    if (!_closureTable) _closureTable = await getSedClosureRelations();
    const rels = (_closureTable && _closureTable.relations) || [];
    el.innerHTML = `
      <div class="table-scroll" style="max-height:220px;overflow-y:auto">
        <table class="table table-sm mb-0" style="font-size:0.74rem">
          <thead><tr><th>id</th><th>α = a·β + b</th><th>p 映射</th><th>ref</th></tr></thead>
          <tbody>
            ${rels.map(r => `<tr>
              <td>${esc(r.id)}${r.injection === true ? ' <span class="badge bg-info text-dark">注入</span>' : ''}</td>
              <td>${r.alpha && r.alpha.a0 != null
                ? `(${sci3(r.alpha.a0)}${r.alpha.qa >= 0 ? '+' : '−'}${sci3(Math.abs(r.alpha.qa))}q)·β ${r.alpha.b0 >= 0 ? '+' : '−'} ${sci3(Math.abs(r.alpha.b0))}${r.alpha.qb >= 0 ? '+' : '−'}${sci3(Math.abs(r.alpha.qb))}q`
                : `${sci3(r.alpha && r.alpha.a)}·β ${r.alpha && r.alpha.b >= 0 ? '+' : '−'} ${sci3(Math.abs(r.alpha && r.alpha.b || 0))}`}</td>
              <td>${esc((r.p) || '（p 无关）')}</td>
              <td class="text-secondary">${esc(r.ref || '')}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>
      <div class="text-secondary mt-1" style="font-size:0.72rem">${esc(((_closureTable || {})._meta || {}).description || '')}</div>`;
  } catch (e) {
    el.innerHTML = `<span class="text-danger">系数表加载失败: ${esc(e.message)}</span>`;
  }
}

// ─── ④b 伪玻尔兹曼光变 ───
async function doBolometric() {
  const out = document.getElementById('sedBoloOut');
  const btn = document.getElementById('sedBoloBtn');
  const dtFrac = numOrNull(document.getElementById('sedBoloDtFrac')?.value);
  if (dtFrac != null && dtFrac <= 0) { showToast('Δt/t 必须 > 0', 'warning'); return; }
  const body = {
    transient_id: _tid,
    bc_sample: document.getElementById('sedBcSample')?.value || 'all',
  };
  if (dtFrac != null) body.dt_frac = dtFrac;
  const z = numOrNull(document.getElementById('sedBoloZ')?.value);
  if (z != null) body.z_override = z;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 计算中（逐历元黑体网格拟合）...';
  try {
    const res = await sedBolometric(body);
    renderBoloResult(res);
  } catch (e) {
    showToast(`伪玻尔兹曼光变计算失败: ${e.message}`, 'danger');
    if (out) out.innerHTML = `<div class="text-danger small">计算失败: ${esc(e.message)}</div>`;
  } finally {
    if (btn.isConnected) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-play-fill"></i> 计算';
    }
  }
}

function renderBoloResult(res) {
  const out = document.getElementById('sedBoloOut');
  if (!out) return;
  const epochs = res.epochs || [];
  const warnings = res.warnings || [];
  const hasL = epochs.some(e => e.L_obs != null || e.L_bb != null || e.L_bc != null);
  const hasTR = epochs.some(e => e.T_bb != null);
  out.innerHTML = `
    <div class="d-flex flex-wrap gap-1 mb-2">
      <span class="badge-tag badge-neutral">${epochs.length} 历元</span>
      <span class="badge-tag badge-neutral">${res.z != null ? `z=${sci3(res.z)}（${esc(res.z_source || '')}）` : '无红移（光度全为 null）'}</span>
      <span class="badge-tag badge-neutral">BC 样本 ${esc(res.bc_sample || '')}</span>
    </div>
    ${warnings.length ? `<div class="alert alert-warning py-2 small">
      <i class="bi bi-exclamation-triangle"></i> ${warnings.map(w => esc(w)).join('<br>')}</div>` : ''}
    ${hasL ? `
    <div class="small text-secondary mb-1">L(t)：L_obs（观测窗口积分）/ L_bb（黑体外推）/ L_bc（Lyman+2014 颜色 BC）</div>
    <div style="position:relative;height:280px"><canvas id="sedLbolChart"></canvas></div>` : ''}
    ${hasTR ? `
    <div class="row g-2 mt-2">
      <div class="col-6">
        <div class="small text-secondary mb-1">T<sub>BB</sub>(t)</div>
        <div style="position:relative;height:200px"><canvas id="sedTbbChart"></canvas></div>
      </div>
      <div class="col-6">
        <div class="small text-secondary mb-1">R<sub>BB</sub>(t)</div>
        <div style="position:relative;height:200px"><canvas id="sedRbbChart"></canvas></div>
      </div>
    </div>` : ''}
    <div class="d-flex justify-content-between align-items-center mt-3 mb-1">
      <span class="small text-secondary">历元表</span>
      <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="sedBoloCsvBtn"><i class="bi bi-download"></i> 下载 CSV</button>
    </div>
    <div class="table-scroll" style="max-height:260px;overflow-y:auto">
      <table class="table table-sm mb-0" style="font-size:0.74rem">
        <thead><tr><th>t (d)</th><th>波段数</th><th>λ 覆盖 (Å)</th><th>L_obs</th><th>L_bb</th><th>L_bc</th><th>T_bb (K)</th><th>R_bb (cm)</th></tr></thead>
        <tbody>
          ${epochs.map(e => `<tr>
            <td>${sci3(e.t_days)}</td>
            <td>${e.n_bands ?? '-'}</td>
            <td>${e.lambda_min_a != null ? `${sci3(e.lambda_min_a)}–${sci3(e.lambda_max_a)}` : '-'}</td>
            <td>${e.L_obs != null ? sci3(e.L_obs) : '-'}</td>
            <td>${e.L_bb != null ? sci3(e.L_bb) : '-'}</td>
            <td>${e.L_bc != null ? `${sci3(e.L_bc)}${e.bc_extrapolated ? '<span class="text-warning" title="颜色超出 Lyman+2014 拟合范围，外推值">*</span>' : ''}` : '-'}</td>
            <td>${e.T_bb != null ? sci3(e.T_bb) : '-'}</td>
            <td>${e.R_bb != null ? sci3(e.R_bb) : '-'}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>
    <div class="text-secondary mt-1" style="font-size:0.72rem">L 单位 erg/s；* = BC 外推值。逐历元 warnings 见后端记录。</div>`;

  document.getElementById('sedBoloCsvBtn')?.addEventListener('click', () => {
    downloadText(`${_tid}_bolometric.csv`, _boloCsvText(res));
  });
  if (hasL) buildLbolChart(epochs);
  if (hasTR) buildTRCharts(epochs);
}

// 伪玻尔兹曼光变 CSV 序列化（口径对齐后端 bolometric_to_csv）
function _boloCsvText(res) {
  const cols = ['t_days', 'n_bands', 'lambda_min_a', 'lambda_max_a', 'L_obs', 'L_obs_err',
                'T_bb', 'T_bb_err', 'R_bb', 'R_bb_err', 'L_bb', 'L_bb_err',
                'L_bc', 'L_bc_err', 'bc_color', 'bc_extrapolated'];
  const lines = [
    '# AJST 伪玻尔兹曼光变',
    `# transient_id=${res.transient_id} z=${res.z} z_source=${res.z_source} bc_sample=${res.bc_sample}`,
    `# bc_ref=${res.bc_ref}`,
    cols.join(','),
  ];
  for (const e of (res.epochs || [])) {
    lines.push(cols.map(k => _csvField(e[k] == null ? '' : e[k])).join(','));
  }
  return lines.join('\n') + '\n';
}

// 取有效点序列：[{x, y, err}]（log 轴要求 y>0）
function _seriesPts(epochs, vKey, eKey) {
  return epochs
    .filter(e => e[vKey] != null && e[vKey] > 0)
    .map(e => ({ x: e.t_days, y: e[vKey], err: (e[eKey] > 0 ? e[eKey] : null) }));
}

function buildLbolChart(epochs) {
  if (_lbolChart) { try { _lbolChart.destroy(); } catch {} _lbolChart = null; }
  const canvas = document.getElementById('sedLbolChart');
  if (!canvas) return;
  if (typeof Chart === 'undefined') return;
  const cc = chartColors();
  const mk = (label, key, ekey, color, style) => {
    const data = _seriesPts(epochs, key, ekey);
    if (!data.length) return null;
    return {
      label, data, showLine: true, borderColor: color, backgroundColor: color,
      pointStyle: style, pointRadius: 4, borderWidth: 1.5, fill: false,
    };
  };
  const datasets = [
    mk('L_obs（窗口积分）', 'L_obs', 'L_obs_err', cc.compare[0], 'circle'),
    mk('L_bb（黑体外推）', 'L_bb', 'L_bb_err', cc.compare[2], 'rectRot'),
    mk('L_bc（Lyman+2014）', 'L_bc', 'L_bc_err', cc.compare[4], 'triangle'),
  ].filter(Boolean);
  const errPlugin = createYErrBarPlugin({ errOf: (ds, raw) => raw.err });
  _lbolChart = new Chart(canvas.getContext('2d'), {
    type: 'scatter',
    data: { datasets },
    plugins: [errPlugin, dragRectPlugin],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: _chartPluginsCfg((ctx) => {
        const p = ctx.raw;
        const errTxt = p.err != null ? `±${sci3(p.err)}` : '';
        return `${ctx.dataset.label}  t=${sci3(p.x)} d  L=${sci3(p.y)}${errTxt} erg/s`;
      }),
      scales: _logScales('t [d]', 'L [erg/s]'),
    },
  });
  _lbolChartHolder.chart = _lbolChart;
  _attachLogDragZoom(_lbolChartHolder, canvas);
}

// T_BB(t) / R_BB(t) 小图（上下左右并排，log-log）
function buildTRCharts(epochs) {
  for (const [varName, canvasId, key, ekey, yTitle] of [
    ['_tbbChart', 'sedTbbChart', 'T_bb', 'T_bb_err', 'T [K]'],
    ['_rbbChart', 'sedRbbChart', 'R_bb', 'R_bb_err', 'R [cm]'],
  ]) {
    const old = varName === '_tbbChart' ? _tbbChart : _rbbChart;
    if (old) { try { old.destroy(); } catch {} }
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === 'undefined') continue;
    const data = _seriesPts(epochs, key, ekey);
    if (!data.length) continue;
    const cc = chartColors();
    const errPlugin = createYErrBarPlugin({ errOf: (ds, raw) => raw.err });
    const ch = new Chart(canvas.getContext('2d'), {
      type: 'scatter',
      data: {
        datasets: [{
          label: yTitle, data, showLine: true,
          borderColor: cc.compare[1], backgroundColor: cc.compare[1],
          pointRadius: 3.5, borderWidth: 1.5, fill: false,
        }],
      },
      plugins: [errPlugin],
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
            callbacks: {
              label: (ctx) => {
                const p = ctx.raw;
                return `t=${sci3(p.x)} d  ${sci3(p.y)}${p.err != null ? ` ± ${sci3(p.err)}` : ''}`;
              },
            },
          },
        },
        scales: _logScales('t [d]', yTitle),
      },
    });
    if (varName === '_tbbChart') _tbbChart = ch; else _rbbChart = ch;
  }
}
