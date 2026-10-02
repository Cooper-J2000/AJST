// === specphot 谱图（自建 canvas 2D 图；零新增依赖，宿主无 D3/plotly，Chart.js 亦不引入） ===
// ① 谱预览：谱曲线（自动线性/对数，以可读为准）+ 通带半透明竖带（API-5 曲线覆盖区，IA-3：纯前端
//    叠加、勾选即变不触发计算）+ 掩膜斜纹（U-13 框选 ⇒ S.maskRanges ⇒ 请求体 mask，改动走 ctx.dirty()
//    即 IA-4 fresh→stale）+ 宿主线表竖线标记（import 宿主 spec_lines.js 的 SPEC_LINE_GROUPS）。
// 数据源：上传/粘贴件用 S.upload 内存数组（F-0i6/IA-11：绝不另写介质）；库内谱用宿主
// GET /spectra/<id>（观测系真空 λ，与后端读路径同一转换）。缺数据降级为只画通带竖带 + 提示。
// U-13 交互（P1 裁量）：普通拖选 = 加入掩膜段；点击掩膜段 = 删除；Shift+拖选 = 缩放；
// 双击 / 「恢复全域」 = 回全域。U-50 手输与确认 UI（U-50/U-52 语义）归 P1b（图注已注明）。
import { api } from '../api.js';
import { esc, escAttr } from '../utils.js';
import { txt22Text } from './export.js';
import { SPEC_LINE_GROUPS } from '../spec_lines.js';

const C_MAX_EXCLUDE = 32;   // Q-2 / V-13（与后端 specphot/constants.py 同值）
const C_DRAW_PTS = 2400;    // 绘制降采样上限（F-109 同族：只改图不改数）
const C_SMOOTH_BOX_MAX_PX = 7;  // U-51 核长上限（与后端 constants.py 同值，越界 E-14）
const TXT26 = '本图为显示平滑（boxcar 核长 N 像元，即全宽 N 像元），只用于看图；'
  + '所有数值、误差与导出件都取自未平滑的逐点谱。（TXT-26，U-51 开启时恒显）';
const LINE_GROUPS = ['h', 'he', 'si_ii', 'ca_ii', 'fe_ii', 'mg_ii'];   // 默认叠加的宿主线组
const HINT = 'U-13（P1 行为）：拖选 = 加入掩膜段 · 点击掩膜段 = 删除 · Shift+拖选 = 缩放 · '
  + '双击/按钮 = 恢复全域（确认 UI 与 U-50 手输归 P1b；掩膜段进请求体 mask，改段即转 stale）';

let _el = null, _ctx = null, _cv = null, _view = null, _drag = null, _spec = null, _dom = null;
let _wired = false;
let _capEl = null;
let _deredResp = null;      // F-89①：API-3 响应（含 de_reddened），U-47 门控在 draw 时判
const _curves = new Map();  // filter_id → {lam,t} | 'pending'（API-5 纯叠加缓存，不进任何哈希）

export function renderSpecPlot(el, ctx) {
  if (!el) return;
  _el = el; _ctx = ctx;
  el.innerHTML = `
    <div class="card mb-3"><div class="card-body py-2">
      <div class="d-flex align-items-center">
        <strong>① 谱预览</strong>
        <span class="small text-secondary ms-2">λ 轴 Å；竖带 = 勾选波段的曲线覆盖区（IA-3 纯叠加）；
          斜纹 = 掩膜段（n_masked_pixels 由计算回显）</span>
        <button class="btn btn-outline-secondary btn-sm ms-auto sp-zreset" title="恢复全域（U-13）">恢复全域</button>
      </div>
      <canvas id="spCv" class="w-100 mt-1" style="height:240px;cursor:crosshair;touch-action:none"
        title="${escAttr(HINT)}"></canvas>
      <div class="small" id="spOverlayCaption"></div>
      <div class="small" id="spSmoothCaption"></div>
      <div class="small text-secondary">${HINT}</div>
    </div></div>`;
  _cv = el.querySelector('#spCv');
  _capEl = el.querySelector('#spOverlayCaption');
  _cv.addEventListener('mousedown', ev => { _drag = { x0: ev.offsetX, x1: ev.offsetX, shift: ev.shiftKey }; draw(); });
  _cv.addEventListener('mousemove', ev => { if (_drag) { _drag.x1 = ev.offsetX; draw(); } });
  _cv.addEventListener('mouseleave', () => { if (_drag) { _drag = null; draw(); } });
  _cv.addEventListener('dblclick', () => { _view = null; draw(); });
  el.querySelector('.sp-zreset').addEventListener('click', () => { _view = null; draw(); });
  // TXT-26（U-51 开启时常显）：图注随平滑开合渲染（纯显示，不触发计算、不转 stale）
  const spx = smoothPx(ctx.S);
  el.querySelector('#spSmoothCaption').innerHTML = spx
    ? `<span class="text-warning" title="${escAttr(TXT26)}">TXT-26：${escAttr(TXT26.replace('N 像元', spx + ' 像元'))}</span>`
    : '';
  if (!_wired) {
    _wired = true;
    window.addEventListener('mouseup', onUp);
    window.addEventListener('resize', () => { if (_cv && _cv.isConnected) draw(); });
  }
  ensureSpec(); updateOverlayCaption(); draw();
}

