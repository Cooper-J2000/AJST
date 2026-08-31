// === 余辉拟合标签页（详情页内嵌） ===
// 由 detail.js 在切换到「余辉拟合」标签时调用 initFittingTab(container, tid)，
// 切走或页面重渲染时调用 destroyFittingTab() 停止轮询、销毁图表。
import {
  isAuthed, isAdmin, showToast, getLightcurves,
  getFittingEngines, submitFittingJob, getFittingJobs, getFittingJob,
  getFittingJobFile, deleteFittingJob,
} from '../api.js';
import { chartColors, academicFonts } from '../theme.js';

const POLL_INTERVAL = 5000;
const BAND_COLORS = chartColors().bands;   // 波段配色（随主题；切换主题后 reload 生效）

let _tid = null;
let _engines = null;        // GET /fitting/engines 缓存
let _jobs = [];             // 任务列表
let _pollTimer = null;
let _chart = null;
let _selectedId = null;     // 当前展开结果的任务 id
let _modelSel = null;       // 当前模型情形选择
let _engineName = null;     // 当前选中的拟合引擎
let _lcBands = null;        // {band: [{x,y,err,isUL,id}]} 光变数据（mJy），叠加图/数据选取用
let _bandColorMap = {};     // band → color
let _axisRange = { xmin: null, xmax: null, ymin: null, ymax: null };  // 叠加图手动范围
let _selChart = null;       // 数据选取预览散点图
let _selExcluded = new Set();  // 数据选取：手动排除的 lightcurve id

// ─── 叠加图坐标范围（手动输入 + 框选缩放共用） ───
function _applyAxisRangeToChart() {
  if (!_chart) return;
  const sx = _chart.options.scales.x, sy = _chart.options.scales.y;
  for (const [k, v] of Object.entries(_axisRange)) {
    const scale = k.startsWith('x') ? sx : sy;
    const prop = k.endsWith('min') ? 'min' : 'max';
    if (v == null) delete scale[prop];
    else scale[prop] = v;
  }
  _chart.update('none');
}

function _fillAxisInputs() {
  const ids = { xmin: 'fitAxXmin', xmax: 'fitAxXmax', ymin: 'fitAxYmin', ymax: 'fitAxYmax' };
  for (const [k, id] of Object.entries(ids)) {
    const el = document.getElementById(id);
    if (el) el.value = _axisRange[k] != null ? sci3(_axisRange[k]) : '';
  }
}

window.fitAxisApply = () => {
  const v = (id) => {
    const s = (document.getElementById(id)?.value || '').trim();
    if (s === '') return null;
    const n = parseFloat(s);
    if (!isFinite(n)) return undefined;  // 非法输入
    return n;
  };
  const vals = { xmin: v('fitAxXmin'), xmax: v('fitAxXmax'), ymin: v('fitAxYmin'), ymax: v('fitAxYmax') };
  if (Object.values(vals).some(x => x === undefined)) { showToast('坐标范围含非法数值', 'warning'); return; }
  const r = { ...vals };
  if ((r.xmin != null && r.xmin <= 0) || (r.xmax != null && r.xmax <= 0) ||
      (r.ymin != null && r.ymin <= 0) || (r.ymax != null && r.ymax <= 0)) {
    showToast('log 轴范围必须为正值', 'warning'); return;
  }
  if ((r.xmin != null && r.xmax != null && r.xmin >= r.xmax) ||
      (r.ymin != null && r.ymax != null && r.ymin >= r.ymax)) {
    showToast('min 需小于 max', 'warning'); return;
  }
  _axisRange = r;
  _applyAxisRangeToChart();
};

window.fitAxisReset = () => {
  _axisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  _fillAxisInputs();
  _applyAxisRangeToChart();
};

// 误差棒开关（数据选取预览图 / 拟合结果图共用状态）：只影响绘制，无动画重绘即可
window.fitSelErrToggle = (on) => {
  _lcShowErr = on;
  if (_selChart) _selChart.update('none');
};
window.fitLcErrToggle = (on) => {
  _lcShowErr = on;
  if (_chart) _chart.update('none');
};

// 框选缩放：在绘图区按住左键拖出矩形，松开后放大到该区域
const _dragRectPlugin = {
  id: 'fitDragRect',
  afterDraw(chart) {
    const r = chart._fitDragRect;
    if (!r) return;
    const cc = chartColors();
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = cc.selectFill;
    ctx.strokeStyle = cc.selectStroke;
    ctx.setLineDash([4, 3]);
    const x = Math.min(r.x0, r.x1), y = Math.min(r.y0, r.y1);
    ctx.fillRect(x, y, Math.abs(r.x1 - r.x0), Math.abs(r.y1 - r.y0));
    ctx.strokeRect(x, y, Math.abs(r.x1 - r.x0), Math.abs(r.y1 - r.y0));
    ctx.restore();
  },
};

function _attachDragZoom(chart, canvas) {
  if (canvas._fitDragZoomOn) return;   // 同一 canvas 只挂一次
  canvas._fitDragZoomOn = true;
  let start = null;
  const rel = (e) => {
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  };
  canvas.addEventListener('mousedown', (e) => {
    const a = chart.chartArea;
    if (!a) return;
    const p = rel(e);
    if (p.x >= a.left && p.x <= a.right && p.y >= a.top && p.y <= a.bottom) {
      start = p;
      e.preventDefault();
    }
  });
  canvas.ownerDocument.addEventListener('mousemove', (e) => {
    if (!start) return;
    const p = rel(e);
    chart._fitDragRect = { x0: start.x, y0: start.y, x1: p.x, y1: p.y };
    chart.draw();
  });
  canvas.ownerDocument.addEventListener('mouseup', (e) => {
    if (!start) return;
    start = null;
    const r = chart._fitDragRect;
    chart._fitDragRect = null;
    chart.draw();
    if (!r || Math.abs(r.x1 - r.x0) < 8 || Math.abs(r.y1 - r.y0) < 8) return;
    const xs = chart.scales.x, ys = chart.scales.y;
    const xmin = xs.getValueForPixel(Math.min(r.x0, r.x1));
    const xmax = xs.getValueForPixel(Math.max(r.x0, r.x1));
    const ymax = ys.getValueForPixel(Math.min(r.y0, r.y1));  // 像素小=值大
    const ymin = ys.getValueForPixel(Math.max(r.y0, r.y1));
    if (![xmin, xmax, ymin, ymax].every(v => isFinite(v) && v > 0)) return;
    _axisRange = { xmin, xmax, ymin, ymax };
    _fillAxisInputs();
    _applyAxisRangeToChart();
  });
}

