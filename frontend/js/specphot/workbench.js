// === specphot 工作台外壳（#/tools/specphot） ===
// 三段式布局（F-0i9）：顶部来源条（source_panel.js）+ 口径徽章条；左 = 谱图占位 + 结果区
// （results.js）+ 锚点表（anchors.js）；右 = 波段选择（②）+ 参数区（③）+ 计算按钮（⑤）；
// 底部页签 S1 / 对照面板 / S2 / S3（U-03：S2 随 P2 2b、S3 随 P3 切片 2 解禁）。
// 状态机 §2.5：idle → dirty → computing → fresh/stale/error（IA-1…IA-19）。
// 上传谱数组只留在本模块内存（F-0i6/F-0i8），参数与手加锚点行按 IA-11 存 sessionStorage。
import { api, isAuthed, API_BASE } from '../api.js';
import { esc, escAttr } from '../utils.js';
import { parseCoord } from '../coords.js';
import { renderSourcePanel } from './source_panel.js';
import { renderAnchors, conflictBands, manualAnchorRows, checkedAnchorBands } from './anchors.js';
import { renderResults, renderComparison, buildPhotometryRequest } from './results.js';
import { renderSpecPlot } from './specplot.js';
import { exportCsv, exportJson } from './export.js';
import { renderS2Panel, renderContinuumResults, attachCurveOverlay } from './continuum_ui.js';
import { renderLinesPanel, renderLinesResults } from './lines_ui.js';

// §7.6 前端同值常量（与后端 specphot/constants.py 对齐；零新增依赖）
const C_MAX_BANDS = 16;            // Q-1
const C_MAX_ANCHORS = 24;          // Q-25
const C_MAX_UPLOAD_BYTES = 2_000_000;   // F-76
const C_MAX_EXCLUDE = 32;          // Q-2 / F-107⑤（与后端 constants.py 同值）
const TXT = {
  t1: '该数值由光谱卷积滤光片响应算出，口径为光子/能量计数之一，不是仪器实测。',      // TXT-1
  t13: '只处理来源条当前装载的这一条谱（库内谱或本次上传件）。',                       // TXT-13
  t27: '本次预处理',                                                                  // TXT-27
  // TXT-28 空态文案归 results.js（W-18 空态契约在结果区渲染，此处不再另存一份防两处漂移）
};

// ─── 模块级工作台状态（F-0i8：跨路由保留；上传数组只在此内存） ───
const S = {
  state: 'idle',            // idle|dirty|computing|fresh|stale|error
  sourceKind: null,         // 'catalog'|'upload'|'paste'|null
  spectrumId: null, tid: null,
  upload: null,             // {spec_hash,n_points,lam_aa,flux,flux_err,columns,flux_unit,flux_unit_assumed,lam_min,lam_max,source}
  metaForm: { ra: '', dec: '', z: '', mjd: '', category: 'other' },
  uploadFrame: 'vacuum',    // U-39：上传件波长框架（库内谱恒 vacuum，只读回显）
  ebv: { value: null, rv: 3.1, law: 'P92', available: null },
  bandsMeta: [], curveCoverage: null, spectrumInfo: null,
  selectedBands: [], bandGroup: '',
  params: { weighting: 'photon', mode: 'auto', mag_system: 'AB', err_policy: 'auto',
            dt_tol_d: '', allow_mono: false, mw_correct: true, ebv_override: '',
            errcol_choice: '',   // U-53 确认态（''=未确认；verdict=ok 时不需要）
            factor: 1,           // U-49 合束因子（P2 2c 解禁；闭集 [1,2,3,4,6,8]）
            smooth_on: false, smooth_px: 3 },   // U-51 绘图平滑（纯前端绘制，不转 stale）
  maskRanges: [], manualRows: [], anchorChecks: {},
  lastResponse: null, activeTab: 's1',
  parseError: null, featureNotice: null, busyNotice: null, errorMsg: null,
  abortCtrl: null, spinnerTimer: null,
  // W-9：429 倒计时（服务忙，按钮禁用至该时间戳）；W-15②：窄屏参数抽屉开合
  busyUntilTs: 0, busyTimer: null, paramOpen: undefined,
  // W-30：上传件框架是否由用户显式声明（false = 默认 vacuum 未声明 ⇒ TXT-21 常驻）
  uploadFrameDeclared: false,
  // U-44（§3.12，P2c 起前三项解禁）：诊断增强开关，默认全关；输出只进
  // diagnostics{}（T-48①：全关时响应与基线逐字节同）。不放进 params —— T-8
  // 的默认值表按 params 扫描，六键的默认 false 由服务端缺省承担。
  diagFlags: { z_from_lines: false, beta_matrix: false, resp_perturb: false,
               anchor_reinsert: false, frame_probe: false, sky_subtract: false },
  // W-28：用户手改的元数据项（前端侧 provenance=user 覆盖）与 z 改动的静止系过期提示
  metaUserEdits: {}, zStaleNote: false,
  // P2 2b（S2 页签）：连续谱拟合参数（U-21/U-22 + U-45…U-47）与最近一次 API-3 响应
  contParams: { models: ['pl', 'pl_dust'], host_ext_mode: 'fit', ebv: '',
                law: 'smc', rv: '', rv_source: '', poly_order: 3,
                overDered: true },   // U-47 双谱叠加（纯显示开关，默认开，F-89①）
  lastContinuum: null,
  // P3 切片 2（S3 页签）：谱线三步向导参数（U-23…U-27/U-30/U-31）与最近一次
  // API-4 响应；lineStep 是向导步（U-23），lineStale = 步 1/2 改动后的「需重算」标记。
  lineParams: { species: '', lambda_rest_aa: '', id_table: '', line_kind: '',
                halfwidth: 40, baseline_order: 1, profile: 'gauss1', R: '',
                line_frame: '', sky_handling: 'mask', n_boot: 200, err_seed: '',
                z: '' },
  lineStep: 1, lineStale: false, lastLines: null,
};

// ─── W-15①/W-15②/W-18 样式（一次性注入，幂等）：首列冻结 + 窄屏参数抽屉 ───
function ensureStyles() {
  if (document.getElementById('sp-style')) return;
  const st = document.createElement('style');
  st.id = 'sp-style';
  st.textContent = `
    /* W-15①：结果表首列冻结（波段列），加背景防透叠；表头同处理且层级更高 */
    .sp-result-table th:first-child, .sp-result-table td:first-child {
      position: sticky; left: 0; background-color: var(--bs-table-bg); z-index: 1;
    }
    .sp-result-table thead th:first-child { z-index: 3; }
    .sp-result-table.table-hover tbody tr:hover > *:first-child {
      background-color: var(--bs-table-hover-bg);
    }
    /* W-15②：≤576px 参数抽屉——sp-closed 只在窄屏生效，桌面永远展开 */
    @media (max-width: 575.98px) {
      .sp-param-body.sp-closed { display: none; }
    }`;
  document.head.appendChild(st);
}

let _root = null, _els = {};

// ─── 带结构化错误信息的 POST（保留 code/reason/phase/status，A-3 错误出口） ───
async function spPost(path, body, signal = null) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin', body: JSON.stringify(body), signal,
  });
  let data = null;
  try { data = await resp.json(); } catch {}
  if (!resp.ok) {
    const err = new Error((data && (data.error || data.message)) || `${resp.status} ${resp.statusText}`);
    err.status = resp.status;
    err.payload = data || null;
    err.code = (data && data.code) || null;
    err.reason = (data && data.reason) || null;
    err.phase = (data && data.phase) || null;
    throw err;
  }
  return data;
}

