// === 模板层（#/compare 叠加 K 改正预测曲线） ===
// 设计文档 02：U-02..U-09 / F-05' / F-20 / F-25 / F-46 / IA-9 / IA-14/15
//
// 纪律（ST-9）：前端零天文算术。本模块唯一的换算是
//   - mag_AB → mJy：bands.js 既有 magABtoMJy（mJyToMagAB 的既有反算，非新口径）；
//   - 「静止系」勾选时 x 除以本条曲线自己的 (1+z_curve)（设计决策：各自曲线各自的 z）。
// 横轴零点平移由服务端直接给出（points.time_rel_s），前端不做 MJD 运算。
//
// 状态保持在模块级（页面内不随 render() 丢，主题切换 location.reload() 才清，IA-6）。
import { getTmplibTemplates, predictTmplib, exportTmplib, showToast } from '../api.js';
import { parseRefEpoch } from './detail_lcchart.js';
import { sortBandsByFreq, magABtoMJy } from '../bands.js';
import { chartColors } from '../theme.js';
import { esc } from '../utils.js';

// ─── 文案（TXT-*，集中一处，前端只按 code/kind 分支，不匹配 message —— E-30） ───
export const TXT = {
  prediction: '虚线为预测：把模板当作谱模型搬到指定 z，不是对某个源实测光变的插值。',
  mono: 'K 改正按单色（delta 波段）近似，与波段积分不可互换；本条曲线不得被引用为带积分 K 改正结果。',
  outOfCoverage: '该波段是此面的边缘波段：视星等 m 可给，K 与 M 按定义无法给。这不是程序错误。',
  clipped: (n) => `${n} 个请求时刻超出模板适用窗，已剔除不画（引擎对此类请求整次拒绝，此处按设计改为逐点裁剪）。`,
  refUser: '本条曲线的横轴零点 = 用户设定的基准时刻，不是该模板的 T0；同图其它曲线的零点各自独立设定。',
  firstRow: '本模板的 t=0 是表内最早一行，不是事件时刻：与同图其它曲线的 T0 不是同一瞬间。可用「基准时刻」填入一个 epoch 覆盖；不确定填什么就别填，改比形状。',
  restFrame: '本模板原始表已声明为静止系时间；引擎的入参/出参一律观测系，本条曲线与其它曲线时间轴口径一致，无需换算。',
  muNote: '本图绝对星等来自两个不同的距离：模板曲线的 μ 由引擎给出，库内源的 μ 来自本库 gext_distmod（=Planck18.distmod(红移)）。当 Δμ 超过 0.15 mag 时，两条曲线的相对高度主要反映距离口径之差，不是天体性质。',
  muBig: 'Δμ 超过引擎色标 cap 0.15 mag',
  muNone: '该模板在库里没有可用对应源，Δμ 无对照（不是 0）。',
  engineDown: 'K 改正引擎当前不可用。其余对比功能不受影响。',
};

const MAX_ROWS = 8;  // C_MAX_CURVES / ST-3

// ─── 模块级状态 ───
let _rows = [];        // {uid, template_id, band, z, mode, refRaw, refMJD, curve, error, reqId}
let _templates = null; // API-2 列表缓存（stale 的置灰，U-03）
let _templatesFailed = false;
let _ctx = { isStale: () => false, onChange: () => {} };
let _uid = 0;

export function tplReset() { _rows = []; }

export function tplHasCustomRef() { return _rows.some(r => r.refMJD != null); }

// ─── 面板 HTML 骨架（挂载点本身由 compare.js 提供） ───
export function tplPanelHTML() {
  return `
    <div class="d-flex justify-content-between align-items-center px-3 py-1">
      <span class="small text-secondary" title="${esc(TXT.prediction)}">叠加模板（引擎预测曲线，虚线 ⌁）ⓘ</span>
      <span class="d-flex gap-1">
        <button class="btn btn-sm btn-outline-primary py-0" id="tplAdd">＋ 添加模板（≤${MAX_ROWS}）</button>
        <button class="btn btn-sm btn-outline-secondary py-0" id="tplClear" style="display:none">清空</button>
        <button class="btn btn-sm btn-outline-secondary py-0" id="tplExpCsv" title="API-9：当前模板曲线 + 已选源的服务端导出（CSV）">下载 CSV</button>
        <button class="btn btn-sm btn-outline-secondary py-0" id="tplExpJson" title="API-9：与 CSV 同批数据的 JSON 渲染（F-56）">下载 JSON</button>
        <button class="btn btn-sm btn-outline-secondary py-0" id="tplCopyImg" title="复制当前对比图为 PNG（剪贴板，失败回退下载）">复制图</button>
      </span>
    </div>
    <div id="tplRows"></div>`;
}