// ─── 工具 ───
function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function sci3(v) {
  if (v == null || !isFinite(v)) return '-';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 0.01 && a < 10000) return String(parseFloat(Number(v).toPrecision(3)));
  return Number(v).toExponential(2);
}

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

// ─── 光变数据 → mJy（与 detail.js 的换算口径一致：优先银消改正值） ───
function magABtoMJy(mag) { return Math.pow(10, (16.4 - mag) / 2.5); }

function toMJy(value, unit) {
  if (value == null) return null;
  switch (unit) {
    case 'mJy': return value;
    case 'uJy': return value * 1e-3;
    case 'Jy': return value * 1e3;
    case 'cgs': case 'erg/cm2/s/Hz': case 'cgs(erg/cm2/s/Hz)': return value * 1e26;
    default: return value;
  }
}

// 返回 {y, err}；星等误差按 σ_F = F·ln10·σ_m/2.5 换算（与 detail.js 口径一致）
function pointToMJy(p) {
  let y = null, err = null;
  if (p.gext_corr && p.flux_density_gextcor != null) {
    y = p.flux_density_gextcor;
    err = p.flux_density_gextcor_err != null ? p.flux_density_gextcor_err : null;
  } else if (p.gext_corr && p.mag_gextcor != null) {
    y = magABtoMJy(p.mag_gextcor);
    err = p.mag_gextcor_err != null ? (Math.LN10 / 2.5) * y * p.mag_gextcor_err : null;
  } else if (p.flux_density_unit === 'mag' || p.flux_density_unit === 'magnitude') {
    y = magABtoMJy(p.flux_density);
    err = p.flux_density_err != null ? (Math.LN10 / 2.5) * y * p.flux_density_err : null;
  } else {
    y = toMJy(p.flux_density, p.flux_density_unit);
    err = p.flux_density_err != null ? toMJy(p.flux_density_err, p.flux_density_unit) : null;
  }
  if (y == null || !isFinite(y)) return null;
  if (y <= 0) y = 1e-6;
  return { y, err };
}

// ─── 误差棒插件：数据点带 err 字段时绘制竖直误差棒（画在数据点下层；上限点与模型线不画） ───
let _lcShowErr = true;   // 是否绘制误差棒（选取预览图与结果图共用此开关状态）
const _lcErrorBarPlugin = {
  id: 'errorBar',
  beforeDatasetsDraw(chart) {
    if (!_lcShowErr) return;
    try {
    const ctx = chart.ctx, yScale = chart.scales.y;
    if (!ctx || !yScale) return;
    chart.data.datasets.forEach((ds, dsIdx) => {
      if (!chart.isDatasetVisible(dsIdx)) return;   // 图例取消勾选时不画其误差棒
      const meta = chart.getDatasetMeta(dsIdx);
      if (!meta || !meta.data) return;
      ctx.save();
      ctx.strokeStyle = ds.borderColor || '#fff';
      ctx.lineWidth = 1;
      const n = Math.min(meta.data.length, ds.data.length);
      for (let i = 0; i < n; i++) {
        const raw = ds.data[i];
        const err = raw && !raw.isUL ? raw.err : null;
        if (err == null || !(err > 0)) continue;
        const el = meta.data[i];
        if (!el || el.skip || raw.y == null || !isFinite(raw.y)) continue;
        const yTop = yScale.getPixelForValue(raw.y + err);
        const yBot = yScale.getPixelForValue(raw.y - err);
        if (!isFinite(yTop) || !isFinite(yBot)) continue;
        const cx = el.x;
        ctx.beginPath();
        ctx.moveTo(cx, Math.min(yTop, yBot));
        ctx.lineTo(cx, Math.max(yTop, yBot));
        ctx.moveTo(cx - 3, yTop);
        ctx.lineTo(cx + 3, yTop);
        ctx.moveTo(cx - 3, yBot);
        ctx.lineTo(cx + 3, yBot);
        ctx.stroke();
      }
      ctx.restore();
    });
    } catch (e) { console.error('errorBar plugin:', e); }
  },
};

async function loadLightcurveBands() {
  _lcBands = {};
  try {
    const lcData = await getLightcurves({ transient_id: _tid, per_page: 9999 });
    const bandSet = new Set();
    for (const p of (lcData.items || [])) {
      if (p.discard) continue;
      const conv = pointToMJy(p);
      if (!conv || !(p.time > 0)) continue;
      if (!_lcBands[p.band]) _lcBands[p.band] = [];
      _lcBands[p.band].push({ x: p.time, y: conv.y, err: conv.err, isUL: !!p.upperlimit, id: p.id });
      bandSet.add(p.band);
    }
    _bandColorMap = {};
    [...bandSet].sort().forEach((b, i) => {
      _bandColorMap[b] = BAND_COLORS[i % BAND_COLORS.length];
    });
  } catch (e) {
    console.warn('光变数据加载失败（叠加图将只画模型）:', e);
  }
}

// 四轴 → case 名（与后端 _case_name 规则一致：缺省轴 tophat/ism/none 不入名）
function resolveCaseName(sel) {
  const parts = [sel.model];
  if (sel.jet && sel.jet !== 'tophat') parts.push(sel.jet);
  if (sel.medium === 'wind') parts.push('wind');
  if (sel.extinction && sel.extinction !== 'none') parts.push(sel.extinction);
  return parts.join('_');
}

// 不支持的组合（如 fs_inject + powerlaw_wing）：返回原因，支持则返回 null
function unsupportedReason(schema, sel) {
  const u = (schema.unsupported || []).find(x => x.model === sel.model && x.jet === sel.jet);
  return u ? u.reason : null;
}

// ─── 先验模板：按所选四轴组合从 schema 重建（切换组合即重置为默认模板） ───
function buildPriorsFromTemplate(schema, sel) {
  const pri = {};
  for (const [k, v] of Object.entries(schema.priors_by_case[resolveCaseName(sel)] || {})) {
    pri[k] = { ...v };
  }
  return pri;
}

