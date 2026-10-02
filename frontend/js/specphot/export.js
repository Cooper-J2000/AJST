// === specphot 导出（F-43/F-44/F-45：纯前端 Blob 下载，不落盘服务端、不新增端点） ===
// CSV 列序 = §4.3 波段表块（发布即冻结；对照面板无独立列集 ⇒ 不另做块）。首行 TXT-11；
// 谱级字段写表头前 # 注释（一次一份）：flux_median_cgs/lambda_unit_assumed/lambda_frame/
// spectrum_source/spec_hash/tid/meta.*/mw.*/mask_hash/read_stats.*（F-113② preprocess 除
// smooth_px 全键逐个 # preprocess.<k>=）+ interp 固定值 + TXT-27/TXT-23 同文 + 锚点注释块。
// err_source/err_scope 序列化 = F-80⑥：`键=值` 对，`; ` 连接，键序随响应装配序（=§4.2 序），
// 单值不带分隔符，空集 = 零长度空字段；不适用误差列一律空字段（就是 F-80⑥ 的空集形）。
// JSON = API-2 响应原文（JSON.stringify，不另设 schema）。文件名 F-44/IA-17：
// <src><id>_<mode>_<weighting>_<UTC时间戳>.<ext>，库内谱 src=spec+id，上传/粘贴 src=up+hash8。

const COLS = [
  'band', 'filter_id', 'curve_kind', 'curve_source', 'curve_px_per_fwhm', 'curve_nodes',
  'weighting', 'band_mode', 'mag_system', 'mag', 'f_mjy', 'mag_err_stat', 'mag_err_cal',
  'mag_err_resp', 'f_err_mjy', 'resp_method', 'sigma_method', 'rho_lag1', 'corr_infl',
  'lambda_pivot_aa', 'lambda_eff_aa', 'lambda_phot_aa', 'lambda_iso_aa', 'delta_a_band_mono',
  'overlap', 'n_used_pixels', 'n_masked_pixels', 'n_nonpos', 'nonpos_frac', 'gap_bridged',
  'n_dup_lam', 'in_coverage', 'extrapolated', 'm_obs', 'm_obs_origin', 'm_obs_mjd', 'dt_obs_d',
  'dt_tol_eff_d', 'time_precision_d', 'delta_m', 'delta_m_err', 'm_syn_local', 'm_obs_kind',
  'warnings', 'err_source', 'err_scope', 'spec_phot_version',
];
// §4.3 第三块「宿主 de-reddened 曲线导出」列序（发布即冻结，F-89②；纯前端派生件
// 导出——不落盘服务端、不入库、不生成子谱，T-57）。误差列直接搬运 FitResult
// （av_err_lo/hi=err_low/high{Av}、ebv_err=ebv_display_err，§3.9.3.1 禁另算）。
const D_COLS = [
  'lambda_obs_vac_aa', 'lambda_rest_vac_aa', 'flux_before', 'flux_after', 'a_host_mag',
  'host_ext_mode', 'law', 'rv', 'rv_source', 'ebv', 'av', 'screen_z', 'lambda_frame',
  'derivation_depth', 'av_err_lo', 'av_err_hi', 'rv_err_lo', 'rv_err_hi', 'ebv_err',
  'warnings', 'spec_phot_version',
];
// §4.3 第二块「谱线导出（API-4）」列序（P3 切片 2，发布即冻结 F-45）。误差键
// 补齐集与量值键同批（F-94⑤）；ew_err_terms 三键拆成 ew_err_photon/
// ew_err_continuum/ew_err_continuum_coherent 三列；mask_applied 两个子键平铺；
// err_source/err_scope 仍按 F-80⑥ 串行化（不按量拆列，F-45 恒定列集）。
const L_COLS = [
  'species', 'line_kind', 'lambda_rest_vac_aa', 'lambda_obs_vac_aa', 'line_frame', 'model',
  'ew_signed_aa', 'ew_obs_aa', 'ew_rest_aa', 'ew_err_aa', 'line_flux', 'depth',
  'fwhm_obs_aa', 'fwhm_intr_aa', 'vel_fwhm_kms', 'r_source', 'snr_res', 'detected',
  'upper_limit_3sigma', 'column_density', 'column_density_lower_bound', 'f_oscillator',
  'f_source', 'sky_subtracted', 'baseline_type', 'mask_applied_abs', 'mask_applied_emis',
  'snr_def', 'notes', 'lambda_err_aa', 'vel_shift_err_kms', 'fwhm_obs_err_aa',
  'fwhm_intr_err_aa', 'vel_fwhm_err_kms', 'ew_rest_err_aa', 'line_flux_err',
  'depth_err_lo', 'depth_err_hi', 'ratio', 'ratio_ref', 'ratio_err', 'column_density_err',
  'ew_err_form', 'ew_err_photon', 'ew_err_continuum', 'ew_err_continuum_coherent',
  'err_source', 'err_scope', 'engine_status', 'cov_method', 'spec_phot_version',
];
const TXT11 = 'TXT-11: 导出不等于入库：本文件为交互结果留档，AJST 星表库内无此点。';
const TXT21 = 'TXT-21: 上传谱的波长未带框架标记，本模块按真空处理；若实际是空气波长，'
  + '窄带偏移即达 0.92–2.20 Å（≈82–87 km/s），会同时污染通带边界与线位。'
  + '库内谱相反：它已由宿主换到真空，但 wavelength_type 留空时「真空」是推定值。';
