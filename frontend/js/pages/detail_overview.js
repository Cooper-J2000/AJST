// === 概览标签功能区（详情页内嵌） ===
// 研究文章条目增删改、基本信息编辑面板、宿主星系摘要行、本源银消改正。
// window.* 全局入口为内联 onclick 契约，模块加载时一次性注册（与每轮 render 重挂等价：
// 这些 handler 原本也只依赖 currentTid 与模块状态，不捕获 render 的局部变量）。
import {
  showToast, isAuthed, isAdmin, updateTransient, runExtinction,
  createArticle, updateArticle, deleteArticle, getHost, exportLightcurves,
} from '../api.js';
import { parseRA, parseDec, attachCoordHint } from '../coords.js';
import { attachChipInput, ensureTagsRegistered } from '../taginput.js';
import { esc, escAttr, safeUrl, sig3 } from '../utils.js';
import { render, currentTid } from './detail.js';

// ─── 模块状态 ───
let articlesData = [];       // 当前源的研究文章条目列表
let _transient = null;       // 当前源完整记录（编辑面板回填/银消改正坐标检查用）
let _editActive = false;     // 编辑面板开关状态
let _editTagsChip = null;    // 编辑面板主标签 chip 输入（taginput.js attachChipInput 句柄）
let _editSubTagsChip = null; // 编辑面板副标签 chip 输入

// render() 获取数据后调用：登记当前源与文章列表，重置编辑面板状态
export function initOverview(transient, articles) {
  _transient = transient;
  articlesData = articles || [];
  _editActive = false;
  _editTagsChip = null;
  _editSubTagsChip = null;
}

// 坐标输入即时解析提示（度 ⇄ 时分秒）；模板插入后每轮 render 调用
export function attachEditCoordHints() {
  attachCoordHint(document.getElementById('editRa'), true);
  attachCoordHint(document.getElementById('editDec'), false);
}

// 主/副标签 chip 泡泡输入（含 datalist 自动补全）；模板插入后每轮 render 调用
export function attachEditTagInputs() {
  _editTagsChip = attachChipInput(document.getElementById('editTags'), 'main', _transient?.tags || []);
  _editSubTagsChip = attachChipInput(document.getElementById('editSubTags'), 'sub', _transient?.sub_tag || []);
}

