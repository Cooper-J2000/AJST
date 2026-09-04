// === Filters Page ===
import { app, showLoading, showError } from './layout.js';
import { isAuthed, isAdmin, showToast, deleteFilter } from '../api.js';
import { chartColors } from '../theme.js';

const API_BASE = '/api';

async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    credentials: 'same-origin',
  };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API_BASE + path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ message: r.statusText }));
    throw new Error(err.message || r.statusText);
  }
  return r.json();
}

let _sort = 'wavelength', _order = 'asc';
let _filters = [];                  // 最近一次加载的滤光片列表
const _unchecked = new Set();       // 行首未勾选的 id（默认全选；勾选态同时驱动总图显示）
const _highlight = new Set();       // 总图中加粗突出的 id
let _overviewChart = null, _curveChart = null;
// 曲线配色：有曲线的滤光片按波长排序后按序号映射色阶（短波→蓝/紫，长波→红）
const _colorRank = new Map();       // id -> 序号
let _colorCount = 0;
// 总图横轴范围（留空 = 自动全范围）
let _xMin = null, _xMax = null;

// ─── 新增弹窗的透过率曲线状态 ───
let _afSvoSelected = null;          // 选定的 SVO filter id
let _afBuiltinLoaded = false;       // pcigale 内置列表是否已加载

function _afShowPane(kind) {
  document.getElementById('afPaneBuiltin').style.display = kind === 'builtin' ? '' : 'none';
  document.getElementById('afPaneUpload').style.display = kind === 'upload' ? '' : 'none';
  document.getElementById('afPaneSvo').style.display = kind === 'svo' ? '' : 'none';
}

async function _afLoadBuiltin() {
  if (_afBuiltinLoaded) return;
  const sel = document.getElementById('afPcigaleName');
  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const r = await api('GET', '/filters/pcigale_builtin');
    sel.innerHTML = '<option value="">-- 请选择 --</option>' +
      r.names.map(n => `<option value="${n}">${n}</option>`).join('');
    _afBuiltinLoaded = true;
  } catch (err) {
    sel.innerHTML = `<option value="">加载失败: ${err.message}</option>`;
  }
}

function _afReset() {
  _afSvoSelected = null;
  document.getElementById('afCkNone').checked = true;
  _afShowPane('none');
  document.getElementById('afCurveFile').value = '';
  document.getElementById('afCurveText').value = '';
  document.getElementById('afCurveMsg').textContent = '';
  document.getElementById('afSvoQ').value = '';
  document.getElementById('afSvoMsg').textContent = '';
  document.getElementById('afSvoResults').innerHTML = '';
  const sel = document.getElementById('afPcigaleName');
  if (_afBuiltinLoaded) sel.value = '';
}

async function _afCurveValidate() {
  const text = document.getElementById('afCurveText').value;
  const msg = document.getElementById('afCurveMsg');
  if (!text.trim()) { msg.innerHTML = '<span class="text-danger">内容为空</span>'; return; }
  msg.textContent = '校验中…';
  try {
    const r = await api('POST', '/filters/parse_curve', { text });
    msg.innerHTML = `<span class="text-success">✓ ${r.npoints} 个点，λ ${r.wl_min.toFixed(1)}–${r.wl_max.toFixed(1)} Å（已归一化）</span>`;
  } catch (err) {
    msg.innerHTML = `<span class="text-danger">✗ ${err.message}</span>`;
  }
}

async function _afSvoSearch() {
  const q = document.getElementById('afSvoQ').value.trim();
  if (!q) { showToast('请输入关键词或 SVO ID', 'warning'); return; }
  const msg = document.getElementById('afSvoMsg');
  const box = document.getElementById('afSvoResults');
  msg.textContent = '搜索中…';
  box.innerHTML = '';
  _afSvoSelected = null;
  try {
    const r = await api('GET', `/filters/svo_search?q=${encodeURIComponent(q)}`);
    const results = [...r.results];
    // 输入看起来像完整 SVO ID（Facility/Instrument.Band）时允许直接抓取
    if (q.includes('/') && q.includes('.') && !results.some(it => it.id === q)) {
      results.unshift({ id: q, facility: '', instrument: '', description: '直接按输入的 SVO ID 抓取' });
    }
    msg.textContent = results.length ? `共 ${results.length} 条候选，点击选定：` : '无匹配结果';
    box.innerHTML = results.map((it, i) => `
      <div class="form-check small">
        <input class="form-check-input af-svo-pick" type="radio" name="afSvoPick" id="afSvoPick${i}" value="${it.id.replace(/"/g, '&quot;')}">
        <label class="form-check-label" for="afSvoPick${i}"><strong>${it.id}</strong> <span class="text-secondary">${[it.facility, it.instrument].filter(Boolean).join('/')}${it.description ? ' — ' + it.description : ''}</span></label>
      </div>`).join('');
    box.querySelectorAll('.af-svo-pick').forEach(radio => {
      radio.addEventListener('change', () => { _afSvoSelected = radio.value; });
    });
  } catch (err) {
    msg.textContent = '';
    box.innerHTML = `<div class="small text-danger">搜索失败: ${err.message}</div>`;
  }
}

window.filterCurveValidate = _afCurveValidate;
window.filterSvoSearch = _afSvoSearch;

