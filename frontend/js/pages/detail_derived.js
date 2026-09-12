// === 派生物理量 (derived) 卡片（详情页概览标签内嵌） ===
// 查看/编辑 extra_data.derived；手动修改打 manual 标记、删除记 _manual_deleted 墓碑，
// 重跑 derive_prompt_params.py 时保留（见 backend 侧脚本约定）。
import { updateTransient, isAdmin, showToast } from '../api.js';
import { esc, escAttr, sciFormat } from '../utils.js';
import { currentTid } from './detail.js';

// ─── 编辑状态 ───
let derivedDraft = null;     // 工作副本
let derivedPristine = null;  // 服务器载入的原始副本（取消编辑时恢复）
let derivedEditMode = false;

// 常见键名（添加新量时的下拉建议，可自定义）
const DERIVED_KEY_SUGGESTIONS = ['ep_rest', 'eiso', 'eiso_1000', 'lp_iso', 'tlag_rest',
  'variability', 'e_gamma', 't90_rest', 't90_obs', 'epeak_obs', 'alpha', 'spec_class', 'grb_type'];

// render() 重建 DOM 前调用：清空编辑状态
export function resetDerived() {
  derivedDraft = null;
  derivedPristine = null;
  derivedEditMode = false;
}

// render() 在模板插入卡片后调用：载入服务器数据并填充卡片正文
export function initDerived(derived) {
  if (!derived) return;
  derivedPristine = JSON.parse(JSON.stringify(derived));
  derivedDraft = JSON.parse(JSON.stringify(derived));
  renderDerivedBody();
  if (isAdmin()) {
    const btn = document.getElementById('derivedEditBtn');
    if (btn) btn.style.display = '';
  }
}

export function renderDerivedCard(extraData) {
  const d = extraData && extraData.derived;
  if (!d || (Object.keys(d.best || {}).length === 0 &&
             Object.keys(d.sources || {}).length === 0 && !d.grb_type)) return '';
  return `
  <div class="row mt-3">
    <div class="col-12">
      <div class="card" id="derivedCard">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span><i class="bi bi-stars"></i> 派生物理量 (derived) <small class="text-secondary">计算于 ${esc(d.computed) || '-'}</small></span>
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
    return `<tr><td class="text-secondary" style="width:130px">${esc(k)}</td><td>${esc(p)}</td>${showSrc ? '<td></td>' : ''}</tr>`;
  }
  const badge = p.manual ? ' <span class="badge bg-warning text-dark" title="手动修改：重跑 derive 脚本时保留">手动</span>' : '';
  return `<tr><td class="text-secondary" style="width:130px">${esc(k)}</td><td>${sciFormat(p.v)}${_fmtDerivedErrView(p.err)}${badge}</td>${showSrc ? `<td class="small text-secondary">${esc(p.src)}</td>` : ''}</tr>`;
}

function derivedViewHTML() {
  const d = derivedDraft || {};
  const parts = [];
  if (d.grb_type && d.grb_type.v != null) {
    const gtBadge = d.grb_type.manual ? ' <span class="badge bg-warning text-dark" title="手动修改：重跑 derive 脚本时保留">手动</span>' : '';
    parts.push(`<div class="mb-2"><strong>GRB 类型:</strong> ${esc(d.grb_type.v)}${gtBadge} <small class="text-secondary">(来源: ${esc(d.grb_type.src) || '-'})</small></div>`);
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
        <summary class="small fw-bold" style="cursor:pointer">${esc(cat)}</summary>
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
// 删除/添加按钮用 data-* + derivedCardBody 上的事件委托（避免把目录名/键名拼进内联 onclick）
function _derivedEditRow(scope, cat, k, p, showSrc) {
  const catAttr = cat != null ? `data-cat="${escAttr(cat)}"` : '';
  const delBtn = `<button class="btn btn-sm btn-outline-danger py-0 px-1" data-ddel data-scope="${escAttr(scope)}" ${catAttr} data-key="${escAttr(k)}" title="删除"><i class="bi bi-trash"></i></button>`;
  if (p != null && typeof p === 'object') {
    return `<tr>
      <td class="text-secondary small">${esc(k)}</td>
      <td><input type="text" class="form-control form-control-sm derived-inp" data-scope="${escAttr(scope)}" ${catAttr} data-key="${escAttr(k)}" data-field="v" value="${escAttr(p.v ?? '')}" style="width:110px" title="v（必须数字）"></td>
      <td><input type="text" class="form-control form-control-sm derived-inp" data-scope="${escAttr(scope)}" ${catAttr} data-key="${escAttr(k)}" data-field="err" value="${escAttr(_errToStr(p.err))}" style="width:110px" title="err：单值或 正,负"></td>
      ${showSrc ? `<td><input type="text" class="form-control form-control-sm derived-inp" data-scope="${escAttr(scope)}" ${catAttr} data-key="${escAttr(k)}" data-field="src" value="${escAttr(p.src)}" style="width:110px" title="来源"></td>` : ''}
      <td>${delBtn}</td>
    </tr>`;
  }
  // 纯字符串条目（如 spec_class）
  return `<tr>
    <td class="text-secondary small">${esc(k)}</td>
    <td colspan="${showSrc ? 3 : 2}"><input type="text" class="form-control form-control-sm derived-inp" data-scope="${escAttr(scope)}" ${catAttr} data-key="${escAttr(k)}" data-field="str" value="${escAttr(p)}" style="width:220px"></td>
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
      <summary class="small fw-bold" style="cursor:pointer">${esc(cat)}</summary>
      <table class="table table-sm table-borderless mb-1 ms-3" style="width:auto"><tbody>
        ${Object.keys(d.sources[cat]).map(k => _derivedEditRow('src', cat, k, d.sources[cat][k], false)).join('') || '<tr><td class="text-secondary small">（空）</td></tr>'}
      </tbody></table>
      <div class="d-flex flex-wrap gap-1 align-items-center ms-3 mb-1 small">
        <input type="text" class="form-control form-control-sm" id="dq_key_${escAttr(cat)}" list="derivedKeyList" placeholder="键名" style="width:120px" title="可下拉选择或自定义">
        <input type="text" class="form-control form-control-sm" id="dq_v_${escAttr(cat)}" placeholder="v" style="width:100px">
        <input type="text" class="form-control form-control-sm" id="dq_err_${escAttr(cat)}" placeholder="err (正,负)" style="width:110px">
        <button class="btn btn-sm btn-outline-primary py-0 px-1" data-dadd="${escAttr(cat)}" title="给 ${escAttr(cat)} 添加新量"><i class="bi bi-plus-lg"></i></button>
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
  if (!el._dgBound) {   // 删除/添加按钮的事件委托（data-* 携带目录名/键名，容器在 innerHTML 重绘间保持）
    el._dgBound = true;
    el.addEventListener('click', (e) => {
      const del = e.target.closest('[data-ddel]');
      if (del) { window.derivedDel(del.dataset.scope, del.dataset.cat ?? null, del.dataset.key ?? null); return; }
      const add = e.target.closest('[data-dadd]');
      if (add) window.derivedAddQty(add.dataset.dadd);
    });
  }
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

// ─── 全局入口（内联 onclick 契约） ───
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
    const resp = await updateTransient(currentTid, { extra_data: { derived: derivedDraft } });
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
