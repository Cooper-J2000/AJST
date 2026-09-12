// === 外部目录参数渲染（详情页概览标签：T90/Epeak/fluence/Eiso 等，逐目录含来源） ===
import { esc, escAttr, safeUrl } from '../utils.js';

const CAT_ORDER = ['fermi_gbm', 'fermi_lat', 'swift_bat', 'swift_grb', 'uvot_grb',
                   'batse', 'heasarc_grbcat', 'agile_mcal', 'mpe_greiner'];

function _sci(v) {
  if (v == null) return '?';
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.01 || a >= 10000)) return v.toExponential(2);
  return +v.toFixed(4);
}

function _fmtErr(err, cl) {
  if (err == null) return '';
  let s;
  if (Array.isArray(err)) s = `<sub>−${_sci(Math.abs(err[1]))}</sub><sup>+${_sci(Math.abs(err[0]))}</sup>`;
  else s = `±${_sci(err)}`;
  return s + (cl === 90 ? ' <small>(90%CL)</small>' : '');
}

function _fmtParam(label, p, unit) {
  if (!p || p.v == null) return '';
  const band = p.band ? ` <small class="text-secondary">[${esc(p.band)}]</small>` : '';
  const model = p.model ? ` <small class="text-secondary">${esc(p.model)}</small>` : '';
  const frame = p.frame === 'rest' ? ' <small class="text-secondary">静止系</small>' : '';
  const dt = p.dt ? ` <small class="text-secondary">${esc(p.dt)}</small>` : '';
  const mod = p.mod ? esc(p.mod) : '';
  return `<span class="me-3 d-inline-block" title="${escAttr(label)}">${label} = ${mod}${_sci(p.v)}${_fmtErr(p.err, p.cl)}${unit}${band}${model}${frame}${dt}</span>`;
}

export function renderCatalogData(extraData) {
  const cd = extraData && extraData.catalog_data;
  if (!cd || Object.keys(cd).length === 0) return '';
  const cats = CAT_ORDER.filter(c => cd[c]).concat(Object.keys(cd).filter(c => !CAT_ORDER.includes(c)));
  const sections = cats.map(cat => {
    const e = cd[cat];
    const p = e.params || {};
    const src = e.source || {};
    const parts = [];
    parts.push(_fmtParam('T90', p.t90, ' s'));
    parts.push(_fmtParam('T50', p.t50, ' s'));
    parts.push(_fmtParam('Epeak', p.epeak, ' keV'));
    parts.push(_fmtParam('Fluence', p.fluence, ' erg/cm²'));
    for (const k of Object.keys(p)) {
      if (k.startsWith('fluence_')) parts.push(_fmtParam('Fluence', p[k], ' erg/cm²'));
      if (k.startsWith('peak_flux')) parts.push(_fmtParam('峰流量', p[k], ' ph/cm²/s'));
    }
    parts.push(_fmtParam('Eiso', p.eiso, ' erg'));
    parts.push(_fmtParam('Eiso(1-1000)', p.eiso_1000, ' erg'));
    parts.push(_fmtParam('α', p.alpha, ''));
    parts.push(_fmtParam('β', p.beta, ''));
    parts.push(_fmtParam('光子指数', p.spectral_index, ''));
    parts.push(_fmtParam('z', p.redshift, ''));
    const body = parts.filter(Boolean).join('') || '<span class="text-secondary small">（无瞬时辐射参数）</span>';
    return `<div class="mb-2">
      <div class="small fw-bold"><a href="${escAttr(safeUrl(src.url))}" target="_blank" rel="noopener" class="text-decoration-none">${esc(src.name || cat)} <i class="bi bi-box-arrow-up-right" style="font-size:0.7em"></i></a>
      <small class="text-secondary fw-normal"> · 获取于 ${esc(src.retrieved) || '-'}</small></div>
      <div class="small">${body}</div>
    </div>`;
  }).join('');
  return `
  <div class="row mt-3">
    <div class="col-12">
      <div class="card">
        <div class="card-header"><i class="bi bi-journal-bookmark"></i> 外部目录参数 <small class="text-secondary">T90 / Epeak / fluence / Eiso 等，逐目录含数据来源</small></div>
        <div class="card-body">${sections}</div>
      </div>
    </div>
  </div>`;
}
