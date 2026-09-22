// === Tag 输入共享模块 ===
// 主/副标签（kind: main=主标签 / sub=副标签）的 datalist 自动补全与保存前新标签登记。
// 每种 kind 的标签索引只拉取一次并缓存；ensureTagsRegistered 创建成功后自动并入缓存，
// 需要强制重拉时调 invalidateTagCache()。
// 输入形态两种：attachTagInput（逗号分隔文本框 + datalist，新建源页用）与
// attachChipInput（chip 泡泡输入，详情页编辑面板用）。
import { getTags, createTag, showToast } from './api.js';
import { esc, escAttr } from './utils.js';

// kind → Promise<[{id,name,kind,description,color}]>（失败时清缓存并抛错，下次重试）
const _tagCache = { main: null, sub: null };

function _fetchTags(kind) {
  if (!_tagCache[kind]) {
    _tagCache[kind] = getTags(kind)
      .then(list => (Array.isArray(list) ? list : []))
      .catch(err => { _tagCache[kind] = null; throw err; });
  }
  return _tagCache[kind];
}

// 使缓存失效（kind 缺省时两种都清）
export function invalidateTagCache(kind = null) {
  if (kind) _tagCache[kind] = null;
  else { _tagCache.main = null; _tagCache.sub = null; }
}

// 给文本输入框挂 datalist 自动补全（option label 显示「name — description」，无描述只显示 name）。
// 逗号分隔的多值输入只补全最后一段（option value 动态拼上已输入前缀）。
// 标签索引拉取失败时静默降级（无补全，不报错）。
export function attachTagInput(inputEl, kind) {
  if (!inputEl) return;
  const dl = document.createElement('datalist');
  dl.id = `taglist_${kind}_${inputEl.id || Math.random().toString(36).slice(2, 8)}`;
  inputEl.setAttribute('list', dl.id);
  inputEl.setAttribute('autocomplete', 'off');
  inputEl.insertAdjacentElement('afterend', dl);

  let tags = [];
  const fill = () => {
    if (!dl.isConnected) return;
    const v = inputEl.value;
    const idx = v.lastIndexOf(',');
    const prefix = idx >= 0 ? v.slice(0, idx + 1).trimEnd() + ' ' : '';
    dl.innerHTML = tags.map(t => {
      const label = t.description ? `${t.name} — ${t.description}` : t.name;
      return `<option value="${escAttr(prefix + t.name)}" label="${escAttr(label)}"></option>`;
    }).join('');
  };
  inputEl.addEventListener('input', fill);
  _fetchTags(kind)
    .then(list => { tags = list; fill(); })
    .catch(() => {});   // 索引拉取失败：无补全，静默降级
}

// 保存前确保所有 tag 已在标签索引中登记。
// 对缓存里不存在的名字逐个 prompt 要求输入文字说明（必填；用户取消或留空 → 返回 null 中止保存），
// 确认后 createTag 登记并并入缓存；全部已存在则直接返回。
// 返回去重后的名字数组；拉取索引失败或登记失败（400/401 等）toast 报错并返回 null。
export async function ensureTagsRegistered(names, kind) {
  const list = [...new Set((names || []).map(s => String(s).trim()).filter(Boolean))];
  if (!list.length) return [];
  const kindLabel = kind === 'main' ? '主标签' : '副标签';
  let known;
  try {
    known = await _fetchTags(kind);
  } catch (err) {
    showToast(`获取${kindLabel}索引失败: ${err.message}`, 'danger');
    return null;
  }
  const knownNames = new Set(known.map(t => t.name));
  for (const name of list) {
    if (knownNames.has(name)) continue;
    const desc = window.prompt(`新${kindLabel}「${name}」尚未登记，请输入该标签的文字说明（必填；取消或留空将中止保存）:`);
    if (desc === null || !desc.trim()) {
      showToast(`已中止保存：新${kindLabel}「${name}」需要文字说明`, 'warning');
      return null;
    }
    try {
      await createTag({ name, kind, description: desc.trim() });
    } catch (err) {
      showToast(`登记${kindLabel}「${name}」失败: ${err.message}`, 'danger');
      return null;
    }
    // 创建成功并入缓存（known 即缓存数组，push 直接生效）
    known.push({ name, kind, description: desc.trim() });
    knownNames.add(name);
  }
  return list;
}

