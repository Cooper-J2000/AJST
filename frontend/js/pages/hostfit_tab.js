// === 宿主星系标签页（详情页内嵌） ===
// 由 detail.js 在切换到「宿主星系」标签时调用 initHostfitTab(container, tid)，
// 切走或页面重渲染时调用 destroyHostfitTab() 停止轮询。
// 三个卡片：宿主信息（查看/编辑/测光表）、拟合配置（CIGALE 网格）、拟合任务与结果。
import {
  isAuthed, isAdmin, showToast,
  getHost, saveHost,
  getHostfitConfig, submitHostfitJob, getHostfitJobs, getHostfitJob,
  hostfitJobFileUrl, deleteHostfitJob,
} from '../api.js';

const POLL_INTERVAL = 5000;
const MIN_FIT_POINTS = 4;   // 参与拟合的勾选测光点下限

let _tid = null;
let _host = null;           // GET /hosts/<tid> 结果；null = 无记录(404)
let _photRows = [];         // 测光表编辑状态 [{use,band,mag,mag_err,mag_sys,source}]
let _hfConfig = null;       // GET /hostfit/config 缓存
let _jobs = [];
let _pollTimer = null;
let _selectedId = null;     // 当前展开结果的任务 id

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

function numOrNull(v) {
  if (v == null || v === '') return null;
  const n = parseFloat(v);
  return isFinite(n) ? n : null;
}

// 逗号/空白分隔的数列表 → number[]（非法项忽略）
function parseNumList(s) {
  return String(s || '').split(/[,\s]+/).map(parseFloat).filter(isFinite);
}

// 嵌套/点号混合的参数对象 → 拍平成点号键（bayes 可能是 {stellar:{m_star}} 或 {'stellar.m_star'}）
function flattenParams(obj, prefix = '') {
  const out = {};
  for (const [k, v] of Object.entries(obj || {})) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) Object.assign(out, flattenParams(v, key));
    else out[key] = v;
  }
  return out;
}

function paramVal(flatParams, path) {
  const v = (flatParams || {})[path];
  return (v == null || !isFinite(Number(v))) ? null : Number(v);
}

// 参数名人性化
const PARAM_LABELS = {
  'universe.redshift': '红移 z',
  'stellar.m_star': '恒星质量 M* (M☉)',
  'sfh.sfr': '恒星形成率 SFR (M☉/yr)',
  'sfh.age_main': '主星族年龄 (Myr)',
  'sfh.tau_main': '主星族 τ (Myr)',
  'attenuation.Av_ISM': 'ISM 消光 Av (mag)',
  'attenuation.Av_BC': 'BC 消光 Av (mag)',
  'dust.luminosity': '尘埃光度 (L☉)',
};
function paramLabel(name) {
  return PARAM_LABELS[name] || name.replace(/\./g, ' · ');
}

const STATUS_BADGE = {
  pending: '<span class="badge bg-secondary"><i class="bi bi-clock"></i> pending</span>',
  running: '<span class="badge bg-primary"><span class="spinner-border spinner-border-sm" style="width:0.7rem;height:0.7rem"></span> running</span>',
  done: '<span class="badge bg-success">done</span>',
  failed: '<span class="badge bg-danger">failed</span>',
  interrupted: '<span class="badge bg-secondary">interrupted</span>',
};

const MAG_SYS_OPTS = ['AB', 'Vega', 'ST'];

