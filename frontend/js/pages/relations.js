// === 统计关系页面（Amati / Yonetoku / Ghirlanda / lag-lum / var-lum / Ep-alpha） ===
import { app, showLoading, showError, statsTabs } from './layout.js';
import { api, showToast } from '../api.js';
import { chartColors, academicFonts } from '../theme.js';

// ─── 常量 ───
const LN10 = Math.LN10;
const TYPE_NAMES = { I: 'I 型', II: 'II 型', unknown: '未知' };
// 数据点/拟合线颜色按主题取：chartColors().type（I 红 / II 蓝 / 未知灰）、chartColors().typeFit

// ─── 页面状态 ───
let relDefs = [];            // 关系定义（/api/relations）
let relSources = {};         // {关系名: [来源...]}
let rawPoints = {};          // 当前来源下 {关系名: [点...]}
let charts = {};             // {关系名: Chart 实例}
const fitTokens = {};        // 服务端拟合请求并发令牌 {关系名: 序号}
const state = {
  source: 'best',
  tags: new Set(),           // 选中的 tag（空 = 不筛选）
  highlight: new Set(),      // 特殊标记的 GRB 名（大写、去空格）
  sigmaInt: true,            // 服务端拟合含内禀弥散 σ_int
  excluded: {},              // 排除记忆：{'关系名:来源': Set(id)}
  ranges: {},                // 坐标范围记忆：{'关系名:来源': {xmin,xmax,ymin,ymax}（null=自动）}
};

// ─── 工具函数 ───
// 科学计数法，3 位有效数字
function sci3(v) {
  if (v == null || !isFinite(v)) return '-';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e-3 && a < 1e4) return Number(v.toPrecision(3)).toString();
  return v.toExponential(2);
}

// 误差统一为 [正误差, 负误差]，无误差返回 null
function normErr(e) {
  if (e == null) return null;
  if (Array.isArray(e)) return [Math.abs(e[0]), Math.abs(e[1])];
  return [Math.abs(e), Math.abs(e)];
}

// 单值误差转 log10 空间误差（取不对称两侧平均）
function logErr(val, err) {
  const e = normErr(err);
  if (!e || val == null || val <= 0) return null;
  return ((e[0] + e[1]) / 2) / (val * LN10);
}

// 排除集合的键：关系 + 来源
function exclKey(name) { return `${name}:${state.source}`; }
function getExcluded(name) {
  const k = exclKey(name);
  if (!state.excluded[k]) state.excluded[k] = new Set();
  return state.excluded[k];
}

// 坐标范围的键：与排除记忆一致（关系 + 来源）
function getRange(name) { return state.ranges[exclKey(name)] || null; }

// ─── OLS 线性拟合（log10 空间），可选加权 ───
// pts: [{lx, ly, slx, sly}]，slx/sly 为 log 空间误差（可为 null）
// 返回 {a(斜率), b(截距), N, r, s, xbar, Sxx} 或 null
function olsFit(pts, weighted) {
  const N = pts.length;
  if (N < 3) return null;
  // 先做一次未加权拟合得到 m0（加权公式需要）
  const fit0 = _wls(pts, null);
  if (!fit0) return null;
  if (!weighted) return fit0;
  // 无误差信息的点用误差中位数代替
  const medSlx = median(pts.map(p => p.slx).filter(v => v != null)) ?? 0;
  const medSly = median(pts.map(p => p.sly).filter(v => v != null)) ?? 0;
  const m0 = fit0.a;
  const ws = pts.map(p => {
    const slx = p.slx ?? medSlx;
    const sly = p.sly ?? medSly;
    const v = sly * sly + m0 * m0 * slx * slx;
    return v > 0 ? 1 / v : 0;
  });
  if (ws.every(w => w === 0)) return fit0;
  return _wls(pts, ws);
}

function _wls(pts, ws) {
  const N = pts.length;
  const w = ws || pts.map(() => 1);
  const sw = w.reduce((s, v) => s + v, 0);
  if (sw <= 0) return null;
  const xbar = pts.reduce((s, p, i) => s + w[i] * p.lx, 0) / sw;
  const ybar = pts.reduce((s, p, i) => s + w[i] * p.ly, 0) / sw;
  let Sxx = 0, Sxy = 0;
  for (let i = 0; i < N; i++) {
    Sxx += w[i] * (pts[i].lx - xbar) ** 2;
    Sxy += w[i] * (pts[i].lx - xbar) * (pts[i].ly - ybar);
  }
  if (Sxx === 0) return null;
  const a = Sxy / Sxx;
  const b = ybar - a * xbar;
  // 残差标准差
  let ssr = 0;
  for (let i = 0; i < N; i++) ssr += w[i] * (pts[i].ly - a * pts[i].lx - b) ** 2;
  const s = Math.sqrt(ssr / (N - 2));
  // Pearson r（未加权）
  const mx = pts.reduce((s, p) => s + p.lx, 0) / N;
  const my = pts.reduce((s, p) => s + p.ly, 0) / N;
  let cxy = 0, cxx = 0, cyy = 0;
  for (const p of pts) {
    cxy += (p.lx - mx) * (p.ly - my);
    cxx += (p.lx - mx) ** 2;
    cyy += (p.ly - my) ** 2;
  }
  const r = (cxx > 0 && cyy > 0) ? cxy / Math.sqrt(cxx * cyy) : 0;
  return { a, b, N, r, s, xbar, Sxx };
}

