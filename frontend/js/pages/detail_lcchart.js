// === 光变曲线图功能区（详情页「光变曲线」标签） ===
// 图表构建/重建、误差棒与顶部副轴插件、波段勾选面板、手动坐标范围、简易模型拟合叠加。
// 状态由 detail.js render() 经 resetLCChart()/setLCSourceParams() 重置与注入；
// window.* 全局入口依赖每轮 render 的 bands/bandNames/spectralColors，由 wireLCChartGlobals() 重挂。
import { showToast } from '../api.js';
import { fitLightcurveModel, getSpectrum } from '../api.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { chartColors, ACADEMIC_FONT, academicFonts } from '../theme.js';
import { mJyToMagAB, sortBandsByFreq, pointToMJy, LOG_AXIS_FLOOR } from '../bands.js';
import { esc, escAttr, sciFormat, sciTick, sci3, sig3, minOf, maxOf } from '../utils.js';
import { createYErrBarPlugin } from '../chart_plugins.js';

let lcChartInstance = null;
const lcChartHolder = { chart: null };  // 框选缩放用的当前图表引用

// ─── 光变图手动坐标范围（null = 该端自动） ───
let lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
// ─── 叠加在光变图上的拟合曲线 ───
let lcFits = [];
const FIT_COLORS = chartColors().fits;   // 拟合线配色（随主题；切换主题后 reload 生效）
const FIT_MODEL_NAMES = { pl: 'powerlaw', bpl: 'broken-powerlaw', sbpl: 'smoothly-broken-powerlaw', fred: 'FRED (Norris+2005)' };
// 拟合列表参数显示：键序与标签（时间参数带 s 后缀）
const FIT_PARAM_ORDER = { pl: ['A', 'alpha'], bpl: ['A', 'alpha1', 'alpha2', 'tb'], sbpl: ['A', 'alpha1', 'alpha2', 'tb', 'n'], fred: ['A', 'tau1', 'tau2', 'x1'] };
const FIT_PARAM_LABELS = { A: 'A', alpha: 'α', alpha1: 'α1', alpha2: 'α2', tb: 'tb', n: 'n', tau1: 'τ1', tau2: 'τ2', x1: 'x1' };
// ─── 当前源的红移/T0/距离模数（光变图静止系、顶部 MJD 轴、绝对星等用） ───
let _lcRedshift = null;
let _lcT0MJD = null;   // T0 对应的 MJD（无 T0 时为 null）
let _lcDistmod = null;
let _lcName = null;    // 源名（复制光变图标题用）
// ─── 当前时刻竖线（图头「显示当前时刻」开关，默认关闭） ───
let _lcShowNow = false;
let _lcZfac = 1;   // 静止系因子（buildLCChart 每轮重建同步）
// ─── 基准时刻（图头「基准时刻」输入；null = 源 T0，即默认行为） ───
let _lcRefMJD = null;
// ─── 光谱观测竖线（detail.js 经 setLCSpectra 注入；图头「显示光谱观测」开关） ───
let _lcSpectra = [];   // [{mjd, instrument, observation_date}]
let _lcShowSpec = false;
// ─── 光变图波段可见性（勾选框面板控制，默认全显示） ───
let lcBandVisible = {};  // band → bool
// ─── 上限点显示开关（#lcBandPanel 勾选，默认显示；与波段勾选取交集） ───
let _lcShowUL = true;
// ─── 光变图顶部副轴配置（null = 不画） ───
let _lcTopAxis = null;   // { mode: 'day'|'mjd' }
let _lcTopAxisSuppress = false;  // 复制合成时插件暂停绘制（副轴改画在离屏插入带）
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

// 解析「基准时刻」输入：MJD 数字或 UTC 时间（如 2022-10-09T13:16:59）；
// 留空或填 't0' = 源 T0（默认行为）。返回 { mjd }（mjd=null 表示默认）或 { err: true }
export function parseRefEpoch(v) {
  const s = (v || '').trim();
  if (s === '' || s.toLowerCase() === 't0') return { mjd: null };
  if (/^[+-]?[\d.]+([eE][+-]?\d+)?$/.test(s)) {
    const n = Number(s);
    if (isFinite(n)) return { mjd: n };
  }
  const mjd = t0ToMJD(s);
  return mjd != null ? { mjd } : { err: true };
}

// 当前有效基准（MJD）：用户输入优先，缺省源 T0；null = 无基准（横轴维持 time 原样）
function _lcEffRefMJD() {
  return _lcRefMJD != null ? _lcRefMJD : _lcT0MJD;
}

// 是否启用了自定义基准（有效基准存在且 ≠ 源 T0）
function _lcUseRefX() {
  const refMJD = _lcEffRefMJD();
  return refMJD != null && refMJD !== _lcT0MJD;
}

// 图坐标相对 T0 秒数的平移量：x = t/zfac + dx（自定义基准且源有 T0 时非零，否则为 0）
function _lcChartDxRef() {
  const refMJD = _lcEffRefMJD();
  if (refMJD == null || refMJD === _lcT0MJD || _lcT0MJD == null) return 0;
  return (_lcT0MJD - refMJD) * 86400 / _lcZfac;
}

// detail.js 获取光谱列表后调用：注入光谱观测时刻（[{mjd, instrument, observation_date}]；
// 传 null/undefined 清空）。设置后若「显示光谱观测」开关开着则重绘
export function setLCSpectra(list) {
  _lcSpectra = Array.isArray(list) ? list.filter(s => s && s.mjd != null && isFinite(s.mjd)) : [];
  const chk = document.getElementById('lcShowSpec');
  if (chk) {
    chk.disabled = _lcSpectra.length === 0;
    const box = chk.closest('.form-check');
    if (box) box.title = _lcSpectra.length
      ? '在光变图上以紫色竖虚线标出各光谱的观测时刻（越界时以三角箭头指示方向）'
      : '该源暂无光谱数据';
  }
  if (lcChartInstance && _lcShowSpec) lcChartInstance.update('none');
}

