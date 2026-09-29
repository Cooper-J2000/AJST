// === 模板库与造模板向导（#/tools/tmplib） ===
// 设计文档 02：U-10..U-22 / API-1/2/3/4/5/7/11/12/13 / F-45 / ST-18 / IA-13
//
// 纪律：前端零天文算术（ST-9）——行账/域内率/Δμ 数字全部来自后端；
// 本页唯一的计算是勾选集合与表单校验。建面唯一触发点是第 4 步按钮（U-17/POS-3）。
import {
  getTmplibConfig, getTmplibGuards, getTmplibTemplates, getTmplibTemplate,
  getTmplibPreview, createTmplibTemplate, rebuildTmplibTemplate,
  deleteTmplibTemplate, budgetTmplib, isAuthed, isAdmin, showToast,
} from '../api.js';
import { esc } from '../utils.js';
import { sortBandsByFreq } from '../bands.js';

// ─── 文案（TXT-*；按 code/kind 分支，不匹配 message —— E-30） ───
const TXT = {
  engineDown: 'K 改正引擎当前不可用，模板库整页降级为只读说明。其余页面不受影响。',
  lowZ: (z) => `z=${z} < 0.02：本征速度给出的距离误差比宇宙学本身还大 ⇒ 宇宙学 d_L 在此不是一个测量。请填写实测 μ / d_L，或显式勾选"我知道这是近似"。`,
  errKind: '引擎要求红移误差必须说明它测的是什么，三者互不等价：① line-precision 单条谱线质心的 1σ；② line-scatter 多条谱线相对系统速度的弥散 1σ；③ unestablished 论文印了 ± 却没说是什么。只有 ①② 会进入误差预算，③ 不传播。',
  inDomain: (ok, n, pct) => `本模板能回答自身表中 ${ok}/${n} = ${pct}% 个实测点（口径：一个点 = 一对 (波段, 静止系 epoch)，predict 返回有限星等即算答上）。这不是覆盖率也不是精度，是"论文发表了而这个面答不上"的条数：${n - ok} 条。`,
  position: '把库内 RA/Dec 写进 manifest 可让引擎按位置改正红移（实测后果 Δμ≈0.0026 mag）。默认勾选；不勾则只用 z 标量。',
  nullPolicy: '测光系统零猜测（F-10）：drop 逐行剔除缺系统的行（默认，全库 1030 个源受影响）；declare 由你按波段声明系统并记进 declarations；drop-band 整波段剔除；drop-source 有任何缺系统行即整源作废（全库 145 个源会作废）。',
};

const STATE_BADGE = {
  fresh: ['text-bg-success', 'fresh'],
  stale: ['text-bg-warning', 'stale'],
  'never-built': ['text-bg-secondary', 'never-built'],
  refused: ['text-bg-danger', 'refused'],
  deleted: ['text-bg-secondary', 'deleted'],
};
const ORIGIN_LABEL = { shipped: '出厂', catalog: '向导', 'catalog-derived': '出厂·同源' };

// ─── 模块级状态 ───
let _cfg = null;         // API-1
let _guards = null;      // API-13
let _templates = null;   // API-2
let _wiz = null;         // 向导状态（见 _wizReset）
let _detailId = null;

function _wizReset() {
  _wiz = {
    step: 1, src: '', preview: null, previewKey: '',
    bandSel: null,            // Set；null = 全部 registry 波段（尚未改勾）
    rowset: 'raw', policy: 'drop', declare: {},
    fields: null,             // 第 3 步表单值（进入第 3 步时从 preview 预填）
    building: false, result: null, buildError: null,
  };
}

// ─── 入口 ───
export async function render() {
  _wizReset(); _detailId = null;
  const app = document.getElementById('app');
  app.innerHTML = `
    <h4 class="mb-3"><i class="bi bi-grid-3x3-gap"></i> 模板库与造模板</h4>
    <div id="tlGuards" class="mb-3"><span class="text-secondary small">守卫状态加载中…</span></div>
    <div id="tlMain" style="display:none">
      <div class="card mb-3">
        <div class="card-header py-1 d-flex justify-content-between align-items-center">
          <span>模板库清单（U-11）</span>
          <span class="d-flex gap-1 align-items-center">
            <span class="small text-secondary" id="tlBatchMsg"></span>
            <button class="btn btn-sm btn-outline-warning py-0" id="tlBatchRebuild" style="display:none"
                    title="勾选的 stale 模板逐条走 API-7 重建（前端串行，不是新端点）">批量重建</button>
          </span>
        </div>
        <div class="card-body p-0"><div class="table-responsive">
          <table class="table table-sm table-hover mb-0 small" id="tlLibTable">
            <thead><tr>
              <th></th><th>id</th><th>名称</th><th>来源</th><th>z</th><th>距离 kind</th>
              <th>波段</th><th>t_valid (d)</th><th>域内率</th><th>状态</th><th></th>
            </tr></thead>
            <tbody id="tlLibBody"><tr><td colspan="11" class="text-secondary">加载中…</td></tr></tbody>
          </table>
        </div></div>
        <div class="card-footer py-1">
          <details id="tlHistory"><summary class="small">族谱（library.json history：build / rebuild / delete / declare，只读）</summary>
            <div class="small mt-1" id="tlHistoryBody"><span class="text-secondary">加载中…</span></div>
          </details>
        </div>
      </div>
      <div class="card mb-3" id="tlDetailCard" style="display:none">
        <div class="card-header py-1">模板详情 / QC（④）<span id="tlDetailTitle"></span></div>
        <div class="card-body py-2 small" id="tlDetailBody"></div>
      </div>
      <div class="card mb-3" id="tlWizardCard">
        <div class="card-header py-1">造模板向导（S2；4 步，任一跳过都会被拦）</div>
        <div class="card-body py-2" id="tlWizardBody"></div>
      </div>
    </div>
    <div id="tlDown" class="alert alert-warning" style="display:none"></div>`;

  try {
    _cfg = await getTmplibConfig();
  } catch (e) {
    document.getElementById('tlGuards').innerHTML =
      `<span class="badge text-bg-danger">配置接口失败</span> <span class="small text-danger">${esc(e.message)}</span>`;
    return;
  }
  try { _guards = await getTmplibGuards(); } catch { _guards = null; }
  _renderGuards();
  if (!_cfg.available) {                       // ST-18：整页降级，不出现向导
    const el = document.getElementById('tlDown');
    el.style.display = '';
    el.textContent = `${TXT.engineDown}（${(_cfg.unavailable_reasons || []).map(r => r.code).join('、') || '未知原因'}）`;
    return;
  }
  document.getElementById('tlMain').style.display = '';
  _loadLibrary();
  _renderWizard();
}

// ─── ① 守卫状态条（U-10：API-1 + API-13，三轴分色 IA-13/ST-15/ST-16） ───
function _axisBadge(name, ax) {
  if (!ax) return `<span class="badge text-bg-secondary" title="该轴未报告">${name}: ?</span>`;
  if (ax.ok === true) return `<span class="badge text-bg-success">${name}: 一致</span>`;
  if (ax.ok === null || ax.ok === undefined)
    return `<span class="badge text-bg-secondary" title="${esc(ax.note || ax.error || '无法判定')}">${name}: 未定</span>`;
  const why = [];
  if (ax.stale && ax.stale.length) why.push(`stale: ${ax.stale.join('、')}`);
  if (ax.code) why.push(ax.code);
  if (ax.error) why.push(ax.error);
  return `<span class="badge text-bg-danger" title="${esc(why.join('；') || '不一致')}">${name}: 漂移</span>`;
}