// ─── 编辑补录曲线小弹窗（复用新增弹窗的三种获取方式；写入 POST /api/filters/<id>/curve） ───
let _cfSvoSelected = null;          // 补录弹窗选定的 SVO filter id
let _cfBuiltinLoaded = false;       // 补录弹窗 pcigale 内置列表是否已加载

function _cfShowPane(kind) {
  document.getElementById('cfPaneBuiltin').style.display = kind === 'builtin' ? '' : 'none';
  document.getElementById('cfPaneUpload').style.display = kind === 'upload' ? '' : 'none';
  document.getElementById('cfPaneSvo').style.display = kind === 'svo' ? '' : 'none';
}

async function _cfLoadBuiltin() {
  if (_cfBuiltinLoaded) return;
  const sel = document.getElementById('cfPcigaleName');
  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const r = await api('GET', '/filters/pcigale_builtin');
    sel.innerHTML = '<option value="">-- 请选择 --</option>' +
      r.names.map(n => `<option value="${n}">${n}</option>`).join('');
    _cfBuiltinLoaded = true;
  } catch (err) {
    sel.innerHTML = `<option value="">加载失败: ${err.message}</option>`;
  }
}

function _cfReset() {
  _cfSvoSelected = null;
  document.getElementById('cfCkNone').checked = true;
  _cfShowPane('none');
  document.getElementById('cfCurveFile').value = '';
  document.getElementById('cfCurveText').value = '';
  document.getElementById('cfCurveMsg').textContent = '';
  document.getElementById('cfSvoQ').value = '';
  document.getElementById('cfSvoMsg').textContent = '';
  document.getElementById('cfSvoResults').innerHTML = '';
  const sel = document.getElementById('cfPcigaleName');
  if (_cfBuiltinLoaded) sel.value = '';
}

window.filterCurveShowFetch = (id) => {
  document.getElementById('cfFid').value = id;
  document.getElementById('cfTitle').textContent = `获取透过率曲线 — ${id}`;
  _cfReset();
  new bootstrap.Modal(document.getElementById('curveFetchModal')).show();
};

async function _cfCurveValidate() {
  const text = document.getElementById('cfCurveText').value;
  const msg = document.getElementById('cfCurveMsg');
  if (!text.trim()) { msg.innerHTML = '<span class="text-danger">内容为空</span>'; return; }
  msg.textContent = '校验中…';
  try {
    const r = await api('POST', '/filters/parse_curve', { text });
    msg.innerHTML = `<span class="text-success">✓ ${r.npoints} 个点，λ ${r.wl_min.toFixed(1)}–${r.wl_max.toFixed(1)} Å（已归一化）</span>`;
  } catch (err) {
    msg.innerHTML = `<span class="text-danger">✗ ${err.message}</span>`;
  }
}

async function _cfSvoSearch() {
  const q = document.getElementById('cfSvoQ').value.trim();
  if (!q) { showToast('请输入关键词或 SVO ID', 'warning'); return; }
  const msg = document.getElementById('cfSvoMsg');
  const box = document.getElementById('cfSvoResults');
  msg.textContent = '搜索中…';
  box.innerHTML = '';
  _cfSvoSelected = null;
  try {
    const r = await api('GET', `/filters/svo_search?q=${encodeURIComponent(q)}`);
    const results = [...r.results];
    if (q.includes('/') && q.includes('.') && !results.some(it => it.id === q)) {
      results.unshift({ id: q, facility: '', instrument: '', description: '直接按输入的 SVO ID 抓取' });
    }
    msg.textContent = results.length ? `共 ${results.length} 条候选，点击选定：` : '无匹配结果';
    box.innerHTML = results.map((it, i) => `
      <div class="form-check small">
        <input class="form-check-input cf-svo-pick" type="radio" name="cfSvoPick" id="cfSvoPick${i}" value="${it.id.replace(/"/g, '&quot;')}">
        <label class="form-check-label" for="cfSvoPick${i}"><strong>${it.id}</strong> <span class="text-secondary">${[it.facility, it.instrument].filter(Boolean).join('/')}${it.description ? ' — ' + it.description : ''}</span></label>
      </div>`).join('');
    box.querySelectorAll('.cf-svo-pick').forEach(radio => {
      radio.addEventListener('change', () => { _cfSvoSelected = radio.value; });
    });
  } catch (err) {
    msg.textContent = '';
    box.innerHTML = `<div class="small text-danger">搜索失败: ${err.message}</div>`;
  }
}

window.filterCurveFetchValidate = _cfCurveValidate;
window.filterCurveFetchSvoSearch = _cfSvoSearch;