// ─── 入口 / 清理 ───
export async function initHostfitTab(container, tid) {
  destroyHostfitTab();
  _tid = tid;
  _host = null;
  _photRows = [];
  _jobs = [];
  _selectedId = null;
  container.innerHTML = `
    <div class="row g-3">
      <div class="col-lg-6">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-houses"></i> 宿主信息</div>
          <div class="card-body" id="hfHostBody">
            <div class="text-secondary small"><span class="spinner-border spinner-border-sm"></span> 加载宿主信息...</div>
          </div>
        </div>
      </div>
      <div class="col-lg-6">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-cpu"></i> 拟合配置</div>
          <div class="card-body" id="hfConfigBody">
            <div class="text-secondary small"><span class="spinner-border spinner-border-sm"></span> 加载拟合配置...</div>
          </div>
        </div>
      </div>
    </div>
    <div class="row g-3 mt-0">
      <div class="col-12">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="bi bi-list-task"></i> 拟合任务</span>
            <button class="btn btn-sm btn-outline-secondary" id="hfRefreshBtn" title="刷新任务列表">
              <i class="bi bi-arrow-clockwise"></i>
            </button>
          </div>
          <div class="card-body p-0" id="hfJobsBody">
            <div class="text-secondary small p-3"><span class="spinner-border spinner-border-sm"></span> 加载任务...</div>
          </div>
        </div>
      </div>
    </div>
    <div id="hfResultArea" class="mt-3"></div>`;

  document.getElementById('hfRefreshBtn').addEventListener('click', () => refreshJobs());

  // 宿主信息（404 = 无记录，正常情况）与拟合配置并行加载
  try {
    _host = await getHost(tid);
  } catch (e) {
    if (/^404|not found/i.test(e.message)) _host = null;
    else showToast(`宿主信息加载失败: ${e.message}`, 'warning');
  }
  _photRows = ((_host && _host.photometry) || []).map(p => ({
    use: true,
    band: p.band ?? '', mag: p.mag ?? null, mag_err: p.mag_err ?? null,
    mag_sys: p.mag_sys || 'AB', source: p.source ?? '',
  }));
  renderHostCard();

  try {
    if (!_hfConfig) _hfConfig = await getHostfitConfig();
    renderConfigCard();
  } catch (e) {
    const body = document.getElementById('hfConfigBody');
    if (body) body.innerHTML = `<div class="text-danger small">拟合配置加载失败: ${esc(e.message)}</div>`;
  }

  refreshJobs();
}

export function destroyHostfitTab() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  _tid = null;
  _host = null;
  _selectedId = null;
}

// ─── 宿主信息卡 ───
function renderHostCard() {
  const body = document.getElementById('hfHostBody');
  if (!body) return;
  const h = _host || {};
  const authed = isAuthed();
  const d = h.derived || {};
  const derivedBits = [
    d.m_star != null ? `M*=${sci3(d.m_star)} M☉` : null,
    d.sfr != null ? `SFR=${sci3(d.sfr)} M☉/yr` : null,
    d.age_main != null ? `age=${sci3(d.age_main)} Myr` : null,
    d.Av_ISM != null ? `Av=${sci3(d.Av_ISM)}` : null,
    d.chi2 != null ? `χ²=${sci3(d.chi2)}` : null,
  ].filter(Boolean);

  body.innerHTML = `
    ${authed ? '' : `<div class="alert alert-warning py-2 small mb-2">
      <i class="bi bi-lock"></i> 未登录：可浏览宿主信息与拟合结果，保存/提交需要先登录。</div>`}
    ${!_host ? `<div class="text-secondary small mb-2">暂无宿主记录，填写并保存即创建。</div>` : ''}
    <div class="row g-2 small">
      <div class="col-6"><label class="form-label small mb-1">RA (度)</label>
        <input type="number" class="form-control form-control-sm" id="hfRa" step="any" value="${h.ra ?? ''}"></div>
      <div class="col-6"><label class="form-label small mb-1">Dec (度)</label>
        <input type="number" class="form-control form-control-sm" id="hfDec" step="any" value="${h.dec ?? ''}"></div>
      <div class="col-4"><label class="form-label small mb-1">红移</label>
        <input type="number" class="form-control form-control-sm" id="hfZ" step="any" value="${h.redshift ?? ''}"></div>
      <div class="col-4"><label class="form-label small mb-1">红移误差</label>
        <input type="number" class="form-control form-control-sm" id="hfZErr" step="any" value="${h.redshift_err ?? ''}"></div>
      <div class="col-4"><label class="form-label small mb-1">红移类型</label>
        <select class="form-select form-select-sm" id="hfZType">
          <option value="">-</option>
          <option value="spec" ${h.redshift_type === 'spec' ? 'selected' : ''}>spec</option>
          <option value="phot" ${h.redshift_type === 'phot' ? 'selected' : ''}>phot</option>
        </select></div>
      <div class="col-12"><label class="form-label small mb-1">备注</label>
        <input type="text" class="form-control form-control-sm" id="hfComment" value="${esc(h.comment || '')}"></div>
    </div>
    ${derivedBits.length ? `<div class="small mt-2">
      <span class="text-secondary">拟合导出量：</span>${derivedBits.map(b => `<span class="badge-tag badge-neutral">${b}</span>`).join(' ')}
      ${d.fit_at ? `<span class="text-secondary" style="font-size:0.72rem">（任务 #${d.job_id ?? '?'}，${fmtTime(d.fit_at)}）</span>` : ''}
    </div>` : ''}
    <div class="d-flex justify-content-between align-items-center mt-3 mb-1">
      <label class="form-label small mb-0"><i class="bi bi-camera"></i> 宿主测光（勾选行参与拟合）</label>
      <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hfAddPhotRow"><i class="bi bi-plus-lg"></i> 添加行</button>
    </div>
    <div class="table-scroll" style="max-height:240px;overflow-y:auto">
      <table class="table table-sm mb-0" style="font-size:0.78rem">
        <thead><tr>
          <th style="width:28px"></th><th>band</th><th>mag</th><th>mag_err</th><th>星等系统</th><th>source</th><th style="width:30px"></th>
        </tr></thead>
        <tbody id="hfPhotBody"></tbody>
      </table>
    </div>
    <button class="btn btn-sm btn-primary w-100 mt-2" id="hfSaveBtn" ${authed ? '' : 'disabled'}>
      <i class="bi bi-check-lg"></i> 保存宿主信息
    </button>`;

  renderPhotRows();
  document.getElementById('hfAddPhotRow').addEventListener('click', () => {
    collectPhotRows();
    _photRows.push({ use: true, band: '', mag: null, mag_err: null, mag_sys: 'AB', source: '' });
    renderPhotRows();
    updateSubmitState();
  });
  document.getElementById('hfSaveBtn').addEventListener('click', saveHostInfo);
}