function currentSchema() {
  const eng = (_engines || []).find(e => e.name === (document.getElementById('fitEngine')?.value || ''))
    || (_engines || [])[0];
  return eng ? eng.schema : null;
}

// ─── 入口 / 清理 ───
export async function initFittingTab(container, tid) {
  destroyFittingTab();
  _tid = tid;
  _selectedId = null;
  _jobs = [];
  _selExcluded = new Set();   // 切换事件重进标签页时重置选取
  container.innerHTML = `
    <div class="row g-3">
      <div class="col-lg-6">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-cpu"></i> 拟合配置</div>
          <div class="card-body" id="fitConfigBody">
            <div class="text-secondary small"><span class="spinner-border spinner-border-sm"></span> 加载引擎配置...</div>
          </div>
        </div>
      </div>
      <div class="col-lg-6">
        <div class="card h-100">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="bi bi-funnel"></i> 数据选取</span>
            <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-1" id="fitSelReset"
                    style="font-size:0.72rem">重置选取</button>
          </div>
          <div class="card-body" id="fitSelBody">
            <div class="text-secondary small">加载光变数据...</div>
          </div>
        </div>
      </div>
    </div>
    <div class="row g-3 mt-0">
      <div class="col-12">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="bi bi-list-task"></i> 拟合任务</span>
            <button class="btn btn-sm btn-outline-secondary" id="fitRefreshBtn" title="刷新任务列表">
              <i class="bi bi-arrow-clockwise"></i>
            </button>
          </div>
          <div class="card-body p-0" id="fitJobsBody">
            <div class="text-secondary small p-3"><span class="spinner-border spinner-border-sm"></span> 加载任务...</div>
          </div>
        </div>
      </div>
    </div>
    <div id="fitResultArea" class="mt-3"></div>`;

  document.getElementById('fitRefreshBtn').addEventListener('click', () => refreshJobs());
  document.getElementById('fitSelReset').addEventListener('click', resetSelection);

  try {
    if (!_engines) _engines = await getFittingEngines();
    renderConfig();
    await Promise.all([refreshJobs(), loadLightcurveBands()]);
    renderDataSelection();
  } catch (e) {
    document.getElementById('fitConfigBody').innerHTML =
      `<div class="text-danger small">加载失败: ${esc(e.message)}</div>`;
    showToast(`拟合配置加载失败: ${e.message}`, 'danger');
  }
}

export function destroyFittingTab() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  if (_chart) { try { _chart.destroy(); } catch {} _chart = null; }
  if (_selChart) { try { _selChart.destroy(); } catch {} _selChart = null; }
  _tid = null;
  _selectedId = null;
}

// ─── 拟合配置区 ───
function _caseInfoHtml(schema) {
  const reason = unsupportedReason(schema, _modelSel);
  if (reason) return `不支持的组合：${_modelSel.model} + ${_modelSel.jet}（${reason}）`;
  const caseName = resolveCaseName(_modelSel);
  const info = ((schema.case_info || {})[caseName]) || {};
  const engTxt = info.engine === 'custom' ? '自定义 MCMC 外壳' : '内置 Fitter';
  let txt = `${caseName}：${info.description || ''} 〔拟合引擎: ${engTxt}〕`;
  if (info.constraint) txt += ` 联合约束: ${info.constraint}`;
  return txt;
}

