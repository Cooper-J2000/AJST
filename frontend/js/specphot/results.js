// === specphot 结果区（IA-6…IA-8、§2.7 呈现规则、§4.2 输出对象） ===
// 职责：① API-2 请求体构造（以 backend/specphot/photometry.py 校验代码 + §5.2.1 为真源）；
// ② 结果表（核心键 + 逐行展开卡，λ 四兄弟 / err_source·err_scope 成对展示）；
// ③ 告警区（IA-8 按类别固定优先级、code 分色）；④ 对照面板（IA-12，同次响应数据，不发请求）。
import { esc, escAttr } from '../utils.js';
import { manualAnchorRows, checkedAnchorBands } from './anchors.js';
import { renderComparePlot } from './specplot.js';

const C_OVERLAP_MIN = 0.95;     // 与后端 constants.py 同值
const C_MAX_EXCLUDE = 32;       // 与后端 constants.py 同值（Q-2 / F-107⑤）
const TXT = {
  t2: '当前曲线为滤光片透过率，未含探测器 QE、镜面反射、大气消光中随波长变化的因子；绝对星等与颜色都带有与源色相关的系统偏差（量级与其成立前提以 F-17 为唯一载体，本行不复写数值）。', // TXT-2
  t3: '误差为代理量（无误差列时由二阶差分估计，已乘一致性因子），只含随机不确定度，不含定标与波长定标系统误差。', // TXT-3
  t4: '合成 vs 锚点（预览，未入库）· 空心三角 = 合成点，实心圆 = 锚点；手加锚点用方框标出，并注明其星等系与时刻由用户自行填报。', // TXT-4
  t5n: '已用 N 个波段的实测点求单一缩放因子 κ*；缩放方向（乘在光谱上）、σ_κ、波段协方差是否启用与所用实测值同时列出。', // TXT-5
  t9: '谱与测光时刻相差 X 天，暂现源在此期间可能已显著变化，二者不宜直接互推。', // TXT-9
  t10: '该波段无透过率曲线，结果按中心波长处单色近似，仅供量级参考。', // TXT-10
  t12: '该谱无可用绝对流量口径，只能走 anchored / model 定标。', // TXT-12
  t23: '每一列误差的求法由 err_source[k] 标明，口径范围由 err_scope[k] 标明只含随机项（stat）还是含定标传播（stat+cal）。标 none 者其误差键是空值并附书面原因，不是 0。', // TXT-23
};
// IA-8 分档（各档完整 ID 清单以 §5.4 分档行为准；此处按已上线 code 归档）
const GROUPS = [
  ['input', '输入口径', 'danger'], ['anchor', '锚点集', 'danger'], ['approx', '近似', 'warning'],
  ['cover', '覆盖·网格·数值条件', 'warning'], ['err', '误差', 'warning'],
  ['readq', '读侧数据质量', 'secondary'], ['time', '时刻', 'info'],
  ['z', '红移与线位自洽', 'info'], ['interp', '结果解释', 'secondary'], ['other', '其他', 'secondary'],
];
function groupOf(w) {
  const m = { 'CA-01': 'input', 'CA-23': 'input', 'CA-30': 'input', 'CA-34': 'input',
    'CA-14': 'anchor', 'CA-33': 'anchor', 'CA-02': 'approx', 'CA-04': 'approx',
    'CA-03': 'cover', 'CA-05': 'cover', 'CA-19': 'cover', 'CA-06': 'err',
    // 分档以 §5.4 末权威行为准：CA-15∈近似、CA-20∈覆盖·网格·数值条件、CA-44∈误差；
    // P2 2b 新发码：CA-11/CA-12/CA-43∈结果解释（CA-24 同列于近似与覆盖两族，按
    // 「首列族优先」先例归近似，与既有 CA-23→输入口径 同一裁量）
    'CA-15': 'approx', 'CA-21': 'err', 'CA-47': 'err', 'CA-10': 'readq',
    'CA-44': 'err', 'CA-20': 'cover', 'CA-24': 'approx',
    'CA-11': 'interp', 'CA-12': 'interp', 'CA-43': 'interp',
    // P2 2c 新发码（§5.4 末分档行）：CA-49∈近似（CA-04/15/24/39/45/49）、
    // CA-48∈覆盖·网格·数值条件（CA-05/20/23/24/46/48）
    'CA-49': 'approx', 'CA-48': 'cover',
    // P2b 评审新发码（§5.4 末分档行）：CA-38∈结果解释（CA-08/11/12/14/37/38/40/41/43）
    'CA-38': 'interp',
    // P2c 新发码（§5.4 末分档行）：CA-39∈近似（与 CA-04/15/24/49 同族——σ_resp
    // 换口径属近似声明）、CA-40∈结果解释（与 CA-38 同族——单一 β 假设的结论句）
    'CA-39': 'approx', 'CA-40': 'interp',
    // P3 切片 1 新发码：CA-27∈误差（§5.4 末分档行「误差（CA-06/21/22/27/44/47）」——
    // 生长曲线饱和 ⇒ column_density 降为下限、σ 键置 null，属误差族而非近似族）
    'CA-27': 'err',
    // P3c 新发码（§5.4 末分档行）：CA-36∈红移与线位自洽（独立一档，IA-8 序在
    // 时刻之后、结果解释之前）、CA-37∈结果解释（CA-08/11/12/14/37/38/40/41/43
    // 族——天光扣除未起效已退回 mask，属「怎么读这条结果」的结论句）
    'CA-36': 'z', 'CA-37': 'interp',
    // P3d 新发码（§5.4 末分档行）：CA-45∈近似（CA-04/15/24/39/45/49——两族
    // log N 之差超阈 ⇒ 连续谱安置系统项主导，与 CA-04 同族）、CA-46∈覆盖·网格·
    // 数值条件（CA-05/20/23/24/46/48——S1 波段量与 S3 翼量不同基，不得并排比对）
    'CA-45': 'approx', 'CA-46': 'cover' };
  if (w.code === 'CA-07') return (w.reason === 'band_shift_only') ? 'z' : 'time';
  return m[w.code] || 'other';
}
function num(v, d = 3) {
  return (v == null || !isFinite(Number(v))) ? '—' : Number(v).toFixed(d);
}

