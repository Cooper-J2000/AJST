// === Transient Detail Page ===
import { app, showLoading, showError } from './layout.js';
import {
  getTransient, getLightcurves, getFilters,
  exportLightcurves, showToast, isAuthed, isAdmin, currentUser, updateTransient, updateLightcurve, createLightcurves,
  deleteLightcurve,
  runExtinction, clearExtinction, fitLightcurveModel, getSpectra, getSpectrum, uploadSpectrum,
  deleteSpectrumApi,
  getArticles, createArticle, updateArticle, deleteArticle,
  getHost
} from '../api.js';
import { initFittingTab, destroyFittingTab } from './fitting_tab.js';
import { initHostfitTab, destroyHostfitTab } from './hostfit_tab.js';
import { showLcUpload } from './lc_upload.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { SPEC_LINE_GROUPS, createSpecLinesPlugin, buildMarkingsPanelHTML } from '../spec_lines.js';
import { chartColors, ACADEMIC_FONT, academicFonts } from '../theme.js';
import { parseRA, parseDec, attachCoordHint } from '../coords.js';
import {
  magABtoMJy, mJyToMagAB, ensureFilterCache,
  getVega2ab, sortBandsByFreq, buildSpectralColors,
} from '../bands.js';

let currentTid = null;
let lcChartInstance = null;
const lcChartHolder = { chart: null };  // 框选缩放用的当前图表引用
let aladinInstance = null;
let filters = [];

// ─── 光变图手动坐标范围（null = 该端自动） ───
let lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
// ─── 数据表排序 ───
let lcItems = [];
let lcSortState = { key: 'time', dir: 1 };
// ─── 基本信息：相关研究文章（当前源的条目列表） ───
let articlesData = [];
// ─── 数据表列显示（lcColVis[key] 显式记录用户选择；未记录的按默认子集），localStorage 持久化 ───
const LC_COLS = [
  ['time', '时间(s)'], ['time_err', '时间误差'], ['band', '波段'], ['flux_density', '流量/星等'],
  ['flux_density_err', '误差'], ['flux_density_unit', '单位'], ['mag_system', '星等系统'],
  ['gext_corr', '银消'], ['upperlimit', '上限'], ['host_subtracted', '扣宿主'], ['gext_Alambda', '银消量'],
  ['mag_gextcor', '银消后AB星等'], ['mag_gextcor_err', '银消后星等误差'],
  ['flux_density_gextcor', '银消后流量'], ['flux_density_gextcor_err', '银消后误差'],
  ['weights', '权重'], ['discard', '丢弃'], ['telescope', '望远镜'], ['instrument', '仪器'],
  ['reference', '引用'], ['comment', '备注'], ['source', '来源'], ['updated_at', '存入/修改时间'],
];
// 默认隐藏的列（紧凑默认子集之外：银消改正结果/权重/丢弃/来源/时间戳，需要时在「列显示」勾出）
const LC_COLS_DEFAULT_HIDDEN = new Set([
  'mag_gextcor', 'mag_gextcor_err', 'flux_density_gextcor', 'flux_density_gextcor_err',
  'weights', 'discard', 'source', 'updated_at',
]);
let lcColVis = (() => {
  try { return JSON.parse(localStorage.getItem('lcColVis') || '{}'); } catch { return {}; }
})();
const lcColShown = (k) => (k in lcColVis) ? lcColVis[k] !== false : !LC_COLS_DEFAULT_HIDDEN.has(k);
// ─── 数据表行标记（勾选高亮定位用，仅前端状态，不写库） ───
let lcMarked = new Set();
let lcMarkedTid = null;
// ─── 叠加在光变图上的拟合曲线 ───
let lcFits = [];
const FIT_COLORS = chartColors().fits;   // 拟合线配色（随主题；切换主题后 reload 生效）
const FIT_MODEL_NAMES = { pl: 'powerlaw', bpl: 'broken-powerlaw', sbpl: 'smoothly-broken-powerlaw' };
// ─── 当前源的红移/T0/距离模数（光变图静止系、顶部 MJD 轴、绝对星等用） ───
let _lcRedshift = null;
let _lcT0MJD = null;   // T0 对应的 MJD（无 T0 时为 null）
let _lcDistmod = null;
// ─── 光变图波段可见性（勾选框面板控制，默认全显示） ───
let lcBandVisible = {};  // band → bool
// ─── 光变图顶部副轴配置（null = 不画） ───
let _lcTopAxis = null;   // { mode: 'day'|'mjd' }

// T0 (naive UTC ISO 字符串) → MJD
function t0ToMJD(t0) {
  if (!t0) return null;
  const s = String(t0);
  const iso = s.includes('T') ? s : s + 'T00:00:00';
  const ms = Date.parse(iso.endsWith('Z') ? iso : iso + 'Z');
  if (!isFinite(ms)) return null;
  return ms / 86400000 + 40587;  // Unix epoch = MJD 40587
}
// ─── derived 卡片编辑状态 ───
let derivedDraft = null;     // 工作副本
let derivedPristine = null;  // 服务器载入的原始副本（取消编辑时恢复）
let derivedEditMode = false;

// ─── AB magnitude ↔ mJy 换算与波段工具已抽至 js/bands.js（magABtoMJy 等直接 import） ───

// ─── 流量单位统一转 mJy ───
function toMJy(value, unit) {
  if (value == null) return null;
  switch (unit) {
    case 'mJy': return value;
    case 'uJy': return value * 1e-3;
    case 'Jy':  return value * 1e3;
    case 'cgs': case 'erg/cm2/s/Hz': case 'cgs(erg/cm2/s/Hz)': return value * 1e26;
    // 每 keV 流量密度 → 每 Hz：F_ν = F_E·h（h=4.1357e-18 keV/Hz），再 ×1e26 到 mJy
    case 'erg/cm2/s/keV': return value * 4.1357e-18 * 1e26;
    default: return value; // 未知单位原值返回
  }
}

// ─── 单点换算到 mJy 空间（绘图与拟合共用；useGext 时优先银消改正值） ───
// 星等数据的误差同步换算: σ_F = F·ln10·σ_m/2.5
function pointToMJy(p, useGext) {
  const unit = p.flux_density_unit;
  let y = null, err = null;
  if (useGext && p.gext_corr) {
    if (p.flux_density_gextcor != null) {
      y = p.flux_density_gextcor;
      err = p.flux_density_gextcor_err != null ? p.flux_density_gextcor_err : null;
    } else if (p.mag_gextcor != null) {
      y = magABtoMJy(p.mag_gextcor);
      err = p.mag_gextcor_err != null ? (Math.LN10 / 2.5) * y * p.mag_gextcor_err : null;
    }
  }
  if (y == null) {
    if (unit === 'mag' || unit === 'magnitude') {
      // Vega 系统先转 AB（绘图统一用 AB 星等），再按 AB 零点换算
      let mag = p.flux_density;
      if ((p.mag_system || '').trim().toLowerCase() === 'vega') mag += getVega2ab(p.band);
      y = magABtoMJy(mag);
      err = p.flux_density_err != null ? (Math.LN10 / 2.5) * y * p.flux_density_err : null;
    } else {
      y = toMJy(p.flux_density, unit);
      err = p.flux_density_err != null ? toMJy(p.flux_density_err, unit) : null;
    }
  }
  if (y == null || !isFinite(y)) return null;
  if (y <= 0) y = 1e-13; // log 轴最小正数约束（不能盖住 X 射线晚期弱流量）
  return { y, err };
}

// ─── 波段名归一化 / 滤波器缓存 / 光谱色阶：已抽至 js/bands.js（详情页与其余图表共用） ───

// ─── 科学计数法格式化 ───
function sciFormat(v) {
  if (v === 0 || v == null) return '0';
  const abs = Math.abs(v);
  if (abs >= 0.1 && abs < 10000) {
    return v.toFixed(2);
  }
  return v.toExponential(1);
}

// ─── 2-3 位有效数字（拟合参数标注用） ───
function sig3(v) {
  if (v == null || !isFinite(v)) return '?';
  if (v === 0) return '0';
  return String(parseFloat(Number(v).toPrecision(3)));
}

// ─── HTML 属性转义（title 属性等） ───
function escAttr(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// ─── 数据表：单行 HTML（排序重绘与首次渲染共用） ───
// 行内编辑权限：管理员可编辑任意记录；普通用户仅可编辑自己录入的记录（source = 本账户），
// 其余记录只能扣点（与后端 PUT /api/lightcurves/<id> 权限一致）
function canEditLc(pt) {
  if (isAdmin()) return true;
  const u = currentUser();
  return isAuthed() && pt.source && u.username === pt.source;
}

// ─── 基本信息：相关研究文章条目（简称 + 标题 + 链接 + BibTeX，可多条） ───
// 条目以整数 id 定位（同一作者同年可有多篇，简称不唯一）
// 权限：登录用户可添加；修改/删除仅管理员（与后端 /api/articles 一致）
function articlesHTML() {
  const items = articlesData.map(a => `
    <div class="mb-1" id="articleItem_${a.id}">
      <div class="d-flex align-items-center gap-2 flex-wrap">
        <a href="${escAttr(a.url)}" target="_blank" rel="noopener noreferrer" style="overflow-wrap:anywhere"><i class="bi bi-journal-text"></i> ${escAttr(a.name)}</a>
        ${a.bibtex ? `<button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="articleBibtexCopy(${a.id})" title="复制 BibTeX 引用信息到剪贴板"><i class="bi bi-clipboard"></i> BibTeX</button>` : ''}
        ${a.source ? `<span class="text-secondary" style="font-size:0.72rem">${escAttr(a.source)}</span>` : ''}
        ${isAdmin() ? `
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="articleEditStart(${a.id})" title="编辑"><i class="bi bi-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="articleDelete(${a.id})" title="删除"><i class="bi bi-trash"></i></button>` : ''}
      </div>
      ${a.title ? `<div class="text-secondary text-truncate" style="font-size:0.78rem;max-width:100%" title="${escAttr(a.title)}">${escAttr(a.title)}</div>` : ''}
    </div>`).join('');
  const addArea = isAuthed() ? `
    <button class="btn btn-sm btn-outline-primary py-0 px-2 mt-1" id="articleAddBtn" onclick="articleAddToggle()"><i class="bi bi-plus-lg"></i> 添加</button>
    <div id="articleAddForm" style="display:none" class="mt-1">
      <input type="text" class="form-control form-control-sm mb-1" id="articleAddName" placeholder="简称（建议：第一作者+年份，如 Dainotti+2024）">
      <input type="text" class="form-control form-control-sm mb-1" id="articleAddTitle" placeholder="文章标题（可选）">
      <input type="text" class="form-control form-control-sm mb-1" id="articleAddUrl" placeholder="文章链接 https://...">
      <textarea class="form-control form-control-sm mb-1" id="articleAddBibtex" rows="3" placeholder="BibTeX 引用信息（可选），如 @article{...}"></textarea>
      <div class="d-flex gap-1">
        <button class="btn btn-sm btn-primary py-0 px-2" onclick="articleAddSave()">保存</button>
        <button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="articleAddToggle()">取消</button>
      </div>
    </div>` : (articlesData.length ? '' : '<span class="text-secondary">登录后可添加</span>');
  return (items || '<div class="text-secondary mb-1">暂无</div>') + addArea;
}

function lcRowHTML(pt) {
  const marked = lcMarked.has(pt.id);
  return `
    <tr id="lcRow_${pt.id}"${marked ? ' class="lc-row-mark"' : ''}>
      <td class="lc-mark-cell"><input type="checkbox" class="lc-mark-chk" data-id="${pt.id}" ${marked ? 'checked ' : ''}onchange="lcMarkRow(${pt.id}, this.checked)" title="标记该行（整行高亮，便于对比定位）"></td>
      <td class="lc-del-cell" style="display:none"><input type="checkbox" class="lc-del-chk" data-id="${pt.id}"></td>
      <td class="lc-val" data-field="time" data-col="time">${pt.time.toFixed(1)}</td>
      <td class="lc-val" data-field="time_err" data-col="time_err">${pt.time_err != null ? pt.time_err.toFixed(1) : '-'}</td>
      <td class="lc-val" data-field="band" data-col="band">${pt.band}</td>
      <td class="lc-val" data-field="flux_density" data-col="flux_density">${pt.flux_density.toFixed(3)}</td>
      <td class="lc-val" data-field="flux_density_err" data-col="flux_density_err">${pt.flux_density_err != null ? pt.flux_density_err.toFixed(3) : '-'}</td>
      <td class="lc-val" data-field="flux_density_unit" data-col="flux_density_unit">${pt.flux_density_unit}</td>
      <td class="lc-val" data-field="mag_system" data-col="mag_system">${pt.mag_system || '-'}</td>
      <td class="lc-val" data-field="gext_corr" data-col="gext_corr">${pt.gext_corr ? 'Y' : 'N'}</td>
      <td class="lc-val" data-field="upperlimit" data-col="upperlimit">${pt.upperlimit ? 'Y' : '-'}</td>
      <td class="lc-val" data-field="host_subtracted" data-col="host_subtracted" title="是否已扣除宿主星系流量">${pt.host_subtracted == null ? '-' : (pt.host_subtracted ? 'Y' : 'N')}</td>
      <td class="lc-val" data-field="gext_Alambda" data-col="gext_Alambda">${pt.gext_Alambda != null ? pt.gext_Alambda.toFixed(4) : '-'}</td>
      <td class="lc-val lc-computed" data-field="mag_gextcor" data-col="mag_gextcor">${pt.mag_gextcor != null ? pt.mag_gextcor.toFixed(3) : '-'}</td>
      <td class="lc-val lc-computed" data-field="mag_gextcor_err" data-col="mag_gextcor_err">${pt.mag_gextcor_err != null ? pt.mag_gextcor_err.toFixed(3) : '-'}</td>
      <td class="lc-val" data-field="flux_density_gextcor" data-col="flux_density_gextcor">${pt.flux_density_gextcor != null ? pt.flux_density_gextcor.toFixed(4) : '-'}</td>
      <td class="lc-val" data-field="flux_density_gextcor_err" data-col="flux_density_gextcor_err">${pt.flux_density_gextcor_err != null ? pt.flux_density_gextcor_err.toFixed(4) : '-'}</td>
      <td class="lc-val" data-field="weights" data-col="weights">${pt.weights != null ? pt.weights.toFixed(2) : '1.00'}</td>
      <td class="lc-val" data-field="discard" data-col="discard">${pt.discard ? 'Y' : 'N'}</td>
      <td class="lc-val" data-field="telescope" data-col="telescope" title="${escAttr(pt.telescope)}">${pt.telescope || '-'}</td>
      <td class="lc-val" data-field="instrument" data-col="instrument" title="${escAttr(pt.instrument)}">${pt.instrument || '-'}</td>
      <td class="small lc-val lc-ref" data-field="reference" data-col="reference" title="${escAttr(pt.reference)}">${pt.reference || '-'}</td>
      <td class="small lc-val lc-comment" data-field="comment" data-col="comment" title="${escAttr(pt.comment)}">${pt.comment || '-'}</td>
      <td class="small" data-col="source" title="数据来源">${pt.source ? escAttr(pt.source) : '-'}</td>
      <td class="small text-secondary text-nowrap" data-col="updated_at" title="存入时间 / 最近修改 (UTC)">${(pt.updated_at || pt.created_at || '').replace('T', ' ').slice(0, 19) || '-'}</td>
      <td class="lc-edit-cell" style="display:none">
        ${canEditLc(pt) ? `
        <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="lcEditStart(${pt.id})" title="编辑"><i class="bi bi-pencil"></i></button>
        ${isAdmin() ? `<button class="btn btn-sm btn-outline-warning py-0 px-1" onclick="lcGextRun(${pt.id})" title="对该数据点执行银消改正"><i class="bi bi-moon-stars"></i></button>` : ''}` : ''}
        ${!isAdmin() ? `
        <button class="btn btn-sm ${pt.discard ? 'btn-outline-success' : 'btn-outline-warning'} py-0 px-1" onclick="lcDiscardToggle(${pt.id}, ${pt.discard ? 'true' : 'false'})" title="${pt.discard ? '恢复该点' : '扣点（标记丢弃）'}"><i class="bi ${pt.discard ? 'bi-arrow-counterclockwise' : 'bi-hand-index'}"></i></button>` : ''}
      </td>
    </tr>`;
}

// ─── 数据表列排序：点击表头切换升/降序，null 排最后 ───
window.lcSort = (key) => {
  if (lcSortState.key === key) lcSortState.dir *= -1;
  else lcSortState = { key, dir: 1 };
  const { dir } = lcSortState;
  const sorted = [...lcItems].sort((a, b) => {
    const va = a[key], vb = b[key];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    if (typeof va === 'boolean' || typeof vb === 'boolean') return (Number(va) - Number(vb)) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });
  const tbody = document.getElementById('lcTableBody');
  if (tbody) tbody.innerHTML = sorted.map(lcRowHTML).join('');
  applyLcColVis();  // 重绘后恢复列隐藏状态
  // 更新排序指示
  document.querySelectorAll('.lc-sort-ind').forEach(el => {
    el.textContent = el.dataset.ind === lcSortState.key ? (dir > 0 ? ' ▲' : ' ▼') : '';
  });
  // 编辑/删除列显示状态与新行保持一致：编辑列（含扣点）登录可见，删除列仅管理员
  if (isAuthed()) {
    document.querySelectorAll('.lc-edit-cell').forEach(el => el.style.display = '');
    if (isAdmin()) {
      document.querySelectorAll('.lc-del-cell').forEach(el => el.style.display = '');
    }
  }
  // 排序重绘后勾选状态清空，同步取消全选
  const delAll = document.getElementById('lcDelAll');
  if (delAll) delAll.checked = false;
};

// ─── 数据表行标记：勾选整行高亮，便于对比定位（仅前端状态，不写库） ───
window.lcMarkRow = (id, on) => {
  if (on) lcMarked.add(id); else lcMarked.delete(id);
  const row = document.getElementById(`lcRow_${id}`);
  if (row) row.classList.toggle('lc-row-mark', on);
};

window.lcMarkToggleAll = (on) => {
  lcMarked = on ? new Set(lcItems.map(p => p.id)) : new Set();
  document.querySelectorAll('#lcTableBody .lc-mark-chk').forEach(chk => { chk.checked = on; });
  document.querySelectorAll('#lcTableBody tr').forEach(tr => tr.classList.toggle('lc-row-mark', on));
};

// ─── 数据表列显示：勾选要显示的列，设置持久化到 localStorage ───
function applyLcColVis() {
  for (const [k] of LC_COLS) {
    const show = lcColShown(k);
    document.querySelectorAll(`#tab-data [data-col="${k}"]`)
      .forEach(el => { el.style.display = show ? '' : 'none'; });
  }
}

function buildLcColPanel() {
  const el = document.getElementById('lcColChecks');
  if (!el) return;
  el.innerHTML = LC_COLS.map(([k, label]) => `
    <div class="form-check form-check-inline mb-0 me-2">
      <input class="form-check-input" type="checkbox" id="lcColChk_${k}"
        ${lcColShown(k) ? 'checked' : ''} onchange="lcColToggle('${k}', this.checked)">
      <label class="form-check-label" for="lcColChk_${k}">${label}</label>
    </div>`).join('') + `
    <button class="btn btn-sm btn-outline-primary py-0" onclick="lcColAll(true)">全选</button>
    <button class="btn btn-sm btn-outline-secondary py-0" onclick="lcColAll(false)">全不选</button>
    <span class="text-secondary">（仅影响页面显示；下载数据表始终为全列完整版）</span>`;
}

window.lcColPanelToggle = () => {
  const p = document.getElementById('lcColPanel');
  if (p) p.style.display = p.style.display === 'none' ? '' : 'none';
};

window.lcColToggle = (key, on) => {
  lcColVis[key] = on;
  try { localStorage.setItem('lcColVis', JSON.stringify(lcColVis)); } catch {}
  applyLcColVis();
};

window.lcColAll = (on) => {
  for (const [k] of LC_COLS) lcColVis[k] = on;
  try { localStorage.setItem('lcColVis', JSON.stringify(lcColVis)); } catch {}
  buildLcColPanel();
  applyLcColVis();
};

// ─── 数据表批量删除 ───
window.lcDelToggleAll = (checked) => {
  document.querySelectorAll('.lc-del-chk').forEach(chk => { chk.checked = checked; });
};

window.lcDeleteSelected = async () => {
  if (!isAdmin()) { showToast('仅管理员可删除数据', 'warning'); return; }
  const ids = [...document.querySelectorAll('.lc-del-chk:checked')].map(chk => parseInt(chk.dataset.id));
  if (!ids.length) { showToast('请先勾选要删除的记录', 'warning'); return; }
  if (!confirm(`确定删除选中的 ${ids.length} 条光变记录？此操作不可恢复`)) return;
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await deleteLightcurve(id); ok++; }
    catch { fail++; }
  }
  if (fail) showToast(`已删除 ${ok} 条，${fail} 条删除失败`, 'warning');
  else showToast(`已删除 ${ok} 条记录`, 'success');
  render(currentTid);
};

