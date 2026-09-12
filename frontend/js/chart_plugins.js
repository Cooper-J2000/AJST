// === 共享 Chart.js 插件 ===
// 各页面误差棒插件原在 detail/compare/fitting_tab/relations/stats_hosts 各存一份，
// 此处收敛为工厂函数；差异通过选项注入，行为与原版逐页对应。
import { chartColors } from './theme.js';
import { normErr } from './utils.js';

// 竖直对称误差棒（beforeDatasetsDraw：画在数据点下层，带端帽）
// opts:
//   id          插件 id（默认 'errorBar'）
//   enabled     () => bool，图头「误差棒」开关（默认恒 true）
//   errOf       (ds, raw, i) => number|null，误差来源（ds._errorValues[i] 或 raw.err）
//   skipDataset (ds) => bool，整个数据集跳过（上限点层/拟合线层等）
//   skipPoint   (raw) => bool，单点跳过（如 raw.isUL）
export function createYErrBarPlugin(opts = {}) {
  const {
    id = 'errorBar',
    enabled = () => true,
    errOf,
    skipDataset = null,
    skipPoint = null,
  } = opts;
  return {
    id,
    beforeDatasetsDraw(chart) {
      if (!enabled()) return;
      try {
        const ctx = chart.ctx, yScale = chart.scales.y;
        if (!ctx || !yScale) return;
        chart.data.datasets.forEach((ds, dsIdx) => {
          if (skipDataset && skipDataset(ds)) return;
          if (!chart.isDatasetVisible(dsIdx)) return;   // 图例取消勾选时不画其误差棒
          const meta = chart.getDatasetMeta(dsIdx);
          if (!meta || !meta.data) return;
          ctx.save();
          ctx.strokeStyle = ds.borderColor || '#fff';
          ctx.lineWidth = 1;
          const n = Math.min(meta.data.length, ds.data.length);
          for (let i = 0; i < n; i++) {
            const raw = ds.data[i];
            if (!raw || raw.y == null || !isFinite(raw.y)) continue;
            if (skipPoint && skipPoint(raw)) continue;
            const err = errOf(ds, raw, i);
            if (err == null || !(err > 0)) continue;
            const el = meta.data[i];
            if (!el || el.skip) continue;
            const yTop = yScale.getPixelForValue(raw.y + err);
            const yBot = yScale.getPixelForValue(raw.y - err);
            if (!isFinite(yTop) || !isFinite(yBot)) continue;
            const cx = el.x;
            ctx.beginPath();
            ctx.moveTo(cx, Math.min(yTop, yBot));
            ctx.lineTo(cx, Math.max(yTop, yBot));
            ctx.moveTo(cx - 3, yTop);
            ctx.lineTo(cx + 3, yTop);
            ctx.moveTo(cx - 3, yBot);
            ctx.lineTo(cx + 3, yBot);
            ctx.stroke();
          }
          ctx.restore();
        });
      } catch (e) { console.error('errorBar plugin:', e); }
    },
  };
}

// x/y 双向（可不对称）误差棒（afterDatasetsDraw；仅散点层；颜色随主题；钳到轴范围）
// 原 relations.js 版：高亮层（ds._noErrBar）不画，log 轴上误差按线性值换算像素
export const xyErrBarPlugin = {
  id: 'errorBars',
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    chart.data.datasets.forEach((ds, di) => {
      if (!ds._isScatter || ds._noErrBar) return;   // 高亮层不画误差棒，保持星形醒目
      const meta = chart.getDatasetMeta(di);
      if (meta.hidden) return;
      const xs = chart.scales.x, ys = chart.scales.y;
      ctx.save();
      ctx.strokeStyle = chartColors().errorBar;
      ctx.lineWidth = 1;
      meta.data.forEach((el, i) => {
        const raw = ds.data[i];
        if (!raw) return;
        const px = el.x, py = el.y;
        const xe = normErr(raw.xerr);
        if (xe) {
          const lo = Math.max(raw.x - xe[1], xs.min);
          const hi = Math.min(raw.x + xe[0], xs.max);
          ctx.beginPath();
          ctx.moveTo(xs.getPixelForValue(lo), py);
          ctx.lineTo(xs.getPixelForValue(hi), py);
          ctx.stroke();
        }
        const ye = normErr(raw.yerr);
        if (ye) {
          const lo = Math.max(raw.y - ye[1], ys.min);
          const hi = Math.min(raw.y + ye[0], ys.max);
          ctx.beginPath();
          ctx.moveTo(px, ys.getPixelForValue(lo));
          ctx.lineTo(px, ys.getPixelForValue(hi));
          ctx.stroke();
        }
      });
      ctx.restore();
    });
  },
};
