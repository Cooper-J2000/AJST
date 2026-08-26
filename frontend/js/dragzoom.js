// === 轻量框选缩放（无外部依赖） ===
// 用法：chart = new Chart(..., { plugins: [dragRectPlugin], ... })
//       attachDragZoom(holder, canvas, onZoom)
// holder = { chart }：图表重建后更新 holder.chart，监听器始终作用于当前实例。
import { chartColors } from './theme.js';

// Chart.js 插件：afterDraw 画选区矩形
export const dragRectPlugin = {
  id: 'dzRect',
  afterDraw(chart) {
    const r = chart._dzRect;
    if (!r) return;
    const cc = chartColors();
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = cc.selectFill;
    ctx.strokeStyle = cc.selectStroke;
    ctx.setLineDash([4, 3]);
    const x = Math.min(r.x0, r.x1), y = Math.min(r.y0, r.y1);
    ctx.fillRect(x, y, Math.abs(r.x1 - r.x0), Math.abs(r.y1 - r.y0));
    ctx.strokeRect(x, y, Math.abs(r.x1 - r.x0), Math.abs(r.y1 - r.y0));
    ctx.restore();
  },
};

// 在 canvas 上挂框选监听（同一 canvas 只挂一次）；onZoom({xmin,xmax,ymin,ymax})
// opts.allowNonPositive：true 时 y 允许 0/负值（线性轴，如光谱图）；缺省 false（log 轴图保持原行为）
export function attachDragZoom(holder, canvas, onZoom, opts = {}) {
  if (canvas._dzOn) return;
  canvas._dzOn = true;
  let start = null;
  const rel = (e) => {
    const b = canvas.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  };
  canvas.addEventListener('mousedown', (e) => {
    const chart = holder.chart;
    const a = chart && chart.chartArea;
    if (!a) return;
    const p = rel(e);
    if (p.x >= a.left && p.x <= a.right && p.y >= a.top && p.y <= a.bottom) {
      start = p;
      e.preventDefault();
    }
  });
  canvas.ownerDocument.addEventListener('mousemove', (e) => {
    const chart = holder.chart;
    if (!start || !chart) return;
    const p = rel(e);
    chart._dzRect = { x0: start.x, y0: start.y, x1: p.x, y1: p.y };
    chart.draw();
  });
  canvas.ownerDocument.addEventListener('mouseup', () => {
    const chart = holder.chart;
    if (!start || !chart) return;
    start = null;
    const r = chart._dzRect;
    chart._dzRect = null;
    chart.draw();
    if (!r || Math.abs(r.x1 - r.x0) < 8 || Math.abs(r.y1 - r.y0) < 8) return;
    const xs = chart.scales.x, ys = chart.scales.y;
    const range = {
      xmin: xs.getValueForPixel(Math.min(r.x0, r.x1)),
      xmax: xs.getValueForPixel(Math.max(r.x0, r.x1)),
      ymax: ys.getValueForPixel(Math.min(r.y0, r.y1)),  // 像素小 = 值大
      ymin: ys.getValueForPixel(Math.max(r.y0, r.y1)),
    };
    if (![range.xmin, range.xmax, range.ymin, range.ymax].every(v => isFinite(v))) return;
    if (!opts.allowNonPositive && ![range.xmin, range.xmax, range.ymin, range.ymax].every(v => v > 0)) return;
    onZoom(range);
  });
}