// ─── 拟合模型求值（mJy 空间；与后端 routes/lightcurves.py fit_model 严格镜像） ───
function fitModelFlux(model, prm, t) {
  if (model === 'pl') return prm.A * Math.pow(t, -prm.alpha);
  if (model === 'sbpl') {
    // F = Fb·[(t/tb)^(n·α1)+(t/tb)^(n·α2)]^(-1/n)，Fb = A·tb^(-α1)；logaddexp 保证数值稳定
    const Fb = prm.A * Math.pow(prm.tb, -prm.alpha1);
    const lnx = Math.log(t / prm.tb);
    const e1 = prm.n * prm.alpha1 * lnx, e2 = prm.n * prm.alpha2 * lnx;
    const m = Math.max(e1, e2);
    return Fb * Math.exp(-(m + Math.log(Math.exp(e1 - m) + Math.exp(e2 - m))) / prm.n);
  }
  // bpl: tb 处连续
  return t <= prm.tb
    ? prm.A * Math.pow(t, -prm.alpha1)
    : prm.A * Math.pow(prm.tb, prm.alpha2 - prm.alpha1) * Math.pow(t, -prm.alpha2);
}

function makeFitLabel(fit) {
  const prm = fit.params;
  if (fit.model === 'pl') return `${fit.band} PL: α=${sig3(prm.alpha)}`;
  if (fit.model === 'sbpl') return `${fit.band} SBPL: α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s, n=${sig3(prm.n)}`;
  return `${fit.band} BPL: α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s`;
}

// ─── 错误条绘制插件（beforeDatasetsDraw：误差棒画在最底层，不遮挡数据点与拟合线） ───
let lcShowErr = true;   // 是否绘制误差棒（图头「误差棒」开关）
const errorBarPlugin = {
  id: 'errorBar',
  beforeDatasetsDraw(chart) {
    if (!lcShowErr) return;
    try {
    const ctx = chart.ctx;
    const xScale = chart.scales.x;
    const yScale = chart.scales.y;
    if (!ctx || !yScale) return;
    chart.data.datasets.forEach((ds, dsIdx) => {
      if (!ds._errorValues || ds._isUpperLimit || ds._isFit) return;
      if (!chart.isDatasetVisible(dsIdx)) return;   // 波段被勾选取消时不画其误差棒
      const meta = chart.getDatasetMeta(dsIdx);
      if (!meta || !meta.data) return;
      ctx.save();
      ctx.strokeStyle = ds.borderColor || '#fff';
      ctx.lineWidth = 1;
      const n = Math.min(meta.data.length, ds._errorValues.length);
      for (let i = 0; i < n; i++) {
        const err = ds._errorValues[i];
        if (err == null || err <= 0) continue;
        const el = meta.data[i];
        const raw = ds.data[i];
        if (!el || el.skip || !raw || raw.y == null || !isFinite(raw.y)) continue;
        const cx = el.x;
        const yTop = yScale.getPixelForValue(raw.y + err);
        const yBot = yScale.getPixelForValue(raw.y - err);
        if (!isFinite(yTop) || !isFinite(yBot)) continue;
        ctx.beginPath();
        ctx.moveTo(cx, Math.min(yTop, yBot));
        ctx.lineTo(cx, Math.max(yTop, yBot));
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(cx - 3, yTop);
        ctx.lineTo(cx + 3, yTop);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(cx - 3, yBot);
        ctx.lineTo(cx + 3, yBot);
        ctx.stroke();
      }
      ctx.restore();
    });
    } catch (e) { console.error('errorBar plugin:', e); }
  },
};

// ─── 光变图顶部副轴（day / MJD）───
// 每帧根据主横轴当前范围在显示单位内取 1-2-5 规整刻度，再换算回秒定位像素
function _niceTicks(lo, hi, target) {
  if (!(hi > lo)) return { ticks: [], step: 1 };
  const raw = (hi - lo) / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  let step = 10 * mag;
  for (const m of [1, 2, 5, 10]) { if (raw <= m * mag) { step = m * mag; break; } }
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) ticks.push(v);
  return { ticks, step };
}

