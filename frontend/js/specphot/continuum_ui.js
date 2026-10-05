// === specphot S2 连续谱拟合 UI（P2 切片 2b/2d；U-21/U-22 + U-45…U-47 + §4.2 FitResult 卡） ===
// 模型选择（§3.8.1 别名表）、host_ext 三态（F-87：off/fit/prescribe，Q-32）、结果卡
// （参数±区间、engine_status、closure、verdict、TXT-22 逐字）、双曲线叠加
// （attachCurveOverlay → specplot，F-89①）、曲线 B CSV 导出与谱图 PNG 导出
// （export.js §4.3 块 + F-89②/U-47 三件套的 PNG 支路）。
// S3 页签保持禁用（A-3/E-15）。
import { esc, escAttr } from '../utils.js';
import { txt22Text, exportContinuumCsv, exportContinuumPng } from './export.js';
import { setCurveOverlay, specCanvas } from './specplot.js';

const MODELS = ['pl', 'pl_dust', 'pl2', 'bb', 'pl_bb', 'dbb', 'poly'];   // U-21/U-22（§3.8.1；P2b 起 pl2 上线）
const LAWS = ['smc', 'lmc', 'mw', 'mw_f99', 'mw_ccm'];            // Q-14 五键
const C_HOST_EBV_MAX = 2.0;        // §7.5（与 constants.py 同值，仅输入闸）
function num(v, d = 4) {
  return (v == null || !isFinite(Number(v))) ? '—' : Number(v).toFixed(d);
}

// ─── API-3 请求体（§5.2.2；预处理块与 S1 buildPhotometryRequest 同源同通道） ──
// F-113① 共用块：errcol_choice（U-53 确认态）+ factor（U-49，P2 2c）+ smooth
// （U-51 核长，纯显示口径；越界值回落默认 3，服务端 Q-33 仍校验）
function buildPreprocessBlock(S) {
  const pp = {};
  if (S.params.errcol_choice) pp.errcol_choice = S.params.errcol_choice;
  if (Number(S.params.factor) > 1) pp.factor = Number(S.params.factor);
  if (S.params.smooth_on) {
    const px = Number(S.params.smooth_px);
    pp.smooth = (isFinite(px) && px >= 1 && px <= 7 && Number.isInteger(px)) ? px : 3;
  }
  return pp;
}

export function buildContinuumRequest(ctx) {
  const S = ctx.S;
  const p = S.contParams;
  const nOr = v => (v === '' || v == null || !isFinite(Number(v))) ? null : Number(v);
  const body = {
    models: [...p.models],                          // Q-13
    host_ext_mode: p.host_ext_mode,                 // Q-32（缺省 fit）
    law: p.law,                                     // Q-14
    mask: S.maskRanges.map(r => [Number(r[0]), Number(r[1])]),
    mw: { correct: S.params.mw_correct === true, ebv: nOr(S.params.ebv_override) },
    z: nOr(S.metaForm.z),                           // null = 谱记录 z（U-37 覆盖语义同 S1）
    // F-113①：三端点共用同一 preprocess{} 块 —— factor/smooth 与 S1 同源（S.params），
    // smooth 纯前端绘制，服务端不消费（T-77②③ 响应逐字节同）
    preprocess: buildPreprocessBlock(S),
  };
  if (nOr(p.rv) != null) body.rv = nOr(p.rv);       // 缺省 = rv_source 基准值（F-88③）
  if (p.rv_source) body.rv_source = p.rv_source;
  if (p.host_ext_mode === 'prescribe') body.ebv = nOr(p.ebv);   // Q-32：prescribe 必填
  if (p.models.includes('poly')) body.poly_order = Number(p.poly_order) || 3;
  if (S.sourceKind === 'catalog' && S.spectrumId != null) body.spectrum_id = S.spectrumId;
  else if (S.upload) {
    const m = S.metaForm;
    const meta = { lambda_frame: S.uploadFrame || 'vacuum', category: m.category || 'other' };
    if (String(m.ra || '').trim()) meta.ra_deg = String(m.ra).trim();
    if (String(m.dec || '').trim()) meta.dec_deg = String(m.dec).trim();
    if (nOr(m.z) != null) meta.z = nOr(m.z);
    if (nOr(m.mjd) != null) meta.mjd = nOr(m.mjd);
    if (S.upload.flux_unit) meta.flux_unit = S.upload.flux_unit;
    body.spectrum = { lam_aa: S.upload.lam_aa, flux: S.upload.flux,
                      flux_err: S.upload.flux_err || null, meta };
  }
  return body;
}