function median(arr) {
  if (!arr.length) return null;
  const s = [...arr].sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// Pearson r（log10 空间，本地计算，配合服务端拟合结果显示）
function pearsonR(lpts) {
  const N = lpts.length;
  if (N < 2) return null;
  const mx = lpts.reduce((s, p) => s + p.lx, 0) / N;
  const my = lpts.reduce((s, p) => s + p.ly, 0) / N;
  let cxy = 0, cxx = 0, cyy = 0;
  for (const p of lpts) {
    cxy += (p.lx - mx) * (p.ly - my);
    cxx += (p.lx - mx) ** 2;
    cyy += (p.ly - my) ** 2;
  }
  return (cxx > 0 && cyy > 0) ? cxy / Math.sqrt(cxx * cyy) : null;
}

// ─── 误差棒插件：在散点上画 x/y 误差棒（细线、50% 透明，支持不对称误差） ───
const errorBarPlugin = {
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
        // log 轴上误差按线性值换算像素
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

// ─── 特殊标记：五角星插件（Chart.js 内置 'star' 只是空心星号线，不够醒目） ───
function drawStarPath(ctx, cx, cy, outerR, innerR) {
  ctx.beginPath();
  for (let i = 0; i < 10; i++) {
    const r = i % 2 === 0 ? outerR : innerR;
    const a = -Math.PI / 2 + i * Math.PI / 5;
    const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.closePath();
}

const highlightStarPlugin = {
  id: 'highlightStars',
  afterDatasetsDraw(chart) {   // 晚于散点和误差棒绘制 → 始终置顶
    const { ctx } = chart;
    chart.data.datasets.forEach((ds, di) => {
      if (!ds._isHighlight) return;
      const meta = chart.getDatasetMeta(di);
      if (meta.hidden) return;
      const cc = chartColors();
      ctx.save();
      for (const el of meta.data) {
        if (el.skip) continue;
        drawStarPath(ctx, el.x, el.y, 11, 4.6);
        ctx.fillStyle = cc.highlight;
        ctx.fill();
        ctx.lineWidth = 1.8;
        ctx.strokeStyle = cc.highlightEdge;
        ctx.stroke();
      }
      ctx.restore();
    });
  },
};

// ─── 当前卡片可见点（tag 筛选 + 个体排除后） ───
function visiblePoints(name) {
  const def = relDefs.find(r => r.name === name);
  const xLog = def ? def.x_log !== false : true;
  const pts = rawPoints[name] || [];
  const excl = getExcluded(name);
  return pts.filter(p => {
    if (excl.has(p.id)) return false;
    if (state.tags.size > 0 && !(p.tags || []).some(t => state.tags.has(t))) return false;
    if (p.x == null || p.y == null) return false;
    if (xLog && p.x <= 0) return false;   // log 轴要求正值
    if (p.y <= 0) return false;           // y 一律为 log 轴
    return true;
  });
}

function isHighlighted(p) {
  return state.highlight.has(p.id.toUpperCase().replace(/\s+/g, ''));
}

// ─── 图表构建 ───
async function buildChart(name) {
  const def = relDefs.find(r => r.name === name);
  const card = document.getElementById(`card-${name}`);
  if (!card || typeof Chart === 'undefined') return;

  const pts = visiblePoints(name);
  const emptyEl = document.getElementById(`empty-${name}`);
  const chartBox = document.getElementById(`chartbox-${name}`);
  const fitEl = document.getElementById(`fitinfo-${name}`);

  if (!pts.length) {
    if (charts[name]) { charts[name].destroy(); charts[name] = null; }
    chartBox.style.display = 'none';
    emptyEl.style.display = 'block';
    emptyEl.textContent = (rawPoints[name] || []).length
      ? '当前筛选条件下无数据点'
      : `来源 "${state.source}" 对此关系无数据`;
    fitEl.innerHTML = '';
    renderExcludedList(name);
    return;
  }
  chartBox.style.display = 'block';
  emptyEl.style.display = 'none';

  const canvas = document.getElementById(`cv-${name}`);
  const xLog = def.x_log !== false;
  const cc = chartColors();
  const TYPE_COLORS = cc.type, FIT_COLORS = cc.typeFit;

  // 按 grb_type 分组散点（高亮点拆出，最后单独成层置顶绘制）
  const groups = { I: [], II: [], unknown: [] };
  const hlPts = [];
  for (const p of pts) {
    const g = p.grb_type === 'I' ? 'I' : (p.grb_type === 'II' ? 'II' : 'unknown');
    if (isHighlighted(p)) hlPts.push({ ...p, _grp: g });
    else groups[g].push(p);
  }
  const datasets = [];
  for (const g of ['I', 'II', 'unknown']) {
    if (!groups[g].length) continue;
    datasets.push({
      label: TYPE_NAMES[g],
      _isScatter: true,
      data: groups[g].map(p => ({ ...p })),
      backgroundColor: TYPE_COLORS[g],
      borderColor: TYPE_COLORS[g],
      pointStyle: 'circle',
      radius: 4,
      hoverRadius: 6,
    });
  }

  // 转 log10 空间坐标
  const toLog = p => ({
    lx: xLog ? Math.log10(p.x) : p.x,
    ly: Math.log10(p.y),
    slx: xLog ? logErr(p.x, p.xerr) : null,
    sly: logErr(p.y, p.yerr),
  });

  // ── 拟合：优先服务端最大似然（POST /relations/<name>/fit），失败回退本地 OLS ──
  const token = (fitTokens[name] = (fitTokens[name] || 0) + 1);
  let fitResp = null, fitFailed = false;
  try {
    fitResp = await api('POST', `/relations/${name}/fit`, {
      points: pts.map(p => ({ x: p.x, y: p.y, xerr: p.xerr ?? null, yerr: p.yerr ?? null, grb_type: p.grb_type ?? null })),
      sigma_int: state.sigmaInt,
    });
  } catch {
    fitFailed = true;   // 接口不可用/请求失败 → 本地 OLS 回退
  }
  if (fitTokens[name] !== token) return;   // 等待期间又有新的重建请求，丢弃本次结果
  if (charts[name]) { charts[name].destroy(); charts[name] = null; }
  const srvXLog = fitResp ? fitResp.x_log !== false : xLog;

  // group_by_type 时 I / II 分别拟合，否则拟合全部点
  const fitTargets = def.group_by_type
    ? [['I', groups.I, FIT_COLORS.I], ['II', groups.II, FIT_COLORS.II]]
    : [['all', pts, FIT_COLORS.all]];
  const fitTexts = [];
  const fitSets = [];   // 拟合线数据集引用，供按当前 x 显示范围重绘
  for (const [gname, gpts, color] of fitTargets) {
    const lpts = gpts.map(toLog);
    const prefix = def.group_by_type ? `${TYPE_NAMES[gname]}: ` : '';
    if (lpts.length < 3) { fitTexts.push(`${prefix}点数不足（<3），无法拟合`); continue; }
    const srv = fitResp && fitResp.groups ? fitResp.groups[gname] : null;
    let a, b, seFn, xLogFit, lxs, text;
    if (srv && !srv.error) {
      // 服务端最大似然结果（log10 空间，x 是否取 log 由 x_log 告知）
      a = srv.slope; b = srv.intercept;
      const ae = srv.slope_err ?? 0, be = srv.intercept_err ?? 0, cov = srv.cov_ab ?? 0;
      // 1σ 置信带半宽：σ_fit(X) = sqrt([X,1]·C·[X,1]ᵀ)
      seFn = lx => Math.sqrt(Math.max(ae * ae * lx * lx + 2 * cov * lx + be * be, 0));
      xLogFit = srvXLog;
      lxs = gpts.filter(p => p.x != null && (!srvXLog || p.x > 0))
        .map(p => srvXLog ? Math.log10(p.x) : p.x);
      const r = pearsonR(lpts);
      text = `${prefix}y = (${sci3(a)}±${sci3(ae)})·x ${b >= 0 ? '+' : '−'} (${sci3(Math.abs(b))}±${sci3(be)}), `
        + `σ_int=${sci3(srv.sigma_int)}, N=${srv.N ?? lpts.length}${r != null ? `, r=${r.toFixed(3)}` : ''} (log10 空间)`;
    } else if (srv && srv.error) {
      fitTexts.push(`${prefix}服务端拟合：${srv.error === 'insufficient' ? '点数不足，无法拟合' : srv.error}`);
      continue;
    } else {
      // 服务端未返回该组（请求失败或缺组）→ 本地 OLS 回退
      const fit = olsFit(lpts, false);
      if (!fit) { fitTexts.push(`${prefix}点数不足（<3），无法拟合`); continue; }
      a = fit.a; b = fit.b;
      // 1σ 置信带：SE(x) = s·sqrt(1/N + (x-x̄)²/Sxx)
      seFn = lx => fit.s * Math.sqrt(1 / fit.N + (lx - fit.xbar) ** 2 / fit.Sxx);
      xLogFit = xLog;
      lxs = lpts.map(p => p.lx);
      text = `${prefix}y = ${a.toFixed(2)}·x ${b >= 0 ? '+' : '−'} ${Math.abs(b).toFixed(2)} (log10 空间), N=${fit.N}, r=${fit.r.toFixed(3)}（本地OLS回退）`;
    }
    if (!lxs.length) { fitTexts.push(`${prefix}点数不足，无法拟合`); continue; }
    // 拟合线（覆盖该组数据 x 范围，留 5% 边距）
    const lo = Math.min(...lxs), hi = Math.max(...lxs);
    const pad = (hi - lo) * 0.05 || 0.1;
    const linePts = [];
    for (let i = 0; i <= 50; i++) {
      const lx = lo - pad + (hi - lo + 2 * pad) * i / 50;
      linePts.push({ lx, ly: a * lx + b });
    }
    // log 空间坐标换回线性坐标
    const toXY = q => ({ x: xLogFit ? 10 ** q.lx : q.lx, y: 10 ** q.ly });
    const label = def.group_by_type
      ? `${TYPE_NAMES[gname]}拟合 (a=${a.toFixed(2)})`
      : `拟合 (a=${a.toFixed(2)})`;
    const band = linePts.map(q => {
      const se = seFn(q.lx);
      return { lo: { lx: q.lx, ly: q.ly - se }, hi: { lx: q.lx, ly: q.ly + se } };
    });
    const lineDs = {
      label, _hideLegend: false, type: 'line',
      data: linePts.map(toXY),
      borderColor: color, borderWidth: 2, pointRadius: 0, fill: false,
    };
    const loDs = {
      label: `${label} 1σ`, _hideLegend: true, type: 'line',
      data: band.map(q => toXY(q.lo)),
      borderColor: 'transparent', pointRadius: 0, fill: false,
    };
    const hiDs = {
      label: `${label} 1σ+`, _hideLegend: true, type: 'line',
      data: band.map(q => toXY(q.hi)),
      borderColor: 'transparent', pointRadius: 0,
      backgroundColor: color + '33', fill: '-1',
    };
    datasets.push(lineDs, loDs, hiDs);
    fitSets.push({ a, b, seFn, xLogFit, lineDs, loDs, hiDs });
    fitTexts.push(text);
  }
  if (fitFailed) fitTexts.unshift('服务端拟合接口不可用，已回退本地 OLS');

  // ── 文献参考虚线（过当前数据 x̄,ȳ 中点、斜率 = lit.slope） ──
  let litSet = null;
  if (def.lit && def.lit.slope != null) {
    const lpts = pts.map(toLog);
    const xbar = lpts.reduce((s, p) => s + p.lx, 0) / lpts.length;
    const ybar = lpts.reduce((s, p) => s + p.ly, 0) / lpts.length;
    const lxs = lpts.map(p => p.lx);
    const lo = Math.min(...lxs), hi = Math.max(...lxs);
    const pad = (hi - lo) * 0.05 || 0.1;
    const data = [];
    for (let i = 0; i <= 50; i++) {
      const lx = lo - pad + (hi - lo + 2 * pad) * i / 50;
      data.push({ x: xLog ? 10 ** lx : lx, y: 10 ** (ybar + def.lit.slope * (lx - xbar)) });
    }
    const litDs = {
      label: def.lit.label || '文献', _hideLegend: false, type: 'line',
      data, borderColor: '#d29922', borderWidth: 1.5, borderDash: [6, 4],
      pointRadius: 0, fill: false,
    };
    datasets.push(litDs);
    litSet = { slope: def.lit.slope, xbar, ybar, xLog, ds: litDs };
  }

  // ── 特殊标记层：最后压栈 → 置顶绘制，星形大点 + 高对比描边 ──
  if (hlPts.length) {
    datasets.push({
      label: `特殊标记 (${hlPts.length})`,
      _isScatter: true,
      _noErrBar: true,
      _isHighlight: true,     // 点形由 highlightStarPlugin 画实心五角星
      data: hlPts,
      radius: 0,              // 隐藏默认点形（tooltip/点击靠 hitRadius）
      hitRadius: 12,
      hoverRadius: 0,
      backgroundColor: cc.highlight,
      borderColor: cc.highlightEdge,
    });
  }

  const gridColor = cc.gridSoft, tickColor = cc.tick;
  const fonts = academicFonts();  // 学术风格：衬线字体（卡片图较小，字号略缩）
  charts[name] = new Chart(canvas, {
    type: 'scatter',
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      onClick(evt, elements, chart) {
        // 点击散点 → 排除该点并重新拟合
        const els = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, true);
        if (!els.length) return;
        const { datasetIndex, index } = els[0];
        const ds = chart.data.datasets[datasetIndex];
        if (!ds._isScatter) return;
        const id = ds.data[index].id;
        getExcluded(name).add(id);
        buildChart(name);
      },
      plugins: {
        legend: {
          labels: {
            color: tickColor, boxWidth: 12, font: { ...fonts.legend, size: 11 },
            filter: item => !datasets[item.datasetIndex]?._hideLegend,
          },
        },
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          filter: item => datasets[item.datasetIndex]?._isScatter,
          callbacks: {
            title: items => items[0]?.raw?.id || '',
            label: item => {
              const p = item.raw;
              const fmtE = e => {
                const ne = normErr(e);
                return ne ? (ne[0] === ne[1] ? `±${sci3(ne[0])}` : `+${sci3(ne[0])}/−${sci3(ne[1])}`) : '-';
              };
              const src = p.src ? `${p.src.x || '-'}/${p.src.y || '-'}` : '-';
              return [
                `${def.x.label} = ${sci3(p.x)} ${def.x.unit || ''} (${fmtE(p.xerr)})`,
                `${def.y.label} = ${sci3(p.y)} ${def.y.unit || ''} (${fmtE(p.yerr)})`,
                `类型: ${TYPE_NAMES[p.grb_type] || '未知'}  z: ${p.z ?? '-'}`,
                `来源: ${src}`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          type: xLog ? 'logarithmic' : 'linear',
          title: { display: true, text: `${def.x.label}${def.x.unit ? ` (${def.x.unit})` : ''}`, color: tickColor, font: { ...fonts.title, size: 12 } },
          ticks: { color: tickColor, font: { ...fonts.tick, size: 11 } }, grid: { color: gridColor },
          border: { color: tickColor },
        },
        y: {
          type: 'logarithmic',
          title: { display: true, text: `${def.y.label}${def.y.unit ? ` (${def.y.unit})` : ''}`, color: tickColor, font: { ...fonts.title, size: 12 } },
          ticks: { color: tickColor, font: { ...fonts.tick, size: 11 } }, grid: { color: gridColor },
          border: { color: tickColor },
        },
      },
    },
    plugins: [errorBarPlugin, highlightStarPlugin],
  });
  charts[name]._fitSets = fitSets;
  charts[name]._litSet = litSet;
  // 应用已记忆的坐标范围（无则保持自动），并让拟合线/文献线跟随当前 x 显示范围
  applyRangeToChart(name);

  fitEl.innerHTML = fitTexts.length
    ? fitTexts.map(t => `<div><i class="bi bi-graph-up"></i> ${t}</div>`).join('')
    : '<div class="text-secondary">点数不足（<3），无法拟合</div>';
  renderExcludedList(name);
}

// ─── 已排除列表（卡片下方，可逐个恢复 / 清空） ───
function renderExcludedList(name) {
  const el = document.getElementById(`excluded-${name}`);
  if (!el) return;
  const excl = [...getExcluded(name)];
  if (!excl.length) { el.innerHTML = ''; return; }
  el.innerHTML = `<div class="d-flex flex-wrap align-items-center gap-1">
    <span class="text-secondary me-1">已排除:</span>
    ${excl.map(id => `<span class="badge bg-secondary" style="cursor:pointer" data-restore="${id}" title="点击恢复">${id} <i class="bi bi-x"></i></span>`).join('')}
    <button class="btn btn-sm btn-outline-secondary py-0 ms-1" data-clear="${name}">清空排除</button>
  </div>`;
  el.querySelectorAll('[data-restore]').forEach(b => {
    b.onclick = () => { getExcluded(name).delete(b.dataset.restore); buildChart(name); };
  });
  el.querySelector('[data-clear]').onclick = () => { getExcluded(name).clear(); buildChart(name); };
}

// ─── 坐标范围：应用 / 恢复默认 / 拟合线跟随显示范围重绘 ───
// 把已记忆的范围写入 chart.options.scales（null 端删除 → 自动），再重绘拟合线
function applyRangeToChart(name) {
  const chart = charts[name];
  if (!chart) return;
  const r = getRange(name);
  for (const axis of ['x', 'y']) {
    const sc = chart.options.scales[axis];
    const mn = r ? r[`${axis}min`] : null;
    const mx = r ? r[`${axis}max`] : null;
    if (mn != null) sc.min = mn; else delete sc.min;
    if (mx != null) sc.max = mx; else delete sc.max;
  }
  chart.update('none');
  reflowFitLines(name);
}

// 拟合线、置信带、文献线按当前 x 显示范围重新采样（y 超出显示范围的点置 NaN 截断）
function reflowFitLines(name) {
  const chart = charts[name];
  if (!chart) return;
  const xs = chart.scales.x, ys = chart.scales.y;
  if (!xs || !ys || xs.min == null || !(ys.min > 0)) return;   // y 一律 log 轴，min 必为正
  const yLo = Math.log10(ys.min), yHi = Math.log10(ys.max);
  const clipY = ly => (ly < yLo || ly > yHi) ? NaN : 10 ** ly;   // 出界置 NaN → 线在边缘断开而非走平
  // log 轴的 min 必为正；防御一下避免 log10(≤0)
  const xToLog = v => Math.log10(Math.max(v, Number.MIN_VALUE));
  for (const fs of chart._fitSets || []) {
    const xLo = fs.xLogFit ? xToLog(xs.min) : xs.min;
    const xHi = fs.xLogFit ? xToLog(xs.max) : xs.max;
    const toXY = (lx, ly) => ({ x: fs.xLogFit ? 10 ** lx : lx, y: clipY(ly) });
    const line = [], lo = [], hi = [];
    for (let i = 0; i <= 50; i++) {
      const lx = xLo + (xHi - xLo) * i / 50;
      const ly = fs.a * lx + fs.b;
      const se = fs.seFn(lx);
      line.push(toXY(lx, ly));
      lo.push(toXY(lx, ly - se));
      hi.push(toXY(lx, ly + se));
    }
    fs.lineDs.data = line;
    fs.loDs.data = lo;
    fs.hiDs.data = hi;
  }
  const L = chart._litSet;
  if (L) {
    const xLo = L.xLog ? xToLog(xs.min) : xs.min;
    const xHi = L.xLog ? xToLog(xs.max) : xs.max;
    const data = [];
    for (let i = 0; i <= 50; i++) {
      const lx = xLo + (xHi - xLo) * i / 50;
      data.push({ x: L.xLog ? 10 ** lx : lx, y: clipY(L.ybar + L.slope * (lx - L.xbar)) });
    }
    L.ds.data = data;
  }
  chart.update('none');
}

// 读取卡片上的四个输入框，校验后写入记忆并应用；留空的端保持自动
function applyRangeFromInputs(name) {
  const def = relDefs.find(r => r.name === name);
  const xLog = def ? def.x_log !== false : true;
  const r = {};
  for (const k of ['xmin', 'xmax', 'ymin', 'ymax']) {
    const raw = (document.getElementById(`rg-${k}-${name}`)?.value || '').trim();
    if (!raw) { r[k] = null; continue; }
    const v = Number(raw);
    const needPos = k[0] === 'y' || xLog;   // log 轴只接受正值（y 一律 log）
    if (!isFinite(v) || (needPos && v <= 0)) {
      showToast(`${k} 无效：需为${needPos ? '正' : ''}数字（log 轴填线性值）`, 'warning');
      return;
    }
    r[k] = v;
  }
  if (r.xmin != null && r.xmax != null && r.xmin >= r.xmax) { showToast('xmin 必须小于 xmax', 'warning'); return; }
  if (r.ymin != null && r.ymax != null && r.ymin >= r.ymax) { showToast('ymin 必须小于 ymax', 'warning'); return; }
  state.ranges[exclKey(name)] = r;
  applyRangeToChart(name);
}

// 恢复默认：删除记忆、清空输入框、回到自动范围
function resetRange(name) {
  delete state.ranges[exclKey(name)];
  fillRangeInputs(name);
  applyRangeToChart(name);
}

// 用已记忆的范围回填输入框（无则清空）
function fillRangeInputs(name) {
  const r = getRange(name);
  for (const k of ['xmin', 'xmax', 'ymin', 'ymax']) {
    const el = document.getElementById(`rg-${k}-${name}`);
    if (el) el.value = r && r[k] != null ? r[k] : '';
  }
}

// ─── 导出 CSV（当前参与显示的点） ───
function exportCsv(name) {
  const pts = visiblePoints(name);
  if (!pts.length) { showToast('没有可导出的数据点', 'warning'); return; }
  const esc = v => {
    const s = v == null ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const rows = ['id,x,xerr,y,yerr,grb_type,z,src'];
  for (const p of pts) {
    const errStr = e => Array.isArray(e) ? `[${e[0]},${e[1]}]` : (e ?? '');
    rows.push([p.id, p.x, errStr(p.xerr), p.y, errStr(p.yerr), p.grb_type ?? '', p.z ?? '',
      p.src ? `x:${p.src.x};y:${p.src.y}` : ''].map(esc).join(','));
  }
  const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${name}_${state.source}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ─── 拉取当前来源下全部关系数据 ───
async function fetchAllData() {
  rawPoints = {};
  await Promise.all(relDefs.map(async def => {
    try {
      const d = await api('GET', `/relations/${def.name}/data?source=${encodeURIComponent(state.source)}`);
      rawPoints[def.name] = d.points || [];
    } catch {
      rawPoints[def.name] = [];   // 该来源对此关系无数据
    }
  }));
}

// ─── 重建 tag 多选列表（选项来自当前已加载数据点） ───
function rebuildTagList() {
  const box = document.getElementById('tagCheckboxList');
  if (!box) return;
  const tagSet = new Set();
  for (const pts of Object.values(rawPoints)) {
    for (const p of pts) (p.tags || []).forEach(t => tagSet.add(t));
  }
  const tags = [...tagSet].sort();
  box.innerHTML = tags.length
    ? tags.map(t => `<label class="dropdown-item d-flex align-items-center gap-2 py-1 mb-0" style="cursor:pointer">
        <input type="checkbox" class="form-check-input m-0" value="${t}" ${state.tags.has(t) ? 'checked' : ''}> ${t}
      </label>`).join('')
    : '<span class="dropdown-item text-secondary small">无标签</span>';
  box.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.onchange = () => {
      if (cb.checked) state.tags.add(cb.value);
      else state.tags.delete(cb.value);
      updateTagBtnLabel();
      rebuildAllCharts();
    };
  });
  updateTagBtnLabel();
}

function updateTagBtnLabel() {
  const btn = document.getElementById('tagFilterBtn');
  if (btn) btn.textContent = state.tags.size ? `标签筛选 (${state.tags.size})` : '标签筛选 (全部)';
}

function rebuildAllCharts() {
  for (const def of relDefs) buildChart(def.name);
}

// ─── 主渲染 ───
export async function render() {
  showLoading();
  try {
    const d = await api('GET', '/relations');
    relDefs = d.relations || [];
    relSources = d.sources || {};
  } catch (err) {
    showError(`加载统计关系失败: ${err.message}`);
    return;
  }

  // 全局来源下拉：best + 各关系来源的并集
  const allSources = ['best', ...new Set(Object.values(relSources).flat().filter(s => s !== 'best'))];
  if (!allSources.includes(state.source)) state.source = 'best';

  app.innerHTML = `
    <div class="page-header d-flex justify-content-between align-items-center">
      <h4 class="mb-0"><i class="bi bi-bar-chart-line"></i> 全局统计</h4>
    </div>
    ${statsTabs('relations')}

    <!-- 控制栏 -->
    <div class="card mb-3">
      <div class="card-body py-2 d-flex flex-wrap align-items-center gap-3">
        <div class="d-flex align-items-center gap-1">
          <label class="small text-secondary">数据来源:</label>
          <select class="form-select form-select-sm" style="width:auto" id="sourceSelect">
            ${allSources.map(s => `<option value="${s}" ${s === state.source ? 'selected' : ''}>${s}</option>`).join('')}
          </select>
        </div>
        <div class="dropdown">
          <button class="btn btn-sm btn-outline-secondary dropdown-toggle" data-bs-toggle="dropdown"
                  data-bs-auto-close="outside" id="tagFilterBtn">标签筛选 (全部)</button>
          <div class="dropdown-menu p-2" id="tagCheckboxList" style="max-height:300px;overflow-y:auto"></div>
        </div>
        <div class="d-flex align-items-center gap-1">
          <label class="small text-secondary">特殊标记:</label>
          <input type="text" class="form-control form-control-sm" style="width:220px"
                 id="highlightInput" placeholder="GRB030329A, GRB170817A"
                 value="${[...state.highlight].join(', ')}">
        </div>
        <div class="form-check form-switch mb-0">
          <input class="form-check-input" type="checkbox" id="sigmaIntCheck" ${state.sigmaInt ? 'checked' : ''}>
          <label class="form-check-label small" for="sigmaIntCheck">含内禀弥散 σ_int</label>
        </div>
      </div>
    </div>

    <!-- 关系卡片：响应式 2 列网格 -->
    <div class="row g-3" id="relCards">
      ${relDefs.map(def => `
        <div class="col-lg-6">
          <div class="card" id="card-${def.name}">
            <div class="card-header d-flex justify-content-between align-items-center">
              <span>${def.title}${def.lit && def.lit.label ? ` · <span class="text-secondary small">${def.lit.label}${def.lit.bibcode ? ` (${def.lit.bibcode})` : ''}</span>` : ''}</span>
              <div class="d-flex gap-1">
                <button class="btn btn-sm btn-outline-secondary py-0" data-range-toggle="${def.name}" title="坐标范围">
                  <i class="bi bi-gear"></i>
                </button>
                <button class="btn btn-sm btn-outline-secondary py-0" data-export="${def.name}">
                  <i class="bi bi-download"></i> 导出 CSV
                </button>
              </div>
            </div>
            <div class="card-body">
              <!-- 坐标范围控件：log 轴填线性值，留空 = 自动 -->
              <div class="mb-2" id="range-${def.name}" style="display:none">
                <div class="d-flex flex-wrap align-items-center gap-1 small">
                  <span class="text-secondary">x${def.x.unit ? ` (${def.x.unit})` : ''}:</span>
                  <input type="number" step="any" class="form-control form-control-sm" style="width:105px" id="rg-xmin-${def.name}" placeholder="xmin">
                  <span class="text-secondary">~</span>
                  <input type="number" step="any" class="form-control form-control-sm" style="width:105px" id="rg-xmax-${def.name}" placeholder="xmax">
                  <span class="text-secondary ms-1">y${def.y.unit ? ` (${def.y.unit})` : ''}:</span>
                  <input type="number" step="any" class="form-control form-control-sm" style="width:105px" id="rg-ymin-${def.name}" placeholder="ymin">
                  <span class="text-secondary">~</span>
                  <input type="number" step="any" class="form-control form-control-sm" style="width:105px" id="rg-ymax-${def.name}" placeholder="ymax">
                  <button class="btn btn-sm btn-outline-primary py-0 ms-1" data-range-apply="${def.name}">应用</button>
                  <button class="btn btn-sm btn-outline-secondary py-0" data-range-reset="${def.name}">恢复默认</button>
                </div>
                <div class="text-secondary" style="font-size:0.72rem">log 轴填原始线性值（如 1e50），留空的端保持自动</div>
              </div>
              <div class="chart-container" style="height:420px" id="chartbox-${def.name}">
                <canvas id="cv-${def.name}"></canvas>
              </div>
              <div class="text-center text-secondary py-4" id="empty-${def.name}" style="display:none">加载中...</div>
              <div class="small mt-2" id="fitinfo-${def.name}"></div>
              <div class="small mt-1" id="excluded-${def.name}"></div>
              ${Array.isArray(def.refs) && def.refs.length ? `
              <div class="text-secondary mt-2" style="font-size:0.78rem">参考文献: ${def.refs.map(r =>
                r && r.url
                  ? `<a href="${r.url}" target="_blank" rel="noopener noreferrer" class="link-secondary">${r.label || r.url}</a>`
                  : (r && r.label) || ''
              ).filter(Boolean).join(' · ')}</div>` : ''}
            </div>
          </div>
        </div>`).join('')}
    </div>
  `;

  // 控件事件
  document.getElementById('sourceSelect').onchange = async e => {
    state.source = e.target.value;
    await fetchAllData();
    rebuildTagList();
    rebuildAllCharts();
  };
  let hlTimer = null;
  document.getElementById('highlightInput').oninput = e => {
    clearTimeout(hlTimer);
    hlTimer = setTimeout(() => {
      // 逗号分隔，容忍空格写法
      state.highlight = new Set(
        e.target.value.split(',').map(s => s.toUpperCase().replace(/\s+/g, '')).filter(Boolean)
      );
      rebuildAllCharts();
    }, 300);
  };
  document.getElementById('sigmaIntCheck').onchange = e => {
    state.sigmaInt = e.target.checked;
    rebuildAllCharts();
  };
  document.querySelectorAll('[data-export]').forEach(b => {
    b.onclick = () => exportCsv(b.dataset.export);
  });
  document.querySelectorAll('[data-range-toggle]').forEach(b => {
    b.onclick = () => {
      const el = document.getElementById(`range-${b.dataset.rangeToggle}`);
      if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
    };
  });
  document.querySelectorAll('[data-range-apply]').forEach(b => {
    b.onclick = () => applyRangeFromInputs(b.dataset.rangeApply);
  });
  document.querySelectorAll('[data-range-reset]').forEach(b => {
    b.onclick = () => resetRange(b.dataset.rangeReset);
  });
  relDefs.forEach(def => fillRangeInputs(def.name));   // 回填已记忆的范围

  await fetchAllData();
  rebuildTagList();
  rebuildAllCharts();
}