function _renderGuards() {
  const el = document.getElementById('tlGuards');
  if (!el) return;
  const eng = _cfg.available
    ? `<span class="badge text-bg-success">引擎 chromashift ${esc(_cfg.engine.version)}</span>`
    : `<span class="badge text-bg-danger">引擎不可用</span>`;
  const axes = (_guards && _guards.axes) || {};
  // catalog_rows 轴含漂移探针语义（F-19）：probe-inconclusive 不算红
  const cr = axes.catalog_rows || {};
  const drifted = Object.entries(cr.templates || {}).filter(([, t]) => t.status === 'drifted');
  const probes = Object.entries(cr.templates || {}).filter(([, t]) => t.status === 'probe-inconclusive');
  const crBadge = cr.ok === false
    ? `<span class="badge text-bg-danger" title="${esc(drifted.map(([t]) => t).join('、'))}">库行指纹: 漂移×${drifted.length}</span>`
    : probes.length
      ? `<span class="badge text-bg-warning" title="${esc(probes.map(([t]) => t).join('、'))}（与本库同源，差异待核 —— F-19）">库行指纹: 探针×${probes.length}</span>`
      : _axisBadge('库行指纹', cr);
  el.innerHTML = [
    eng,
    _axisBadge('引擎输入', axes.engine_inputs),
    crBadge,
    _axisBadge('滤光片 vendor', axes.filter_vendor),
    (_cfg.library ? `<span class="badge text-bg-light border">已登记 ${_cfg.library.templates_registered} 模板</span>` : ''),
  ].join(' ');
}

// ─── ② 库清单（U-11） ───
let _history = [];       // library.json history（族谱，S4 只读展示）

async function _loadLibrary() {
  try {
    const data = await getTmplibTemplates();
    _templates = data.templates || [];
    _history = data.history || [];
  } catch (e) {
    _templates = null;
    _history = [];
    const tb = document.getElementById('tlLibBody');
    if (tb) tb.innerHTML = `<tr><td colspan="11" class="text-danger">${esc(e.message)}</td></tr>`;
    return;
  }
  _renderLibrary();
  _renderHistory();
}

function _renderHistory() {
  const body = document.getElementById('tlHistoryBody');
  if (!body) return;
  if (!_history.length) {
    body.innerHTML = '<span class="text-secondary">尚无历史记录</span>';
    return;
  }
  const ACTION_LABEL = { build: '建面', rebuild: '重建', delete: '删除', declare: '声明' };
  body.innerHTML = `<table class="table table-sm small mb-0" style="max-width:760px">
    <thead><tr><th>时刻 (UTC)</th><th>动作</th><th>id</th><th>操作者</th><th>备注</th></tr></thead>
    <tbody>${_history.slice().reverse().map(h => `<tr>
      <td class="text-nowrap">${esc(h.at || '')}</td>
      <td>${esc(ACTION_LABEL[h.action] || h.action || '')}</td>
      <td><code>${esc(h.id || '')}</code></td>
      <td>${esc(h.by || '—')}</td>
      <td class="text-secondary">${esc(h.note || '')}</td>
    </tr>`).join('')}</tbody></table>`;
}

function _staleTitle(sb) {
  if (!sb) return '';
  const parts = [];
  if (sb.engine_inputs && sb.engine_inputs.length) parts.push(`引擎输入: ${sb.engine_inputs.join('、')}`);
  if (sb.catalog_rows && sb.catalog_rows.length) parts.push(`库行: ${sb.catalog_rows.join('、')}`);
  if (sb.filter_vendor && sb.filter_vendor.length) parts.push(`vendor: ${sb.filter_vendor.join('、')}`);
  return parts.join('；');
}

function _renderLibrary() {
  const tb = document.getElementById('tlLibBody');
  if (!tb) return;
  if (!_templates.length) {
    tb.innerHTML = '<tr><td colspan="11" class="text-secondary">库为空</td></tr>';
    return;
  }
  tb.innerHTML = _templates.map(t => {
    const [cls, label] = STATE_BADGE[t.state] || ['text-bg-secondary', t.state || '?'];
    const idom = t.in_domain;
    const idomTxt = idom && idom.total ? `${idom.answered}/${idom.total}` : '—';
    const tv = t.t_valid_days ? `${t.t_valid_days[0].toFixed(1)}…${t.t_valid_days[1].toFixed(1)}` : '—';
    const canRebuild = isAuthed() && t.state === 'stale';
    const canDelete = isAdmin() && t.origin === 'catalog';
    return `<tr class="tl-row ${t.state === 'stale' ? 'opacity-75' : ''}" data-id="${esc(t.id)}" style="cursor:pointer">
      <td>${canRebuild ? `<input type="checkbox" class="form-check-input tl-batch-cb" data-id="${esc(t.id)}" title="勾选后可批量重建">` : ''}</td>
      <td><code>${esc(t.id)}</code></td>
      <td>${esc(t.label || '')}</td>
      <td>${esc(ORIGIN_LABEL[t.origin] || t.origin)}</td>
      <td>${t.z != null ? t.z : '—'}</td>
      <td>${esc(t.distance_kind || '—')}</td>
      <td>${(t.bands || []).length ? esc(sortBandsByFreq(t.bands).join(' ')) : '—'}</td>
      <td>${tv}</td><td>${idomTxt}</td>
      <td><span class="badge ${cls}" title="${esc(_staleTitle(t.stale_because))}">${label}</span></td>
      <td class="text-nowrap">
        ${canRebuild ? `<button class="btn btn-sm btn-outline-warning py-0 tl-rebuild" data-id="${esc(t.id)}" title="重建面（API-7）">重建</button>` : ''}
        ${canDelete ? `<button class="btn btn-sm btn-outline-danger py-0 tl-delete" data-id="${esc(t.id)}" title="删除（API-11）">删除</button>` : ''}
      </td></tr>`;
  }).join('');
  _refreshBatchButton();
}

// 批量重建（S4）：勾选的 stale 模板逐条走 API-7，前端串行（不是新端点）
function _refreshBatchButton() {
  const btn = document.getElementById('tlBatchRebuild');
  if (!btn) return;
  const n = document.querySelectorAll('.tl-batch-cb:checked').length;
  btn.style.display = n ? '' : 'none';
  btn.textContent = `批量重建（${n}）`;
}

let _batchRunning = false;
async function _doBatchRebuild() {
  if (_batchRunning) return;
  const ids = [...document.querySelectorAll('.tl-batch-cb:checked')].map(cb => cb.dataset.id);
  if (!ids.length) return;
  _batchRunning = true;
  const msg = document.getElementById('tlBatchMsg');
  const btn = document.getElementById('tlBatchRebuild');
  if (btn) btn.disabled = true;
  const results = [];
  for (const [i, id] of ids.entries()) {          // 串行：一条完了再下一条
    if (msg) msg.textContent = `重建中 ${i + 1}/${ids.length}：${id}…`;
    try {
      const res = await rebuildTmplibTemplate(id);
      results.push(`${id} ✓(${res.state || 'fresh'})`);
    } catch (e) {
      results.push(`${id} ✕(${e.message})`);
    }
  }
  _batchRunning = false;
  if (msg) msg.textContent = '';
  showToast(`批量重建完成：${results.join('；')}`,
            results.some(r => r.includes('✕')) ? 'warning' : 'success');
  _loadLibrary();
}