const TXT23 = 'TXT-23: 每一列误差的求法由 err_source[k] 标明，口径范围由 err_scope[k] 标明'
  + '只含随机项（stat）还是含定标传播（stat+cal）。标 none 者其误差键是空值并附书面原因，不是 0。';

// F-80③ 浮点打印式：先舍入 12 位有效数字，再最短往返 repr（JS String(number) 即最短往返）
function fnum(x) {
  if (typeof x !== 'number' || !isFinite(x)) return '';
  return String(Number(x.toPrecision(12)));
}
function scalar(v) {
  if (v == null) return '';
  if (typeof v === 'number') return fnum(v);
  if (typeof v === 'boolean') return String(v);
  return String(v);
}
// F-80⑥ 多值列：`键=值` + `; ` 连接（键序不排序）；空对象 = 零长度空字段
function serPairs(o) {
  return Object.entries(o || {}).map(([k, v]) => `${k}=${scalar(v)}`).join('; ');
}
// warnings 告警列：code[·reason] 串（嵌套对象不进表体，F-44；信息位是 code 与 reason）
function serWarnings(ws) {
  return (ws || []).map(w => w.code + (w.reason ? '·' + w.reason : '')).join('; ');
}
function csvSafe(s) {
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function cell(v) {
  if (v != null && typeof v === 'object' && !Array.isArray(v)) return csvSafe(serPairs(v));
  return csvSafe(scalar(v));
}

function headerLines(r, S = null) {
  const L = ['# ' + TXT11];
  // W-30③：框架为默认 vacuum（未声明）/ unknown ⇒ 首部追加 TXT-21 声明（与来源条常驻块同源）；
  // 库内谱由宿主换真空，不在此挂上传件声明。
  const isUpload = r.spectrum_source !== 'catalog';
  if (r.lambda_frame === 'unknown' || (isUpload && !(S && S.uploadFrameDeclared))) {
    L.push('# ' + TXT21);
  }
  const h = (k, v) => L.push('# ' + k + '=' + (v == null ? '' :
    (typeof v === 'object' ? JSON.stringify(v) : typeof v === 'number' ? fnum(v) : String(v))));
  h('spec_phot_version', r.spec_phot_version);
  h('spectrum_source', r.spectrum_source);
  h('spectrum_id', r.spectrum_id);
  h('spec_hash', r.spec_hash);
  h('tid', r.tid);
  h('lambda_frame', r.lambda_frame);
  h('lambda_unit_assumed', r.lambda_unit_assumed);
  h('flux_kind', r.flux_kind);
  h('flux_median_cgs', r.flux_median_cgs);
  h('mask_hash', r.mask_hash);
  h('mode_requested', r.mode_requested);
  h('mode_effective', r.mode_effective);
  for (const [k, v] of Object.entries(r.meta || {})) h('meta.' + k, v);       // 元数据逐键
  for (const [k, v] of Object.entries(r.mw || {})) h('mw.' + k, v);           // 银消逐键
  for (const [k, v] of Object.entries(r.read_stats || {})) h('read_stats.' + k, v);
  h('interp', (r.results || [])[0] && r.results[0].interp);                   // F-46 固定值
  const pp = r.preprocess || {};
  for (const k of Object.keys(pp)) if (k !== 'smooth_px') h('preprocess.' + k, pp[k]);  // F-113②
  // TXT-27（恒在场，恒等态写 none，不得整行消失；F-108②：factor>1 时摘要行
  // 写明丢弃 n mod k 个像元 —— n=read_stats.n_points 与 k、n_rebinned_pixels 均
  // 已在导出头回显 ⇒ 可复算，不另立键）
  const r0 = (r.results || [])[0] || {};
  let droppedTxt = '';
  if (Number(pp.factor) > 1 && pp.n_rebinned_pixels != null
      && r.read_stats && r.read_stats.n_points != null) {
    droppedTxt = ' ／ 丢弃 ' + (Number(r.read_stats.n_points)
      - Number(pp.factor) * Number(pp.n_rebinned_pixels)) + ' 像元（n mod k）';
  }
  L.push('# TXT-27 本次预处理：factor=' + scalar(pp.factor) + ' ／ rebin_gain=' + scalar(pp.rebin_gain)
    + ' ／ 掩膜 ' + ((pp.mask_ranges || []).length) + ' 段（mask_hash=' + scalar(pp.mask_hash) + '）'
    + ' ／ 误差列判定 ' + scalar(pp.errcol_verdict) + '（sigma_method=' + scalar(r0.sigma_method) + '）'
    + ' ／ preprocess_hash=' + scalar(pp.preprocess_hash) + droppedTxt);
  L.push('# ' + TXT23);
  // 锚点注释块（TXT-20：手加锚点自证；脱离库可复核）
  L.push('#anchor,band,mag,mag_system,mjd,mag_err,anchor_origin,used');
  for (const a of (r.anchor_rows || [])) {
    L.push('#anchor,' + ['band', 'mag', 'mag_system', 'mjd', 'mag_err', 'anchor_origin', 'used']
      .map(k => cell(a[k])).join(','));
  }
  return L;
}

// F-44 文件名：<src><id>_<mode>_<weighting>_<UTC时间戳>.<ext>（up 件 id 位 = spec_hash 前 8 位）
function fname(r, ext) {
  const src = r.spectrum_source === 'catalog'
    ? 'spec' + (r.spectrum_id != null ? r.spectrum_id : '')
    : 'up' + String(r.spec_hash || '').slice(0, 8);
  const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  const w = ((r.results || [])[0] || {}).weighting || '';
  return `${src}_${r.mode_effective || r.mode_requested || ''}_${w}_${ts}.${ext}`;
}

function download(name, text, mime) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: mime + ';charset=utf-8' }));
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 500);
}

