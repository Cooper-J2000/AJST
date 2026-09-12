// === 前端共享工具（HTML 转义 / 数值格式化 / URL 校验 / 大数组极值） ===

// HTML 正文/属性通用转义（& < > " '）
export function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
export const escAttr = esc;

// null 容错的定点格式化（null/非有限值 → '-'）
export function fmtNum(v, digits) {
  if (v == null || !isFinite(v)) return '-';
  return Number(v).toFixed(digits);
}

// 外链 URL 校验：仅允许 http/https，其余（javascript: 等）回退 '#'
export function safeUrl(u) {
  if (u == null) return '#';
  const s = String(u).trim();
  if (/^https?:\/\//i.test(s)) return s;
  return '#';
}

// 大数组极值（循环实现，避免 Math.min(...arr) 参数展开溢出）
export function minOf(arr, acc) {
  let m = Infinity;
  for (let i = 0; i < arr.length; i++) {
    const v = acc ? acc(arr[i]) : arr[i];
    if (v != null && isFinite(v) && v < m) m = v;
  }
  return m === Infinity ? null : m;
}

export function maxOf(arr, acc) {
  let m = -Infinity;
  for (let i = 0; i < arr.length; i++) {
    const v = acc ? acc(arr[i]) : arr[i];
    if (v != null && isFinite(v) && v > m) m = v;
  }
  return m === -Infinity ? null : m;
}

// 3 位有效数字的科学计数格式化（|v| ∈ [1e-3, 1e4) 走小数，其余指数；null/非有限 → '-'）
export function sci3(v) {
  if (v == null || !isFinite(v)) return '-';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e-3 && a < 1e4) return Number(v.toPrecision(3)).toString();
  return v.toExponential(2);
}

// 误差统一为 [正误差, 负误差]，无误差返回 null
export function normErr(e) {
  if (e == null) return null;
  if (Array.isArray(e)) return [Math.abs(e[0]), Math.abs(e[1])];
  return [Math.abs(e), Math.abs(e)];
}

// 科学计数法格式化（光变图刻度/拟合范围标注用；|v| ∈ [0.1, 1e4) 保留两位小数）
export function sciFormat(v) {
  if (v === 0 || v == null) return '0';
  const abs = Math.abs(v);
  if (abs >= 0.1 && abs < 10000) {
    return v.toFixed(2);
  }
  return v.toExponential(1);
}

// 2-3 位有效数字（拟合参数标注/宿主摘要用）
export function sig3(v) {
  if (v == null || !isFinite(v)) return '?';
  if (v === 0) return '0';
  return String(parseFloat(Number(v).toPrecision(3)));
}