window.filterCurveFetchSave = async () => {
  const id = document.getElementById('cfFid').value;
  const kind = (document.querySelector('input[name=cfCurveKind]:checked') || {}).value || 'none';
  let curve = null;
  if (kind === 'builtin') {
    const name = document.getElementById('cfPcigaleName').value;
    if (!name) { showToast('请选择 pcigale 内置滤光片（或改选跳过）', 'warning'); return; }
    curve = { kind: 'pcigale_builtin', name };
  } else if (kind === 'upload') {
    const text = document.getElementById('cfCurveText').value;
    if (!text.trim()) { showToast('请粘贴或上传曲线内容（或改选跳过）', 'warning'); return; }
    curve = { kind: 'upload', text };
  } else if (kind === 'svo') {
    if (!_cfSvoSelected) { showToast('请先搜索并选定一条 SVO 候选（或改选跳过）', 'warning'); return; }
    curve = { kind: 'svo', svo_id: _cfSvoSelected };
  }
  if (!curve) { showToast('请选择一种获取方式', 'warning'); return; }
  try {
    const r = await api('POST', `/filters/${encodeURIComponent(id)}/curve`, { curve });
    bootstrap.Modal.getInstance(document.getElementById('curveFetchModal')).hide();
    showToast(r.warning ? `曲线已写入；${r.warning}` : '曲线已写入', r.warning ? 'warning' : 'success');
    loadFilters();
  } catch (err) {
    showToast(`获取曲线失败: ${err.message}`, 'danger');
  }
};

// matplotlib Spectral 色带锚点（ColorBrewer 9 色），取样 1-t 使短波→蓝/紫、长波→红
const SPECTRAL = [[158,1,66],[213,62,79],[244,109,67],[253,174,97],[254,224,139],
                  [230,245,152],[171,221,164],[102,194,165],[50,136,189]];