// ─── U-44 诊断增强开关组（§3.12 / §9 P2c·P3c 行；W-32 前后半：每项旁写明新增
// 列与新增计算规模，未解锁项禁用且 tooltip 指期次/前置（E-15/A-3）。P3c 定态：
// z_from_lines/frame_probe 实现已就位但判据挂 M-6（线表帧/f 未复核，registry
// 复核表为空）⇒ 保持禁用、tooltip 写明解锁条件；sky_subtract 的消费点在 S3
// 步 1 的 U-31（F-86），本 S1/S2 面板的开关不消费 ⇒ 禁用并指路） ─────────────
const DIAG_META = [
  ['z_from_lines', 'M6',
   '由线位反推红移（F-81）：实现已随 P3c 就位，但候选线表须 M-6 复核（line_frame + f 值，registry 复核表仍为空）才有判据——整数 Å 线表的空气/真空歧义即 82–87 km/s。解锁条件：M-6 完成。开启后新增 diagnostics{}.z_from_lines（z_fit/σ_z/逐线采信-否决），CA-36 可达；计算 ≤300 ms（FFT 互相关，无迭代）'],
  ['beta_matrix', 'P2c',
   '两两颜色 β 矩阵（F-82）：新增 diagnostics{}.beta_matrix 上三角格集（每格 beta/σ_β/color/dt_d），CA-40 可达；新增主表列：无；新增计算规模：O(波段²) 纯函数，可忽略'],
  ['resp_perturb', 'P2c',
   '通带形状扰动 ⇒ 真实 σ_resp（F-83）：mag_err_resp 换扰动散布口径（resp_method=perturbation，CA-39）；新增主表列：无（既有两列换值）；新增计算规模：60 组 × 波段数次向量化积分（≤2 s 预算内，触顶降规模并回显 perturb_n，Q-27/ST-9）'],
  ['anchor_reinsert', 'P2c',
   '锚点残差再插入（F-84）：新增列 m_syn_local（诊断镜像，不顶替 mag）+ diagnostics{}.anchor_reinsert 每波段 (1−h_i) 收缩量并排；新增计算规模：每参与波段 1 次重积分'],
  ['frame_probe', 'M6',
   '上传件波长框架反证（F-85）：实现已随 P3c 就位，但参考特征（大气带/天光线）的真空位置须经 M-6 复核才有判据（C_MASK 表整数 Å 中心既非 air 也非 vacuum 精确值）。解锁条件：M-6 完成。开启后新增 diagnostics{}.frame_probe（frame_suggestion/n_support/log_lik_ratio），只进诊断、不自动改写 U-39'],
  ['sky_subtract', 'U31',
   "天光发射线联合线性扣除（F-86）：已随 P3c 上线，但消费点在 S3 步 1 的 U-31（sky_handling='subtract'，API-4 线测量；CA-37 失败自动退回 mask，sky_subtraction 计入 not_in_budget）。本 S1/S2 测光面板不消费该开关"],
];

// ─── IA-11 参数记忆：sessionStorage（不存结果、绝不存上传谱数组） ───
function sessionKey() {
  if (S.sourceKind === 'catalog' && S.spectrumId != null) return 'specphot:' + S.spectrumId;
  if ((S.sourceKind === 'upload' || S.sourceKind === 'paste') && S.upload) {
    return 'specphot:up:' + String(S.upload.spec_hash || '').slice(0, 12);
  }
  return null;
}
function saveSession() {
  const k = sessionKey();
  if (!k) return;
  try {   // 配额异常即放弃（ST-12：不得静默截断 JSON，全量写入失败就丢弃本条）
    sessionStorage.setItem(k, JSON.stringify({
      params: S.params, manualRows: S.manualRows, selectedBands: S.selectedBands,
      maskRanges: S.maskRanges, anchorChecks: S.anchorChecks, diagFlags: S.diagFlags,
    }));
  } catch {}
}
function loadSession() {
  const k = sessionKey();
  if (!k) return;
  try {
    const j = JSON.parse(sessionStorage.getItem(k) || 'null');
    if (!j) return;
    if (j.params) S.params = { ...S.params, ...j.params };
    if (Array.isArray(j.manualRows)) S.manualRows = j.manualRows;
    if (Array.isArray(j.selectedBands)) S.selectedBands = j.selectedBands;
    if (Array.isArray(j.maskRanges)) S.maskRanges = j.maskRanges;
    if (j.anchorChecks) S.anchorChecks = j.anchorChecks;
    if (j.diagFlags) S.diagFlags = { ...S.diagFlags, ...j.diagFlags };
  } catch {}
}

// ─── 缺项清单（IA-16：列表呈现，不得只标红） ───
function missingList() {
  const out = [];
  if (!isAuthed()) out.push('未登录：解析上传件、银消查询与计算需要登录');
  const hasSpec = (S.sourceKind === 'catalog' && S.spectrumId != null) || !!S.upload;
  if (!hasSpec) out.push('缺谱：先选择一条库内谱，或上传 / 粘贴并解析成功');
  if (hasSpec && !S.selectedBands.length) out.push('缺波段：至少勾选 1 个波段');
  const conf = conflictBands(S);
  if (conf.size) out.push(`锚点冲突（CA-33）：同波段出现两行 ${[...conf].join('、')}，请选一条`);
  for (const [key, label] of [['ra', 'RA'], ['dec', 'Dec']]) {
    const v = String(S.metaForm[key] || '').trim();
    if (v && parseCoord(v, key === 'ra').err) out.push(`${label} 坐标无法解析（十进制度或 12:34:56.7）`);
  }
  if (S.params.mode === 'anchored') {
    const n = (S.spectrumInfo ? (S.spectrumInfo.n_catalog_anchors || 0) : 0) + S.manualRows.length;
    if (!n) out.push('anchored 需至少 1 个锚点（源表行或手加行）');
  }
  // U-53/IA-16（F-110③，P1b）：已知 errcol_verdict ≠ ok 而未确认 ⇒ 「计算」禁用
  // 并列进缺项清单（不得由服务端默默择一；auto 缺省仅在首次计算前允许）
  const verdict = knownErrcolVerdict();
  if (verdict && verdict !== 'ok' && !S.params.errcol_choice) {
    out.push(`误差列判定 ${verdict} ≠ ok：请在「误差列确认（U-53）」二选一后再计算`);
  }
  // U-22（§3.3 model 行，P2 2b）：model 定标需先有可比的 S2 拟合（F-33 单向耦合）
  if (S.params.mode === 'model') {
    const fit = bestComparableFit(S);
    if (!fit) out.push('model 定标需先在 S2 页签完成一次含可比拟合（无 CA-44）的连续谱计算');
  }
  return out;
}

// U-22：S1 model 定标消费的 S2 拟合（verdict.best 优先，须 comparable，F-91⑦/F-33）
function bestComparableFit(S) {
  const r = S.lastContinuum;
  if (!r || !Array.isArray(r.fits)) return null;
  const comp = r.fits.filter(f => f.comparable);
  return comp.find(f => f.model === (r.verdict && r.verdict.best)) || comp[0] || null;
}

// 已知的 errcol_verdict（不触发请求）：上次计算响应 > 上传/粘贴解析响应（API-7
// 的 P1b 新键）⇒ U-53 必须在算之前给出（W-37）；都无 ⇒ null（首次计算不受闸）。
function knownErrcolVerdict() {
  if (S.lastResponse && S.lastResponse.preprocess) return S.lastResponse.preprocess.errcol_verdict || null;
  if (S.upload && S.upload.errcol_verdict) return S.upload.errcol_verdict;
  return null;
}