export function tplMuCardHTML() {
  return `
    <div class="card-header py-1">模板口径卡（Δμ 披露）</div>
    <div class="card-body py-2 small" id="tplMuBody"></div>
    <div class="card-footer py-1 small text-secondary" id="tplMuNote" style="display:none">${esc(TXT.muNote)}</div>`;
}

// ─── 行渲染 ───
function _rowHTML(r) {
  const t = (_templates || []).find(x => x.id === r.template_id);
  const bands = sortBandsByFreq((t && t.bands) || []);
  const modes = (t && t.modes) || {};
  const opts = (_templates || []).map(x =>
    `<option value="${esc(x.id)}" ${x.id === r.template_id ? 'selected' : ''}
      ${x.state === 'stale' ? 'disabled' : ''}>${esc(x.id)}${x.state === 'stale' ? '（需重建）' : ''}</option>`).join('');
  const bandOpts = bands.map(b =>
    `<option value="${esc(b)}" ${b === r.band ? 'selected' : ''}>${esc(b)}${modes[b] === 'mono' ? '（mono）' : ''}</option>`).join('');
  return `
    <div class="tpl-row d-flex flex-wrap align-items-center gap-1 px-3 py-1 border-top" data-uid="${r.uid}">
      <select class="form-select form-select-sm tpl-tmpl" style="width:auto" title="模板">${opts}</select>
      <select class="form-select form-select-sm tpl-band" style="width:auto" title="观测波段（该面真有节点的波段）">${bandOpts}</select>
      <input type="text" class="form-control form-control-sm tpl-z" style="width:84px" value="${r.z ?? ''}" title="红移（默认模板自身 z）" placeholder="z">
      <select class="form-select form-select-sm tpl-mode" style="width:auto" title="auto：有曲线走波段积分，无曲线降级单色">
        ${['auto', 'band', 'mono'].map(m => `<option ${m === r.mode ? 'selected' : ''}>${m}</option>`).join('')}
      </select>
      <input type="text" class="form-control form-control-sm tpl-ref-epoch cmp-ref-epoch" style="width:150px"
             value="${esc(r.refRaw || '')}" placeholder="留空=模板T0；MJD 或 UTC"
             title="${esc(_refTitle(r))}">
      <span class="tpl-badges small">${_badgesHTML(r)}</span>
      <button class="btn btn-sm btn-outline-danger py-0 tpl-del" title="移除该模板曲线">✕</button>
    </div>
    <div class="tpl-err small text-danger px-3 ${r.error ? '' : 'd-none'}" data-uid="${r.uid}">${esc(r.error || '')}</div>`;
}

function _refTitle(r) {
  const c = r.curve;
  if (c && c.time_origin) {
    const to = c.time_origin;
    if (to.suggested_mjd != null)
      return `建议零点 MJD ${to.suggested_mjd}（${to.suggested_source || '模板声明'}）；建议值不代填`;
  }
  return '留空=该模板自己的 T0；MJD 数字或 UTC';
}

