// === 光变数据表功能区（详情页「数据表」标签） ===
// 行渲染/排序/行标记/列显隐/行内编辑/新增记录/扣点/批量删除/单点银消改正/CSV 上传。
// window.* 全局入口为内联 onclick 契约，模块加载时一次性注册。
import {
  showToast, isAuthed, isAdmin, currentUser,
  updateLightcurve, createLightcurves, deleteLightcurve, runExtinction,
} from '../api.js';
import { showLcUpload } from './lc_upload.js';
import { esc, escAttr, fmtNum } from '../utils.js';
import { getFilterIdsSorted } from '../bands.js';
import { render, currentTid } from './detail.js';
import { syncLcPoint } from './detail_lcchart.js';

// ─── 数据表列显示（lcColVis[key] 显式记录用户选择；未记录的按默认子集），localStorage 持久化 ───
export const LC_COLS = [
  ['time', '时间(s)'], ['mjd', 'MJD'], ['time_err', '时间误差'], ['band', '波段'], ['flux_density', '流量/星等'],
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

// ─── 数据表状态 ───
let lcItems = [];
let lcSortState = { key: 'time', dir: 1 };
// ─── 数据表行标记（勾选高亮定位用，仅前端状态，不写库） ───
let lcMarked = new Set();
let lcMarkedTid = null;

// render() 获取光变数据后调用：登记条目、重置排序；切换源时清空行标记
export function setLCItems(items, tid) {
  if (lcMarkedTid !== tid) { lcMarked = new Set(); lcMarkedTid = tid; }
  lcItems = items || [];
  lcSortState = { key: 'time', dir: 1 };
}

// ─── 数据表：单行 HTML（排序重绘与首次渲染共用） ───
// 行内编辑权限：管理员可编辑任意记录；普通用户仅可编辑自己录入的记录（source = 本账户），
// 其余记录只能扣点（与后端 PUT /api/lightcurves/<id> 权限一致）
function canEditLc(pt) {
  if (isAdmin()) return true;
  const u = currentUser();
  return isAuthed() && pt.source && u.username === pt.source;
}

export function lcRowHTML(pt) {
  const marked = lcMarked.has(pt.id);
  return `
    <tr id="lcRow_${pt.id}"${marked ? ' class="lc-row-mark"' : ''}>
      <td class="lc-mark-cell"><input type="checkbox" class="lc-mark-chk" data-id="${pt.id}" ${marked ? 'checked ' : ''}onchange="lcMarkRow(${pt.id}, this.checked)" title="标记该行（整行高亮；管理员可批量删除勾选项）"></td>
      <td class="lc-val" data-field="time" data-col="time">${fmtNum(pt.time, 1)}</td>
      <td class="lc-val" data-field="mjd" data-col="mjd">${fmtNum(pt.mjd, 5)}</td>
      <td class="lc-val" data-field="time_err" data-col="time_err">${fmtNum(pt.time_err, 1)}</td>
      <td class="lc-val" data-field="band" data-col="band">${esc(pt.band)}</td>
      <td class="lc-val" data-field="flux_density" data-col="flux_density">${fmtNum(pt.flux_density, 3)}</td>
      <td class="lc-val" data-field="flux_density_err" data-col="flux_density_err">${fmtNum(pt.flux_density_err, 3)}</td>
      <td class="lc-val" data-field="flux_density_unit" data-col="flux_density_unit">${esc(pt.flux_density_unit)}</td>
      <td class="lc-val" data-field="mag_system" data-col="mag_system">${esc(pt.mag_system) || '-'}</td>
      <td class="lc-val" data-field="gext_corr" data-col="gext_corr">${pt.gext_corr ? 'Y' : 'N'}</td>
      <td class="lc-val" data-field="upperlimit" data-col="upperlimit">${pt.upperlimit ? 'Y' : '-'}</td>
      <td class="lc-val" data-field="host_subtracted" data-col="host_subtracted" title="是否已扣除宿主星系流量">${pt.host_subtracted == null ? '-' : (pt.host_subtracted ? 'Y' : 'N')}</td>
      <td class="lc-val" data-field="gext_Alambda" data-col="gext_Alambda">${fmtNum(pt.gext_Alambda, 4)}</td>
      <td class="lc-val lc-computed" data-field="mag_gextcor" data-col="mag_gextcor">${fmtNum(pt.mag_gextcor, 3)}</td>
      <td class="lc-val lc-computed" data-field="mag_gextcor_err" data-col="mag_gextcor_err">${fmtNum(pt.mag_gextcor_err, 3)}</td>
      <td class="lc-val" data-field="flux_density_gextcor" data-col="flux_density_gextcor">${fmtNum(pt.flux_density_gextcor, 4)}</td>
      <td class="lc-val" data-field="flux_density_gextcor_err" data-col="flux_density_gextcor_err">${fmtNum(pt.flux_density_gextcor_err, 4)}</td>
      <td class="lc-val" data-field="weights" data-col="weights">${pt.weights != null ? fmtNum(pt.weights, 2) : '1.00'}</td>
      <td class="lc-val" data-field="discard" data-col="discard">${pt.discard ? 'Y' : 'N'}</td>
      <td class="lc-val" data-field="telescope" data-col="telescope" title="${escAttr(pt.telescope)}">${esc(pt.telescope) || '-'}</td>
      <td class="lc-val" data-field="instrument" data-col="instrument" title="${escAttr(pt.instrument)}">${esc(pt.instrument) || '-'}</td>
      <td class="small lc-val lc-ref" data-field="reference" data-col="reference" title="${escAttr(pt.reference)}">${esc(pt.reference) || '-'}</td>
      <td class="small lc-val lc-comment" data-field="comment" data-col="comment" title="${escAttr(pt.comment)}">${esc(pt.comment) || '-'}</td>
      <td class="small" data-col="source" title="数据来源">${pt.source ? escAttr(pt.source) : '-'}</td>
      <td class="small text-secondary text-nowrap" data-col="updated_at" title="存入时间 / 最近修改 (UTC)">${esc((pt.updated_at || pt.created_at || '').replace('T', ' ').slice(0, 19)) || '-'}</td>
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
  // 编辑列（含扣点）登录可见
  if (isAuthed()) {
    document.querySelectorAll('.lc-edit-cell').forEach(el => el.style.display = '');
  }
  // 标记状态在重绘后保留，表头全选框同步为实际状态
  const markAll = document.getElementById('lcMarkAll');
  if (markAll) markAll.checked = !!(lcItems.length && lcItems.every(p => lcMarked.has(p.id)));
};

// ─── 单点更新后的局部行替换：PUT 返回更新后的完整行，就地更新内存与 DOM，不整页重载 ───
// 注意：若当前已排序，被改行的位置不随新值重排（下次点表头排序时按新值归位）
function applyLcRowUpdate(id, pt) {
  const idx = lcItems.findIndex(p => p.id === id);
  const old = idx >= 0 ? lcItems[idx] : null;
  if (idx >= 0) lcItems[idx] = pt;
  const row = document.getElementById(`lcRow_${id}`);
  if (row) {
    row.outerHTML = lcRowHTML(pt);
    applyLcColVis();  // 恢复列隐藏状态
    if (isAuthed()) {  // 编辑列（含扣点）登录可见
      const cell = document.querySelector(`#lcRow_${id} .lc-edit-cell`);
      if (cell) cell.style.display = '';
    }
  }
  // 同步光变图/拟合数据源（取消编辑时 old === pt，无实际变化，跳过）；
  // 无法局部同步的情形（如波段被改成新波段）回退整页渲染
  if (old && old !== pt && !syncLcPoint(old, pt)) render(currentTid);
}

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
export function applyLcColVis() {
  for (const [k] of LC_COLS) {
    const show = lcColShown(k);
    document.querySelectorAll(`#tab-data [data-col="${k}"]`)
      .forEach(el => { el.style.display = show ? '' : 'none'; });
  }
}