function renderPhotRows() {
  const tbody = document.getElementById('hfPhotBody');
  if (!tbody) return;
  if (!_photRows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-secondary small py-2">暂无测光点，点击「添加行」录入</td></tr>';
    return;
  }
  tbody.innerHTML = _photRows.map((p, i) => `
    <tr>
      <td><input class="form-check-input hf-phot-use" type="checkbox" data-i="${i}" ${p.use ? 'checked' : ''} title="参与拟合"></td>
      <td><input type="text" class="form-control form-control-sm hf-phot" data-i="${i}" data-f="band" value="${esc(p.band)}" style="width:80px"></td>
      <td><input type="number" class="form-control form-control-sm hf-phot" data-i="${i}" data-f="mag" value="${p.mag ?? ''}" step="any" style="width:80px"></td>
      <td><input type="number" class="form-control form-control-sm hf-phot" data-i="${i}" data-f="mag_err" value="${p.mag_err ?? ''}" step="any" style="width:76px"></td>
      <td><select class="form-select form-select-sm hf-phot" data-i="${i}" data-f="mag_sys" style="width:76px">
        ${MAG_SYS_OPTS.map(s => `<option value="${s}" ${p.mag_sys === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></td>
      <td><input type="text" class="form-control form-control-sm hf-phot" data-i="${i}" data-f="source" value="${esc(p.source)}" style="width:90px"></td>
      <td><button class="btn btn-sm btn-outline-danger py-0 px-1 hf-phot-del" data-i="${i}" title="删除行"><i class="bi bi-x"></i></button></td>
    </tr>`).join('');

  tbody.querySelectorAll('.hf-phot-use').forEach(cb => {
    cb.addEventListener('change', () => {
      _photRows[parseInt(cb.dataset.i, 10)].use = cb.checked;
      updateSubmitState();
    });
  });
  tbody.querySelectorAll('.hf-phot').forEach(el => {
    el.addEventListener('input', () => {
      const i = parseInt(el.dataset.i, 10), f = el.dataset.f;
      _photRows[i][f] = (f === 'mag' || f === 'mag_err') ? numOrNull(el.value) : el.value;
      updateSubmitState();
    });
  });
  tbody.querySelectorAll('.hf-phot-del').forEach(btn => {
    btn.addEventListener('click', () => {
      collectPhotRows();
      _photRows.splice(parseInt(btn.dataset.i, 10), 1);
      renderPhotRows();
      updateSubmitState();
    });
  });
}

// 把 DOM 中未触发 input 的当前值收回 _photRows（重绘前调用）
function collectPhotRows() {
  document.querySelectorAll('#hfPhotBody .hf-phot').forEach(el => {
    const i = parseInt(el.dataset.i, 10), f = el.dataset.f;
    if (!_photRows[i]) return;
    _photRows[i][f] = (f === 'mag' || f === 'mag_err') ? numOrNull(el.value) : el.value;
  });
  document.querySelectorAll('#hfPhotBody .hf-phot-use').forEach(cb => {
    const i = parseInt(cb.dataset.i, 10);
    if (_photRows[i]) _photRows[i].use = cb.checked;
  });
}

function checkedPhotPoints() {
  collectPhotRows();
  return _photRows.filter(p => p.use && p.band && p.mag != null);
}

async function saveHostInfo() {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  collectPhotRows();
  const photometry = _photRows
    .filter(p => p.band && p.mag != null)
    .map(p => ({ band: p.band, mag: p.mag, mag_err: p.mag_err, mag_sys: p.mag_sys || 'AB', source: p.source || null }));
  const body = {
    ra: numOrNull(document.getElementById('hfRa')?.value),
    dec: numOrNull(document.getElementById('hfDec')?.value),
    redshift: numOrNull(document.getElementById('hfZ')?.value),
    redshift_err: numOrNull(document.getElementById('hfZErr')?.value),
    redshift_type: document.getElementById('hfZType')?.value || null,
    photometry,
    comment: document.getElementById('hfComment')?.value || null,
  };
  const btn = document.getElementById('hfSaveBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 保存中...';
  try {
    _host = await saveHost(_tid, body);
    showToast('宿主信息已保存', 'success');
    renderHostCard();
    renderConfigCard();   // 固定红移模式的只读值跟随刷新
  } catch (e) {
    showToast(`保存失败: ${e.message}`, 'danger');
  } finally {
    if (btn.isConnected) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-check-lg"></i> 保存宿主信息';
    }
  }
}

// ─── 拟合配置卡 ───
function renderConfigCard() {
  const body = document.getElementById('hfConfigBody');
  if (!body || !_hfConfig) return;
  const dft = _hfConfig.defaults || {};
  const authed = isAuthed();
  const listStr = (arr) => Array.isArray(arr) ? arr.join(', ') : '';
  const fixedZ = (_host && _host.redshift != null) ? _host.redshift : '';

  body.innerHTML = `
    <div class="small mb-2">
      <div class="form-check">
        <input class="form-check-input" type="radio" name="hfMode" id="hfModeFixed" value="fixed" checked>
        <label class="form-check-label" for="hfModeFixed">固定红移</label>
        <input type="text" class="form-control form-control-sm d-inline-block ms-2" id="hfFixedZ" value="${fixedZ}"
               readonly style="width:110px" title="取当前宿主红移；保存宿主信息后刷新">
        ${fixedZ === '' ? '<span class="text-secondary" style="font-size:0.72rem">（宿主红移未设置，提交前需先保存）</span>' : ''}
      </div>
      <div class="form-check mt-2">
        <input class="form-check-input" type="radio" name="hfMode" id="hfModePhotoz" value="photoz">
        <label class="form-check-label" for="hfModePhotoz">测光红移</label>
      </div>
      <div class="row g-2 mt-1 ms-3" id="hfZGridRow" style="max-width:420px">
        <div class="col-4"><label class="small text-secondary mb-0">z_min</label>
          <input type="number" class="form-control form-control-sm hf-zgrid" id="hfZMin" step="any" value="${dft.z_min ?? 0}"></div>
        <div class="col-4"><label class="small text-secondary mb-0">z_max</label>
          <input type="number" class="form-control form-control-sm hf-zgrid" id="hfZMax" step="any" value="${dft.z_max ?? 6}"></div>
        <div class="col-4"><label class="small text-secondary mb-0">z_step</label>
          <input type="number" class="form-control form-control-sm hf-zgrid" id="hfZStep" step="any" value="${dft.z_step ?? 0.05}"></div>
      </div>
    </div>
    <div class="mb-2">
      <a class="small text-secondary text-decoration-none" data-bs-toggle="collapse" href="#hfAdvanced" role="button" aria-expanded="false">
        <i class="bi bi-sliders"></i> 高级：参数网格（逗号分隔）
      </a>
      <div class="collapse mt-2" id="hfAdvanced">
        <div class="row g-2">
          <div class="col-12"><label class="small text-secondary mb-0">tau_main (Myr)</label>
            <input type="text" class="form-control form-control-sm hf-grid" id="hfTauMain" value="${esc(listStr(dft.tau_main))}"></div>
          <div class="col-12"><label class="small text-secondary mb-0">age_main (Myr)</label>
            <input type="text" class="form-control form-control-sm hf-grid" id="hfAgeMain" value="${esc(listStr(dft.age_main))}"></div>
          <div class="col-12"><label class="small text-secondary mb-0">Av_ISM (mag)</label>
            <input type="text" class="form-control form-control-sm hf-grid" id="hfAvIsm" value="${esc(listStr(dft.Av_ISM))}"></div>
        </div>
      </div>
    </div>
    <div class="small mb-2">
      <span class="text-secondary">模型组合：</span><code id="hfModuleLine"></code>
      <div class="form-check mt-1">
        <input class="form-check-input hf-optmod" type="checkbox" id="hfUseNebular">
        <label class="form-check-label" for="hfUseNebular">星云发射（nebular）</label>
      </div>
      <div class="form-check">
        <input class="form-check-input hf-optmod" type="checkbox" id="hfUseDl2014">
        <label class="form-check-label" for="hfUseDl2014">尘埃红外发射（dl2014）</label>
      </div>
      <div class="text-secondary mt-1" style="font-size:0.78rem" id="hfGridHint"></div>
    </div>
    <div class="small text-secondary mb-2" id="hfFitHint"></div>
    <button class="btn btn-sm btn-primary w-100" id="hfSubmitBtn" ${authed ? '' : 'disabled'}>
      <i class="bi bi-play-fill"></i> 提交拟合
    </button>`;

  body.querySelectorAll('input[name="hfMode"]').forEach(r => {
    r.addEventListener('change', () => { updateGridHint(); updateSubmitState(); });
  });
  body.querySelectorAll('.hf-grid, .hf-zgrid, .hf-optmod').forEach(el => {
    el.addEventListener('input', updateGridHint);
    el.addEventListener('change', updateGridHint);
  });
  updateModuleLine();
  updateGridHint();
  updateSubmitState();
  document.getElementById('hfSubmitBtn').addEventListener('click', submitJob);
}

// 任务实际使用的模块组合（从任务 config 还原）
function jobModules(detail) {
  const base = (_hfConfig && _hfConfig.modules || '').split('+').filter(Boolean);
  const mods = [];
  const cfg = detail.config || {};
  base.forEach(m => {
    mods.push(m);
    if (m === 'bc03' && cfg.use_nebular) mods.push('nebular');
    if (m === 'dustatt_modified_CF00' && cfg.use_dl2014) mods.push('dl2014');
  });
  return '模型：' + mods.join(' + ');
}

// 模型组合展示：基础链 + 勾选的可选模块
function updateModuleLine() {  const el = document.getElementById('hfModuleLine');
  if (!el || !_hfConfig) return;
  const base = (_hfConfig.modules || '').split('+').filter(Boolean);
  const mods = [];
  base.forEach(m => {
    mods.push(m);
    if (m === 'bc03' && document.getElementById('hfUseNebular')?.checked) mods.push('nebular');
    if (m === 'dustatt_modified_CF00' && document.getElementById('hfUseDl2014')?.checked) mods.push('dl2014');
  });
  el.textContent = mods.join(' + ');
}

// 网格规模提示：模型数 = tau×age×Av×（测光红移模式的 z 网格数）
function updateGridHint() {
  const el = document.getElementById('hfGridHint');
  if (!el) return;
  const n = (id) => parseNumList(document.getElementById(id)?.value).length;
  let models = Math.max(1, n('hfTauMain')) * Math.max(1, n('hfAgeMain')) * Math.max(1, n('hfAvIsm'));
  const mode = document.querySelector('input[name="hfMode"]:checked')?.value;
  if (mode === 'photoz') {
    const zmin = numOrNull(document.getElementById('hfZMin')?.value);
    const zmax = numOrNull(document.getElementById('hfZMax')?.value);
    const zstep = numOrNull(document.getElementById('hfZStep')?.value);
    if (zmin != null && zmax != null && zstep >  0) {
      models *= Math.floor((zmax - zmin) / zstep) + 1;
    }
  }
  const est = Math.ceil(models / 40);  // 实测约 40 模型/秒
  el.innerHTML = `模型数约 <b>${models.toLocaleString()}</b>，预计耗时约 ${est} 秒` +
    (models > 20000 ? ' <span class="text-danger"><i class="bi bi-exclamation-triangle"></i> 网格过大，可能超过 600s 超时被杀</span>'
      : models > 5000 ? ' <span class="text-warning">网格偏大，注意耗时</span>' : '');
}

// 勾选测光点 ≥4 且已登录才可提交；不足则禁用并提示
function updateSubmitState() {
  const btn = document.getElementById('hfSubmitBtn');
  const hint = document.getElementById('hfFitHint');
  if (!btn) return;
  const pts = checkedPhotPoints();
  const authed = isAuthed();
  const enough = pts.length >= MIN_FIT_POINTS;
  btn.disabled = !authed || !enough;
  if (hint) {
    hint.innerHTML = !authed
      ? '<i class="bi bi-lock"></i> 登录后可提交拟合'
      : `参与拟合的测光点：<b>${pts.length}</b> / 至少 ${MIN_FIT_POINTS} 个` +
        (enough ? '' : '（在左侧测光表中勾选/录入更多点）');
  }
}

async function submitJob() {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  const mode = document.querySelector('input[name="hfMode"]:checked')?.value || 'fixed';
  const photometry = checkedPhotPoints().map(p => ({
    band: p.band, mag: p.mag, mag_err: p.mag_err, mag_sys: p.mag_sys || 'AB', source: p.source || null,
  }));
  if (photometry.length < MIN_FIT_POINTS) {
    showToast(`参与拟合的测光点不足 ${MIN_FIT_POINTS} 个`, 'warning');
    return;
  }
  const grid = {
    tau_main: parseNumList(document.getElementById('hfTauMain')?.value),
    age_main: parseNumList(document.getElementById('hfAgeMain')?.value),
    Av_ISM: parseNumList(document.getElementById('hfAvIsm')?.value),
  };
  const payload = { transient_id: _tid, mode, grid, photometry,
                    use_nebular: !!document.getElementById('hfUseNebular')?.checked,
                    use_dl2014: !!document.getElementById('hfUseDl2014')?.checked };
  if (mode === 'fixed') {
    const z = numOrNull(document.getElementById('hfFixedZ')?.value);
    if (z == null) { showToast('固定红移模式需要先在宿主信息中设置并保存红移', 'warning'); return; }
    payload.redshift = z;
  } else {
    grid.z_min = numOrNull(document.getElementById('hfZMin')?.value);
    grid.z_max = numOrNull(document.getElementById('hfZMax')?.value);
    grid.z_step = numOrNull(document.getElementById('hfZStep')?.value);
    if (grid.z_min == null || grid.z_max == null || !(grid.z_step > 0)) {
      showToast('测光红移模式需要有效的 z_min / z_max / z_step', 'warning');
      return;
    }
  }

  const btn = document.getElementById('hfSubmitBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 提交中...';
  try {
    const res = await submitHostfitJob(payload);
    showToast(`拟合任务 #${res.id} 已提交`, 'success');
    await refreshJobs();
  } catch (e) {
    showToast(`提交失败: ${e.message}`, 'danger');
  } finally {
    updateSubmitState();
    if (btn.isConnected) btn.innerHTML = '<i class="bi bi-play-fill"></i> 提交拟合';
  }
}

// ─── 任务列表 ───
async function refreshJobs() {
  if (!_tid) return;
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  try {
    _jobs = await getHostfitJobs(_tid);
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
      const area = document.getElementById('hfResultArea');
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
  const body = document.getElementById('hfJobsBody');
  if (!body) return;
  if (!_jobs.length) {
    body.innerHTML = '<div class="text-secondary small p-3">暂无拟合任务</div>';
    return;
  }
  body.innerHTML = `
    <div class="table-scroll" style="max-height:400px;overflow-y:auto">
      <table class="table table-sm table-hover mb-0" style="font-size:0.8rem">
        <thead><tr>
          <th>#</th><th>创建时间</th><th>模式</th><th>状态</th><th>χ²</th><th></th>
        </tr></thead>
        <tbody>
          ${_jobs.map(j => `
            <tr data-jobid="${j.id}" class="${j.id === _selectedId ? 'table-active' : ''}" style="cursor:pointer">
              <td>${j.id}</td>
              <td class="text-nowrap small">${fmtTime(j.created_at)}</td>
              <td>${j.mode === 'photoz' ? '测光红移' : (j.mode === 'fixed' ? '固定红移' : (j.mode || '-'))}</td>
              <td>${STATUS_BADGE[j.status] || esc(j.status)}</td>
              <td>${j.chi_squared != null ? sci3(j.chi_squared) : '-'}</td>
              <td class="text-nowrap">
                ${j.status === 'done' ? `<button class="btn btn-sm btn-outline-primary py-0 px-1 hf-view" data-jobid="${j.id}">查看结果</button>` : ''}
                ${isAdmin() && ['done', 'failed', 'interrupted'].includes(j.status)
                  ? `<button class="btn btn-sm btn-outline-danger py-0 px-1 hf-del" data-jobid="${j.id}" title="删除"><i class="bi bi-trash"></i></button>` : ''}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>`;

  body.querySelectorAll('.hf-view').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      loadResult(parseInt(btn.dataset.jobid, 10));
    });
  });
  body.querySelectorAll('.hf-del').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = parseInt(btn.dataset.jobid, 10);
      if (!confirm(`删除拟合任务 #${id} 及其产物？`)) return;
      try {
        await deleteHostfitJob(id);
        showToast(`任务 #${id} 已删除`, 'success');
        if (_selectedId === id) {
          _selectedId = null;
          const area = document.getElementById('hfResultArea');
          if (area) area.innerHTML = '';
        }
        await refreshJobs();
      } catch (err) {
        showToast(`删除失败: ${err.message}`, 'danger');
      }
    });
  });
  body.querySelectorAll('tr[data-jobid]').forEach(row => {
    row.addEventListener('click', () => {
      const id = parseInt(row.dataset.jobid, 10);
      const job = _jobs.find(j => j.id === id);
      if (job && job.status === 'done') loadResult(id);
    });
  });
}