// ─── API-2 请求体（Q-23/§5.2.1：spectrum_id 与 spectrum 二选一；键名对齐 photometry.py） ───
export function buildPhotometryRequest(ctx) {
  const S = ctx.S;
  const nOr = (v) => (v === '' || v == null || !isFinite(Number(v))) ? null : Number(v);
  // Q-5：mono 必须显式授权；且只有选中了无曲线波段才真正走 mono——
  // 纯曲线波段保持 integrated（CA-04 只标在确实走单色近似的行）
  const _bm = id => S.bandsMeta.find(b => b.id === id) || {};
  const anyNoCurve = S.selectedBands.some(id => !_bm(id).has_curve);
  const useMono = S.params.allow_mono === true && anyNoCurve;
  const body = {
    bands: [...S.selectedBands],
    weighting: S.params.weighting,                 // photon|energy
    band_mode: useMono ? 'mono' : 'integrated',
    allow_mono: useMono,                           // Q-5：mono 必须显式授权
    mag_system: S.params.mag_system,               // AB|Vega（ST 服务端 400，前端已禁用）
    mode: S.params.mode,                           // auto|direct|anchored|model（P2 2b 起 model 解禁，需 use_model）
    err_policy: S.params.err_policy,               // auto|force_proxy
    mask: S.maskRanges.map(r => [Number(r[0]), Number(r[1])]),
    mw: { correct: S.params.mw_correct === true,
          ebv: nOr(S.params.ebv_override) },       // 手填覆盖 ⇒ ebv_source='user'（TXT-19）
    redshift: nOr(S.metaForm.z),                   // null = 用谱记录/默认 z（Q-12）
    dt_tol_d: nOr(S.params.dt_tol_d),              // null = 按类别表（F-61）
    // U-44（P2c 解禁前三项）：只携带勾选的开关（Q-27 只收布尔；全关 = {} 与
    // 基线请求逐字节同形，T-48①）。后三项前端仍禁用（P3c，E-15/A-3）。
    diagnostics: Object.fromEntries(
      Object.entries(S.diagFlags || {}).filter(([, v]) => v === true)),
  };
  // P1b/P2（§5.2 Q-33 / U-53）：preprocess 仅在非默认时携带（缺省 = 服务端恒等态）。
  // U-50 手输段写 S.maskRanges（与 U-13 同一个布尔数组 ⇒ body.mask 通道，服务端
  // 归并进同一用户类；preprocess.ranges 通道保留给 API 直调，服务端同判）。
  // errcol_choice 仅在 U-53 显式确认后携带（auto = 服务端回落缺省）。
  // P2 2c（U-49/U-51）：factor>1 走合束算术（F-108，改动 ⇒ stale）；smooth 为
  // 绘图平滑核长（F-109，纯前端绘制——随请求携带只为 Q-33 校验与口径同源，
  // 服务端不消费：开/关平滑响应逐字节同，T-77②③）。
  const pp = {};
  if (S.params.errcol_choice) pp.errcol_choice = S.params.errcol_choice;
  if (Number(S.params.factor) > 1) pp.factor = Number(S.params.factor);
  if (S.params.smooth_on) {
    const px = Number(S.params.smooth_px);
    pp.smooth = (isFinite(px) && px >= 1 && px <= 7 && Number.isInteger(px)) ? px : 3;
  }
  body.preprocess = pp;
  const manual = manualAnchorRows(S);
  if (manual.length) body.anchor_rows = manual;
  const ab = checkedAnchorBands(S);
  if (ab != null) body.anchor_bands = ab;          // U-40 勾选集（null = 服务端按全行）
  // U-22（§3.3 model 行，P2 2b）：mode='model' ⇒ 携带 use_model（Q-9 键集：
  // mask_hash/preprocess_hash/frame/flux_transform_applied/comparable + 模型与参数），
  // 来源 = S2 页签最近一次可比拟合（verdict.best 优先，F-91⑦：CA-44 者不可消费）。
  if (S.params.mode === 'model' && S.lastContinuum) {
    const comp = S.lastContinuum.fits.filter(f => f.comparable);
    const fit = comp.find(f => f.model === S.lastContinuum.verdict.best) || comp[0];
    if (fit) {
      body.use_model = {
        mask_hash: fit.mask_hash,                  // F-29①：与本次掩膜一致才可比
        preprocess_hash: (S.lastContinuum.preprocess || {}).preprocess_hash ?? null,  // Q-34
        frame: 'obs', flux_transform_applied: 'none',   // P1 恒观测系（Q-10）
        comparable: fit.comparable === true,
        model: fit.model, params: fit.params,
        nu0: fit.nu0, law: S.lastContinuum.law, rv: S.lastContinuum.rv,
        z: S.lastContinuum.z_eff,
      };
    }
  }
  if (S.sourceKind === 'catalog' && S.spectrumId != null) {
    body.spectrum_id = S.spectrumId;               // Q-23：与 spectrum 互斥
  } else if (S.upload) {
    const m = S.metaForm;
    const meta = { lambda_frame: S.uploadFrame || 'vacuum', category: m.category || 'other' };
    if (String(m.ra || '').trim()) meta.ra_deg = String(m.ra).trim();       // Q-24：字符串可（HMS/十进制）
    if (String(m.dec || '').trim()) meta.dec_deg = String(m.dec).trim();
    if (nOr(m.z) != null) meta.z = nOr(m.z);
    if (nOr(m.mjd) != null) meta.mjd = nOr(m.mjd);
    if (S.upload.flux_unit) meta.flux_unit = S.upload.flux_unit;
    body.spectrum = { lam_aa: S.upload.lam_aa, flux: S.upload.flux,
                      flux_err: S.upload.flux_err || null, meta };   // A-7：上传件随体携带
  }
  return body;
}