function renderConfig() {
  const body = document.getElementById('fitConfigBody');
  if (!body) return;
  if (!_engines || _engines.length === 0) {
    body.innerHTML = '<div class="text-secondary small">无可用拟合引擎</div>';
    return;
  }
  const eng = _engines.find(e => e.name === _engineName) || _engines[0];
  _engineName = eng.name;
  const schema = eng.schema;
  const dc = schema.default_config;
  const opts = schema.options || {};
  const labels = schema.option_labels || {};
  if (!_modelSel) {
    _modelSel = { model: dc.model, jet: dc.jet, medium: dc.medium, extinction: dc.extinction };
  }
  const sp = schema.sampler || {};
  const authed = isAuthed();

  // 四个并列物理轴下拉：成分拓扑 × 喷流 × 介质 × 消光
  const axisSelect = (axis, id, title) => `
      <div class="col-6">
        <label class="form-label small mb-1">${title}</label>
        <select class="form-select form-select-sm fit-model-sel" id="${id}">
          ${(opts[axis] || []).map(v => `<option value="${v}" ${v === _modelSel[axis] ? 'selected' : ''}>${v} — ${esc(((labels[axis] || {})[v]) || '')}</option>`).join('')}
        </select>
      </div>`;
  const unsupported = unsupportedReason(schema, _modelSel);
  const modelControls = `
      ${axisSelect('model', 'fitAxModel', '成分拓扑 (model)')}
      ${axisSelect('jet', 'fitAxJet', '喷流结构 (jet)')}
      ${axisSelect('medium', 'fitAxMedium', '环境介质 (medium)')}
      ${axisSelect('extinction', 'fitAxExtinction', '宿主消光 (extinction)')}
      <div class="col-12">
        <div class="${unsupported ? 'text-danger' : 'text-secondary'} mt-1" style="font-size:0.72rem" id="fitCaseInfo">${esc(_caseInfoHtml(schema))}</div>
      </div>`;

  const seedInput = ('seed' in sp) ? `
          <div class="col-3"><label class="small text-secondary mb-0">seed</label>
            <input type="number" class="form-control form-control-sm" id="fitSeed" value="${sp.seed ?? 42}" min="0"></div>` : '';

  body.innerHTML = `
    ${authed ? '' : `<div class="alert alert-warning py-2 small mb-3">
      <i class="bi bi-lock"></i> 未登录：可浏览任务与结果，提交拟合需要先登录。</div>`}
    <div class="row g-2 small">
      <div class="col-12">
        <label class="form-label small mb-1">引擎</label>
        <select class="form-select form-select-sm" id="fitEngine">
          ${_engines.map(e => `<option value="${esc(e.name)}" ${e.name === _engineName ? 'selected' : ''}>${esc(e.label)} (${esc(e.name)})</option>`).join('')}
        </select>
      </div>
      ${modelControls}
      <div class="col-12 mt-2">
        <label class="form-label small mb-1"><i class="bi bi-sliders"></i> 先验（切换组合会重置为默认模板，可手动修改）</label>
        <div class="table-scroll" style="max-height:260px;overflow-y:auto">
          <table class="table table-sm mb-0" style="font-size:0.78rem">
            <thead><tr><th>参数</th><th>min</th><th>max</th><th>scale</th></tr></thead>
            <tbody id="fitPriorsBody"></tbody>
          </table>
        </div>
      </div>
      <div class="col-12 mt-2">
        <label class="form-label small mb-1"><i class="bi bi-stopwatch"></i> 采样设置</label>
        <div class="row g-2">
          <div class="col-3"><label class="small text-secondary mb-0">nsteps</label>
            <input type="number" class="form-control form-control-sm" id="fitNsteps" value="${sp.nsteps ?? 2000}" min="1" step="100"></div>
          <div class="col-3"><label class="small text-secondary mb-0">nburn</label>
            <input type="number" class="form-control form-control-sm" id="fitNburn" value="${sp.nburn ?? 1000}" min="0" step="100"></div>
          <div class="col-3"><label class="small text-secondary mb-0">top_k</label>
            <input type="number" class="form-control form-control-sm" id="fitTopK" value="${sp.top_k ?? 10}" min="1"></div>
          <div class="col-3"><label class="small text-secondary mb-0">npool</label>
            <input type="number" class="form-control form-control-sm" id="fitNpool" value="${sp.npool ?? 4}" min="1" max="${sp.npool_max ?? 8}"></div>
          ${seedInput}
        </div>
        <div class="text-secondary mt-1" style="font-size:0.72rem">步数越大耗时越长（分钟~十分钟级）</div>
      </div>
      <div class="col-12 mt-2">
        <button class="btn btn-sm btn-primary w-100" id="fitSubmitBtn" ${authed && !unsupported ? '' : 'disabled'}>
          <i class="bi bi-play-fill"></i> 提交拟合
        </button>
      </div>
    </div>`;

  renderPriorsTable();

  // 引擎切换 → 重置模型选择与先验，重渲染配置卡
  document.getElementById('fitEngine').addEventListener('change', (ev) => {
    _engineName = ev.target.value;
    _modelSel = null;
    renderConfig();
  });

  // 任一物理轴切换 → 重置先验为默认模板；不支持的组合给出提示并禁用提交
  body.querySelectorAll('.fit-model-sel').forEach(el => {
    el.addEventListener('change', () => {
      _modelSel = {
        model: document.getElementById('fitAxModel').value,
        jet: document.getElementById('fitAxJet').value,
        medium: document.getElementById('fitAxMedium').value,
        extinction: document.getElementById('fitAxExtinction').value,
      };
      const reason = unsupportedReason(schema, _modelSel);
      const infoEl = document.getElementById('fitCaseInfo');
      if (infoEl) {
        infoEl.textContent = _caseInfoHtml(schema);
        infoEl.classList.toggle('text-danger', !!reason);
        infoEl.classList.toggle('text-secondary', !reason);
      }
      const btn = document.getElementById('fitSubmitBtn');
      if (btn) btn.disabled = !isAuthed() || !!reason;
      renderPriorsTable();
    });
  });

  document.getElementById('fitSubmitBtn').addEventListener('click', submitJob);
}

// ─── 数据选取：波段多选 + 全局/分波段时段 + 预览散点图单点排除 ───
function _selCheckedBands() {
  return [...document.querySelectorAll('.fit-sel-band')].filter(c => c.checked).map(c => c.value);
}

function _selTimeRange() {
  const tmin = parseFloat(document.getElementById('fitSelTmin')?.value);
  const tmax = parseFloat(document.getElementById('fitSelTmax')?.value);
  return { tmin: isFinite(tmin) ? tmin : null, tmax: isFinite(tmax) ? tmax : null };
}

// 各波段输入的分波段时段：{band: {tmin, tmax}}，留空一端为 null
function _selBandRanges() {
  const map = {};
  for (const el of document.querySelectorAll('.fit-sel-band-tmin, .fit-sel-band-tmax')) {
    const b = el.dataset.band;
    if (!map[b]) map[b] = { tmin: null, tmax: null };
    const v = parseFloat(el.value);
    map[b][el.classList.contains('fit-sel-band-tmin') ? 'tmin' : 'tmax'] = isFinite(v) ? v : null;
  }
  return map;
}

// 波段有效时段：该波段自身设置优先（按端），留空端跟随全局
function _selEffectiveRange(band, global, bandRanges) {
  const br = bandRanges[band] || {};
  return {
    tmin: br.tmin != null ? br.tmin : global.tmin,
    tmax: br.tmax != null ? br.tmax : global.tmax,
  };
}

function resetSelection() {
  _selExcluded.clear();
  document.querySelectorAll('.fit-sel-band').forEach(cb => { cb.checked = true; });
  for (const id of ['fitSelTmin', 'fitSelTmax']) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  document.querySelectorAll('.fit-sel-band-tmin, .fit-sel-band-tmax').forEach(el => { el.value = ''; });
  buildSelChart();
}