// ─── 结果展示 ───
async function loadResult(jobId) {
  const area = document.getElementById('hfResultArea');
  if (!area) return;
  _selectedId = jobId;
  renderJobs();  // 高亮选中行
  area.innerHTML = `<div class="card"><div class="card-body text-secondary small">
    <span class="spinner-border spinner-border-sm"></span> 加载任务 #${jobId} 结果...</div></div>`;

  let detail;
  try {
    detail = await getHostfitJob(jobId);
  } catch (e) {
    area.innerHTML = `<div class="card"><div class="card-body text-danger small">结果加载失败: ${esc(e.message)}</div></div>`;
    return;
  }

  const best = flattenParams(detail.parameters && detail.parameters.best);
  const bayes = flattenParams(detail.parameters && detail.parameters.bayes);
  const bayesErr = (detail.parameters && detail.parameters.bayes_err) || {};
  const names = [...new Set([...Object.keys(best), ...Object.keys(bayes)])].sort();
  const authed = isAuthed();
  const done = detail.status === 'done';
  const ts = Date.now();  // 防缓存

  area.innerHTML = `
    <div class="card">
      <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
        <span><i class="bi bi-graph-up-arrow"></i> 拟合结果 — 任务 #${detail.id}
          <span class="text-secondary small">${detail.mode === 'photoz' ? '测光红移' : '固定红移'} · χ²=${detail.chi_squared != null ? sci3(detail.chi_squared) : '-'}</span>
          <div class="text-secondary" style="font-size:0.72rem">${esc(jobModules(detail))}</div></span>
        ${authed && done ? `<button class="btn btn-sm btn-outline-success" id="hfWriteHost">
          <i class="bi bi-box-arrow-in-down"></i> 写入宿主信息</button>` : ''}
      </div>
      <div class="card-body">
        <div class="row g-3">
          <div class="col-lg-5">
            <div class="table-scroll" style="max-height:380px;overflow-y:auto">
              <table class="table table-sm mb-0" style="font-size:0.8rem">
                <thead><tr><th>参数</th><th>best</th><th>bayes ± err</th></tr></thead>
                <tbody>
                  ${names.length ? names.map(n => `
                    <tr>
                      <td class="text-nowrap" title="${esc(n)}">${esc(paramLabel(n))}</td>
                      <td>${best[n] != null ? sci3(Number(best[n])) : '-'}</td>
                      <td>${bayes[n] != null
                        ? `${sci3(Number(bayes[n]))}${bayesErr[n] != null ? ` ± ${sci3(Number(bayesErr[n]))}` : ''}`
                        : '-'}</td>
                    </tr>`).join('')
                  : '<tr><td colspan="3" class="text-secondary text-center py-2">无参数结果</td></tr>'}
                </tbody>
              </table>
            </div>
          </div>
          <div class="col-lg-7">
            <div class="small text-secondary mb-1">最佳模型 SED</div>
            <img src="${hostfitJobFileUrl(jobId, 'sed_png')}?t=${ts}" class="img-fluid rounded border" alt="SED"
                 onerror="this.outerHTML='<div class=\\'text-secondary small\\'>SED 图加载失败（任务可能未产出图）</div>'">
          </div>
        </div>
        <div class="mt-3 d-flex flex-wrap gap-2">
          <a class="btn btn-sm btn-outline-secondary" href="${hostfitJobFileUrl(jobId, 'best_model')}" download>
            <i class="bi bi-download"></i> best_model.fits</a>
          <a class="btn btn-sm btn-outline-secondary" href="${hostfitJobFileUrl(jobId, 'results')}" download>
            <i class="bi bi-download"></i> results.txt</a>
          <a class="btn btn-sm btn-outline-secondary" href="${hostfitJobFileUrl(jobId, 'log')}" download>
            <i class="bi bi-download"></i> run.log</a>
        </div>
      </div>
    </div>`;

  const writeBtn = document.getElementById('hfWriteHost');
  if (writeBtn) writeBtn.addEventListener('click', () => writeToHost(detail));
}