// ─── ④ 模板详情 / QC ───
async function _loadDetail(id) {
  _detailId = id;
  const card = document.getElementById('tlDetailCard');
  const body = document.getElementById('tlDetailBody');
  card.style.display = '';
  document.getElementById('tlDetailTitle').textContent = ` — ${id}`;
  body.innerHTML = '<span class="text-secondary">加载中…</span>';
  try {
    const d = await getTmplibTemplate(id);
    body.innerHTML = _detailHTML(d);
  } catch (e) {
    body.innerHTML = `<span class="text-danger">${esc(e.message)}</span>`;
  }
}

// ─── ④b 误差预算面板（API-10；F-34/35/36/37/39） ───
function _budgetCtlHTML(d) {
  const bands = sortBandsByFreq((d.surface && d.surface.bands) || Object.keys(d.per_band || {}));
  const z = (d.manifest || {}).z;
  return `
    <div class="fw-bold mt-2">误差预算（API-10）</div>
    <div class="d-flex flex-wrap gap-1 align-items-center mb-1">
      <select class="form-select form-select-sm" id="tlBudgetBand" style="width:auto" title="预算针对的观测波段">
        ${bands.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('')}
      </select>
      <input class="form-control form-control-sm" id="tlBudgetZ" style="width:90px"
             value="${z != null ? z : ''}" placeholder="z" title="红移（默认模板自身 z）">
      <input class="form-control form-control-sm" id="tlBudgetDraws" style="width:150px"
             placeholder="n_draws 默认 32" title="Monte-Carlo 次数：0=只算系统项；3–200；默认 32（实测 2.4–3.1 s；200 实测 13.7–18.1 s）">
      <button class="btn btn-sm btn-outline-primary py-0" id="tlBudgetRun">计算预算</button>
    </div>
    <div id="tlBudgetOut"></div>`;
}

