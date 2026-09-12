// === 光变曲线图功能区（详情页「光变曲线」标签） ===
// 图表构建/重建、误差棒与顶部副轴插件、波段勾选面板、手动坐标范围、简易模型拟合叠加。
// 状态由 detail.js render() 经 resetLCChart()/setLCSourceParams() 重置与注入；
// window.* 全局入口依赖每轮 render 的 bands/bandNames/spectralColors，由 wireLCChartGlobals() 重挂。
import { showToast } from '../api.js';
import { fitLightcurveModel } from '../api.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { chartColors, ACADEMIC_FONT, academicFonts } from '../theme.js';
import { mJyToMagAB, sortBandsByFreq, pointToMJy } from '../bands.js';
import { esc, escAttr, sciFormat, sig3, minOf, maxOf } from '../utils.js';
import { createYErrBarPlugin } from '../chart_plugins.js';

let lcChartInstance = null;
const lcChartHolder = { chart: null };  // 框选缩放用的当前图表引用

// ─── 光变图手动坐标范围（null = 该端自动） ───
let lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
// ─── 叠加在光变图上的拟合曲线 ───
let lcFits = [];
const FIT_COLORS = chartColors().fits;   // 拟合线配色（随主题；切换主题后 reload 生效）
const FIT_MODEL_NAMES = { pl: 'powerlaw', bpl: 'broken-powerlaw', sbpl: 'smoothly-broken-powerlaw' };
// ─── 当前源的红移/T0/距离模数（光变图静止系、顶部 MJD 轴、绝对星等用） ───
let _lcRedshift = null;
let _lcT0MJD = null;   // T0 对应的 MJD（无 T0 时为 null）
let _lcDistmod = null;
// ─── 光变图波段可见性（勾选框面板控制，默认全显示） ───
let lcBandVisible = {};  // band → bool
// ─── 光变图顶部副轴配置（null = 不画） ───
let _lcTopAxis = null;   // { mode: 'day'|'mjd' }
// ─── 当前图数据源（wireLCChartGlobals 每轮 render 登记，供数据表行内编辑后同步） ───
let _bands = null, _bandNames = null, _spectralColors = null;

// T0 (naive UTC ISO 字符串) → MJD
export function t0ToMJD(t0) {
  if (!t0) return null;
  const s = String(t0);
  const iso = s.includes('T') ? s : s + 'T00:00:00';
  const ms = Date.parse(iso.endsWith('Z') ? iso : iso + 'Z');
  if (!isFinite(ms)) return null;
  return ms / 86400000 + 40587;  // Unix epoch = MJD 40587
}

// render() 获取源数据后调用：注入光变图用的源级参数
export function setLCSourceParams({ redshift, t0, distmod }) {
  _lcRedshift = (redshift != null && redshift > -1) ? redshift : null;
  _lcT0MJD = t0ToMJD(t0);
  _lcDistmod = (distmod != null) ? distmod : null;
}

// render() 重建 DOM 前调用：销毁图表并重置全部图状态
export function resetLCChart() {
  if (lcChartInstance) { try { lcChartInstance.destroy(); } catch {} lcChartInstance = null; }
  lcFits = [];
  lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  lcBandVisible = {};
  lcShowErr = true;
  _lcTopAxis = null;
  _lcRedshift = null;
  _lcT0MJD = null;
  _lcDistmod = null;
  _bands = null;
  _bandNames = null;
  _spectralColors = null;
}

// ─── 拟合模型求值（mJy 空间；与后端 routes/lightcurves.py fit_model 严格镜像） ───
function fitModelFlux(model, prm, t) {
  if (model === 'pl') return prm.A * Math.pow(t, -prm.alpha);
  if (model === 'sbpl') {
    // F = Fb·[(t/tb)^(n·α1)+(t/tb)^(n·α2)]^(-1/n)，Fb = A·tb^(-α1)；logaddexp 保证数值稳定
    const Fb = prm.A * Math.pow(prm.tb, -prm.alpha1);
    const lnx = Math.log(t / prm.tb);
    const e1 = prm.n * prm.alpha1 * lnx, e2 = prm.n * prm.alpha2 * lnx;
    const m = Math.max(e1, e2);
    return Fb * Math.exp(-(m + Math.log(Math.exp(e1 - m) + Math.exp(e2 - m))) / prm.n);
  }
  // bpl: tb 处连续
  return t <= prm.tb
    ? prm.A * Math.pow(t, -prm.alpha1)
    : prm.A * Math.pow(prm.tb, prm.alpha2 - prm.alpha1) * Math.pow(t, -prm.alpha2);
}