// ─── 基本信息：相关研究文章条目（简称 + 标题 + 链接 + BibTeX，可多条） ───
// 条目以整数 id 定位（同一作者同年可有多篇，简称不唯一）
// 权限：登录用户可添加；修改/删除仅管理员（与后端 /api/articles 一致）
export function articlesHTML() {
  const COLLAPSE_N = 3;  // 超过该条数时折叠其余，点「展开全部」显示
  const itemHTML = a => `
    <div class="mb-1" id="articleItem_${a.id}">
      <div class="d-flex align-items-center gap-2 flex-wrap">
        <a href="${escAttr(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer" style="overflow-wrap:anywhere"><i class="bi bi-journal-text"></i> ${escAttr(a.name)}</a>
        ${a.bibtex ? `<button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="articleBibtexCopy(${a.id})" title="复制 BibTeX 引用信息到剪贴板"><i class="bi bi-clipboard"></i> BibTeX</button>` : ''}
        ${a.source ? `<span class="text-secondary" style="font-size:0.72rem">${escAttr(a.source)}</span>` : ''}
        ${isAdmin() ? `
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="articleEditStart(${a.id})" title="编辑"><i class="bi bi-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="articleDelete(${a.id})" title="删除"><i class="bi bi-trash"></i></button>` : ''}
      </div>
      ${a.title ? `<div class="text-secondary text-truncate" style="font-size:0.78rem;max-width:100%" title="${escAttr(a.title)}">${escAttr(a.title)}</div>` : ''}
    </div>`;
  let items = articlesData.map(itemHTML).join('');
  if (articlesData.length > COLLAPSE_N) {
    const head = articlesData.slice(0, COLLAPSE_N).map(itemHTML).join('');
    const rest = articlesData.slice(COLLAPSE_N).map(itemHTML).join('');
    items = head + `<div id="articleOverflow" style="display:none">${rest}</div>`
      + `<button class="btn btn-sm btn-outline-secondary py-0 px-2 mt-1" id="articleListToggleBtn" onclick="articleListToggle()"><i class="bi bi-chevron-down"></i> 展开全部 (${articlesData.length})</button>`;
  }
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

// ─── 概览「宿主星系」行：异步拉取摘要，点击跳转到宿主星系标签页 ───
export async function fillHostSummary(tid) {
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
  if (host.redshift != null) parts.push(`z=${esc(host.redshift)} (${escAttr(host.redshift_type || '?')})`);
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

// ─── 研究文章条目增删改（内联 onclick 契约） ───
window.articleListToggle = () => {
  const rest = document.getElementById('articleOverflow');
  const b = document.getElementById('articleListToggleBtn');
  if (!rest || !b) return;
  const show = rest.style.display === 'none';
  rest.style.display = show ? '' : 'none';
  b.innerHTML = show
    ? '<i class="bi bi-chevron-up"></i> 收起'
    : `<i class="bi bi-chevron-down"></i> 展开全部 (${articlesData.length})`;
};

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
    await createArticle({ transient_id: currentTid, name, url, title, bibtex });
    showToast('已添加', 'success');
    render(currentTid);
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
    render(currentTid);
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
    render(currentTid);
  } catch (err) {
    showToast(`删除失败: ${err.message}`, 'danger');
  }
};

// ─── 银河系消光改正（本源全量；单点改正 lcGextRun 在 detail_lctable.js） ───
window.runGextSource = async () => {
  if (!isAdmin()) { showToast('仅管理员可执行银消改正', 'warning'); return; }
  if (!_transient || _transient.ra == null || _transient.dec == null) {
    showToast('该源缺少坐标，无法执行银消改正', 'warning');
    return;
  }
  if (!confirm(`对 ${currentTid} 的全部光学数据执行银河系消光改正？\n（CSFD 尘埃图 + P92 消光曲线，Rv=3.1）`)) return;
  const btn = document.getElementById('gextBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 计算中...'; }
  try {
    const st = await runExtinction({ transient_id: currentTid });
    showToast(`银消改正完成: ${st.corrected}/${st.total} 点已改正 (E(B-V)=${st.ebv != null ? st.ebv.toFixed(4) : '?'})` +
      (st.skipped_band ? `, ${st.skipped_band} 点波段不支持` : '') +
      (st.skipped_not_optical ? `, ${st.skipped_not_optical} 点非光学波段跳过` : '') +
      (st.note ? `。${st.note}` : ''), 'success');
    render(currentTid);
  } catch (err) {
    showToast(`银消改正失败: ${err.message}`, 'danger');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-moon-stars"></i> 银消改正'; }
  }
};

// ─── 基本信息编辑面板 ───
window.handleEditClick = () => {
  if (!isAdmin()) { showToast('仅管理员可修改事件信息', 'warning'); return; }
  _editActive = !_editActive;
  const editPanel = document.getElementById('editPanel');
  const metaCard = document.getElementById('metaCard');
  const editBtn = document.getElementById('editBtn');
  if (!editPanel || !metaCard || !editBtn) return;
  if (_editActive) {
    metaCard.style.display = 'none';
    editPanel.style.display = 'block';
    editBtn.innerHTML = '<i class="bi bi-x-lg"></i> 取消';
  } else {
    metaCard.style.display = 'block';
    editPanel.style.display = 'none';
    editBtn.innerHTML = '<i class="bi bi-pencil"></i>';
  }
};

window.saveDetailEdit = async () => {
  const val = (id) => document.getElementById(id)?.value?.trim() || null;
  const num = (id) => { const v = val(id); return v === null || v === '' ? null : parseFloat(v); };

  const raV = parseRA(val('editRa'));
  if (typeof raV === 'number' && isNaN(raV)) { showToast('RA 格式无法解析（支持十进制度或时分秒，如 08h08m27.4s）', 'danger'); return; }
  const decV = parseDec(val('editDec'));
  if (typeof decV === 'number' && isNaN(decV)) { showToast('Dec 格式无法解析（支持十进制度或时分秒，如 +40d36m44.8s）', 'danger'); return; }

  // T0 偏移量：空 → null，否则必须是数字（秒，可正可负）
  const t0OffsetStr = val('editT0Offset');
  let t0Offset = null;
  if (t0OffsetStr) {
    t0Offset = Number(t0OffsetStr);
    if (!isFinite(t0Offset)) { showToast('T0 偏移量需为数字（秒，可正可负）', 'danger'); return; }
  }

  // 主/副标签保存前登记（chip 输入收集名字；新标签需补文字说明；返回 null = 用户中止，不保存）
  const tags = await ensureTagsRegistered(_editTagsChip ? _editTagsChip.getValues() : [], 'main');
  if (tags === null) return;
  const subTags = await ensureTagsRegistered(_editSubTagsChip ? _editSubTagsChip.getValues() : [], 'sub');
  if (subTags === null) return;

  const body = {
    ra: raV,
    dec: decV,
    t0: val('editT0'),
    t0_ref: val('editT0Ref'),
    t0_offset: t0Offset,
    t0_offset_ref: val('editT0OffsetRef'),
    redshift: num('editZ'),
    redshift_type: val('editZType') || null,
    redshift_ref: val('editZRef'),
    pos_error: num('editPosErr'),
    pos_ref: val('editPosRef'),
    comment: val('editComment'),
    trigger_instrument: val('editTrigger'),
    tags,
    sub_tag: subTags,
    aliases: (val('editAliases') || '').split(',').map(s => s.trim()).filter(Boolean),
  };

  try {
    await updateTransient(currentTid, body);
    showToast('保存成功', 'success');
    _editActive = false;
    render(currentTid);
  } catch (err) {
    showToast(`保存失败: ${err.message}`, 'danger');
  }
};

// ─── 导出光变数据（弹窗选基准时刻：源 T0 / 自定义 MJD / 自定义 UTC） ───
// 弹窗模板在 detail.js（#lcExportModal）；tRef=null 时后端按源 T0 导出
function _lcExportHint() {
  const hint = document.getElementById('lcExportHint');
  const mode = document.getElementById('lcExportMode')?.value || 't0';
  const custom = document.getElementById('lcExportCustom');
  if (!hint || !custom) return;
  custom.style.display = mode === 't0' ? 'none' : '';
  if (mode === 't0') {
    hint.textContent = (_transient && _transient.t0)
      ? `将以源 T0（${_transient.t0}）为基准导出。`
      : '该源无 T0，将以 MJD 列为准导出。';
  } else {
    hint.textContent = mode === 'mjd'
      ? '请输入 MJD 数字作为基准时刻。'
      : '请输入 UTC 时间（ISO 8601，如 2025-12-02T01:48:23Z）。';
  }
}

window.exportLCWithRef = () => {
  const mode = document.getElementById('lcExportMode');
  if (!mode) return;
  mode.value = 't0';
  const custom = document.getElementById('lcExportCustom');
  if (custom) custom.value = '';
  _lcExportHint();
  new bootstrap.Modal(document.getElementById('lcExportModal')).show();
};

window.lcExportModeChanged = () => _lcExportHint();

window.doLCExport = () => {
  const mode = document.getElementById('lcExportMode')?.value || 't0';
  const custom = document.getElementById('lcExportCustom')?.value.trim() || '';
  let tRef = null;
  if (mode === 'mjd') {
    if (!custom || !isFinite(Number(custom))) { showToast('请输入有效的 MJD 数字', 'warning'); return; }
    tRef = custom;
  } else if (mode === 'utc') {
    if (!custom || isNaN(Date.parse(custom))) { showToast('请输入有效的 UTC 时间（ISO 8601）', 'warning'); return; }
    tRef = custom;
  }
  exportLightcurves(currentTid, 'csv', tRef);
  bootstrap.Modal.getInstance(document.getElementById('lcExportModal'))?.hide();
};