// ─── U-05 波段默认勾选 ───
function defaultBands() {
  const usable = S.bandsMeta.filter(b => b.has_curve);
  const anchorBands = new Set(S.manualRows.map(r => r.band));
  let base = usable.filter(b => anchorBands.has(b.id));
  if (!base.length) {   // 锚点表为空：griz 集（Sloan），四档全缺退化为 λ 升序前 6
    const griz = ['g', 'r', 'i', 'z'].filter(b => usable.some(x => x.id === b));
    if (griz.length === 4) base = usable.filter(b => griz.includes(b.id));
  }
  if (!base.length) {
    base = usable.slice()
      .sort((a, b) => (a.wavelength - b.wavelength) || String(a.id).localeCompare(String(b.id)))
      .slice(0, 6);
  }
  return base.map(b => b.id);
}

// ─── 状态机与全区刷新 ───
function setStatus(st) { S.state = st; refreshChrome(); }
function dirty() {                     // IA-4：fresh 态改参数 ⇒ stale
  saveSession();
  if (S.state === 'fresh') S.state = 'stale';
  else if (S.state === 'idle' && (S.sourceKind === 'catalog' ? S.spectrumId != null : !!S.upload)) S.state = 'dirty';
  scheduleChrome();
}

// 状态变更同步落账，DOM 刷新推迟一拍：修「输入 blur → change → dirty() 同步重建
// 面板落在按钮 mousedown 与 mouseup 之间 ⇒ Chromium 对已分离节点不派发 click、
// 该次点击被吞」（真实用户改完值立刻点按钮的典型路径；终验复现，事件委托兜不住
// 未派发的 click）。状态先行、重绘异步，IA-4 语义不变。
let _chromeScheduled = false;
function scheduleChrome() {
  if (_chromeScheduled) return;
  _chromeScheduled = true;
  setTimeout(() => { _chromeScheduled = false; refreshChrome(); }, 0);
}
function refreshChrome() { renderBadges(); renderTabs(); renderBandCard(); renderRightPanel(); refreshPlot(); refreshAnchors(); refreshResults(); refreshComparison(); refreshContinuum(); refreshLines(); }

function refreshAnchors() { if (_els.anchors) renderAnchors(_els.anchors, ctx); }
function refreshResults() { if (_els.results) renderResults(_els.results, ctx); }
function refreshComparison() { if (_els.compare) renderComparison(_els.compare, ctx); }
function refreshContinuum() { if (_els.cont) renderContinuumResults(_els.cont, ctx); }
function refreshLines() { if (_els.lines) renderLinesResults(_els.lines, ctx); }

// ─── 口径徽章条（F-0i10，四字段与结果 JSON 同源） ───
function renderBadges() {
  const el = _els.badges;
  if (!el) return;
  const r = S.lastResponse;
  const grey = S.state === 'computing';
  const stale = S.state === 'stale' || S.state === 'dirty';
  const seg = (k, v, warn = '') =>
    `<span class="badge rounded-pill ${stale ? 'bg-warning text-dark' : 'bg-secondary'}"
        title="${escAttr(TXT.t1)}" style="${grey ? 'opacity:.5' : ''}">${k}: ${v}${warn}</span>`;
  const kind = r ? [...new Set(r.results.map(x => x.curve_kind))].join('/') : '—';
  const kindWarn = kind.includes('transmission') || kind.includes('unknown') ? '⚠' : '';
  const up = S.upload ? ` <span class="text-secondary small" title="来源标记（IA-17）">up:${esc(String(S.upload.spec_hash || '').slice(0, 8))}</span>` : '';
  el.innerHTML = `
    <div class="d-flex flex-wrap gap-2 align-items-center py-1 border-bottom">
      ${seg('模式', r ? esc(r.mode_effective) : '—')}
      ${seg('加权', r ? esc(r.results[0] && r.results[0].weighting || S.params.weighting) : esc(S.params.weighting))}
      ${seg('星等系', r ? esc(r.results[0] && r.results[0].mag_system || S.params.mag_system) : esc(S.params.mag_system))}
      ${seg('曲线', esc(kind), kindWarn)}
      ${up}
      <span class="ms-auto small text-secondary" style="${grey ? 'opacity:.5' : ''}">
        ${S.state === 'stale' || S.state === 'dirty' ? '参数已改动，结果已过期' : ''}</span>
    </div>
    <div class="small text-secondary py-1">${TXT.t27}：<span>${preprocessLine(r)}</span></div>
    ${rebinScreenLine(r)}`;
}
function preprocessLine(r) {   // TXT-27：恒在场，空操作写 none，不得整行消失
  if (!r) return '尚未计算（factor=none ／ 掩膜 none 段 ／ 误差列判定 none）';
  const p = r.preprocess || {};
  // F-108②：尾块规则 —— n%k≠0 丢弃尾块 ⇒ 摘要行写明丢弃 n mod k 个像元
  //（n = read_stats.n_points 与 k/factor、n_rebinned_pixels 均已回显 ⇒ 可复算）
  let dropped = '';
  if ((p.factor || 1) > 1 && p.n_rebinned_pixels != null) {
    const n = (r.read_stats && r.read_stats.n_points) != null
      ? Number(r.read_stats.n_points) : null;
    if (n != null) dropped = ` ／ 丢弃 ${n - p.factor * p.n_rebinned_pixels} 像元（n mod k）`;
  }
  return `factor=${p.factor ?? 'none'} ／ rebin_gain=${p.rebin_gain ?? 'none'} ／ `
    + `掩膜 ${(p.mask_ranges || []).length} 段（mask_hash=${esc(p.mask_hash || 'none')}）／ `
    + `误差列判定 ${esc(p.errcol_verdict || 'none')}（sigma_method=${esc(r.results[0] && r.results[0].sigma_method || 'none')}）／ `
    + `preprocess_hash=${esc(p.preprocess_hash || 'none')}${dropped}`;
}
// CA-49 同屏行（factor>1 时常驻）：rebin_gain / curve_nodes / px_per_fwhm_after
// 三键必须同屏（F-108⑤/U-49/CA-49 末句），并提示并排比较需同一 preprocess_hash
function rebinScreenLine(r) {
  const p = (r && r.preprocess) || {};
  if ((p.factor || 1) <= 1) return '';
  const nodes = r.results && r.results[0] && r.results[0].curve_nodes;
  const rhoNote = p.rebin_gain == null
    ? ' <span class="text-warning">无相关估计 ⇒ 收益不可声明（CA-49③）</span>' : '';
  return `<div class="small text-secondary py-1">合束回显（CA-49 三键同屏）：`
    + `rebin_gain=<b>${p.rebin_gain ?? 'null'}</b>${rhoNote} ／ curve_nodes=${nodes ?? 'none'} ／ `
    + `px_per_fwhm_after=${p.px_per_fwhm_after ?? 'null（无 R ⇒ 闸门无从判定，CA-47③）'} ／ `
    + `<span title="F-107③/F-29①">要并排比较两个因子，需要同一 preprocess_hash</span></div>`;
}

