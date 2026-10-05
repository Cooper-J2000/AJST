// === specphot 锚点表（④ 常驻输入面，U-40…U-42、F-78、IA-18、CA-33） ===
// 库内实测点行：来自 API-2 响应 anchor_rows（anchor_origin='catalog'），只读展示 +
// U-40 勾选（取消勾选 = 不参与锚定与比对，但保留在表里，经 anchor_bands 传达服务端）。
// 手加行：U-41 动态行编辑器（波段 ▾ 来自 API-1 波段清单 + 星等 + 星等系 ▾ + 时刻 + 备注）。
// 同波段冲突（源表 + 手加，或两条手加）⇒ 标红 + 「计算」禁用（CA-33），服务端不代择一。
import { esc, escAttr } from '../utils.js';

const C_MAX_ANCHORS = 24;   // Q-25（与后端 constants.py 同值）
const TXT = {
  t20: '手填锚点与源测光表同权参与缩放因子求解；请如实填星等系与时刻，二者会原样写进导出件。', // TXT-20
};

// 冲突波段集（同波段 ≥2 行 ⇒ CA-33）：workbench 的 IA-16 / 「计算」门控消费
export function conflictBands(S) {
  const count = new Map();
  for (const r of catalogRows(S)) count.set(r.band, (count.get(r.band) || 0) + 1);
  for (const r of S.manualRows) count.set(r.band, (count.get(r.band) || 0) + 1);
  return new Set([...count.entries()].filter(([, n]) => n > 1).map(([b]) => b));
}

function catalogRows(S) {
  const ar = (S.lastResponse || {}).anchor_rows;
  return Array.isArray(ar) ? ar.filter(r => r.anchor_origin === 'catalog') : [];
}

// 手加行 → API-2 anchor_rows（U-41；mjd 空 = null ⇒ 服务端只做幅值比对，F-77）
export function manualAnchorRows(S) {
  return S.manualRows.map(r => ({
    band: r.band,
    mag: Number(r.mag),
    mag_system: r.mag_system || 'AB',
    mjd: (r.mjd === '' || r.mjd == null) ? null : Number(r.mjd),
    note: r.note || '',
  }));
}

// U-40 勾选集 → API-2 anchor_bands：全部默认勾选时返回 null（服务端按全行处理）；
// 任一行被取消勾选 ⇒ 只提交勾选行的波段（未勾行仍显示、计入 n_anchor_excluded，F-78②）
export function checkedAnchorBands(S) {
  const rows = [...catalogRows(S), ...S.manualRows];
  if (!rows.length) return null;
  const anyUnchecked = rows.some(r => S.anchorChecks[r.band] === false);
  if (!anyUnchecked) return null;
  const checked = [...new Set(rows.filter(r => S.anchorChecks[r.band] !== false).map(r => r.band))];
  return checked;   // 可能为空数组：全部退出锚定（direct/自动降级由服务端裁决）
}