function makeFitLabel(fit) {
  const prm = fit.params;
  if (fit.model === 'pl') return `${fit.band} PL: α=${sig3(prm.alpha)}`;
  if (fit.model === 'sbpl') return `${fit.band} SBPL: α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s, n=${sig3(prm.n)}`;
  return `${fit.band} BPL: α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s`;
}

// ─── 错误条绘制插件（beforeDatasetsDraw：误差棒画在最底层，不遮挡数据点与拟合线） ───
let lcShowErr = true;   // 是否绘制误差棒（图头「误差棒」开关）
const errorBarPlugin = createYErrBarPlugin({
  enabled: () => lcShowErr,
  errOf: (ds, raw, i) => ds._errorValues ? ds._errorValues[i] : null,
  skipDataset: ds => ds._isUpperLimit || ds._isFit,
});

// ─── 光变图顶部副轴（day / MJD）───
// 每帧根据主横轴当前范围在显示单位内取 1-2-5 规整刻度，再换算回秒定位像素
function _niceTicks(lo, hi, target) {
  if (!(hi > lo)) return { ticks: [], step: 1 };
  const raw = (hi - lo) / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  let step = 10 * mag;
  for (const m of [1, 2, 5, 10]) { if (raw <= m * mag) { step = m * mag; break; } }
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) ticks.push(v);
  return { ticks, step };
}