const lcTopAxisPlugin = {
  id: 'lcTopAxis',
  afterDraw(chart) {
    const cfg = _lcTopAxis;
    if (!cfg || !cfg.mode) return;
    const x = chart.scales.x;
    const area = chart.chartArea;
    if (!x || !area) return;
    // day = t/86400；mjd = T0(MJD) + t/86400
    const toDisp = cfg.mode === 'mjd' ? (t) => cfg.t0mjd + t / 86400 : (t) => t / 86400;
    const fromDisp = cfg.mode === 'mjd' ? (d) => (d - cfg.t0mjd) * 86400 : (d) => d * 86400;
    const { ticks, step } = _niceTicks(toDisp(x.min), toDisp(x.max), 5);
    const dec = step >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log10(step)));
    const fmt = cfg.mode === 'mjd'
      ? (v) => v.toFixed(Math.max(dec, 1))
      : (v) => String(parseFloat(v.toFixed(Math.max(dec, 2))));
    const cc = chartColors();
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = cc.tick;
    ctx.fillStyle = cc.tick;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(area.left, area.top);
    ctx.lineTo(area.right, area.top);
    ctx.stroke();
    ctx.font = `11px ${ACADEMIC_FONT}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    for (const d of ticks) {
      const px = x.getPixelForValue(fromDisp(d));
      if (px < area.left + 2 || px > area.right - 2) continue;
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.top - 5);
      ctx.stroke();
      ctx.fillText(fmt(d), px, area.top - 7);
    }
    ctx.fillText(cfg.mode === 'mjd' ? 'MJD' : 'time since T0 (day)', (area.left + area.right) / 2, area.top - 24);
    ctx.restore();
  },
};

// ─── 光变图波段勾选面板（替代内置图例的划线开关） ───
function applyBandVisibility() {
  const chart = lcChartInstance;
  if (!chart) return;
  chart.data.datasets.forEach((ds, i) => {
    if (ds._isFit || ds._band == null) return;
    chart.setDatasetVisibility(i, lcBandVisible[ds._band] !== false);
  });
  chart.update();
}

function buildBandPanel(sortedBands, spectralColors) {
  const el = document.getElementById('lcBandPanel');
  if (!el) return;
  el.innerHTML = `<span class="text-secondary">波段:</span>` + sortedBands.map((b, i) => {
    const vis = lcBandVisible[b] !== false;
    const c = spectralColors[b] || '#58a6ff';
    return `<span class="form-check form-check-inline mb-0">
      <input class="form-check-input lc-band-chk" type="checkbox" id="lcBandChk_${i}" data-band="${escAttr(b)}" ${vis ? 'checked' : ''}>
      <label class="form-check-label" for="lcBandChk_${i}"><span style="color:${c}">●</span> ${b}</label>
    </span>`;
  }).join('') + `
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="lcBandAll">全选</button>
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="lcBandNone">全不选</button>`;
  el.querySelectorAll('.lc-band-chk').forEach(chk => {
    chk.addEventListener('change', () => {
      lcBandVisible[chk.dataset.band] = chk.checked;
      applyBandVisibility();
    });
  });
  document.getElementById('lcBandAll')?.addEventListener('click', () => {
    for (const b of sortedBands) lcBandVisible[b] = true;
    el.querySelectorAll('.lc-band-chk').forEach(c => { c.checked = true; });
    applyBandVisibility();
  });
  document.getElementById('lcBandNone')?.addEventListener('click', () => {
    for (const b of sortedBands) lcBandVisible[b] = false;
    el.querySelectorAll('.lc-band-chk').forEach(c => { c.checked = false; });
    applyBandVisibility();
  });
}

// ─── 概览「宿主星系」行：异步拉取摘要，点击跳转到宿主星系标签页 ───
async function fillHostSummary(tid) {
  const cell = document.getElementById('hostSummaryCell');
  if (!cell) return;
  let host = null;
  try { host = await getHost(tid); } catch { host = null; }   // 404/失败按暂无处理
  if (currentTid !== tid || !cell.isConnected) return;        // 已切换源或页面重建
  if (!host) {
    cell.innerHTML = '<span class="text-secondary">暂无</span>';
    return;
  }
  const d = host.derived || {};
  const parts = [];
  if (host.redshift != null) parts.push(`z=${host.redshift} (${escAttr(host.redshift_type || '?')})`);
  if (d.m_star != null) parts.push(`M*=${sig3(d.m_star)} M☉`);
  if (d.sfr != null) parts.push(`SFR=${sig3(d.sfr)} M☉/yr`);
  if (host.ra != null && host.dec != null) parts.push(`(${Number(host.ra).toFixed(4)}, ${Number(host.dec).toFixed(4)})`);
  cell.innerHTML = `<a href="#" id="hostSummaryLink" title="查看宿主星系标签页">${parts.join(' · ') || '有记录'}</a>`;
  const link = document.getElementById('hostSummaryLink');
  if (link) link.addEventListener('click', (e) => {
    e.preventDefault();
    document.querySelector('#detailTabs .nav-link[data-tab="host"]')?.click();
  });
}

// ─── 主渲染函数 ───
export async function render(tid) {
  currentTid = tid;
  if (lcMarkedTid !== tid) { lcMarked = new Set(); lcMarkedTid = tid; }  // 切换源时清空行标记
  // 重置图/拟合/坐标范围/编辑状态（DOM 即将重建）
  if (lcChartInstance) { try { lcChartInstance.destroy(); } catch {} lcChartInstance = null; }
  if (specChartInstance) { try { specChartInstance.destroy(); } catch {} specChartInstance = null; }
  _spectraLoadedFor = null;
  specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  destroyFittingTab(); // 停止拟合标签页轮询/图表（DOM 即将重建）
  destroyHostfitTab(); // 停止宿主星系标签页轮询（DOM 即将重建）
  lcFits = [];
  lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  lcBandVisible = {};
  lcShowErr = true;
  _lcTopAxis = null;
  _lcRedshift = null;
  _lcT0MJD = null;
  _lcDistmod = null;
  derivedDraft = null;
  derivedPristine = null;
  derivedEditMode = false;
  showLoading();

  try {
    const [transient, lcData, filtersData, articles] = await Promise.all([
      getTransient(tid),
      getLightcurves({ transient_id: tid, per_page: 9999 }),
      getFilters(),
      getArticles(tid),
    ]);
    articlesData = articles;
    lcItems = lcData.items;
    lcSortState = { key: 'time', dir: 1 };
    filters = filtersData;
    ensureFilterCache(filtersData);
    // 光变图用的源级参数
    _lcRedshift = (transient.redshift != null && transient.redshift > -1) ? transient.redshift : null;
    _lcT0MJD = t0ToMJD(transient.t0);
    _lcDistmod = (transient.distmod != null) ? transient.distmod : null;
    const canRestFrame = _lcRedshift != null;
    const canAbsMag = _lcDistmod != null;
    const canMJD = _lcT0MJD != null;

    // 按波段分组
    const bands = {};
    for (const pt of lcData.items) {
      if (!bands[pt.band]) bands[pt.band] = [];
      bands[pt.band].push(pt);
    }
    const bandNames = Object.keys(bands);
    const spectralColors = buildSpectralColors(bandNames);
    const hasCoords = (transient.ra != null && transient.dec != null);
    // 无坐标源：Aladin 照常渲染，指向银河系中心（Sgr A* 方向）作占位展示
    const aladinRa = hasCoords ? transient.ra : 266.405;
    const aladinDec = hasCoords ? transient.dec : -28.936;

    app.innerHTML = `
      <div class="mb-3">
        <a href="#/" class="text-secondary text-decoration-none small"><i class="bi bi-arrow-left"></i> 返回列表</a>
      </div>

      <!-- Header -->
      <div class="d-flex justify-content-between align-items-start mb-3">
        <div>
          <h4 class="mb-1"><strong>${transient.id}</strong>
            <small class="text-secondary ms-2 fs-6">
              ${(transient.aliases || []).join(', ')}
            </small>
          </h4>
          <div class="text-secondary small">
            ${(transient.tags || []).map(t => `<span class="badge-tag">${t}</span>`).join('')}
            ${(transient.sub_tag || []).map(t => `<span class="badge-neutral me-1">${t}</span>`).join('')}
          </div>
        </div>
        <div class="d-flex gap-1">
          <button class="btn btn-sm btn-outline-warning" onclick="runGextSource()" id="gextBtn" title="对本源全部光学数据执行银河系消光改正">
            <i class="bi bi-moon-stars"></i> 银消改正
          </button>
          <button class="btn btn-sm btn-outline-secondary" onclick="handleEditClick()" id="editBtn" title="编辑">
            <i class="bi bi-pencil"></i>
          </button>
          <button class="btn btn-sm btn-outline-secondary" onclick="APIImport.exportLC('${tid}')" title="导出光变CSV">
            <i class="bi bi-download"></i>
          </button>
        </div>
      </div>

      <!-- Tabs -->
      <ul class="nav nav-tabs mb-3" id="detailTabs">
        <li class="nav-item"><a class="nav-link active" href="#" data-tab="overview">概览</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="lc">光变曲线</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="data">数据表</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="fitting">余辉拟合</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="host">宿主星系</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="spectra">光谱数据</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="sed">余辉SED分析 <small class="text-secondary">(开发中)</small></a></li>
      </ul>

      <div id="tabContent">
        <!-- ─── 概览页（含 Aladin 方形全天图） ─── -->
        <div id="tab-overview" class="tab-pane active">
          <div class="row g-3 align-items-stretch">
            <div class="col-md-4">
              <div class="card h-100" id="metaCard">
                <div class="card-header"><i class="bi bi-info-circle"></i> 基本信息</div>
                <div class="card-body">
                  <table class="table table-sm table-borderless" id="metaTable" style="table-layout:fixed">
                    <tbody>
                      <tr><td class="text-secondary" style="width:140px">RA</td><td>${transient.ra != null ? transient.ra.toFixed(6) : '-'}</td></tr>
                      <tr><td class="text-secondary">Dec</td><td>${transient.dec != null ? transient.dec.toFixed(6) : '-'}</td></tr>
                      <tr><td class="text-secondary">T0</td><td>${transient.t0 || '-'}</td></tr>
                      <tr><td class="text-secondary">触发仪器</td><td>${transient.trigger_instrument || '-'}</td></tr>
                      <tr><td class="text-secondary">红移</td><td>${transient.redshift != null ? `${transient.redshift} (${transient.redshift_type || '?'})` : '<span class="text-secondary">未知</span>'}</td></tr>
                      <tr><td class="text-secondary">红移引用</td><td class="small">${transient.redshift_ref || '-'}</td></tr>
                      <tr><td class="text-secondary">位置误差</td><td>${transient.pos_error != null ? `${transient.pos_error} ${transient.pos_error_unit || 'arcsec'}` : '-'}</td></tr>
                      <tr><td class="text-secondary" style="width:140px">位置引用</td><td class="small">${transient.pos_ref || '-'}</td></tr>
                      <tr><td class="text-secondary">备注</td><td class="small">${transient.comment || '-'}</td></tr>
                      <tr><td class="text-secondary">研究文章</td><td class="small" id="articleCell">${articlesHTML()}</td></tr>
                      <tr><td class="text-secondary">数据点</td><td>${lcData.total}</td></tr>
                      <tr><td class="text-secondary">宿主星系</td><td class="small" id="hostSummaryCell"><span class="text-secondary">加载中…</span></td></tr>
                    </tbody>
                  </table>
                </div>
              </div>
              <!-- 编辑面板（默认隐藏） -->
              <div class="card" id="editPanel" style="display:none">
                <div class="card-header"><i class="bi bi-pencil"></i> 编辑基本信息</div>
                <div class="card-body">
                  <form onsubmit="return false">
                    <div class="row g-2">
                      <div class="col-6"><label class="form-label small">RA</label><input type="text" class="form-control form-control-sm" id="editRa" value="${transient.ra ?? ''}" placeholder="度 或 08h08m27.4s"></div>
                      <div class="col-6"><label class="form-label small">Dec</label><input type="text" class="form-control form-control-sm" id="editDec" value="${transient.dec ?? ''}" placeholder="度 或 +40d36m44.8s"></div>
                      <div class="col-6"><label class="form-label small">T0</label><input type="text" class="form-control form-control-sm" id="editT0" value="${transient.t0 || ''}"></div>
                      <div class="col-6"><label class="form-label small">触发仪器</label><input type="text" class="form-control form-control-sm" id="editTrigger" value="${transient.trigger_instrument || ''}"></div>
                      <div class="col-4"><label class="form-label small">红移</label><input type="number" class="form-control form-control-sm" id="editZ" step="any" value="${transient.redshift ?? ''}"></div>
                      <div class="col-4"><label class="form-label small">红移类型</label>
                        <select class="form-select form-select-sm" id="editZType"><option value="">-</option><option value="value" ${transient.redshift_type==='value'?'selected':''}>value</option><option value="phot_z" ${transient.redshift_type==='phot_z'?'selected':''}>phot_z</option><option value="spec" ${transient.redshift_type==='spec'?'selected':''}>spec</option><option value="spec-host" ${transient.redshift_type==='spec-host'?'selected':''}>spec-host</option><option value="upperlimit" ${transient.redshift_type==='upperlimit'?'selected':''}>upperlimit</option></select>
                      </div>
                      <div class="col-4"><label class="form-label small">红移引用</label><input type="text" class="form-control form-control-sm" id="editZRef" value="${transient.redshift_ref || ''}"></div>
                      <div class="col-4"><label class="form-label small">位置误差</label><input type="number" class="form-control form-control-sm" id="editPosErr" step="any" value="${transient.pos_error ?? ''}"></div>
                      <div class="col-8"><label class="form-label small">位置引用</label><input type="text" class="form-control form-control-sm" id="editPosRef" value="${transient.pos_ref || ''}"></div>
                      <div class="col-12"><label class="form-label small">备注</label><textarea class="form-control form-control-sm" id="editComment" rows="2">${transient.comment || ''}</textarea></div>
                      <div class="col-4"><label class="form-label small">标签 (逗号分隔)</label><input type="text" class="form-control form-control-sm" id="editTags" value="${(transient.tags || []).join(', ')}"></div>
                      <div class="col-4"><label class="form-label small">子标签 (逗号分隔)</label><input type="text" class="form-control form-control-sm" id="editSubTags" value="${(transient.sub_tag || []).join(', ')}"></div>
                      <div class="col-4"><label class="form-label small">别名 (逗号分隔)</label><input type="text" class="form-control form-control-sm" id="editAliases" value="${(transient.aliases || []).join(', ')}"></div>
                    </div>
                    <div class="mt-3 d-flex gap-2">
                      <button class="btn btn-sm btn-primary" onclick="saveDetailEdit()"><i class="bi bi-check-lg"></i> 保存</button>
                      <button class="btn btn-sm btn-outline-secondary" onclick="handleEditClick()">取消</button>
                    </div>
                  </form>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card h-100">
                <div class="card-header"><i class="bi bi-bar-chart"></i> 光变概览</div>
                <div class="card-body">
                  <div class="row g-2">
                    <div class="col-6"><div class="stat-card"><div class="stat-value">${bandNames.length}</div><div class="stat-label">波段</div></div></div>
                    <div class="col-6"><div class="stat-card"><div class="stat-value">${lcData.total}</div><div class="stat-label">数据点</div></div></div>
                  </div>
                  <div class="mt-2">
                    <strong class="small text-secondary">波段覆盖:</strong>
                    <div class="mt-1 d-flex flex-wrap gap-1">
                      ${bandNames.map(b => `<span class="badge-tag" style="background:${spectralColors[b] || '#58a6ff'}33;color:${spectralColors[b] || '#58a6ff'}">${b}</span>`).join('')}
                    </div>
                  </div>
                  <div class="mt-3">
                    <strong class="small text-secondary">望远镜:</strong>
                    <div class="mt-1 small text-secondary">
                      ${[...new Set(lcData.items.map(p => p.telescope).filter(Boolean))].join(', ')}
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card h-100">
                <div class="card-header"><i class="bi bi-globe2"></i> Aladin 全天图
                  ${hasCoords
                    ? `<small class="text-secondary">RA=${transient.ra.toFixed(4)}° Dec=${transient.dec.toFixed(4)}°</small>`
                    : '<small class="text-warning">该源暂无坐标，图示为银河系中心</small>'}
                </div>
                <div class="card-body p-0 d-flex flex-grow-1">
                  <div id="aladinContainer" style="width:100%;min-height:520px;flex:1;border-radius:0 0 8px 8px;background:#000"></div>
                </div>
              </div>
            </div>
          </div>
          <!-- 外部目录参数（T90/Epeak/fluence/Eiso 等，含来源） -->
          ${renderCatalogData(transient.extra_data)}
          <!-- 派生物理量 (derived) -->
          ${renderDerivedCard(transient.extra_data)}
        </div>

        <!-- ─── 光变曲线 ─── -->
        <div id="tab-lc" class="tab-pane" style="display:none">
          <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
              <span><i class="bi bi-graph-up"></i> 光变曲线</span>
              <div class="d-flex gap-2 align-items-center flex-wrap">
                <select class="form-select form-select-sm" style="width:auto" id="gextMode" onchange="rebuildLCPlot()">
                  <option value="raw" selected>数据: 原始</option>
                  <option value="gext">数据: 银消改正后</option>
                </select>
                <select class="form-select form-select-sm" style="width:auto" id="yMode" onchange="rebuildLCPlot()"
                        ${canAbsMag ? '' : 'title="该源无红移，无法计算距离模数，绝对星等不可用"'}>
                  <option value="flux" selected>Y: 流量密度</option>
                  <option value="absmag" ${canAbsMag ? '' : 'disabled'}>Y: 绝对星等</option>
                </select>
                <select class="form-select form-select-sm" style="width:auto" id="xScale" onchange="rebuildLCPlot()">
                  <option value="logarithmic" selected>X: 对数</option>
                  <option value="linear">X: 线性</option>
                </select>
                <select class="form-select form-select-sm" style="width:auto" id="topAxis" onchange="rebuildLCPlot()"
                        ${canMJD ? '' : 'title="该源无 T0，MJD 轴不可用"'}>
                  <option value="day" selected>顶部轴: 天</option>
                  <option value="mjd" ${canMJD ? '' : 'disabled'}>顶部轴: MJD</option>
                  <option value="none">顶部轴: 无</option>
                </select>
                <div class="form-check form-check-inline mb-0" title="是否绘制数据点的星等/流量密度误差棒">
                  <input class="form-check-input" type="checkbox" id="lcShowErr" checked onchange="lcShowErrToggle(this.checked)">
                  <label class="form-check-label small" for="lcShowErr">误差棒</label>
                </div>
                <div class="form-check form-check-inline mb-0" ${canRestFrame ? 'title="时间轴除以 (1+z) 改正到静止系"' : 'title="该源无红移，静止系不可用"'}>
                  <input class="form-check-input" type="checkbox" id="lcRestFrame" onchange="rebuildLCPlot()" ${canRestFrame ? '' : 'disabled'}>
                  <label class="form-check-label small" for="lcRestFrame">静止系 t/(1+z)</label>
                </div>
                <button class="btn btn-sm btn-outline-secondary" onclick="resetLCZoom()"><i class="bi bi-arrows-expand"></i></button>
              </div>
            </div>
            <div class="card-body">
              <!-- 坐标范围手动调节（留空 = 该端自动） -->
              <div class="d-flex flex-wrap gap-1 align-items-center mb-2 small">
                <span class="text-secondary me-1">坐标范围:</span>
                <input type="text" class="form-control form-control-sm" id="axXmin" placeholder="xmin" title="x 轴最小值（当前 x 轴单位，秒）" style="width:80px">
                <input type="text" class="form-control form-control-sm" id="axXmax" placeholder="xmax" title="x 轴最大值（当前 x 轴单位，秒）" style="width:80px">
                <input type="text" class="form-control form-control-sm" id="axYmin" placeholder="ymin" title="y 轴最小值 (mJy)" style="width:80px">
                <input type="text" class="form-control form-control-sm" id="axYmax" placeholder="ymax" title="y 轴最大值 (mJy)" style="width:80px">
                <button class="btn btn-sm btn-outline-primary" onclick="applyLCAxisRange()">应用</button>
                <button class="btn btn-sm btn-outline-secondary" onclick="resetLCAxisRange()">恢复默认</button>
              </div>
              <div class="chart-container" style="height:auto;aspect-ratio:3/2;min-height:0;overflow:hidden"><canvas id="lcChart"></canvas></div>
              <!-- 波段显示勾选面板（替代内置图例的划线开关） -->
              <div id="lcBandPanel" class="d-flex flex-wrap gap-2 align-items-center mt-2 small"></div>
              ${lcData.total > 0 ? `
              <!-- 光变曲线拟合 -->
              <div class="mt-3 border-top pt-2" id="lcFitSection">
                <div class="d-flex flex-wrap gap-1 align-items-center small">
                  <span class="text-secondary me-1"><i class="bi bi-bezier2"></i> 添加拟合:</span>
                  <select class="form-select form-select-sm" id="fitModel" style="width:auto" onchange="fitModelChanged()">
                    <option value="pl">powerlaw</option>
                    <option value="bpl">broken-powerlaw</option>
                    <option value="sbpl">smoothly-broken-powerlaw</option>
                  </select>
                  <select class="form-select form-select-sm" id="fitBand" style="width:auto">
                    ${bandNames.map(b => `<option value="${escAttr(b)}">${b}</option>`).join('')}
                  </select>
                  <input type="text" class="form-control form-control-sm" id="fitTmin" placeholder="拟合 t_min (s)" title="拟合数据时间下限（留空=全范围）" style="width:100px">
                  <input type="text" class="form-control form-control-sm" id="fitTmax" placeholder="拟合 t_max (s)" title="拟合数据时间上限（留空=全范围）" style="width:100px">
                  <span id="fitTbRange" class="align-items-center gap-1" style="display:none">
                    <span class="text-secondary">tb∈[</span>
                    <input type="text" class="form-control form-control-sm" id="fitTbMin" placeholder="tb_min" title="拐点 tb 预设下限（秒，留空=数据范围）" style="width:70px">
                    <span class="text-secondary">,</span>
                    <input type="text" class="form-control form-control-sm" id="fitTbMax" placeholder="tb_max" title="拐点 tb 预设上限（秒，留空=数据范围）" style="width:70px">
                    <span class="text-secondary">]s</span>
                  </span>
                  <input type="text" class="form-control form-control-sm" id="fitPmin" placeholder="绘制 t_min (s)" title="拟合线绘制范围下限（外推用；留空=与拟合范围一致）" style="width:100px">
                  <input type="text" class="form-control form-control-sm" id="fitPmax" placeholder="绘制 t_max (s)" title="拟合线绘制范围上限（外推用；留空=与拟合范围一致）" style="width:100px">
                  <button class="btn btn-sm btn-outline-primary" onclick="addLCFit()"><i class="bi bi-plus-lg"></i> 添加</button>
                </div>
                <div id="lcFitList" class="mt-2 small"></div>
              </div>` : ''}
            </div>
          </div>
        </div>

        <!-- ─── 数据表 ─── -->
        <div id="tab-data" class="tab-pane" style="display:none">
          <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
              <span><i class="bi bi-table"></i> 光变数据表</span>
              <div class="d-flex gap-1">
                <button class="btn btn-sm btn-outline-secondary" onclick="lcColPanelToggle()" title="勾选要显示的列"><i class="bi bi-layout-three-columns"></i> 列显示</button>
                <button class="btn btn-sm btn-outline-primary" id="lcUploadBtn" style="display:none" onclick="lcUploadShow()"><i class="bi bi-upload"></i> 上传数据表</button>
                <button class="btn btn-sm btn-outline-primary" id="lcAddBtn" style="display:none" onclick="lcAddNewRow()"><i class="bi bi-plus-circle"></i> 添加记录</button>
                <button class="btn btn-sm btn-outline-danger" id="lcDelBtn" style="display:none" onclick="lcDeleteSelected()"><i class="bi bi-trash"></i> 删除选中</button>
              </div>
            </div>
            <div class="card-body p-0">
              <!-- 列显示勾选面板（「列显示」按钮展开） -->
              <div id="lcColPanel" class="px-2 py-2 border-bottom" style="display:none">
                <div class="d-flex flex-wrap gap-1 align-items-center small" id="lcColChecks"></div>
              </div>
              <div class="table-scroll" style="max-height:500px">
                <table class="table table-sm table-hover mb-0" style="font-size:0.8rem">
                  <thead>
                    <tr>
                      <th title="标记/取消标记全部行（整行高亮，便于对比定位）"><input type="checkbox" id="lcMarkAll" onclick="lcMarkToggleAll(this.checked)"></th>
                      <th id="lcDelHeader" style="display:none"><input type="checkbox" id="lcDelAll" onclick="lcDelToggleAll(this.checked)" title="全选"></th>
                      ${LC_COLS.map(([k, label]) => `
                      <th class="lc-sortable" data-col="${k}" data-sort="${k}" onclick="lcSort('${k}')" title="点击排序">${label}<span class="lc-sort-ind" data-ind="${k}">${k === 'time' ? ' ▲' : ''}</span></th>`).join('')}
                      <th id="lcEditHeader" style="display:none">编辑</th>
                    </tr>
                  </thead>
                  <tbody id="lcTableBody">
                    ${lcData.items.map(lcRowHTML).join('')}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
        <!-- ─── 余辉拟合（由 fitting_tab.js 填充） ─── -->
        <div id="tab-fitting" class="tab-pane" style="display:none"></div>
        <!-- ─── 宿主星系（由 hostfit_tab.js 填充） ─── -->
        <div id="tab-host" class="tab-pane" style="display:none"></div>
        <!-- ─── 光谱数据（单条查看 / 多条对比） ─── -->
        <div id="tab-spectra" class="tab-pane" style="display:none">
          <div class="row g-3">
            <div class="col-md-4">
              <div class="card h-100">
                <div class="card-header d-flex justify-content-between align-items-center">
                  <span><i class="bi bi-list-ul"></i> 光谱列表</span>
                  <button class="btn btn-sm btn-outline-primary py-0" id="specUploadBtn" style="display:none" onclick="showSpecUpload()" title="上传光谱"><i class="bi bi-upload"></i> 上传</button>
                </div>
                <div class="card-body p-0" style="max-height:560px;overflow-y:auto">
                  <div class="small text-secondary px-2 pt-1">点击行切换选择，可多选对比</div>
                  <table class="table table-sm table-hover mb-0" style="font-size:0.85rem">
                    <thead><tr><th>观测时间 (MJD)</th><th>仪器</th><th>观测者</th></tr></thead>
                    <tbody id="spectraListBody">
                      <tr><td colspan="3" class="text-center text-secondary py-3">加载中...</td></tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
            <div class="col-md-8">
              <div class="card h-100">
                <div class="card-header d-flex justify-content-between align-items-center">
                  <span><i class="bi bi-activity"></i> <span id="specTitle">光谱</span></span>
                  <div class="d-flex align-items-center gap-2">
                    <small class="text-secondary" id="specMeta"></small>
                    <div class="form-check form-check-inline mb-0" id="specErrChkWrap" title="是否绘制流量误差条">
                      <input class="form-check-input" type="checkbox" id="specErrChk" checked onchange="toggleSpecErr(this.checked)">
                      <label class="form-check-label small text-secondary" for="specErrChk">误差</label>
                    </div>
                    <select class="form-select form-select-sm" style="width:auto" id="specModeSel" onchange="setSpecMode(this.value)">
                      <option value="absolute" selected>绝对流量</option>
                      <option value="relative">相对流量</option>
                    </select>
                  </div>
                </div>
                <div class="card-body">
                  <div class="d-flex flex-wrap gap-1 align-items-center mb-2 small">
                    <span class="text-secondary me-1">坐标范围:</span>
                    <input type="text" class="form-control form-control-sm" id="specAxXmin" placeholder="xmin" title="x 轴最小值（Å，观测者系）" style="width:80px">
                    <input type="text" class="form-control form-control-sm" id="specAxXmax" placeholder="xmax" title="x 轴最大值（Å，观测者系）" style="width:80px">
                    <input type="text" class="form-control form-control-sm" id="specAxYmin" placeholder="ymin" title="y 轴最小值（流量）" style="width:80px">
                    <input type="text" class="form-control form-control-sm" id="specAxYmax" placeholder="ymax" title="y 轴最大值（流量）" style="width:80px">
                    <button class="btn btn-sm btn-outline-primary" onclick="applySpecAxisRange()">应用</button>
                    <button class="btn btn-sm btn-outline-secondary" onclick="resetSpecAxisRange()">恢复默认</button>
                    <select class="form-select form-select-sm" style="width:auto" id="specXTypeSel" onchange="setSpecAxisType()">
                      <option value="linear" ${specAxisType.x === 'linear' ? 'selected' : ''}>X: 线性</option>
                      <option value="logarithmic" ${specAxisType.x === 'logarithmic' ? 'selected' : ''}>X: 对数</option>
                    </select>
                    <select class="form-select form-select-sm" style="width:auto" id="specYTypeSel" onchange="setSpecAxisType()">
                      <option value="linear" ${specAxisType.y === 'linear' ? 'selected' : ''}>Y: 线性</option>
                      <option value="logarithmic" ${specAxisType.y === 'logarithmic' ? 'selected' : ''}>Y: 对数</option>
                    </select>
                    <span class="text-secondary ms-1">（也可在图上拖拽框选缩放）</span>
                  </div>
                  <details class="mb-2">
                    <summary class="small text-secondary" style="cursor:pointer">谱线标记（TNS 风格：H/He/C/N/O… 常见线、自定义波长、Tellurics、星系线、WR 线，z 与 v<sub>exp</sub> 可调）</summary>
                    <div id="specMarkingsBody" class="border rounded p-2 mt-1" style="max-height:260px;overflow-y:auto"></div>
                  </details>
                  <div class="chart-container" style="height:500px"><canvas id="specChart"></canvas></div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <!-- ─── 余辉SED分析（预留） ─── -->
        <div id="tab-sed" class="tab-pane" style="display:none">
          <div class="card">
            <div class="card-body text-center text-secondary py-5">
              <i class="bi bi-graph-up-arrow" style="font-size:2rem"></i>
              <p class="mt-2 mb-0">余辉 SED 分析功能开发中，敬请期待</p>
            </div>
          </div>
        </div>
      </div>

      <!-- 上传光谱弹窗 -->
      <div class="modal fade" id="specUploadModal" tabindex="-1">
        <div class="modal-dialog">
          <div class="modal-content">
            <div class="modal-header"><h6 class="mb-0"><i class="bi bi-upload"></i> 上传光谱到 ${tid}</h6>
              <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
            <div class="modal-body">
              <div class="mb-2">
                <label class="form-label small">光谱文件（两列文本 或 OpenSNSpectra 风格 JSON）</label>
                <input type="file" class="form-control form-control-sm" id="specFile" accept=".txt,.dat,.csv,.json">
              </div>
              <div class="row g-2">
                <div class="col-6"><label class="form-label small">仪器</label><input class="form-control form-control-sm" id="specUpInstrument" placeholder="如 VLT-FORS2"></div>
                <div class="col-6"><label class="form-label small">MJD（观测日）</label><input class="form-control form-control-sm" id="specUpMjd" placeholder="如 53786.0"></div>
                <div class="col-6"><label class="form-label small">观测者</label><input class="form-control form-control-sm" id="specUpObserver"></div>
                <div class="col-6"><label class="form-label small">归算者(可选)</label><input class="form-control form-control-sm" id="specUpReducer"></div>
                <div class="col-6"><label class="form-label small">流量类型</label>
                  <select class="form-select form-select-sm" id="specUpFluxType">
                    <option value="absolute" selected>绝对流量 (erg/s/cm²/Å)</option>
                    <option value="normalized">归一化流量（谱形保留，无量纲）</option>
                  </select>
                </div>
              </div>
              <div class="alert alert-dark small mt-3 mb-0" role="note">
                <strong>格式要求：</strong>波长一律为<strong>观测者系</strong>（单位 Å）。
                ① 两列或三列文本：<code>波长 流量 [流量误差]</code>，空格/逗号分隔，<code>#</code> 开头为注释，可用 <code># instrument: xx</code>、<code># mjd: xx</code> 声明元数据；② JSON：<code>{"名称": {"spectra": {"time": MJD, "instrument": "...", "data": [[波长, 流量, (误差)], ...]}}}</code>。
                至少 10 个数据点，波长范围 100–10⁷ Å。流量为绝对流量（erg/s/cm²/Å）或归一化流量（在上方"流量类型"选择）。
              </div>
              <div class="small text-danger mt-2" id="specUpError" style="display:none"></div>
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
              <button class="btn btn-sm btn-primary" id="specUpSubmit" onclick="doSpecUpload()">上传</button>
            </div>
          </div>
        </div>
      </div>
    `;

    // ─── Tab 切换 ───
    document.querySelectorAll('#detailTabs .nav-link').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        if (link.classList.contains('disabled')) return;
        document.querySelectorAll('#detailTabs .nav-link').forEach(l => l.classList.remove('active'));
        link.classList.add('active');
        document.querySelectorAll('#tabContent .tab-pane').forEach(t => t.style.display = 'none');
        const pane = document.getElementById(`tab-${link.dataset.tab}`);
        if (pane) pane.style.display = 'block';
        // Aladin：概览页首次显示时初始化（无坐标源指向银河系中心占位）
        if (link.dataset.tab === 'overview' && !aladinInstance) {
          setTimeout(() => initAladin(aladinRa, aladinDec, hasCoords ? transient.id : null), 200);
        }
        // LC chart
        if (link.dataset.tab === 'lc') {
          setTimeout(() => initLCPlot(bands, bandNames, spectralColors), 100);
        }
        // 余辉拟合：进入时初始化，切走时停止轮询
        if (link.dataset.tab === 'fitting') {
          initFittingTab(pane, tid);
        } else {
          destroyFittingTab();
        }
        // 宿主星系：进入时初始化，切走时停止轮询
        if (link.dataset.tab === 'host') {
          initHostfitTab(pane, tid);
        } else {
          destroyHostfitTab();
        }
        // 光谱数据：进入时加载光谱
        if (link.dataset.tab === 'spectra') {
          setTimeout(() => initSpectraTab(tid, transient.redshift), 100);
        }
      });
    });

    // 默认初始化 Aladin（概览页默认显示；无坐标源指向银河系中心占位）
    setTimeout(() => initAladin(aladinRa, aladinDec, hasCoords ? transient.id : null), 500);

    // ─── derived 卡片初始化 ───
    if (transient.extra_data && transient.extra_data.derived) {
      derivedPristine = JSON.parse(JSON.stringify(transient.extra_data.derived));
      derivedDraft = JSON.parse(JSON.stringify(transient.extra_data.derived));
      renderDerivedBody();
      if (isAdmin()) {
        const btn = document.getElementById('derivedEditBtn');
        if (btn) btn.style.display = '';
      }
    }

    // 概览「宿主星系」摘要行（独立请求，不阻塞主渲染）
    fillHostSummary(tid);

    // 全局函数
    window.APIImport = { exportLC: exportLightcurves };
    window.rebuildLCPlot = () => rebuildLCPlot(bands, bandNames, spectralColors);
    window.resetLCZoom = () => { if (lcChartInstance) lcChartInstance.resetZoom(); };
    window.lcShowErrToggle = (on) => {   // 误差棒开关：只影响绘制，不动数据，无动画重绘即可
      lcShowErr = on;
      if (lcChartInstance) lcChartInstance.update('none');
    };

    // ── 坐标范围手动调节 ──
    window.applyLCAxisRange = () => {
      const v = (id) => {
        const el = document.getElementById(id);
        const s = el ? el.value.trim() : '';
        if (s === '') return null;
        const n = parseFloat(s);
        return isFinite(n) ? n : null;
      };
      lcAxisRange = { xmin: v('axXmin'), xmax: v('axXmax'), ymin: v('axYmin'), ymax: v('axYmax') };
      rebuildLCPlot(bands, bandNames, spectralColors);
    };
    window.resetLCAxisRange = () => {
      lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
      ['axXmin', 'axXmax', 'axYmin', 'axYmax'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      rebuildLCPlot(bands, bandNames, spectralColors);
    };

    // ── 光变曲线拟合 ──
    window.fitModelChanged = () => {
      const model = document.getElementById('fitModel')?.value;
      const rng = document.getElementById('fitTbRange');
      if (rng) rng.style.display = (model === 'bpl' || model === 'sbpl') ? 'inline-flex' : 'none';
    };
    window.addLCFit = async () => {
      const model = document.getElementById('fitModel')?.value || 'pl';
      const band = document.getElementById('fitBand')?.value;
      const tminIn = parseFloat(document.getElementById('fitTmin')?.value);
      const tmaxIn = parseFloat(document.getElementById('fitTmax')?.value);
      const useGext = document.getElementById('gextMode')?.value === 'gext';
      // 该波段 + 时间范围内的探测点（排除 discard 与上限点），统一换算到 mJy 空间
      const pts = (bands[band] || [])
        .filter(p => !p.discard && !p.upperlimit)
        .map(p => {
          const m = pointToMJy(p, useGext);
          return m ? { t: p.time, f: m.y, ferr: m.err } : null;
        })
        .filter(d => d && d.t > 0 && d.f > 0
          && (isNaN(tminIn) || d.t >= tminIn)
          && (isNaN(tmaxIn) || d.t <= tmaxIn));
      const need = model === 'pl' ? 3 : (model === 'bpl' ? 5 : 6);
      if (pts.length < need) {
        showToast(`有效数据点不足（${FIT_MODEL_NAMES[model] || model} 需 ≥${need} 点，当前 ${pts.length} 点）`, 'warning');
        return;
      }
      // bpl/sbpl 拐点预设范围（留空端=数据范围）
      let bounds = null;
      if (model === 'bpl' || model === 'sbpl') {
        const lo = parseFloat(document.getElementById('fitTbMin')?.value);
        const hi = parseFloat(document.getElementById('fitTbMax')?.value);
        if ((isFinite(lo) || isFinite(hi))) {
          const ts = pts.map(d => d.t);
          const blo = isFinite(lo) ? lo : Math.min(...ts);
          const bhi = isFinite(hi) ? hi : Math.max(...ts);
          if (blo < bhi) bounds = { tb: [blo, bhi] };
          else { showToast('tb 预设范围无效（tb_min 需小于 tb_max）', 'warning'); return; }
        }
      }
      try {
        const payload = { model, points: pts };
        if (bounds) payload.bounds = bounds;
        const res = await fitLightcurveModel(payload);
        const ts = pts.map(d => d.t);
        const tmin = isNaN(tminIn) ? Math.min(...ts) : tminIn;
        const tmax = isNaN(tmaxIn) ? Math.max(...ts) : tmaxIn;
        const pminIn = parseFloat(document.getElementById('fitPmin')?.value);
        const pmaxIn = parseFloat(document.getElementById('fitPmax')?.value);
        const fit = {
          model, band,
          tmin, tmax,
          // 绘制（外推）范围：仅影响拟合线在图上的绘制区间，默认与拟合范围一致
          pmin: (isFinite(pminIn) && pminIn > 0) ? pminIn : tmin,
          pmax: (isFinite(pmaxIn) && pmaxIn > 0) ? pmaxIn : tmax,
          params: res.params,
          N: res.N,
          color: FIT_COLORS[lcFits.length % FIT_COLORS.length],
        };
        if (!(fit.pmax > fit.pmin)) {
          showToast('绘制范围无效（绘制 t_max 需大于 t_min）', 'warning');
          return;
        }
        fit.label = makeFitLabel(fit);
        lcFits.push(fit);
        renderFitList();
        rebuildLCPlot(bands, bandNames, spectralColors);
        showToast('拟合已添加', 'success');
      } catch (err) {
        showToast(`拟合失败: ${err.message}`, 'danger');
      }
    };
    window.removeLCFit = (i) => {
      lcFits.splice(i, 1);
      renderFitList();
      rebuildLCPlot(bands, bandNames, spectralColors);
    };

    // ── 银河系消光改正 ──
    window.runGextSource = async () => {
      if (!isAdmin()) { showToast('仅管理员可执行银消改正', 'warning'); return; }
      if (transient.ra == null || transient.dec == null) {
        showToast('该源缺少坐标，无法执行银消改正', 'warning');
        return;
      }
      if (!confirm(`对 ${tid} 的全部光学数据执行银河系消光改正？\n（CSFD 尘埃图 + P92 消光曲线，Rv=3.1）`)) return;
      const btn = document.getElementById('gextBtn');
      if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 计算中...'; }
      try {
        const st = await runExtinction({ transient_id: tid });
        showToast(`银消改正完成: ${st.corrected}/${st.total} 点已改正 (E(B-V)=${st.ebv != null ? st.ebv.toFixed(4) : '?'})` +
          (st.skipped_band ? `, ${st.skipped_band} 点波段不支持` : ''), 'success');
        render(tid);
      } catch (err) {
        showToast(`银消改正失败: ${err.message}`, 'danger');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-moon-stars"></i> 银消改正'; }
      }
    };

    window.lcGextRun = async (id) => {
      if (!isAdmin()) { showToast('仅管理员可执行银消改正', 'warning'); return; }
      try {
        const st = await runExtinction({ lightcurve_id: id });
        if (st.corrected > 0) {
          showToast('该数据点已完成银消改正', 'success');
        } else {
          showToast('该数据点无法改正（缺坐标 / 波段不支持 / 流量无效）', 'warning');
        }
        render(tid);
      } catch (err) {
        showToast(`银消改正失败: ${err.message}`, 'danger');
      }
    };

    // ── 光变数据行内编辑（管理员任意记录；普通用户仅自己录入的记录，其余用 lcDiscardToggle 扣点） ──
    window.lcDiscardToggle = async (id, cur) => {
      if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
      try {
        await updateLightcurve(id, { discard: !cur });
        showToast(cur ? '已恢复该点' : '已扣点（标记丢弃）', 'success');
        render(tid);
      } catch (err) {
        showToast(`操作失败: ${err.message}`, 'danger');
      }
    };

    // ── 基本信息：研究文章条目增删改 ──
    window.articleAddToggle = () => {
      const f = document.getElementById('articleAddForm');
      const b = document.getElementById('articleAddBtn');
      if (!f) return;
      const show = f.style.display === 'none';
      f.style.display = show ? '' : 'none';
      if (b) b.style.display = show ? 'none' : '';
    };

    window.articleAddSave = async () => {
      const name = document.getElementById('articleAddName').value.trim();
      const title = document.getElementById('articleAddTitle').value.trim();
      const url = document.getElementById('articleAddUrl').value.trim();
      const bibtex = document.getElementById('articleAddBibtex').value.trim();
      if (!name || !url) { showToast('请填写文章简称和链接', 'warning'); return; }
      try {
        await createArticle({ transient_id: tid, name, url, title, bibtex });
        showToast('已添加', 'success');
        render(tid);
      } catch (err) {
        showToast(`添加失败: ${err.message}`, 'danger');
      }
    };

    window.articleEditStart = (id) => {
      const a = articlesData.find(x => x.id === id);
      const el = document.getElementById(`articleItem_${id}`);
      if (!a || !el) return;
      el.innerHTML = `
        <div class="d-flex gap-1 flex-grow-1 mb-1">
          <input type="text" class="form-control form-control-sm" id="articleEditName_${id}" value="${escAttr(a.name)}" placeholder="简称">
          <input type="text" class="form-control form-control-sm" id="articleEditUrl_${id}" value="${escAttr(a.url)}" placeholder="链接">
        </div>
        <input type="text" class="form-control form-control-sm mb-1" id="articleEditTitle_${id}" value="${escAttr(a.title || '')}" placeholder="文章标题（可选）">
        <textarea class="form-control form-control-sm mb-1" id="articleEditBibtex_${id}" rows="3" placeholder="BibTeX 引用信息（可选）">${escAttr(a.bibtex || '')}</textarea>
        <button class="btn btn-sm btn-primary py-0 px-1" onclick="articleEditSave(${id})" title="保存"><i class="bi bi-check-lg"></i></button>
        <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="render(currentTid)" title="取消"><i class="bi bi-x-lg"></i></button>`;
    };

    window.articleEditSave = async (id) => {
      const name = document.getElementById(`articleEditName_${id}`).value.trim();
      const url = document.getElementById(`articleEditUrl_${id}`).value.trim();
      const title = document.getElementById(`articleEditTitle_${id}`).value.trim();
      const bibtex = document.getElementById(`articleEditBibtex_${id}`).value.trim();
      if (!name || !url) { showToast('简称和链接不能为空', 'warning'); return; }
      try {
        await updateArticle(id, { name, url, title, bibtex });
        showToast('已更新', 'success');
        render(tid);
      } catch (err) {
        showToast(`更新失败: ${err.message}`, 'danger');
      }
    };

    // 复制 BibTeX 到剪贴板（http 非安全上下文时回退到 execCommand）
    window.articleBibtexCopy = async (id) => {
      const a = articlesData.find(x => x.id === id);
      if (!a || !a.bibtex) { showToast('该条目没有 BibTeX 信息', 'warning'); return; }
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(a.bibtex);
        } else {
          const ta = document.createElement('textarea');
          ta.value = a.bibtex;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        showToast('BibTeX 已复制到剪贴板', 'success');
      } catch (err) {
        showToast(`复制失败: ${err.message}`, 'danger');
      }
    };

    window.articleDelete = async (id) => {
      if (!confirm('删除该文章条目？')) return;
      try {
        await deleteArticle(id);
        showToast('已删除', 'success');
        render(tid);
      } catch (err) {
        showToast(`删除失败: ${err.message}`, 'danger');
      }
    };

    window.lcEditStart = (id) => {
      const pt = lcItems.find(p => p.id === id);
      if (!pt || !canEditLc(pt)) { showToast('仅管理员或该记录的录入者可编辑', 'warning'); return; }
      const row = document.getElementById(`lcRow_${id}`);
      if (!row) return;
      const cells = row.querySelectorAll('.lc-val');
      let editing = false;
      cells.forEach(cell => {
        const field = cell.dataset.field;
        if (!field) return;
        if (cell.classList.contains('lc-computed')) return; // 计算列（银消星等）不手工编辑
        const val = cell.textContent.trim();
        if (field === 'host_subtracted') {
          cell.innerHTML = `<select class="form-select form-select-sm lc-edit-input" data-field="${field}" style="width:70px">
            <option value="null" ${val === '-' ? 'selected' : ''}>未知</option>
            <option value="false" ${val === 'N' ? 'selected' : ''}>否</option>
            <option value="true" ${val === 'Y' ? 'selected' : ''}>是</option>
          </select>`;
        } else if (['upperlimit', 'gext_corr', 'discard'].includes(field)) {
          cell.innerHTML = `<select class="form-select form-select-sm lc-edit-input" data-field="${field}" style="width:65px">
            <option value="false" ${val === 'N' || val === '-' ? 'selected' : ''}>否</option>
            <option value="true" ${val === 'Y' ? 'selected' : ''}>是</option>
          </select>`;
        } else if (field === 'mag_system') {
          cell.innerHTML = `<select class="form-select form-select-sm lc-edit-input" data-field="mag_system" style="width:80px">
            <option value="AB" ${val === 'AB' ? 'selected' : ''}>AB</option>
            <option value="Vega" ${val === 'Vega' ? 'selected' : ''}>Vega</option>
            <option value="" ${val === '-' ? 'selected' : ''}>-</option>
          </select>`;
        } else {
          cell.innerHTML = `<input type="text" class="form-control form-control-sm lc-edit-input" data-field="${field}" value="${val === '-' ? '' : val}" style="width:90px">`;
        }
        editing = true;
      });
      if (!editing) return;
      // 编辑按钮 → 保存/取消
      const editCell = row.querySelector('.lc-edit-cell');
      if (editCell) {
        editCell.innerHTML = `
          <button class="btn btn-sm btn-primary py-0 px-1" onclick="lcEditSave(${id})" title="保存"><i class="bi bi-check-lg"></i></button>
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="lcEditCancel(${id}, true)" title="取消"><i class="bi bi-x-lg"></i></button>
        `;
      }
    };

    window.lcEditCancel = (id, confirmed) => {
      if (confirmed || confirm('取消编辑？')) {
        render(tid);
      }
    };

    window.lcEditSave = async (id) => {
      const row = document.getElementById(`lcRow_${id}`);
      if (!row) return;
      const inputs = row.querySelectorAll('.lc-edit-input');
      const body = {};
      inputs.forEach(inp => {
        const field = inp.dataset.field;
        let val = inp.value.trim();
        if (field === 'host_subtracted') {
          body[field] = val === 'null' ? null : val === 'true';
        } else if (['upperlimit', 'gext_corr', 'discard'].includes(field)) {
          body[field] = val === 'true';
        } else if (['time', 'time_err', 'flux_density', 'flux_density_err', 'gext_Alambda', 'mag_gextcor', 'mag_gextcor_err', 'flux_density_gextcor', 'flux_density_gextcor_err', 'weights'].includes(field)) {
          body[field] = val ? parseFloat(val) : null;
        } else {
          body[field] = val || null;
        }
      });
      try {
        await updateLightcurve(id, body);
        showToast('已更新', 'success');
        render(tid);
      } catch (err) {
        showToast(`更新失败: ${err.message}`, 'danger');
      }
    };

    // ── 添加新光变记录 ──
    const LC_NEW_EDITABLE = new Set(['time','time_err','band','flux_density','flux_density_err','flux_density_unit','mag_system','gext_corr','upperlimit','host_subtracted','gext_Alambda','mag_gextcor','mag_gextcor_err','flux_density_gextcor','flux_density_gextcor_err','weights','discard','telescope','instrument','reference','comment']);
    window.lcAddNewRow = () => {
      // 检查是否已有新增行
      if (document.getElementById('lcNewRow')) return;
      const tbody = document.querySelector('.table-scroll tbody');
      if (!tbody) return;
      const tr = document.createElement('tr');
      tr.id = 'lcNewRow';
      tr.className = 'row-new';
      tr.innerHTML = '<td class="lc-mark-cell"></td><td class="lc-del-cell"></td>' + LC_COLS.map(([f]) => {
        if (!LC_NEW_EDITABLE.has(f)) {
          // source / updated_at 由后端自动填充
          return `<td data-col="${f}"><span class="text-secondary small">自动</span></td>`;
        }
        let input;
        if (f === 'mag_system') {
          input = `<select class="form-select form-select-sm lc-new-input" data-field="${f}" style="width:80px"><option value="AB">AB</option><option value="Vega">Vega</option><option value="">-</option></select>`;
        } else if (f === 'host_subtracted') {
          input = `<select class="form-select form-select-sm lc-new-input" data-field="${f}" style="width:70px"><option value="false">否</option><option value="true">是</option><option value="null">未知</option></select>`;
        } else if (['gext_corr','upperlimit','discard'].includes(f)) {
          input = `<select class="form-select form-select-sm lc-new-input" data-field="${f}" style="width:65px"><option value="false">否</option><option value="true">是</option></select>`;
        } else {
          input = `<input type="text" class="form-control form-control-sm lc-new-input" data-field="${f}" placeholder="${f}" style="width:90px">`;
        }
        return `<td data-col="${f}">${input}</td>`;
      }).join('');
      tr.innerHTML += `<td class="lc-edit-cell" style="display:table-cell">
        <button class="btn btn-sm btn-success py-0 px-1" onclick="lcAddNewSave()" title="保存"><i class="bi bi-check-lg"></i></button>
        <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="lcAddNewCancel()" title="取消"><i class="bi bi-x-lg"></i></button>
      </td>`;
      tbody.appendChild(tr);
      applyLcColVis();  // 新行同样遵循当前列隐藏状态
    };

    window.lcAddNewCancel = () => {
      const row = document.getElementById('lcNewRow');
      if (row) row.remove();
    };

    window.lcAddNewSave = async () => {
      const inputs = document.querySelectorAll('#lcNewRow .lc-new-input');
      if (!inputs.length) return;
      const body = { transient_id: tid };
      let missing = false;
      inputs.forEach(inp => {
        const field = inp.dataset.field;
        let val = inp.value.trim();
        if (!val && ['time', 'band', 'flux_density', 'flux_density_unit'].includes(field)) {
          missing = true;
          return;
        }
        if (field === 'host_subtracted') {
          body[field] = val === 'null' ? null : val === 'true';
        } else if (['gext_corr', 'upperlimit', 'discard'].includes(field)) {
          body[field] = val === 'true';
        } else if (['time', 'time_err', 'flux_density', 'flux_density_err', 'gext_Alambda', 'mag_gextcor', 'mag_gextcor_err', 'flux_density_gextcor', 'flux_density_gextcor_err', 'weights'].includes(field)) {
          body[field] = val ? parseFloat(val) : null;
        } else {
          body[field] = val || null;
        }
      });
      if (missing) { showToast('请填写 time / band / flux_density', 'warning'); return; }
      try {
        await createLightcurves([body]);
        showToast('已添加', 'success');
        render(tid);
      } catch (err) {
        showToast(`添加失败: ${err.message}`, 'danger');
      }
    };

    // ── derived 卡片编辑 ──
    window.derivedEditToggle = () => {
      if (!isAdmin()) { showToast('仅管理员可编辑数据', 'warning'); return; }
      if (derivedEditMode) {
        // 取消：恢复服务器载入时的原始副本
        derivedDraft = JSON.parse(JSON.stringify(derivedPristine));
      }
      derivedEditMode = !derivedEditMode;
      renderDerivedBody();
      const btn = document.getElementById('derivedEditBtn');
      if (btn) btn.innerHTML = derivedEditMode ? '<i class="bi bi-x-lg"></i> 取消' : '<i class="bi bi-pencil"></i> 编辑';
    };
    window.derivedDel = (scope, cat, key) => {
      if (scope === 'best') {
        if (derivedDraft.best) delete derivedDraft.best[key];
      } else if (scope === 'src' && derivedDraft.sources && derivedDraft.sources[cat]) {
        delete derivedDraft.sources[cat][key];
      } else if (scope === 'grb_type') {
        delete derivedDraft.grb_type;
      }
      renderDerivedBody();
    };
    window.derivedAddQty = (cat) => {
      const key = (document.getElementById(`dq_key_${cat}`)?.value || '').trim();
      const vStr = (document.getElementById(`dq_v_${cat}`)?.value || '').trim();
      const errStr = (document.getElementById(`dq_err_${cat}`)?.value || '').trim();
      if (!key) { showToast('请填写键名', 'warning'); return; }
      const v = parseFloat(vStr);
      if (vStr === '' || !isFinite(v)) { showToast('v 必须是数字', 'warning'); return; }
      const err = parseErrInput(errStr);
      if (err === false) { showToast('err 格式应为 "正" 或 "正,负"', 'warning'); return; }
      if (!derivedDraft.sources) derivedDraft.sources = {};
      if (!derivedDraft.sources[cat]) derivedDraft.sources[cat] = {};
      derivedDraft.sources[cat][key] = { v, err };
      renderDerivedBody();
    };
    window.derivedAddCat = () => {
      const name = (document.getElementById('derivedNewCat')?.value || '').trim();
      if (!name) { showToast('请填写目录名', 'warning'); return; }
      if (!derivedDraft.sources) derivedDraft.sources = {};
      if (derivedDraft.sources[name]) { showToast('该目录已存在', 'warning'); return; }
      derivedDraft.sources[name] = {};
      renderDerivedBody();
    };
    window.derivedSave = async () => {
      // 先从输入框收集并校验（v 必须数字；err 单值或 "正,负"）
      const errMsg = derivedCollectInputs();
      if (errMsg) { showToast(errMsg, 'warning'); return; }
      derivedMarkManual();  // 给手动修改打 manual 标记、记录删除墓碑
      try {
        const resp = await updateTransient(tid, { extra_data: { derived: derivedDraft } });
        showToast('derived 已保存', 'success');
        derivedPristine = JSON.parse(JSON.stringify(resp.extra_data.derived || {}));
        derivedDraft = JSON.parse(JSON.stringify(resp.extra_data.derived || {}));
        derivedEditMode = false;
        renderDerivedBody();
        const btn = document.getElementById('derivedEditBtn');
        if (btn) btn.innerHTML = '<i class="bi bi-pencil"></i> 编辑';
      } catch (err) {
        showToast(`保存失败: ${err.message}（如为 401 请重新登录）`, 'danger');
      }
    };

    // ── 编辑模式状态 ──
    let editActive = false;
    let editData = { ...transient };

    const editBtn = document.getElementById('editBtn');
    // 数据表编辑列（含扣点按钮）登录后显示；删除/编辑本源等仅管理员
    if (isAuthed()) {
      document.getElementById('lcEditHeader').style.display = '';
      document.querySelectorAll('.lc-edit-cell').forEach(el => el.style.display = '');
      const addBtn = document.getElementById('lcAddBtn');
      if (addBtn) addBtn.style.display = 'inline-block';
      const uploadBtn = document.getElementById('lcUploadBtn');
      if (uploadBtn) uploadBtn.style.display = 'inline-block';
      const specUpBtn = document.getElementById('specUploadBtn');
      if (specUpBtn) specUpBtn.style.display = 'inline-block';
    }
    if (isAdmin()) {
      document.getElementById('lcDelHeader').style.display = '';
      document.querySelectorAll('.lc-del-cell').forEach(el => el.style.display = '');
      const delBtn = document.getElementById('lcDelBtn');
      if (delBtn) delBtn.style.display = 'inline-block';
    }
    // 列显示面板：生成勾选框并应用当前列可见性
    buildLcColPanel();
    applyLcColVis();

    // ── 上传数据表（CSV 列映射导入） ──
    window.lcUploadShow = () => {
      if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
      showLcUpload(tid, () => render(tid));
    };

    window.handleEditClick = () => {
      if (!isAdmin()) { showToast('仅管理员可修改事件信息', 'warning'); return; }
      editActive = !editActive;
      const editPanel = document.getElementById('editPanel');
      const metaCard = document.getElementById('metaCard');
      if (!editPanel || !metaCard) return;
      if (editActive) {
        metaCard.style.display = 'none';
        editPanel.style.display = 'block';
        editBtn.innerHTML = '<i class="bi bi-x-lg"></i> 取消';
      } else {
        metaCard.style.display = 'block';
        editPanel.style.display = 'none';
        editBtn.innerHTML = '<i class="bi bi-pencil"></i>';
      }
    };

    // 坐标输入即时解析提示（度 ⇄ 时分秒）
    attachCoordHint(document.getElementById('editRa'), true);
    attachCoordHint(document.getElementById('editDec'), false);

    window.saveDetailEdit = async () => {
      const val = (id) => document.getElementById(id)?.value?.trim() || null;
      const num = (id) => { const v = val(id); return v === null || v === '' ? null : parseFloat(v); };

      const raV = parseRA(val('editRa'));
      if (typeof raV === 'number' && isNaN(raV)) { showToast('RA 格式无法解析（支持十进制度或时分秒，如 08h08m27.4s）', 'danger'); return; }
      const decV = parseDec(val('editDec'));
      if (typeof decV === 'number' && isNaN(decV)) { showToast('Dec 格式无法解析（支持十进制度或时分秒，如 +40d36m44.8s）', 'danger'); return; }

      const body = {
        ra: raV,
        dec: decV,
        t0: val('editT0'),
        redshift: num('editZ'),
        redshift_type: val('editZType') || null,
        redshift_ref: val('editZRef'),
        pos_error: num('editPosErr'),
        pos_ref: val('editPosRef'),
        comment: val('editComment'),
        trigger_instrument: val('editTrigger'),
        tags: (val('editTags') || '').split(',').map(s => s.trim()).filter(Boolean),
        sub_tag: (val('editSubTags') || '').split(',').map(s => s.trim()).filter(Boolean),
        aliases: (val('editAliases') || '').split(',').map(s => s.trim()).filter(Boolean),
      };

      try {
        await updateTransient(tid, body);
        showToast('保存成功', 'success');
        editActive = false;
        render(tid);
      } catch (err) {
        showToast(`保存失败: ${err.message}`, 'danger');
      }
    };

  } catch (err) {
    showError(`加载事件详情失败: ${err.message}`);
  }
}

// ─── 拟合列表 UI ───
function renderFitList() {
  const el = document.getElementById('lcFitList');
  if (!el) return;
  if (lcFits.length === 0) { el.innerHTML = ''; return; }
  el.innerHTML = lcFits.map((fit, i) => {
    const prm = fit.params;
    const ptxt = fit.model === 'pl'
      ? `α=${sig3(prm.alpha)}`
      : fit.model === 'sbpl'
        ? `α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s, n=${sig3(prm.n)}`
        : `α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s`;
    const prange = (fit.pmin !== fit.tmin || fit.pmax !== fit.tmax)
      ? ` · 绘制[${sciFormat(fit.pmin)}, ${sciFormat(fit.pmax)}]s` : '';
    return `<div class="d-flex align-items-center gap-2 mb-1">
      <span style="display:inline-block;width:24px;border-top:2px dashed ${fit.color}"></span>
      <span>${FIT_MODEL_NAMES[fit.model] || fit.model} · ${fit.band} · 拟合[${sciFormat(fit.tmin)}, ${sciFormat(fit.tmax)}]s${prange} · ${ptxt} · N=${fit.N}</span>
      <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="removeLCFit(${i})" title="删除该拟合"><i class="bi bi-trash"></i></button>
    </div>`;
  }).join('');
}

// ─── 外部目录参数渲染 ───
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
  const band = p.band ? ` <small class="text-secondary">[${p.band}]</small>` : '';
  const model = p.model ? ` <small class="text-secondary">${p.model}</small>` : '';
  const frame = p.frame === 'rest' ? ' <small class="text-secondary">静止系</small>' : '';
  const dt = p.dt ? ` <small class="text-secondary">${p.dt}</small>` : '';
  const mod = p.mod ? p.mod : '';
  return `<span class="me-3 d-inline-block" title="${label}">${label} = ${mod}${_sci(p.v)}${_fmtErr(p.err, p.cl)}${unit}${band}${model}${frame}${dt}</span>`;
}

function renderCatalogData(extraData) {
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
      <div class="small fw-bold"><a href="${src.url || '#'}" target="_blank" rel="noopener" class="text-decoration-none">${src.name || cat} <i class="bi bi-box-arrow-up-right" style="font-size:0.7em"></i></a>
      <small class="text-secondary fw-normal"> · 获取于 ${src.retrieved || '-'}</small></div>
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

// ─── 派生物理量 (derived) 卡片 ───
// 常见键名（添加新量时的下拉建议，可自定义）
const DERIVED_KEY_SUGGESTIONS = ['ep_rest', 'eiso', 'eiso_1000', 'lp_iso', 'tlag_rest',
  'variability', 'e_gamma', 't90_rest', 't90_obs', 'epeak_obs', 'alpha', 'spec_class', 'grb_type'];

function renderDerivedCard(extraData) {
  const d = extraData && extraData.derived;
  if (!d || (Object.keys(d.best || {}).length === 0 &&
             Object.keys(d.sources || {}).length === 0 && !d.grb_type)) return '';
  return `
  <div class="row mt-3">
    <div class="col-12">
      <div class="card" id="derivedCard">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span><i class="bi bi-stars"></i> 派生物理量 (derived) <small class="text-secondary">计算于 ${d.computed || '-'}</small></span>
          <button class="btn btn-sm btn-outline-secondary" id="derivedEditBtn" style="display:none" onclick="derivedEditToggle()" title="编辑 derived"><i class="bi bi-pencil"></i> 编辑</button>
        </div>
        <div class="card-body" id="derivedCardBody"></div>
      </div>
    </div>
  </div>`;
}

function _fmtDerivedErrView(err) {
  if (err == null) return '';
  if (Array.isArray(err)) return ` <span class="text-secondary"><sup>+${sciFormat(err[0])}</sup><sub>−${sciFormat(err[1])}</sub></span>`;
  return ` <span class="text-secondary">±${sciFormat(err)}</span>`;
}

// best / sources 条目视图行（条目可能是 {v,err,src} 对象或纯字符串）
function _derivedViewRow(k, p, showSrc) {
  if (p == null) return '';
  if (typeof p !== 'object') {
    return `<tr><td class="text-secondary" style="width:130px">${k}</td><td>${p}</td>${showSrc ? '<td></td>' : ''}</tr>`;
  }
  const badge = p.manual ? ' <span class="badge bg-warning text-dark" title="手动修改：重跑 derive 脚本时保留">手动</span>' : '';
  return `<tr><td class="text-secondary" style="width:130px">${k}</td><td>${sciFormat(p.v)}${_fmtDerivedErrView(p.err)}${badge}</td>${showSrc ? `<td class="small text-secondary">${p.src || ''}</td>` : ''}</tr>`;
}

function derivedViewHTML() {
  const d = derivedDraft || {};
  const parts = [];
  if (d.grb_type && d.grb_type.v != null) {
    const gtBadge = d.grb_type.manual ? ' <span class="badge bg-warning text-dark" title="手动修改：重跑 derive 脚本时保留">手动</span>' : '';
    parts.push(`<div class="mb-2"><strong>GRB 类型:</strong> ${d.grb_type.v}${gtBadge} <small class="text-secondary">(来源: ${d.grb_type.src || '-'})</small></div>`);
  }
  const bestKeys = Object.keys(d.best || {});
  if (bestKeys.length) {
    parts.push(`<div class="small fw-bold text-secondary mb-1">最佳值 (best)</div>
      <table class="table table-sm table-borderless mb-2" style="font-size:0.85rem;width:auto"><tbody>
      ${bestKeys.map(k => _derivedViewRow(k, d.best[k], true)).join('')}
      </tbody></table>`);
  }
  const srcCats = Object.keys(d.sources || {});
  if (srcCats.length) {
    parts.push(`<div class="small fw-bold text-secondary mb-1">分目录 (sources)</div>`);
    parts.push(srcCats.map(cat => `
      <details class="mb-1">
        <summary class="small fw-bold" style="cursor:pointer">${cat}</summary>
        <table class="table table-sm table-borderless mb-1 ms-3" style="font-size:0.82rem;width:auto"><tbody>
          ${Object.keys(d.sources[cat]).map(k => _derivedViewRow(k, d.sources[cat][k], false)).join('') || '<tr><td class="text-secondary small">（空）</td></tr>'}
        </tbody></table>
      </details>`).join(''));
  }
  parts.push(`<div class="small text-secondary mt-2">带「手动」标记的修改（含删除）在重跑 derive_prompt_params.py 时会保留；未标记的条目会被重新计算覆盖</div>`);
  return parts.join('') || '<span class="text-secondary small">（无内容）</span>';
}

// 保存前给手动修改打标：与 pristine 对比，新增/改动的条目标 manual:true，
// pristine 中存在而 draft 中消失的路径记入 _manual_deleted（重跑 derive 时不再复活）
function derivedMarkManual() {
  const pri = derivedPristine || {};
  const dft = derivedDraft;
  if (!dft) return;
  const strip = o => { const c = { ...(o || {}) }; delete c.manual; return JSON.stringify(c); };
  const tomb = new Set(Array.isArray(pri._manual_deleted) ? pri._manual_deleted : []);

  const priBest = pri.best || {}, dftBest = dft.best || {};
  for (const k of Object.keys(priBest)) if (!(k in dftBest)) tomb.add(`best.${k}`);
  for (const [k, obj] of Object.entries(dftBest)) {
    if (obj && typeof obj === 'object') {
      if (!(k in priBest) || strip(priBest[k]) !== strip(obj)) obj.manual = true;
      if (obj.manual) tomb.delete(`best.${k}`);
    }
  }

  const priSrc = pri.sources || {}, dftSrc = dft.sources || {};
  for (const [cat, quants] of Object.entries(priSrc)) {
    for (const k of Object.keys(quants || {})) {
      if (!dftSrc[cat] || !(k in dftSrc[cat])) tomb.add(`sources.${cat}.${k}`);
    }
  }
  for (const [cat, quants] of Object.entries(dftSrc)) {
    for (const [k, obj] of Object.entries(quants || {})) {
      if (obj && typeof obj === 'object') {
        const p = (priSrc[cat] || {})[k];
        if (p === undefined || typeof p !== 'object' || strip(p) !== strip(obj)) obj.manual = true;
        if (obj.manual) tomb.delete(`sources.${cat}.${k}`);
      }
    }
  }

  if (pri.grb_type && !dft.grb_type) tomb.add('grb_type');
  if (dft.grb_type && typeof dft.grb_type === 'object') {
    if (!pri.grb_type || strip(pri.grb_type) !== strip(dft.grb_type)) dft.grb_type.manual = true;
    if (dft.grb_type.manual) tomb.delete('grb_type');
  }

  if (tomb.size) dft._manual_deleted = [...tomb].sort();
  else delete dft._manual_deleted;
}

// err 输入解析：空 → null；单值 → 数字；"正,负" → [正,负]；非法 → false
function parseErrInput(s) {
  if (!s) return null;
  const parts = s.split(',').map(x => parseFloat(x.trim()));
  if (parts.some(x => !isFinite(x)) || parts.length > 2 || parts.length === 0) return false;
  return parts.length === 2 ? [parts[0], parts[1]] : parts[0];
}

function _errToStr(err) {
  if (err == null) return '';
  return Array.isArray(err) ? `${err[0]},${err[1]}` : `${err}`;
}

// 编辑模式条目行（对象条目：v/err[/src] 输入；字符串条目：单个文本输入）
function _derivedEditRow(scope, cat, k, p, showSrc) {
  const catAttr = cat != null ? `data-cat="${escAttr(cat)}"` : '';
  const delBtn = `<button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="derivedDel('${scope}', ${cat != null ? `'${cat}'` : 'null'}, '${k}')" title="删除"><i class="bi bi-trash"></i></button>`;
  if (p != null && typeof p === 'object') {
    return `<tr>
      <td class="text-secondary small">${k}</td>
      <td><input type="text" class="form-control form-control-sm derived-inp" data-scope="${scope}" ${catAttr} data-key="${escAttr(k)}" data-field="v" value="${p.v ?? ''}" style="width:110px" title="v（必须数字）"></td>
      <td><input type="text" class="form-control form-control-sm derived-inp" data-scope="${scope}" ${catAttr} data-key="${escAttr(k)}" data-field="err" value="${escAttr(_errToStr(p.err))}" style="width:110px" title="err：单值或 正,负"></td>
      ${showSrc ? `<td><input type="text" class="form-control form-control-sm derived-inp" data-scope="${scope}" ${catAttr} data-key="${escAttr(k)}" data-field="src" value="${escAttr(p.src)}" style="width:110px" title="来源"></td>` : ''}
      <td>${delBtn}</td>
    </tr>`;
  }
  // 纯字符串条目（如 spec_class）
  return `<tr>
    <td class="text-secondary small">${k}</td>
    <td colspan="${showSrc ? 3 : 2}"><input type="text" class="form-control form-control-sm derived-inp" data-scope="${scope}" ${catAttr} data-key="${escAttr(k)}" data-field="str" value="${escAttr(p)}" style="width:220px"></td>
    <td>${delBtn}</td>
  </tr>`;
}

function derivedEditHTML() {
  const d = derivedDraft || {};
  const parts = [];
  // grb_type
  const gt = d.grb_type || {};
  parts.push(`<div class="d-flex flex-wrap gap-1 align-items-center mb-2 small">
    <span class="text-secondary" style="width:90px">grb_type:</span>
    <input type="text" class="form-control form-control-sm derived-inp" data-scope="grb_type" data-field="v" value="${escAttr(gt.v)}" style="width:70px" title="I / II">
    <input type="text" class="form-control form-control-sm derived-inp" data-scope="grb_type" data-field="src" value="${escAttr(gt.src)}" style="width:140px" title="来源">
    <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="derivedDel('grb_type', null, null)" title="删除 grb_type"><i class="bi bi-trash"></i></button>
  </div>`);
  // best
  parts.push(`<div class="small fw-bold text-secondary mb-1">最佳值 (best)</div>
    <table class="table table-sm table-borderless mb-2" style="width:auto"><tbody>
    ${Object.keys(d.best || {}).map(k => _derivedEditRow('best', null, k, d.best[k], true)).join('') || '<tr><td class="text-secondary small">（空）</td></tr>'}
    </tbody></table>`);
  // sources
  parts.push(`<div class="small fw-bold text-secondary mb-1">分目录 (sources)</div>`);
  parts.push(Object.keys(d.sources || {}).map(cat => `
    <details class="mb-2" open>
      <summary class="small fw-bold" style="cursor:pointer">${cat}</summary>
      <table class="table table-sm table-borderless mb-1 ms-3" style="width:auto"><tbody>
        ${Object.keys(d.sources[cat]).map(k => _derivedEditRow('src', cat, k, d.sources[cat][k], false)).join('') || '<tr><td class="text-secondary small">（空）</td></tr>'}
      </tbody></table>
      <div class="d-flex flex-wrap gap-1 align-items-center ms-3 mb-1 small">
        <input type="text" class="form-control form-control-sm" id="dq_key_${cat}" list="derivedKeyList" placeholder="键名" style="width:120px" title="可下拉选择或自定义">
        <input type="text" class="form-control form-control-sm" id="dq_v_${cat}" placeholder="v" style="width:100px">
        <input type="text" class="form-control form-control-sm" id="dq_err_${cat}" placeholder="err (正,负)" style="width:110px">
        <button class="btn btn-sm btn-outline-primary py-0 px-1" onclick="derivedAddQty('${cat}')" title="给 ${cat} 添加新量"><i class="bi bi-plus-lg"></i></button>
      </div>
    </details>`).join(''));
  // 添加新目录
  parts.push(`<div class="d-flex flex-wrap gap-1 align-items-center small mt-1">
    <input type="text" class="form-control form-control-sm" id="derivedNewCat" placeholder="新目录名" style="width:140px">
    <button class="btn btn-sm btn-outline-primary py-0 px-1" onclick="derivedAddCat()"><i class="bi bi-plus-lg"></i> 添加目录</button>
  </div>
  <datalist id="derivedKeyList">${DERIVED_KEY_SUGGESTIONS.map(k => `<option value="${k}">`).join('')}</datalist>`);
  // 保存 / 取消
  parts.push(`<div class="mt-3 d-flex gap-2">
    <button class="btn btn-sm btn-primary" onclick="derivedSave()"><i class="bi bi-check-lg"></i> 保存 derived</button>
    <button class="btn btn-sm btn-outline-secondary" onclick="derivedEditToggle()">取消</button>
  </div>
  <div class="small text-secondary mt-2">手动修改会在重跑 derive_prompt_params.py 时被覆盖</div>`);
  return parts.join('');
}

function renderDerivedBody() {
  const el = document.getElementById('derivedCardBody');
  if (!el || !derivedDraft) return;
  el.innerHTML = derivedEditMode ? derivedEditHTML() : derivedViewHTML();
}

// 从编辑输入框收集值到 derivedDraft；返回错误消息或 null
function derivedCollectInputs() {
  const inputs = document.querySelectorAll('#derivedCardBody .derived-inp');
  for (const inp of inputs) {
    const { scope, cat, key, field } = inp.dataset;
    const val = inp.value.trim();
    if (scope === 'grb_type') {
      if (val === '' && field === 'v') { delete derivedDraft.grb_type; continue; }
      if (!derivedDraft.grb_type) derivedDraft.grb_type = {};
      derivedDraft.grb_type[field] = val || null;
      continue;
    }
    if (field === 'str') {
      if (scope === 'best') derivedDraft.best[key] = val;
      else if (derivedDraft.sources && derivedDraft.sources[cat]) derivedDraft.sources[cat][key] = val;
      continue;
    }
    const target = scope === 'best'
      ? (derivedDraft.best || {})[key]
      : ((derivedDraft.sources || {})[cat] || {})[key];
    if (target == null || typeof target !== 'object') continue;
    if (field === 'v') {
      const num = parseFloat(val);
      if (val === '' || !isFinite(num)) return `量 ${key} 的 v 必须是数字`;
      target.v = num;
    } else if (field === 'err') {
      const err = parseErrInput(val);
      if (err === false) return `量 ${key} 的 err 格式应为 "正" 或 "正,负"`;
      target.err = err;
    } else if (field === 'src') {
      target.src = val || null;
    }
  }
  return null;
}

// ─── 光谱（光谱数据标签页：单条/多条、绝对/归一化流量、双横轴） ───
let specChartInstance = null;
const specChartHolder = { chart: null };  // 框选缩放用的当前图表引用
// 光谱图手动坐标范围（null = 该端自动；x 为观测者系波长 Å）
let specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
// 光谱图坐标轴类型（linear / logarithmic）
let specAxisType = { x: 'linear', y: 'linear' };
// TNS 风格谱线标记状态：组key → {on, z, v, wl}
let _specMarkings = {};
function resetSpecMarkings() {
  _specMarkings = {};
  for (const g of SPEC_LINE_GROUPS) _specMarkings[g.key] = { on: false, z: _specZ || 0, v: 0, wl: null };
}
const specLinesPlugin = createSpecLinesPlugin(() => _specMarkings);
let _spectraLoadedFor = null;
let _specMode = 'absolute';            // 'absolute' | 'relative'
let _specZ = null;                     // 当前源红移（用于静止系横轴）
const _specSelected = new Set();   // 选中的 spectrum id
const _specCache = new Map();      // id → {meta, objName, sp, pts, errs}
const _specOffsets = {};           // 相对流量模式下每条光谱的纵向偏移
const _specListMeta = new Map();   // id → {flux_type, mjd}
const SPEC_COLORS = ['#79b8ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#56d4dd', '#ff7b72', '#e3b341'];

// 光谱误差条插件（绝对流量模式 + 开关开启时）
let _specShowErr = true;
window.toggleSpecErr = (on) => { _specShowErr = on; renderSpectraPlot(); };

const specErrBarPlugin = {
  id: 'specErrBar',
  afterDatasetsDraw(chart) {
    if (!_specShowErr) return;
    const { ctx, scales: { x: xs, y: ys } } = chart;
    chart.data.datasets.forEach((ds, di) => {
      if (!ds._errs) return;
      const meta = chart.getDatasetMeta(di);
      ctx.save();
      ctx.strokeStyle = ds.borderColor || '#fff';
      ctx.lineWidth = 0.8;
      ds._errs.forEach((e, i) => {
        if (e == null || e <= 0) return;
        const el = meta.data[i];
        if (!el || el.skip || !el.parsed) return;
        const yT = ys.getPixelForValue(el.parsed.y + e);
        const yB = ys.getPixelForValue(el.parsed.y - e);
        if (!isFinite(yT) || !isFinite(yB)) return;
        ctx.beginPath(); ctx.moveTo(el.x, yT); ctx.lineTo(el.x, yB); ctx.stroke();
      });
      ctx.restore();
    });
  },
};

// 静止系波长顶部副轴插件：每帧直接从主横轴刻度换算 λ/(1+z) 绘制，保证严格实时对应
const restAxisPlugin = {
  id: 'restAxis',
  afterDraw(chart) {
    const z = _specZ;
    if (!z || z <= 0) return;
    const x = chart.scales.x;
    if (!x) return;
    const area = chart.chartArea;
    if (!area) return;
    const ctx = chart.ctx;
    const fmt = (v) => {
      const a = Math.abs(v);
      if (a === 0) return '0';
      if (a >= 1e4 || a < 1e-2) return v.toExponential(1);
      return Number(v.toPrecision(4)).toString();
    };
    ctx.save();
    ctx.strokeStyle = '#8b949e';
    ctx.fillStyle = '#8b949e';
    // 轴线
    ctx.beginPath();
    ctx.moveTo(area.left, area.top);
    ctx.lineTo(area.right, area.top);
    ctx.stroke();
    // 刻度（与观测者系主横轴刻度逐点对应）
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    for (const t of x.ticks) {
      const px = x.getPixelForValue(t.value);
      if (px < area.left - 1 || px > area.right + 1) continue;
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.top - 5);
      ctx.stroke();
      ctx.fillText(fmt(t.value / (1 + z)), px, area.top - 7);
    }
    // 标题
    ctx.fillText('静止系波长 (Å)', (area.left + area.right) / 2, area.top - 22);
    ctx.restore();
  },
};

async function initSpectraTab(tid, redshift) {
  if (_spectraLoadedFor === tid) return;
  _spectraLoadedFor = tid;
  _specZ = redshift || null;
  specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  resetSpecMarkings();
  const mkBody = document.getElementById('specMarkingsBody');
  if (mkBody) mkBody.innerHTML = buildMarkingsPanelHTML(_specZ);
  _specSelected.clear();
  _specCache.clear();
  _specListMeta.clear();
  for (const k of Object.keys(_specOffsets)) delete _specOffsets[k];
  const tbody = document.getElementById('spectraListBody');
  try {
    const list = await getSpectra(tid);
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-secondary py-3">暂无光谱数据</td></tr>';
      document.getElementById('specTitle').textContent = '光谱';
      document.getElementById('specMeta').textContent = '';
      return;
    }
    const admin = isAdmin();
    tbody.innerHTML = list.map(s => {
      const ft = (s.extra_data && s.extra_data.flux_type) || 'absolute';
      const mjd = (s.extra_data && s.extra_data.mjd) || (s.observation_date ? s.observation_date.substring(0, 10) : '-');
      const remarks = (s.extra_data && s.extra_data.remarks) || '';
      _specListMeta.set(s.id, { flux_type: ft, mjd });
      return `
      <tr class="row-link" id="specRow_${s.id}" onclick="toggleSpectrum(${s.id})">
        <td><i class="bi bi-check-lg text-primary" id="specChk_${s.id}" style="visibility:hidden"></i> ${mjd}</td>
        <td>${s.instrument || '-'} ${ft === 'normalized' ? '<span class="badge-tag" style="background:rgba(210,153,34,0.15);color:#d29922" title="归一化流量">归一</span>' : ''}</td>
        <td class="small">${(s.extra_data && s.extra_data.observer) || '-'}</td>
        <td class="text-nowrap" onclick="event.stopPropagation()">
          ${remarks ? `<button class="btn btn-sm btn-outline-info py-0 px-1" title="${escAttr(remarks)}" onclick="toggleSpecRemarks(${s.id})"><i class="bi bi-info-circle"></i></button>` : ''}
          <input type="number" class="form-control form-control-sm d-inline-block spec-offset" data-id="${s.id}"
                 style="width:60px;display:${_specMode === 'relative' ? 'inline-block' : 'none'};font-size:0.75rem;padding:1px 4px"
                 step="0.1" value="${_specOffsets[s.id] || 0}" title="纵向偏移（相对流量模式）" onchange="setSpecOffset(${s.id}, this.value)">
          ${admin ? `<button class="btn btn-sm btn-outline-danger py-0 px-1" title="删除该光谱" onclick="deleteSpectrum(${s.id})"><i class="bi bi-trash"></i></button>` : ''}
        </td>
      </tr>${remarks ? `
      <tr id="specRem_${s.id}" style="display:none">
        <td colspan="4" class="small text-secondary" style="white-space:normal"><i class="bi bi-chat-left-text"></i> ${escAttr(remarks)}</td>
      </tr>` : ''}`;
    }).join('');
    toggleSpectrum(list[0].id);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-center text-danger py-3">加载失败: ${err.message}</td></tr>`;
  }
}