// P1-2：二进制件下载（PNG 用；与 download 同一 F-44 命名与 revoke 纪律）
function downloadBlob(name, blob) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 500);
}

// U-17：导出 CSV（§4.3 波段表块；null 一律空字段，TXT-11 首行）
export function exportCsv(S) {
  const r = S && S.lastResponse;
  if (!r) return;
  const rows = [COLS.join(',')].concat((r.results || []).map(row =>
    COLS.map(k => (k === 'warnings' ? csvSafe(serWarnings(row.warnings))
                   : cell(k === 'spec_phot_version' && row[k] == null ? r.spec_phot_version : row[k])))
      .join(',')));
  download(fname(r, 'csv'), headerLines(r, S).concat(rows).join('\r\n'), 'text/csv');
}

// U-17：导出 JSON = API-2 响应原文（不另设 schema，F-44）
export function exportJson(S) {
  const r = S && S.lastResponse;
  if (!r) return;
  download(fname(r, 'json'), JSON.stringify(r, null, 2), 'application/json');
}

// ─── 宿主 de-reddened 曲线导出（P2 切片 2d，F-89② + W-33 导出件头） ─────────

// TXT-22 逐字（§2 TXT-22 行原文，值按 rv_source/模式替换 …；fit 模式用规格给的
// 改写句；末句「宿主与银河之间那段路径上的消光未计」三处逐字之一——本函数同时
// 供结果卡/图注/导出件头消费，保证三处一字不差，W-33/F-88）。返回纯文本（调用
// 方自行 HTML 转义）。
export function txt22Text(r) {
  const b = r && r.fits ? (r.fits.find(f => f.host_ext_basis) || {}).host_ext_basis : null;
  if (r.host_ext_mode === 'off' || !b) return '';
  const z = (b.screen_z ?? 0);
  const zTxt = '尘埃屏 z=' + (typeof z === 'number' ? String(Number(z.toPrecision(12))) : z);
  const head = r.host_ext_mode === 'fit'
    ? `以下 A_V 为拟合值，E(B−V) 由它反算、仅供显示（R_V=${b.rv}，${b.rv_source_note || b.rv_source}）`
      + `⇒ A_V = R_V·E(B−V) = ${(r.fits.find(f => f.ebv_display != null) || {}).params?.Av ?? '—'}，`
      + `逐点作用在静止系真空波长上（${zTxt}）。`
    : `本次宿主星系消光取 E(B−V)=${r.ebv_used}、R_V=${b.rv}（${b.rv_source_note || b.rv_source}）`
      + `⇒ A_V = R_V·E(B−V) = ${r.av_prescribed}，逐点作用在静止系真空波长上（${zTxt}）。`;
  return head + '图中"改正后"曲线与导出件里的对应列是本页派生件：未入库、未生成子谱'
    + '（与银河侧的 gext_corr 子谱约定不同，F-89）。'
    + '本方案不设中间消光屏 ⇒ 宿主与银河之间那段路径上的消光未计（§1.3）';
}