// ─── ② 波段选择（U-04 系统筛选 + U-05 chips） ───
function bandGroups() {
  const groups = new Map([['', '全部']]);
  for (const b of S.bandsMeta) {
    const g = String(b.description || '').trim().split(/[\s·/]+/)[0] || '其他';
    if (!groups.has(g)) groups.set(g, g);
  }
  return groups;
}
function renderBandCard() {
  const el = _els.bandCard;
  if (!el) return;
  if (!S.bandsMeta.length) {
    el.innerHTML = '<div class="card"><div class="card-body py-2 small text-secondary">波段清单加载中…</div></div>';
    return;
  }
  const full = S.selectedBands.length >= C_MAX_BANDS;
  const groups = [...bandGroups().entries()].map(([v, label]) =>
    `<option value="${escAttr(v)}" ${S.bandGroup === v ? 'selected' : ''}>${esc(label)}</option>`).join('');
  const chips = S.bandsMeta
    .filter(b => !S.bandGroup || String(b.description || '').startsWith(S.bandGroup))
    .map(b => {
      const on = S.selectedBands.includes(b.id);
      const dis = !b.has_curve || (full && !on);
      const title = b.has_curve
        ? `λ=${b.wavelength ?? '—'} Å · 曲线口径 ${esc(b.curve_kind || '未登记')}${b.has_curve ? '' : ''}`
        : '无透过率曲线，仅可单色近似';
      const dot = b.has_curve ? '' : '<i class="bi bi-slash-circle text-secondary ms-1"></i>';
      return `<label class="me-2 ${dis ? 'text-secondary' : ''}" title="${escAttr(title)}">
        <input type="checkbox" class="form-check-input mt-0 me-1 sp-band-chip" value="${escAttr(b.id)}"
          ${on ? 'checked' : ''} ${dis ? 'disabled' : ''}>${esc(b.id)}${dot}</label>`;
    }).join('');
  el.innerHTML = `
    <div class="card mb-3"><div class="card-body py-2">
      <div class="d-flex align-items-center mb-1">
        <strong class="me-2">② 波段选择</strong>
        <select class="form-select form-select-sm w-auto ms-auto sp-band-group">${groups}</select>
        <span class="small text-secondary ms-2">${S.selectedBands.length}/${C_MAX_BANDS}</span>
      </div>
      <div class="small">${chips || '<span class="text-secondary">该筛选下无波段</span>'}</div>
      <div class="form-check form-check-inline small mt-1" title="开启后无曲线波段可走 mono 单色近似；开启即必挂 CA-04">
        <input class="form-check-input sp-mono" type="checkbox" id="spAllowMono" ${S.params.allow_mono ? 'checked' : ''}>
        <label class="form-check-label" for="spAllowMono">单色回退（mono）<span class="text-danger">⚠</span></label>
      </div>
    </div></div>`;
  el.querySelectorAll('.sp-band-chip').forEach(cb => cb.addEventListener('change', () => {
    const id = cb.value;
    if (cb.checked) { if (!S.selectedBands.includes(id)) S.selectedBands.push(id); }
    else S.selectedBands = S.selectedBands.filter(x => x !== id);
    dirty();
  }));
  el.querySelector('.sp-band-group').addEventListener('change', e => { S.bandGroup = e.target.value; renderBandCard(); });
  el.querySelector('.sp-mono').addEventListener('change', e => { S.params.allow_mono = e.target.checked; dirty(); });
}

// ─── ③ 参数区 + ⑤ 动作按钮 + IA-16 缺项清单 ───
function radio(cls, name, val, cur, dis, label, title = '') {
  return `<div class="form-check form-check-inline" ${title ? `title="${escAttr(title)}"` : ''}>
    <input class="form-check-input ${cls}" type="radio" name="${name}" value="${val}"
      ${cur === val ? 'checked' : ''} ${dis ? 'disabled' : ''}>
    <label class="form-check-label${dis ? ' text-secondary' : ''}">${label}</label></div>`;
}

// ─── W-17：当前谱的流量口径（F-13 双键判定的前端镜像，计算后以响应 flux_kind 为准） ───
// 返回 'absolute' | 'uncalibrated' | 'contradictory' | 'unknown'（与后端 fluxcal.classify 同域）
function currentFluxKind() {
  const r = S.lastResponse;
  if (r && r.flux_kind) return r.flux_kind;                    // API-2 回显优先
  const ABS = ['erg/s/cm^2/angstrom'];                         // 与后端 fluxcal._ABS_UNITS 同值
  const norm = v => (v == null ? null : String(v).trim().toLowerCase());
  if (S.sourceKind === 'catalog' && S.spectrumInfo) {          // 双键一致才 absolute（etl 单键标签不可信）
    const ft = norm(S.spectrumInfo.flux_type), uf = norm(S.spectrumInfo.u_fluxes);
    if ((ft && ft !== 'absolute') || (uf && uf !== 'uncalibrated' && !ABS.includes(uf))) return 'contradictory';
    if (ft === 'absolute' && uf === 'uncalibrated') return 'contradictory';
    if (uf === 'uncalibrated') return 'uncalibrated';
    if (ft === 'absolute' && uf && ABS.includes(uf)) return 'absolute';
    return 'unknown';
  }
  if (S.upload) {                                              // 上传件：flux_unit 头键充当 u_fluxes
    const uf = norm(S.upload.flux_unit);
    return (uf && ABS.includes(uf)) ? 'absolute' : 'unknown';
  }
  return 'unknown';
}
// ─── U-53（F-110③）：误差列退化确认（仅 verdict ≠ ok 时出现；二选一） ────────
function errcolBlock(computing) {
  const verdict = knownErrcolVerdict();
  if (!verdict) return '';
  if (verdict === 'ok') {
    return '<div class="text-secondary mt-1">误差列确认（U-53）：errcol_verdict=ok，无需确认</div>';
  }
  const cur = S.params.errcol_choice;
  return `<div class="mt-1 sp-errcol">误差列判定 <b>${esc(verdict)}</b> ≠ ok
    （U-53 二选一，F-110③；缺确认 ⇒ 计算禁用，IA-16）：
    ${radio('sp-ec', 'spEc', 'proxy', cur, computing, '按代理误差重算（CA-47②）',
            '回落 F-54 二阶差分代理；该误差列不参与本次结果')}
    ${radio('sp-ec', 'spEc', 'as_provided', cur, computing, '坚持用该误差列（CA-47①）',
            "sigma_method='spec_err' 常驻；该列已被判定不可信")}
  </div>`;
}