// ─── F-89①/U-47 双谱叠加（P2 切片 2d）：曲线 A=改正前、B=改正后=派生件未入库 ───
// U-47 开（S.params.overDered !== false，默认开）且 host_ext_mode ≠ off 且
// de_reddened.n_points > 0 ⇒ 生效；A、B 共用同一观测系真空 λ 横轴与同一纵轴
// 单位（F-89① 禁止为 B 单开一套轴）；TXT-22 全文随开合常驻图注（W-33 图注位）。
export function setCurveOverlay(resp) {
  _deredResp = (resp && resp.host_ext_mode !== 'off'
    && resp.de_reddened && Number(resp.de_reddened.n_points) > 0) ? resp : null;
  if (_el && _el.isConnected) { updateOverlayCaption(); draw(); }
}
function overlayActive() {
  return !!(_deredResp && _ctx && _ctx.S.params
    && _ctx.S.params.overDered !== false);
}
// P1-2（F-89② PNG 导出）：把当前谱图画布交给 export.exportContinuumPng。
// 只交引用不复制——导出函数自建副本并在其上补图注，本画布不被改写。
export function specCanvas() {
  return (_cv && _cv.isConnected) ? _cv : null;
}
function updateOverlayCaption() {
  if (!_capEl) return;
  // P2-3（U-45）：TXT-22 图注绑 host_ext_mode≠off（_deredResp 的门控集即
  // U-45≠off 且 de_reddened 可用），**不随 U-47 开关消失**——图注声明的是
  // "改正后曲线是本页派生件、未入库"这一口径，双谱叠不叠加都成立；曲线
  // 叠加本身的显示门控仍在 overlayActive()（draw 时判）。
  const t = _deredResp ? txt22Text(_deredResp) : '';
  _capEl.innerHTML = t
    ? `<span class="text-secondary" title="${escAttr(t)}">TXT-22：${esc(t)}</span>`
    : '';
}

// ─── 谱数据：上传件直取内存数组；库内谱宿主接口取（内存缓存一份，随谱切换失效） ───
function ensureSpec() {
  const S = _ctx.S;
  if (S.upload) {
    const key = 'up:' + S.upload.spec_hash;
    if (!_spec || _spec.key !== key) { _spec = { key, lam: S.upload.lam_aa, flux: S.upload.flux }; _view = null; }
    return;
  }
  if (S.sourceKind === 'catalog' && S.spectrumId != null) {
    const key = 'cat:' + S.spectrumId;
    if (_spec && _spec.key === key) return;
    _spec = null;
    api('GET', '/spectra/' + S.spectrumId).then(j => {
      const d = (j && j.data) || {};
      for (const k of Object.keys(d)) {
        const rows = (d[k] && d[k].spectra && d[k].spectra.data) || [];
        const pts = rows.map(r => [Number(r[0]), Number(r[1])])
          .filter(p => isFinite(p[0]) && isFinite(p[1]));
        if (pts.length) {
          pts.sort((a, b) => a[0] - b[0]);
          _spec = { key, lam: pts.map(p => p[0]), flux: pts.map(p => p[1]) };
          _view = null;
          break;
        }
      }
      draw();
    }).catch(() => { _spec = null; draw(); });   // 取不到谱 ⇒ 降级画通带竖带 + 提示
  } else _spec = null;
}