// §3.9.3.1：av = A_V、ebv = ebv_display；误差列直接搬运 FitResult，不得另算。
// prescribe 下 av/ebv 是输入 ⇒ 误差列空字段 + CA-43 书面原因（不是 0）；
// rv_free=false ⇒ rv 是所选律常数 ⇒ rv_err_* 恒空字段（metadata）。
function deredPick(r) {
  if (r.host_ext_mode === 'fit') {
    const pick = (r.fits || []).find(f => f.ebv_display != null);
    if (!pick) return null;
    return { pick,
      av: pick.params ? pick.params.Av : null, ebv: pick.ebv_display,
      av_err_lo: pick.err_low ? pick.err_low.Av : null,
      av_err_hi: pick.err_high ? pick.err_high.Av : null,
      ebv_err: pick.ebv_display_err };
  }
  if (r.host_ext_mode === 'prescribe') {
    return { pick: null, av: r.av_prescribed, ebv: r.ebv_used,
             av_err_lo: null, av_err_hi: null, ebv_err: null };
  }
  return null;
}

// F-44 文件名口径（de-reddened 块无 weighting 概念，该位记 'dered' 标识块身份）：
// <src><id>_<host_ext_mode>_dered_<UTC时间戳>.csv（up 件 id 位 = spec_hash 前 8 位）
function fnameDered(r, ext) {
  const src = r.spectrum_source === 'catalog'
    ? 'spec' + (r.spectrum_id != null ? r.spectrum_id : '')
    : 'up' + String(r.spec_hash || '').slice(0, 8);
  const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  return `${src}_${r.host_ext_mode || ''}_dered_${ts}.${ext}`;
}