function renderDataSelection() {
  const el = document.getElementById('fitSelBody');
  if (!el) return;
  const bands = Object.keys(_lcBands || {}).sort();
  if (!bands.length) {
    el.innerHTML = '<div class="text-secondary small">该源无可用光变数据</div>';
    return;
  }
  el.innerHTML = `
    <div class="row g-2 mb-2">
      <div class="col-6"><label class="small text-secondary mb-0">全局 t_min (s，各波段默认)</label>
        <input type="number" class="form-control form-control-sm" id="fitSelTmin" min="0" step="any" placeholder="不限"></div>
      <div class="col-6"><label class="small text-secondary mb-0">全局 t_max (s，各波段默认)</label>
        <input type="number" class="form-control form-control-sm" id="fitSelTmax" min="0" step="any" placeholder="不限"></div>
    </div>
    <div class="mb-2">
      ${bands.map((b, i) => `
        <div class="d-flex align-items-center gap-2 mb-1">
          <div class="form-check mb-0 text-nowrap">
            <input class="form-check-input fit-sel-band" type="checkbox" id="fitSelBand_${i}" value="${esc(b)}" checked>
            <label class="form-check-label small" for="fitSelBand_${i}">
              <span style="color:${_bandColorMap[b] || 'inherit'}">●</span> ${esc(b)}
            </label>
          </div>
          <input type="number" class="form-control form-control-sm fit-sel-band-tmin ms-auto" data-band="${esc(b)}"
                 min="0" step="any" placeholder="全局" style="width:88px;font-size:0.78rem" title="${esc(b)} 波段 t_min (s)，留空跟随全局">
          <input type="number" class="form-control form-control-sm fit-sel-band-tmax" data-band="${esc(b)}"
                 min="0" step="any" placeholder="全局" style="width:88px;font-size:0.78rem" title="${esc(b)} 波段 t_max (s)，留空跟随全局">
        </div>`).join('')}
    </div>
    <div class="d-flex align-items-center gap-2 mb-1">
      <span class="small text-secondary">点击散点切换 排除/保留（灰 × = 手动排除；淡色 = 被波段/时段过滤）</span>
      <div class="form-check form-check-inline mb-0 ms-auto" title="是否绘制数据点的误差棒">
        <input class="form-check-input" type="checkbox" id="fitSelErr" ${_lcShowErr ? 'checked' : ''} onchange="fitSelErrToggle(this.checked)">
        <label class="form-check-label small" for="fitSelErr">误差棒</label>
      </div>
    </div>
    <div style="position:relative;height:220px"><canvas id="fitSelChart"></canvas></div>
    <div class="small mt-1" id="fitSelCount"></div>`;
  el.querySelectorAll('.fit-sel-band').forEach(cb => cb.addEventListener('change', buildSelChart));
  for (const id of ['fitSelTmin', 'fitSelTmax']) {
    document.getElementById(id).addEventListener('input', buildSelChart);
  }
  el.querySelectorAll('.fit-sel-band-tmin, .fit-sel-band-tmax')
    .forEach(inp => inp.addEventListener('input', buildSelChart));
  buildSelChart();
}

function buildSelChart() {
  if (_selChart) { try { _selChart.destroy(); } catch {} _selChart = null; }
  const canvas = document.getElementById('fitSelChart');
  if (!canvas) return;
  if (typeof Chart === 'undefined') {
    canvas.parentElement.innerHTML = '<div class="text-secondary small">Chart.js 未加载</div>';
    return;
  }
  const cc = chartColors();
  const bandSet = new Set(_selCheckedBands());
  const global = _selTimeRange();
  const bandRanges = _selBandRanges();
  const datasets = [];
  let kept = 0, total = 0;
  for (const [band, pts] of Object.entries(_lcBands || {})) {
    const color = _bandColorMap[band] || '#58a6ff';
    const { tmin, tmax } = _selEffectiveRange(band, global, bandRanges);
    const keep = [], dim = [], excl = [];
    for (const p of pts) {
      total++;
      if (_selExcluded.has(p.id)) { excl.push(p); continue; }
      const out = !bandSet.has(band)
        || (tmin != null && p.x < tmin) || (tmax != null && p.x > tmax);
      if (out) dim.push(p); else { keep.push(p); kept++; }
    }
    if (keep.length) {
      datasets.push({
        label: band, data: keep, showLine: false,
        backgroundColor: color, borderColor: color,
        pointRadius: 2.5, pointHoverRadius: 5,
      });
    }
    if (dim.length) {
      datasets.push({
        label: '', data: dim, showLine: false,
        backgroundColor: color + '38', borderColor: color + '38',
        pointRadius: 2.5, pointHoverRadius: 5,
      });
    }
    if (excl.length) {
      datasets.push({
        label: '', data: excl, showLine: false,
        backgroundColor: cc.tickSub, borderColor: cc.tickSub,
        pointStyle: 'crossRot', pointRadius: 5, pointHoverRadius: 7,
      });
    }
  }
  const cnt = document.getElementById('fitSelCount');
  if (cnt) cnt.innerHTML = `入选 <b>${kept}</b> / 共 ${total} 点`;

  _selChart = new Chart(canvas.getContext('2d'), {
    type: 'scatter',
    data: { datasets },
    plugins: [_lcErrorBarPlugin],
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'nearest', intersect: true },
      onHover(evt, elements) {
        if (evt.native && evt.native.target) {
          evt.native.target.style.cursor = elements.length ? 'pointer' : 'default';
        }
      },
      onClick(evt, elements, chart) {
        // 点击散点 → 切换 排除/保留（与 relations.js 相同交互）
        const els = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, true);
        if (!els.length) return;
        const { datasetIndex, index } = els[0];
        const p = chart.data.datasets[datasetIndex].data[index];
        if (!p || p.id == null) return;
        if (_selExcluded.has(p.id)) _selExcluded.delete(p.id);
        else _selExcluded.add(p.id);
        // 延迟到事件分发结束后重建图表（避免在 Chart.js 自身回调中销毁实例报错）
        setTimeout(buildSelChart, 0);
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: cc.legend, boxWidth: 12, font: { ...academicFonts().legend, size: 11 },
            filter: item => item.text !== '',
          },
        },
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          callbacks: {
            label: (ctx) => {
              const p = ctx.raw;
              const band = ctx.dataset.label || '';
              const state = _selExcluded.has(p.id) ? ' [已排除]' : '';
              const errTxt = p.err != null && p.err > 0 ? `±${sci3(p.err)}` : '';
              return `${band} t=${sci3(p.x)}s  F=${sci3(p.y)}${errTxt}mJy${state}`;
            },
          },
        },
      },
      scales: {
        x: {
          type: 'logarithmic',
          title: { display: true, text: '时间 (s)', color: cc.tick, font: academicFonts().title },
          ticks: { color: cc.tick, font: academicFonts().tick }, grid: { color: cc.gridSoft },
          border: { color: cc.tick },
        },
        y: {
          type: 'logarithmic',
          title: { display: true, text: '流量密度 (mJy)', color: cc.tick, font: academicFonts().title },
          ticks: { color: cc.tick, font: academicFonts().tick }, grid: { color: cc.gridSoft },
          border: { color: cc.tick },
        },
      },
    },
  });
}

