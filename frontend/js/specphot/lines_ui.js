// === specphot S3 谱线测量 UI（P3 切片 2；U-23…U-27/U-30/U-31 三步向导 + §4.2 LineResult 卡） ===
// 三步状态机（U-23/§3.9）：步1 选线区（线区候选来自宿主 spec_lines.js——位置标记，
// M-6 未复核不得用于速度 + line_kind 确认 U-30，未确认「下一步」禁用 E-09，W-23）
// → 步2 定基线（U-25 手改阶数；基线形态说明随 line_kind 同步切换加性/乘性，W-23）
// → 步3 拟合轮廓（U-26 轮廓 + U-27 R + F-38/M-6 的 line_frame + vel_* 控件禁用）。
// 结果卡复用 S1/S2 的「结果卡机器」形态（表 + err_source/err_scope 逐键 + 告警区），
// 导出走 export.js 的 §4.3 谱线块（exportLinesCsv）。vel_*/z_fit 本期恒 null（M-6）。
import { esc, escAttr } from '../utils.js';
import { SPEC_LINE_GROUPS } from '../spec_lines.js';
import { exportLinesCsv, exportAbsorberCsv } from './export.js';

const PROFILES = ['gauss1', 'gauss2', 'lorentz'];   // U-26；voigt 全库无 R ⇒ 禁用（Q-21/F-39）
const C_LINE_WIN = 40;                              // §7.10（与 constants.py 同值）
const C_POLY_ORDER_MAX = 7;                         // F-62④
const C_MAX_BOOT = 1000;                            // Q-15
// TXT-8（S3 步 3 脚注，常驻）与 TXT-15（步 1 线型旁，随 line_kind 语义切换，W-23）
const TXT8 = '未提供仪器分辨率时，宽度是观测宽度（含仪器卷积），不是本征线宽。';
const TXT15 = {
  emission: '发射线的流量是「高出连续谱的部分」（加性）；EW 的符号约定为发射正（F-66/F-67）。',
  absorption: '吸收线的「深度」是「低于连续谱的部分」（乘性）；EW 的符号约定为吸收负（F-66/F-67）。',
};
// M-6/F-38：宿主线表整数 Å 且未标空气/真空帧（82–87 km/s 系统差）⇒ 只作位置标记
const M6_TIP = 'M-6/F-38：宿主线表静止系波长全为整数 Å 且未标空气/真空帧（偏移即 '
  + '82–87 km/s 系统差），未经复核只作位置标记，不得用于速度；vel_*/z_fit 恒 null。';
// TXT-24（U-48 任一项开启时常驻；CA-45 触发时换成"系统项"那句，F-100③）与
// TXT-25（forest_stats 空值旁常驻说明；闭集四值，T-71）
const TXT24 = '该 log N(H I) 来自 Lyα 阻尼翼的 Voigt 拟合：z 固定在金属线测得区间内（z_source）、'
  + 'b 不是自由参数（b_source 写明它来自哪一路），误差是 Δχ²=1 剖面区间 ⇒ 只含随机项，'
  + '不含连续谱安置的系统项。同一卡同屏给两个连续谱族的值与四个区间端点，以及二者的差 wing_spread_dex。';
const TXT24_CA45 = '两族之差已超过统计误差 ⇒ 主导项是连续谱安置，不得当 1σ 读。';
const TXT25 = '本模块不产出 Lyman-森林统计量与中性氢分数：forest_stats 恒为空值，'
  + 'forest_stats_reasons[] 给出全部适用原因（单条视线 / 连续谱未定 / 分辨率低于参照 / LLS 弥散）。'
  + '原因集是闭集，不得自创第五条、不得留空。';
const U48_TIP = 'U-48/P3d：三开关只随 API-4 提交（F-105③）；未勾 absorber_ident 时另两项禁用'
  + '（不静默连带开启）；R 不可得（r_source=\'none\'，F-69）时 wing_logn 禁用——阻尼翼的形状需要仪器宽度。'
  + 'M-6/M-4 前置缺 ⇒ 输出按缺前置降级（null + 书面原因）；全关时数值与既有键与基线逐字节相同（T-72①）。';

function num(v, d = 4) {
  return (v == null || !isFinite(Number(v))) ? '—' : Number(v).toFixed(d);
}
function nOr(v) {
  return (v === '' || v == null || !isFinite(Number(v))) ? null : Number(v);
}