// ─── S2 结果卡（TXT-22 + verdict + 逐模型参数±区间 + engine_status + closure + 曲线 B） ──
export function renderContinuumResults(el, ctx) {
  const r = ctx.S.lastContinuum;
  if (!r) {
    el.innerHTML = '<div class="card mb-3"><div class="card-body py-2">'
      + '<strong>S2 连续谱拟合</strong><div class="small text-secondary mt-1">'
      + '尚未计算：在右侧选模型与宿主消光三态后点「计算」。</div></div></div>';
    return;
  }
  const rows = (r.fits || []).map(f => {
    // W-24（F-65/F-91②）：网格最优解落在搜索网格端点的参数，报告值旁标
    // 「边界值」（title 全句 + 角标），回显件 = 服务端 fit.grid_boundary_params
    const bnd = new Set(f.grid_boundary_params || []);
    const ps = Object.entries(f.params || {}).map(([k, v]) => {
      const lo = f.err_low ? f.err_low[k] : null, hi = f.err_high ? f.err_high[k] : null;
      const es = f.err_source ? f.err_source[k] : null;
      const mark = bnd.has(k)
        ? ' <sup class="text-warning" title="网格最优解落在搜索网格端点：报告值是边界值而非似然峰（F-65/CA-24，W-24）">边界值</sup>'
        : '';
      return `<div>${k} = ${num(v, 4)} (+${num(hi)}/−${num(lo)})${mark}`
        + `<span class="text-secondary"> [${esc(es || 'none')}]</span></div>`;
    }).join('');
    const cl = f.closure;
    const en = f.engine_status;
    return `<tr><td>${esc(f.model)}${f.model === r.verdict.best ? ' <span class="badge bg-success">best</span>' : ''}</td>
      <td>${num(f.chi2, 3)}/${f.dof}</td><td>${num(f.bic, 3)}</td><td>${f.n_par}</td>
      <td>${esc(f.comparable ? 'ok' : 'CA-44')}</td>
      <td><div class="small">${ps}</div></td>
      <td class="small">${en ? `status=${en.status}·opt=${num(en.optimality, 3)}·nfev=${en.nfev}·start=${en.n_starts}` : '线性层'}</td>
      <td class="small">${cl ? `${num(cl.factor_lo)}…${num(cl.factor_hi)}（三候选区间，TXT-7）` : '—'}</td></tr>`;
  }).join('');
  // P2b 评审（fit.warnings 接线）：S2 卡此前只显示 TXT-22/ebv_ignored/闸门行，
  // 顶层与逐模型 warnings（CA-24 边界值、CA-44、CA-11、CA-15、CA-38 等）被
  // 丢弃 ⇒ 就地渲染，码+reason+message 全文，不聚合不折叠（S2 条数有限）。
  const topWarn = (r.warnings || []).length
    ? `<div class="small text-warning mt-1">S2 告警：${(r.warnings || [])
      .map(w => `[${esc(w.code)}${w.reason ? '·' + esc(w.reason) : ''}] ${esc(w.message)}`)
      .join('；')}</div>` : '';
  const fitWarn = (r.fits || []).filter(f => (f.warnings || []).length)
    .map(f => `<div class="small text-warning mt-1">模型 ${esc(f.model)} 告警：${(f.warnings || [])
      .map(w => `[${esc(w.code)}${w.reason ? '·' + esc(w.reason) : ''}] ${esc(w.message)}`)
      .join('；')}</div>`).join('');
  const dr = r.de_reddened || {};
  const deredOn = r.host_ext_mode !== 'off' && Number(dr.n_points) > 0;
  // F-112（P2 2c）端点闸门行：仅 poly 拟合在请求内时 preprocess{} 的 S2 四键为
  // 真值；CA-48 触发时本行可见并写明"建议掩膜段未经确认不会被掩掉"（U-52 路径）
  const pp = r.preprocess || {};
  const gateRow = pp.n_high_leverage != null
    ? `<div class="small ${pp.baseline_edge_spread != null && pp.baseline_edge_spread > 0.25 ? 'text-warning' : 'text-secondary'} mt-1">`
      + `端点闸门（F-112）：baseline_edge_spread=${num(pp.baseline_edge_spread, 3)} ／ `
      + `edge_spread_after_downgrade=${num(pp.edge_spread_after_downgrade, 3)} ／ `
      + `n_high_leverage=${esc(pp.n_high_leverage)}（只标记不剔除，Σh_ii=p 恒成立）`
      + ((pp.suggested_edge_ranges || []).length
        ? ` ／ 建议掩膜段 ${esc(JSON.stringify(pp.suggested_edge_ranges))}——<b>未经确认不会被掩掉</b>（U-52 确认路径）`
        : '')
      + `</div>`
    : '';
  el.innerHTML = `<div class="card mb-3"><div class="card-body py-2">
    <div class="d-flex align-items-center"><strong>S2 连续谱拟合</strong>
      <span class="small text-secondary ms-2">host_ext_mode=${esc(r.host_ext_mode)} · law=${esc(r.law)} ·
      rv=${num(r.rv, 3)}(${esc(r.rv_source)}) · z_eff=${num(r.z_eff, 4)} ·
      拟合区 ${r.fit_region ? r.fit_region.n_pixels : '—'} px</span></div>
    ${r.host_ext_mode !== 'off' ? `<div class="small text-secondary mt-1 border-start border-3 ps-2">TXT-22：${esc(txt22Text(r))}</div>` : ''}
    ${r.ebv_ignored ? '<div class="small text-warning mt-1">Q-32/F-90②：off/fit 模式下携带的 ebv 已忽略（ebv_ignored=true），未参与计算。</div>' : ''}
    ${gateRow}
    <div class="table-responsive mt-1" style="max-height:340px;overflow:auto">
      <table class="table table-sm table-hover mb-0 small sp-result-table">
        <thead><tr><th>模型</th><th>χ²/dof</th><th>BIC</th><th>n_par</th><th>可比</th>
          <th>参数 ± 区间（err_source）</th><th>engine_status</th><th>closure</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    <div class="small text-secondary mt-1">verdict（F-29/T-17）：best=<b>${esc(String(r.verdict.best || '—'))}</b> ·
      F 检验 ${(r.verdict.ftest || []).map(e => `${esc(e.pair.join('⊂'))}: p=${num(e.p, 4)}→${esc(e.verdict)}`).join('；') || '—'} ·
      ΔBIC ${(r.verdict.dbic || []).map(e => `${esc(e.pair.join(' vs '))}: ${num(e.dbic, 3)}→${esc(e.verdict)}`).join('；') || '—'}</div>
    ${(r.verdict.davies || []).length ? `<div class="small text-warning mt-1">断点检验（F-29③/T-18，Davies）：${(r.verdict.davies || []).map(e =>
      `${esc(e.pair.join('⊂'))}: p=${num(e.p, 4)}（p_method=${esc(e.p_method)}，n_boot=${esc(e.n_boot)}）→<b>${esc(e.verdict)}</b>`
      + `；旁证 p_chi2mix=${num(e.p_chi2mix, 4)}（边界 χ² 混合 50:50，不得作为判定依据）`).join('；')}</div>` : ''}
    ${topWarn}${fitWarn}
    <div class="small text-secondary mt-1">de_reddened（曲线 B，纯派生件）：n_points=${dr.n_points ?? 0} ·
      derivation_depth=${dr.derivation_depth ?? 0} · curve_hash=${esc(String(dr.curve_hash || 'none'))} ·
      exported=${dr.exported === true}；图上双曲线叠加见①谱预览（U-47 开启时曲线 A/B 同轴同单位，F-89①）。</div>
  </div></div>`;
}