function _budgetHTML(d) {
  const b = d.budget || {};
  const terms = b.terms_max_mag || {};
  const comps = b.components || {};
  const photo = comps.photometric || {};
  const perBand = photo.per_band || {};
  const sed = comps.sed_residual || {};
  const allBands = sed.all_bands_mag || {};
  const dist = d.distance || {};
  const TERM_LABEL = { photometric: 'photometric（测光 MC）', sed_residual: 'sed_residual（SED 残差）',
                       colour_term: 'colour_term（模式选择）', distance: 'distance（距离）' };
  const termRows = Object.entries(TERM_LABEL).map(([k, label]) => {
    if (!(k in terms))  // F-34：不可测项缺席而非 0
      return `<tr class="tl-budget-term" data-term="${k}"><td>${label}</td><td class="text-secondary">缺席（不可测，不是 0）</td></tr>`;
    const dom = b.dominant === k;
    return `<tr class="tl-budget-term" data-term="${k}"><td>${label}${dom ? ' <span class="badge text-bg-danger">dominant</span>' : ''}</td>
      <td>${terms[k]}</td></tr>`;
  }).join('');
  const gainRows = Object.entries(perBand).map(([band, g]) => {
    // F-36：gain_source 不是 unfold fit 的标灰 —— 那个 −2.5 是定义不是测量
    const grey = !g.gain_from_unfold;
    return `<tr class="${grey ? 'text-secondary' : ''}" ${grey ? 'style="opacity:0.65" title="delta-band 极限：-2.5 是定义不是测量（F-36）"' : ''}>
      <td><code>${esc(band)}</code></td><td>${g.gain_mag_per_dex != null ? g.gain_mag_per_dex : '—'}</td>
      <td>${esc(g.gain_source || '')}</td></tr>`;
  }).join('');
  const sedRows = Object.entries(allBands).map(([band, v]) =>
    `<tr><td><code>${esc(band)}</code></td><td>${v}</td></tr>`).join('');
  const desc = dist.describe || {};
  return `
    <div class="small text-secondary mb-1">${esc((d.notes || []).join('；'))}</div>
    <div class="form-check form-check-inline mb-1">
      <input class="form-check-input" type="checkbox" id="tlBudgetDistOnly">
      <label class="form-check-label small" for="tlBudgetDistOnly">只看 distance 项（F-35）</label>
    </div>
    <table class="table table-sm small mb-1" style="max-width:560px" id="tlBudgetTerms">
      <thead><tr><th>误差项（max mag，平方和合成 total=${b.total_max_mag}）</th><th></th></tr></thead>
      <tbody>${termRows}</tbody>
    </table>
    <div class="${dist.is_dominant ? 'alert alert-danger' : 'alert alert-light border'} py-1 small" style="max-width:720px">
      distance 项：<b>${dist.mag != null ? dist.mag : '—'} mag</b>${dist.is_dominant ? ' ⚠ dominant —— 比其它项都大（F-35）' : ''}
      <br><span class="text-secondary">kind=${esc(desc.kind || '?')}，z=${desc.z != null ? desc.z : '—'}，z_err=${desc.z_err != null ? desc.z_err : '不传播'}，
      d_L=${desc.d_L_Mpc != null ? Number(desc.d_L_Mpc).toFixed(1) : '—'} Mpc${desc.cosmology ? `（${esc(desc.cosmology)}）` : ''}</span>
    </div>
    ${gainRows ? `<div class="fw-bold small">逐带实测色标增益（F-36；灰行 = 非 unfold fit，−2.5 是定义不是测量）</div>
      <table class="table table-sm small mb-1" style="max-width:560px">
        <thead><tr><th>波段</th><th>gain (mag/dex)</th><th>gain_source</th></tr></thead><tbody>${gainRows}</tbody></table>` : ''}
    ${sedRows ? `<details open class="mb-1"><summary class="small fw-bold">sed_residual 逐带表（F-37；本带 ${sed.band_value_mag ?? '—'}，${esc(sed.source || '')}）</summary>
      <table class="table table-sm small" style="max-width:400px">
        <thead><tr><th>波段</th><th>residual (mag)</th></tr></thead><tbody>${sedRows}</tbody></table></details>` : ''}
    ${(b.warnings || []).length ? `<div class="fw-bold small">引擎 warnings（F-39 原样透传）</div>
      <ul class="small text-warning-emphasis mb-1">${b.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    <div class="small text-secondary">n_draws=${b.n_draws}，seed=${b.seed}（固定可复现）；峰值处 ${b.at_peak ? `${b.at_peak.mag_AB} ± ${b.at_peak.mag_AB_err} AB` : '—'}</div>`;
}

async function _doBudget() {
  if (!_detailId) return;
  const out = document.getElementById('tlBudgetOut');
  const band = document.getElementById('tlBudgetBand').value;
  const zRaw = document.getElementById('tlBudgetZ').value.trim();
  const ndRaw = document.getElementById('tlBudgetDraws').value.trim();
  const payload = { template_id: _detailId, band };
  if (zRaw) payload.z = Number(zRaw);
  if (ndRaw) payload.n_draws = Number(ndRaw);
  out.innerHTML = '<span class="text-secondary small">预算计算中（默认 32 次 MC，实测 2.4–3.1 s）…</span>';
  try {
    const res = await budgetTmplib(payload);
    out.innerHTML = _budgetHTML(res);
  } catch (e) {
    out.innerHTML = `<div class="alert alert-danger py-1 small">${esc(e.code ? `[${e.code}] ` : '')}${esc(e.message)}</div>`;
  }
}

function _inDomainHTML(idom) {
  if (!idom || !idom.total) return '<div class="text-secondary">尚无实测域内率</div>';
  const pct = (100.0 * idom.answered / idom.total).toFixed(1);
  const rows = Object.entries(idom.by_band || {}).map(([b, v]) =>
    `<tr><td>${esc(b)}</td><td>${v.n}</td><td>${v.ok}</td><td>${esc(v.reason || '')}</td></tr>`).join('');
  return `<div>${esc(TXT.inDomain(idom.answered, idom.total, pct))}</div>
    <table class="table table-sm small mt-1 mb-0" style="max-width:520px">
      <thead><tr><th>波段</th><th>n</th><th>答上</th><th>主因</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function _qcHTML(qc) {
  if (!qc) return '<div class="text-secondary">无 QC sidecar</div>';
  // 波段准入分区：引擎 QC 的实测键是 excluded_bands + excluded_reasons
  // （逐带剔除原因）；audit.rejections 是逐行账目。
  const reasons = qc.excluded_reasons || {};
  const excluded = qc.excluded_bands || [];
  const admRows = excluded.map(b =>
    `<tr><td>${esc(b)}</td><td><span class="text-danger">剔除</span></td>
     <td class="text-secondary">${esc(JSON.stringify(reasons[b] || ''))}</td></tr>`).join('');
  const admitted = qc.bands || [];
  const rest = Object.assign({}, qc);
  delete rest.band_admission;
  return `
    <div class="fw-bold mt-1">波段准入（入面 ${admitted.length} 带；剔除 ${excluded.length} 带）</div>
    <div class="small">入面：${esc(admitted.join(' ') || '—')}</div>
    ${admRows ? `<table class="table table-sm small" style="max-width:640px">
        <thead><tr><th>剔除波段</th><th></th><th>原因</th></tr></thead><tbody>${admRows}</tbody></table>`
      : '<div class="small text-secondary">无被剔除波段</div>'}
    <details class="mt-2"><summary>QC 全文（原文照排，含 audit 逐行账）</summary>
      <pre class="small bg-body-tertiary p-2" style="max-height:320px;overflow:auto">${esc(JSON.stringify(rest, null, 1))}</pre>
    </details>`;
}

function _detailHTML(d) {
  const m = d.manifest || {};
  const mu = d.mu;
  const muTxt = mu
    ? (mu.ok ? `Δμ = ${mu.delta > 0 ? '+' : ''}${mu.delta} mag（${esc(mu.cause || '')}）`
             : `Δμ 无对照（${esc(mu.message || '库内无对应源')}）`)
    : '';
  return `
    <div class="row">
      <div class="col-md-6">
        <div><span class="badge ${(STATE_BADGE[d.state] || ['text-bg-secondary'])[0]}">${esc(d.state)}</span>
          <span class="text-secondary">${esc(ORIGIN_LABEL[d.origin] || d.origin || '')}</span></div>
        <table class="table table-sm small mt-1">
          <tbody>
            <tr><td>object_class</td><td>${esc(m.object_class || '')}</td></tr>
            <tr><td>z</td><td>${m.z != null ? m.z : '—'}（${esc((m.redshift || {}).source || '')}）</td></tr>
            <tr><td>距离</td><td>${esc((m.distance || {}).kind || '')}</td></tr>
            <tr><td>时间</td><td>列 ${esc((m.time || {}).column || '')} / ${esc((m.time || {}).unit || '')} / ${esc((m.time || {}).frame || '')}；零点 ${esc(JSON.stringify((m.time || {}).epoch_zero || {}))}</td></tr>
            <tr><td>消光</td><td>mw_removed=${(m.reddening || {}).mw_removed}，host=${esc((m.reddening || {}).host_removed || '')}</td></tr>
            ${muTxt ? `<tr><td>Δμ</td><td>${muTxt}</td></tr>` : ''}
          </tbody></table>
        <div class="fw-bold">域内率（F-45 实测）</div>${_inDomainHTML(d.in_domain)}
        ${d.state !== 'never-built' ? _budgetCtlHTML(d) : ''}
      </div>
      <div class="col-md-6">${_qcHTML(d.qc)}</div>
    </div>`;
}

// ─── ③ 向导 ───
function _renderWizard() {
  const body = document.getElementById('tlWizardBody');
  if (!body) return;
  if (!isAuthed()) {
    body.innerHTML = '<div class="text-secondary small">造模板需要登录（API-4/5/7 均要求登录；删除需管理员）。</div>';
    return;
  }
  const steps = ['选源', '行账与波段', '强制声明', '建面与 QC'];
  body.innerHTML = `
    <div class="mb-2">${steps.map((s, i) =>
      `<span class="badge ${i + 1 === _wiz.step ? 'text-bg-primary' : i + 1 < _wiz.step ? 'text-bg-success' : 'text-bg-secondary'}">${i + 1}. ${s}</span>`).join(' ')}
    </div>
    <div id="tlWizStep"></div>`;
  _renderStep();
}

function _renderStep() {
  const el = document.getElementById('tlWizStep');
  if (!el) return;
  if (_wiz.step === 1) el.innerHTML = _step1HTML();
  else if (_wiz.step === 2) el.innerHTML = _step2HTML();
  else if (_wiz.step === 3) { if (!_wiz.fields) _prefillFields(); el.innerHTML = _step3HTML(); _validateStep3(); }
  else el.innerHTML = _step4HTML();
}

// ── 第 1 步：选源（U-12/U-19/CA-12/D-6） ──
function _step1HTML() {
  const p = _wiz.preview;
  let info = '';
  if (p && p.code === 'TL_OK') {
    const tr = p.transient || {};
    const est = p.indomain_estimate || {};
    info = `
      <table class="table table-sm small mt-2" style="max-width:720px"><tbody>
        <tr><td style="width:160px">源</td><td><a href="#/transient/${encodeURIComponent(tr.id)}">${esc(tr.id)}</a>
          ${(tr.aliases || []).length ? `<span class="text-secondary">（别名 ${esc(tr.aliases.join('、'))}）</span>` : ''}</td></tr>
        <tr><td>红移</td><td>${tr.redshift != null ? tr.redshift : '<span class="text-danger">库内为空</span>'}
          ${tr.redshift_type ? `（${esc(tr.redshift_type)}）` : ''}
          ${tr.redshift_ref ? `<span class="text-secondary">出处：${esc(tr.redshift_ref)}</span>` : ''}</td></tr>
        <tr><td>gext_distmod</td><td>${tr.gext_distmod != null ? tr.gext_distmod : '—'}</td></tr>
        <tr><td>t0 / 坐标</td><td>${esc(tr.t0 || '—')}；RA ${tr.ra != null ? tr.ra : '—'}，Dec ${tr.dec != null ? tr.dec : '—'}</td></tr>
        <tr><td>总行数</td><td>${p.ledger.total}</td></tr>
        <tr><td>域内率预估（U-19）</td><td id="tlInDomain">星等行 ${est.magnitude_rows ?? '—'}，落在有曲线波段上的可入行 ${est.on_curve_band_rows ?? '—'}，
          预估可答约 ${est.estimated_pct != null ? est.estimated_pct + '%' : '—'}
          <span class="text-secondary">（${esc(est.note || '')}）</span></td></tr>
        <tr><td>建议模板 id</td><td><code>${esc(p.suggested_id)}</code></td></tr>
      </tbody></table>
      ${p.same_event ? `<div class="alert alert-warning py-1 small">${esc(p.same_event.message)}</div>` : ''}
      ${(p.z_conflicts || []).map(c =>
        `<div class="alert alert-warning py-1 small">z 冲突（D-6）：既有模板 <code>${esc(c.template_id)}</code> z=${c.template_z}，
         库 z=${c.catalog_z}，Δμ=${c.delta_mu != null ? c.delta_mu : '—'}。${esc(c.note)}</div>`).join('')}`;
  }
  return `
    <div class="d-flex gap-2 align-items-center" style="max-width:560px">
      <input class="form-control form-control-sm" id="tlWizardSrc" placeholder="源 id（如 EP250108a）"
             value="${esc(_wiz.src)}">
      <button class="btn btn-sm btn-primary text-nowrap" id="tlPreviewBtn">预检（API-4）</button>
    </div>
    <div id="tlStep1Err" class="small text-danger mt-1"></div>
    ${info}
    <div class="mt-2">
      <button class="btn btn-sm btn-primary" id="tlToStep2" ${p && p.code === 'TL_OK' ? '' : 'disabled'}>下一步：行账与波段 →</button>
    </div>`;
}

async function _doPreview() {
  const src = document.getElementById('tlWizardSrc').value.trim();
  const errEl = document.getElementById('tlStep1Err');
  errEl.textContent = '';
  if (!src) { errEl.textContent = '请输入源 id'; return; }
  _wiz.src = src; _wiz.bandSel = null; _wiz.fields = null; _wiz.result = null;
  try {
    _wiz.preview = await getTmplibPreview(src);
    _wiz.previewKey = '';
  } catch (e) {
    _wiz.preview = null;
    errEl.textContent = e.message;
    _renderStep();
    return;
  }
  _renderStep();
}

// U-13：改勾/改档 ⇒ 带参重算（口径唯一权威在服务端）
async function _refetchPreview() {
  if (!_wiz.src) return;
  const opts = { rowset: _wiz.rowset, null_system_policy: _wiz.policy };
  if (_wiz.bandSel !== null) opts.bands = [..._wiz.bandSel];
  if (_wiz.policy === 'declare') opts.declare = _wiz.declare;
  const key = JSON.stringify(opts);
  if (key === _wiz.previewKey) return;
  if (_wiz.step === 3 && _wiz.fields) _collectFields();   // 重绘前保住表单输入
  try {
    const p = await getTmplibPreview(_wiz.src, opts);
    _wiz.preview = p; _wiz.previewKey = key;
  } catch (e) {
    showToast(`行账重算失败：${e.message}`, 'danger');
    return;
  }
  _renderStep();
}

// ── 第 2 步：行账 + 波段映射（U-13/F-08） ──
function _ledgerHTML(led) {
  if (!led) return '';
  if (led.source_rejected)
    return `<div class="alert alert-danger py-1 small">null_system_policy=drop-source：本源自称有 ${led.null_system_rows} 行缺测光系统 ⇒ 整源作废（F-10）。请改用其它档。</div>`;
  const rj = led.rejected || {};
  const names = {
    nonmag: '非星等行（F-14）', unmapped_band: '波段不在注册表（F-08 严格同名）',
    band_deselected: '未勾选', gext_missing: 'mag_gextcor 空值（不回落，F-11）',
    mag_system_missing: '缺测光系统（F-10）', missing_value: '缺值/缺时刻',
    upperlimit: '上限（写进 CSV 但不进拟合，F-12）', discard: 'discard（同上）',
  };
  const items = Object.entries(names).filter(([k]) => rj[k]).map(([k, label]) =>
    `<tr><td>${label}</td><td>${rj[k]}</td></tr>`).join('');
  return `
    <table class="table table-sm small" style="max-width:640px"><tbody>
      <tr><td style="width:320px">总行数</td><td>${led.total}</td></tr>
      <tr><td>进面行数（kept）</td><td><b>${led.kept}</b></td></tr>
      <tr><td>写进 CSV 行数（含上限/discard）</td><td>${led.csv_rows}</td></tr>
      ${items}
    </tbody></table>`;
}

function _step2HTML() {
  const p = _wiz.preview;
  if (!p || p.code !== 'TL_OK') return '<div class="text-secondary small">先回第 1 步完成预检。</div>';
  if (_wiz.bandSel === null) {
    _wiz.bandSel = new Set((p.bands || []).filter(b => b.in_registry).map(b => b.band));
  }
  const rowsHtml = (p.bands || []).map(b => {
    const sel = _wiz.bandSel.has(b.band);
    const sys = Object.entries(b.systems || {}).filter(([, n]) => n).map(([k, n]) => `${k}:${n}`).join(' ');
    return `<tr>
      <td>${b.in_registry
        ? `<input type="checkbox" class="form-check-input tl-band-chk" data-band="${esc(b.band)}" ${sel ? 'checked' : ''}>`
        : '<span class="text-secondary" title="不在滤光片注册表，进不了面">✕</span>'}</td>
      <td><code>${esc(b.band)}</code>${b.trust ? ` <span class="text-secondary">${esc(b.trust)}</span>` : ''}</td>
      <td>${b.rows}</td><td>${esc(sys)}</td>
      <td>${b.upperlimit || 0}</td><td>${b.discard || 0}</td>
      <td>${b.gext_with_value || 0} / <span class="${b.gext_missing ? 'text-danger' : ''}">${b.gext_missing || 0}</span></td>
    </tr>`;
  }).join('');
  return `
    <div class="row">
      <div class="col-lg-6">
        <div class="fw-bold small">行账（当前口径：rowset=${esc(p.query.rowset)}，policy=${esc(p.query.null_system_policy)}；改勾/改档即重算）</div>
        ${_ledgerHTML(p.ledger)}
        <div class="small text-secondary">gext 档对照（U-16）：可进面 ${p.rowsets.gext.kept} 行；
          本源 mag_gextcor 空值行 ${p.rowsets.gext.gext_missing}（gext 档下这些行被剔除、不回落 raw）。</div>
      </div>
      <div class="col-lg-6" id="tlWizardBandMap">
        <div class="fw-bold small">波段映射（严格同名 F-08：库里叫 <code>R</code> 就不会是 <code>r</code>）</div>
        <div class="table-responsive" style="max-height:340px;overflow:auto">
        <table class="table table-sm small mb-0">
          <thead><tr><th>入面</th><th>波段（库标签）</th><th>行数</th><th>系统分布</th><th>上限</th><th>discard</th><th>gext 有/缺</th></tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table></div>
      </div>
    </div>
    <div class="mt-2 d-flex gap-2">
      <button class="btn btn-sm btn-outline-secondary" id="tlBack1">← 上一步</button>
      <button class="btn btn-sm btn-primary" id="tlToStep3" ${p.ledger.kept > 0 ? '' : 'disabled'}>下一步：强制声明 →</button>
      ${p.ledger.kept > 0 ? '' : '<span class="small text-danger align-self-center">当前口径没有任何可进面的行</span>'}
    </div>`;
}

// ── 第 3 步：强制声明（Q-2/U-14..U-22） ──
function _prefillFields() {
  const p = _wiz.preview || {};
  const tr = p.transient || {};
  _wiz.fields = {
    id: p.suggested_id || '',
    label: '',
    object_class: '',
    z: tr.redshift != null ? String(tr.redshift) : '',
    z_source: tr.redshift_ref ? `库内 redshift_ref：${tr.redshift_ref}` : 'AJST 库 transients.redshift',
    z_err: '', z_kind: '',
    use_position: true,
    dist_kind: 'cosmological', mu: '', d_L: '', allow_low_z: false, dist_source: '',
    require_min_bands: 2, validity_notes: '', citations: '',
  };
}

function _fld(id, label, opts = {}) {
  const f = _wiz.fields;
  const err = (_wiz.errs || {})[id];
  return `<div class="mb-1" style="max-width:${opts.width || 520}px">
    <label class="form-label small mb-0">${label}${opts.required === false ? '' : ' <span class="text-danger">*</span>'}</label>
    <input class="form-control form-control-sm ${err ? 'is-invalid' : ''}" id="${id}"
           value="${esc(f[opts.key] ?? '')}" placeholder="${esc(opts.ph || '')}">
    ${err ? `<div class="invalid-feedback">${esc(err)}</div>` : ''}
    ${opts.note ? `<div class="form-text">${esc(opts.note)}</div>` : ''}
  </div>`;
}

function _step3HTML() {
  const f = _wiz.fields;
  const p = _wiz.preview;
  const tr = (p && p.transient) || {};
  const z = parseFloat(f.z);
  const lowZ = isFinite(z) && z < 0.02;
  const kinds = (_cfg.enums && _cfg.enums.distance_kinds) ||
    ['cosmological', 'measured', 'measured-secondary', 'redshift-velocity-field'];
  const zkinds = (_cfg.enums && _cfg.enums.z_error_kinds) ||
    ['line-precision', 'line-scatter', 'unestablished'];
  const sysKinds = (_cfg.enums && _cfg.enums.known_systems) || ['ab', 'vega'];
  const nullBands = (p.bands || []).filter(b => (b.systems || {}).null > 0 && b.in_registry && _wiz.bandSel.has(b.band));
  const rsG = p.rowsets.gext;

  return `
    <div class="row small">
      <div class="col-lg-6" id="tlWizardDeclare">
        ${_fld('tlId', '模板 id（小写，重复 id 是错误不是覆盖）', { key: 'id' })}
        ${_fld('tlLabel', '显示名（可空）', { key: 'label', required: false })}
        ${_fld('tlClass', 'object_class（如 SN Ia / kilonova / GRB afterglow）', { key: 'object_class' })}
        ${_fld('tlZ', 'redshift.value（默认库内值；改它请确认出处）', { key: 'z' })}
        ${_fld('tlZSrc', 'redshift.source（必填，永不代填）', { key: 'z_source' })}
        ${_fld('tlZErr', 'redshift.error（可空；AJST 无此列，留空才是如实）', { key: 'z_err', required: false })}
        <div class="mb-2" id="tlErrKind" style="display:${f.z_err ? '' : 'none'};max-width:640px">
          <label class="form-label small mb-0">error_kind（填了 error 必选）<span class="text-danger">*</span></label>
          <select class="form-select form-select-sm" id="tlZKind">
            <option value="">— 选择 —</option>
            ${zkinds.map(k => `<option value="${esc(k)}" ${f.z_kind === k ? 'selected' : ''}>${esc(k)}</option>`).join('')}
          </select>
          <div class="form-text">${esc(TXT.errKind)}</div>
        </div>
        <div class="form-check mb-2" id="tlPosition">
          <input class="form-check-input" type="checkbox" id="tlUsePos" ${f.use_position ? 'checked' : ''}
                 ${tr.ra == null || tr.dec == null ? 'disabled' : ''}>
          <label class="form-check-label" for="tlUsePos">把库内 RA/Dec 写进 manifest（${tr.ra != null ? `${tr.ra}, ${tr.dec}` : '本源无坐标'}）</label>
          <div class="form-text">${esc(TXT.position)}</div>
        </div>
      </div>
      <div class="col-lg-6">
        <div class="mb-1" style="max-width:520px" id="tlDistanceKind">
          <label class="form-label small mb-0">distance.kind（四选一；库 gext_distmod 是 Planck18 缓存，标 measured 是造假）<span class="text-danger">*</span></label>
          <select class="form-select form-select-sm" id="tlDistKind">
            ${kinds.map(k => `<option value="${esc(k)}" ${f.dist_kind === k ? 'selected' : ''}>${esc(k)}</option>`).join('')}
          </select>
        </div>
        <div id="tlDistVals" style="display:${f.dist_kind === 'cosmological' ? 'none' : ''}">
          ${_fld('tlMu', '距离模数 μ（mag）', { key: 'mu', required: false })}
          ${_fld('tlDL', '或光度距离 d_L（Mpc）', { key: 'd_L', required: false })}
          ${_fld('tlDistSrc', 'distance.source（实测距离的出处）', { key: 'dist_source', required: false })}
        </div>
        <div class="form-check mb-2" id="tlLowZ" style="display:${lowZ && f.dist_kind === 'cosmological' ? '' : 'none'}">
          <input class="form-check-input" type="checkbox" id="tlAllowLowZ" ${f.allow_low_z ? 'checked' : ''}>
          <label class="form-check-label" for="tlAllowLowZ">我知道这是近似（allow_low_z）</label>
          <div class="form-text text-warning">${esc(TXT.lowZ(isFinite(z) ? z : '?'))}</div>
        </div>
        <div class="mb-1" style="max-width:520px" id="tlRedRows">
          <label class="form-label small mb-0">行集 rowset（与 mw_removed 绑死，F-11）<span class="text-danger">*</span></label>
          <select class="form-select form-select-sm" id="tlRowset">
            <option value="raw" ${_wiz.rowset === 'raw' ? 'selected' : ''}>raw — 未扣银消（本源可进面 ${p.ledger.kept} 行）</option>
            <option value="gext" ${_wiz.rowset === 'gext' ? 'selected' : ''}>gext — 已扣银消（可进面 ${rsG.kept} 行；mag_gextcor 空值 ${rsG.gext_missing} 行被剔除不回落）</option>
          </select>
        </div>
        <div class="mb-1" style="max-width:520px" id="tlSysNullPolicy">
          <label class="form-label small mb-0">缺测光系统行的处置（F-10 零猜测）<span class="text-danger">*</span></label>
          <select class="form-select form-select-sm" id="tlPolicy">
            ${['drop', 'declare', 'drop-band', 'drop-source'].map(k =>
              `<option value="${k}" ${_wiz.policy === k ? 'selected' : ''}>${k}</option>`).join('')}
          </select>
          <div class="form-text">${esc(TXT.nullPolicy)}</div>
        </div>
        <div class="mb-2" id="tlDeclareRows" style="display:${_wiz.policy === 'declare' ? '' : 'none'}">
          ${nullBands.length ? nullBands.map(b => `
            <div class="d-flex gap-2 align-items-center mb-1" style="max-width:420px">
              <code style="width:80px">${esc(b.band)}</code>
              <span class="text-secondary">${b.systems.null} 行缺系统 ⇒ 声明为</span>
              <select class="form-select form-select-sm tl-declare-sel" data-band="${esc(b.band)}" style="width:auto">
                <option value="">—</option>
                ${sysKinds.map(s => `<option value="${esc(s)}" ${_wiz.declare[b.band] === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}
              </select>
            </div>`).join('')
          : '<div class="text-secondary">当前勾选的波段没有缺系统的行，declare 无可声明项。</div>'}
        </div>
        ${_fld('tlRmb', 'require_min_bands（≥2；>2 必须写理由）', { key: 'require_min_bands' })}
        ${_fld('tlNotes', 'validity_notes（require_min_bands>2 时必填理由）', { key: 'validity_notes', required: false })}
        ${_fld('tlCite', 'citations（可空，追加进 provenance）', { key: 'citations', required: false })}
      </div>
    </div>
    <div class="mt-2 d-flex gap-2 align-items-center">
      <button class="btn btn-sm btn-outline-secondary" id="tlBack2">← 上一步</button>
      <button class="btn btn-sm btn-primary" id="tlToStep4" disabled>下一步：建面与 QC →</button>
      <span class="small text-danger" id="tlStep3Msg"></span>
    </div>`;
}

function _collectFields() {
  const g = (id) => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
  const f = _wiz.fields;
  f.id = g('tlId'); f.label = g('tlLabel'); f.object_class = g('tlClass');
  f.z = g('tlZ'); f.z_source = g('tlZSrc'); f.z_err = g('tlZErr');
  f.z_kind = g('tlZKind');
  const posEl = document.getElementById('tlUsePos');
  if (posEl) f.use_position = posEl.checked;
  f.dist_kind = g('tlDistKind') || f.dist_kind;
  f.mu = g('tlMu'); f.d_L = g('tlDL'); f.dist_source = g('tlDistSrc');
  const alz = document.getElementById('tlAllowLowZ');
  if (alz) f.allow_low_z = alz.checked;
  f.require_min_bands = parseInt(g('tlRmb'), 10);
  f.validity_notes = g('tlNotes'); f.citations = g('tlCite');
}

// U-14：任一空 ⇒ 下一步禁用 + 逐字段红字（与服务端 Q-2 同口径的前置版）
function _validateStep3() {
  _collectFields();
  const f = _wiz.fields;
  const errs = {};
  if (!/^[a-z0-9][a-z0-9._-]{0,47}$/.test(f.id)) errs.tlId = 'id 必须匹配 ^[a-z0-9][a-z0-9._-]{0,47}$';
  if (!f.object_class) errs.tlClass = 'object_class 必填';
  const z = parseFloat(f.z);
  if (!isFinite(z) || z < 0) errs.tlZ = 'redshift.value 必须是 ≥0 的数值';
  if (!f.z_source) errs.tlZSrc = 'redshift.source 必填（无可信默认，猜了就是谎报）';
  if (f.z_err) {
    const ze = parseFloat(f.z_err);
    if (!isFinite(ze) || ze <= 0) errs.tlZErr = 'error 必须 > 0';
    else if (!f.z_kind) errs.tlZErr = '填了 error 必须选 error_kind（Q-12）';
  }
  if (f.dist_kind !== 'cosmological' && !f.mu && !f.d_L) {
    errs.tlMu = '非 cosmological 必须填 μ 或 d_L（U-22）';
  }
  if (f.dist_kind === 'cosmological' && isFinite(z) && z < 0.02 && !f.allow_low_z) {
    errs.tlZ = 'z<0.02 选 cosmological 必须显式勾选 allow_low_z（TXT-13）';
  }
  if (!(f.require_min_bands >= 2)) errs.tlRmb = 'require_min_bands 必须显式给出且 ≥2（F-47）';
  if (f.require_min_bands > 2 && !f.validity_notes) errs.tlNotes = 'require_min_bands>2 必须写理由（F-47）';
  _wiz.errs = errs;
  const btn = document.getElementById('tlToStep4');
  const msg = document.getElementById('tlStep3Msg');
  if (btn) btn.disabled = Object.keys(errs).length > 0;
  if (msg) msg.textContent = Object.keys(errs).length ? `还有 ${Object.keys(errs).length} 项未通过` : '';
  // 逐字段红字（只加 class，不重绘整步以免打断输入）
  for (const [id, m] of Object.entries(errs)) {
    const el = document.getElementById(id);
    if (el) { el.classList.add('is-invalid'); el.title = m; }
  }
  for (const id of ['tlId', 'tlClass', 'tlZ', 'tlZSrc', 'tlZErr', 'tlMu', 'tlRmb', 'tlNotes']) {
    if (!errs[id]) { const el = document.getElementById(id); if (el) { el.classList.remove('is-invalid'); el.title = ''; } }
  }
}

// ── 第 4 步：建面与 QC（U-17/U-18） ──
function _step4HTML() {
  const f = _wiz.fields;
  const p = _wiz.preview;
  const summary = `
    <table class="table table-sm small" style="max-width:640px"><tbody>
      <tr><td style="width:200px">模板 id</td><td><code>${esc(f.id)}</code></td></tr>
      <tr><td>源 / rowset / policy</td><td>${esc(_wiz.src)} / ${esc(_wiz.rowset)} / ${esc(_wiz.policy)}</td></tr>
      <tr><td>入面波段</td><td>${esc(sortBandsByFreq([...(_wiz.bandSel || [])]).join(' ') || '—')}</td></tr>
      <tr><td>进面行数</td><td>${p.ledger.kept} / ${p.ledger.total}</td></tr>
      <tr><td>z / 距离</td><td>${esc(f.z)} / ${esc(f.dist_kind)}${f.mu ? `（μ=${esc(f.mu)}）` : ''}${f.d_L ? `（d_L=${esc(f.d_L)} Mpc）` : ''}</td></tr>
    </tbody></table>`;
  let out = `
    <div class="small text-warning mb-2">建面是唯一触发点（POS-3）：点下按钮才写盘；引擎忙（429）时原样显示、不自动重试。</div>
    ${summary}
    <div class="d-flex gap-2 align-items-center">
      <button class="btn btn-sm btn-outline-secondary" id="tlBack3" ${_wiz.building ? 'disabled' : ''}>← 上一步</button>
      <button class="btn btn-sm btn-danger" id="tlBuild" ${_wiz.building ? 'disabled' : ''}>
        ${_wiz.building ? '建面中…（1–3 s）' : '建面（POST API-5）'}</button>
    </div>
    <div id="tlBuildErr" class="small mt-2"></div>
    <div id="tlQcPanel" class="mt-2"></div>`;
  return out;
}

async function _doBuild() {
  if (_wiz.building) return;
  _wiz.building = true;
  _renderStep();
  const f = _wiz.fields;
  const payload = {
    id: f.id, transient_id: _wiz.src, label: f.label || undefined,
    object_class: f.object_class,
    redshift: {
      value: parseFloat(f.z), source: f.z_source,
      error: f.z_err ? parseFloat(f.z_err) : undefined,
      error_kind: f.z_err ? f.z_kind : undefined,
      use_position: f.use_position,
    },
    distance: {
      kind: f.dist_kind,
      mu: f.mu ? parseFloat(f.mu) : undefined,
      d_L_Mpc: f.d_L ? parseFloat(f.d_L) : undefined,
      allow_low_z: f.allow_low_z || undefined,
      source: f.dist_source || undefined,
    },
    rowset: _wiz.rowset, null_system_policy: _wiz.policy,
    declarations: Object.entries(_wiz.declare).filter(([, s]) => s)
      .map(([band, system]) => ({ band, system })),
    bands: [...(_wiz.bandSel || [])],
    require_min_bands: f.require_min_bands,
    validity_notes: f.validity_notes || undefined,
    citations: f.citations || undefined,
  };
  const errEl = document.getElementById('tlBuildErr');
  try {
    const res = await createTmplibTemplate(payload);
    _wiz.result = res; _wiz.buildError = null;
    showToast(`模板 ${res.id} 建面完成`, 'success');
  } catch (e) {
    _wiz.result = null; _wiz.buildError = e;
  }
  _wiz.building = false;
  _renderStep();
  const errEl2 = document.getElementById('tlBuildErr');
  if (_wiz.buildError && errEl2) {
    const e = _wiz.buildError;
    let html = `<div class="alert alert-danger py-1 small">${esc(e.message)}</div>`;
    const missing = e.payload && e.payload.missing;
    if (missing && missing.length)
      html += `<div class="small text-danger">缺字段：${esc(missing.join('、'))}</div>`;
    errEl2.innerHTML = html;
  }
  if (_wiz.result) {
    const qcEl = document.getElementById('tlQcPanel');
    if (qcEl) qcEl.innerHTML = _buildResultHTML(_wiz.result);
    _loadLibrary();                       // 清单出现新模板
  }
}

function _buildResultHTML(res) {
  const idom = res.in_domain;
  const idomTxt = idom && idom.total
    ? esc(TXT.inDomain(idom.answered, idom.total, (100.0 * idom.answered / idom.total).toFixed(1)))
    : '';
  const s = res.surface || {};
  return `
    <div class="alert alert-success py-1 small">模板 <code>${esc(res.id)}</code> 已入库（state=${esc(res.state)}）。</div>
    <div class="row small">
      <div class="col-md-6">
        <table class="table table-sm small"><tbody>
          <tr><td>面波段</td><td>${esc((s.bands || []).join(' '))}</td></tr>
          <tr><td>t_valid (d)</td><td>${(s.t_valid_days || []).map(v => v.toFixed(1)).join(' … ')}</td></tr>
          <tr><td>时间节点数</td><td>${s.n_time_nodes ?? '—'}</td></tr>
          <tr><td>行账</td><td>kept ${res.row_ledger.kept} / ${res.row_ledger.total}</td></tr>
        </tbody></table>
        <div class="fw-bold">真实域内率（TXT-20）</div>${idomTxt}${_inDomainHTML(idom)}
      </div>
      <div class="col-md-6">
        <div class="fw-bold">QC 报告（U-18，原文照排）</div>
        ${_qcHTML(res.qc)}
      </div>
    </div>`;
}

// ─── 事件委托 ───
document.addEventListener('click', async (ev) => {
  const t = ev.target;
  if (!(t instanceof HTMLElement)) return;
  if (t.id === 'tlPreviewBtn') { _doPreview(); return; }
  if (t.id === 'tlToStep2') { _wiz.step = 2; _renderStep(); return; }
  if (t.id === 'tlBack1') { _wiz.step = 1; _renderStep(); return; }
  if (t.id === 'tlToStep3') { _wiz.step = 3; _renderStep(); return; }
  if (t.id === 'tlBack2') { _wiz.step = 2; _renderStep(); return; }
  if (t.id === 'tlToStep4') { _wiz.step = 4; _renderStep(); return; }
  if (t.id === 'tlBack3') { _wiz.step = 3; _renderStep(); return; }
  if (t.id === 'tlBuild') { _doBuild(); return; }
  if (t.id === 'tlBudgetRun') { _doBudget(); return; }
  if (t.id === 'tlBatchRebuild') { _doBatchRebuild(); return; }
  if (t.classList.contains('tl-batch-cb')) { ev.stopPropagation(); return; }  // 不触发行点击详情
  const rebuildBtn = t.closest('.tl-rebuild');
  if (rebuildBtn) {
    ev.stopPropagation();
    const id = rebuildBtn.dataset.id;
    rebuildBtn.disabled = true;
    try {
      const res = await rebuildTmplibTemplate(id);
      showToast(`${id} 重建完成（${esc(res.state || 'fresh')}）`, 'success');
    } catch (e) {
      showToast(`重建被拒：${e.message}`, 'danger');
    }
    _loadLibrary();
    return;
  }
  const delBtn = t.closest('.tl-delete');
  if (delBtn) {
    ev.stopPropagation();
    const id = delBtn.dataset.id;
    const hard = confirm(`软删 ${id}？（文件移 .trash，索引保留 deleted 标记）\n\n「确定」= 软删；「取消」后还想硬删请再次点击并选硬删。`);
    if (!hard) {
      if (!confirm(`要硬删 ${id} 吗？文件与索引条目一并删除，不可恢复。`)) return;
      const password = prompt('硬删需要当前管理员密码二次校验：');
      if (!password) return;
      try {
        await deleteTmplibTemplate(id, { hard: true, password });
        showToast(`${id} 已硬删`, 'success');
      } catch (e) {
        showToast(`硬删被拒：${e.message}`, 'danger');
      }
    } else {
      try {
        await deleteTmplibTemplate(id);
        showToast(`${id} 已软删（文件在 .trash）`, 'success');
      } catch (e) {
        showToast(`软删被拒：${e.message}`, 'danger');
      }
    }
    _loadLibrary();
    return;
  }
  const row = t.closest('tr.tl-row');
  if (row && row.dataset.id) { _loadDetail(row.dataset.id); return; }
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!(t instanceof HTMLElement)) return;
  if (t.classList.contains('tl-batch-cb')) { _refreshBatchButton(); return; }
  if (t.id === 'tlBudgetDistOnly') {          // F-35：只看 distance 项
    document.querySelectorAll('.tl-budget-term').forEach(row => {
      row.style.display = (t.checked && row.dataset.term !== 'distance') ? 'none' : '';
    });
    return;
  }
  if (t.classList.contains('tl-band-chk')) {          // U-13：改勾重算账本
    if (t.checked) _wiz.bandSel.add(t.dataset.band);
    else _wiz.bandSel.delete(t.dataset.band);
    _refetchPreview();
    return;
  }
  if (t.classList.contains('tl-declare-sel')) {
    if (t.value) _wiz.declare[t.dataset.band] = t.value;
    else delete _wiz.declare[t.dataset.band];
    _refetchPreview();
    return;
  }
  if (t.id === 'tlRowset') { _wiz.rowset = t.value; _refetchPreview(); return; }
  if (t.id === 'tlPolicy') { _wiz.policy = t.value; _refetchPreview(); return; }
  if (_wiz.step === 3 && _wiz.fields) {
    if (t.id === 'tlDistKind') {
      _wiz.fields.dist_kind = t.value;
      const dv = document.getElementById('tlDistVals');
      if (dv) dv.style.display = t.value === 'cosmological' ? 'none' : '';
      const z = parseFloat(_wiz.fields.z);
      const lz = document.getElementById('tlLowZ');
      if (lz) lz.style.display = (isFinite(z) && z < 0.02 && t.value === 'cosmological') ? '' : 'none';
    }
    _validateStep3();
  }
});

document.addEventListener('input', (ev) => {
  const t = ev.target;
  if (!(t instanceof HTMLElement) || _wiz.step !== 3 || !_wiz.fields) return;
  if (t.id === 'tlWizardSrc') return;
  _validateStep3();
  // 联动显隐（不改值，只切显示）
  if (t.id === 'tlZErr') {
    const k = document.getElementById('tlErrKind');
    if (k) k.style.display = t.value.trim() ? '' : 'none';
  }
  if (t.id === 'tlZ') {
    const z = parseFloat(t.value);
    const lz = document.getElementById('tlLowZ');
    if (lz) lz.style.display = (isFinite(z) && z < 0.02 && _wiz.fields.dist_kind === 'cosmological') ? '' : 'none';
  }
});