function deredHeaderLines(r, dr) {
  const L = ['# ' + TXT11];
  L.push('# TXT-22: ' + txt22Text(r));                  // W-33：导出件头逐字（含末句）
  const h = (k, v) => L.push('# ' + k + '=' + (v == null ? '' :
    (typeof v === 'object' ? JSON.stringify(v) : typeof v === 'number' ? fnum(v) : String(v))));
  h('spec_phot_version', r.spec_phot_version);
  h('spectrum_source', r.spectrum_source);
  h('spectrum_id', r.spectrum_id);
  h('spec_hash', r.spec_hash);
  h('tid', r.tid);
  // host_ext_basis 七键（F-88/T-56：未平铺进表体的 rv_source_note/lam_axis/form
  // 三键与其余谱级字段同规则写在 # 注释行 ⇒ 七键在 CSV 内仍逐键可核）
  const b = dr.host_ext_basis || {};
  for (const k of ['law', 'rv', 'rv_source', 'rv_source_note', 'screen_z',
                   'lam_axis', 'form']) h('host_ext_basis.' + k, b[k]);
  h('derivation_depth', dr.derivation_depth);           // F-89④：恒 1，禁止对 B 再改正
  h('curve_hash', dr.curve_hash);
  h('mask_hash', r.mask_hash);                          // preprocess 双哈希（F-113② 同族）
  h('preprocess_hash', (r.preprocess || {}).preprocess_hash);
  if (r.host_ext_mode === 'fit') {
    L.push('# 误差列直接搬运 FitResult（§3.9.3.1，不得另算）：av_err_lo/hi='
      + 'err_low/high{Av}；ebv_err=ebv_display_err；rv 为所选律常数 ⇒ rv_err_* 空字段（metadata）');
  } else {
    L.push('# CA-43: host_ext_mode=prescribe ⇒ av/ebv 是给定输入不是测值，'
      + '其误差列写空字段并附本书面原因（不是 0；§3.9.3.1）');
  }
  L.push('# lambda_rest_vac_aa 与 lambda_obs_vac_aa 为定义导出（λ_rest = λ_obs/(1+z)，'
      + 'metadata，TXT-23 照抄/定义类不立误差列）；null 一律写空字段');
  return L;
}

// P2 切片 2d：导出 de-reddened 曲线 CSV（§4.3 B 块列序；TXT-11 首行 + TXT-22 旁注；
// n_points=0 或 host_ext_mode=off ⇒ 无曲线可导，调用方禁用按钮）
export function exportContinuumCsv(r) {
  if (!r || r.host_ext_mode === 'off') return;
  const dr = r.de_reddened || {};
  const n = Number(dr.n_points) || 0;
  if (!n || !Array.isArray(dr.flux_after) || dr.flux_after.length !== n) return;
  const pk = deredPick(r);
  if (!pk) return;
  const b = dr.host_ext_basis || {};
  const frame = (r.meta || {}).lambda_frame;
  const scal = {
    host_ext_mode: r.host_ext_mode, law: b.law, rv: b.rv, rv_source: b.rv_source,
    ebv: pk.ebv, av: pk.av, screen_z: b.screen_z, lambda_frame: frame,
    derivation_depth: dr.derivation_depth,
    av_err_lo: pk.av_err_lo, av_err_hi: pk.av_err_hi,
    rv_err_lo: null, rv_err_hi: null, ebv_err: pk.ebv_err,
    warnings: serWarnings(r.warnings), spec_phot_version: r.spec_phot_version,
  };
  const rows = [D_COLS.join(',')].concat(
    Array.from({ length: n }, (_, i) => D_COLS.map(k => {
      if (k === 'lambda_obs_vac_aa') return cell(dr.lam_obs_vac_aa[i]);
      if (k === 'lambda_rest_vac_aa') return cell(dr.lam_rest_vac_aa[i]);
      if (k === 'flux_before') return cell(dr.flux_before[i]);
      if (k === 'flux_after') return cell(dr.flux_after[i]);
      if (k === 'a_host_mag') return cell(dr.a_host_mag[i]);
      return cell(scal[k]);
    }).join(',')));
  download(fnameDered(r, 'csv'), deredHeaderLines(r, dr).concat(rows).join('\r\n'),
           'text/csv');
}