export function buildLcColPanel() {
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

// ─── 数据表批量删除（复用行标记勾选，管理员） ───
window.lcDeleteSelected = async () => {
  if (!isAdmin()) { showToast('仅管理员可删除数据', 'warning'); return; }
  const ids = lcItems.filter(p => lcMarked.has(p.id)).map(p => p.id);
  if (!ids.length) { showToast('请先勾选要删除的记录', 'warning'); return; }
  if (!confirm(`确定删除勾选的 ${ids.length} 条光变记录？此操作不可恢复`)) return;
  const results = await Promise.allSettled(ids.map(id => deleteLightcurve(id)));
  let ok = 0, fail = 0;
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') { lcMarked.delete(ids[i]); ok++; }
    else fail++;
  });
  if (fail) showToast(`已删除 ${ok} 条，${fail} 条删除失败`, 'warning');
  else showToast(`已删除 ${ok} 条记录`, 'success');
  render(currentTid);
};

// ─── 光变数据行内编辑（管理员任意记录；普通用户仅自己录入的记录，其余用 lcDiscardToggle 扣点） ───
window.lcDiscardToggle = async (id, cur) => {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  try {
    const updated = await updateLightcurve(id, { discard: !cur });
    showToast(cur ? '已恢复该点' : '已扣点（标记丢弃）', 'success');
    applyLcRowUpdate(id, updated);
  } catch (err) {
    showToast(`操作失败: ${err.message}`, 'danger');
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
      cell.innerHTML = `<input type="text" class="form-control form-control-sm lc-edit-input" data-field="${field}" value="${val === '-' ? '' : escAttr(val)}" style="width:90px">` +
        (field === 'mjd' ? `<div class="small text-secondary text-nowrap">MJD 与相对秒数可互算，填其一即可</div>` : '');
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
  if (!(confirmed || confirm('取消编辑？'))) return;
  const pt = lcItems.find(p => p.id === id);
  if (pt) applyLcRowUpdate(id, pt);  // 内存里仍是未修改的原值，就地还原
  else render(currentTid);
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
    } else if (['time', 'mjd', 'time_err', 'flux_density', 'flux_density_err', 'gext_Alambda', 'mag_gextcor', 'mag_gextcor_err', 'flux_density_gextcor', 'flux_density_gextcor_err', 'weights'].includes(field)) {
      body[field] = val ? parseFloat(val) : null;
    } else {
      body[field] = val || null;
    }
  });
  try {
    const updated = await updateLightcurve(id, body);
    showToast('已更新', 'success');
    applyLcRowUpdate(id, updated);
  } catch (err) {
    showToast(`更新失败: ${err.message}`, 'danger');
  }
};