// ─── API-5 曲线覆盖区（缺曲线的 mono 波段不画；失败静默） ───
function bandBoxes() {
  const out = [];
  for (const id of _ctx.S.selectedBands) {
    let c = _curves.get(id);
    if (!c) {
      _curves.set(id, 'pending');
      api('GET', '/specphot/curve/' + encodeURIComponent(id)).then(r => {
        _curves.set(id, { lam: r.lam_aa, t: r.t }); draw();
      }).catch(() => _curves.delete(id));
      continue;
    }
    if (c === 'pending') continue;
    let mx = -Infinity;
    for (const v of c.t) if (v > mx) mx = v;
    let lo = null, hi = null;
    for (let i = 0; i < c.t.length; i++) if (c.t[i] > mx * 0.02) { if (lo == null) lo = c.lam[i]; hi = c.lam[i]; }
    if (lo != null) out.push({ id, lo, hi });
  }
  return out;
}

// ─── 主绘制 ───
function draw() {
  if (!_cv || !_cv.isConnected || !_ctx) return;
  const S = _ctx.S, W = _cv.clientWidth || 600, H = _cv.clientHeight || 240;
  const dpr = window.devicePixelRatio || 1;
  _cv.width = W * dpr; _cv.height = H * dpr;
  const g = _cv.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const M = { l: 58, r: 12, t: 12, b: 30 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
  let x0 = null, x1 = null;
  if (_spec && _spec.lam.length) { x0 = _spec.lam[0]; x1 = _spec.lam[_spec.lam.length - 1]; }
  const bands = bandBoxes();
  for (const b of bands) { if (x0 == null || b.lo < x0) x0 = b.lo; if (x1 == null || b.hi > x1) x1 = b.hi; }
  if (x0 == null) {
    g.fillStyle = '#888'; g.font = '12px sans-serif';
    g.fillText('缺谱与波段：装载谱并勾选波段后显示（谱数据缺失时只画通带竖带）', M.l, H / 2);
    return;
  }
  if (x1 - x0 < 1e-9) x1 = x0 + 1;
  if (_view) { x0 = _view[0]; x1 = _view[1]; }
  _dom = { x0, x1, M, pw };
  // y 域：视图内谱点 2–98 分位；动态范围 >100× 用对数（以可读为准）
  const ov = overlayActive() ? _deredResp.de_reddened : null;
  let vals = [];
  if (_spec) for (let i = 0; i < _spec.lam.length; i++) {
    const f = Number(_spec.flux[i]);
    if (isFinite(f) && _spec.lam[i] >= x0 && _spec.lam[i] <= x1) vals.push(f);
  }
  if (ov) for (let i = 0; i < ov.lam_obs_vac_aa.length; i++) {
    const lamI = Number(ov.lam_obs_vac_aa[i]);
    if (lamI < x0 || lamI > x1) continue;
    for (const k of ['flux_before', 'flux_after']) {
      const v = Number(ov[k][i]);
      if (isFinite(v)) vals.push(v);
    }
  }
  vals.sort((a, b) => a - b);
  const q = p => vals.length ? vals[Math.round(p * (vals.length - 1))] : null;
  let ylo = q(0.02), yhi = q(0.98);
  const useLog = ylo != null && ylo > 0 && yhi > 0 && yhi / ylo > 100;
  if (useLog) { ylo = Math.log10(ylo); yhi = Math.log10(yhi); }
  if (ylo == null) { ylo = 0; yhi = 1; }
  if (yhi - ylo < 1e-12) yhi = ylo + (ylo ? Math.abs(ylo) * 0.1 : 1);
  const pad = (yhi - ylo) * 0.06; ylo -= pad; yhi += pad;
  const X = v => M.l + (v - x0) / (x1 - x0) * pw;
  const Y = useLog
    ? v => M.t + (1 - (Math.log10(Math.max(v, 1e-300)) - ylo) / (yhi - ylo)) * ph
    : v => M.t + (1 - (v - ylo) / (yhi - ylo)) * ph;
  // 通带竖带（画在最底层）
  for (const b of bands) {
    const a = Math.max(X(b.lo), M.l), b2 = Math.min(X(b.hi), W - M.r);
    if (b2 <= M.l || a >= W - M.r) continue;
    g.fillStyle = 'rgba(30,144,255,0.13)'; g.fillRect(a, M.t, b2 - a, ph);
    g.fillStyle = '#1a5c96'; g.font = '9px sans-serif'; g.fillText(b.id, a + 2, M.t + 9);
  }
  // 掩膜斜纹（U-13 / U-50 同一个数组：S.maskRanges）
  for (const r of (S.maskRanges || [])) {
    const lo = Number(r[0]), hi = Number(r[1]);
    const a = Math.max(X(lo), M.l), b2 = Math.min(X(hi), W - M.r);
    if (!(b2 > a)) continue;
    g.save(); g.beginPath(); g.rect(a, M.t, b2 - a, ph); g.clip();
    g.strokeStyle = 'rgba(204,0,0,0.55)'; g.lineWidth = 1; g.beginPath();
    for (let x = a - ph; x < b2; x += 7) { g.moveTo(x, M.t + ph); g.lineTo(x + ph, M.t); }
    g.stroke(); g.restore();
    g.strokeStyle = 'rgba(204,0,0,0.9)'; g.strokeRect(a, M.t, b2 - a, ph);
  }
  // 谱曲线（或降级提示）
  g.font = '10px sans-serif';
  if (_spec && _spec.lam.length) {
    const lam = _spec.lam, fl = _spec.flux;
    const step = Math.max(1, Math.ceil(lam.length / C_DRAW_PTS));
    g.strokeStyle = '#1f6fb2'; g.lineWidth = 1; g.beginPath();
    let on = false;
    for (let i = 0; i < lam.length; i += step) {
      const v = Number(fl[i]);
      if (!isFinite(v) || (useLog && v <= 0)) { on = false; continue; }
      const px = X(lam[i]);
      if (px < M.l - 2 || px > W - M.r + 2) continue;
      if (!on) { g.moveTo(px, Y(v)); on = true; } else g.lineTo(px, Y(v));
    }
    g.stroke();
    drawSmoothed(g, X, Y, useLog, M, W, pw);
    if (ov) drawDered(g, X, Y, useLog, M, W, ov);
  } else {
    g.fillStyle = '#888'; g.font = '12px sans-serif';
    g.fillText('缺谱数据：上传/粘贴或选库内谱后显示曲线（当前只画通带竖带）', M.l + 6, M.t + ph / 2);
  }
  // 宿主线表标记（SPEC_LINE_GROUPS 按 z 折算：λ_观测 = λ0·(1+z)，F-38 位置参考）
  const z = Number(S.metaForm && S.metaForm.z) || 0;
  g.setLineDash([3, 3]); g.lineWidth = 1;
  for (const grp of SPEC_LINE_GROUPS) {
    if (!LINE_GROUPS.includes(grp.key)) continue;
    g.strokeStyle = grp.color; g.globalAlpha = 0.45;
    for (const l0 of grp.lines) {
      const lm = l0 * (1 + z);
      if (lm < x0 || lm > x1) continue;
      g.beginPath(); g.moveTo(X(lm), M.t); g.lineTo(X(lm), M.t + ph); g.stroke();
    }
    g.globalAlpha = 1;
  }
  g.setLineDash([]);
  // 框选橡皮筋
  if (_drag) {
    const a = Math.min(_drag.x0, _drag.x1), b2 = Math.max(_drag.x0, _drag.x1);
    g.fillStyle = _drag.shift ? 'rgba(255,165,0,0.18)' : 'rgba(204,0,0,0.15)';
    g.fillRect(a, M.t, b2 - a, ph);
  }
  // 轴与刻度
  const fmt = v => Math.abs(v) >= 1000 ? String(Math.round(v)) : String(Number(v.toPrecision(4)));
  g.fillStyle = '#888'; g.strokeStyle = '#ccc';
  for (const t of niceTicks(x0, x1, 7)) {
    g.beginPath(); g.moveTo(X(t), M.t + ph); g.lineTo(X(t), M.t + ph + 4); g.stroke();
    g.fillText(fmt(t), X(t) - 12, H - 10);
  }
  for (const t of (useLog ? logTicks(ylo, yhi) : niceTicks(ylo, yhi, 5))) {
    const v = useLog ? Math.pow(10, t) : t;
    g.beginPath(); g.moveTo(M.l - 4, Y(v)); g.lineTo(M.l, Y(v)); g.stroke();
    g.fillText(useLog ? fmt(v) : fmt(v), 6, Y(v) + 3);
  }
  g.fillText('λ (Å)', W - 44, H - 4);
  g.fillText(useLog ? 'Fλ (log)' : 'Fλ', 6, 10);
}

// ─── U-51（F-109⑤ 纯前端绘制）：display_smoothed 只在此处产生并只画在图上 ——
// 不进请求数值语义之外的任何状态、不进哈希、不进导出件、不触发计算/转 stale；
// 数值表与结果一律取自未平滑的逐点谱（TXT-26 随开合常驻图注）。
function smoothPx(S) {
  if (!S.params || !S.params.smooth_on) return 0;
  const v = Number(S.params.smooth_px);
  return (isFinite(v) && v >= 1 && v <= C_SMOOTH_BOX_MAX_PX && Number.isInteger(v)) ? v : 3;
}
function drawSmoothed(g, X, Y, useLog, M, W, pw) {
  const px = smoothPx(_ctx.S);
  if (!px || !_spec || !_spec.lam.length) return;
  const h = px >> 1, fl = _spec.flux, n = fl.length;
  g.strokeStyle = '#e08214'; g.lineWidth = 1.4; g.beginPath();
  let on = false;
  for (let i = 0; i < n; i++) {
    let sum = 0, cnt = 0;
    for (let j = Math.max(0, i - h); j <= Math.min(n - 1, i + h); j++) {
      const v = Number(fl[j]);
      if (isFinite(v) && (!useLog || v > 0)) { sum += v; cnt++; }
    }
    if (!cnt) { on = false; continue; }
    const lamI = Number(_spec.lam[i]);
    const pxx = X(lamI);
    if (pxx < M.l - 2 || pxx > W - M.r + 2) continue;
    if (!on) { g.moveTo(pxx, Y(sum / cnt)); on = true; } else g.lineTo(pxx, Y(sum / cnt));
  }
  g.stroke();
}

// ─── F-89① 曲线 A/B 绘制：与谱同轴同单位（观测系真空 λ 横轴，Fλ 纵轴），
// B 只从 A 派生一次（derivation_depth=1，F-89④）；legend 写明"派生件、未入库"。
function drawDered(g, X, Y, useLog, M, W, ov) {
  const n = ov.flux_after.length;
  const step = Math.max(1, Math.ceil(n / C_DRAW_PTS));
  const line = (arr, color, width) => {
    g.strokeStyle = color; g.lineWidth = width; g.beginPath();
    let on = false;
    for (let i = 0; i < n; i += step) {
      const v = Number(arr[i]);
      if (!isFinite(v) || (useLog && v <= 0)) { on = false; continue; }
      const px = X(Number(ov.lam_obs_vac_aa[i]));
      if (px < M.l - 2 || px > W - M.r + 2) continue;
      if (!on) { g.moveTo(px, Y(v)); on = true; } else g.lineTo(px, Y(v));
    }
    g.stroke();
  };
  line(ov.flux_before, '#555', 1);            // 曲线 A = 改正前（银消后装载态）
  line(ov.flux_after, '#c0392b', 1.4);        // 曲线 B = 改正后（本页派生件）
  g.font = '10px sans-serif'; g.textAlign = 'right';
  g.fillStyle = '#555';
  g.fillText('— 曲线 A：改正前', W - M.r - 4, M.t + 10);
  g.fillStyle = '#c0392b';
  g.fillText('— 曲线 B：改正后 = 派生件、未入库（U-47）', W - M.r - 4, M.t + 22);
  g.textAlign = 'left';
}

// ─── U-13 交互收尾：Shift=缩放；点击=删段；拖选=加段（改段即 dirty ⇒ stale） ───
function onUp() {
  if (!_drag || !_cv || !_cv.isConnected || !_ctx) { _drag = null; return; }
  const S = _ctx.S;
  const a = Math.min(_drag.x0, _drag.x1), b = Math.max(_drag.x0, _drag.x1);
  const shift = _drag.shift; _drag = null;
  if (!_dom) { draw(); return; }
  const px2lam = px => _dom.x0 + (px - _dom.M.l) / _dom.pw * (_dom.x1 - _dom.x0);
  const lamA = px2lam(a), lamB = px2lam(b);
  if (shift) { if (b - a > 4) _view = [lamA, lamB]; draw(); return; }
  if (b - a < 4) {   // 点击：命中掩膜段即删（P1：再点即删；确认 UI 归 P1b）
    const hit = (S.maskRanges || []).findIndex(r => Number(r[0]) <= lamA && lamA <= Number(r[1]));
    if (hit >= 0) { S.maskRanges.splice(hit, 1); _ctx.dirty(); return; }
    draw(); return;
  }
  if ((S.maskRanges || []).length >= C_MAX_EXCLUDE) {
    S.featureNotice = `掩膜段已达上限 ${C_MAX_EXCLUDE}（Q-2），先删一段再加`;
    _ctx.refreshChrome(); return;
  }
  S.maskRanges.push([lamA, lamB]);   // px 递增 ⇒ lamA < lamB
  _ctx.dirty();                      // saveSession + idle→dirty / fresh→stale（IA-4/U-13）
}

function niceTicks(a, b, n) {
  const span = b - a, step0 = span / Math.max(1, n);
  const mag = Math.pow(10, Math.floor(Math.log10(step0 || 1)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  const out = [];
  for (let t = Math.ceil(a / step) * step; t <= b + step * 1e-6; t += step) out.push(t);
  return out;
}
function logTicks(a, b) {
  const out = [];
  for (let d = Math.ceil(a); d <= Math.floor(b); d++)
    for (const m of [1, 2, 5]) { const v = Math.log10(m) + d; if (v >= a && v <= b) out.push(v); }
  return out;
}

// ─── 对照面板比对图（IA-12：Δm 对波长 + 残差条；数据全部来自同一次响应，不发请求） ───
export function renderComparePlot(el, ctx) {
  if (!el) return;
  const r = ctx.S.lastResponse;
  const rows = r ? (r.results || []).filter(x => x.delta_m != null && x.lambda_pivot_aa != null) : [];
  if (!rows.length) { el.innerHTML = '<div class="small text-secondary">无 Δm 数据（需配对锚点且该波段合成成功）。</div>'; return; }
  el.innerHTML = '<canvas style="width:100%;height:220px;display:block"></canvas>'
    + '<div class="small text-secondary">TXT-4：合成 vs 锚点（预览，未入库）· 点 = Δm=m_obs−mag（按 λ_pivot），'
    + '竖条 = Δm_err，方框 = 手加锚点行，实心圆 = 源表锚点行；红虚线 = Δm=0。数据只来自同一次响应（IA-12）。</div>';
  const cv = el.querySelector('canvas');
  const W = el.clientWidth || 600, H = 220, dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const M = { l: 48, r: 14, t: 14, b: 28 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
  const xs = rows.map(x => Number(x.lambda_pivot_aa));
  const ys = rows.map(x => Number(x.delta_m));
  const es = rows.map(x => Math.abs(Number(x.delta_m_err) || 0));
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  const padx = (x1 - x0) * 0.08 || 100; x0 -= padx; x1 += padx;
  let y0 = Math.min(...ys.map((y, i) => y - es[i])), y1 = Math.max(...ys.map((y, i) => y + es[i]));
  const pady = ((y1 - y0) || 0.2) * 0.12; y0 -= pady; y1 += pady;
  const X = v => M.l + (v - x0) / (x1 - x0) * pw, Y = v => M.t + (1 - (v - y0) / (y1 - y0)) * ph;
  const fmt = v => Math.abs(v) >= 1000 ? String(Math.round(v)) : String(Number(v.toPrecision(3)));
  g.font = '10px sans-serif';
  g.strokeStyle = '#d22'; g.setLineDash([4, 3]);
  g.beginPath(); g.moveTo(M.l, Y(0)); g.lineTo(W - M.r, Y(0)); g.stroke(); g.setLineDash([]);
  g.strokeStyle = '#e5e5e5';
  for (const t of niceTicks(y0, y1, 4)) {
    g.beginPath(); g.moveTo(M.l, Y(t)); g.lineTo(W - M.r, Y(t)); g.stroke();
    g.fillStyle = '#888'; g.fillText(fmt(t), 6, Y(t) + 3);
  }
  g.fillStyle = '#888';
  for (const t of niceTicks(x0, x1, 6)) g.fillText(fmt(t), X(t) - 12, H - 8);
  g.fillText('λ_pivot (Å)', W - 80, H - 4); g.fillText('Δm = m_obs − mag', 6, 10);
  rows.forEach((x, i) => {
    const px = X(xs[i]), py = Y(ys[i]);
    g.strokeStyle = '#666';
    g.beginPath(); g.moveTo(px, Y(ys[i] - es[i])); g.lineTo(px, Y(ys[i] + es[i])); g.stroke();
    if (x.m_obs_origin === 'manual') { g.strokeStyle = '#08c'; g.strokeRect(px - 3, py - 3, 6, 6); }
    else { g.fillStyle = '#333'; g.beginPath(); g.arc(px, py, 3, 0, 7); g.fill(); }
  });
}