const lcTopAxisPlugin = {
  id: 'lcTopAxis',
  afterDraw(chart) {
    const cfg = _lcTopAxis;
    if (!cfg || !cfg.mode) return;
    const x = chart.scales.x;
    const area = chart.chartArea;
    if (!x || !area) return;
    // day = t/86400；mjd = T0(MJD) + t/86400
    const toDisp = cfg.mode === 'mjd' ? (t) => cfg.t0mjd + t / 86400 : (t) => t / 86400;
    const fromDisp = cfg.mode === 'mjd' ? (d) => (d - cfg.t0mjd) * 86400 : (d) => d * 86400;
    const { ticks, step } = _niceTicks(toDisp(x.min), toDisp(x.max), 5);
    const dec = step >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log10(step)));
    const fmt = cfg.mode === 'mjd'
      ? (v) => v.toFixed(Math.max(dec, 1))
      : (v) => String(parseFloat(v.toFixed(Math.max(dec, 2))));
    const cc = chartColors();
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = cc.tick;
    ctx.fillStyle = cc.tick;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(area.left, area.top);
    ctx.lineTo(area.right, area.top);
    ctx.stroke();
    ctx.font = `11px ${ACADEMIC_FONT}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    for (const d of ticks) {
      const px = x.getPixelForValue(fromDisp(d));
      if (px < area.left + 2 || px > area.right - 2) continue;
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.top - 5);
      ctx.stroke();
      ctx.fillText(fmt(d), px, area.top - 7);
    }
    ctx.fillText(cfg.mode === 'mjd' ? 'MJD' : 'time since T0 (day)', (area.left + area.right) / 2, area.top - 24);
    ctx.restore();
  },
};

// ─── 光变图波段勾选面板（替代内置图例的划线开关） ───
function applyBandVisibility() {
  const chart = lcChartInstance;
  if (!chart) return;
  chart.data.datasets.forEach((ds, i) => {
    if (ds._isFit || ds._band == null) return;
    chart.setDatasetVisibility(i, lcBandVisible[ds._band] !== false);
  });
  chart.update();
}

function buildBandPanel(sortedBands, spectralColors) {
  const el = document.getElementById('lcBandPanel');
  if (!el) return;
  el.innerHTML = `<span class="text-secondary">波段:</span>` + sortedBands.map((b, i) => {
    const vis = lcBandVisible[b] !== false;
    const c = spectralColors[b] || '#58a6ff';
    return `<span class="form-check form-check-inline mb-0">
      <input class="form-check-input lc-band-chk" type="checkbox" id="lcBandChk_${i}" data-band="${escAttr(b)}" ${vis ? 'checked' : ''}>
      <label class="form-check-label" for="lcBandChk_${i}"><span style="color:${c}">●</span> ${esc(b)}</label>
    </span>`;
  }).join('') + `
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="lcBandAll">全选</button>
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="lcBandNone">全不选</button>`;
  el.querySelectorAll('.lc-band-chk').forEach(chk => {
    chk.addEventListener('change', () => {
      lcBandVisible[chk.dataset.band] = chk.checked;
      applyBandVisibility();
    });
  });
  document.getElementById('lcBandAll')?.addEventListener('click', () => {
    for (const b of sortedBands) lcBandVisible[b] = true;
    el.querySelectorAll('.lc-band-chk').forEach(c => { c.checked = true; });
    applyBandVisibility();
  });
  document.getElementById('lcBandNone')?.addEventListener('click', () => {
    for (const b of sortedBands) lcBandVisible[b] = false;
    el.querySelectorAll('.lc-band-chk').forEach(c => { c.checked = false; });
    applyBandVisibility();
  });
}

// ─── 拟合列表 UI ───
function renderFitList() {
  const el = document.getElementById('lcFitList');
  if (!el) return;
  if (lcFits.length === 0) { el.innerHTML = ''; return; }
  el.innerHTML = lcFits.map((fit, i) => {
    const prm = fit.params;
    const ptxt = fit.model === 'pl'
      ? `α=${sig3(prm.alpha)}`
      : fit.model === 'sbpl'
        ? `α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s, n=${sig3(prm.n)}`
        : `α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s`;
    const prange = (fit.pmin !== fit.tmin || fit.pmax !== fit.tmax)
      ? ` · 绘制[${sciFormat(fit.pmin)}, ${sciFormat(fit.pmax)}]s` : '';
    return `<div class="d-flex align-items-center gap-2 mb-1">
      <span style="display:inline-block;width:24px;border-top:2px dashed ${fit.color}"></span>
      <span>${FIT_MODEL_NAMES[fit.model] || fit.model} · ${esc(fit.band)} · 拟合[${sciFormat(fit.tmin)}, ${sciFormat(fit.tmax)}]s${prange} · ${ptxt} · N=${fit.N}</span>
      <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="removeLCFit(${i})" title="删除该拟合"><i class="bi bi-trash"></i></button>
    </div>`;
  }).join('');
}

// ─── 光变图初始化 / 重建 ───
export function initLCPlot(bands, bandNames, spectralColors) {
  if (lcChartInstance) return;
  if (typeof Chart === 'undefined') {
    console.error('Chart.js not loaded');
    const canvas = document.getElementById('lcChart');
    if (canvas) {
      const cc = chartColors();
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = cc.canvasBg;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = cc.tick;
      ctx.font = '14px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Chart.js 加载失败，请检查网络连接', canvas.width/2, canvas.height/2);
    }
    return;
  }
  buildLCChart(bands, bandNames, spectralColors);
}

function rebuildLCPlot(bands, bandNames, spectralColors) {
  if (lcChartInstance) {
    lcChartInstance.destroy();
    lcChartInstance = null;
  }
  buildLCChart(bands, bandNames, spectralColors);
}

// ─── 全局入口接线（每轮 render 以最新的 bands/bandNames/spectralColors 重挂） ───
export function wireLCChartGlobals(bands, bandNames, spectralColors) {
  _bands = bands;
  _bandNames = bandNames;
  _spectralColors = spectralColors;
  window.rebuildLCPlot = () => rebuildLCPlot(bands, bandNames, spectralColors);
  window.resetLCZoom = () => { if (lcChartInstance) lcChartInstance.resetZoom(); };
  window.lcShowErrToggle = (on) => {   // 误差棒开关：只影响绘制，不动数据，无动画重绘即可
    lcShowErr = on;
    if (lcChartInstance) lcChartInstance.update('none');
  };

  // ── 坐标范围手动调节 ──
  window.applyLCAxisRange = () => {
    const v = (id) => {
      const el = document.getElementById(id);
      const s = el ? el.value.trim() : '';
      if (s === '') return null;
      const n = parseFloat(s);
      return isFinite(n) ? n : null;
    };
    lcAxisRange = { xmin: v('axXmin'), xmax: v('axXmax'), ymin: v('axYmin'), ymax: v('axYmax') };
    rebuildLCPlot(bands, bandNames, spectralColors);
  };
  window.resetLCAxisRange = () => {
    lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
    ['axXmin', 'axXmax', 'axYmin', 'axYmax'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    rebuildLCPlot(bands, bandNames, spectralColors);
  };

  // ── 光变曲线拟合 ──
  window.fitModelChanged = () => {
    const model = document.getElementById('fitModel')?.value;
    const rng = document.getElementById('fitTbRange');
    if (rng) rng.style.display = (model === 'bpl' || model === 'sbpl') ? 'inline-flex' : 'none';
  };
  window.addLCFit = async () => {
    const model = document.getElementById('fitModel')?.value || 'pl';
    const band = document.getElementById('fitBand')?.value;
    const tminIn = parseFloat(document.getElementById('fitTmin')?.value);
    const tmaxIn = parseFloat(document.getElementById('fitTmax')?.value);
    const useGext = document.getElementById('gextMode')?.value === 'gext';
    // 该波段 + 时间范围内的探测点（排除 discard 与上限点），统一换算到 mJy 空间
    const pts = (bands[band] || [])
      .filter(p => !p.discard && !p.upperlimit)
      .map(p => {
        const m = pointToMJy(p, useGext);
        return m ? { t: p.time, f: m.y, ferr: m.err } : null;
      })
      .filter(d => d && d.t > 0 && d.f > 0
        && (isNaN(tminIn) || d.t >= tminIn)
        && (isNaN(tmaxIn) || d.t <= tmaxIn));
    const need = model === 'pl' ? 3 : (model === 'bpl' ? 5 : 6);
    if (pts.length < need) {
      showToast(`有效数据点不足（${FIT_MODEL_NAMES[model] || model} 需 ≥${need} 点，当前 ${pts.length} 点）`, 'warning');
      return;
    }
    // bpl/sbpl 拐点预设范围（留空端=数据范围）
    let bounds = null;
    if (model === 'bpl' || model === 'sbpl') {
      const lo = parseFloat(document.getElementById('fitTbMin')?.value);
      const hi = parseFloat(document.getElementById('fitTbMax')?.value);
      if ((isFinite(lo) || isFinite(hi))) {
        const ts = pts.map(d => d.t);
        const blo = isFinite(lo) ? lo : minOf(ts);
        const bhi = isFinite(hi) ? hi : maxOf(ts);
        if (blo < bhi) bounds = { tb: [blo, bhi] };
        else { showToast('tb 预设范围无效（tb_min 需小于 tb_max）', 'warning'); return; }
      }
    }
    try {
      const payload = { model, points: pts };
      if (bounds) payload.bounds = bounds;
      const res = await fitLightcurveModel(payload);
      const ts = pts.map(d => d.t);
      const tmin = isNaN(tminIn) ? minOf(ts) : tminIn;
      const tmax = isNaN(tmaxIn) ? maxOf(ts) : tmaxIn;
      const pminIn = parseFloat(document.getElementById('fitPmin')?.value);
      const pmaxIn = parseFloat(document.getElementById('fitPmax')?.value);
      const fit = {
        model, band,
        tmin, tmax,
        // 绘制（外推）范围：仅影响拟合线在图上的绘制区间，默认与拟合范围一致
        pmin: (isFinite(pminIn) && pminIn > 0) ? pminIn : tmin,
        pmax: (isFinite(pmaxIn) && pmaxIn > 0) ? pmaxIn : tmax,
        params: res.params,
        N: res.N,
        color: FIT_COLORS[lcFits.length % FIT_COLORS.length],
      };
      if (!(fit.pmax > fit.pmin)) {
        showToast('绘制范围无效（绘制 t_max 需大于 t_min）', 'warning');
        return;
      }
      fit.label = makeFitLabel(fit);
      lcFits.push(fit);
      renderFitList();
      rebuildLCPlot(bands, bandNames, spectralColors);
      showToast('拟合已添加', 'success');
    } catch (err) {
      showToast(`拟合失败: ${err.message}`, 'danger');
    }
  };
  window.removeLCFit = (i) => {
    lcFits.splice(i, 1);
    renderFitList();
    rebuildLCPlot(bands, bandNames, spectralColors);
  };
}

// ─── 数据表行内编辑/扣点成功后同步光变图数据源 ───
// 就地替换 bands 分组数组中的点对象（与数据表 lcItems 共享同一引用），使已叠加的
// 拟合曲线失效（数据已变）并重建当前图；图尚未创建时下次进入 tab 自然用新数据。
// 返回 false 表示无法局部同步（如新波段不在分组中，影响波段列表/配色），调用方应整页 render
export function syncLcPoint(oldPt, newPt) {
  if (!_bands) return true;
  const oldBand = oldPt && oldPt.band;
  if (oldBand && oldBand !== newPt.band) {
    const oldArr = _bands[oldBand];
    if (!oldArr) return false;
    const i = oldArr.findIndex(p => p.id === newPt.id);
    if (i >= 0) oldArr.splice(i, 1);
    if (!_bands[newPt.band]) return false;
    _bands[newPt.band].push(newPt);
  } else {
    const arr = _bands[newPt.band];
    if (!arr) return false;
    const i = arr.findIndex(p => p.id === newPt.id);
    if (i >= 0) arr[i] = newPt; else arr.push(newPt);
  }
  if (lcFits.length) { lcFits = []; renderFitList(); }
  if (lcChartInstance) rebuildLCPlot(_bands, _bandNames, _spectralColors);
  return true;
}

function buildLCChart(bands, bandNames, spectralColors) {
  const ctx = document.getElementById('lcChart');
  if (!ctx) return;
  if (typeof Chart === 'undefined') {
    console.error('Chart.js not loaded, cannot build chart');
    return;
  }

  const cc = chartColors();
  const fonts = academicFonts();
  const xType = document.getElementById('xScale')?.value || 'logarithmic';
  const useGext = document.getElementById('gextMode')?.value === 'gext';
  // 绝对星等模式（需距离模数）：y = m_AB − μ，线性反向轴
  const yMode = document.getElementById('yMode')?.value || 'flux';
  const absMag = yMode === 'absmag' && _lcDistmod != null;
  // 静止系：t/(1+z)（需红移）
  const restFrame = (document.getElementById('lcRestFrame')?.checked || false) && _lcRedshift != null;
  const zfac = restFrame ? (1 + _lcRedshift) : 1;
  // 顶部副轴
  const topMode = document.getElementById('topAxis')?.value || 'day';
  _lcTopAxis = (topMode === 'none' || (topMode === 'mjd' && _lcT0MJD == null))
    ? null
    : { mode: topMode, t0mjd: _lcT0MJD };

  // 数据默认以 mJy 绘制（左轴 log mJy；右轴由 y2 换算显示 AB 星等）；
  // 绝对星等模式下 y 为 M（mag，线性反向轴）
  const toY = (mJy) => absMag ? mJyToMagAB(mJy) - _lcDistmod : mJy;
  const toYerr = (mJy, err) => {
    if (err == null) return null;
    // 星等空间误差 σ_m = (2.5/ln10)·σ_F/F
    return absMag ? (mJy > 0 ? (2.5 / Math.LN10) * err / mJy : null) : err;
  };
  const datasets = [];

  // 按频率排序波段（与其他图表同一规则，见 js/bands.js）
  const sortedBands = sortBandsByFreq(bandNames);

  for (const band of sortedBands) {
    const pts = bands[band];
    const color = spectralColors[band] || '#58a6ff';
    const detections = pts.filter(p => !p.upperlimit);
    const upperLimits = pts.filter(p => p.upperlimit);

    // ── 探测点（带误差条） ──
    if (detections.length > 0) {
      const vals = detections.map(p => {
        const m = pointToMJy(p, useGext);
        if (!m) return null;
        return { x: p.time / zfac, y: toY(m.y), err: toYerr(m.y, m.err), raw: p, clipped: m.clipped };
      }).filter(d => d && isFinite(d.x) && isFinite(d.y)
        && !(absMag && d.clipped));  // 绝对星等模式下原始值≤0 的点无对应星等，不绘制（流量模式截断到底部并标注）

      if (vals.length > 0) {
        datasets.push({
          label: band,
          data: vals.map(d => ({ x: d.x, y: d.y })),
          backgroundColor: color,
          borderColor: color,
          pointBackgroundColor: color,
          pointBorderColor: color,
          showLine: false,
          pointRadius: 3,
          pointHoverRadius: 5,
          _errorValues: vals.map(d => d.err),
          _clippedFlags: vals.map(d => d.clipped),
          _isUpperLimit: false,
          _band: band,
        });
      }
    }

    // ── 上限点 ──
    if (upperLimits.length > 0) {
      const uv = upperLimits.map(p => {
        const m = pointToMJy(p, useGext);
        if (!m) return null;
        return { x: p.time / zfac, y: toY(m.y), raw: p, clipped: m.clipped };
      }).filter(d => d && isFinite(d.x) && isFinite(d.y)
        && !(absMag && d.clipped));  // 同探测点：绝对星等模式不绘制原始值≤0 的截断点

      if (uv.length > 0) {
        datasets.push({
          label: `${band} ↑`,
          data: uv.map(d => ({ x: d.x, y: d.y })),
          backgroundColor: color,
          borderColor: color,
          pointBackgroundColor: color,
          pointBorderColor: color,
          showLine: false,
          pointStyle: 'triangle',
          pointRadius: 5,
          pointRotation: 180,
          _errorValues: [],
          _isUpperLimit: true,
          _band: band,
        });
      }
    }
  }

  // ── 叠加拟合曲线（虚线，按绘制范围 ~120 点；order 保证画在最上层） ──
  for (const fit of lcFits) {
    const t0 = fit.pmin ?? fit.tmin, t1 = fit.pmax ?? fit.tmax;
    if (!(t0 > 0) || !(t1 > t0)) continue;
    const data = [];
    for (let i = 0; i < 120; i++) {
      const tt = t0 * Math.pow(t1 / t0, i / 119);
      const f = fitModelFlux(fit.model, fit.params, tt);
      if (!(f > 0) || !isFinite(f)) continue;
      data.push({ x: tt / zfac, y: toY(f) });
    }
    datasets.push({
      type: 'line',
      label: fit.label,
      data,
      borderColor: fit.color,
      backgroundColor: fit.color,
      borderDash: [6, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      pointHoverRadius: 0,
      fill: false,
      showLine: true,
      order: -1,   // Chart.js：order 越小越晚绘制（显示在最上层）
      _isFit: true,
    });
  }

  // ── 范围计算函数（供 afterDataLimits 使用；手动范围优先） ──
  function computeAxisRange(chart, mode) {
    const allX = [], allY = [];
    chart.data.datasets.forEach((ds, i) => {
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden) return;
      ds.data.forEach(p => {
        if (p.x > 0 && isFinite(p.x)) allX.push(p.x);
        // 绝对星等模式 y 可为负（线性轴）；流量模式仅取正值（log 轴）
        if (absMag ? isFinite(p.y) : (isFinite(p.y) && p.y > 0)) allY.push(p.y);
      });
    });
    if (mode === 'x') {
      let r;
      if (allX.length === 0) {
        r = { min: 0.1, max: 1000 };
      } else {
        const mn = minOf(allX), mx = maxOf(allX);
        if (xType === 'logarithmic') {
          r = { min: mn * 0.8, max: mx * 1.5 };
        } else {
          const pad = (mx - mn) * 0.1;
          r = { min: Math.max(0, mn - pad), max: mx + pad };
        }
      }
      // 手动范围覆盖（留空端自动）
      if (lcAxisRange.xmin != null) r.min = lcAxisRange.xmin;
      if (lcAxisRange.xmax != null) r.max = lcAxisRange.xmax;
      if (xType === 'logarithmic' && r.min <= 0) r.min = 0.1; // log 轴防 log(0)
      return r;
    } else {
      let r;
      if (allY.length === 0) {
        r = absMag ? { min: -30, max: -10 } : { min: 1e-6, max: 1 };
      } else if (absMag) {
        const mn = minOf(allY), mx = maxOf(allY);
        const pad = Math.max(0.3, (mx - mn) * 0.08);
        r = { min: mn - pad, max: mx + pad };
      } else {
        const mn = minOf(allY), mx = maxOf(allY);
        r = { min: Math.max(1e-13, mn * 0.5), max: mx * 2 };
      }
      if (lcAxisRange.ymin != null) r.min = lcAxisRange.ymin;
      if (lcAxisRange.ymax != null) r.max = lcAxisRange.ymax;
      if (!absMag && r.min <= 0) r.min = 1e-6; // log 轴最小正数约束
      return r;
    }
  }

  try {
    lcChartInstance = new Chart(ctx, {
    type: 'scatter',
    data: { datasets },
    plugins: [errorBarPlugin, lcTopAxisPlugin, dragRectPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: _lcTopAxis ? { padding: { top: 38 } } : undefined,
      interaction: { mode: 'nearest', intersect: true },
      plugins: {
        legend: { display: false },  // 波段开关由下方勾选面板承担
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          callbacks: {
            label: (ctx) => {
              const p = ctx.parsed;
              const ds = ctx.dataset;
              if (ds._isFit) return ds.label;
              const err = ds._errorValues ? ds._errorValues[ctx.dataIndex] : null;
              const tTxt = restFrame ? `t/(1+z)=${sciFormat(p.x)}s` : `t=${sciFormat(p.x)}s`;
              let txt = absMag
                ? `${tTxt}, M=${p.y.toFixed(2)}`
                : `${tTxt}, ${sciFormat(p.y)} mJy (AB ${mJyToMagAB(p.y).toFixed(2)})`;
              if (err != null && err > 0) txt += ` ±${absMag ? err.toFixed(2) : sciFormat(err)}`;
              if (ds._clippedFlags && ds._clippedFlags[ctx.dataIndex]) txt += ' [原始值≤0，已截断]';
              return txt;
            },
          },
        },
      },
      scales: {
        x: {
          type: xType,
          reverse: false,
          title: { display: true, text: restFrame ? 't/(1+z)  (s)' : 'time since T0  (s)', color: cc.tick, font: fonts.title },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: { color: cc.tick, font: fonts.tick, callback: (v) => sciFormat(v) },
          afterDataLimits(scale) {
            const r = computeAxisRange(scale.chart, 'x');
            scale.min = r.min;
            scale.max = r.max;
          },
        },
        // 左纵轴：流量密度 (mJy, log)；绝对星等模式为线性反向星等轴
        y: {
          type: absMag ? 'linear' : 'logarithmic',
          reverse: absMag,
          title: {
            display: true,
            text: absMag ? '绝对星等 M (AB)' : '流量密度 (mJy)' + (useGext ? ' · 银消改正' : ''),
            color: cc.tick, font: fonts.title,
          },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: {
            color: cc.tick,
            font: fonts.tick,
            callback: (v) => absMag ? Number(v).toFixed(1) : sciFormat(v),
          },
          afterDataLimits(scale) {
            const r = computeAxisRange(scale.chart, 'y');
            scale.min = r.min;
            scale.max = r.max;
          },
        },
        // 右纵轴：AB 星等，与左轴 mJy 物理对应（m = 16.4 − 2.5·log10(F_mJy)）
        // 数值方向与流量相反（顶部为小星等）由同一 log 映射自动保证；绝对星等模式下隐藏
        y2: {
          display: !absMag,
          type: 'logarithmic',
          position: 'right',
          title: { display: true, text: '星等 (AB)', color: cc.tick, font: fonts.title },
          grid: { drawOnChartArea: false },
          border: { color: cc.tick },
          ticks: {
            color: cc.tick,
            font: fonts.tick,
            callback: (v) => mJyToMagAB(v).toFixed(1),
          },
          afterDataLimits(scale) {
            // 与左轴同步 min/max
            const ys = scale.chart.scales.y;
            if (ys) { scale.min = ys.min; scale.max = ys.max; }
          },
        },
      },
    },
  });
    lcChartHolder.chart = lcChartInstance;
    buildBandPanel(sortedBands, spectralColors);
    applyBandVisibility();
    attachDragZoom(lcChartHolder, ctx, (range) => {
      lcAxisRange = range;
      const ids = { xmin: 'axXmin', xmax: 'axXmax', ymin: 'axYmin', ymax: 'axYmax' };
      for (const [k, id] of Object.entries(ids)) {
        const el = document.getElementById(id);
        if (el) el.value = sciFormat(range[k]);
      }
      lcChartInstance.update('none');
    });
  } catch (err) {
    console.error('Chart creation error:', err);
    const c = ctx.getContext('2d');
    c.fillStyle = cc.canvasBg;
    c.fillRect(0, 0, ctx.width, ctx.height);
    c.fillStyle = '#f85149';
    c.font = '14px sans-serif';
    c.textAlign = 'center';
    c.fillText('光变图渲染失败: ' + err.message, ctx.width/2, ctx.height/2);
  }
}