// P1-2（F-89②/U-47 三件套的 PNG 支路）：谱图（含双曲线叠加）导出 PNG。
// specplot 画布本身只烘焙了 legend（曲线 A/B 两行文字），TXT-22 图注是画布外的
// DOM 行 ⇒ 导出前在画布副本底部临时绘制图注（txt22Text 同一构造器 ⇒ 与结果卡/
// 导出件头三处一字不差，W-33），不改原画布、不落盘服务端、不新增端点。
// 文件名沿用 F-44 de-reddened 块口径（fnameDered），扩展名 .png。
// host_ext_mode=off 或画布不可用 ⇒ 无可导图（调用方按钮禁用同 CSV）。
export function exportContinuumPng(r, canvas) {
  if (!r || r.host_ext_mode === 'off' || !canvas || !canvas.width) return;
  const txt = txt22Text(r);
  const dpr = window.devicePixelRatio || 1;
  const fs = Math.max(11, Math.round(11 * dpr));
  const pad = Math.round(6 * dpr), lh = Math.round(fs * 1.45);
  const maxW = canvas.width - 2 * pad;
  // 中文无空格断词 ⇒ 逐字符量宽换行（只读量宽，不画）
  const meas = document.createElement('canvas').getContext('2d');
  meas.font = fs + 'px sans-serif';
  const lines = [];
  if (txt) {
    let line = '';
    for (const ch of ('TXT-22：' + txt)) {
      if (line && meas.measureText(line + ch).width > maxW) { lines.push(line); line = ch; }
      else line += ch;
    }
    if (line) lines.push(line);
  }
  const off = document.createElement('canvas');
  off.width = canvas.width;
  off.height = canvas.height + (lines.length ? pad + lines.length * lh : 0);
  const g = off.getContext('2d');
  g.fillStyle = '#ffffff';                       // 画布透明底 ⇒ 导出件铺白底自成一幅图
  g.fillRect(0, 0, off.width, off.height);
  g.drawImage(canvas, 0, 0);
  g.font = fs + 'px sans-serif';
  g.fillStyle = '#444444';
  g.textBaseline = 'top';
  let y = canvas.height;
  for (const ln of lines) { g.fillText(ln, pad, y); y += lh; }
  off.toBlob(blob => { if (blob) downloadBlob(fnameDered(r, 'png'), blob); }, 'image/png');
}