// 组装 config.data_selection：缺省字段=不限制；无任何限制时返回 null
function collectDataSelection() {
  const bandEls = [...document.querySelectorAll('.fit-sel-band')];
  if (!bandEls.length) return null;   // 选取块未渲染（无光变数据）
  const checked = bandEls.filter(c => c.checked).map(c => c.value);
  const { tmin, tmax } = _selTimeRange();
  const ds = {};
  if (checked.length !== bandEls.length) ds.bands = checked;
  if (tmin != null) ds.tmin = tmin;
  if (tmax != null) ds.tmax = tmax;
  // 分波段时段：只收至少一端有值的波段（两端皆空=跟随全局，不列入）
  const bandRanges = {};
  for (const [band, r] of Object.entries(_selBandRanges())) {
    if (r.tmin != null || r.tmax != null) bandRanges[band] = [r.tmin, r.tmax];
  }
  if (Object.keys(bandRanges).length) ds.band_ranges = bandRanges;
  if (_selExcluded.size) ds.exclude_ids = [..._selExcluded];
  return Object.keys(ds).length ? ds : null;
}

function renderPriorsTable() {
  const tbody = document.getElementById('fitPriorsBody');
  if (!tbody) return;
  const schema = currentSchema();
  if (!schema) { tbody.innerHTML = ''; return; }
  const priors = buildPriorsFromTemplate(schema, _modelSel);
  tbody.innerHTML = Object.entries(priors).map(([name, p]) => `
    <tr>
      <td class="text-nowrap" title="${esc(name)}">${esc(name)}</td>
      <td><input type="number" class="form-control form-control-sm fit-prior" data-pname="${esc(name)}" data-field="min"
           value="${p.min}" step="any" style="width:90px;font-size:0.75rem"></td>
      <td><input type="number" class="form-control form-control-sm fit-prior" data-pname="${esc(name)}" data-field="max"
           value="${p.max}" step="any" style="width:90px;font-size:0.75rem" ${p.scale === 'fixed' ? 'disabled' : ''}></td>
      <td><select class="form-select form-select-sm fit-prior" data-pname="${esc(name)}" data-field="scale" style="width:80px;font-size:0.75rem">
        ${['log', 'linear', 'fixed'].map(s => `<option value="${s}" ${p.scale === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></td>
    </tr>`).join('');
}

async function submitJob() {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  const schema = currentSchema();
  if (!schema) { showToast('引擎配置未加载', 'danger'); return; }
  const reason = unsupportedReason(schema, _modelSel);
  if (reason) { showToast(`不支持的组合：${reason}`, 'warning'); return; }

  // 收集先验（含手动修改）
  const template = buildPriorsFromTemplate(schema, _modelSel);
  const priors = {};
  for (const name of Object.keys(template)) priors[name] = { ...template[name] };
  document.querySelectorAll('.fit-prior').forEach(el => {
    const { pname, field } = el.dataset;
    if (!priors[pname]) return;
    if (field === 'scale') priors[pname].scale = el.value;
    else {
      const v = parseFloat(el.value);
      if (!isFinite(v)) return;
      priors[pname][field] = v;
    }
  });

  const samplerCfg = {
    nsteps: parseInt(document.getElementById('fitNsteps').value, 10) || 2000,
    nburn: parseInt(document.getElementById('fitNburn').value, 10) || 1000,
    top_k: parseInt(document.getElementById('fitTopK').value, 10) || 10,
    npool: parseInt(document.getElementById('fitNpool').value, 10) || 4,
  };
  const seedEl = document.getElementById('fitSeed');   // 仅 case 型引擎渲染
  if (seedEl) samplerCfg.seed = parseInt(seedEl.value, 10) || 42;

  const config = {
    ..._modelSel,
    priors,
    sampler: samplerCfg,
  };
  const engine = document.getElementById('fitEngine').value;
  const ds = collectDataSelection();
  if (ds) config.data_selection = ds;

  const btn = document.getElementById('fitSubmitBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 提交中...';
  try {
    const res = await submitFittingJob({ transient_id: _tid, engine, config });
    showToast(`拟合任务 #${res.id} 已提交`, 'success');
    await refreshJobs();
  } catch (e) {
    showToast(`提交失败: ${e.message}`, 'danger');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-play-fill"></i> 提交拟合';
  }
}

// ─── 任务列表 ───
const STATUS_BADGE = {
  pending: '<span class="badge bg-secondary"><i class="bi bi-clock"></i> pending</span>',
  running: '<span class="badge bg-primary"><span class="spinner-border spinner-border-sm" style="width:0.7rem;height:0.7rem"></span> running</span>',
  done: '<span class="badge bg-success">done</span>',
  failed: '<span class="badge bg-danger">failed</span>',
  interrupted: '<span class="badge bg-secondary">interrupted</span>',
};

async function refreshJobs() {
  if (!_tid) return;
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  try {
    _jobs = await getFittingJobs(_tid);
  } catch (e) {
    showToast(`任务列表加载失败: ${e.message}`, 'danger');
    return;
  }
  renderJobs();
  // 若选中任务状态有更新，刷新结果区
  if (_selectedId != null) {
    const j = _jobs.find(x => x.id === _selectedId);
    if (!j) {
      _selectedId = null;
      const area = document.getElementById('fitResultArea');
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
  const body = document.getElementById('fitJobsBody');
  if (!body) return;
  if (_jobs.length === 0) {
    body.innerHTML = '<div class="text-secondary small p-3">暂无拟合任务</div>';
    return;
  }
  body.innerHTML = `
    <div class="table-scroll" style="max-height:400px;overflow-y:auto">
      <table class="table table-sm table-hover mb-0" style="font-size:0.8rem">
        <thead><tr>
          <th>#</th><th>模型</th><th>状态</th><th>chi2</th><th>耗时</th><th>创建时间</th><th></th>
        </tr></thead>
        <tbody>
          ${_jobs.map(j => `
            <tr data-jobid="${j.id}" class="${j.id === _selectedId ? 'table-active' : ''}" style="cursor:pointer">
              <td>${j.id}</td>
              <td class="text-nowrap">${esc(j.model_name)}</td>
              <td>${STATUS_BADGE[j.status] || esc(j.status)}</td>
              <td>${j.chi2 != null ? sci3(j.chi2) : '-'}</td>
              <td>${fmtRuntime(j.runtime_s)}</td>
              <td class="text-nowrap small">${fmtTime(j.created_at)}</td>
              <td class="text-nowrap">
                ${j.status === 'done' ? `<button class="btn btn-sm btn-outline-primary py-0 px-1 fit-view" data-jobid="${j.id}">查看结果</button>` : ''}
                ${isAdmin() && ['done', 'failed', 'interrupted'].includes(j.status)
                  ? `<button class="btn btn-sm btn-outline-danger py-0 px-1 fit-del" data-jobid="${j.id}" title="删除"><i class="bi bi-trash"></i></button>` : ''}
              </td>
            </tr>
            ${j.status === 'failed' ? `<tr class="fit-err-row" data-jobid="${j.id}" style="display:none"><td colspan="7" class="small text-danger" id="fitErr_${j.id}">加载错误信息...</td></tr>` : ''}
          `).join('')}
        </tbody>
      </table>
    </div>`;

  body.querySelectorAll('.fit-view').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      loadResult(parseInt(btn.dataset.jobid, 10));
    });
  });
  body.querySelectorAll('.fit-del').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = parseInt(btn.dataset.jobid, 10);
      if (!confirm(`删除拟合任务 #${id} 及其产物？`)) return;
      try {
        await deleteFittingJob(id);
        showToast(`任务 #${id} 已删除`, 'success');
        if (_selectedId === id) {
          _selectedId = null;
          const area = document.getElementById('fitResultArea');
          if (area) area.innerHTML = '';
        }
        await refreshJobs();
      } catch (err) {
        showToast(`删除失败: ${err.message}`, 'danger');
      }
    });
  });
  // 行点击：done → 查看结果；failed → 展开错误
  body.querySelectorAll('tr[data-jobid]:not(.fit-err-row)').forEach(row => {
    row.addEventListener('click', async () => {
      const id = parseInt(row.dataset.jobid, 10);
      const job = _jobs.find(j => j.id === id);
      if (!job) return;
      if (job.status === 'done') loadResult(id);
      else if (job.status === 'failed') {
        const errRow = body.querySelector(`.fit-err-row[data-jobid="${id}"]`);
        if (!errRow) return;
        const show = errRow.style.display === 'none';
        errRow.style.display = show ? '' : 'none';
        if (show) {
          try {
            const d = await getFittingJob(id);
            const cell = document.getElementById(`fitErr_${id}`);
            if (cell) cell.textContent = d.error || '（无错误信息）';
          } catch (e) {
            const cell = document.getElementById(`fitErr_${id}`);
            if (cell) cell.textContent = `错误信息加载失败: ${e.message}`;
          }
        }
      }
    });
  });
}