// ─── API-4 请求体（§5.2.3；预处理/掩膜/银消块与 S1/S2 同源同通道，F-113①） ──
export function buildLinesRequest(ctx) {
  const S = ctx.S, p = S.lineParams;
  const body = {
    line: {
      species: p.species || null,
      lambda_rest_aa: Number(p.lambda_rest_aa),
      id_table: p.id_table || null,
    },
    line_kind: p.line_kind,                                   // 必填无默认（Q-18/F-66）
    window_halfwidth_aa: nOr(p.halfwidth) ?? C_LINE_WIN,      // F-35
    baseline: { order: Number(p.baseline_order) || 1 },       // F-36（side_px/iter 服务端钉死）
    profile: p.profile,                                       // U-26
    sky_handling: p.sky_handling || 'mask',                   // U-31
    line_frame: p.line_frame || null,                         // F-38：用户核对后填
    n_boot: Number(p.n_boot) || 200,
    mw: { correct: S.params.mw_correct === true, ebv: nOr(S.params.ebv_override) },
    mask: S.maskRanges.map(r => [Number(r[0]), Number(r[1])]),
    preprocess: (S.params.errcol_choice || Number(S.params.factor) > 1)
      ? { ...(S.params.errcol_choice ? { errcol_choice: S.params.errcol_choice } : {}),
          ...(Number(S.params.factor) > 1 ? { factor: Number(S.params.factor) } : {}) } : {},
  };
  if (nOr(p.R) != null) body.R = nOr(p.R);                    // U-27（缺省 r_source='none'）
  if (nOr(p.z) != null) body.z = nOr(p.z);                    // null = 谱记录/默认 0（Q-12）
  if (String(p.err_seed).trim() !== '' && nOr(p.err_seed) != null) {
    body.err_seed = Number(p.err_seed);                       // Q-35（缺省 = spec_hash 派生）
  }
  // U-48 三开关（F-105③：只随 API-4 提交；联动在渲染层禁用 + 此处不携带未勾项，
  // 全关 ⇒ 不带 diagnostics 键，与基线请求同形，T-72①）
  const diag = {};
  if (p.absorber_ident) {
    diag.absorber_ident = true;
    if (p.wing_logn) diag.wing_logn = true;
    if (p.metal_sat_check) diag.metal_sat_check = true;
  }
  if (Object.keys(diag).length) body.diagnostics = diag;
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

// 缺项清单（IA-16）：步 3 的 E-09 闸在「下一步」按钮上（W-23），这里管计算闸
export function linesMissing(ctx) {
  const S = ctx.S, p = S.lineParams;
  const out = [];
  if (!((S.sourceKind === 'catalog' && S.spectrumId != null) || !!S.upload)) out.push('缺谱：先装载一条谱');
  if (!(Number(p.lambda_rest_aa) > 0)) out.push('缺线心：line.lambda_rest_aa 必填且为正数（F-34）');
  if (!['emission', 'absorption'].includes(p.line_kind)) {
    out.push('缺线型：line_kind 未确认（U-30 必选无默认，未确认不得进步 3 —— E-09）');
  }
  if (p.line_kind === 'absorption' && p.sky_handling === 'subtract') {
    out.push("U-31/F-72②：天光『按线形扣除』只作用于天光发射表（F-86 扣除 [O I] 气辉线后再测吸收线）——"
             + '若线心本身落在气辉线上仍会被 F-72③ 拒绝；确认这是你要的操作');
  }
  return out;
}

// ─── 三步向导（U-23 步骤条；回退不清后续，但标「需重算」，W-23 联动） ────────
export function renderLinesPanel(el, ctx) {
  const S = ctx.S, p = S.lineParams;
  const computing = S.state === 'computing';
  const busyLeft = S.busyUntilTs > Date.now() ? Math.ceil((S.busyUntilTs - Date.now()) / 1000) : 0;
  const step = S.lineStep || 1;
  const kindOk = ['emission', 'absorption'].includes(p.line_kind);
  const canNext1 = kindOk && Number(p.lambda_rest_aa) > 0;
  const missing = linesMissing(ctx);
  // W-23：步 1 的 line_kind 切换会改变步 2 基线形态与步 3 拟合 ⇒ 已有结果标「需重算」
  const staleBadge = S.lineStale && S.lastLines
    ? ' <span class="badge bg-warning text-dark">参数已改动，需重算</span>' : '';
  const stepBar = ['选线区', '定基线', '拟合轮廓'].map((t, i) =>
    `<span class="${i + 1 === step ? 'fw-bold' : 'text-secondary'}">${i + 1}. ${t}</span>`)
    .join('<span class="text-secondary mx-1">→</span>');
  let bodyHtml = '';
  if (step === 1) {
    // F-34：线区候选来自宿主 spec_lines.js 线表（位置标记，M-6 未复核）；U-30 线型必选
    bodyHtml = `
    <div class="mt-1">线区（F-34/U-24，候选 = 宿主 spec_lines.js，<span title="${escAttr(M6_TIP)}">只作位置标记 ⚠</span>）：
      <select class="form-select form-select-sm w-auto d-inline sp-lng">
        <option value="">手输 / 选组…</option>
        ${SPEC_LINE_GROUPS.filter(g => g.lines && g.hasZ).map(g =>
          `<option value="${escAttr(g.key)}" ${p.id_table === 'spec_lines.js#' + g.key ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}
      </select>
      species：<input type="text" class="form-control form-control-sm d-inline-block w-auto sp-lspecies"
        value="${escAttr(p.species)}" title="${escAttr(M6_TIP)}">
      λ_rest(Å)：<input type="number" step="any" min="0" class="form-control form-control-sm d-inline-block w-auto sp-llam"
        value="${escAttr(p.lambda_rest_aa)}">
      半宽（F-35，±Å）：<input type="number" step="any" min="1" class="form-control form-control-sm d-inline-block w-auto sp-lhw"
        value="${escAttr(p.halfwidth)}"></div>
    <div class="mt-1" title="${escAttr(TXT15[p.line_kind] || 'F-66：两类线在连续谱两侧角色相反，未确认不得进步 3（E-09）')}">
      线型（U-30，必选无默认）：${['emission', 'absorption'].map(k =>
        `<label class="me-2"><input type="radio" name="spLk" class="sp-lkind" value="${k}"
          ${p.line_kind === k ? 'checked' : ''} ${computing ? 'disabled' : ''}>${k === 'emission' ? '发射 emission' : '吸收 absorption'}</label>`).join('')}
      <span class="text-secondary small">TXT-15：${esc(TXT15[p.line_kind] || '选定为止——发射加性 / 吸收乘性，角色相反（F-66）')}</span></div>
    <div class="mt-1">天光处理（U-31）：
      <label class="me-2"><input type="radio" name="spSky" class="sp-lsky" value="mask"
        ${p.sky_handling !== 'subtract' ? 'checked' : ''} ${computing ? 'disabled' : ''}>mask（剔除）</label>
      <label class="me-2" title="F-86/U-31（P3c 上线）：线窗内命中的天光发射段（C_MASK_EMIS_TABLE 的 [O I] 气辉线）按线形联合线性扣除；失败自动退回 mask + CA-37。大气吸收带永远只能 mask（F-72①）">
        <input type="radio" name="spSky" class="sp-lsky" value="subtract"
        ${p.sky_handling === 'subtract' ? 'checked' : ''} ${computing ? 'disabled' : ''}>subtract（按线形扣除，F-86）</label>
      <span class="text-secondary small">大气吸收带永远只能 mask（F-72①）</span></div>`;
  } else if (step === 2) {
    const additive = p.line_kind !== 'absorption';
    bodyHtml = `
    <div class="mt-1">基线形态（W-23：随步 1 的线型同步切换，F-36/F-66）：</div>
    <div class="border-start border-3 ps-2 mt-1 ${additive ? 'text-success' : 'text-primary'}">
      ${additive
        ? '发射线 = <b>加性</b>基线：F(λ) = C(λ) + L(λ)，L ≥ 0；线区外残差 F−C 应回到 0；line_flux = ∫(F−C)dλ。'
        : '吸收线 = <b>乘性</b>基线：F(λ) = C(λ)·[1 − r(λ)]，0 ≤ r ≤ 1；线区外残差 F/C−1 应回到 0；depth = 1−F/C（线心）。'}</div>
    <div class="mt-1 small text-secondary">EW 符号（F-67）：内部恒算有符号 W_signed，对外 ew_obs_aa=|W| 并同时回显
      line_kind 与 ew_signed_aa——${additive ? '发射 > 0' : '吸收 < 0'}；${esc(TXT15[p.line_kind] || '')}</div>
    <div class="mt-1">基线阶数（U-25/F-36，Chebyshev·lnλ，每侧 25 点、2 次 σ 裁剪——服务端钉死 C_BASE_SIDE/C_BASE_ITER）：
      <input type="number" min="0" max="${C_POLY_ORDER_MAX}" step="1"
        class="form-control form-control-sm d-inline-block w-auto sp-lorder" value="${escAttr(p.baseline_order)}"
        ${computing ? 'disabled' : ''}></div>
    <div class="mt-1 small text-secondary">掩膜段与预处理通道与 S1/S2 同源（F-107③，F-113①）：当前 ${S.maskRanges.length} 段手输/框选掩膜随请求携带。</div>`;
  } else {
    bodyHtml = `
    <div class="mt-1">轮廓（U-26）：${PROFILES.map(k =>
      `<label class="me-2"><input type="radio" name="spLp" class="sp-lprof" value="${k}"
        ${p.profile === k ? 'checked' : ''} ${computing ? 'disabled' : ''}>${k}</label>`).join('')}
      <label class="me-2 text-secondary" title="Q-21/F-39/U-26：全库无仪器分辨率 R（r_source='none'，前置 M-4：导入 FITS 携带 R/误差列）⇒ voigt 禁用（后端 E-14 voigt_disabled_no_r，T-22）；宽度只能作观测宽度报告（TXT-8/CA-08）">
        <input type="radio" disabled>voigt（禁用：无 R，前置 M-4）</label></div>
    <div class="mt-1">R（U-27，留空 = 无仪器分辨率）：<input type="number" step="any" min="0"
      class="form-control form-control-sm d-inline-block w-auto sp-lR" value="${escAttr(p.R)}"
      ${computing ? 'disabled' : ''}>
      <span class="text-secondary small" title="TXT-8：${escAttr(TXT8)}">空值 ⇒ CA-08 + TXT-8（观测宽度）</span></div>
    <div class="mt-1">line_frame（F-38/M-6，逐条核对后填）：
      <select class="form-select form-select-sm w-auto d-inline sp-lframe" ${computing ? 'disabled' : ''}>
        ${[['', 'null（未核对——vel_* 禁用）'], ['air', 'air（空气）'], ['vacuum', 'vacuum（真空）']]
          .map(([v, t]) => `<option value="${v}" ${(p.line_frame || '') === v ? 'selected' : ''}>${t}</option>`).join('')}
      </select> <span class="text-secondary small" title="${escAttr(M6_TIP)}">⚠ M-6 未复核</span></div>
    <div class="mt-1">z 覆写（Q-12，留空 = 谱记录/默认 0）：<input type="number" step="any" min="0"
      class="form-control form-control-sm d-inline-block w-auto sp-lz" value="${escAttr(p.z)}" ${computing ? 'disabled' : ''}></div>
    <div class="mt-1">n_boot（Q-15，≤${C_MAX_BOOT}）：<input type="number" min="1" max="${C_MAX_BOOT}" step="1"
      class="form-control form-control-sm d-inline-block w-auto sp-lnb" value="${escAttr(p.n_boot)}" ${computing ? 'disabled' : ''}>
      err_seed（Q-35，留空 = spec_hash 派生）：<input type="number" step="1"
      class="form-control form-control-sm d-inline-block w-auto sp-lseed" value="${escAttr(p.err_seed)}" ${computing ? 'disabled' : ''}></div>
    <div class="mt-1 text-secondary" title="${escAttr(M6_TIP + ' W-25：任何 vel_* 强提交返回 E-14。')}">
      <label><input type="checkbox" class="form-check-input sp-lvel" disabled> velocity_output（禁用）</label>
      —— M-6 线表帧未复核 ⇒ vel_shift/vel_fwhm/z_fit 恒 null（W-25①② 两道独立的门，E-14）</div>
    <div class="mt-1" title="${escAttr(U48_TIP)}">吸收系统 / 阻尼翼（U-48，只随 API-4 提交，F-105③）：
      <label class="me-2"><input type="checkbox" class="form-check-input sp-u48" data-k="absorber_ident"
        ${p.absorber_ident ? 'checked' : ''} ${computing ? 'disabled' : ''}>absorber_ident（F-98）</label>
      <label class="me-2" title="${p.absorber_ident ? (nOr(p.R) == null ? 'F-69：R 不可得（r_source=\'none\'）⇒ wing_logn 禁用——阻尼翼的形状需要仪器宽度' : 'F-99/F-100：红侧翼的 Voigt 剖面拟合（z/b 钉住）+ 双连续谱族对照') : 'W-35：未勾 absorber_ident 时禁用（依赖摆出来，不静默连带开启）'}">
        <input type="checkbox" class="form-check-input sp-u48" data-k="wing_logn"
        ${p.wing_logn ? 'checked' : ''} ${(!p.absorber_ident || nOr(p.R) == null || computing) ? 'disabled' : ''}>wing_logn（F-99/F-100${nOr(p.R) == null ? '，禁用：无 R（F-69）' : ''}）</label>
      <label class="me-2" title="${p.absorber_ident ? 'F-104：饱和/未分辨方向判定（sat_flag 只由被链接线行 depth 导出）' : 'W-35：未勾 absorber_ident 时禁用'}">
        <input type="checkbox" class="form-check-input sp-u48" data-k="metal_sat_check"
        ${p.metal_sat_check ? 'checked' : ''} ${(!p.absorber_ident || computing) ? 'disabled' : ''}>metal_sat_check（F-104）</label>
      <span class="text-secondary small">前置 M-4+M-6 缺 ⇒ 输出降级（null + 书面原因）；TXT-25 恒显于结果卡。</span></div>
    <div class="mt-1 small text-secondary">TXT-8：${esc(TXT8)}</div>`;
  }
  el.innerHTML = `<div class="card mb-3">
    <div class="card-header d-flex align-items-center py-2">③ 参数（S3）
      <span class="ms-2 fw-normal">${stepBar}</span>${staleBadge}
      <span class="ms-auto small text-secondary fw-normal">S3 谱线测量（API-4，P3 切片 2）</span></div>
    <div class="card-body py-2 small">
    ${bodyHtml}
    <hr class="my-2"><strong>⑤ 动作</strong>
    <div class="d-flex flex-wrap gap-2 align-items-center mt-1">
      <button class="btn btn-outline-secondary btn-sm sp-lprev" ${step <= 1 || computing ? 'disabled' : ''}>上一步</button>
      <button class="btn btn-outline-secondary btn-sm sp-lnext" ${step >= 3 || computing ? 'disabled' : ''}
        ${step === 1 && !canNext1 ? 'title="E-09：line_kind 未确认（U-30 必选）；线心必填" data-gate="1" disabled' : ''}>下一步</button>
      <button class="btn btn-primary btn-sm sp-lcompute" ${missing.length || computing || busyLeft ? 'disabled' : ''}
        title="步 3 的计算核（API-4）；步 1/2 也可直接算——缺项清单照常把门">计算</button>
      <button class="btn btn-outline-secondary btn-sm sp-lexpcsv" ${!S.lastLines || computing ? 'disabled' : ''}
        title="§4.3 谱线导出块（TXT-11+TXT-23+双哈希头，F-44）">导出 CSV（线表）</button>
      <button class="btn btn-outline-secondary btn-sm sp-lexpabs" ${!S.lastLines || computing ? 'disabled' : ''}
        title="§4.3 第四块：吸收系统表导出（列集恒定；U-48 关 ⇒ 表头照写、无数据行，F-105①④/T-72②）">导出 CSV（吸收系统）</button>
      <button class="btn btn-outline-secondary btn-sm sp-lexpjson" ${!S.lastLines || computing ? 'disabled' : ''}
        title="U-17/F-44：API-4 响应原文">导出 JSON</button>
    </div>
    <div class="mt-1 text-warning small">${esc(S.busyNotice || '')}</div>
    <div class="mt-1 text-warning small">${esc(S.featureNotice || '')}</div>
    <div class="mt-1 text-danger small" role="alert">${esc(S.errorMsg || '')}</div>
    <div class="mt-1 ${missing.length ? 'text-warning' : 'text-success'}">缺项清单（IA-16）：${missing.length
      ? '<ul class="mb-0">' + missing.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>' : '无，可以计算'}</div>
  </div></div>`;
  // 事件（改动 ⇒ 标需重算：步 1 的线区/线型决定步 2/3 的口径，U-23）
  const markStale = () => { S.lineStale = true; ctx.dirty(); };
  el.querySelectorAll('.sp-lkind').forEach(r => r.addEventListener('change', () => {
    p.line_kind = r.value; markStale(); renderLinesPanel(el, ctx);   // W-23 同步切换
  }));
  el.querySelectorAll('.sp-lsky').forEach(r => r.addEventListener('change', () => { p.sky_handling = r.value; markStale(); }));
  // P1-B：步 1 专属控件（.sp-lng/.sp-lspecies/.sp-llam/.sp-lhw）在步 2/3 不存在，
  // 须判空后再挂监听——否则 TypeError 中断其后全部 wiring（上一步/下一步/计算/
  // 导出与 U-48 之外的监听都挂不上去）。
  const lng = el.querySelector('.sp-lng');
  if (lng) lng.addEventListener('change', e => {
    const g = SPEC_LINE_GROUPS.find(x => x.key === e.target.value);
    if (g && g.lines && g.lines.length) {
      p.id_table = 'spec_lines.js#' + g.key; p.species = g.name;
      p.lambda_rest_aa = String(g.lines[Math.floor(g.lines.length / 2)]);   // 组内中位线心作候选
    } else { p.id_table = null; }
    markStale(); renderLinesPanel(el, ctx);
  });
  const lspecies = el.querySelector('.sp-lspecies');
  if (lspecies) lspecies.addEventListener('change', e => { p.species = e.target.value; markStale(); });
  const llam = el.querySelector('.sp-llam');
  if (llam) llam.addEventListener('change', e => { p.lambda_rest_aa = e.target.value; markStale(); });
  const lhw = el.querySelector('.sp-lhw');
  if (lhw) lhw.addEventListener('change', e => { p.halfwidth = e.target.value; markStale(); });
  const lorder = el.querySelector('.sp-lorder');
  if (lorder) lorder.addEventListener('change', e => {
    const v = Number(e.target.value);
    if (Number.isInteger(v) && v >= 0 && v <= C_POLY_ORDER_MAX) { p.baseline_order = v; markStale(); }
  });
  el.querySelectorAll('.sp-lprof').forEach(r => r.addEventListener('change', () => { p.profile = r.value; markStale(); }));
  // U-48 三开关联动（W-35）：改 absorber_ident 时重渲染以切换另两项的禁用态；
  // 未勾 ident 时另两项不携带进请求（不静默连带开启）
  el.querySelectorAll('.sp-u48').forEach(cb => cb.addEventListener('change', () => {
    p[cb.dataset.k] = cb.checked; markStale(); renderLinesPanel(el, ctx);
  }));
  const lR = el.querySelector('.sp-lR');
  if (lR) lR.addEventListener('change', e => { p.R = e.target.value; markStale(); });
  const lframe = el.querySelector('.sp-lframe');
  if (lframe) lframe.addEventListener('change', e => { p.line_frame = e.target.value; markStale(); });
  const lz = el.querySelector('.sp-lz');
  if (lz) lz.addEventListener('change', e => { p.z = e.target.value; markStale(); });
  const lnb = el.querySelector('.sp-lnb');
  if (lnb) lnb.addEventListener('change', e => { p.n_boot = e.target.value; markStale(); });
  const lseed = el.querySelector('.sp-lseed');
  if (lseed) lseed.addEventListener('change', e => { p.err_seed = e.target.value; markStale(); });
  // 动作条点击用**事件委托**挂在容器上（容器不被 innerHTML 重建替换）：
  // 修复「输入 blur → change → renderLinesPanel 重建落在 mousedown 与 mouseup
  // 之间 ⇒ 该次 click 被吞、要点的按钮已是新元素」的时序缺陷。
  if (!el.dataset.spLinesDelegated) {
    el.dataset.spLinesDelegated = '1';
    el.addEventListener('click', e => {
      const t = e.target.closest('.sp-lprev,.sp-lnext,.sp-lcompute,.sp-lexpcsv,.sp-lexpjson,.sp-lexpabs');
      if (!t || t.disabled) return;
      const kindOk = !!(S.lineParams && S.lineParams.line_kind);
      const hasCenter = Number(S.lineParams && S.lineParams.lambda_rest_aa) > 0;
      const st = S.lineStep || 1;
      if (t.classList.contains('sp-lprev')) { S.lineStep = Math.max(1, st - 1); renderLinesPanel(el, ctx); }
      else if (t.classList.contains('sp-lnext')) {
        if (st === 1 && !(kindOk && hasCenter)) return;      // E-09/W-23 闸
        S.lineStep = Math.min(3, st + 1); renderLinesPanel(el, ctx);
      }
      else if (t.classList.contains('sp-lcompute')) computeLines(ctx);
      else if (t.classList.contains('sp-lexpcsv')) exportLinesCsv(S.lastLines);
      else if (t.classList.contains('sp-lexpabs')) exportAbsorberCsv(S.lastLines);
      else if (t.classList.contains('sp-lexpjson')) {
        const keep = S.lastResponse;              // 与 S2 JSON 导出同一换出手法
        S.lastResponse = S.lastLines;
        try { ctx.exportJson(S); } finally { S.lastResponse = keep; }
      }
    });
  }
}

// ─── S3 结果卡（§4.2 LineResult；F-94⑤ 值键与误差键并排 + F-67 互斥空值） ────
export function renderLinesResults(el, ctx) {
  const r = ctx.S.lastLines;
  if (!r) {
    el.innerHTML = '<div class="card mb-3"><div class="card-body py-2">'
      + '<strong>S3 谱线测量</strong><div class="small text-secondary mt-1">'
      + '尚未计算：右侧三步向导——选线区 → 定基线 → 拟合轮廓（U-23）。</div></div></div>';
    return;
  }
  const row = (r.lines || [])[0] || {};
  const isEm = row.line_kind === 'emission';
  const warn = (r.warnings || []).map(w => `[${esc(w.code)}${w.reason ? '·' + esc(w.reason) : ''}] ${esc(w.message)}`).join('；');
  const excl = (row.excluded_segments || []).map(s =>
    `<li>${num(s.lo, 2)}–${num(s.hi, 2)} Å：${esc(s.reason)}</li>`).join('');
  const g = r.frame_gates || {};
  const es = Object.entries(row.err_source || {})
    .map(([k, v]) => `${esc(k)}=${esc(v)}`).join('; ');
  const ewPair = (v, e, d = 3) => `${num(v, d)} ± ${num(e, d)}`;
  el.innerHTML = `<div class="card mb-3"><div class="card-body py-2">
    <div class="d-flex align-items-center"><strong>S3 谱线测量</strong>
      <span class="small text-secondary ms-2">${esc(row.species || '')} · line_kind=${esc(row.line_kind)} ·
      profile=${esc(row.model)} · z_eff=${num(r.z_eff, 4)} · r_source=${esc(row.r_source)} ·
      λ_center_obs=${num(r.lambda_center_obs_aa, 2)} Å ± ${num(r.half_width_aa, 1)}</span>
      <span class="ms-auto badge ${row.detected ? 'bg-success' : 'bg-secondary'}">${row.detected ? '检出' : '未检出'}</span></div>
    <div class="small mt-1">TXT-15：${esc(TXT15[row.line_kind] || '')}
      <span class="text-secondary">F-67：${isEm ? 'depth 恒 null（发射线）' : 'line_flux 恒 null（吸收线）'}</span></div>
    <div class="table-responsive mt-1"><table class="table table-sm mb-0 small sp-result-table"><tbody>
      <tr><td>λ_obs</td><td>${ewPair(row.lambda_obs_vac_aa, row.lambda_err_aa, 3)} Å
        <span class="text-secondary">[err_source.lambda_obs_vac_aa=${esc((row.err_source || {}).lambda_obs_vac_aa || 'none')}]</span></td></tr>
      <tr><td>EW</td><td>signed ${ewPair(row.ew_signed_aa, row.ew_err_aa)} Å（TXT-15 符号约定）·
        |W| ${num(row.ew_obs_aa, 3)} Å · rest ${ewPair(row.ew_rest_aa, row.ew_rest_err_aa)} Å
        （ew_err_form=${esc(row.ew_err_form)}：photon ${num((row.ew_err_terms || {}).photon)} /
        continuum ${num((row.ew_err_terms || {}).continuum)} /
        coherent ${num((row.ew_err_terms || {}).continuum_coherent)}）</td></tr>
      ${isEm
        ? `<tr><td>line_flux</td><td>${ewPair(row.line_flux, row.line_flux_err)} erg/s/cm²
            <span class="text-secondary">[err_source=${esc((row.err_source || {}).line_flux || 'none')}]</span></td></tr>`
        : `<tr><td>depth</td><td>${num(row.depth, 4)}（+${num(row.depth_err_hi, 4)}/−${num(row.depth_err_lo, 4)}；
            ${esc((row.err_source || {}).depth || 'none')}——不对称分位不得当 1σ 读，F-95②）</td></tr>`}
      <tr><td>FWHM_obs</td><td>${ewPair(row.fwhm_obs_aa, row.fwhm_obs_err_aa, 3)} Å
        <span class="text-secondary">TXT-8：${esc(row.width_note || TXT8)}</span> ·
        fwhm_intr/vel_fwhm/z_fit = ${esc(String(row.fwhm_intr_aa))}/${esc(String(row.vel_fwhm_kms))}/${esc(String(row.z_fit))}
        <span title="${escAttr(row.velocity_family_note || 'M-6 留位')}">（M-6 留位）</span></td></tr>
      <tr><td>snr_res</td><td>${num(row.snr_res, 2)}（snr_def=${esc(row.snr_def)}，F-68 每分辨率元口径） ·
        n_win_pix=${esc(String(row.n_win_pix))} · χ²/dof=${num(row.chi2, 2)}/${esc(String(row.dof))} ·
        cov_method=${esc(String(row.cov_method))} · rho_used=${num(row.rho_used, 3)}</td></tr>
      ${row.detected ? '' : `<tr><td>上限</td><td>W &lt; ${num(row.upper_limit_3sigma, 3)} Å（3σ，F-68/CA-44④）——
        ${esc(row.null_reason || '')}</td></tr>`}
      <tr><td>基线</td><td>type=${esc(row.baseline_type || 'poly')}（${esc((row.baseline || {}).poly_basis || '')}）·
        order=${esc(String((row.baseline || {}).order))} · cond_2=${num((row.baseline || {}).cond_2, 3)} ·
        n_nodes=${esc(String((row.baseline || {}).n_nodes))}（F-36 侧带）</td></tr>
      <tr><td>线比/柱密度</td><td>ratio=${esc(String(row.ratio))} · ratio_ref=${esc(String(row.ratio_ref))} ·
        column_density=${esc(String(row.column_density))}（单行请求 + M-6 振子强度库未备 ⇒ null + 书面原因，见 notes）</td></tr>
    </tbody></table></div>
    <div class="small text-secondary mt-1">err_source（TXT-23 逐键口径）：${es || '—'}</div>
    <div class="small ${excl ? 'text-warning' : 'text-secondary'} mt-1">掩膜（F-72①②，两类措辞不混用）：${excl
      ? '<ul class="mb-0">' + excl + '</ul>' : '线窗内无掩膜段命中'}</div>
    ${row.sky_subtract ? `<div class="small ${row.sky_subtract.reverted ? 'text-warning' : 'text-secondary'} mt-1">天光处理（F-86/U-31）：
      sky_subtracted=${esc(String(row.sky_subtracted))} · n_sky_lines=${esc(String(row.sky_subtract.n_sky_lines))} ·
      RMS 比=${num(row.sky_subtract.rms_ratio, 3)}（C_SKY_RESID_FLOOR=0.7）——${esc(row.sky_subtract.note || '')}</div>` : ''}
    <div class="small text-secondary mt-1">W-25①（线表侧）：line_frame=${esc(String((g.w25_1_line_side || {}).line_frame))} ·
      verified=${esc(String((g.w25_1_line_side || {}).verified))}——未经 M-6 复核 ⇒ vel_* 只作禁用、强提交返回
      E-14（line_frame_unverified）。</div>
    <div class="small text-secondary mt-1">W-25②（谱级）：lambda_frame=${esc(String((g.w25_2_spectrum_side || {}).lambda_frame))} ·
      blocked=${esc(String((g.w25_2_spectrum_side || {}).blocked))}——unknown ⇒ vel_*/z_fit 禁用
      （lambda_frame_unknown）。两道独立的门，文案不合并。</div>
    <div class="small text-secondary mt-1">diagnostics.cross_link_gate（F-103）：${esc(JSON.stringify((r.diagnostics || {}).cross_link_gate || {}))}</div>
    ${absorberCardHtml(ctx)}
    ${warn ? `<div class="small text-warning mt-1">告警：${warn}</div>` : ''}
  </div></div>`;
}

// ─── 吸收系统卡（U-48，W-35：两族 logN + 四个区间端点 + spread + b/z source +
// class + class_thresholds_dex 同屏；TXT-24/25 常驻；任何位置不出现中性氢分数与森林统计量列，F-105②） ──
function absorberCardHtml(ctx) {
  const S = ctx.S, r = S.lastLines || {};
  const dg = r.diagnostics || {};
  const systems = dg.absorber_systems || [];
  const u48on = !!(S.lineParams.absorber_ident || S.lineParams.wing_logn
    || S.lineParams.metal_sat_check);
  const cards = systems.map(sy => {
    const spreadBad = sy.wing_spread_dex != null && sy.wing_spread_dex > 0.15;
    const sw = (sy.warnings || []).map(w =>
      `[${esc(w.code)}] ${esc(w.message)}`).join('；');
    return `<div class="mt-1 border rounded p-1">
      <b>吸收系统 ${esc(String(sy.system_id))}</b>
      <span class="badge ${spreadBad ? 'bg-warning text-dark' : 'bg-secondary'}"
        title="${escAttr(TXT24 + (spreadBad ? ' ' + TXT24_CA45 : ''))}">wing_spread_dex=${num(sy.wing_spread_dex, 3)}</span>
      <div class="small">z_abs=${num(sy.z_abs, 5)} ± ${num(sy.z_err, 5)}（z_source=${esc(String(sy.z_source))}）·
        class=${esc(String(sy.class))}（thresholds_dex=${esc(JSON.stringify(sy.class_thresholds_dex || {}))}）·
        ${esc(String(sy.intervening_or_host))} · b=${num(sy.wing_b_assumption_kms, 1)} km/s（b_source=${esc(String(sy.b_source))}）·
        wing_snr_res=${num(sy.wing_snr_res, 1)} · wing_n_pixels=${esc(String(sy.wing_n_pixels))} ·
        sat_flag=${esc(String(sy.sat_flag))} · masked=${esc(JSON.stringify(sy.masked_ranges || []))} ·
        blue_side_igm_masked=${esc(String(sy.blue_side_igm_masked))}</div>
      <div class="small">族A（${esc(String(sy.cont_family_a))}）logN=${num(sy.wing_logn, 3)}
        [+${num(sy.wing_logn_err_hi, 3)}/−${num(sy.wing_logn_err_lo, 3)}] ·
        族B（${esc(String(sy.cont_family_b))}）logN_alt=${num(sy.wing_logn_alt, 3)}
        [+${num(sy.wing_logn_alt_err_hi, 3)}/−${num(sy.wing_logn_alt_err_lo, 3)}]——TXT-24：${esc(spreadBad ? TXT24_CA45 : TXT24)}</div>
      ${(sy.notes || []).length ? `<div class="small text-secondary">notes：${(sy.notes || []).map(n => esc(n)).join('；')}</div>` : ''}
      ${sw ? `<div class="small text-warning">系统告警（CA-45/CA-46 不折叠，导出件同一行可见）：${sw}</div>` : ''}
    </div>`;
  }).join('');
  return `${u48on ? `<div class="small text-secondary mt-1">TXT-25：${esc(TXT25)}
      （forest_stats=${esc(String((r.diagnostics || {}).forest_stats))}，forest_stats_reasons=${esc(JSON.stringify(dg.forest_stats_reasons || []))}）</div>` : ''}
    ${cards ? `<div class="mt-1"><b>吸收系统卡（U-48/F-105④）</b>${cards}</div>` : ''}`;
}

// ─── S3 计算（API-4；状态机与 S1/S2 同一套 IA 状态） ─────────────────────────
export async function computeLines(ctx) {
  const S = ctx.S;
  if (linesMissing(ctx).length || S.state === 'computing') return;
  if (S.abortCtrl) S.abortCtrl.abort();
  S.abortCtrl = new AbortController();
  S.state = 'computing'; S.busyNotice = S.errorMsg = S.featureNotice = null;
  ctx.refreshChrome();
  try {
    const resp = await ctx.spPost('/specphot/line', buildLinesRequest(ctx), S.abortCtrl.signal);
    S.lastLines = resp;
    S.lineStale = false;
    S.state = 'fresh';
  } catch (e) {
    if (e.name === 'AbortError') { S.state = 'dirty'; S.busyNotice = '已取消'; }
    else if (e.status === 429) {
      const secs = Math.max(1, Number((e.payload || {}).retry_after_s) || 2);
      S.busyUntilTs = Date.now() + secs * 1000; S.busyNotice = null; S.state = 'dirty';
      ctx.startBusyCountdown && ctx.startBusyCountdown();
    } else if (e.code === 'feature_disabled') {
      S.featureNotice = e.message; S.state = 'dirty';
    } else { S.errorMsg = `${e.message}${e.reason ? '（' + e.reason + '）' : ''}`; S.state = 'error'; }
  } finally {
    S.abortCtrl = null;
    ctx.refreshChrome();
  }
}
