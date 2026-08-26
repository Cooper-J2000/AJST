// === 抠图取数核心算法（纯函数，无 DOM 依赖，Node 可单测） ===
// 坐标标定变换 / sRGB→CIE Lab / 颜色掩膜 / 逐列描线 / 连通域符号识别

// ─── 坐标轴标定变换 ───
// calib: { px1, px2, x1, x2, xLog, py1, py2, y1, y2, yLog }
// 像素点 (px*,py*) 与数据值 (x*,y*) 对应；对数轴先取 log10 再线性插值。
// X 轴只用像素 x，Y 轴只用像素 y（假设轴正交）；星等反转轴由两点值序天然处理。
export function makeCalibTransform(c) {
  const { px1, px2, x1, x2, xLog, py1, py2, y1, y2, yLog } = c;
  if (px1 === px2 || py1 === py2) throw new Error('同一轴的两个标定点像素位置不能相同');
  for (const [v, name, log] of [[x1, 'x1', xLog], [x2, 'x2', xLog], [y1, 'y1', yLog], [y2, 'y2', yLog]]) {
    if (v == null || !isFinite(v)) throw new Error(`标定值 ${name} 缺失或非法`);
    if (log && v <= 0) throw new Error(`对数轴的标定值必须为正数（${name}=${v}）`);
  }
  const lx1 = xLog ? Math.log10(x1) : x1, lx2 = xLog ? Math.log10(x2) : x2;
  const ly1 = yLog ? Math.log10(y1) : y1, ly2 = yLog ? Math.log10(y2) : y2;
  const toData = (px, py) => {
    const lx = lx1 + (lx2 - lx1) * (px - px1) / (px2 - px1);
    const ly = ly1 + (ly2 - ly1) * (py - py1) / (py2 - py1);
    return { x: xLog ? Math.pow(10, lx) : lx, y: yLog ? Math.pow(10, ly) : ly };
  };
  const toPixel = (x, y) => {
    const lx = xLog ? Math.log10(x) : x, ly = yLog ? Math.log10(y) : y;
    return {
      px: px1 + (px2 - px1) * (lx - lx1) / (lx2 - lx1),
      py: py1 + (py2 - py1) * (ly - ly1) / (ly2 - ly1),
    };
  };
  return { toData, toPixel };
}