// ─── §4.3 谱线导出（P3 切片 2，API-4 响应 → 「每线一行」平面表，F-44/F-45） ──
// TXT-11 首行 + TXT-23 表头注释 + 双哈希（mask_hash/preprocess_hash，F-113② 同族）
// 与谱级字段写 # 注释行；preprocess 除 smooth_px 全键逐个 # preprocess.<k>=。
// F-67/F-94⑤：null 一律空字段（吸收线 line_flux、发射线 depth、vel_* 家族、
// ratio 族单行请求恒 null——书面原因在 notes 列）。
// engine_status 是簿记容器不进 F-80⑥ 的键=值纪律：九键按 `键=值; ` 序列化
// （active_mask 数组以 , 连接），不另立列（实现裁量登记，键集=§4.2 九键恒定）。
function serEngineStatus(es) {
  if (!es || typeof es !== 'object') return '';
  return Object.entries(es)
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(',') : scalar(v)}`)
    .join('; ');
}

function lineCell(row, r, k) {
  if (k === 'spec_phot_version') return cell(row[k] == null ? r.spec_phot_version : row[k]);
  if (k === 'baseline_type') return cell(row.baseline_type);
  if (k === 'mask_applied_abs') return cell((row.mask_applied || {}).abs_table);
  if (k === 'mask_applied_emis') return cell((row.mask_applied || {}).emis_table);
  if (k === 'notes') return csvSafe((row.notes || []).join('; '));
  if (k === 'ew_err_photon') return cell((row.ew_err_terms || {}).photon);
  if (k === 'ew_err_continuum') return cell((row.ew_err_terms || {}).continuum);
  if (k === 'ew_err_continuum_coherent') return cell((row.ew_err_terms || {}).continuum_coherent);
  if (k === 'err_source') return csvSafe(serPairs(row.err_source));
  if (k === 'err_scope') return csvSafe(serPairs(row.err_scope));
  if (k === 'engine_status') return csvSafe(serEngineStatus(row.engine_status));
  return cell(row[k]);
}

// F-44 文件名（线表块无 weighting 位，该位记 'line' 标识块身份，与 dered 块同裁量）：
// <src><id>_line_<UTC时间戳>.csv（up 件 id 位 = spec_hash 前 8 位）
function fnameLines(r, ext) {
  const src = r.spectrum_source === 'catalog'
    ? 'spec' + (r.spectrum_id != null ? r.spectrum_id : '')
    : 'up' + String(r.spec_hash || '').slice(0, 8);
  const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  return `${src}_line_${ts}.${ext}`;
}

function linesHeaderLines(r) {
  const L = ['# ' + TXT11, '# ' + TXT23];
  const h = (k, v) => L.push('# ' + k + '=' + (v == null ? '' :
    (typeof v === 'object' ? JSON.stringify(v) : typeof v === 'number' ? fnum(v) : String(v))));
  h('spec_phot_version', r.spec_phot_version);
  h('spectrum_source', r.spectrum_source);
  h('spectrum_id', r.spectrum_id);
  h('spec_hash', r.spec_hash);                     // 双哈希之一（F-80）
  h('tid', r.tid);
  h('lambda_frame', (r.meta || {}).lambda_frame);   // 谱级（与逐条 line_frame 分列）
  h('z_eff', r.z_eff);
  h('mask_hash', r.mask_hash);                      // 双哈希之二
  h('preprocess_hash', (r.preprocess || {}).preprocess_hash);
  for (const [k, v] of Object.entries(r.meta || {})) h('meta.' + k, v);
  const pp = r.preprocess || {};
  for (const k of Object.keys(pp)) if (k !== 'smooth_px') h('preprocess.' + k, pp[k]);  // F-113②
  // W-25 两条框架闸门（不得合并表述）：线表侧/谱级各占一行注释
  const g = r.frame_gates || {};
  L.push('# W-25① 线表侧：line_frame=' + scalar((g.w25_1_line_side || {}).line_frame)
    + ' verified=' + scalar((g.w25_1_line_side || {}).verified)
    + '（未经 M-6 复核 ⇒ 位置线只作标记，vel_* 恒 null）');
  L.push('# W-25② 谱级：lambda_frame=' + scalar((g.w25_2_spectrum_side || {}).lambda_frame)
    + ' blocked=' + scalar((g.w25_2_spectrum_side || {}).blocked)
    + '（unknown ⇒ vel_*/z_fit 禁用；与①是两道独立的门，文案不合并）');
  return L;
}

// U-17（S3 页签）：导出 §4.3「谱线导出」块 CSV；lastLines 缺失或无 lines 行 ⇒ 不导
export function exportLinesCsv(r) {
  if (!r || !Array.isArray(r.lines) || !r.lines.length) return;
  const rows = [L_COLS.join(',')].concat(r.lines.map(row =>
    L_COLS.map(k => lineCell(row, r, k)).join(',')));
  download(fnameLines(r, 'csv'), linesHeaderLines(r).concat(rows).join('\r\n'), 'text/csv');
}

// ─── §4.3 第四块：吸收系统表导出（P3d，API-4 的 absorber_systems[]） ──
// 列序冻结（F-45）；U-48 关 ⇒ 表头照写、无数据行（F-105①，T-72②）；
// 四个表体列（class_thresholds_dex/metal_line_ids/masked_ranges/
// forest_stats_reasons）按 F-80⑥ 序列化成 ';' 连接串（F-44 唯一例外写法）；
// 首行 TXT-11，TXT-24/TXT-25 进导出件头（TXT-24 行明文"导出件头"）；
// null 一律写空字段；forest_stats 列恒空、forest_stats_reasons 列恒非空（F-102③）。
const TXT24_ABS = 'TXT-24: 该 log N(H I) 来自 Lyα 阻尼翼的 Voigt 拟合：z 固定在金属线测得区间内（z_source）、'
  + 'b 不是自由参数（b_source 写明它来自哪一路），误差是 Δχ²=1 剖面区间 ⇒ 只含随机项，不含连续谱安置的系统项。';
const ABS_COLS = [
  'system_id', 'class', 'class_thresholds_dex', 'intervening_or_host',
  'z_abs', 'z_err', 'z_source', 'lambda_rest_lya_vac_aa',
  'wing_logn', 'wing_logn_err_lo', 'wing_logn_err_hi',
  'wing_logn_alt', 'wing_logn_alt_err_lo', 'wing_logn_alt_err_hi',
  'wing_spread_dex', 'wing_b_assumption_kms', 'b_source',
  'cont_family_a', 'cont_family_b', 'wing_snr_res', 'wing_n_pixels',
  'metal_line_ids', 'metal_sat_flags', 'masked_ranges',
  'forest_stats', 'forest_stats_reasons', 'blue_side_igm_masked',
  'n_lines_in_masked_absorbers', 'notes', 'warnings', 'err_source',
  'err_scope', 'spec_phot_version'];

function absorberCell(sy, r, k) {
  if (k === 'spec_phot_version') return cell(sy[k] == null ? r.spec_phot_version : sy[k]);
  if (k === 'class_thresholds_dex')
    return csvSafe(Object.entries(sy.class_thresholds_dex || {})
      .map(([kk, vv]) => `${kk}=${fnum(vv)}`).join('; '));
  if (k === 'metal_line_ids') return csvSafe((sy.metal_line_ids || []).join('; '));
  if (k === 'metal_sat_flags') return csvSafe(JSON.stringify(sy.metal_sat_flags || []));
  if (k === 'masked_ranges')
    return csvSafe((sy.masked_ranges || []).map(seg => seg.map(v => fnum(v)).join('-')).join('; '));
  if (k === 'forest_stats') return cell(null);        // 恒空（F-102③）
  if (k === 'forest_stats_reasons') return csvSafe((sy.forest_stats_reasons || (r.diagnostics || {}).forest_stats_reasons || []).join('; '));
  if (k === 'n_lines_in_masked_absorbers')
    return cell(sy.n_lines_in_masked_absorbers == null
      ? ((r.diagnostics || {}).cross_link_gate || {}).n_lines_in_masked_absorbers
      : sy.n_lines_in_masked_absorbers);
  if (k === 'notes') return csvSafe((sy.notes || []).join('; '));
  if (k === 'warnings') return csvSafe(serWarnings(sy.warnings));
  if (k === 'err_source') return csvSafe(serPairs(sy.err_source));
  if (k === 'err_scope') return csvSafe(serPairs(sy.err_scope));
  return cell(sy[k]);
}

export function exportAbsorberCsv(r) {
  if (!r) return;
  const systems = (r.diagnostics || {}).absorber_systems || [];
  const rows = [ABS_COLS.join(',')].concat(systems.map(sy =>
    ABS_COLS.map(k => absorberCell(sy, r, k)).join(',')));
  const L = ['# ' + TXT11, '# ' + TXT24_ABS];
  L.push('# mask_hash=' + (r.mask_hash || ''));
  L.push('# cross_link_gate=' + JSON.stringify((r.diagnostics || {}).cross_link_gate || {}));
  download(fnameLines(r, 'csv').replace('_line_', '_absorber_'),
    L.concat(rows).join('\r\n'), 'text/csv');
}