function _badgesHTML(r) {
  const out = [];
  const c = r.curve;
  if (r.error) return '<span class="text-danger">✕ 被拒</span>';
  if (!c) return '<span class="text-secondary">…</span>';
  if (c.mode === 'mono') out.push('<span class="badge text-bg-secondary" title="' + esc(TXT.mono) + '">mono</span>');
  if (c.time_origin && c.time_origin.kind === 'table-first-row') {
    const off = c.time_origin.offset_vs_catalog_t0_days;
    out.push('<span class="badge text-bg-warning" title="' + esc(TXT.firstRow) +
      `">⚠零点=表首行${off != null ? `（差 ${off} d）` : ''}</span>`);
  }
  if (r.refMJD != null) out.push('<span class="badge text-bg-info" title="' + esc(TXT.refUser) + '">基准≠T0</span>');
  if (c.domain && c.domain.state === 'extrapolated')
    out.push('<span class="badge text-bg-warning">外推/降级</span>');
  if (c.mu) {
    if (c.mu.ok) {
      const big = c.mu.alert === 'CA-13';
      out.push(`<span class="badge ${big ? 'text-bg-danger' : 'text-bg-light border'}" title="${esc(c.mu.cause + '：' + (c.mu.detail || ''))}${big ? '；' + esc(TXT.muBig) : ''}">Δμ ${c.mu.delta > 0 ? '+' : ''}${c.mu.delta}</span>`);
    } else {
      out.push(`<span class="badge text-bg-light border" title="${esc(c.mu.message || TXT.muNone)}">Δμ 无对照</span>`);
    }
  }
  return out.join(' ');
}

// ─── 取数 ───
async function _fetchRow(r) {
  const reqId = ++r.reqId;
  r.error = null;
  _renderRows();
  try {
    const body = { template_id: r.template_id, band: r.band, mode: r.mode };
    if (r.z != null) body.z = r.z;
    if (r.refMJD != null) body.time_origin = r.refMJD;
    const resp = await predictTmplib(body);
    if (reqId !== r.reqId || _ctx.isStale()) return;  // IA-7：旧回调直接丢
    r.curve = resp.curve;
    r.error = null;
  } catch (e) {
    if (reqId !== r.reqId || _ctx.isStale()) return;
    r.curve = null;
    // E-30：按 code 分支组织文案；message 只做展示
    const ctx = e.context || {};
    if (e.code === 'TL_OUT_OF_DOMAIN' && ctx.z_max_for_band != null) {
      r.error = `z=${r.z} 超出 ${r.band} 波段的适用域 [${ctx.z_min_for_band}, ${ctx.z_max_for_band}]（band 积分口径）。需要更蓝/更红的静止频率 ⇒ 换波段或换模板。`;
    } else if (e.code === 'TL_ENGINE_UNAVAILABLE') {
      r.error = TXT.engineDown;
    } else {
      r.error = e.message;
    }
  }
  _renderRows();
  tplRenderSidecars();
  _ctx.onChange();
}

// ─── 面板与侧卡渲染 ───
function _renderRows() {
  const box = document.getElementById('tplRows');
  if (!box) return;
  box.innerHTML = _rows.map(_rowHTML).join('');
  const clear = document.getElementById('tplClear');
  if (clear) clear.style.display = _rows.length ? '' : 'none';
}

export function tplRenderSidecars() {
  // Δμ 卡（U-08）：≥1 条曲线时出现；TXT-18 说明只出现一次
  const card = document.getElementById('tplMuCard');
  if (card) {
    const withMu = _rows.filter(r => r.curve && r.curve.mu);
    card.style.display = withMu.length ? '' : 'none';
    const body = document.getElementById('tplMuBody');
    if (body) {
      body.innerHTML = withMu.map(r => {
        const mu = r.curve.mu;
        if (!mu.ok) return `<div>▸ <b>${esc(r.template_id)}</b>：${esc(mu.message || TXT.muNone)}</div>`;
        const big = mu.alert === 'CA-13';
        return `<div class="${big ? 'text-danger' : ''}">▸ <b>${esc(r.template_id)}</b> Δμ = ${mu.delta > 0 ? '+' : ''}${mu.delta} mag${big ? ' ⚠' : ''}<br>
          <span class="text-secondary">引擎 ${mu.engine} / 库 ${mu.catalog}（${esc(mu.counterpart_id || '?')}）；成因：${esc(mu.cause)}${mu.detail ? ` — ${esc(mu.detail)}` : ''}</span></div>`;
      }).join('');
    }
    const note = document.getElementById('tplMuNote');
    if (note) note.style.display = withMu.some(r => r.curve.mu.ok) ? '' : 'none';
  }
  // 域判定条（IA-10：唯一"能不能引用"出口；有不可/外推/说明项时才出现）
  const bar = document.getElementById('tplDomainBar');
  if (bar) {
    const lines = [];
    for (const r of _rows) {
      if (r.error) { lines.push(`<div>✕ <b>${esc(r.template_id)}·${esc(r.band)}</b>：${esc(r.error)}</div>`); continue; }
      const c = r.curve;
      if (!c) continue;
      const d = c.domain || {};
      const notes = [];
      if ((d.reasons || []).includes('mode-downgraded-to-mono')) notes.push(TXT.mono);
      if ((d.reasons || []).some(x => x.includes('out-of-coverage'))) notes.push(TXT.outOfCoverage);
      if ((d.clipped_epochs || []).length) notes.push(TXT.clipped(d.clipped_epochs.length));
      if ((d.reasons || []).includes('table-time-frame-rest')) notes.push(TXT.restFrame);
      if (notes.length || d.state === 'extrapolated') {
        lines.push(`<div>▸ <b>${esc(r.template_id)}·${esc(r.band)}</b>（${d.state}）：${notes.map(esc).join('；') || (d.reasons || []).map(esc).join('；')}</div>`);
      }
    }
    bar.style.display = lines.length ? '' : 'none';
    bar.innerHTML = lines.join('');
  }
}