// render() 获取源数据后调用：注入光变图用的源级参数
export function setLCSourceParams({ redshift, t0, distmod, name }) {
  _lcRedshift = (redshift != null && redshift > -1) ? redshift : null;
  _lcT0MJD = t0ToMJD(t0);
  _lcDistmod = (distmod != null) ? distmod : null;
  _lcName = name || null;
}

// render() 重建 DOM 前调用：销毁图表并重置全部图状态
export function resetLCChart() {
  if (lcChartInstance) { try { lcChartInstance.destroy(); } catch {} lcChartInstance = null; }
  lcFits = [];
  lcAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  lcBandVisible = {};
  _lcShowUL = true;
  lcShowErr = true;
  _lcShowNow = false;
  _lcZfac = 1;
  _lcRefMJD = null;
  _lcSpectra = [];
  _lcShowSpec = false;
  _lcTopAxis = null;
  _lcTopAxisSuppress = false;
  _lcRedshift = null;
  _lcT0MJD = null;
  _lcDistmod = null;
  _lcName = null;
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
  if (model === 'fred') {
    // Norris 2005 脉冲形：μ=(τ1/τ2)^(1/2)，exp(2μ) 归一化使 A 即峰值强度；
    // 定义域 u = t+μ-x1 > 0，域外取 0
    const mu = Math.sqrt(prm.tau1 / prm.tau2);
    const u = t + mu - prm.x1;
    if (u <= 0) return 0;
    return prm.A * Math.exp(2 * mu - prm.tau1 / u - u / prm.tau2);
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
  if (fit.model === 'fred') return `${fit.band} FRED: A=${sci3(prm.A)}, τ1=${sig3(prm.tau1)}s, τ2=${sig3(prm.tau2)}s, x1=${sig3(prm.x1)}s`;
  return `${fit.band} BPL: α1=${sig3(prm.alpha1)}, α2=${sig3(prm.alpha2)}, tb=${sig3(prm.tb)}s`;
}

// ─── 错误条绘制插件（beforeDatasetsDraw：误差棒画在最底层，不遮挡数据点与拟合线） ───
let lcShowErr = true;   // 是否绘制误差棒（图头「误差棒」开关）
const _lcYErrBar = createYErrBarPlugin({
  enabled: () => lcShowErr,
  errOf: (ds, raw, i) => ds._errorValues ? ds._errorValues[i] : null,
  xErrOf: (ds, raw, i) => ds._timeErrValues ? ds._timeErrValues[i] : null,
  skipDataset: ds => ds._isUpperLimit || ds._isFit,
});
// 包装层：手绘误差棒前把画布裁剪到 chartArea（手动坐标范围下误差棒不越界；
// 共享实现 chart_plugins.js 不动，clip 只加在本页）
const errorBarPlugin = {
  id: 'lcYErrBarClip',
  beforeDatasetsDraw(chart, args, opts) {
    const area = chart.chartArea;
    if (!area) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.left, area.top, area.width, area.height);
    ctx.clip();
    try {
      _lcYErrBar.beforeDatasetsDraw(chart, args, opts);
    } finally {
      ctx.restore();
    }
  },
};