// ─── W-8/IA-8 告警去重聚合：同一 (code, reason) 命中多行/多层 ⇒ 只出现一次并列出波段列表。
// 聚合 results[].warnings + 顶层 warnings；message 取首条，非 CA-05 追加「涉及波段：a/b/c」
// （CA-05 row_rejected_* 的 message 已含单波段名，避免重复罗列）；不同 reason 不合并。
function aggregateWarnings(resp) {
  const map = new Map();   // key = code|reason（reason 可为空串）
  const put = (w, band) => {
    const key = `${w.code || ''}|${w.reason || ''}`;
    if (!map.has(key)) {
      map.set(key, { code: w.code, reason: w.reason || '', message: w.message || '',
        bands: band ? [band] : [] });
    } else if (band && !map.get(key).bands.includes(band)) {
      map.get(key).bands.push(band);
    }
  };
  for (const w of (resp.warnings || [])) put(w, null);            // 顶层（谱级）告警
  for (const row of (resp.results || [])) {
    for (const w of (row.warnings || [])) put(w, row.band);
  }
  return [...map.values()].map(w => ({
    ...w,
    text: w.message
      + (!w.bands.length || w.code === 'CA-05' ? '' : `（涉及波段：${w.bands.join('/')}）`),
  }));
}

// ─── 告警区（U-20：图标 + 中文 + 定位到控件；>4 条折叠；aria-live 播报条数；W-8 聚合） ───
function renderWarnings(resp) {
  const ws = aggregateWarnings(resp);
  const rows = GROUPS.map(([key, label, color]) => {
    const items = ws.filter(w => groupOf(w) === key);
    if (!items.length) return '';
    return `<div class="small mb-1"><span class="badge bg-${color}">${esc(label)}</span>
      ${items.map(w => `<div class="ms-3">[${esc(w.code)}${w.reason ? '·' + esc(w.reason) : ''}] ${esc(w.text)}
        <a href="javascript:void(0)" class="ms-1 sp-locate small">定位到控件</a></div>`).join('')}</div>`;
  }).join('');
  const folded = ws.length > 4;
  return `<div class="alert alert-secondary py-2 mb-2" aria-live="polite">
    <strong class="small">告警（${ws.length} 条）</strong>
    <div class="${folded ? 'd-none sp-warnmore' : ''}">${rows || '<span class="small text-secondary">无</span>'}</div>
    ${folded ? '<a href="javascript:void(0)" class="small sp-warntoggle">展开全部</a>' : ''}
  </div>`;
}