function spectralColor(t) {
  t = Math.min(1, Math.max(0, t));
  const x = t * (SPECTRAL.length - 1);
  const i = Math.min(Math.floor(x), SPECTRAL.length - 2);
  const f = x - i;
  const c = SPECTRAL[i].map((v, k) => Math.round(v + (SPECTRAL[i + 1][k] - v) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function wlColor(f) {
  // 按"有曲线的滤光片按波长排序后的序号"归一化；无曲线的滤光片取色阶中点
  const n = _colorCount;
  if (n < 1) return spectralColor(0.5);
  const i = _colorRank.get(f.id);
  const t = (i == null || n === 1) ? 0.5 : i / (n - 1);
  return spectralColor(1 - t);
}

function hasCurve(f) {
  return f.extra_data && f.extra_data.transmission && f.extra_data.transmission.wl;
}

// ─── 透过率总图（显示由行首勾选驱动；点击图例或表格 ID 加粗突出） ───
function rebuildOverview() {
  const canvas = document.getElementById('filterOverviewChart');
  if (!canvas) return;
  if (_overviewChart) { _overviewChart.destroy(); _overviewChart = null; }
  if (typeof Chart === 'undefined') return;
  const cc = chartColors();
  const shown = _filters.filter(f => hasCurve(f) && !_unchecked.has(f.id));
  const mk = f => {
    const color = wlColor(f);
    const hl = _highlight.has(f.id);
    return {
      label: f.id,
      data: f.extra_data.transmission.wl.map((x, i) => ({ x, y: f.extra_data.transmission.tr[i] })),
      borderColor: color,
      backgroundColor: color,   // 图例色块实心填充（line 型 legend 用 backgroundColor 画色块）
      borderWidth: hl ? 3 : 1.3,
      pointRadius: 0,
      pointHitRadius: 5,
      tension: 0,
      fill: false,
      order: hl ? 0 : 1,        // 加粗的先绘制顺序靠前（数值小→后画→置顶），不改 dataset 顺序
    };
  };
  _overviewChart = new Chart(canvas, {
    type: 'line',
    // dataset 顺序固定为列表顺序（图例顺序不变）；置顶绘制靠 dataset.order
    data: { datasets: shown.map(mk) },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          labels: { color: cc.legend, boxWidth: 10, boxHeight: 10, font: { size: 10 } },
          // 点击图例 = 加粗/取消加粗（隐藏由行首勾选框控制）
          onClick: (e, item) => toggleHighlight(item.text),
        },
        tooltip: { backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText },
      },
      scales: {
        x: {
          type: 'linear',
          min: _xMin ?? undefined, max: _xMax ?? undefined,   // 留空 = 自动全范围
          title: { display: true, text: '波长 (Å)', color: cc.tick },
          ticks: { color: cc.tick }, grid: { color: cc.grid },
        },
        y: { min: 0, suggestedMax: 1, title: { display: true, text: '透过率', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
      },
    },
  });
}

function toggleHighlight(id) {
  if (_highlight.has(id)) _highlight.delete(id); else _highlight.add(id);
  rebuildOverview();
}

function applyXRange() {
  const rawMin = (document.getElementById('ovXMin')?.value || '').trim();
  const rawMax = (document.getElementById('ovXMax')?.value || '').trim();
  const mn = rawMin ? Number(rawMin) : null;
  const mx = rawMax ? Number(rawMax) : null;
  if ((rawMin && (!isFinite(mn) || mn <= 0)) || (rawMax && (!isFinite(mx) || mx <= 0))) {
    showToast('波长范围无效：需为正数', 'warning');
    return;
  }
  if (mn != null && mx != null && mn >= mx) {
    showToast('最小波长必须小于最大波长', 'warning');
    return;
  }
  _xMin = mn;
  _xMax = mx;
  rebuildOverview();
}

function syncCheckAll() {
  const all = document.getElementById('filterCheckAll');
  if (all) all.checked = _filters.length > 0 && _filters.every(f => !_unchecked.has(f.id));
}

export async function render() {
  showLoading();
  app.innerHTML = `
    <div class="page-header d-flex justify-content-between align-items-center">
      <h4 class="mb-0"><i class="bi bi-funnel"></i> 光学滤光片</h4>
      <div>
        <button class="btn btn-sm btn-outline-danger" id="filterDelBtn" style="display:none" onclick="filterDeleteSelected()"><i class="bi bi-trash"></i> 删除所选</button>
        <button class="btn btn-sm btn-outline-primary ms-2" id="filterAddBtn" style="display:none" onclick="filterShowAdd()"><i class="bi bi-plus-circle"></i> 添加滤光片</button>
        <small class="text-secondary ms-2" id="filterCount"></small>
      </div>
    </div>
    <!-- 透过率总图 -->
    <div class="card mb-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-graph-up"></i> 透过率总览</span>
        <small class="text-secondary">行首勾选控制显示；点击图例或表格 ID 加粗突出</small>
      </div>
      <div class="card-body">
        <div class="chart-container" style="height:240px"><canvas id="filterOverviewChart"></canvas></div>
        <div class="d-flex flex-wrap align-items-center gap-1 mt-2 small">
          <span class="text-secondary">横轴范围 (Å):</span>
          <input type="number" step="any" class="form-control form-control-sm" style="width:110px" id="ovXMin" placeholder="最小（自动）">
          <span class="text-secondary">~</span>
          <input type="number" step="any" class="form-control form-control-sm" style="width:110px" id="ovXMax" placeholder="最大（自动）">
          <button class="btn btn-sm btn-outline-primary py-0 ms-1" id="ovXApply">应用</button>
          <button class="btn btn-sm btn-outline-secondary py-0" id="ovXReset">恢复自动</button>
          <span class="text-secondary" style="font-size:0.72rem">留空 = 自动全范围</span>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-body p-0">
        <div class="table-responsive">
          <table class="table table-sm table-hover mb-0" style="font-size:0.85rem">
            <thead>
              <tr>
                <th style="width:30px"><input type="checkbox" id="filterCheckAll" checked title="全选/全不选（同步总图显示）"></th>
                <th class="f-sort" data-sort="id" style="cursor:pointer">ID <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="wavelength" style="cursor:pointer">波长 (Å) <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="filter_type" style="cursor:pointer">类型 <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="vega2ab" style="cursor:pointer">Vega→AB <span class="sort-icon"></span></th>
                <th>透过率</th>
                <th>说明</th>
                <th id="filterEditHeader" style="display:none">编辑</th>
              </tr>
            </thead>
            <tbody id="filterBody">
              <tr><td colspan="8" class="text-center text-secondary py-4">加载中...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <!-- 添加弹窗 -->
    <div class="modal fade" id="addFilterModal" tabindex="-1">
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header"><h6 class="mb-0">添加滤光片</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <div class="row g-2">
              <div class="col-4"><label class="form-label small">ID</label><input class="form-control form-control-sm" id="afId"></div>
              <div class="col-4"><label class="form-label small">波长 (Å)</label><input class="form-control form-control-sm" id="afWl" type="number" step="any"></div>
              <div class="col-4"><label class="form-label small">类型</label>
                <select class="form-select form-select-sm" id="afType"><option value="">-</option><option value="mean">mean</option><option value="ref">ref</option><option value="eff">eff</option><option value="guess">guess</option></select>
              </div>
              <div class="col-4"><label class="form-label small">Vega→AB</label><input class="form-control form-control-sm" id="afVega" type="number" step="any" value="0">
                <div class="form-text">AB = Vega + offset</div></div>
              <div class="col-8"><label class="form-label small">说明</label><input class="form-control form-control-sm" id="afDesc"></div>
            </div>
            <hr class="my-2">
            <div class="small text-warning mb-1"><i class="bi bi-exclamation-triangle"></i> 缺失透过率曲线的滤光片无法用于宿主星系拟合等相关过程（可跳过，事后补充）。</div>
            <label class="form-label small mb-1">获取透过率曲线（可选）</label>
            <div class="btn-group btn-group-sm w-100 mb-2" role="group">
              <input type="radio" class="btn-check" name="afCurveKind" id="afCkNone" value="none" checked>
              <label class="btn btn-outline-secondary" for="afCkNone">跳过</label>
              <input type="radio" class="btn-check" name="afCurveKind" id="afCkBuiltin" value="builtin">
              <label class="btn btn-outline-secondary" for="afCkBuiltin">pcigale 内置</label>
              <input type="radio" class="btn-check" name="afCurveKind" id="afCkUpload" value="upload">
              <label class="btn btn-outline-secondary" for="afCkUpload">上传曲线文件</label>
              <input type="radio" class="btn-check" name="afCurveKind" id="afCkSvo" value="svo">
              <label class="btn btn-outline-secondary" for="afCkSvo">SVO 搜索</label>
            </div>
            <div id="afPaneBuiltin" style="display:none">
              <label class="form-label small">映射到 pcigale 自带滤光片</label>
              <select class="form-select form-select-sm" id="afPcigaleName"><option value="">（选择后加载列表）</option></select>
              <div class="form-text">曲线直接取自 pcigale 自带库，无需注册。</div>
            </div>
            <div id="afPaneUpload" style="display:none">
              <input type="file" class="form-control form-control-sm mb-1" id="afCurveFile" accept=".csv,.txt,.dat">
              <textarea class="form-control form-control-sm font-monospace" id="afCurveText" rows="5" placeholder="也可直接粘贴曲线内容"></textarea>
              <ul class="form-text mb-1 ps-3">
                <li>两列：第一列波长（单位 Å），第二列透过率</li>
                <li>无表头；空白行与 # 开头行忽略</li>
                <li>列分隔符自动识别逗号/空白；支持 .csv / .txt / .dat</li>
                <li>透过率请先归一到峰值 ≈ 1</li>
              </ul>
              <button class="btn btn-sm btn-outline-secondary mt-1" onclick="filterCurveValidate()">校验</button>
              <span class="small ms-2" id="afCurveMsg"></span>
            </div>
            <div id="afPaneSvo" style="display:none">
              <div class="input-group input-group-sm mb-1">
                <input class="form-control" id="afSvoQ" placeholder="关键词（如 uvm2）或完整 SVO ID（如 Swift/UVOT.UVM2）">
                <button class="btn btn-outline-secondary" onclick="filterSvoSearch()">搜索</button>
              </div>
              <div id="afSvoMsg" class="small text-secondary"></div>
              <div id="afSvoResults" style="max-height:200px;overflow:auto"></div>
            </div>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
            <button class="btn btn-sm btn-primary" onclick="filterAddSave()">保存</button>
          </div>
        </div>
      </div>
    </div>
    <!-- 编辑补录曲线弹窗 -->
    <div class="modal fade" id="curveFetchModal" tabindex="-1">
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header"><h6 class="mb-0" id="cfTitle">获取透过率曲线</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <input type="hidden" id="cfFid">
            <div class="small text-warning mb-2"><i class="bi bi-exclamation-triangle"></i> 若已有曲线，保存后将覆盖原有曲线（pcigale 自注册条目会重新注册）。</div>
            <div class="btn-group btn-group-sm w-100 mb-2" role="group">
              <input type="radio" class="btn-check" name="cfCurveKind" id="cfCkNone" value="none" checked>
              <label class="btn btn-outline-secondary" for="cfCkNone">跳过</label>
              <input type="radio" class="btn-check" name="cfCurveKind" id="cfCkBuiltin" value="builtin">
              <label class="btn btn-outline-secondary" for="cfCkBuiltin">pcigale 内置</label>
              <input type="radio" class="btn-check" name="cfCurveKind" id="cfCkUpload" value="upload">
              <label class="btn btn-outline-secondary" for="cfCkUpload">上传曲线文件</label>
              <input type="radio" class="btn-check" name="cfCurveKind" id="cfCkSvo" value="svo">
              <label class="btn btn-outline-secondary" for="cfCkSvo">SVO 搜索</label>
            </div>
            <div id="cfPaneBuiltin" style="display:none">
              <label class="form-label small">映射到 pcigale 自带滤光片</label>
              <select class="form-select form-select-sm" id="cfPcigaleName"><option value="">（选择后加载列表）</option></select>
              <div class="form-text">曲线直接取自 pcigale 自带库，无需注册。</div>
            </div>
            <div id="cfPaneUpload" style="display:none">
              <input type="file" class="form-control form-control-sm mb-1" id="cfCurveFile" accept=".csv,.txt,.dat">
              <textarea class="form-control form-control-sm font-monospace" id="cfCurveText" rows="5" placeholder="也可直接粘贴曲线内容"></textarea>
              <ul class="form-text mb-1 ps-3">
                <li>两列：第一列波长（单位 Å），第二列透过率</li>
                <li>无表头；空白行与 # 开头行忽略</li>
                <li>列分隔符自动识别逗号/空白；支持 .csv / .txt / .dat</li>
                <li>透过率请先归一到峰值 ≈ 1</li>
              </ul>
              <button class="btn btn-sm btn-outline-secondary mt-1" onclick="filterCurveFetchValidate()">校验</button>
              <span class="small ms-2" id="cfCurveMsg"></span>
            </div>
            <div id="cfPaneSvo" style="display:none">
              <div class="input-group input-group-sm mb-1">
                <input class="form-control" id="cfSvoQ" placeholder="关键词（如 uvm2）或完整 SVO ID（如 Swift/UVOT.UVM2）">
                <button class="btn btn-outline-secondary" onclick="filterCurveFetchSvoSearch()">搜索</button>
              </div>
              <div id="cfSvoMsg" class="small text-secondary"></div>
              <div id="cfSvoResults" style="max-height:200px;overflow:auto"></div>
            </div>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
            <button class="btn btn-sm btn-primary" onclick="filterCurveFetchSave()">保存曲线</button>
          </div>
        </div>
      </div>
    </div>
    <!-- 单条透过率曲线弹窗 -->
    <div class="modal fade" id="curveModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <div class="modal-header"><h6 class="mb-0" id="curveTitle">透过率曲线</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <div style="position:relative;width:100%;aspect-ratio:1/1"><canvas id="curveCanvas"></canvas></div>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-outline-primary" id="curveDlBtn"><i class="bi bi-download"></i> 下载曲线数据表 (CSV)</button>
          </div>
        </div>
      </div>
    </div>
    <!-- 删除确认密码弹窗 -->
    <div class="modal fade" id="delPwModal" tabindex="-1">
      <div class="modal-dialog modal-sm">
        <div class="modal-content">
          <div class="modal-header"><h6 class="mb-0">删除确认</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <div class="small mb-2">即将删除 <strong id="delPwCount"></strong> 个滤光片。请输入管理员密码以确认：</div>
            <input type="password" class="form-control form-control-sm" id="delPwInput" autocomplete="current-password" placeholder="管理员密码">
            <div class="small text-danger mt-1" id="delPwMsg"></div>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
            <button class="btn btn-sm btn-danger" id="delPwOk">确认删除</button>
          </div>
        </div>
      </div>
    </div>
  `;

  // Sort click handlers
  document.querySelectorAll('.f-sort').forEach(th => {
    th.addEventListener('click', () => {
      const sort = th.dataset.sort;
      if (_sort === sort) {
        _order = _order === 'asc' ? 'desc' : 'asc';
      } else {
        _sort = sort;
        _order = 'asc';
      }
      updateSortIcons();
      loadFilters();
    });
  });

  // 总图横轴范围（应用 / 恢复自动 / 回车）
  document.getElementById('ovXApply').addEventListener('click', applyXRange);
  document.getElementById('ovXReset').addEventListener('click', () => {
    document.getElementById('ovXMin').value = '';
    document.getElementById('ovXMax').value = '';
    _xMin = _xMax = null;
    rebuildOverview();
  });
  ['ovXMin', 'ovXMax'].forEach(id => {
    document.getElementById(id).addEventListener('keydown', e => {
      if (e.key === 'Enter') applyXRange();
    });
  });
  // 已设置的范围回填输入框（重渲染后保留）
  if (_xMin != null) document.getElementById('ovXMin').value = _xMin;
  if (_xMax != null) document.getElementById('ovXMax').value = _xMax;

  // 曲线弹窗：下载数据表按钮（打开时绑定当前滤光片）；关闭时销毁图表
  document.getElementById('curveModal').addEventListener('hidden.bs.modal', () => {
    if (_curveChart) { _curveChart.destroy(); _curveChart = null; }
  });

  // 新增弹窗：获取方式切换 / 文件读入文本框 / 关闭时清理状态
  document.querySelectorAll('input[name=afCurveKind]').forEach(r => {
    r.addEventListener('change', () => {
      _afShowPane(r.value);
      if (r.value === 'builtin') _afLoadBuiltin();
    });
  });
  document.getElementById('afCurveFile').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { document.getElementById('afCurveText').value = reader.result; };
    reader.readAsText(file);
  });
  document.getElementById('addFilterModal').addEventListener('hidden.bs.modal', _afReset);

  // 补录曲线弹窗：获取方式切换 / 文件读入文本框 / 关闭时清理状态
  document.querySelectorAll('input[name=cfCurveKind]').forEach(r => {
    r.addEventListener('change', () => {
      _cfShowPane(r.value);
      if (r.value === 'builtin') _cfLoadBuiltin();
    });
  });
  document.getElementById('cfCurveFile').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { document.getElementById('cfCurveText').value = reader.result; };
    reader.readAsText(file);
  });
  document.getElementById('curveFetchModal').addEventListener('hidden.bs.modal', _cfReset);

  // 删除确认密码弹窗：确认删除按钮（待删 id 由 filterDeleteSelected 存到 dataset）
  document.getElementById('delPwOk').addEventListener('click', async () => {
    const modalEl = document.getElementById('delPwModal');
    const ids = JSON.parse(modalEl.dataset.ids || '[]');
    const pw = document.getElementById('delPwInput').value;
    const msgEl = document.getElementById('delPwMsg');
    if (!pw) { msgEl.textContent = '请输入管理员密码'; return; }
    msgEl.textContent = '';
    let ok = 0;
    const fails = [];
    for (const id of ids) {
      try { await deleteFilter(id, pw); ok++; }
      catch (err) { fails.push(`${id}: ${err.message}`); }
    }
    bootstrap.Modal.getInstance(modalEl).hide();
    showToast(fails.length
      ? `已删除 ${ok} 个；失败 ${fails.length} 个: ${fails.join('; ')}`
      : `已删除 ${ok} 个滤光片`, fails.length ? 'warning' : 'success');
    loadFilters();
  });

  // 表头全选/全不选（同步总图显示）
  document.getElementById('filterCheckAll').addEventListener('change', (e) => {
    _unchecked.clear();
    if (!e.target.checked) _filters.forEach(f => _unchecked.add(f.id));
    document.querySelectorAll('.filter-check').forEach(cb => { cb.checked = e.target.checked; });
    rebuildOverview();
  });

  // 新增滤波器登录即可；编辑/删除仅管理员
  if (isAuthed()) {
    document.getElementById('filterAddBtn').style.display = 'inline-block';
  }
  if (isAdmin()) {
    document.getElementById('filterEditHeader').style.display = '';
    document.getElementById('filterDelBtn').style.display = 'inline-block';
  }

  updateSortIcons();
  await loadFilters();
}