// ─── 当前时刻竖线（红色虚线，贯通全图；越界时在对应侧上角画红色三角；afterDraw 手绘，不参与轴范围计算） ───
const NOW_LINE_COLOR = '#f85149';
const lcNowLinePlugin = {
  id: 'lcNowLine',
  afterDraw(chart) {
    chart._lcNowTriSide = null;   // 记录本帧 now 越界三角占用的一侧（光谱越界三角据此错开 y 槽位）
    const refMJD = _lcEffRefMJD();   // 与横轴同一基准（默认源 T0）
    if (!_lcShowNow || refMJD == null) return;
    const xs = chart.scales.x;
    const area = chart.chartArea;
    if (!xs || !area) return;
    // 与横轴同单位同坐标系：观测系秒数 = (当前 MJD − 基准 MJD)×86400，静止系再除 (1+z)
    const nowMJD = Date.now() / 86400000 + 40587;  // Unix epoch = MJD 40587
    const tNow = (nowMJD - refMJD) * 86400 / _lcZfac;
    const ctx = chart.ctx;
    if (!(tNow >= xs.min && tNow <= xs.max)) {
      // 越界：左越界左上角画朝左三角，右越界右上角画朝右三角（与光谱紫三角同槽位体系，now 占第 0 槽）
      const left = tNow < xs.min;
      const ax = left ? area.left + 6 : area.right - 6;
      const ay = area.top + 10;
      ctx.save();
      ctx.fillStyle = NOW_LINE_COLOR;
      ctx.beginPath();
      if (left) {
        ctx.moveTo(ax, ay);
        ctx.lineTo(ax + 7, ay - 5);
        ctx.lineTo(ax + 7, ay + 5);
      } else {
        ctx.moveTo(ax, ay);
        ctx.lineTo(ax - 7, ay - 5);
        ctx.lineTo(ax - 7, ay + 5);
      }
      ctx.closePath();
      ctx.fill();
      ctx.restore();
      chart._lcNowTriSide = left ? 'left' : 'right';
      return;
    }
    const px = xs.getPixelForValue(tNow);
    if (!isFinite(px)) return;
    ctx.save();
    ctx.strokeStyle = NOW_LINE_COLOR;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(px, area.top);
    ctx.lineTo(px, area.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = NOW_LINE_COLOR;
    ctx.font = `14px ${ACADEMIC_FONT}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText('now', px + 4, area.top + 4);
    ctx.restore();
  },
};

// ─── 光谱观测竖线（紫色虚线 + 越界三角箭头；afterDraw 手绘，只读 chartArea，不动轴范围） ───
const SPEC_LINE_COLOR = '#bc8cff';   // 紫色，与「显示当前时刻」红线 #f85149 明显区分
const lcSpecLinesPlugin = {
  id: 'lcSpecLines',
  afterDraw(chart) {
    chart._lcSpecHits = [];   // 本帧命中区（竖线 px / 越界三角包围盒），悬停提示用
    if (!_lcShowSpec || !_lcSpectra.length) return;
    const xs = chart.scales.x;
    const area = chart.chartArea;
    if (!xs || !area) return;
    const refMJD = _lcEffRefMJD();   // 与横轴同一基准（默认源 T0）
    if (refMJD == null) return;
    const ctx = chart.ctx;
    // 越界三角 y 槽位：若本帧 now 越界三角占用了某侧第 0 槽（lcNowLinePlugin 先画），该侧从第 1 槽起
    let nLeft = chart._lcNowTriSide === 'left' ? 1 : 0;
    let nRight = chart._lcNowTriSide === 'right' ? 1 : 0;
    for (const sp of _lcSpectra) {
      const xSec = (sp.mjd - refMJD) * 86400 / _lcZfac;
      if (xSec >= xs.min && xSec <= xs.max) {
        const px = xs.getPixelForValue(xSec);
        if (!isFinite(px)) continue;
        ctx.save();
        ctx.strokeStyle = SPEC_LINE_COLOR;
        ctx.lineWidth = 1.2;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(px, area.top);
        ctx.lineTo(px, area.bottom);
        ctx.stroke();
        ctx.restore();
        chart._lcSpecHits.push({ kind: 'line', px, sp });
      } else {
        // 越界（需求4.6）：左越界在左上角画指向左的三角，右越界在右上角画指向右的三角；
        // 多个越界箭头纵向错开约 14px（now 越界三角占用时相应侧已顺延，见上方槽位注释）
        const left = xSec < xs.min;
        const ax = left ? area.left + 6 : area.right - 6;
        const ay = area.top + 10 + (left ? nLeft++ : nRight++) * 14;
        ctx.save();
        ctx.fillStyle = SPEC_LINE_COLOR;
        ctx.beginPath();
        if (left) {
          ctx.moveTo(ax, ay);
          ctx.lineTo(ax + 7, ay - 5);
          ctx.lineTo(ax + 7, ay + 5);
        } else {
          ctx.moveTo(ax, ay);
          ctx.lineTo(ax - 7, ay - 5);
          ctx.lineTo(ax - 7, ay + 5);
        }
        ctx.closePath();
        ctx.fill();
        ctx.restore();
        chart._lcSpecHits.push({ kind: 'tri', x0: ax - 8, y0: ay - 7, x1: ax + 8, y1: ay + 7, sp });
      }
    }
  },
};

// ─── 光谱竖线悬停提示（canvas mousemove 命中检测 + 绝对定位 tooltip div） ───
let _specTip = null;
function _hideSpecTip() { if (_specTip) _specTip.style.display = 'none'; }

// tooltip 内容：观测日期（observation_date 有就直接用，否则由 mjd 换算 YYYY-MM-DD）+ 仪器名
function _specTipText(sp) {
  let date = sp.observation_date ? String(sp.observation_date).slice(0, 10) : '';
  if (!date) date = new Date((sp.mjd - 40587) * 86400000).toISOString().slice(0, 10);  // Unix epoch = MJD 40587
  return `${date} · ${sp.instrument || '未知仪器'}`;
}

// 命中检测（悬停提示与点击弹窗共用）：返回命中的光谱项或 null
function _hitSpecLine(chart, mx, my) {
  const hits = chart && chart._lcSpecHits;
  if (!chart || !hits || !hits.length || !chart.chartArea) return null;
  const area = chart.chartArea;
  for (const h of hits) {
    if (h.kind === 'line') {
      // 距竖线 ≤4px 且在绘图区纵向范围内
      if (Math.abs(mx - h.px) <= 4 && my >= area.top && my <= area.bottom) return h.sp;
    } else if (mx >= h.x0 - 2 && mx <= h.x1 + 2 && my >= h.y0 - 2 && my <= h.y1 + 2) return h.sp;
  }
  return null;
}

function _attachSpecHover(canvas) {
  if (canvas._lcSpecHoverOn) return;   // 同一 canvas 只挂一次
  canvas._lcSpecHoverOn = true;
  const container = canvas.parentElement;
  if (!container) return;
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
  const cc = chartColors();
  _specTip = document.createElement('div');
  _specTip.style.cssText = `position:absolute;display:none;pointer-events:none;z-index:10;`
    + `padding:3px 8px;border-radius:4px;font-size:12px;white-space:nowrap;`
    + `background:${cc.tooltipBg};color:${cc.tooltipText}`;
  container.appendChild(_specTip);
  canvas.addEventListener('mousemove', (e) => {
    const chart = lcChartInstance;
    if (!chart || !_lcShowSpec || !chart.chartArea) { _hideSpecTip(); canvas.style.cursor = ''; return; }
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const hit = _hitSpecLine(chart, mx, my);
    canvas.style.cursor = hit ? 'pointer' : '';   // 可点击反馈
    if (!hit) { _hideSpecTip(); return; }
    _specTip.textContent = _specTipText(hit) + '（点击查看光谱）';
    _specTip.style.display = 'block';
    // 右半区向左展开，避免超出容器
    _specTip.style.left = (mx > rect.width / 2 ? mx - 12 - _specTip.offsetWidth : mx + 12) + 'px';
    _specTip.style.top = (my + 12) + 'px';
  });
  canvas.addEventListener('mouseleave', () => { _hideSpecTip(); canvas.style.cursor = ''; });
  // 点击竖线/越界三角 → 弹出 4:3 小窗绘制该光谱（记录按下位置，拖动框选缩放后松手不触发）
  let downPos = null;
  canvas.addEventListener('mousedown', (e) => { downPos = { x: e.clientX, y: e.clientY }; });
  canvas.addEventListener('click', (e) => {
    if (downPos && Math.hypot(e.clientX - downPos.x, e.clientY - downPos.y) > 5) return;
    const chart = lcChartInstance;
    if (!chart || !_lcShowSpec) return;
    const rect = canvas.getBoundingClientRect();
    const hit = _hitSpecLine(chart, e.clientX - rect.left, e.clientY - rect.top);
    if (hit) _openSpecPopup(hit);
  });
}

// ─── 光谱点击弹窗（横纵比 4:3 小窗内绘制该条光谱） ───
let _specPopupChart = null;
let _specPopupKeyHandler = null;
function _closeSpecPopup() {
  if (_specPopupChart) { try { _specPopupChart.destroy(); } catch {} _specPopupChart = null; }
  if (_specPopupKeyHandler) { document.removeEventListener('keydown', _specPopupKeyHandler); _specPopupKeyHandler = null; }
  document.getElementById('lcSpecPopup')?.remove();
}

async function _openSpecPopup(sp) {
  if (sp == null || sp.id == null) return;
  _closeSpecPopup();
  const cc = chartColors();
  const overlay = document.createElement('div');
  overlay.id = 'lcSpecPopup';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:2000;display:flex;align-items:center;justify-content:center;'
    + 'background:rgba(0,0,0,0.45);';
  // 小窗：宽 min(560px, 86vw)，高度按 4:3 宽高比
  const win = document.createElement('div');
  win.style.cssText = 'width:min(560px,86vw);aspect-ratio:4/3;display:flex;flex-direction:column;'
    + 'background:var(--bg-card);border:1px solid var(--border-color);border-radius:8px;overflow:hidden;'
    + 'box-shadow:0 8px 30px rgba(0,0,0,0.5);';
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;'
    + 'padding:6px 12px;border-bottom:1px solid var(--border-color);font-size:0.9em;';
  const title = document.createElement('span');
  title.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-secondary);';
  title.textContent = `光谱 · ${_specTipText(sp)}`;
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'btn-close';   // bootstrap 图标按钮（随主题）
  closeBtn.title = '关闭 (Esc)';
  header.appendChild(title);
  header.appendChild(closeBtn);
  const body = document.createElement('div');
  body.style.cssText = 'flex:1;min-height:0;position:relative;padding:8px 12px 12px;';
  const cv = document.createElement('canvas');
  body.appendChild(cv);
  win.appendChild(header);
  win.appendChild(body);
  overlay.appendChild(win);
  document.body.appendChild(overlay);
  const close = () => _closeSpecPopup();
  closeBtn.addEventListener('click', close);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });
  _specPopupKeyHandler = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', _specPopupKeyHandler);

  try {
    const resp = await getSpectrum(sp.id);
    if (!document.body.contains(cv)) return;   // 弹窗已关闭
    const objName = Object.keys(resp.data)[0];
    const spec = resp.data[objName].spectra;
    const pts = spec.data.map(d => ({ x: Number(d[0]), y: Number(d[1]) }))
      .filter(d => isFinite(d.x) && isFinite(d.y));
    const errs = spec.data.map(d => (d.length > 2 && isFinite(Number(d[2]))) ? Number(d[2]) : null);
    title.textContent = `${objName} · ${resp.meta.filename || ''} · MJD ${spec.time || '-'} · ${_specTipText(sp)}`;
    if (!pts.length) { showToast('该光谱没有可绘制的数据点', 'warning'); return; }
    const errBar = createYErrBarPlugin({ errOf: (ds, raw, i) => ds._errorValues ? ds._errorValues[i] : null });
    _specPopupChart = new Chart(cv.getContext('2d'), {
      type: 'line',
      data: {
        datasets: [{
          label: resp.meta.filename || 'spectrum',
          data: pts,
          borderColor: SPEC_LINE_COLOR,
          backgroundColor: SPEC_LINE_COLOR,
          borderWidth: 1.2,
          pointRadius: 0,
          tension: 0.15,
          _errorValues: errs.some(e => e != null) ? errs : null,
        }],
      },
      plugins: [errBar],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => `λ=${c.parsed.x.toFixed(1)} Å, F=${c.parsed.y.toExponential(3)}` } },
        },
        scales: {
          x: { type: 'linear', title: { display: true, text: '波长 (Å)', color: cc.tick },
               grid: { color: cc.grid }, ticks: { color: cc.tick } },
          y: { type: 'linear', title: { display: true, text: spec.u_fluxes || '流量', color: cc.tick },
               grid: { color: cc.grid }, ticks: { color: cc.tick, callback: v => v.toExponential(1) } },
        },
      },
    });
  } catch (err) {
    showToast(`加载光谱失败: ${err.message}`, 'danger');
    _closeSpecPopup();
  }
}

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

// 顶部副轴刻度计算（插件手绘与复制合成共用；返回 null = 不画）
function _lcTopAxisTicks(chart) {
  const cfg = _lcTopAxis;
  if (!cfg || !cfg.mode) return null;
  const x = chart.scales.x;
  if (!x) return null;
  // day = t/86400；mjd = T0(MJD) + t/86400
  const toDisp = cfg.mode === 'mjd' ? (t) => cfg.t0mjd + t / 86400 : (t) => t / 86400;
  const fromDisp = cfg.mode === 'mjd' ? (d) => (d - cfg.t0mjd) * 86400 : (d) => d * 86400;
  const lo = toDisp(x.min), hi = toDisp(x.max);
  let ticks, fmt;
  if (cfg.mode !== 'mjd' && x.type === 'logarithmic' && hi / Math.max(lo, 1e-12) > 30) {
    // 对数主轴+大动态范围：线性规整刻度会簇聚在右端，改用每十倍程 1/2/5 对数刻度
    ticks = [];
    for (let e = Math.floor(Math.log10(Math.max(lo, 1e-12))); e <= Math.ceil(Math.log10(hi)); e++)
      for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= lo && v <= hi) ticks.push(v); }
    fmt = (v) => String(parseFloat(v.toPrecision(6)));
  } else {
    const r = _niceTicks(lo, hi, 8);
    ticks = r.ticks;
    const dec = r.step >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log10(r.step)));
    fmt = cfg.mode === 'mjd'
      ? (v) => v.toFixed(Math.max(dec, 1))
      : (v) => String(parseFloat(v.toFixed(Math.max(dec, 2))));
  }
  return { mode: cfg.mode, ticks, fmt, toPx: (d) => x.getPixelForValue(fromDisp(d)) };
}

// 顶部副轴带内容绘制（插件画在 chartArea 上方；复制合成画在离屏插入带 yTop 处，带高 46）
function _drawTopAxisBand(ctx, chart, yLine, titleY) {
  const info = _lcTopAxisTicks(chart);
  const area = chart.chartArea;
  if (!info || !area) return;
  const cc = chartColors();
  ctx.save();
  ctx.strokeStyle = cc.tick;
  ctx.fillStyle = cc.tick;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(area.left, yLine);
  ctx.lineTo(area.right, yLine);
  ctx.stroke();
  ctx.font = `11px ${ACADEMIC_FONT}`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  for (const d of info.ticks) {
    const px = info.toPx(d);
    if (px < area.left + 2 || px > area.right - 2) continue;
    ctx.beginPath();
    ctx.moveTo(px, yLine);
    ctx.lineTo(px, yLine - 5);
    ctx.stroke();
    ctx.fillText(info.fmt(d), px, yLine - 7);
  }
  ctx.fillText(info.mode === 'mjd' ? 'MJD' : (_lcUseRefX() ? 'time since 基准 (day)' : 'time since T0 (day)'), (area.left + area.right) / 2, titleY);
  ctx.restore();
}

const lcTopAxisPlugin = {
  id: 'lcTopAxis',
  afterDraw(chart) {
    if (_lcTopAxisSuppress) return;
    const area = chart.chartArea;
    if (!area) return;
    _drawTopAxisBand(chart.ctx, chart, area.top, area.top - 24);
  },
};

// ─── 光变图波段勾选面板（替代内置图例的划线开关） ───
function applyBandVisibility() {
  const chart = lcChartInstance;
  if (!chart) return;
  chart.data.datasets.forEach((ds, i) => {
    if (ds._isFit || ds._band == null) return;
    // 波段勾选与「显示上限点」取交集：上限点数据集受两个开关共同控制
    let vis = lcBandVisible[ds._band] !== false;
    if (ds._isUpperLimit && !_lcShowUL) vis = false;
    chart.setDatasetVisibility(i, vis);
  });
  chart.update();
}

function buildBandPanel(sortedBands, spectralColors) {
  const el = document.getElementById('lcBandPanel');
  if (!el) return;
  el.innerHTML = `<span class="text-secondary">波段:</span>` + sortedBands.map((b, i) => {
    const vis = lcBandVisible[b] !== false;
    const c = spectralColors[b] || '#58a6ff';
    return `<span class="form-check form-check-inline mb-0 d-inline-flex align-items-center">
      <input class="form-check-input lc-band-chk mt-0" type="checkbox" id="lcBandChk_${i}" data-band="${escAttr(b)}" ${vis ? 'checked' : ''}>
      <label class="form-check-label" for="lcBandChk_${i}"><span style="color:${c}">●</span> ${esc(b)}</label>
    </span>`;
  }).join('') + `
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="lcBandAll">全选</button>
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="lcBandNone">全不选</button>
    <span class="form-check form-check-inline mb-0 ms-2 border-start ps-2 d-inline-flex align-items-center" title="是否在图上显示上限点（倒三角）；取消勾选只显示探测点">
      <input class="form-check-input mt-0" type="checkbox" id="lcShowUL" ${_lcShowUL ? 'checked' : ''}>
      <label class="form-check-label" for="lcShowUL">显示上限点</label>
    </span>`;
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
  document.getElementById('lcShowUL')?.addEventListener('change', (e) => {
    _lcShowUL = e.target.checked;
    applyBandVisibility();
  });
}

// ─── 1σ 置信带（mJy 空间）───
// 优先用 MCMC 后验样本路径（fit.samples：输出参数基的 dict 列表，后端 fit_model 返回）：
// 每个样本在网格上求值，逐网格点取 16/84 分位作为 lo/hi；
// fit.samples 缺失时回退 Jacobian 路径：采样点中心差分数值 Jacobian，σ(t)=√(J·cov·Jᵀ)
// fit.param_cov = { keys: [...], matrix: [[...]] }（后端 fit_model 返回，键序为 params 参数基）
function fitBandPoints(fit, ts) {
  if (Array.isArray(fit.samples) && fit.samples.length > 10) {
    const curve = fit.samples.map(prm => ts.map(tt => fitModelFlux(fit.model, prm, tt)));
    const out = [];
    for (let i = 0; i < ts.length; i++) {
      const col = curve.map(v => v[i]).filter(v => v > 0 && isFinite(v)).sort((a, b) => a - b);
      if (col.length < 10) continue;
      out.push({ x: ts[i], lo: col[Math.floor(0.16 * (col.length - 1))], hi: col[Math.ceil(0.84 * (col.length - 1))] });
    }
    if (out.length > 2) return out;
  }
  const cov = fit.param_cov;
  if (!cov || !Array.isArray(cov.keys) || !Array.isArray(cov.matrix)) return null;
  const keys = cov.keys, M = cov.matrix, k = keys.length;
  if (!k || M.length !== k || M.some(row => !Array.isArray(row) || row.length !== k)) return null;
  const base = keys.map(key => fit.params[key]);
  if (base.some(v => v == null || !isFinite(v))) return null;
  const modelAt = (pv, tt) => {
    const prm = {};
    keys.forEach((key, j) => { prm[key] = pv[j]; });
    return fitModelFlux(fit.model, prm, tt);
  };
  const out = [];
  for (const tt of ts) {
    const f0 = modelAt(base, tt);
    if (!(f0 > 0) || !isFinite(f0)) continue;
    const J = new Array(k);
    let bad = false;
    for (let j = 0; j < k; j++) {
      const h = Math.max(Math.abs(base[j]), 1e-12) * 1e-5;
      const pp = [...base], pm = [...base];
      pp[j] += h; pm[j] -= h;
      const fp = modelAt(pp, tt), fm = modelAt(pm, tt);
      if (!isFinite(fp) || !isFinite(fm)) { bad = true; break; }
      J[j] = (fp - fm) / (2 * h);
    }
    if (bad) continue;
    let v = 0;
    for (let i = 0; i < k; i++) for (let j = 0; j < k; j++) v += J[i] * M[i][j] * J[j];
    if (!(v >= 0)) continue;
    const s = Math.sqrt(v);
    out.push({ x: tt, lo: f0 - s, hi: f0 + s });
  }
  return out;
}

// 拟合参数文本（含 1σ 误差；A 用科学计数，时间参数带 s 后缀）
function fitParamsText(fit) {
  const errs = fit.param_errors || {};
  return (FIT_PARAM_ORDER[fit.model] || []).map(k => {
    const v = fit.params[k];
    if (v == null) return null;
    const fmt = k === 'A' ? sci3 : sig3;
    const unit = (k === 'tb' || k === 'x1') ? 's' : '';
    const e = errs[k];
    return `${FIT_PARAM_LABELS[k]}=${fmt(v)}${(e != null && isFinite(e)) ? '±' + fmt(e) : ''}${unit}`;
  }).filter(Boolean).join(', ');
}

// ─── 拟合列表 UI ───
function renderFitList() {
  const el = document.getElementById('lcFitList');
  if (!el) return;
  if (lcFits.length === 0) { el.innerHTML = ''; return; }
  // 拟合内部存 T0 相对秒数；自定义基准时显示换算回图坐标（与拟合面板输入同坐标系）
  const dxRef = _lcChartDxRef();
  const toChartT = (t) => t / _lcZfac + dxRef;
  el.innerHTML = lcFits.map((fit, i) => {
    const ptxt = fitParamsText(fit);
    const prange = (fit.pmin !== fit.tmin || fit.pmax !== fit.tmax)
      ? ` · 绘制[${sciFormat(toChartT(fit.pmin))}, ${sciFormat(toChartT(fit.pmax))}]s` : '';
    return `<div class="d-flex align-items-center gap-2 mb-1">
      <span style="display:inline-block;width:24px;border-top:2px dashed ${fit.color}"></span>
      <span>${FIT_MODEL_NAMES[fit.model] || fit.model} · ${esc(fit.band)} · 拟合[${sciFormat(toChartT(fit.tmin))}, ${sciFormat(toChartT(fit.tmax))}]s${prange} · ${ptxt} · N=${fit.N}</span>
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

// ─── 图头控件注入（「基准时刻」输入 + 「显示光谱观测」开关；模板在 detail.js 预留
// #lcRefEpochSlot / #lcSpecChkSlot 占位槽，本模块在每轮 render 的 wireLCChartGlobals 时填充） ───
function _ensureLCHeaderControls() {
  if (document.getElementById('lcRefEpoch')) return;
  const refSlot = document.getElementById('lcRefEpochSlot');
  const specSlot = document.getElementById('lcSpecChkSlot');
  if (!refSlot || !specSlot) return;

  // 基准时刻输入（MJD 数字或 UTC 时间；留空/填 t0 = 源 T0）
  const refSpan = document.createElement('span');
  refSpan.className = 'd-inline-flex align-items-center gap-1';
  refSpan.title = '横轴零点：可填 MJD 数字或 UTC 时间（如 2022-10-09T13:16:59）；留空或填 t0 = 源 T0';
  refSpan.innerHTML = `<span class="text-secondary small">基准时刻:</span>
    <input type="text" class="form-control form-control-sm" id="lcRefEpoch" style="width:260px;font-size:0.72rem"
           placeholder="MJD 或 UTC，留空=源 T0">`;
  refSlot.appendChild(refSpan);
  const refInp = refSpan.querySelector('#lcRefEpoch');
  refInp.value = _lcRefMJD != null ? String(_lcRefMJD) : '';
  refInp.addEventListener('change', () => {
    const r = parseRefEpoch(refInp.value);
    if (r.err) {   // 解析失败：toast 报错并保持原值
      showToast('基准时刻无法解析：请填 MJD 数字或 UTC 时间（留空/t0 = 源 T0）', 'warning');
      refInp.value = _lcRefMJD != null ? String(_lcRefMJD) : '';
      return;
    }
    if (r.mjd === _lcRefMJD) return;
    _lcRefMJD = r.mjd;
    refInp.value = r.mjd != null ? String(r.mjd) : '';   // 归一化显示为 MJD
    // 自定义基准可替代源 T0 支撑 MJD 副轴与「当前时刻」竖线：按可用性刷新禁用态
    const mjdOpt = document.querySelector('#topAxis option[value="mjd"]');
    if (mjdOpt) mjdOpt.disabled = _lcEffRefMJD() == null;
    const nowChk0 = document.getElementById('lcShowNow');
    if (nowChk0) nowChk0.disabled = _lcEffRefMJD() == null;
    if (_bands && lcChartInstance) rebuildLCPlot(_bands, _bandNames, _spectralColors);
  });

  // 显示光谱观测开关（无光谱数据时禁用）
  const specDiv = document.createElement('div');
  specDiv.className = 'form-check form-check-inline mb-0';
  specDiv.title = _lcSpectra.length
    ? '在光变图上以紫色竖虚线标出各光谱的观测时刻（越界时以三角箭头指示方向）'
    : '该源暂无光谱数据';
  specDiv.innerHTML = `<input class="form-check-input" type="checkbox" id="lcShowSpec"
      ${_lcShowSpec ? 'checked' : ''} ${_lcSpectra.length ? '' : 'disabled'}>
    <label class="form-check-label small" for="lcShowSpec">显示光谱观测</label>`;
  specSlot.appendChild(specDiv);
  specDiv.querySelector('#lcShowSpec').addEventListener('change', (e) => {
    _lcShowSpec = e.target.checked;
    if (!_lcShowSpec) _hideSpecTip();
    if (lcChartInstance) lcChartInstance.update('none');
  });
}

// ─── 全局入口接线（每轮 render 以最新的 bands/bandNames/spectralColors 重挂） ───
export function wireLCChartGlobals(bands, bandNames, spectralColors) {
  _bands = bands;
  _bandNames = bandNames;
  _spectralColors = spectralColors;
  _ensureLCHeaderControls();
  window.rebuildLCPlot = () => rebuildLCPlot(bands, bandNames, spectralColors);
  window.lcShowErrToggle = (on) => {   // 误差棒开关：只影响绘制，不动数据，无动画重绘即可
    lcShowErr = on;
    if (lcChartInstance) lcChartInstance.update('none');
  };
  window.lcShowNowToggle = (on) => {   // 当前时刻竖线开关：只影响绘制，不动轴范围
    _lcShowNow = on;
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
    // 拟合面板 t 输入与图同坐标系：自定义基准时图坐标含平移，换算回 T0 相对秒数供拟合
    // （自定义基准但源无 T0 时 dxRef=0，输入按 T0 相对秒数解释——此时拟合线也不叠加，见 buildLCChart）
    const dxRef = _lcChartDxRef();
    const fromChartT = (v) => isFinite(v) ? (v - dxRef) * _lcZfac : v;
    const tminIn = fromChartT(parseFloat(document.getElementById('fitTmin')?.value));
    const tmaxIn = fromChartT(parseFloat(document.getElementById('fitTmax')?.value));
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
    const need = model === 'pl' ? 2 : (model === 'sbpl' ? 5 : 4);
    if (pts.length < need) {
      showToast(`有效数据点不足（${FIT_MODEL_NAMES[model] || model} 需 ≥${need} 点，当前 ${pts.length} 点）`, 'warning');
      return;
    }
    // bpl/sbpl 拐点预设范围（留空端=数据范围）
    let bounds = null;
    if (model === 'bpl' || model === 'sbpl') {
      const lo = fromChartT(parseFloat(document.getElementById('fitTbMin')?.value));
      const hi = fromChartT(parseFloat(document.getElementById('fitTbMax')?.value));
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
      const pminIn = fromChartT(parseFloat(document.getElementById('fitPmin')?.value));
      const pmaxIn = fromChartT(parseFloat(document.getElementById('fitPmax')?.value));
      const fit = {
        model, band,
        tmin, tmax,
        // 绘制（外推）范围：仅影响拟合线在图上的绘制区间，默认与拟合范围一致
        pmin: (isFinite(pminIn) && pminIn > 0) ? pminIn : tmin,
        pmax: (isFinite(pmaxIn) && pmaxIn > 0) ? pmaxIn : tmax,
        params: res.params,
        param_errors: res.param_errors || null,
        param_cov: res.param_cov || null,
        samples: Array.isArray(res.samples) ? res.samples : null,
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
      if (res.degenerate) showToast('点数仅够确定参数，无法给出误差估计', 'warning');
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

// ─── 复制光变图到剪贴板（含图例与标题） ───
// 临时打开内置图例/标题同步重绘，离屏 canvas 铺底色合成后写剪贴板；
// 非安全上下文或剪贴板写图失败时回退为下载 PNG
window.copyLCChart = async () => {
  const chart = lcChartInstance;
  if (!chart) { showToast('光变图尚未生成，请先打开光变曲线页', 'warning'); return; }
  const cc = chartColors();
  const plg = chart.options.plugins;
  const legend = plg.legend, title = plg.title || (plg.title = {});
  const layout = chart.options.layout || (chart.options.layout = {});
  const prev = { lg: legend.display, ti: title.display, text: title.text, pad: layout.padding };
  legend.display = true;
  legend.position = 'top';
  // 图例只列当前可见的数据集（未勾选的波段不进图例；置信带等空 label 数据集始终排除）
  legend.labels = {
    color: cc.legend, font: academicFonts().legend,
    filter: (item) => item.text !== '' &&
      (item.datasetIndex == null || chart.isDatasetVisible(item.datasetIndex)),
  };
  title.display = true;
  title.text = `${_lcName || ''} 光变曲线`;
  title.color = cc.legend;
  title.font = academicFonts().title;
  // 顶部留白给图例上方留出边距；手绘顶部副轴在合成时改画进离屏插入带（见下），
  // 因为 layout.padding 位于所有 dock 组件（含图例）之外，无法用它隔开图例与副轴
  layout.padding = { ...(typeof prev.pad === 'object' && prev.pad ? prev.pad : {}), top: 40 };
  _lcTopAxisSuppress = true;
  chart.update('none');
  const restore = () => {
    legend.display = prev.lg;
    title.display = prev.ti;
    title.text = prev.text;
    layout.padding = prev.pad;
    _lcTopAxisSuppress = false;
    chart.update('none');
  };
  try {
    const src = chart.canvas;
    const area = chart.chartArea;
    const dpr = chart.currentDevicePixelRatio || 1;
    // 有顶部副轴时在图例/标题与绘图区之间插入 46px 高的副轴带，彻底避免与图例重叠
    const band = _lcTopAxis ? 46 : 0;
    const bandPx = Math.round(band * dpr);
    // 切割线抬高 10px：最顶端一行 y 轴刻度标签（跨越 chartArea 顶边）整体划入下半部分，
    // 避免被水平切成两半（其网格线随下半部分同步下移，对齐保持）
    const cutY = Math.round(Math.max(0, (area ? area.top : 0) - (band ? 10 : 0)) * dpr);
    const off = document.createElement('canvas');
    off.width = src.width;
    off.height = src.height + bandPx;
    const octx = off.getContext('2d');
    octx.fillStyle = cc.canvasBg;
    octx.fillRect(0, 0, off.width, off.height);
    // 上半（标题+图例）原样；下半（绘图区）整体下移一个带高
    octx.drawImage(src, 0, 0, src.width, cutY, 0, 0, src.width, cutY);
    octx.drawImage(src, 0, cutY, src.width, src.height - cutY,
      0, cutY + bandPx, src.width, src.height - cutY);
    if (band && area) {
      octx.save();
      octx.scale(dpr, dpr);
      _drawTopAxisBand(octx, chart, area.top + band - 8, area.top + 13);
      octx.restore();
    }
    const blob = await new Promise(r => off.toBlob(r, 'image/png'));
    if (!blob) throw new Error('图像导出失败');
    if (navigator.clipboard && window.isSecureContext && typeof ClipboardItem !== 'undefined') {
      try {
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
        showToast('光变图已复制到剪贴板', 'success');
        return;
      } catch { /* 剪贴板写图失败，回退下载 */ }
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${_lcName || 'lightcurve'}_lc.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    showToast('当前环境不支持剪贴板写图，已改为下载 PNG', 'warning');
  } catch (err) {
    showToast(`复制失败: ${err.message}`, 'danger');
  } finally {
    restore();
  }
};

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
  _lcZfac = zfac;  // 模块级同步（当前时刻竖线/光谱竖线用）
  // 有效基准（MJD）：用户输入优先，缺省源 T0；自定义基准（≠源 T0）时横轴按 mjd 换算
  const refMJD = _lcEffRefMJD();
  const useRefX = refMJD != null && refMJD !== _lcT0MJD;
  // 点的横轴 x（秒，静止系除 zfac）：默认 p.time/zfac；自定义基准时
  // x = (p.mjd − ref)×86400/zfac，无 mjd 的点回退 (T0 + time/86400)；
  // 源无 T0 且点无 mjd：无法换算，返回 null 不绘制
  const toX = (p) => {
    if (!useRefX) return p.time / zfac;
    const mjd = (p.mjd != null) ? p.mjd : (_lcT0MJD != null ? _lcT0MJD + p.time / 86400 : null);
    return mjd != null ? (mjd - refMJD) * 86400 / zfac : null;
  };
  // 自定义基准且源有 T0 时，图坐标 = T0 相对秒数/zfac 的纯平移（拟合线/置信带叠加用）
  const dxRef = (useRefX && _lcT0MJD != null) ? (_lcT0MJD - refMJD) * 86400 / zfac : 0;
  // 顶部副轴（mjd 模式零点与横轴同一基准）
  const topMode = document.getElementById('topAxis')?.value || 'day';
  _lcTopAxis = (topMode === 'none' || (topMode === 'mjd' && refMJD == null))
    ? null
    : { mode: topMode, t0mjd: refMJD };

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
        const x = toX(p);
        if (x == null) return null;
        // 时间误差（秒）与横轴同坐标系：静止系同样除 (1+z)（时标平移不影响误差宽度）
        const terr = (p.time_err != null && p.time_err > 0) ? p.time_err / zfac : null;
        return { x, y: toY(m.y), err: toYerr(m.y, m.err), terr, raw: p, clipped: m.clipped };
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
          clip: true,   // 手动坐标范围下把散点裁剪在 chartArea 内（Chart.js 默认对有点半径的散点不裁剪）
          pointRadius: 3,
          pointHoverRadius: 5,
          _errorValues: vals.map(d => d.err),
          _timeErrValues: vals.map(d => d.terr),
          _clippedFlags: vals.map(d => d.clipped),
          _telescope: vals.map(d => d.raw.telescope || null),
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
        const x = toX(p);
        if (x == null) return null;
        return { x, y: toY(m.y), raw: p, clipped: m.clipped };
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
          clip: true,   // 同探测点：裁剪到 chartArea
          pointStyle: 'triangle',
          pointRadius: 5,
          pointRotation: 180,
          _errorValues: [],
          _telescope: uv.map(d => d.raw.telescope || null),
          _isUpperLimit: true,
          _band: band,
        });
      }
    }
  }

  // ── 叠加拟合曲线（虚线，按绘制范围 ~120 点；order 保证画在最上层） ──
  for (const fit of lcFits) {
    // 自定义基准但源无 T0：拟合内部坐标（T0 相对秒数）无法换算到新横轴，跳过叠加
    if (useRefX && _lcT0MJD == null) continue;
    const t0 = fit.pmin ?? fit.tmin, t1 = fit.pmax ?? fit.tmax;
    if (!(t0 > 0) || !(t1 > t0)) continue;
    const ts = [];
    for (let i = 0; i < 120; i++) ts.push(t0 * Math.pow(t1 / t0, i / 119));
    const data = [];
    for (const tt of ts) {
      const f = fitModelFlux(fit.model, fit.params, tt);
      if (!(f > 0) || !isFinite(f)) continue;
      data.push({ x: tt / zfac + dxRef, y: toY(f) });
    }
    // 1σ 置信带（半透明阴影；先压入上/下边界，拟合线后画在其上）
    const band = fitBandPoints(fit, ts);
    if (band && band.length > 2) {
      const hiPts = [], loPts = [];
      for (const b of band) {
        const yHi = toY(b.hi);
        const yLo = toY(Math.max(b.lo, LOG_AXIS_FLOOR));
        if (isFinite(yHi) && isFinite(yLo)) {
          hiPts.push({ x: b.x / zfac + dxRef, y: yHi });
          loPts.push({ x: b.x / zfac + dxRef, y: yLo });
        }
      }
      if (hiPts.length > 2) {
        datasets.push({
          type: 'line', label: '', data: hiPts,
          borderWidth: 0, pointRadius: 0, pointHoverRadius: 0,
          showLine: true, fill: false, order: -1, _isFit: true, clip: true,
        });
        datasets.push({
          type: 'line', label: '', data: loPts,
          borderWidth: 0, pointRadius: 0, pointHoverRadius: 0,
          showLine: true, fill: '-1', backgroundColor: fit.color + '2e',
          order: -1, _isFit: true, clip: true,
        });
      }
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
      clip: true,   // 拟合线/置信带同样裁剪在 chartArea 内
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
        // 自定义基准下线性轴的 x 可为负（基准前的点）；log 轴仍只取正值
        if (isFinite(p.x) && (p.x > 0 || (useRefX && xType !== 'logarithmic'))) allX.push(p.x);
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
          // 自定义基准时 x 可为负，不做 ≥0 钳制
          r = { min: useRefX ? mn - pad : Math.max(0, mn - pad), max: mx + pad };
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
    plugins: [errorBarPlugin, lcNowLinePlugin, lcSpecLinesPlugin, lcTopAxisPlugin, dragRectPlugin],
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
              if (ds._band) txt += ` · 波段: ${ds._band}`;
              const tel = ds._telescope ? ds._telescope[ctx.dataIndex] : null;
              if (tel) txt += ` · 望远镜: ${tel}`;
              return txt;
            },
          },
        },
      },
      scales: {
        x: {
          type: xType,
          reverse: false,
          title: {
            display: true,
            text: restFrame
              ? (useRefX ? 't/(1+z) since 基准  (s)' : 't/(1+z)  (s)')
              : (useRefX ? 'time since 基准  (s)' : 'time since T0  (s)'),
            color: cc.tick, font: fonts.title,
          },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: { color: cc.tick, font: fonts.tick, callback: (v) => sciTick(v) },
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
    _attachSpecHover(ctx);   // 光谱竖线悬停提示（同一 canvas 只挂一次）
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