// ─── 添加新光变记录 ───
const LC_NEW_EDITABLE = new Set(['time','mjd','time_err','band','flux_density','flux_density_err','flux_density_unit','mag_system','gext_corr','upperlimit','host_subtracted','gext_Alambda','mag_gextcor','mag_gextcor_err','flux_density_gextcor','flux_density_gextcor_err','weights','discard','telescope','instrument','reference','comment']);
// 单位下拉候选（对齐 bands.js toMJy/pointToMJy 支持的单位；magnitude 配合 mag_system 列）
const LC_NEW_UNITS = ['mJy', 'uJy', 'Jy', 'cgs(erg/cm2/s/Hz)', 'erg/cm2/s/keV', 'magnitude'];
// 时间/时间误差乘积因子（提交时前端乘好以秒入库，time_unit 保持 's'）
const LC_NEW_TFACS = [[1, '秒 x1'], [60, '分 x60'], [3600, '小时 x3600'], [86400, '天 x86400']];
window.lcAddNewRow = () => {
  // 检查是否已有新增行
  if (document.getElementById('lcNewRow')) return;
  const tbody = document.getElementById('lcTableBody');
  if (!tbody) return;
  // 波段候选：滤光片 id（按波长排序）+ 本源已有波段名去重
  const bandCands = [...new Set([...getFilterIdsSorted(), ...lcItems.map(p => p.band).filter(Boolean)])];
  const tfacSelect = (field) =>
    `<select class="form-select form-select-sm lc-new-tfac" data-for="${field}" style="width:82px" title="输入值乘以此因子后以秒入库">` +
    LC_NEW_TFACS.map(([v, label]) => `<option value="${v}" ${v === 1 ? 'selected' : ''}>${label}</option>`).join('') +
    `</select>`;
  const tr = document.createElement('tr');
  tr.id = 'lcNewRow';
  tr.className = 'row-new';
  tr.innerHTML = '<td class="lc-mark-cell"></td>' + LC_COLS.map(([f]) => {
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
    } else if (f === 'band') {
      input = `<input type="text" class="form-control form-control-sm lc-new-input" data-field="${f}" list="lcNewBandList" placeholder="band" style="width:100px">` +
        `<datalist id="lcNewBandList">${bandCands.map(b => `<option value="${escAttr(b)}"></option>`).join('')}</datalist>`;
    } else if (f === 'flux_density_unit') {
      input = `<select class="form-select form-select-sm lc-new-input" data-field="${f}" style="width:130px">` +
        LC_NEW_UNITS.map(u => `<option value="${escAttr(u)}" ${u === 'mJy' ? 'selected' : ''}>${esc(u)}</option>`).join('') +
        `</select>`;
    } else if (f === 'time' || f === 'time_err') {
      input = `<div class="d-flex gap-1 align-items-center"><input type="text" class="form-control form-control-sm lc-new-input" data-field="${f}" placeholder="${f}" style="width:80px">${tfacSelect(f)}</div>`;
    } else if (f === 'mjd') {
      input = `<input type="text" class="form-control form-control-sm lc-new-input" data-field="${f}" placeholder="MJD" style="width:110px">` +
        `<div class="small text-secondary text-nowrap">MJD 与相对秒数可互算，填其一即可</div>`;
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
  // 只滚动表格容器到底部露出新行，不滚动页面；等下一帧布局稳定后再滚
  requestAnimationFrame(() => {
    const sc = tr.closest('.table-scroll');
    if (sc) sc.scrollTop = sc.scrollHeight;
  });
};

window.lcAddNewCancel = () => {
  const row = document.getElementById('lcNewRow');
  if (row) row.remove();
};

window.lcAddNewSave = async () => {
  const inputs = document.querySelectorAll('#lcNewRow .lc-new-input');
  if (!inputs.length) return;
  const body = { transient_id: currentTid };
  let missing = false;
  inputs.forEach(inp => {
    const field = inp.dataset.field;
    let val = inp.value.trim();
    if (!val && ['band', 'flux_density', 'flux_density_unit'].includes(field)) {
      missing = true;
      return;
    }
    if (field === 'host_subtracted') {
      body[field] = val === 'null' ? null : val === 'true';
    } else if (['gext_corr', 'upperlimit', 'discard'].includes(field)) {
      body[field] = val === 'true';
    } else if (['time', 'time_err'].includes(field)) {
      // 乘积因子换算：输入值 × 因子后以秒入库（time_unit 保持 's'）
      const facEl = inp.closest('td')?.querySelector('.lc-new-tfac');
      const fac = facEl ? parseFloat(facEl.value) : 1;
      body[field] = val ? parseFloat(val) * (isFinite(fac) ? fac : 1) : null;
    } else if (['mjd', 'flux_density', 'flux_density_err', 'gext_Alambda', 'mag_gextcor', 'mag_gextcor_err', 'flux_density_gextcor', 'flux_density_gextcor_err', 'weights'].includes(field)) {
      body[field] = val ? parseFloat(val) : null;
    } else {
      body[field] = val || null;
    }
  });
  if (missing) { showToast('请填写 band / flux_density', 'warning'); return; }
  // time 与 MJD 至少填一个；两者都给了原样提交，服务端负责互算
  if (body.time == null && body.mjd == null) {
    showToast('time 与 MJD 至少填一个', 'warning');
    return;
  }
  try {
    await createLightcurves([body]);
    showToast('已添加', 'success');
    render(currentTid);
  } catch (err) {
    showToast(`添加失败: ${err.message}`, 'danger');
  }
};

// ─── 单点银消改正（管理员；全源改正在 detail_overview.js 的 runGextSource） ───
window.lcGextRun = async (id) => {
  if (!isAdmin()) { showToast('仅管理员可执行银消改正', 'warning'); return; }
  try {
    const st = await runExtinction({ lightcurve_id: id });
    const skipped = st.stats?.skipped_not_optical || 0;
    const extra = (st.note ? `；${st.note}` : '') +
      (skipped > 0 ? `；跳过非光学波段 ${skipped} 点` : '');
    if (st.corrected > 0) {
      showToast('该数据点已完成银消改正' + extra, 'success');
    } else {
      showToast('该数据点无法改正（缺坐标 / 波段不支持 / 流量无效）' + extra, 'warning');
    }
    render(currentTid);
  } catch (err) {
    showToast(`银消改正失败: ${err.message}`, 'danger');
  }
};

// ─── 上传数据表（CSV 列映射导入） ───
window.lcUploadShow = () => {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  showLcUpload(currentTid, () => render(currentTid));
};