// ─── 结果表（含逐行展开卡） ───
function resultRows(resp) {
  return resp.results.map((r, i) => {
    const proxy = ['second_diff', 'mad_window', 'mixed', 'second_diff_degraded'].includes(r.sigma_method);
    const dm = r.delta_m;
    const dmCls = dm == null ? '' : (Math.abs(dm) > 0.5 ? 'text-danger fw-bold'
      : Math.abs(dm) > 0.2 ? 'text-danger' : '');
    const ov = r.overlap;
    const ovCls = (ov != null && ov < C_OVERLAP_MIN) ? 'text-warning fw-bold' : '';
    const flags = [
      r.nonpos_frac != null && r.nonpos_frac > 0 ? '⚑nonpos' : '',
      r.gap_bridged ? '⚑gap' : '', r.n_dup_lam > 0 ? '⚑dupλ' : '',
    ].filter(Boolean).join(' ');
    const detail = `
      <div class="small text-secondary mt-1 sp-detail" data-i="${i}" style="display:none">
        <div>λ 四兄弟（F-18）：pivot=${num(r.lambda_pivot_aa, 1)} · eff=${num(r.lambda_eff_aa, 1)} ·
          phot=${num(r.lambda_phot_aa, 1)} · iso=${num(r.lambda_iso_aa, 1)} Å</div>
        <div>像素：used=${r.n_used_pixels} · masked=${r.n_masked_pixels} · nonpos=${r.n_nonpos}
          (frac=${num(r.nonpos_frac, 4)}) · gap_bridged=${r.gap_bridged} · n_dup_lam=${r.n_dup_lam}
          ${r.dup_lam_rel_spread != null ? `(相对散布 ${num(r.dup_lam_rel_spread, 4)})` : ''}</div>
        <div>曲线：${esc(r.curve_kind)}/${esc(r.curve_source)} · nodes=${r.curve_nodes ?? '—'} ·
          px/FWHM=${num(r.curve_px_per_fwhm, 1)} · interp=${esc(r.interp)} ·
          in_coverage=${r.in_coverage} · extrapolated=${r.extrapolated}${r.extrapolated ? '（TXT-6：覆盖范围外外推，不可靠）' : ''}</div>
        <div>时刻：dt_obs=${num(r.dt_obs_d, 3)} d / 容差 ${num(r.dt_tol_eff_d, 3)} d
          (time_precision_d=${num(r.time_precision_d, 3)}) · origin=${esc(r.m_obs_origin || '—')} ·
          kind=${esc(r.m_obs_kind || '—')}${r.m_obs_kind === 'interpolated'
            ? `<span class="text-warning">（TXT-9：${TXT.t9.replace('X 天', '见 dt_obs')}）</span>` : ''}</div>
        <div>消光：delta_a_band_mono=${num(r.delta_a_band_mono, 4)}（TXT-14：银河消光在观测系波长逐点改正、波段积分之前完成）</div>
        <div>误差（TXT-23 成对展示）：${Object.entries(r.err_source || {})
          .map(([k, v]) => `${k}=${esc(v)}/${esc((r.err_scope || {})[k] ?? '—')}`).join(' · ')}</div>
        <div>σ_stat 通路：sigma_method=${esc(r.sigma_method)} · rho_lag1=${num(r.rho_lag1, 3)} ·
          corr_infl=${num(r.corr_infl, 3)}${proxy ? ' <span class="badge bg-warning text-dark">代理（CA-06）</span>' : ''}
          ${r.corr_infl > 1 && r.mag_err_stat != null
            ? `（放大前 ${num(r.mag_err_stat / r.corr_infl, 4)} → 放大后 ${num(r.mag_err_stat, 4)}，F-55）` : ''}</div>
        <div>不在误差预算内（F-58）：${esc((r.not_in_budget || []).join('、'))}</div>
        ${(r.warnings || []).length ? `<div class="text-warning">行内告警：${r.warnings
          .map(w => `[${esc(w.code)}] ${esc(w.message)}`).join('；')}</div>` : ''}
      </div>`;
    return `<tr class="sp-row" data-i="${i}" style="cursor:pointer">
      <td><i class="bi bi-caret-right"></i> ${esc(r.band)}</td>
      <td>${num(r.mag)}${r.curve_kind === 'transmission' ? '⚠' : ''}</td>
      <td title="${escAttr(TXT.t3)}">${num(r.mag_err_stat, 4)}${proxy ? '<sup class="text-warning">代理</sup>' : ''}</td>
      <td title="${escAttr(TXT.t23)}">${num(r.mag_err_cal, 4)}</td>
      <td>${num(r.mag_err_resp, 4)}</td>
      <td>${num(r.f_mjy, 4)} ± ${num(r.f_err_mjy, 4)}</td>
      <td class="${ovCls}">${ov == null ? '—' : (ov * 100).toFixed(0) + '%'}</td>
      <td>${num(r.m_obs)}</td>
      <td class="${dmCls}">${num(dm)} ± ${num(r.delta_m_err, 4)}</td>
      <td class="small">${esc(r.curve_kind)}${r.band_mode === 'mono' ? ' <span class="text-warning" title="' + escAttr(TXT.t10) + '">mono</span>' : ''}</td>
      <td class="small text-secondary">${flags}</td>
    </tr>${detail}`;
  }).join('');
}