window.toggleSpecRemarks = (id) => {
  const el = document.getElementById(`specRem_${id}`);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
};

window.toggleSpectrum = async (id) => {
  if (!_specCache.has(id)) {
    try {
      const resp = await getSpectrum(id);
      const objName = Object.keys(resp.data)[0];
      const sp = resp.data[objName].spectra;
      _specCache.set(id, {
        meta: resp.meta, objName, sp,
        pts: sp.data.map(d => ({ x: Number(d[0]), y: Number(d[1]) }))
          .filter(d => isFinite(d.x) && isFinite(d.y)),
        errs: sp.data.map(d => (d.length > 2 && isFinite(Number(d[2]))) ? Number(d[2]) : null),
      });
    } catch (err) {
      showToast(`加载光谱失败: ${err.message}`, 'danger');
      return;
    }
  }
  if (_specSelected.has(id)) _specSelected.delete(id);
  else _specSelected.add(id);
  document.getElementById(`specChk_${id}`).style.visibility = _specSelected.has(id) ? 'visible' : 'hidden';
  document.getElementById(`specRow_${id}`)?.classList.toggle('table-active', _specSelected.has(id));
  renderSpectraPlot();
};

window.setSpecMode = (mode) => {
  _specMode = mode;
  document.querySelectorAll('.spec-offset').forEach(el => {
    el.style.display = mode === 'relative' ? 'inline-block' : 'none';
  });
  renderSpectraPlot();
};