function renderRightPanel() {
  const el = _els.right;
  if (!el) return;
  if (S.activeTab === 's2') { renderS2Panel(el, ctx); return; }   // P2 2b：S2 参数面板
  if (S.activeTab === 's3') { renderLinesPanel(el, ctx); return; }   // P3 切片 2：S3 三步向导
  const p = S.params;
  const r = S.lastResponse;
  const missing = missingList();
  const computing = S.state === 'computing';
  // W-9：429 倒计时剩余秒数（>0 = 服务忙，「计算」禁用）
  const busyLeft = S.busyUntilTs > Date.now() ? Math.ceil((S.busyUntilTs - Date.now()) / 1000) : 0;
  if (!busyLeft && S.busyUntilTs) { S.busyUntilTs = 0; S.busyNotice = null; }
  // W-17：flux_kind 非 absolute ⇒ direct 置灰 + tooltip 指向 CA-03 降级语义（TXT-12）
  const fluxKind = currentFluxKind();
  const directDis = computing || ['uncalibrated', 'contradictory', 'unknown'].includes(fluxKind);
  const directTip = 'CA-03/TXT-12：该谱无可用绝对流量口径，direct 会按 anchored/model 降级，故置灰';
  const anchorsN = (S.spectrumInfo ? (S.spectrumInfo.n_catalog_anchors || 0) : 0) + S.manualRows.length;
  // W-15②：窄屏（≤576px）参数抽屉；首次渲染按视口决定默认态（窄=收起），桌面恒展开
  if (S.paramOpen === undefined) S.paramOpen = window.innerWidth > 576;
  const computeDis = !!(missing.length || computing || busyLeft);
  el.innerHTML = `
    <div class="card mb-3"><div class="card-body py-2" id="spParams">
      <div class="d-flex align-items-center">
        <strong>③ 参数</strong>
        <button type="button" class="btn btn-sm btn-outline-secondary ms-auto d-sm-none sp-param-toggle"
          aria-expanded="${S.paramOpen ? 'true' : 'false'}" aria-controls="spParamBody"
          title="开合参数抽屉（W-15）">参数</button>
      </div>
      <div class="sp-param-body ${S.paramOpen ? '' : 'sp-closed'}" id="spParamBody">
      <div class="row g-2 small mt-1">
        <div class="col-6">
          <div>加权（U-07）：</div>
          ${['photon', 'energy'].map(v => radio('sp-w', 'spW', v, p.weighting, computing, v)).join('')}
        </div>
        <div class="col-6">
          <div>定标模式（U-08）：</div>
          ${['auto', 'anchored'].map(v => radio('sp-mode', 'spMode', v, p.mode, computing, v)).join('')}
          ${radio('sp-mode', 'spMode', 'direct', p.mode, directDis, 'direct', directDis ? directTip : '')}
          ${radio('sp-mode', 'spMode', 'model', p.mode, computing, 'model', 'model 定标：用 S2 连续谱模型曲线代替观测谱（§3.3 model 行，U-22）；需先在 S2 页签完成可比拟合')}
        </div>
        <div class="col-4">
          <div>星等制（U-29）：</div>
          ${radio('sp-ms', 'spMs', 'AB', p.mag_system, computing, 'AB')}
          ${radio('', '', 'ST', '', true, 'ST', 'ST 需库内没有的 Vega 参考谱（P1 禁用，F-48/E-08）')}
          ${radio('sp-ms', 'spMs', 'Vega', p.mag_system, computing, 'Vega')}
        </div>
        <div class="col-4">
          <div>误差处理（U-10）：</div>
          ${['auto', 'force_proxy'].map(v => radio('sp-ep', 'spEp', v, p.err_policy, computing, v)).join('')}
        </div>
        <div class="col-4">
          <label>时刻容差 dt_tol_d（U-11，天，留空 = 按类别表）：</label>
          <input type="number" min="0" step="any" class="form-control form-control-sm sp-dt" value="${escAttr(p.dt_tol_d)}" placeholder="自动" ${computing ? 'disabled' : ''}>
        </div>
      </div>
      <hr class="my-2">
      <div class="small text-secondary mt-1">诊断增强（U-44，F-81…F-86，默认全关；
        开关改动 ⇒ stale（IA-4），输出只进 diagnostics{} 子对象，不进主表必填列）：</div>
      <div class="row g-1 small mt-1">
        ${DIAG_META.map(([k, phase, tip]) => {
          const locked = phase !== 'P2c';
          const badge = phase === 'P2c' ? '' : phase === 'M6'
            ? ' <span class="badge bg-secondary">M-6</span>'
            : ' <span class="badge bg-secondary">U-31</span>';
          const on = !!(S.diagFlags || {})[k];
          return `<div class="col-6 col-md-4">
            <label class="form-check${locked ? ' text-secondary' : ''}" title="${escAttr(tip)}">
              <input type="checkbox" class="form-check-input sp-diag" data-k="${k}"
                ${on ? 'checked' : ''} ${locked || computing ? 'disabled' : ''}> ${k}${badge}
            </label></div>`;
        }).join('')}
      </div>
      <hr class="my-2">
      <div class="small">
        <strong>预处理（U-49…U-53）</strong>
        <div class="row g-2 mt-0">
          <div class="col-4">
            <div>合束因子（U-49）：</div>
            <select class="form-select form-select-sm sp-factor" ${computing ? 'disabled' : ''}
              title="合束（F-108，P2）：只加粗不减细的块内平均，面积守恒；目标采样 = 每分辨率元 2 点（C_REBIN_TARGET_PER_FWHM，F-108④）；改动 ⇒ stale（IA-4）。取值闭集 [1,2,3,4,6,8]（表外 E-14）">
              ${[1, 2, 3, 4, 6, 8].map(k => `<option value="${k}" ${Number(S.params.factor) === k ? 'selected' : ''}>${k}</option>`).join('')}
            </select>
            <div class="text-secondary" title="F-108④/CA-47③：全库现状 r_source='none'">无 R ⇒ 欠分辨闸门无从判定（CA-47③）</div>
          </div>
          <div class="col-3">
            <div>绘图平滑（U-51）：</div>
            <label class="form-check form-check-inline" title="display_smoothed（F-109）：只改图不改数——开启不转 stale、不进任何哈希、不进导出件（纯前端绘制）；开启时 TXT-26 常驻图注">
              <input type="checkbox" class="form-check-input sp-smooth-on" ${S.params.smooth_on ? 'checked' : ''}> 开启
            </label>
            <input type="number" min="1" max="7" step="1" class="form-control form-control-sm sp-smooth-px"
              value="${escAttr(String(S.params.smooth_px ?? 3))}" aria-label="平滑核长（全宽，像元）"
              title="核长（全宽）C_SMOOTH_BOX_PX=3 默认，上限 7（越界 E-14）" ${computing ? 'disabled' : ''}>
          </div>
          <div class="col-5">
            <div>掩膜段手输（U-50，Å）：</div>
            <div class="d-flex gap-1">
              <input type="number" step="any" class="form-control form-control-sm sp-mask-lo"
                placeholder="lo" aria-label="掩膜段起点 Å" ${computing ? 'disabled' : ''}>
              <input type="number" step="any" class="form-control form-control-sm sp-mask-hi"
                placeholder="hi" aria-label="掩膜段终点 Å" ${computing ? 'disabled' : ''}>
              <button type="button" class="btn btn-outline-secondary btn-sm py-0 sp-mask-add"
                ${computing ? 'disabled' : ''}>加入</button>
            </div>
            <div class="text-secondary">与 U-13 框选同一个布尔数组；lo&lt;hi 且须与覆盖重叠（V-13），总段数 ≤ ${C_MAX_EXCLUDE}</div>
          </div>
        </div>
        ${errcolBlock(computing)}
      </div>
      <hr class="my-2">
      <div class="small">
        <strong>⑤ 动作</strong>
        <div class="d-flex flex-wrap gap-2 align-items-center mt-1">
          <button class="btn btn-primary btn-sm sp-compute" ${computeDis ? 'disabled' : ''}
            aria-describedby="spComputeError" title="${escAttr(TXT.t13)}">
            <span class="spinner-border spinner-border-sm d-none sp-spin"></span> 计算</button>
          <button class="btn btn-outline-secondary btn-sm sp-cancel" style="display:${computing ? '' : 'none'}">取消</button>
          <button class="btn btn-outline-secondary btn-sm sp-reset" ${computing ? 'disabled' : ''}>重置为默认</button>
          <button class="btn btn-outline-secondary btn-sm sp-expcsv" ${!r || computing ? 'disabled' : ''}
            title="U-17/F-44：§4.3 列序 + TXT-11 头注释；无结果时禁用">导出 CSV</button>
          <button class="btn btn-outline-secondary btn-sm sp-expjson" ${!r || computing ? 'disabled' : ''}
            title="U-17/F-44：API-2 响应原文">导出 JSON</button>
          <span class="small text-secondary">${esc(TXT.t13)}</span>
        </div>
        <div class="mt-1 text-warning small" id="spBusyNotice">${busyLeft
          ? `服务忙，${busyLeft}s 后可重试（429）` : esc(S.busyNotice || '')}</div>
        <div class="mt-1 text-warning small">${esc(S.featureNotice || '')}</div>
        <div class="mt-1 text-danger small" id="spComputeError" role="alert">${esc(S.errorMsg || '')}</div>
      </div>
      <div class="small mt-2 ${missing.length ? 'text-warning' : 'text-success'}">
        缺项清单（IA-16）：${missing.length ? '<ul class="mb-0">' + missing.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>' : '无，可以计算'}
      </div>
      </div><!-- /sp-param-body（W-15② 窄屏抽屉） -->
    </div></div>`;
  // 事件
  el.querySelectorAll('.sp-w').forEach(r => r.addEventListener('change', () => { S.params.weighting = r.value; dirty(); }));
  el.querySelectorAll('.sp-mode').forEach(r => r.addEventListener('change', () => { S.params.mode = r.value; dirty(); }));
  el.querySelectorAll('.sp-ms').forEach(r => r.addEventListener('change', () => { S.params.mag_system = r.value; dirty(); }));
  el.querySelectorAll('.sp-ep').forEach(r => r.addEventListener('change', () => { S.params.err_policy = r.value; dirty(); }));
  el.querySelector('.sp-dt').addEventListener('change', e => { S.params.dt_tol_d = e.target.value; dirty(); });
  // U-44（P2c 前三项解禁）：诊断开关改动 ⇒ dirty（IA-4 fresh→stale）；随请求体
  // diagnostics 只携带勾选项（Q-27 只收布尔，全关 = {} 与基线请求同形）。
  // 后三项渲染但禁用（P3c，E-15/A-3）⇒ 无事件路径。
  el.querySelectorAll('.sp-diag').forEach(cb => cb.addEventListener('change', () => {
    if (cb.dataset.k in S.diagFlags) {
      S.diagFlags[cb.dataset.k] = cb.checked;
      dirty(); saveSession();
    }
  }));
  // U-49（P2 2c 解禁）：合束因子改动 ⇒ dirty（IA-4 fresh→stale），随请求体 preprocess.factor
  const factorSel = el.querySelector('.sp-factor');
  if (factorSel) factorSel.addEventListener('change', e => {
    S.params.factor = Number(e.target.value); dirty();
  });
  // U-51（P2 2c 解禁）：绘图平滑只改图不改数 —— 不 dirty、不转 stale（F-109⑤/W-38），
  // 只重绘本地图层（TXT-26 图注随开合出现）；核长越界即时提示（服务端 Q-33 仍校验）
  const smoothOn = el.querySelector('.sp-smooth-on');
  const smoothPx = el.querySelector('.sp-smooth-px');
  if (smoothOn) smoothOn.addEventListener('change', () => {
    S.params.smooth_on = smoothOn.checked; saveSession(); refreshPlot();
  });
  if (smoothPx) smoothPx.addEventListener('change', () => {
    const v = Number(smoothPx.value);
    if (smoothPx.value !== '' && isFinite(v) && v >= 1 && v <= 7 && Number.isInteger(v)) {
      S.params.smooth_px = v; S.errorMsg = null;
    } else {
      S.errorMsg = 'U-51：平滑核长须为 [1, 7] 内整数（Q-33，越界 E-14）';
    }
    saveSession(); refreshPlot(); renderRightPanel();
  });
  // U-50：手输掩膜段 → S.maskRanges（与 U-13 同一个布尔数组；V-13 逐段过滤在
  // 服务端，前端只做 lo<hi 形态检查）；并入 mask_hash 的斜纹随 refreshPlot 出现
  const maskAdd = el.querySelector('.sp-mask-add');
  if (maskAdd) maskAdd.addEventListener('click', () => {
    const loS = el.querySelector('.sp-mask-lo').value, hiS = el.querySelector('.sp-mask-hi').value;
    const lo = Number(loS), hi = Number(hiS);
    if (loS === '' || hiS === '' || !isFinite(lo) || !isFinite(hi) || lo >= hi) {
      S.errorMsg = 'U-50：掩膜段须为数字且 lo < hi'; renderRightPanel(); return;
    }
    if (S.maskRanges.length >= C_MAX_EXCLUDE) {   // Q-2/F-107⑤（服务端并集后同闸）
      S.errorMsg = `掩膜段数已达上限 ${C_MAX_EXCLUDE}`; renderRightPanel(); return;
    }
    S.errorMsg = null;
    S.maskRanges.push([lo, hi]);
    dirty();
  });
  // U-53：二选一确认（写 params.errcol_choice，随请求体 preprocess.errcol_choice）
  el.querySelectorAll('.sp-ec').forEach(r => r.addEventListener('change', () => {
    S.params.errcol_choice = r.value; dirty();
  }));
  el.querySelector('.sp-compute').addEventListener('click', compute);
  el.querySelector('.sp-cancel').addEventListener('click', () => { if (S.abortCtrl) S.abortCtrl.abort(); });
  el.querySelector('.sp-reset').addEventListener('click', resetAll);
  el.querySelector('.sp-expcsv').addEventListener('click', () => exportCsv(S));   // U-17/F-44
  el.querySelector('.sp-expjson').addEventListener('click', () => exportJson(S));
  const ptoggle = el.querySelector('.sp-param-toggle');   // W-15②：窄屏抽屉开合
  if (ptoggle) ptoggle.addEventListener('click', () => { S.paramOpen = !S.paramOpen; renderRightPanel(); });
}