// ─── U-52（F-111②）：离群候选「剔除并记录」逐段确认（P1b） ──────────────────
// 候选段随 CA-10/outlier_flagged 告警的 segments 字段回显（服务端只标记不剔除，
// T-79③）；点击才写进 S.maskRanges（与 U-13/U-50 同一个布尔数组），mask_hash 随
// 之改变。候选占比超 C_CLIP_MAX_FRAC ⇒ 服务端只发 CA-47④（无 segments）⇒ 按钮不出现。
function renderOutlierConfirm(resp, ctx) {
  const S = ctx.S;
  const w = (resp.warnings || []).find(x => x.reason === 'outlier_flagged'
    && Array.isArray(x.segments) && x.segments.length);
  if (!w) return '';
  const rows = w.segments.map((seg, i) => `<li class="ms-3">${num(seg[0], 1)}–${num(seg[1], 1)} Å
      <button type="button" class="btn btn-outline-secondary btn-sm py-0 ms-1 sp-outlier-accept"
        data-i="${i}" title="写入用户掩膜（F-107① 用户数值段类），下次计算生效">剔除并记录</button></li>`).join('');
  return `<div class="small mt-1" title="F-111②：离群点的正确去处是掩膜而不是损失函数；服务端不自动剔除">
      <span class="badge bg-secondary">离群候选</span> ${esc(w.message)}
      <ul class="mb-0 sp-outlier-list">${rows}</ul></div>`;
}