window.setSpecOffset = (id, val) => {
  _specOffsets[id] = parseFloat(val) || 0;
  renderSpectraPlot();
};

window.deleteSpectrum = async (id) => {
  if (!isAdmin()) { showToast('仅管理员可删除数据', 'warning'); return; }
  const m = _specCache.get(id);
  const name = m ? `${m.objName} ${m.meta.filename}` : `#${id}`;
  if (!confirm(`确认删除光谱 ${name}？（数据库记录与文件一并删除）`)) return;
  try {
    await deleteSpectrumApi(id);
    showToast('已删除', 'success');
    _specSelected.delete(id);
    _specCache.delete(id);
    _specListMeta.delete(id);
    document.getElementById(`specRow_${id}`)?.remove();
    renderSpectraPlot();
    if (!document.querySelector('#spectraListBody tr.row-link')) {
      _spectraLoadedFor = null;
      initSpectraTab(currentTid, _specZ);
    }
  } catch (err) {
    showToast(`删除失败: ${err.message}`, 'danger');
  }
};

function _median(arr) {
  const a = arr.filter(v => isFinite(v)).sort((x, y) => x - y);
  if (!a.length) return 1;
  const m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}

function renderSpectraPlot() {
  const selected = [..._specSelected];
  const titleEl = document.getElementById('specTitle');
  const metaEl = document.getElementById('specMeta');
  const datasets = [];
  let skippedNorm = 0;

  selected.forEach((id, i) => {
    const c = _specCache.get(id);
    if (!c) return;
    const ft = (_specListMeta.get(id) || {}).flux_type || 'absolute';
    const color = SPEC_COLORS[i % SPEC_COLORS.length];
    const label = `${c.objName} · ${c.meta.filename} · MJD ${c.sp.time || '-'}`;
    if (_specMode === 'absolute') {
      if (ft !== 'absolute') { skippedNorm++; return; }
      datasets.push({
        label, data: c.pts, borderColor: color, backgroundColor: color,
        borderWidth: 1.2, pointRadius: 0, tension: 0.15,
        _errs: c.errs.some(e => e != null) ? c.errs : null,
      });
    } else {
      // 相对流量：每条按自身中位数归一 + 用户偏移
      const med = _median(c.pts.map(p => p.y)) || 1;
      const off = _specOffsets[id] || 0;
      datasets.push({
        label: label + (off ? ` (偏移${off >= 0 ? '+' : ''}${off})` : ''),
        data: c.pts.map(p => ({ x: p.x, y: p.y / med + off })),
        borderColor: color, backgroundColor: color,
        borderWidth: 1.2, pointRadius: 0, tension: 0.15,
      });
    }
  });

  if (!selected.length) {
    titleEl.textContent = '光谱（未选择）';
    metaEl.textContent = '';
  } else if (_specMode === 'absolute') {
    titleEl.textContent = selected.length === 1
      ? `${_specCache.get(selected[0]).objName} — ${_specCache.get(selected[0]).meta.filename}`
      : `光谱对比（${datasets.length} 条，绝对流量）`;
    metaEl.textContent = '横轴为观测者系波长' + (_specZ ? `；上横轴为静止系 (z=${_specZ})` : '')
      + (skippedNorm ? `；${skippedNorm} 条归一化光谱未显示` : '');
  } else {
    titleEl.textContent = `光谱对比（${datasets.length} 条，归一化流量）`;
    metaEl.textContent = '横轴为观测者系波长；各谱按自身中位数归一，可用输入框调整纵向偏移';
  }

  const xLabel = '观测者系波长 (Å)';
  const yLabel = _specMode === 'absolute'
    ? (selected.length ? (_specCache.get(selected[0]).sp.u_fluxes || '') : '')
    : '归一化流量';
  plotSpectrum(datasets, xLabel, yLabel, datasets.length > 1 || _specMode === 'relative');
}