// 「写入宿主信息」：把 bayes 结果（photoz 时含红移）PUT 回 /hosts/<tid>
async function writeToHost(detail) {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  if (!confirm(`将任务 #${detail.id} 的拟合结果写入 ${_tid} 的宿主信息？（覆盖 derived 字段）`)) return;
  const bayes = flattenParams(detail.parameters && detail.parameters.bayes);
  const bayesErr = (detail.parameters && detail.parameters.bayes_err) || {};
  const derived = {
    m_star: paramVal(bayes, 'stellar.m_star'),
    sfr: paramVal(bayes, 'sfh.sfr'),
    age_main: paramVal(bayes, 'sfh.age_main'),
    Av_ISM: paramVal(bayes, 'attenuation.Av_ISM'),
    chi2: detail.chi_squared ?? null,
    fit_at: new Date().toISOString(),
    job_id: detail.id,
  };
  for (const k of Object.keys(derived)) if (derived[k] == null) delete derived[k];
  const body = { derived };
  if (detail.mode === 'photoz') {
    const z = paramVal(bayes, 'universe.redshift');
    if (z != null) {
      body.redshift = z;
      body.redshift_type = 'phot';
      const zerr = bayesErr['universe.redshift'];
      if (zerr != null && isFinite(Number(zerr))) body.redshift_err = Number(zerr);
    }
  }
  try {
    _host = await saveHost(_tid, body);
    showToast('拟合结果已写入宿主信息', 'success');
    renderHostCard();
    renderConfigCard();
  } catch (e) {
    showToast(`写入失败: ${e.message}`, 'danger');
  }
}