// ─── U-44 诊断卡（P2c：diagnostics{} 开启项渲染；全关/空对象 ⇒ 整卡不出现，
// 与基线视图逐字节同形）。beta_matrix / anchor_reinsert 的 note 常驻
// causal_use='diagnostic_only' 声明（F-82/F-84）。 ────────────────────────────
function renderDiagnostics(resp) {
  const d = resp.diagnostics || {};
  if (!d || (!d.beta_matrix && !d.resp_perturb && !d.anchor_reinsert)) return '';
  const rows = [];
  if (d.beta_matrix) {
    const cells = d.beta_matrix.cells || [];
    rows.push(`<div class="small mt-1"><strong>β 矩阵（F-82，causal_use=${esc(d.beta_matrix.causal_use || 'diagnostic_only')}）</strong>
      ${cells.length ? `<div class="table-responsive"><table class="table table-sm mb-1 small">
        <thead><tr><th>波段对</th><th>β ± σ_β</th><th>color ± stat/cal</th><th>dt (d)</th></tr></thead>
        <tbody>${cells.map(c => `<tr><td>${esc(c.band_i)}–${esc(c.band_j)}</td>
          <td class="${c.sigma_beta != null && Math.abs(c.beta) > 3 * c.sigma_beta ? 'text-danger fw-bold' : ''}">${num(c.beta, 4)} ± ${num(c.sigma_beta, 4)}</td>
          <td>${num(c.color, 4)} ± ${num(c.color_err_stat, 4)}/${num(c.color_err_cal, 4)}</td>
          <td>${num(c.dt_d, 3)}</td></tr>`).join('')}</tbody></table></div>`
        : '<span class="text-secondary">无可成格的波段对（两支锚点都须在 dt_tol_eff 容差内）。</span>'}
      <div class="text-secondary">${esc(d.beta_matrix.note || '')}</div></div>`);
  }
  if (d.resp_perturb) {
    rows.push(`<div class="small mt-1"><strong>通带形状扰动（F-83）</strong>：resp_method=perturbation，
      扰动组数 perturb_n=${d.resp_perturb.perturb_n}（请求 ${d.resp_perturb.perturb_n_requested}${d.resp_perturb.downscaled ? '，预算触顶已降规模（Q-27/ST-9）' : ''}），
      幅度 C_SHAPE_PERT_EPS=${d.resp_perturb.eps}（工程假定，CA-39：库内无一条曲线自带形状误差）。</div>`);
  }
  if (d.anchor_reinsert) {
    const cells = (d.anchor_reinsert.cells || []).filter(c => c.band != null);
    rows.push(`<div class="small mt-1"><strong>锚点残差再插入（F-84，causal_use=${esc(d.anchor_reinsert.causal_use || 'diagnostic_only')}）</strong>
      ${cells.length ? `<div class="table-responsive"><table class="table table-sm mb-1 small">
        <thead><tr><th>波段</th><th>m_syn_local</th><th>Δm_local（与实测差）</th><th>1−h_i（F-56 收缩）</th></tr></thead>
        <tbody>${cells.map(c => `<tr><td>${esc(c.band)}</td><td>${num(c.m_syn_local, 4)}</td>
          <td>${num(c.delta_m_local, 4)}</td><td>${num(c.shrink_1_minus_h, 4)}</td></tr>`).join('')}</tbody></table></div>`
        : `<span class="text-secondary">${esc((d.anchor_reinsert.note || '').split('｜').pop())}</span>`}
      <div class="text-secondary">m_syn_local 是诊断镜像列，不顶替主列 mag（F-11：单一全局缩放因子不被架空）；与 (1−h_i) 收缩量并排显示。</div>
      <div class="text-secondary">${esc(d.anchor_reinsert.note || '')}</div></div>`);
  }
  return `<div class="card mb-3"><div class="card-body py-2">
    <strong>诊断增强</strong><span class="small text-secondary ms-2">diagnostics{}（U-44 开启项；关闭时不进入任何计算路径，T-48）</span>
    ${rows.join('')}</div></div>`;
}

// ─── 结果区总渲染（徽章条在 workbench；这里：TXT-2/12 + 告警 + 表 + 脚注） ───