function plotSpectrum(datasets, xLabel, yLabel, showLegend) {
  const ctx = document.getElementById('specChart');
  if (!ctx || typeof Chart === 'undefined') return;
  if (specChartInstance) { specChartInstance.destroy(); specChartInstance = null; }
  // 对数 Y 轴：滤除非正流量点（对数轴无法表示），误差数组同步过滤保持索引对齐
  if (specAxisType.y === 'logarithmic') {
    datasets = datasets.map(ds => {
      const keep = ds.data.map((p, i) => (p.y > 0 ? i : -1)).filter(i => i >= 0);
      return {
        ...ds,
        data: keep.map(i => ds.data[i]),
        _errs: ds._errs ? keep.map(i => ds._errs[i]) : ds._errs,
      };
    });
  }
  specChartInstance = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    plugins: [specErrBarPlugin, specLinesPlugin, restAxisPlugin, dragRectPlugin],
    options: {
      responsive: true, maintainAspectRatio: false,
      layout: { padding: { top: 34 } },   // 给顶部静止系副轴留位
      plugins: {
        legend: { display: !!showLegend, position: 'bottom', labels: { color: '#8b949e', boxWidth: 12, font: { size: 10 } } },
        tooltip: { callbacks: { label: c => `${c.dataset.label ? c.dataset.label.split('·')[0].trim() + ' ' : ''}λ=${c.parsed.x.toFixed(1)} Å, F=${c.parsed.y.toExponential(2)}` } },
      },
      scales: {
        x: { type: specAxisType.x, position: 'bottom', title: { display: true, text: xLabel, color: '#8b949e' },
             grid: { color: '#30363d' }, ticks: { color: '#8b949e' },
             min: specAxisRange.xmin ?? undefined, max: specAxisRange.xmax ?? undefined },
        y: { type: specAxisType.y, title: { display: true, text: yLabel, color: '#8b949e' },
             grid: { color: '#30363d' }, ticks: { color: '#8b949e', callback: v => v.toExponential(1) },
             min: specAxisRange.ymin ?? undefined, max: specAxisRange.ymax ?? undefined },
      },
    },
  });
  specChartHolder.chart = specChartInstance;
  attachDragZoom(specChartHolder, ctx, (range) => {
    specAxisRange = range;
    const ids = { xmin: 'specAxXmin', xmax: 'specAxXmax', ymin: 'specAxYmin', ymax: 'specAxYmax' };
    for (const [k, id] of Object.entries(ids)) {
      const el = document.getElementById(id);
      if (el) el.value = sciFormat(range[k]);
    }
    const o = specChartInstance.options.scales;
    o.x.min = range.xmin; o.x.max = range.xmax;
    o.y.min = range.ymin; o.y.max = range.ymax;
    specChartInstance.update('none');
  }, { allowNonPositive: true });
}