// ─── 结果展示 ───
async function loadResult(jobId) {
  const area = document.getElementById('fitResultArea');
  if (!area) return;
  _selectedId = jobId;
  _axisRange = { xmin: null, xmax: null, ymin: null, ymax: null };  // 新结果回默认范围
  renderJobs(); // 高亮选中行
  area.innerHTML = `<div class="card"><div class="card-body text-secondary small">
    <span class="spinner-border spinner-border-sm"></span> 加载任务 #${jobId} 结果...</div></div>`;

  let detail;
  try {
    detail = await getFittingJob(jobId);
  } catch (e) {
    area.innerHTML = `<div class="card"><div class="card-body text-danger small">结果加载失败: ${esc(e.message)}</div></div>`;
    return;
  }

  let lcModel = null;
  if (detail.files && detail.files.lc_model) {
    try { lcModel = await getFittingJobFile(jobId, 'lc_model'); } catch (e) {
      console.warn('lc_model 加载失败:', e);
    }
  }

  const params = detail.parameters || {};
  const warnings = detail.warnings || [];
  const stats = [
    ['chi2', detail.chi2 != null ? sci3(detail.chi2) : '-'],
    ['dof', detail.dof ?? '-'],
    ['BIC', detail.bic != null ? sci3(detail.bic) : '-'],
    ['AIC', detail.aic != null ? sci3(detail.aic) : '-'],
  ];

  area.innerHTML = `
    <div class="card">
      <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
        <span><i class="bi bi-graph-up-arrow"></i> 拟合结果 — 任务 #${detail.id} <span class="text-secondary small">${esc(detail.model_name)}</span></span>
        <span class="d-flex gap-1">
          ${detail.files && detail.files.metrics ? `<a class="btn btn-sm btn-outline-secondary" href="${detail.files.metrics}" target="_blank" title="查看 metrics.txt">
            <i class="bi bi-file-text"></i> metrics</a>` : ''}
          ${detail.files && detail.files.h5 ? `<a class="btn btn-sm btn-outline-secondary" href="${detail.files.h5}" title="下载采样链 h5">
            <i class="bi bi-download"></i> chain_record.h5</a>` : ''}
        </span>
      </div>
      <div class="card-body">
        <div class="row g-3">
          <div class="col-lg-5">
            <div class="d-flex flex-wrap gap-2 mb-2">
              ${stats.map(([k, v]) => `<span class="badge-tag badge-neutral">${k} = ${v}</span>`).join('')}
            </div>
            ${warnings.length ? `<div class="alert alert-warning py-2 small">
              <i class="bi bi-exclamation-triangle"></i> ${warnings.map(w => esc(w)).join('<br>')}</div>` : ''}
            <div class="table-scroll" style="max-height:340px;overflow-y:auto">
              <table class="table table-sm mb-0" style="font-size:0.8rem">
                <thead><tr><th>参数</th><th>值 ± 误差</th></tr></thead>
                <tbody>
                  ${Object.entries(params).map(([name, p]) => `
                    <tr><td>${esc(name)}</td><td>${sci3(p.v)} ± ${sci3(p.err)}</td></tr>`).join('')}
                </tbody>
              </table>
            </div>
          </div>
          <div class="col-lg-7">
            <div class="small text-secondary mb-1">模型光变叠加（68% 置信带，mJy）</div>
            <div class="d-flex flex-wrap gap-1 align-items-center small mb-1">
              <span class="text-secondary">坐标范围:</span>
              <input type="text" class="form-control form-control-sm" id="fitAxXmin" placeholder="xmin (s)" style="width:80px">
              <input type="text" class="form-control form-control-sm" id="fitAxXmax" placeholder="xmax (s)" style="width:80px">
              <input type="text" class="form-control form-control-sm" id="fitAxYmin" placeholder="ymin" style="width:80px">
              <input type="text" class="form-control form-control-sm" id="fitAxYmax" placeholder="ymax" style="width:80px">
              <button class="btn btn-sm btn-outline-primary" onclick="fitAxisApply()">应用</button>
              <button class="btn btn-sm btn-outline-secondary" onclick="fitAxisReset()">恢复默认</button>
              <div class="form-check form-check-inline mb-0" title="是否绘制数据点的误差棒">
                <input class="form-check-input" type="checkbox" id="fitLcErr" ${_lcShowErr ? 'checked' : ''} onchange="fitLcErrToggle(this.checked)">
                <label class="form-check-label small" for="fitLcErr">误差棒</label>
              </div>
              <span class="text-secondary ms-1" title="在图上按住左键拖出矩形框可放大该区域"><i class="bi bi-info-circle"></i> 可框选缩放</span>
            </div>
            <div style="position:relative;height:380px"><canvas id="fitLCChart"></canvas></div>
          </div>
        </div>
        ${detail.files && detail.files.corner ? `
        <div class="row mt-3">
          <div class="col-lg-8 mx-auto">
            <div class="small text-secondary mb-1">角图 (corner)</div>
            <img src="${detail.files.corner}" class="img-fluid rounded border" alt="corner plot"
                 onerror="this.parentElement.innerHTML='<div class=\\'text-secondary small\\'>角图加载失败</div>'">
          </div>
        </div>` : ''}
        ${detail.files && detail.files.lc_plot ? `
        <div class="row mt-3">
          <div class="col-lg-10 mx-auto">
            <div class="small text-secondary mb-1">光变拟合图（错位分波段，虚线为成分拆分）</div>
            <img src="${detail.files.lc_plot}" class="img-fluid rounded border" alt="lightcurve plot"
                 onerror="this.parentElement.innerHTML='<div class=\\'text-secondary small\\'>光变图加载失败</div>'">
            ${detail.files.lc_ratio ? `
            <div class="small text-secondary mt-2 mb-1">光变拟合图 + data/model 比值子图</div>
            <img src="${detail.files.lc_ratio}" class="img-fluid rounded border" alt="lightcurve ratio plot"
                 onerror="this.style.display='none'">` : ''}
          </div>
        </div>` : ''}
      </div>
    </div>`;

  buildFitChart(lcModel);
}