// ─── sRGB → CIE Lab（D65，标准公式） ───
export function rgbToLab(r, g, b) {
  const lin = (v) => {
    v /= 255;
    return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  const R = lin(r), G = lin(g), B = lin(b);
  let X = R * 0.4124564 + G * 0.3575761 + B * 0.1804375;
  let Y = R * 0.2126729 + G * 0.7151522 + B * 0.0721750;
  let Z = R * 0.0193339 + G * 0.1191920 + B * 0.9503041;
  X /= 0.95047; Z /= 1.08883;   // D65 白点归一
  const f = (t) => t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116;
  const fx = f(X), fy = f(Y), fz = f(Z);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

export function labDist2(l1, l2) {
  const dL = l1[0] - l2[0], da = l1[1] - l2[1], db = l1[2] - l2[2];
  return dL * dL + da * da + db * db;
}

// 3×3 邻域均值色（图像取色用），返回 Lab
export function avgColorLab(pixels, width, height, cx, cy) {
  let r = 0, g = 0, b = 0, n = 0;
  for (let dy = -1; dy <= 1; dy++) {
    for (let dx = -1; dx <= 1; dx++) {
      const x = Math.round(cx) + dx, y = Math.round(cy) + dy;
      if (x < 0 || y < 0 || x >= width || y >= height) continue;
      const o = (y * width + x) * 4;
      r += pixels[o]; g += pixels[o + 1]; b += pixels[o + 2]; n++;
    }
  }
  if (!n) return null;
  return rgbToLab(r / n, g / n, b / n);
}

// ─── 颜色掩膜：与目标色 Lab 距离 ≤ tolerance 的像素置 1 ───
// pixels: RGBA Uint8ClampedArray；返回 Uint8Array(width*height)
export function buildMask(pixels, width, height, targetLab, tolerance) {
  const mask = new Uint8Array(width * height);
  const tol2 = tolerance * tolerance;
  for (let i = 0; i < width * height; i++) {
    const o = i * 4;
    if (pixels[o + 3] < 128) continue;  // 透明像素跳过
    const lab = rgbToLab(pixels[o], pixels[o + 1], pixels[o + 2]);
    if (labDist2(lab, targetLab) <= tol2) mask[i] = 1;
  }
  return mask;
}

// ─── 描线模式：逐列扫描掩膜，取与前一 y 最接近的连续段的质心 ───
// 返回 [{px, py}]（像素坐标）；首列取最长段，之后按连续性选段
export function traceLine(mask, width, height, { x0 = 0, x1 = width - 1, stride = 1 } = {}) {
  const pts = [];
  let prevY = null;
  const xa = Math.max(0, Math.round(x0)), xb = Math.min(width - 1, Math.round(x1));
  for (let x = xa; x <= xb; x += Math.max(1, stride)) {
    // 收集该列的连续掩膜段 [yStart, yEnd]
    const runs = [];
    let start = -1;
    for (let y = 0; y < height; y++) {
      if (mask[y * width + x]) {
        if (start < 0) start = y;
      } else if (start >= 0) {
        runs.push([start, y - 1]);
        start = -1;
      }
    }
    if (start >= 0) runs.push([start, height - 1]);
    if (!runs.length) continue;  // 空列：prevY 保持，跨过缺口
    let chosen;
    if (prevY == null) {
      chosen = runs.reduce((a, b) => (b[1] - b[0]) > (a[1] - a[0]) ? b : a);
    } else {
      chosen = runs.reduce((a, b) => {
        const ca = (a[0] + a[1]) / 2, cb = (b[0] + b[1]) / 2;
        return Math.abs(cb - prevY) < Math.abs(ca - prevY) ? b : a;
      });
    }
    const yc = (chosen[0] + chosen[1]) / 2;
    pts.push({ px: x, py: yc });
    prevY = yc;
  }
  return pts;
}

// ─── 符号模式：4-连通域分析，按面积过滤，取质心 ───
// 返回 [{px, py, area}]（像素坐标）
export function detectSymbols(mask, width, height, { minArea = 4, maxArea = Infinity } = {}) {
  const visited = new Uint8Array(width * height);
  const out = [];
  const stack = [];
  for (let i = 0; i < width * height; i++) {
    if (!mask[i] || visited[i]) continue;
    let sx = 0, sy = 0, n = 0;
    stack.length = 0;
    stack.push(i);
    visited[i] = 1;
    while (stack.length) {
      const cur = stack.pop();
      const cx = cur % width, cy = (cur / width) | 0;
      sx += cx; sy += cy; n++;
      if (cx > 0) { const j = cur - 1; if (mask[j] && !visited[j]) { visited[j] = 1; stack.push(j); } }
      if (cx < width - 1) { const j = cur + 1; if (mask[j] && !visited[j]) { visited[j] = 1; stack.push(j); } }
      if (cy > 0) { const j = cur - width; if (mask[j] && !visited[j]) { visited[j] = 1; stack.push(j); } }
      if (cy < height - 1) { const j = cur + width; if (mask[j] && !visited[j]) { visited[j] = 1; stack.push(j); } }
    }
    if (n >= minArea && n <= maxArea) out.push({ px: sx / n, py: sy / n, area: n });
  }
  return out;
}

// ─── 直线插值采样（像素空间）：p0→p1 沿主轴每 stepPx 取一点，含末端点 ───
export function sampleLine(p0, p1, stepPx) {
  const step = Math.max(0.5, stepPx || 5);
  let [a, b] = p0.px <= p1.px ? [p0, p1] : [p1, p0];
  const out = [];
  const dx = b.px - a.px;
  if (Math.abs(dx) < 1e-9) {  // 垂直线：沿 y 采样
    const y0 = Math.min(a.py, b.py), y1 = Math.max(a.py, b.py);
    for (let y = y0; y < y1; y += step) out.push({ px: a.px, py: y });
    out.push({ px: a.px, py: y1 });
    return out;
  }
  const dy = b.py - a.py;
  for (let x = a.px; x < b.px; x += step) {
    out.push({ px: x, py: a.py + dy * (x - a.px) / dx });
  }
  out.push({ px: b.px, py: b.py });
  return out;
}

// ─── 自然三次样条（xs 严格递增），返回求值函数 ───
export function makeNaturalSpline(xs, ys) {
  const n = xs.length;
  if (n < 2) throw new Error('样条至少需要 2 个控制点');
  const h = [];
  for (let i = 0; i < n - 1; i++) {
    h.push(xs[i + 1] - xs[i]);
    if (h[i] <= 0) throw new Error('控制点 x 必须严格递增');
  }
  if (n === 2) {  // 两点退化为直线
    return (x) => ys[0] + (ys[1] - ys[0]) * (x - xs[0]) / h[0];
  }
  // 解三对角方程求内部点二阶导 M（自然边界 M0=Mn-1=0），Thomas 算法
  const m = n - 2;
  const lower = [], diag = [], upper = [], rhs = [];
  for (let j = 0; j < m; j++) {   // j 对应内点 i=j+1
    lower.push(h[j]);
    diag.push(2 * (h[j] + h[j + 1]));
    upper.push(h[j + 1]);
    rhs.push(6 * ((ys[j + 2] - ys[j + 1]) / h[j + 1] - (ys[j + 1] - ys[j]) / h[j]));
  }
  for (let j = 1; j < m; j++) {
    const w = lower[j] / diag[j - 1];
    diag[j] -= w * upper[j - 1];
    rhs[j] -= w * rhs[j - 1];
  }
  const M = new Array(n).fill(0);
  const sol = new Array(m);
  sol[m - 1] = rhs[m - 1] / diag[m - 1];
  for (let j = m - 2; j >= 0; j--) sol[j] = (rhs[j] - upper[j] * sol[j + 1]) / diag[j];
  for (let j = 0; j < m; j++) M[j + 1] = sol[j];
  return (x) => {
    let i = 0;
    while (i < n - 2 && x > xs[i + 1]) i++;
    const hi = h[i];
    const A = (xs[i + 1] - x) / hi, B = (x - xs[i]) / hi;
    return A * ys[i] + B * ys[i + 1] + ((A * A * A - A) * M[i] + (B * B * B - B) * M[i + 1]) * hi * hi / 6;
  };
}

// ─── Catmull-Rom 样条（非均匀 x，有限差分切线；tension∈[0,1] 越大越贴直线、越不易过冲） ───
export function makeCatmullRom(xs, ys, tension = 0.5) {
  const n = xs.length;
  if (n < 2) throw new Error('样条至少需要 2 个控制点');
  const h = [];
  for (let i = 0; i < n - 1; i++) {
    h.push(xs[i + 1] - xs[i]);
    if (h[i] <= 0) throw new Error('控制点 x 必须严格递增');
  }
  if (n === 2) {
    return (x) => ys[0] + (ys[1] - ys[0]) * (x - xs[0]) / h[0];
  }
  const s = 1 - Math.min(1, Math.max(0, tension));  // 张力→切线缩放
  const m = new Array(n);
  m[0] = s * (ys[1] - ys[0]) / h[0];
  for (let i = 1; i < n - 1; i++) m[i] = s * (ys[i + 1] - ys[i - 1]) / (xs[i + 1] - xs[i - 1]);
  m[n - 1] = s * (ys[n - 1] - ys[n - 2]) / h[n - 2];
  return (x) => {
    let i = 0;
    while (i < n - 2 && x > xs[i + 1]) i++;
    const u = (x - xs[i]) / h[i];
    const u2 = u * u, u3 = u2 * u;
    // Hermite 基函数
    return (2 * u3 - 3 * u2 + 1) * ys[i] + (u3 - 2 * u2 + u) * h[i] * m[i]
         + (-2 * u3 + 3 * u2) * ys[i + 1] + (u3 - u2) * h[i] * m[i + 1];
  };
}

// ─── 样条插值采样（像素空间）：控制点按 px 排序去重，x∈[min,max] 每 stepPx 取一点 ───
// method: 'natural'（自然三次，过点光滑但可能过冲）/ 'catmull'（Catmull-Rom，tension 可调）
export function sampleSpline(pts, stepPx, { method = 'natural', tension = 0.5 } = {}) {
  const step = Math.max(0.5, stepPx || 5);
  const sorted = [...pts].sort((a, b) => a.px - b.px);
  const xs = [], ys = [];
  for (const p of sorted) {
    if (xs.length && Math.abs(p.px - xs[xs.length - 1]) < 1e-9) continue;  // 同 x 去重
    xs.push(p.px); ys.push(p.py);
  }
  const f = method === 'catmull' ? makeCatmullRom(xs, ys, tension) : makeNaturalSpline(xs, ys);
  const out = [];
  for (let x = xs[0]; x < xs[xs.length - 1]; x += step) out.push({ px: x, py: f(x) });
  out.push({ px: xs[xs.length - 1], py: ys[ys.length - 1] });
  return out;
}