// ─── S2 右栏面板（U-21/U-22 模型 + U-45…U-47 三态 + U-46 换算行 + ⑤ 动作） ────
export function renderS2Panel(el, ctx) {
  const S = ctx.S, p = S.contParams;
  const missing = s2Missing(ctx);
  const computing = S.state === 'computing';
  const busyLeft = S.busyUntilTs > Date.now() ? Math.ceil((S.busyUntilTs - Date.now()) / 1000) : 0;
  const rv = Number(p.rv) > 0 ? Number(p.rv) : 3.1;   // 仅 U-46 常驻显示换算（非计算通路）
  const av = p.host_ext_mode === 'prescribe' && isFinite(Number(p.ebv))
    ? (rv * Number(p.ebv)) : null;
  const ebvDis = computing || p.host_ext_mode !== 'prescribe';
  // U-46（F-88/F-90②）：fit 态该框只读，显示由拟合 A_V 反算的 E(B−V)（响应键
  // ebv_display；只作显示，禁止回喂计算通路）
  const fitEbv = p.host_ext_mode === 'fit' && S.lastContinuum
    ? ((S.lastContinuum.fits || []).find(f => f.ebv_display != null) || {}).ebv_display
    : null;
  const ebvBoxVal = p.host_ext_mode === 'fit'
    ? (fitEbv == null ? '' : String(fitEbv)) : p.ebv;
  const ebvNote = p.host_ext_mode === 'fit'
    ? (fitEbv == null ? 'fit 态只读：计算后在此显示由拟合 A_V 反算的 E(B−V)（F-88）'
       : 'fit 态只读：由拟合 A_V 反算、仅供显示（F-88/F-90② 禁回喂）')
    : `A_V = R_V·E(B−V) = ${av == null ? '—' : num(av, 4)}（显示换算，rv_source 见响应）`;
  // U-47（F-89①）：双谱叠加开关（纯显示口径，不触发计算、不转 stale）
  const deredReady = !!S.lastContinuum && S.lastContinuum.host_ext_mode !== 'off'
    && Number((S.lastContinuum.de_reddened || {}).n_points) > 0;
  const csvTitle = !S.lastContinuum ? '先计算一次（API-3）'
    : S.lastContinuum.host_ext_mode === 'off'
      ? 'host_ext_mode=off：无 de-reddened 曲线（F-87①），导出禁用'
      : deredReady ? '导出 §4.3「宿主 de-reddened 曲线导出」块 CSV（TXT-11+TXT-22 头，纯前端派生件）'
        : '响应无 de_reddened 逐点数据，导出禁用';
  el.innerHTML = `<div class="card mb-3"><div class="card-body py-2 small">
    <strong>③ 参数（S2）</strong>
    <div class="mt-1">模型（U-21/U-22，§3.8.1 别名）：${MODELS.map(m =>
      `<label class="me-2"><input type="checkbox" class="form-check-input sp-cm" value="${m}"
        ${p.models.includes(m) ? 'checked' : ''} ${computing ? 'disabled' : ''}>${m}</label>`).join('')}</div>
    <div class="mt-1">host_ext_mode（U-45…U-47，F-87 三态）：${['off', 'fit', 'prescribe'].map(v =>
      `<label class="me-2"><input type="radio" name="spHem" class="sp-hem" value="${v}"
        ${p.host_ext_mode === v ? 'checked' : ''} ${computing ? 'disabled' : ''}>${v}</label>`).join('')}</div>
    <div class="mt-1">双谱叠加（U-47，F-89①：U-45≠off 时图上曲线 A=改正前、B=改正后=派生件、未入库；纯显示开关）：
      <input type="checkbox" class="form-check-input sp-ovd" ${p.overDered !== false ? 'checked' : ''}
        ${computing ? 'disabled' : ''}></div>
    <div class="mt-1">E(B−V)（U-46，仅 prescribe 必填，≤${C_HOST_EBV_MAX}）：
      <input type="number" min="0" max="${C_HOST_EBV_MAX}" step="any" class="form-control form-control-sm d-inline-block w-auto sp-ebv"
        value="${escAttr(ebvBoxVal)}" ${ebvDis ? 'disabled' : ''}>
      <span class="text-secondary">${ebvNote}</span></div>
    <div class="mt-1">law（U-32）：<select class="form-select form-select-sm w-auto d-inline sp-law" ${computing ? 'disabled' : ''}>
      ${LAWS.map(l => `<option ${p.law === l ? 'selected' : ''}>${l}</option>`).join('')}</select>
      R_V（留空 = rv_source 基准值）：<input type="number" step="any" class="form-control form-control-sm d-inline-block w-auto sp-rv" value="${escAttr(p.rv)}" ${computing ? 'disabled' : ''}>
      <select class="form-select form-select-sm w-auto d-inline sp-rvs" ${computing ? 'disabled' : ''}>
        ${['', 'nominal', 'intrinsic'].map(v => `<option value="${v}" ${p.rv_source === v ? 'selected' : ''}>${v || 'rv_source 缺省'}</option>`).join('')}</select></div>
    <div class="mt-1">poly_order（仅 poly）：<input type="number" min="1" max="6" step="1" class="form-control form-control-sm d-inline-block w-auto sp-po" value="${escAttr(p.poly_order)}" ${computing || !p.models.includes('poly') ? 'disabled' : ''}></div>
    <hr class="my-2"><strong>⑤ 动作</strong>
    <div class="d-flex gap-2 align-items-center mt-1">
      <button class="btn btn-primary btn-sm sp-ccompute" ${missing.length || computing || busyLeft ? 'disabled' : ''}>计算</button>
      <button class="btn btn-outline-secondary btn-sm sp-cexpjson" ${!S.lastContinuum || computing ? 'disabled' : ''}>导出 JSON</button>
      <button class="btn btn-outline-secondary btn-sm sp-cexpcsv" ${!deredReady || computing ? 'disabled' : ''}
        title="${escAttr(csvTitle)}">导出 CSV（曲线 B）</button>
      <button class="btn btn-outline-secondary btn-sm sp-cexppng" ${!deredReady || computing ? 'disabled' : ''}
        title="${escAttr(csvTitle.replace('CSV', 'PNG'))}">导出 PNG（双谱图，含 TXT-22 图注）</button>
      <span class="text-secondary">S2 拟合（API-3）；掩膜与预处理通道与 S1 同源（F-107③）</span></div>
    <div class="mt-1 text-warning small">${esc(S.busyNotice || '')}</div>
    <div class="mt-1 text-warning small">${esc(S.featureNotice || '')}</div>
    <div class="mt-1 text-danger small" role="alert">${esc(S.errorMsg || '')}</div>
    <div class="mt-1 ${missing.length ? 'text-warning' : 'text-success'}">缺项清单（IA-16）：${missing.length
      ? '<ul class="mb-0">' + missing.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>' : '无，可以计算'}</div>
  </div></div>`;
  el.querySelectorAll('.sp-cm').forEach(cb => cb.addEventListener('change', () => {
    p.models = [...el.querySelectorAll('.sp-cm:checked')].map(x => x.value); ctx.dirty(); renderS2Panel(el, ctx);
  }));
  el.querySelectorAll('.sp-hem').forEach(r => r.addEventListener('change', () => {
    p.host_ext_mode = r.value; ctx.dirty(); renderS2Panel(el, ctx);
  }));
  el.querySelector('.sp-ovd').addEventListener('change', e => {
    p.overDered = e.target.checked;               // U-47：纯显示开关，不转 stale
    ctx.refreshChrome();
  });
  el.querySelector('.sp-ebv').addEventListener('change', e => { p.ebv = e.target.value; ctx.dirty(); });
  el.querySelector('.sp-law').addEventListener('change', e => { p.law = e.target.value; ctx.dirty(); });
  el.querySelector('.sp-rv').addEventListener('change', e => { p.rv = e.target.value; ctx.dirty(); });
  el.querySelector('.sp-rvs').addEventListener('change', e => { p.rv_source = e.target.value; ctx.dirty(); });
  el.querySelector('.sp-po').addEventListener('change', e => { p.poly_order = e.target.value; ctx.dirty(); });
  el.querySelector('.sp-ccompute').addEventListener('click', () => computeContinuum(ctx));
  el.querySelector('.sp-cexpjson').addEventListener('click', () => {
    const keep = S.lastResponse;              // 临时换出，不动 S1 的结果状态
    S.lastResponse = S.lastContinuum;
    try { ctx.exportJson(S); } finally { S.lastResponse = keep; }
  });
  el.querySelector('.sp-cexpcsv').addEventListener('click', () =>
    exportContinuumCsv(S.lastContinuum));     // §4.3 B 块（TXT-11+TXT-22 头，F-44 名）
  el.querySelector('.sp-cexppng').addEventListener('click', () =>
    exportContinuumPng(S.lastContinuum, specCanvas()));   // P1-2：谱图 PNG（F-89②/U-47）
}