// ─── 渲染（常驻、不折叠，F-0i9） ───
export function renderAnchors(el, ctx) {
  const S = ctx.S;
  const conflicts = conflictBands(S);
  const rows = [
    ...catalogRows(S).map(r => ({ ...r, _readonly: true })),
    ...S.manualRows.map((r, mi) => ({ ...r, anchor_origin: 'manual', _readonly: false, _mi: mi })),
  ];
  const bandOpts = (sel, extra = '') => ['<option value="">波段…</option>']
    .concat((S.bandsMeta || []).map(b =>
      `<option value="${escAttr(b.id)}" ${sel === b.id ? 'selected' : ''} ${extra}>${esc(b.id)}</option>`))
    .join('');

  const trs = rows.map((r, i) => {
    const bad = conflicts.has(r.band);
    const key = r.band;
    const checked = S.anchorChecks[key] !== false;
    return `
    <tr class="${bad ? 'table-danger' : ''}">
      <td>${r._readonly
        ? `<input type="checkbox" class="form-check-input mt-0 sp-achk" data-band="${escAttr(key)}" ${checked ? 'checked' : ''}>`
        : `<input type="checkbox" class="form-check-input mt-0 sp-achk" data-band="${escAttr(key)}" ${checked ? 'checked' : ''}>`}</td>
      <td>${esc(r.band)}${bad ? ' <i class="bi bi-exclamation-triangle text-danger" title="CA-33：同波段出现两行，请选一条"></i>' : ''}</td>
      <td>${esc(r.mag)}</td>
      <td>${esc(r.mag_system || 'AB')}</td>
      <td>${r.mjd != null && r.mjd !== '' ? esc(r.mjd)
        : '<span class="text-secondary">不做时刻比对（F-77）</span>'}</td>
      <td>${r.mag_err != null ? esc(r.mag_err) : '<span class="text-secondary">—</span>'}</td>
      <td>${r.anchor_origin === 'catalog'
        ? '<span class="badge bg-secondary">源表</span>'
        : '<span class="badge bg-info text-dark">手加</span>'}</td>
      <td class="small text-secondary">${esc(r.note || '')}</td>
      <td>${r._readonly ? '' : `<button class="btn btn-sm btn-outline-danger py-0 px-1 sp-adel" data-mi="${r._mi}">删</button>`}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `
  <div class="card mb-3">
    <div class="card-header d-flex align-items-center py-2">
      ④ 定标锚点表
      <span class="small text-secondary fw-normal ms-2">手加 ${S.manualRows.length}/${C_MAX_ANCHORS}
        ${conflicts.size ? `<span class="text-danger ms-2">CA-33：波段 ${[...conflicts].join('、')} 冲突，计算已禁用——请选一条</span>` : ''}</span>
    </div>
    <div class="card-body py-2">
    <div class="table-responsive mt-1" style="max-height:280px;overflow:auto">
      <table class="table table-sm table-striped mb-0 small">
        <thead><tr><th>锚定</th><th>波段</th><th>星等</th><th>系统</th><th>时刻 MJD</th>
          <th>mag_err</th><th>来源</th><th>备注</th><th></th></tr></thead>
        <tbody>${trs || `<tr><td colspan="9" class="text-secondary">暂无行：库内实测点行在首次计算后由响应 anchor_rows 回显；上传/直入可先手加行（B5）。</td></tr>`}</tbody>
      </table>
    </div>
    <details class="mt-1 small">
      <summary>＋ 手加一行（U-41）</summary>
      <div class="row g-2 mt-1 align-items-end" title="${escAttr(TXT.t20)}">
        <div class="col-auto"><label class="form-label mb-0">波段</label>
          <select class="form-select form-select-sm sp-aband">${bandOpts('')}</select></div>
        <div class="col-auto"><label class="form-label mb-0">星等</label>
          <input type="number" step="any" class="form-control form-control-sm sp-amag sp-w110"></div>
        <div class="col-auto"><label class="form-label mb-0">星等系</label>
          <select class="form-select form-select-sm sp-ams">
            <option value="AB" selected>AB</option>
            <option value="ST" disabled title="ST 待 M-5（P1 不可用）">ST</option>
            <option value="Vega" disabled title="Vega 待该波段登记 vega2ab（F-9）——P1 手加行固定 AB">Vega</option>
          </select></div>
        <div class="col-auto"><label class="form-label mb-0">时刻 MJD（可空）</label>
          <input type="number" step="any" class="form-control form-control-sm sp-amjd sp-w130"></div>
        <div class="col-auto"><label class="form-label mb-0">备注</label>
          <input type="text" class="form-control form-control-sm sp-anote sp-w160"></div>
        <div class="col-auto"><button class="btn btn-sm btn-outline-primary sp-aadd">加入</button></div>
        <div class="col-12 text-secondary">${TXT.t20}</div>
      </div>
    </details>
  </div></div>`;

  // ── 事件 ──
  el.querySelectorAll('.sp-achk').forEach(cb => cb.addEventListener('change', () => {
    S.anchorChecks[cb.dataset.band] = cb.checked;
    ctx.dirty();
  }));
  el.querySelectorAll('.sp-adel').forEach(btn => btn.addEventListener('click', () => {
    S.manualRows.splice(Number(btn.dataset.mi), 1);
    ctx.dirty(); renderAnchors(el, ctx);
  }));
  const add = el.querySelector('.sp-aadd');
  if (add) add.addEventListener('click', () => {
    const band = el.querySelector('.sp-aband').value;
    const mag = el.querySelector('.sp-amag').value;
    const ms = el.querySelector('.sp-ams').value;
    const mjd = el.querySelector('.sp-amjd').value;
    const note = el.querySelector('.sp-anote').value;
    const q = (c) => el.querySelector(c);
    if (!band || mag === '' || !isFinite(Number(mag))) return;   // 行内静默校验 + 缺项清单兜底
    if (S.manualRows.length >= C_MAX_ANCHORS) return;
    S.manualRows.push({ band, mag, mag_system: 'AB', mjd, note });
    q('.sp-aband').value = ''; q('.sp-amag').value = ''; q('.sp-amjd').value = ''; q('.sp-anote').value = '';
    ctx.dirty(); renderAnchors(el, ctx);
  });
}