// W-18：TXT-28 空态文案（逐字取自规格 §2.6 TXT-28 行，前端不另写一句）；
// 容器 #specphot-empty 内恰一个 <a id="specphot-empty-register">，包住「滤光片」页短语，
// href 运行时从宿主 index.html 顶栏「滤光片」导航项 DOM 取（不写路由字面量）⇒ 宿主改路由不陈旧。
const T28_BEFORE = '本源登记的波段都还没有透过率/响应曲线 ⇒ 没有曲线可卷，本模块不猜。下一步：到宿主';
const T28_LINK = '「滤光片」页';
const T28_AFTER = '为需要的波段登记曲线（本页只读，不代填）；急看量级可先切 mono 单色近似（F-15、TXT-10，它给的是量级而不是结果），或改用 anchored 模式以实测测光点定标（F-49、F-51）。';
const TXT13 = '只处理来源条当前装载的这一条谱（库内谱或本次上传件）。';
function filtersNavHref() {
  for (const a of document.querySelectorAll('#navLinks a.nav-link')) {
    if ((a.textContent || '').includes('滤光片')) return a.getAttribute('href') || '#';
  }
  return '#';   // 宿主顶栏缺失时的兜底（正常布局不会走到）
}
function renderEmptyState(el) {   // W-18：触发面 = 无来源 / 未计算 / curve_coverage.with_curve = 0
  const card = document.createElement('div');
  card.className = 'card mb-3';
  const body = document.createElement('div');
  body.className = 'card-body py-3 text-center';
  const box = document.createElement('div');
  box.id = 'specphot-empty';
  box.className = 'text-secondary';
  box.appendChild(document.createTextNode(T28_BEFORE));
  const a = document.createElement('a');
  a.id = 'specphot-empty-register';
  a.href = filtersNavHref();
  a.textContent = T28_LINK;
  box.appendChild(a);
  box.appendChild(document.createTextNode(T28_AFTER));
  body.appendChild(box);
  const note = document.createElement('div');   // TXT-13 旁注在场（在 #specphot-empty 之外，保证其 textContent 逐字）
  note.className = 'small text-secondary mt-2';
  note.textContent = 'TXT-13：' + TXT13;
  body.appendChild(note);
  card.appendChild(body);
  el.innerHTML = '';
  el.appendChild(card);
}

export function renderResults(el, ctx) {
  const S = ctx.S;
  const r = S.lastResponse;
  if (!r) {
    renderEmptyState(el);   // W-18：空态 DOM 契约（替换原「尚未计算」占位）
    return;
  }
  const anyTransmission = r.results.some(x => x.curve_kind !== 'throughput');
  const uncal = r.flux_kind && r.flux_kind !== 'absolute';
  const sa = r.s_anchor;
  const anchored = r.mode_effective === 'anchored' && sa;
  el.innerHTML = `
    <div class="card mb-3"><div class="card-body py-2">
      <div class="d-flex align-items-center">
        <strong>结果表</strong>
        <span class="small text-secondary ms-2">spectrum_source=${esc(r.spectrum_source)}
          ${r.spectrum_id != null ? '#' + r.spectrum_id : 'up:' + esc(String(r.spec_hash || '').slice(0, 8))}
          · mode ${esc(r.mode_requested)}→${esc(r.mode_effective)} · flux_kind=${esc(r.flux_kind)}</span>
      </div>
      ${anyTransmission ? `<div class="small text-warning mt-1">⚠ ${TXT.t2}</div>` : ''}
      ${uncal ? `<div class="small text-warning mt-1">TXT-12：${TXT.t12}</div>` : ''}
      ${renderWarnings(r)}
      ${renderOutlierConfirm(r, ctx)}
      ${renderDiagnostics(r)}
      ${anchored ? `<div class="small mt-1" title="${escAttr(TXT.t5n)}">TXT-5（anchored）：已用 ${sa.n_bands_used} 个波段
        求单一缩放因子 κ*=${num(sa.value, 6)}（方向 ${esc(sa.direction || '—')}），σ_κ=${num(sa.sigma, 6)}，
        协方差 ${esc(sa.cov_method || '—')}，χ²=${num(sa.chi2, 3)}/dof=${sa.dof ?? '—'}，
        锚点色域 ${sa.anchor_lambda_range_aa ? sa.anchor_lambda_range_aa.map(x => num(x, 0)).join('–') + ' Å' : '—'}；
        所用实测值：${(sa.anchor_provenance || []).map(p => `${esc(p.band)}(${esc(p.anchor_origin)})`).join('、')}。</div>` : ''}
      <div class="table-responsive mt-1" style="max-height:420px;overflow:auto">
        <table class="table table-sm table-hover mb-0 small sp-result-table">
          <!-- W-15①：首列（波段）position:sticky 冻结，样式见 workbench.ensureStyles -->
          <thead><tr><th>波段</th><th title="curve_kind='transmission' 时后缀 ⚠">mag</th>
            <th title="${escAttr(TXT.t3)}">σ_stat</th><th title="${escAttr(TXT.t23)}">σ_cal</th>
            <th>σ_resp</th><th>f_mJy ± f_err</th><th>overlap</th><th>m_obs</th>
            <th>Δm ± err</th><th>曲线</th><th></th></tr></thead>
          <tbody>${resultRows(r)}</tbody>
        </table>
      </div>
      <div class="small text-secondary mt-1" title="${escAttr(TXT.t23)}">TXT-23：误差三列分列（不合成总误差，F-57）；
        每列求法/口径见表头与展开卡的 err_source/err_scope；Δm 着色：|Δm|&gt;0.2 红、&gt;0.5 深红。</div>
    </div></div>`;
  el.querySelectorAll('.sp-row').forEach(tr => tr.addEventListener('click', () => {
    const d = el.querySelector(`.sp-detail[data-i="${tr.dataset.i}"]`);
    if (d) d.style.display = d.style.display === 'none' ? '' : 'none';
  }));
  const tog = el.querySelector('.sp-warntoggle');
  if (tog) tog.addEventListener('click', () => {
    el.querySelector('.sp-warnmore').classList.toggle('d-none');
  });
  el.querySelectorAll('.sp-locate').forEach(a => a.addEventListener('click', () => {
    const p = document.getElementById('spParams');
    if (p) p.scrollIntoView({ behavior: 'smooth', block: 'start' });   // U-20：定位到参数区
  }));
  // U-52：逐段「剔除并记录」→ 写 S.maskRanges（同一布尔数组），dirty() 触发
  // stale（IA-4）+ 斜纹/mask_hash 在下次计算生效
  el.querySelectorAll('.sp-outlier-accept').forEach(btn => btn.addEventListener('click', () => {
    const w = (r.warnings || []).find(x => x.reason === 'outlier_flagged'
      && Array.isArray(x.segments));
    const seg = w && w.segments[Number(btn.dataset.i)];
    if (!seg || S.maskRanges.length >= C_MAX_EXCLUDE) return;
    S.maskRanges.push([Number(seg[0]), Number(seg[1])]);
    ctx.dirty();
  }));
}