// ─── U-16 重置为默认：清当前谱的 sessionStorage，参数与元数据回默认 ───
async function resetAll() {
  const k = sessionKey();
  if (k) { try { sessionStorage.removeItem(k); } catch {} }
  S.params = { weighting: 'photon', mode: 'auto', mag_system: 'AB', err_policy: 'auto',
               dt_tol_d: '', allow_mono: false, mw_correct: true, ebv_override: '',
               errcol_choice: '', factor: 1, smooth_on: false, smooth_px: 3 };
  S.manualRows = []; S.maskRanges = []; S.anchorChecks = {}; S.lastResponse = null;
  S.lastContinuum = null;
  S.contParams = { models: ['pl', 'pl_dust'], host_ext_mode: 'fit', ebv: '',
                   law: 'smc', rv: '', rv_source: '', poly_order: 3 };
  S.lastLines = null; S.lineStep = 1; S.lineStale = false;
  S.lineParams = { species: '', lambda_rest_aa: '', id_table: '', line_kind: '',
                   halfwidth: 40, baseline_order: 1, profile: 'gauss1', R: '',
                   line_frame: '', sky_handling: 'mask', n_boot: 200, err_seed: '',
                   z: '' };
  S.featureNotice = S.busyNotice = S.errorMsg = null;
  S.state = 'idle';
  if (S.sourceKind === 'catalog' && S.spectrumId != null) await loadCatalogSpectrum(S.spectrumId, true);
  else { S.selectedBands = []; }
  refreshChrome();
}