// ─── 光谱图坐标轴类型（线性/对数） ───
window.setSpecAxisType = () => {
  const x = document.getElementById('specXTypeSel');
  const y = document.getElementById('specYTypeSel');
  specAxisType = {
    x: x ? x.value : 'linear',
    y: y ? y.value : 'linear',
  };
  renderSpectraPlot();
};

// ─── 光谱图坐标范围（输入框应用 / 恢复默认） ───
window.applySpecAxisRange = () => {
  const v = (id) => {
    const el = document.getElementById(id);
    const n = parseFloat(el && el.value);
    return isFinite(n) ? n : null;
  };
  specAxisRange = { xmin: v('specAxXmin'), xmax: v('specAxXmax'), ymin: v('specAxYmin'), ymax: v('specAxYmax') };
  renderSpectraPlot();
};
window.resetSpecAxisRange = () => {
  specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  ['specAxXmin', 'specAxXmax', 'specAxYmin', 'specAxYmax'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  renderSpectraPlot();
};

// ─── TNS 风格谱线标记面板事件 ───
window.specMarkingToggle = (key, on) => {
  if (!_specMarkings[key]) return;
  _specMarkings[key].on = on;
  if (specChartInstance) specChartInstance.update('none');
};
window.specMarkingSet = (key, field, val) => {
  if (!_specMarkings[key]) return;
  const n = parseFloat(val);
  _specMarkings[key][field] = isFinite(n) ? n : (field === 'wl' ? null : 0);
  if (specChartInstance) specChartInstance.update('none');
};
window.specMarkingStep = (field, val) => {
  const step = parseFloat(val);
  if (!isFinite(step) || step <= 0) return;
  document.querySelectorAll(`#specMarkingsBody .spec-mk-${field}`).forEach(el => { el.step = step; });
};

// ─── 光谱上传 ───
window.showSpecUpload = () => {
  if (!isAuthed()) {
    const modal = new bootstrap.Modal(document.getElementById('loginModal'));
    modal.show();
    return;
  }
  document.getElementById('specUpError').style.display = 'none';
  new bootstrap.Modal(document.getElementById('specUploadModal')).show();
};

window.doSpecUpload = async () => {
  const file = document.getElementById('specFile').files[0];
  const errEl = document.getElementById('specUpError');
  errEl.style.display = 'none';
  if (!file) {
    errEl.textContent = '请选择光谱文件';
    errEl.style.display = 'block';
    return;
  }
  const content = await file.text();
  const btn = document.getElementById('specUpSubmit');
  btn.disabled = true;
  try {
    const resp = await uploadSpectrum({
      transient_id: currentTid,
      filename: file.name,
      content,
      instrument: document.getElementById('specUpInstrument').value.trim() || null,
      mjd: document.getElementById('specUpMjd').value.trim() || null,
      observer: document.getElementById('specUpObserver').value.trim() || null,
      reducer: document.getElementById('specUpReducer').value.trim() || null,
      flux_type: document.getElementById('specUpFluxType').value,
    });
    bootstrap.Modal.getInstance(document.getElementById('specUploadModal')).hide();
    showToast(`光谱已上传: ${resp.filename}（${resp.n_points} 点）`, 'success');
    _spectraLoadedFor = null;   // 强制重载列表
    _specSelected.clear();
    _specCache.clear();
    initSpectraTab(currentTid);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
};

// ─── Aladin Lite ───
function initAladin(ra, dec, id) {
  if (aladinInstance) return;
  const container = document.getElementById('aladinContainer');
  if (!container) return;
  try {
    aladinInstance = A.aladin('#aladinContainer', {
      target: `${ra} ${dec}`,
      fov: 0.1,
      survey: 'P/DSS2/color',
      showReticle: true,
      showSimbadPointer: false,
      showCooGrid: true,
    });
    // Aladin Lite v3：marker 需挂在 catalog 图层上（v2 的 addMarker 已移除）；
    // id 为 null 表示无坐标源（图示为银河系中心占位），不放置源标记
    if (id != null) {
      const cat = A.catalog({ name: id });
      aladinInstance.addCatalog(cat);
      cat.addSources([
        A.marker(ra, dec, { popupTitle: id, popupDesc: `RA=${ra.toFixed(4)}°, Dec=${dec.toFixed(4)}°` })
      ]);
    }
  } catch (err) {
    console.error('Aladin init error:', err);
  }
}

// ─── 光变图初始化 / 重建 ───
function initLCPlot(bands, bandNames, spectralColors) {
  if (lcChartInstance) return;
  if (typeof Chart === 'undefined') {
    console.error('Chart.js not loaded');
    const canvas = document.getElementById('lcChart');
    if (canvas) {
      const cc = chartColors();
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = cc.canvasBg;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = cc.tick;
      ctx.font = '14px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Chart.js 加载失败，请检查网络连接', canvas.width/2, canvas.height/2);
    }
    return;
  }
  buildLCChart(bands, bandNames, spectralColors);
}

function rebuildLCPlot(bands, bandNames, spectralColors) {
  if (lcChartInstance) {
    lcChartInstance.destroy();
    lcChartInstance = null;
  }
  buildLCChart(bands, bandNames, spectralColors);
}

function buildLCChart(bands, bandNames, spectralColors) {
  const ctx = document.getElementById('lcChart');
  if (!ctx) return;
  if (typeof Chart === 'undefined') {
    console.error('Chart.js not loaded, cannot build chart');
    return;
  }

  const cc = chartColors();
  const fonts = academicFonts();
  const xType = document.getElementById('xScale')?.value || 'logarithmic';
  const useGext = document.getElementById('gextMode')?.value === 'gext';
  // 绝对星等模式（需距离模数）：y = m_AB − μ，线性反向轴
  const yMode = document.getElementById('yMode')?.value || 'flux';
  const absMag = yMode === 'absmag' && _lcDistmod != null;
  // 静止系：t/(1+z)（需红移）
  const restFrame = (document.getElementById('lcRestFrame')?.checked || false) && _lcRedshift != null;
  const zfac = restFrame ? (1 + _lcRedshift) : 1;
  // 顶部副轴
  const topMode = document.getElementById('topAxis')?.value || 'day';
  _lcTopAxis = (topMode === 'none' || (topMode === 'mjd' && _lcT0MJD == null))
    ? null
    : { mode: topMode, t0mjd: _lcT0MJD };

  // 数据默认以 mJy 绘制（左轴 log mJy；右轴由 y2 换算显示 AB 星等）；
  // 绝对星等模式下 y 为 M（mag，线性反向轴）
  const toY = (mJy) => absMag ? mJyToMagAB(mJy) - _lcDistmod : mJy;
  const toYerr = (mJy, err) => {
    if (err == null) return null;
    // 星等空间误差 σ_m = (2.5/ln10)·σ_F/F
    return absMag ? (mJy > 0 ? (2.5 / Math.LN10) * err / mJy : null) : err;
  };
  const datasets = [];

  // 按频率排序波段（与其他图表同一规则，见 js/bands.js）
  const sortedBands = sortBandsByFreq(bandNames);

  for (const band of sortedBands) {
    const pts = bands[band];
    const color = spectralColors[band] || '#58a6ff';
    const detections = pts.filter(p => !p.upperlimit);
    const upperLimits = pts.filter(p => p.upperlimit);

    // ── 探测点（带误差条） ──
    if (detections.length > 0) {
      const vals = detections.map(p => {
        const m = pointToMJy(p, useGext);
        if (!m) return null;
        return { x: p.time / zfac, y: toY(m.y), err: toYerr(m.y, m.err), raw: p };
      }).filter(d => d && isFinite(d.x) && isFinite(d.y));

      if (vals.length > 0) {
        datasets.push({
          label: band,
          data: vals.map(d => ({ x: d.x, y: d.y })),
          backgroundColor: color,
          borderColor: color,
          pointBackgroundColor: color,
          pointBorderColor: color,
          showLine: false,
          pointRadius: 3,
          pointHoverRadius: 5,
          _errorValues: vals.map(d => d.err),
          _isUpperLimit: false,
          _band: band,
        });
      }
    }

    // ── 上限点 ──
    if (upperLimits.length > 0) {
      const uv = upperLimits.map(p => {
        const m = pointToMJy(p, useGext);
        if (!m) return null;
        return { x: p.time / zfac, y: toY(m.y), raw: p };
      }).filter(d => d && isFinite(d.x) && isFinite(d.y));

      if (uv.length > 0) {
        datasets.push({
          label: `${band} ↑`,
          data: uv.map(d => ({ x: d.x, y: d.y })),
          backgroundColor: color,
          borderColor: color,
          pointBackgroundColor: color,
          pointBorderColor: color,
          showLine: false,
          pointStyle: 'triangle',
          pointRadius: 5,
          pointRotation: 180,
          _errorValues: [],
          _isUpperLimit: true,
          _band: band,
        });
      }
    }
  }

  // ── 叠加拟合曲线（虚线，按绘制范围 ~120 点；order 保证画在最上层） ──
  for (const fit of lcFits) {
    const t0 = fit.pmin ?? fit.tmin, t1 = fit.pmax ?? fit.tmax;
    if (!(t0 > 0) || !(t1 > t0)) continue;
    const data = [];
    for (let i = 0; i < 120; i++) {
      const tt = t0 * Math.pow(t1 / t0, i / 119);
      const f = fitModelFlux(fit.model, fit.params, tt);
      if (!(f > 0) || !isFinite(f)) continue;
      data.push({ x: tt / zfac, y: toY(f) });
    }
    datasets.push({
      type: 'line',
      label: fit.label,
      data,
      borderColor: fit.color,
      backgroundColor: fit.color,
      borderDash: [6, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      pointHoverRadius: 0,
      fill: false,
      showLine: true,
      order: -1,   // Chart.js：order 越小越晚绘制（显示在最上层）
      _isFit: true,
    });
  }

  // ── 范围计算函数（供 afterDataLimits 使用；手动范围优先） ──
  function computeAxisRange(chart, mode) {
    const allX = [], allY = [];
    chart.data.datasets.forEach((ds, i) => {
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden) return;
      ds.data.forEach(p => {
        if (p.x > 0 && isFinite(p.x)) allX.push(p.x);
        // 绝对星等模式 y 可为负（线性轴）；流量模式仅取正值（log 轴）
        if (absMag ? isFinite(p.y) : (isFinite(p.y) && p.y > 0)) allY.push(p.y);
      });
    });
    if (mode === 'x') {
      let r;
      if (allX.length === 0) {
        r = { min: 0.1, max: 1000 };
      } else {
        const mn = Math.min(...allX), mx = Math.max(...allX);
        if (xType === 'logarithmic') {
          r = { min: mn * 0.8, max: mx * 1.5 };
        } else {
          const pad = (mx - mn) * 0.1;
          r = { min: Math.max(0, mn - pad), max: mx + pad };
        }
      }
      // 手动范围覆盖（留空端自动）
      if (lcAxisRange.xmin != null) r.min = lcAxisRange.xmin;
      if (lcAxisRange.xmax != null) r.max = lcAxisRange.xmax;
      if (xType === 'logarithmic' && r.min <= 0) r.min = 0.1; // log 轴防 log(0)
      return r;
    } else {
      let r;
      if (allY.length === 0) {
        r = absMag ? { min: -30, max: -10 } : { min: 1e-6, max: 1 };
      } else if (absMag) {
        const mn = Math.min(...allY), mx = Math.max(...allY);
        const pad = Math.max(0.3, (mx - mn) * 0.08);
        r = { min: mn - pad, max: mx + pad };
      } else {
        const mn = Math.min(...allY), mx = Math.max(...allY);
        r = { min: Math.max(1e-13, mn * 0.5), max: mx * 2 };
      }
      if (lcAxisRange.ymin != null) r.min = lcAxisRange.ymin;
      if (lcAxisRange.ymax != null) r.max = lcAxisRange.ymax;
      if (!absMag && r.min <= 0) r.min = 1e-6; // log 轴最小正数约束
      return r;
    }
  }

  try {
    lcChartInstance = new Chart(ctx, {
    type: 'scatter',
    data: { datasets },
    plugins: [errorBarPlugin, lcTopAxisPlugin, dragRectPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: _lcTopAxis ? { padding: { top: 38 } } : undefined,
      interaction: { mode: 'nearest', intersect: true },
      plugins: {
        legend: { display: false },  // 波段开关由下方勾选面板承担
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          callbacks: {
            label: (ctx) => {
              const p = ctx.parsed;
              const ds = ctx.dataset;
              if (ds._isFit) return ds.label;
              const err = ds._errorValues ? ds._errorValues[ctx.dataIndex] : null;
              const tTxt = restFrame ? `t/(1+z)=${sciFormat(p.x)}s` : `t=${sciFormat(p.x)}s`;
              let txt = absMag
                ? `${tTxt}, M=${p.y.toFixed(2)}`
                : `${tTxt}, ${sciFormat(p.y)} mJy (AB ${mJyToMagAB(p.y).toFixed(2)})`;
              if (err != null && err > 0) txt += ` ±${absMag ? err.toFixed(2) : sciFormat(err)}`;
              return txt;
            },
          },
        },
      },
      scales: {
        x: {
          type: xType,
          reverse: false,
          title: { display: true, text: restFrame ? 't/(1+z)  (s)' : 'time since T0  (s)', color: cc.tick, font: fonts.title },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: { color: cc.tick, font: fonts.tick, callback: (v) => sciFormat(v) },
          afterDataLimits(scale) {
            const r = computeAxisRange(scale.chart, 'x');
            scale.min = r.min;
            scale.max = r.max;
          },
        },
        // 左纵轴：流量密度 (mJy, log)；绝对星等模式为线性反向星等轴
        y: {
          type: absMag ? 'linear' : 'logarithmic',
          reverse: absMag,
          title: {
            display: true,
            text: absMag ? '绝对星等 M (AB)' : '流量密度 (mJy)' + (useGext ? ' · 银消改正' : ''),
            color: cc.tick, font: fonts.title,
          },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: {
            color: cc.tick,
            font: fonts.tick,
            callback: (v) => absMag ? Number(v).toFixed(1) : sciFormat(v),
          },
          afterDataLimits(scale) {
            const r = computeAxisRange(scale.chart, 'y');
            scale.min = r.min;
            scale.max = r.max;
          },
        },
        // 右纵轴：AB 星等，与左轴 mJy 物理对应（m = 16.4 − 2.5·log10(F_mJy)）
        // 数值方向与流量相反（顶部为小星等）由同一 log 映射自动保证；绝对星等模式下隐藏
        y2: {
          display: !absMag,
          type: 'logarithmic',
          position: 'right',
          title: { display: true, text: '星等 (AB)', color: cc.tick, font: fonts.title },
          grid: { drawOnChartArea: false },
          border: { color: cc.tick },
          ticks: {
            color: cc.tick,
            font: fonts.tick,
            callback: (v) => mJyToMagAB(v).toFixed(1),
          },
          afterDataLimits(scale) {
            // 与左轴同步 min/max
            const ys = scale.chart.scales.y;
            if (ys) { scale.min = ys.min; scale.max = ys.max; }
          },
        },
      },
    },
  });
    lcChartHolder.chart = lcChartInstance;
    buildBandPanel(sortedBands, spectralColors);
    applyBandVisibility();
    attachDragZoom(lcChartHolder, ctx, (range) => {
      lcAxisRange = range;
      const ids = { xmin: 'axXmin', xmax: 'axXmax', ymin: 'axYmin', ymax: 'axYmax' };
      for (const [k, id] of Object.entries(ids)) {
        const el = document.getElementById(id);
        if (el) el.value = sciFormat(range[k]);
      }
      lcChartInstance.update('none');
    });
  } catch (err) {
    console.error('Chart creation error:', err);
    const c = ctx.getContext('2d');
    c.fillStyle = cc.canvasBg;
    c.fillRect(0, 0, ctx.width, ctx.height);
    c.fillStyle = '#f85149';
    c.font = '14px sans-serif';
    c.textAlign = 'center';
    c.fillText('光变图渲染失败: ' + err.message, ctx.width/2, ctx.height/2);
  }
}