// ─── 数据集（compare.js 的 renderCompareChart 在源数据集之后追加） ───
export function tplDatasets({ absMag, restFrame }) {
  const colors = chartColors().bands;  // 模板用 bands 调色板，与 compare 源色循环不冲突
  return _rows.filter(r => r.curve).map((r, i) => {
    const c = r.curve, p = c.points;
    const zfac = restFrame ? (1 + c.z) : 1;   // 各自曲线自己的 z（设计决策）
    const xs = (r.refMJD != null && p.time_rel_s) ? p.time_rel_s : p.time_obs_s;
    const ys = absMag ? p.M_abs_AB : p.mag_AB.map(m => (m == null ? null : magABtoMJy(m)));
    // IA-9：M/K 为 null 的点不静默丢线 —— null 保留（spanGaps 跨过），全空也留图例
    const data = xs.map((x, j) => ({ x: x / zfac, y: ys[j] }));
    let label = `⌁ ${r.template_id}·${r.band}·z=${c.z}·${c.mode}`;
    if (c.time_origin && c.time_origin.kind === 'table-first-row' && r.refMJD == null) label += ' ⚠零点=表首行';
    if (r.refMJD != null) label += ' ⚠基准≠T0';
    return {
      type: 'line', label, data,
      borderColor: colors[i % colors.length],
      backgroundColor: colors[i % colors.length],
      showLine: true, pointRadius: 0, pointHoverRadius: 3,
      borderDash: [6, 4], borderWidth: 1.5,
      order: -1, spanGaps: true, _isTemplate: true,
    };
  });
}