// ─── 换谱 / 换来源（IA-9 / IA-19）：回 idle、清结果，上传件出内存即弃 ───
function switchSource(kind, opts = {}) {
  const prev = S.sourceKind;
  S.sourceKind = kind;
  if (prev && prev !== kind && (prev === 'upload' || prev === 'paste')) {
    S.upload = null;   // 上传件立即从内存丢弃（TXT-18：未入库）
    S.parseError = null;
  }
  if (!opts.keepResponse) {
    S.lastResponse = null; S.lastContinuum = null; S.lastLines = null; S.state = 'idle';
    // P3-D（U-53 粘滞）：errcol_choice 是对当前谱误差列判定的二选一确认，判定
    // 上下文（lastResponse / 上传解析件的 errcol_verdict）随换谱重建 ⇒ 旧确认
    // 不得粘连到新谱（verdict=ok 时携带会得到服务端 E-14 errcol_choice）。置于
    // loadSession 之前：同 spec_hash 重传仍可恢复自身一致的确认，异谱则干净起步。
    S.params.errcol_choice = '';
  }
  S.featureNotice = S.busyNotice = S.errorMsg = null;
  if (!opts.keepSelection) {
    S.spectrumId = null; S.tid = null; S.spectrumInfo = null; S.selectedBands = [];
    S.manualRows = []; S.maskRanges = []; S.anchorChecks = [];
    S.metaForm = { ra: '', dec: '', z: '', mjd: '', category: 'other' };
    S.uploadFrame = 'vacuum';
    S.uploadFrameDeclared = false;          // W-30：换来源 = 框架声明作废，回默认 vacuum
    S.metaUserEdits = {}; S.zStaleNote = false;   // W-28：来源重建，手改标记与静止系提示清零
    S.ebv = { value: null, rv: 3.1, law: 'P92', available: null };
  }
}

// ─── 库内谱装载（API-1 ?spectrum_id=…；规范化在服务端完成，不调 API-7） ───
async function loadCatalogSpectrum(sid, isReset = false) {
  try {
    const meta = await api('GET', `/specphot/meta?spectrum_id=${encodeURIComponent(sid)}`);
    S.bandsMeta = meta.bands || S.bandsMeta;
    S.curveCoverage = meta.curve_coverage || null;
    S.spectrumInfo = meta.spectrum || null;
    if (meta.spectrum) {
      S.spectrumId = meta.spectrum.spectrum_id;
      S.tid = meta.spectrum.tid;
      const m = meta.spectrum.meta || {};
      S.metaForm = {
        ra: m.ra_deg != null ? String(m.ra_deg) : '',
        dec: m.dec_deg != null ? String(m.dec_deg) : '',
        z: m.z != null ? String(m.z) : '',
        mjd: m.mjd != null ? String(m.mjd) : '',
        category: m.category || 'other',
      };
    }
    if (!isReset) {
      S.selectedBands = defaultBands();
      loadSession();   // IA-11：同谱恢复参数与手加锚点行
    }
  } catch (e) {
    S.errorMsg = `装载库内谱失败：${e.message}`;
  }
}

// ─── 上传 / 粘贴解析成功后的装配（upload 对象只含本片需要的内存数组，F-0i6） ───
function adoptUpload(parsed, kind, textLen) {
  const lam = parsed.lam_aa || [];
  switchSource(kind, { keepSelection: false });
  S.upload = {
    spec_hash: parsed.spec_hash, n_points: parsed.n_points,
    lam_aa: lam, flux: parsed.flux || [], flux_err: parsed.flux_err || null,
    columns: parsed.columns, flux_unit: parsed.flux_unit,
    flux_unit_assumed: parsed.flux_unit_assumed,
    lam_min: lam.length ? Math.min(...lam) : null,
    lam_max: lam.length ? Math.max(...lam) : null,
    source: kind, bytes: textLen,
    // W-30②：API-7 parse 响应的 warnings 不再丢弃，保留进来源条徽章条
    warnings: Array.isArray(parsed.warnings) ? parsed.warnings : [],
    meta_provenance: parsed.meta_provenance || null,   // W-28③：上传件元数据来源
    // P1b（W-37）：解析即带 errcol_verdict ⇒ U-53 必须在算之前给出（IA-16 闸）
    errcol_verdict: parsed.errcol_verdict || null,
  };
  S.parseError = null;
  loadSession();          // 同一 spec_hash 恢复已填参数（F-80⑤c）
  S.selectedBands = S.selectedBands.length ? S.selectedBands : defaultBands();
}

// ─── API-2 计算（T3：同步；IA-5/IA-6/IA-10） ───
async function compute() {
  if (missingList().length || S.state === 'computing') return;
  if (S.abortCtrl) S.abortCtrl.abort();          // IA-10：在途请求唯一
  S.abortCtrl = new AbortController();
  const signal = S.abortCtrl.signal;
  S.state = 'computing'; S.busyNotice = S.errorMsg = S.featureNotice = null;
  refreshChrome();
  const spin = document.querySelector('.sp-spin');   // IA-5：< 300 ms 不显示 spinner（防闪烁）
  let spinTimer = spin ? setTimeout(() => spin.classList.remove('d-none'), 300) : null;
  try {
    const body = buildPhotometryRequest(ctx);
    const resp = await spPost('/specphot/photometry', body, signal);
    S.lastResponse = resp;
    S.state = 'fresh';                            // IA-6：徽章条 + 结果表 + 告警区同次渲染
  } catch (e) {
    if (e.name === 'AbortError') { S.state = 'dirty'; S.busyNotice = '已取消'; }
    else if (e.code === 'feature_disabled') {     // 501：UI 上表现为对应功能禁用，不弹错
      S.featureNotice = `${e.message}`;
      S.state = 'dirty';
    } else if (e.status === 429) {
      // W-9/E-12：消费响应 retry_after_s（缺省 2s），「计算」禁用并秒级倒计时，到 0 自动恢复
      const secs = Math.max(1, Number((e.payload || {}).retry_after_s) || 2);
      S.busyUntilTs = Date.now() + secs * 1000;
      S.busyNotice = null; S.state = 'dirty';
      startBusyCountdown();
    } else { S.errorMsg = e.message; S.state = 'error'; }
  } finally {
    if (spinTimer) clearTimeout(spinTimer);
    if (spin) spin.classList.add('d-none');
    S.abortCtrl = null;
    refreshChrome();
  }
}

// ─── W-9：429 秒级倒计时——每秒重画右栏（按钮禁用态 + 「服务忙，Ns 后可重试」），到 0 自动恢复 ───
function startBusyCountdown() {
  if (S.busyTimer) clearInterval(S.busyTimer);
  S.busyTimer = setInterval(() => {
    if (S.busyUntilTs <= Date.now()) {
      clearInterval(S.busyTimer); S.busyTimer = null; S.busyUntilTs = 0;
    }
    renderRightPanel();   // 剩余秒数在按钮禁用态与提示行中回显
  }, 1000);
}