// ─── Chip（泡泡）式多值输入 ───
// attachChipInput(container, kind, initial)：容器内渲染「已固化 chip + 待输入文本框」组合。
// 逗号/回车/datalist 选择把当前文本固化为 chip（× 可删）；输入框为空时退格删除最后一个 chip；
// 粘贴含逗号/换行的文本自动拆成多个 chip；自动补全复用 attachTagInput 的 datalist
// （chip 模式下输入框只含当前段，补全天然无前缀）。× 用 mousedown+preventDefault 处理：
// 保持在 chip DOM 重绘前完成删除，且不打断输入框焦点（click 会被 blur 时序吃掉）。
// chip 样式沿用列表页 badge：主标签 badge-tag、副标签 badge-neutral。
// 返回 { getValues }：去重后的名字数组（含尚未固化的待输入文本，保存时直接用）。
export function attachChipInput(container, kind, initial = []) {
  if (!container) return { getValues: () => [] };
  const badgeCls = kind === 'main' ? 'badge-tag' : 'badge-neutral';
  container.innerHTML = '';
  container.className = 'form-control form-control-sm d-flex flex-wrap align-items-center gap-1';
  container.style.height = 'auto';
  container.style.minHeight = 'calc(1.5em + 0.5rem + 2px)';
  container.style.cursor = 'text';

  let chips = [];
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = '输入后回车/逗号固化';
  input.style.cssText = 'border:none;background:transparent;outline:none;box-shadow:none;flex:1 1 90px;min-width:90px;padding:0;font-size:0.8rem;color:inherit';

  function renderChips() {
    container.querySelectorAll('.chip-item').forEach(n => n.remove());
    chips.forEach((name, idx) => {
      const chip = document.createElement('span');
      chip.className = `chip-item ${badgeCls}`;
      chip.style.cssText = 'margin-right:0;display:inline-flex;align-items:center;gap:2px;white-space:nowrap';
      chip.innerHTML = `<span>${esc(name)}</span><i class="bi bi-x-lg" role="button" title="移除" style="cursor:pointer;font-size:0.65rem"></i>`;
      chip.querySelector('i').addEventListener('mousedown', (e) => {
        e.preventDefault();    // 不触发输入框 blur，× 删除不被 blur 时序吃掉
        e.stopPropagation();
        chips.splice(idx, 1);
        renderChips();
      });
      container.insertBefore(chip, input);
    });
  }

  // 把输入框当前文本固化为 chip（去重；空文本忽略）
  function solidify() {
    const v = input.value.trim().replace(/,+$/, '').trim();
    input.value = '';
    if (v && !chips.includes(v)) { chips.push(v); renderChips(); }
  }

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); solidify(); }
    else if (e.key === 'Backspace' && input.value === '' && chips.length) {
      chips.pop();
      renderChips();
    }
  });
  // datalist 选择确认（change）时固化；未固化的待输入文本由 getValues 兜底收集
  input.addEventListener('change', solidify);
  input.addEventListener('paste', (e) => {
    const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
    if (!/[,\n]/.test(text)) return;   // 单段文本走默认粘贴
    e.preventDefault();
    for (const part of text.split(/[,\n]+/).map(s => s.trim()).filter(Boolean)) {
      if (!chips.includes(part)) chips.push(part);
    }
    renderChips();
  });
  container.addEventListener('click', () => input.focus());
  container.appendChild(input);
  attachTagInput(input, kind);

  chips = [...new Set((initial || []).map(s => String(s).trim()).filter(Boolean))];
  renderChips();

  return {
    getValues: () => {
      const pending = input.value.trim().replace(/,+$/, '').trim();
      const all = pending ? [...chips, pending] : [...chips];
      return [...new Set(all.map(s => s.trim()).filter(Boolean))];
    },
  };
}