// ─── 挂载（compare.js render() 内调用一次） ───
export async function bindTplPanel(ctx) {
  _ctx = ctx || _ctx;
  const panel = document.getElementById('tplPanel');
  if (!panel) return;
  panel.innerHTML = tplPanelHTML();
  _renderRows();
  tplRenderSidecars();

  if (_templates === null && !_templatesFailed) {
    try {
      const resp = await getTmplibTemplates();
      if (_ctx.isStale()) return;
      _templates = resp.templates || [];
    } catch (e) {
      _templatesFailed = true;
      console.warn('模板库列表不可用:', e.message);
    }
  }

  document.getElementById('tplAdd').addEventListener('click', () => {
    if (_rows.length >= MAX_ROWS) { showToast(`模板曲线最多 ${MAX_ROWS} 条`, 'warning'); return; }
    if (_templatesFailed) { showToast(TXT.engineDown, 'warning'); return; }
    const t = (_templates || []).find(x => x.state !== 'stale');
    if (!t) { showToast('模板库不可用', 'warning'); return; }
    const bands = sortBandsByFreq(t.bands || []);
    const band = bands.find(b => (t.modes || {})[b] !== 'mono') || bands[0];
    const r = {
      uid: ++_uid, template_id: t.id, band, z: t.z, mode: 'auto',
      refRaw: '', refMJD: null, curve: null, error: null, reqId: 0,
    };
    _rows.push(r);
    _renderRows();
    _fetchRow(r);
  });

  document.getElementById('tplClear').addEventListener('click', () => {
    _rows = [];
    _renderRows();
    tplRenderSidecars();
    _ctx.onChange();
  });

  // API-9 导出（F-40 前端不重算：参数原样发给服务端，文件由服务端渲染）
  const _exportPayload = () => {
    const curves = _rows.filter(r => !r.error).map(r => {
      const c = { template_id: r.template_id, band: r.band, mode: r.mode };
      if (r.z != null) c.z = r.z;
      if (r.refMJD != null) c.time_origin = r.refMJD;
      return c;
    });
    const sources = ((_ctx.getExportSources && _ctx.getExportSources()) || [])
      .map(id => ({ transient_id: id }));
    return { curves, sources, includeMeasured: true };
  };
  for (const [id, fmt] of [['tplExpCsv', 'csv'], ['tplExpJson', 'json']]) {
    document.getElementById(id).addEventListener('click', () => {
      const p = _exportPayload();
      if (!p.curves.length && !p.sources.length) {
        showToast('没有可导出的内容：先添加模板曲线或选择事件', 'warning');
        return;
      }
      exportTmplib(fmt, p);
    });
  }
  // 复制图（F-40：复用 detail_lcchart.js copyLCChart 的离屏合成方式，实现在 compare.js）
  document.getElementById('tplCopyImg').addEventListener('click', () => {
    if (typeof window.copyCompareChart === 'function') window.copyCompareChart();
    else showToast('对比图尚未生成', 'warning');
  });

  // 行内控件：事件委托（U-03 换模板 ⇒ 波段/z/mode/基准重置）
  document.getElementById('tplRows').addEventListener('change', (e) => {
    const rowEl = e.target.closest('.tpl-row');
    if (!rowEl) return;
    const r = _rows.find(x => x.uid === Number(rowEl.dataset.uid));
    if (!r) return;
    if (e.target.classList.contains('tpl-tmpl')) {
      const t = _templates.find(x => x.id === e.target.value);
      if (!t) return;
      const bands = sortBandsByFreq(t.bands || []);
      r.template_id = t.id;
      r.band = bands.find(b => (t.modes || {})[b] !== 'mono') || bands[0];
      r.z = t.z; r.mode = 'auto'; r.refRaw = ''; r.refMJD = null;
    } else if (e.target.classList.contains('tpl-band')) {
      r.band = e.target.value;
    } else if (e.target.classList.contains('tpl-mode')) {
      r.mode = e.target.value;
    } else if (e.target.classList.contains('tpl-ref-epoch')) {
      const p = parseRefEpoch(e.target.value);
      if (p.err) {
        showToast('基准时刻无法解析：请填 MJD 数字或 UTC 时间（留空/t0 = 模板 T0）', 'warning');
        e.target.value = r.refRaw || '';
        return;
      }
      if (p.mjd === r.refMJD) return;
      r.refMJD = p.mjd;
      r.refRaw = p.mjd != null ? String(p.mjd) : '';
      e.target.value = r.refRaw;   // 归一化显示为 MJD（与宿主既有控件一致）
    } else if (e.target.classList.contains('tpl-z')) {
      const v = e.target.value.trim() === '' ? null : Number(e.target.value);
      if (v != null && (!isFinite(v) || v < 0)) {
        showToast('z 必须是 ≥0 的有限数值', 'warning');
        e.target.value = r.z != null ? String(r.z) : '';
        return;
      }
      r.z = v;   // null = 模板自身 z
    } else {
      return;
    }
    r.curve = null; r.error = null;
    _fetchRow(r);
  });

  document.getElementById('tplRows').addEventListener('click', (e) => {
    const del = e.target.closest('.tpl-del');
    if (!del) return;
    const rowEl = del.closest('.tpl-row');
    _rows = _rows.filter(x => x.uid !== Number(rowEl.dataset.uid));
    _renderRows();
    tplRenderSidecars();
    _ctx.onChange();
  });
}