// ─── 页签（U-03） ───
function renderTabs() {
  const el = _els.tabs;
  if (!el) return;
  const anchN = ((S.lastResponse || {}).anchor_rows || []).length + S.manualRows.length;
  const cmpDis = anchN < 1;
  const item = (id, label, extra = '') =>
    `<li class="nav-item"><a class="nav-link sp-tab ${S.activeTab === id ? 'active' : ''}" href="javascript:void(0)"
        data-tab="${id}" ${extra}><i class="bi bi-card-list"></i> ${label}</a></li>`;
  el.innerHTML = `<ul class="nav nav-tabs mt-3">
    ${item('s1', 'S1 合成测光')}
    ${item('cmp', '对照面板', cmpDis ? `title="需 ≥1 个锚点才能对照（U-18）" data-disabled="1" style="pointer-events:auto;opacity:.5"` : '')}
    ${item('s2', 'S2 连续谱拟合')}
    ${item('s3', 'S3 谱线测量')}
  </ul>`;
  el.querySelectorAll('.sp-tab').forEach(a => a.addEventListener('click', () => {
    if (a.dataset.disabled) return;   // 未到期页签禁用（E-15）
    S.activeTab = a.dataset.tab;
    layoutPanes(); renderTabs();
  }));
}
function layoutPanes() {
  if (!_els.main || !_els.compare) return;
  // P1-A：S2/S3 的参数面板渲染在 #spRight、谱预览在 #spPlot，二者都在 #spMain 内
  // ⇒ #spMain 只能在 cmp 页签整体隐藏；s1 专属窗格（#spResults/#spAnchors/
  // #spBandCard——波段选择只被 S1 测光消费）在 s2/s3 收起，右栏与谱预览保留
  // 可见（与 S2 空态文案「在右侧选模型」一致）。W-15 窄屏抽屉与 S1 行为不变。
  _els.main.style.display = S.activeTab === 'cmp' ? 'none' : '';
  for (const s1Pane of [_els.results, _els.anchors, _els.bandCard]) {
    if (s1Pane) s1Pane.style.display = S.activeTab === 's1' ? '' : 'none';
  }
  _els.compare.style.display = S.activeTab === 'cmp' ? '' : 'none';
  if (_els.cont) _els.cont.style.display = S.activeTab === 's2' ? '' : 'none';
  if (_els.lines) _els.lines.style.display = S.activeTab === 's3' ? '' : 'none';
  if (S.activeTab === 'cmp') refreshComparison();
  if (S.activeTab === 's2') { renderRightPanel(); refreshContinuum(); }
  if (S.activeTab === 's3') { renderRightPanel(); refreshLines(); }
}

// ─── 页头（U-43 回到该源详情 + 标题） ───
function renderHeader() {
  const el = _els.header;
  const back = (S.sourceKind === 'catalog' && S.tid)
    ? `<a class="btn btn-sm btn-outline-secondary" href="#/transient/${escAttr(S.tid)}" title="回到该源详情（不承诺停留在某个 tab，F-0i4）"><i class="bi bi-box-arrow-up-left"></i> 回到 #${escAttr(S.tid)} 详情</a>` : '';
  el.innerHTML = `
    <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2">
      <h5 class="mb-0"><i class="bi bi-rulers"></i> 光谱 × 滤光片
        <span class="small text-secondary" title="${escAttr(TXT.t13)}">S1 合成测光</span></h5>
      <div class="d-flex gap-2">${back}</div>
    </div>`;
}

// ─── ① 谱图（specplot.js：自建 canvas 图 + 通带叠加 + U-13 掩膜框选，随状态机重绘） ───
function refreshPlot() {
  if (!_els.plot) return;
  renderSpecPlot(_els.plot, ctx);
  // F-89①/U-47：曲线 A/B 双谱叠加（U-47 开且 host_ext_mode≠off 时生效；
  // off/换谱/清结果 ⇒ 传 null 清除叠加，不留孤立旧 B）
  attachCurveOverlay(_els.plot, S.lastContinuum);
}

// ─── 组装外壳 + 挂载 ───
const ctx = {
  S, spPost, isAuthed,
  dirty, saveSession, loadSession,
  setStatus, refreshChrome, refreshAnchors, refreshResults,
  switchSource, adoptUpload, loadCatalogSpectrum, defaultBands,
  exportJson,   // S2 结果卡 JSON 导出（U-17/F-44，continuum_ui 消费）
  setBandsMeta(list, cov) {
    S.bandsMeta = list || []; S.curveCoverage = cov || null;
    renderBandCard();
    refreshResults();   // W-18：curve_coverage（with_curve=0）到达后空态契约需重渲染
  },
};

// ─── W-14：Esc 键——按优先级收起：①窄屏参数抽屉 ②展开的诊断卡 ③Notice 提示条 ───
// 宿主无全局 keydown 监听（已核对 app.js / pages/*），不干扰宿主快捷键；不命中任何目标时不 preventDefault。
function onEsc(e) {
  if (e.key !== 'Escape') return;
  if (S.paramOpen && window.innerWidth <= 576) {          // ① 关参数抽屉（W-15②）
    S.paramOpen = false; renderRightPanel(); e.preventDefault(); return;
  }
  const open = _root ? [..._root.querySelectorAll('.sp-detail')] : [];   // ② 收起展开的诊断卡
  const visible = open.find(d => d.style.display !== 'none');
  if (visible) { visible.style.display = 'none'; e.preventDefault(); return; }
  if (S.busyNotice || S.featureNotice || S.errorMsg) {    // ③ 清 Notice
    S.busyNotice = S.featureNotice = S.errorMsg = null;
    renderRightPanel(); e.preventDefault();
  }
}

export function mountWorkbench(root, spectrumId) {
  _root = root;
  ensureStyles();   // W-15①/②：注入首列冻结与参数抽屉样式（幂等）
  if (S.paramOpen === undefined) S.paramOpen = window.innerWidth > 576;   // W-15②：窄屏默认收起
  root.innerHTML = `
    <div id="spHeader"></div>
    <div id="spSource"></div>
    <div id="spBadges"></div>
    <div class="row g-3" id="spMain">
      <div class="col-lg-7">
        <div id="spPlot"></div>
        <div id="spResults"></div>
        <div id="spAnchors"></div>
      </div>
      <div class="col-lg-5">
        <div id="spBandCard"></div>
        <div id="spRight"></div>
      </div>
    </div>
    <div id="spCompare" style="display:none"></div>
    <div id="spCont" style="display:none"></div>
    <div id="spLines" style="display:none"></div>
    <div id="spTabs"></div>`;
  _els = {
    header: root.querySelector('#spHeader'), source: root.querySelector('#spSource'),
    badges: root.querySelector('#spBadges'), main: root.querySelector('#spMain'),
    plot: root.querySelector('#spPlot'), results: root.querySelector('#spResults'),
    anchors: root.querySelector('#spAnchors'), bandCard: root.querySelector('#spBandCard'),
    right: root.querySelector('#spRight'), compare: root.querySelector('#spCompare'),
    cont: root.querySelector('#spCont'), lines: root.querySelector('#spLines'),
    tabs: root.querySelector('#spTabs'),
  };
  renderHeader();   // 谱图占位由 refreshChrome→refreshPlot→renderSpecPlot 提供
  document.addEventListener('keydown', onEsc);   // W-14：Esc 走完键盘主流程（卸载时解除）
  renderSourcePanel(_els.source, ctx);
  // API-1 全量波段清单（IA-2：首次展开即取回并缓存于内存，不让用户点了才报错）
  api('GET', '/specphot/meta').then(meta => {
    ctx.setBandsMeta(meta.bands, meta.curve_coverage);
  }).catch(() => ctx.setBandsMeta([], null));
  // 带参直入 = 装载该库内谱（IA-1：装载 ≠ 计算，进页即 idle，不自动计算）
  if (spectrumId != null) {
    S.sourceKind = 'catalog';
    loadCatalogSpectrum(spectrumId).then(() => { refreshChrome(); renderSourcePanel(_els.source, ctx); renderTabs(); layoutPanes(); });
  }
  refreshChrome(); renderTabs(); layoutPanes();
}

export function unmountWorkbench() {
  document.removeEventListener('keydown', onEsc);   // W-14：解除全局监听
  if (S.busyTimer) { clearInterval(S.busyTimer); S.busyTimer = null; }   // W-9：停倒计时
  if (S.spinnerTimer) { clearTimeout(S.spinnerTimer); S.spinnerTimer = null; }
  if (S.abortCtrl) { try { S.abortCtrl.abort(); } catch {} S.abortCtrl = null; }
  _root = null; _els = {};   // DOM 随宿主路由卸载；S（含上传数组）按 F-0i8 留在内存
}