// ─── 叠加图：数据点 + 模型中值虚线 + 1σ 置信带（log-log） ───
function buildFitChart(lcModel) {
  if (_chart) { try { _chart.destroy(); } catch {} _chart = null; }
  const canvas = document.getElementById('fitLCChart');
  if (!canvas) return;
  if (typeof Chart === 'undefined') {
    canvas.parentElement.innerHTML = '<div class="text-secondary small">Chart.js 未加载</div>';
    return;
  }

  const cc = chartColors();
  const datasets = [];

  // 数据点（探测 + 上限三角）
  for (const [band, pts] of Object.entries(_lcBands || {})) {
    const color = _bandColorMap[band] || '#58a6ff';
    const det = pts.filter(p => !p.isUL);
    const ul = pts.filter(p => p.isUL);
    if (det.length) {
      datasets.push({
        label: band, data: det, showLine: false,
        backgroundColor: color, borderColor: color,
        pointRadius: 2.5, pointHoverRadius: 5,
      });
    }
    if (ul.length) {
      datasets.push({
        label: `${band} 上限`, data: ul, showLine: false,
        backgroundColor: color + '60', borderColor: color,
        pointStyle: 'triangle', pointRotation: 180, pointRadius: 5,
      });
    }
  }

  // 模型：先画置信带（fill 到前一数据集），再画中值虚线
  for (const b of (lcModel?.bands || [])) {
    const color = _bandColorMap[b.band] || cc.tick;
    const t = b.t || [];
    if (b.f_lo && b.f_hi) {
      datasets.push({
        label: '', data: t.map((x, i) => ({ x, y: b.f_hi[i] })),
        borderWidth: 0, pointRadius: 0, showLine: true, fill: false,
      });
      datasets.push({
        label: '', data: t.map((x, i) => ({ x, y: Math.max(b.f_lo[i], 1e-10) })),
        borderWidth: 0, pointRadius: 0, showLine: true,
        fill: '-1', backgroundColor: color + '2e',
      });
    }
    if (b.f_med) {
      datasets.push({
        label: `${b.band} 模型`, data: t.map((x, i) => ({ x, y: b.f_med[i] })),
        borderColor: color, borderDash: [6, 4], borderWidth: 2,
        pointRadius: 0, showLine: true, fill: false,
      });
    }
  }

  _chart = new Chart(canvas.getContext('2d'), {
    type: 'scatter',
    data: { datasets },
    plugins: [_dragRectPlugin, _lcErrorBarPlugin],
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: cc.legend, boxWidth: 12, font: { ...academicFonts().legend, size: 11 },
            filter: item => item.text !== '',
          },
        },
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          callbacks: {
            label: (ctx) => {
              const p = ctx.raw;
              const errTxt = p.err != null && p.err > 0 ? `±${sci3(p.err)}` : '';
              return `${ctx.dataset.label || ''} t=${sci3(p.x)}s  F=${sci3(p.y)}${errTxt}mJy`;
            },
          },
        },
      },
      scales: {
        x: {
          type: 'logarithmic',
          title: { display: true, text: '时间 (s)', color: cc.tick, font: academicFonts().title },
          ticks: { color: cc.tick, font: academicFonts().tick }, grid: { color: cc.gridSoft },
          border: { color: cc.tick },
        },
        y: {
          type: 'logarithmic',
          title: { display: true, text: '流量密度 (mJy)', color: cc.tick, font: academicFonts().title },
          ticks: { color: cc.tick, font: academicFonts().tick }, grid: { color: cc.gridSoft },
          border: { color: cc.tick },
        },
      },
    },
  });
  _attachDragZoom(_chart, canvas);
  _fillAxisInputs();
  _applyAxisRangeToChart();   // 重绘后恢复用户已设范围
}