function updateSortIcons() {
  document.querySelectorAll('.f-sort').forEach(th => {
    const icon = th.querySelector('.sort-icon');
    const sort = th.dataset.sort;
    if (sort === _sort) {
      icon.textContent = _order === 'asc' ? ' ▲' : ' ▼';
    } else {
      icon.textContent = '';
    }
  });
}

async function loadFilters() {
  try {
    const filters = await api('GET', `/filters?sort=${_sort}&order=${_order}`);
    _filters = filters;
    // 清理已不存在条目的勾选/加粗状态
    const ids = new Set(filters.map(f => f.id));
    for (const id of [..._unchecked]) if (!ids.has(id)) _unchecked.delete(id);
    for (const id of [..._highlight]) if (!ids.has(id)) _highlight.delete(id);
    // 配色序号：有曲线的滤光片按波长从小到大排序，序号归一化到色阶
    const curved = filters.filter(f => hasCurve(f) && f.wavelength != null)
      .sort((a, b) => a.wavelength - b.wavelength || (a.id < b.id ? -1 : 1));
    _colorRank.clear();
    curved.forEach((f, i) => _colorRank.set(f.id, i));
    _colorCount = curved.length;

    document.getElementById('filterCount').textContent = `共 ${filters.length} 个`;
    const tbody = document.getElementById('filterBody');
    tbody.innerHTML = filters.map(f => `
      <tr id="filterRow_${f.id}">
        <td><input type="checkbox" class="filter-check" data-id="${f.id}" ${_unchecked.has(f.id) ? '' : 'checked'}></td>
        <td><strong style="cursor:pointer;color:${wlColor(f)}" title="点击在总图中加粗/取消加粗" onclick="filterToggleHl('${f.id}')">${f.id}</strong></td>
        <td class="fv" data-field="wavelength">${f.wavelength != null ? f.wavelength.toFixed(2) : '-'}</td>
        <td class="fv" data-field="filter_type">${f.filter_type || '-'}</td>
        <td class="fv" data-field="vega2ab">${f.vega2ab != null ? f.vega2ab.toFixed(3) : '0.000'}</td>
        <td>${hasCurve(f)
          ? `<span class="badge bg-success" style="cursor:pointer" title="点击查看透过率曲线" onclick="filterShowCurve('${f.id}')">曲线 ${f.extra_data.transmission.wl.length} 点</span>`
          : '<span class="badge bg-secondary">—</span>'}</td>
        <td class="fv" data-field="description" style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${f.description || '-'}</td>
        <td class="filter-edit-cell" style="display:${isAdmin() ? '' : 'none'}">
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="filterEditStart('${f.id}')" title="编辑"><i class="bi bi-pencil"></i></button>
        </td>
      </tr>
    `).join('');

    tbody.querySelectorAll('.filter-check').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) _unchecked.delete(cb.dataset.id); else _unchecked.add(cb.dataset.id);
        syncCheckAll();
        rebuildOverview();
      });
    });
    syncCheckAll();
    rebuildOverview();

    // ── 添加 ──
    window.filterShowAdd = () => {
      new bootstrap.Modal(document.getElementById('addFilterModal')).show();
    };
    window.filterAddSave = async () => {
      const id = document.getElementById('afId').value.trim();
      if (!id) { showToast('请输入 ID', 'warning'); return; }
      const body = {
        id, wavelength: parseFloat(document.getElementById('afWl').value) || 0,
        filter_type: document.getElementById('afType').value || null,
        vega2ab: parseFloat(document.getElementById('afVega').value) || 0,
        description: document.getElementById('afDesc').value.trim() || null,
      };
      const kind = (document.querySelector('input[name=afCurveKind]:checked') || {}).value || 'none';
      if (kind === 'builtin') {
        const name = document.getElementById('afPcigaleName').value;
        if (!name) { showToast('请选择 pcigale 内置滤光片（或改选跳过）', 'warning'); return; }
        body.curve = { kind: 'pcigale_builtin', name };
      } else if (kind === 'upload') {
        const text = document.getElementById('afCurveText').value;
        if (!text.trim()) { showToast('请粘贴或上传曲线内容（或改选跳过）', 'warning'); return; }
        body.curve = { kind: 'upload', text };
      } else if (kind === 'svo') {
        if (!_afSvoSelected) { showToast('请先搜索并选定一条 SVO 候选（或改选跳过）', 'warning'); return; }
        body.curve = { kind: 'svo', svo_id: _afSvoSelected };
      }
      try {
        const r = await api('POST', '/filters', body);
        bootstrap.Modal.getInstance(document.getElementById('addFilterModal')).hide();
        showToast(r.warning ? `已添加；${r.warning}` : '已添加', r.warning ? 'warning' : 'success');
        loadFilters();
      } catch (err) {
        showToast(`添加失败: ${err.message}`, 'danger');
      }
    };

    // ── 单条透过率曲线弹窗 ──
    window.filterShowCurve = (id) => {
      const f = _filters.find(x => x.id === id);
      if (!f || !hasCurve(f) || typeof Chart === 'undefined') return;
      if (_curveChart) { _curveChart.destroy(); _curveChart = null; }
      const cc = chartColors();
      const tr = f.extra_data.transmission;
      document.getElementById('curveTitle').textContent = `透过率曲线 — ${f.id}`;
      // 下载该曲线数据表（两列 CSV：wavelength_A,transmission）
      document.getElementById('curveDlBtn').onclick = () => {
        const rows = ['wavelength_A,transmission'];
        for (let i = 0; i < tr.wl.length; i++) rows.push(`${tr.wl[i]},${tr.tr[i]}`);
        const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `filter_${f.id}_transmission.csv`;
        a.click();
        URL.revokeObjectURL(a.href);
      };
      _curveChart = new Chart(document.getElementById('curveCanvas'), {
        type: 'line',
        data: { datasets: [{
          label: f.id,
          data: tr.wl.map((x, i) => ({ x, y: tr.tr[i] })),
          borderColor: wlColor(f),
          borderWidth: 2,
          pointRadius: 0,
          pointHitRadius: 5,
          tension: 0,
          fill: false,
        }] },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          interaction: { mode: 'nearest', intersect: false },
          plugins: { legend: { display: false } },
          scales: {
            x: { type: 'linear', title: { display: true, text: '波长 (Å)', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
            y: { min: 0, suggestedMax: 1, title: { display: true, text: '透过率', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
          },
        },
      });
      new bootstrap.Modal(document.getElementById('curveModal')).show();
    };

    // ── 加粗突出（总图图例点击或表格 ID 点击均走这里） ──
    window.filterToggleHl = (id) => toggleHighlight(id);

    // ── 删除所选（仅管理员；勾选框同时驱动总图显示） ──
    window.filterDeleteSelected = async () => {
      if (!isAdmin()) { showToast('仅管理员可删除数据', 'warning'); return; }
      const ids = _filters.filter(f => !_unchecked.has(f.id)).map(f => f.id);
      if (!ids.length) { showToast('未勾选任何滤光片', 'warning'); return; }
      if (!confirm(`确定删除所选 ${ids.length} 个滤光片？\n${ids.join(', ')}\n\n注意：若有数据使用此波段，统计与消光改正将受影响。`)) return;
      if (!confirm(`再次确认：真的要删除这 ${ids.length} 个滤光片吗？此操作不可撤销。`)) return;
      // 第三次：输入管理员密码（密码弹窗，确认按钮事件在 render() 中绑定一次）
      const modalEl = document.getElementById('delPwModal');
      modalEl.dataset.ids = JSON.stringify(ids);
      document.getElementById('delPwCount').textContent = ids.length;
      document.getElementById('delPwInput').value = '';
      document.getElementById('delPwMsg').textContent = '';
      const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
      modal.show();
      document.getElementById('delPwInput').focus();
    };

    // ── 编辑 ──
    window.filterEditStart = (id) => {
      if (!isAdmin()) { showToast('仅管理员可编辑数据', 'warning'); return; }
      const row = document.getElementById(`filterRow_${id}`);
      if (!row) return;
      const cells = row.querySelectorAll('.fv');
      cells.forEach(cell => {
        const field = cell.dataset.field;
        const val = cell.textContent.trim();
        if (field === 'filter_type') {
          cell.innerHTML = `<select class="form-select form-select-sm fi" data-field="filter_type" style="width:90px">
            <option value="mean" ${val === 'mean' ? 'selected' : ''}>mean</option>
            <option value="ref" ${val === 'ref' ? 'selected' : ''}>ref</option>
            <option value="eff" ${val === 'eff' ? 'selected' : ''}>eff</option>
            <option value="guess" ${val === 'guess' ? 'selected' : ''}>guess</option>
            <option value="" ${val === '-' ? 'selected' : ''}>-</option>
          </select>`;
        } else {
          cell.innerHTML = `<input type="text" class="form-control form-control-sm fi" data-field="${field}" value="${val === '-' ? '' : val}" style="width:${field === 'description' ? 280 : 120}px">`;
        }
      });
      const editCell = row.querySelector('.filter-edit-cell');
      if (editCell) {
        // 取消固定走 filterEditCancel()（重新拉取列表恢复该行显示态），
        // 避免动态赋值时机问题导致个别行点取消无反应
        editCell.innerHTML = `
          <button class="btn btn-sm btn-primary py-0 px-1" onclick="filterEditSave('${id}')" title="保存"><i class="bi bi-check-lg"></i></button>
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="filterEditCancel()" title="取消"><i class="bi bi-x-lg"></i></button>
          <button class="btn btn-sm btn-outline-info py-0 px-1" onclick="filterCurveShowFetch('${id}')" title="获取/补录透过率曲线"><i class="bi bi-graph-up-arrow"></i></button>
        `;
      }
    };

    window.filterEditCancel = () => loadFilters();

    window.filterEditSave = async (id) => {
      const row = document.getElementById(`filterRow_${id}`);
      if (!row) return;
      const inputs = row.querySelectorAll('.fi');
      const body = {};
      inputs.forEach(inp => {
        const field = inp.dataset.field;
        let val = inp.value.trim();
        if (field === 'wavelength') {
          body[field] = val ? parseFloat(val) : null;
        } else if (field === 'vega2ab') {
          body[field] = val !== '' ? parseFloat(val) : 0;
        } else {
          body[field] = val || null;
        }
      });
      try {
        await api('PUT', `/filters/${encodeURIComponent(id)}`, body);
        showToast('已更新', 'success');
        loadFilters();
      } catch (err) {
        showToast(`更新失败: ${err.message}`, 'danger');
      }
    };

  } catch (err) {
    document.getElementById('filterBody').innerHTML =
      `<tr><td colspan="8" class="text-center text-danger py-4">加载失败: ${err.message}</td></tr>`;
  }
}