export function s2Missing(ctx) {
  const S = ctx.S, p = S.contParams;
  const out = [];
  if (!ctx.isAuthed()) out.push('未登录：计算需要登录');
  if (!((S.sourceKind === 'catalog' && S.spectrumId != null) || !!S.upload)) out.push('缺谱：先装载一条谱');
  if (!p.models.length) out.push('缺模型：至少勾选 1 个模型（U-21/U-22）');
  if (p.host_ext_mode === 'prescribe') {
    const v = Number(p.ebv);
    if (p.ebv === '' || !isFinite(v) || v < 0) out.push('prescribe 需给出 E(B−V)（U-46，无默认）');
    else if (v > C_HOST_EBV_MAX) out.push(`E(B−V) 超过 C_HOST_EBV_MAX=${C_HOST_EBV_MAX}（Q-32）`);
  }
  return out;
}

// ─── S2 计算（API-3；状态机与 S1 compute 同一套 IA 状态） ────────────────────
export async function computeContinuum(ctx) {
  const S = ctx.S;
  if (s2Missing(ctx).length || S.state === 'computing') return;
  if (S.abortCtrl) S.abortCtrl.abort();
  S.abortCtrl = new AbortController();
  S.state = 'computing'; S.busyNotice = S.errorMsg = S.featureNotice = null;
  ctx.refreshChrome();
  try {
    const resp = await ctx.spPost('/specphot/continuum', buildContinuumRequest(ctx), S.abortCtrl.signal);
    S.lastContinuum = resp;
    S.state = 'fresh';
  } catch (e) {
    if (e.name === 'AbortError') { S.state = 'dirty'; S.busyNotice = '已取消'; }
    else if (e.status === 429) {
      const secs = Math.max(1, Number((e.payload || {}).retry_after_s) || 2);
      S.busyUntilTs = Date.now() + secs * 1000; S.busyNotice = null; S.state = 'dirty';
      ctx.startBusyCountdown && ctx.startBusyCountdown();
    } else { S.errorMsg = e.message; S.state = 'error'; }
  } finally {
    S.abortCtrl = null;
    ctx.refreshChrome();
  }
}

// F-89①/U-47 双曲线叠加（P2 切片 2d 实装）：把 API-3 响应交给 specplot。
// U-47 开（S.params.overDered !== false，默认开）且 host_ext_mode ≠ off 且
// de_reddened.n_points > 0 ⇒ 图上曲线 A（flux_before）与 B（flux_after）同轴同
// 单位；off/无曲线 ⇒ 清除叠加只画 A。门控与绘制都在 specplot（draw 时判），
// 这里只做状态移交；workbench.refreshPlot 每次全区刷新都会重调本接口。
export function attachCurveOverlay(_plotEl, resp) {
  setCurveOverlay(resp || null);
}