// ─── 对照面板（IA-12：只画同次响应里的数据，切页签不发请求，不写宿主全局点集） ───
export function renderComparison(el, ctx) {
  const r = ctx.S.lastResponse;
  const rows = r ? (r.results || []).filter(x => x.m_obs != null) : [];
  el.innerHTML = `
  <div class="card mb-3"><div class="card-body py-2">
    <strong>对照面板</strong>
    <div class="small text-secondary mt-1" title="${escAttr(TXT.t4)}">TXT-4：${TXT.t4}</div>
    ${!r ? '<div class="text-secondary small mt-2">尚无计算结果。</div>' : rows.length ? `
      <div class="table-responsive mt-2" style="max-height:300px;overflow:auto">
        <table class="table table-sm table-striped mb-0 small">
          <thead><tr><th>波段</th><th>mag（合成）</th><th>m_obs</th><th>Δm</th><th>Δm_err</th>
            <th>来源</th><th>配对</th><th>dt_obs (d)</th></tr></thead>
          <tbody>${rows.map(x => `<tr>
            <td>${esc(x.band)}</td><td>${num(x.mag)}</td><td>${num(x.m_obs)}</td>
            <td class="${Math.abs(x.delta_m || 0) > 0.2 ? 'text-danger' : ''}">${num(x.delta_m)}</td>
            <td>${num(x.delta_m_err, 4)}</td>
            <td><span class="badge ${x.m_obs_origin === 'manual' ? 'bg-info text-dark' : 'bg-secondary'}">${esc(x.m_obs_origin || '—')}</span></td>
            <td>${esc(x.m_obs_kind || '—')}</td><td>${num(x.dt_obs_d, 3)}</td></tr>`).join('')}</tbody>
        </table>
      </div>
      <div id="spCmpPlot" class="mt-2"></div>`
    : '<div class="text-secondary small mt-2">本次响应没有可对照的锚点波段（m_obs 全空）。</div>'}
  </div></div>`;
  renderComparePlot(el.querySelector('#spCmpPlot'), ctx);   // IA-12 比对图（specplot.js）
}
