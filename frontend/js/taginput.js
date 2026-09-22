// === Tag 输入共享模块 ===
// 主/副标签（kind: main=主标签 / sub=副标签）的 datalist 自动补全与保存前新标签登记。
// 每种 kind 的标签索引只拉取一次并缓存；ensureTagsRegistered 创建成功后自动并入缓存，
// 需要强制重拉时调 invalidateTagCache()。
import { getTags, createTag, showToast } from './api.js';
import { escAttr } from './utils.js';

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
