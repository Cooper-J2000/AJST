// === Compare Page (multi-source overlay) ===
import { app, showLoading, showError, navSeq, navStale } from './layout.js';
import { getTransientMeta, getLightcurves, getFilters, compareTmplib, showToast } from '../api.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { chartColors, academicFonts } from '../theme.js';
import { ensureFilterCache, mJyToMagAB, pointToMJy } from '../bands.js';
import { esc, minOf, maxOf } from '../utils.js';
import { createYErrBarPlugin } from '../chart_plugins.js';
import { t0ToMJD, parseRefEpoch } from './detail_lcchart.js';
import { tplMuCardHTML, bindTplPanel, tplDatasets, tplHasCustomRef, tplRenderSidecars } from './compare_template.js';

// ─── 工具函数 ───
function sciFmt(v) {
  if (v == null || !isFinite(v)) return '0';
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1 && a < 1e4) return v.toFixed(1);
  return v.toExponential(2);
}

// ─── 全局 ───
let compareChart = null;
const cmpChartHolder = { chart: null };  // 框选缩放用的当前图表引用
let cmpAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };  // 框选手动范围
let selectedTransients = [];
let filtersCache = null;
let _cmpReqId = 0; // 异步请求令牌
let cmpRefMJD = {};      // 各源基准时刻（id → MJD；未设置 = 该源 T0，即默认行为）
let transientMeta = {};    // id → { z, dm }（红移 / 距离模数）
let lastAllLC = null;      // 最近一次拉取的光变数据（与 selectedTransients 对齐）
let bandSel = {};          // id → 已勾选波段数组（默认全选）
let allTransients = [];    // 全部事件（供名称/别名筛选）
let lastKcorr = null;      // 最近一次 API-8 响应 {key, curves, error}（kcorr 模式专用，T-29 不触旧路径）
let _kcorrReqId = 0;

// TXT-9（D-7：旧 absmag 保留并加标注）
const TXT_ABSMAG = '此模式不含 K 改正：M = m_AB − μ(z)。不同源在不同静止波长上比较 —— 与「K 改正绝对星等」不可混用同一句话引用。';

// ─── kcorr 模式（S3）：API-8 取数与数据集构建。只在新模式下生效（F-23 互斥切换） ───
async function _fetchKcorr(key) {
  const myReq = ++_kcorrReqId;
  try {
    const resp = await compareTmplib({
      sources: selectedTransients.map(id => ({ transient_id: id })),
    });
    if (myReq !== _kcorrReqId) return;          // 已有新请求，丢弃旧结果
    lastKcorr = { key, curves: resp.curves || [] };
  } catch (e) {
    if (myReq !== _kcorrReqId) return;
    lastKcorr = { key, curves: [], error: e.message };
  }
  if (typeof window.renderCompareChart === 'function') window.renderCompareChart();
}

// 同源同色；measured 实心 / 上限倒三角 / K 改正空心（IA-8 三种线型不混，W-25）
function _kcorrDatasets(restFrame, colors, bandStyles) {
  const out = [];
  const curves = (lastKcorr && lastKcorr.curves) || [];
  const srcIdx = {};
  selectedTransients.forEach((id, i) => { srcIdx[id] = i; });
  for (const c of curves) {
    if (c.state === 'error') continue;          // ✕ 在标注行列出，不画（F-23/W-24）
    const color = colors[(srcIdx[c.transient_id] ?? 0) % colors.length];
    const meta = transientMeta[c.transient_id] || {};
    const zfac = (restFrame && meta.z != null && meta.z > -1) ? (1 + meta.z) : 1;
    const t0mjd = t0ToMJD(meta.t0);
    const refMJD = cmpRefMJD[c.transient_id] ?? null;
    const xOf = (t) => {                        // 与旧分支同语义：默认基准=源 T0
      if (t == null) return null;
      if (refMJD != null) {
        if (t0mjd == null) return null;
        return (t - (refMJD - t0mjd) * 86400) / zfac;
      }
      return t / zfac;
    };
    const ptsOf = (arr, yOf, errOf) => arr.map(p => {
      const x = xOf(p.t_obs_s), y = yOf(p);
      if (x == null || y == null) return null;
      return { x, y, err: errOf ? errOf(p) : null, tObs: p.t_obs_s };
    }).filter(d => d && isFinite(d.x) && isFinite(d.y));
    const byBand = (arr) => {
      const g = {};
      arr.forEach(p => { (g[p.band] = g[p.band] || []).push(p); });
      return g;
    };
    if (c.kind === 'measured') {
      const det = byBand(c.points.detections);
      Object.keys(det).sort().forEach((band, bi) => {
        const pts = ptsOf(det[band], p => p.m_AB, p => p.mag_err);
        if (pts.length) out.push({
          label: `${c.transient_id} · ${band}`, data: pts,
          backgroundColor: color, borderColor: color,
          showLine: false, pointRadius: 3, pointHoverRadius: 5,
          pointStyle: bandStyles[bi % bandStyles.length], clip: true,
        });
      });
      const ulPts = ptsOf(c.points.upper_limits, p => p.m_AB, null);
      if (ulPts.length) out.push({
        label: `${c.transient_id} · 上限`, data: ulPts,
        backgroundColor: color, borderColor: color,
        showLine: false, pointRadius: 5, pointHoverRadius: 5,
        pointStyle: 'triangle', pointRotation: 180, clip: true,
        _isUpperLimit: true, hidden: !_cmpShowUL,
      });
    } else if (c.kind === 'kcorrected-measured') {
      const det = byBand(c.points.detections);
      Object.keys(det).sort().forEach((band, bi) => {
        const pts = ptsOf(det[band], p => p.M_meas, p => p.m_AB_err);
        if (pts.length) out.push({               // IA-8：空心点（同色描边、透明填充）
          label: `${c.transient_id} · ${band} K改正`, data: pts,
          backgroundColor: 'transparent', borderColor: color, borderWidth: 1.5,
          showLine: false, pointRadius: 4, pointHoverRadius: 6,
          pointStyle: bandStyles[bi % bandStyles.length], clip: true,
        });
      });
    }
  }
  return out;
}

// F-06' 计数上报 + ✕ 列表（W-24：无模板源逐条标 ✕）
function _kcorrNoteHTML() {
  const curves = (lastKcorr && lastKcorr.curves) || [];
  const lines = [];
  for (const c of curves) {
    if (c.state === 'error') {
      lines.push(`<div class="text-danger">✕ <b>${esc(c.transient_id || c.template_id || '?')}</b>：${esc(c.error.message)}</div>`);
    } else if (c.kind === 'kcorrected-measured') {
      const n = c.counts;
      lines.push(`<div>▸ <b>${esc(c.transient_id)}</b>（模板 <code>${esc(c.template_id)}</code>，μ 取引擎值 ${c.distance_modulus_engine}）：K 改正 ${n.detections} 点；`
        + `已剔除 discard ${n.discard} 点、上限单列 ${n.upper_limits} 点、出 t_valid 窗 ${n.clipped} 点、答不上 ${n.failed} 点</div>`);
    } else if (c.kind === 'measured' && !curves.some(x => x.kind === 'kcorrected-measured' && x.transient_id === c.transient_id && !x.state)) {
      const n = c.counts;
      lines.push(`<div class="text-secondary">▸ <b>${esc(c.transient_id)}</b>：实测 ${n.detections} 点；已剔除 discard ${n.discard} 点、上限单列 ${n.upper_limits} 点</div>`);
    }
  }
  if (lastKcorr && lastKcorr.error) lines.push(`<div class="text-danger">K 改正取数失败：${esc(lastKcorr.error)}</div>`);
  return lines.join('');
}

// POS-9/W-28：kcorr 图含引擎 μ ⇒ Δμ 卡必须出现；模板行已驱动时（同 μ）不重复填
function _kcorrMuCard() {
  const card = document.getElementById('tplMuCard');
  if (!card || card.style.display !== 'none') return;   // 模板行的 Δμ 卡已在
  const mus = ((lastKcorr && lastKcorr.curves) || [])
    .filter(c => c.kind === 'kcorrected-measured' && !c.state && c.mu && c.mu.ok);
  if (!mus.length) return;
  const body = document.getElementById('tplMuBody');
  const note = document.getElementById('tplMuNote');
  if (!body) return;
  body.innerHTML = mus.map(c => {
    const mu = c.mu;
    const big = mu.alert === 'CA-13';
    return `<div class="${big ? 'text-danger' : ''}">▸ <b>${esc(c.template_id)}</b> Δμ = ${mu.delta > 0 ? '+' : ''}${mu.delta} mag${big ? ' ⚠' : ''}<br>
      <span class="text-secondary">引擎 ${mu.engine} / 库 ${mu.catalog}（${esc(mu.counterpart_id || '?')}）；成因：${esc(mu.cause)}</span></div>`;
  }).join('');
  card.style.display = '';
  if (note) note.style.display = '';          // TXT-18 只在这里出现一次
}

// ─── 事件列表行 HTML（首绘与筛选重绘共用；勾选状态以 selectedTransients 为准） ───
function compareRowsHTML(items) {
  return items.map(t => `
    <tr class="row-link" data-tid="${esc(t.id)}">
      <td style="width:30px"><input type="checkbox" class="form-check-input cmp-cb" ${selectedTransients.includes(t.id) ? 'checked' : ''}></td>
      <td>${esc(t.id)} <span class="small text-secondary">（${t.lc_count ?? 0} 点）</span>${(t.aliases && t.aliases.length) ? `<div class="small text-secondary">${esc(t.aliases.join(', '))}</div>` : ''}</td>
      <td class="small text-secondary">z=${t.redshift != null ? t.redshift.toFixed(2) : '?'}</td>
    </tr>
  `).join('');
}

export async function render() {
  showLoading();
  const seq = navSeq();  // 导航序号：请求期间切到其它路由则丢弃本次渲染
  try {
    const [data, filters] = await Promise.all([
      getTransientMeta(),
      getFilters(),
    ]);
    if (navStale(seq)) return;  // 请求期间已切换路由
    filtersCache = filters;
    transientMeta = {};
    allTransients = data.items;
    for (const t of data.items) transientMeta[t.id] = { z: t.redshift, dm: t.distmod ?? null, t0: t.t0 ?? null };
    cmpRefMJD = {};      // 页面重渲染后各源基准输入清空，基准复位为各源 T0
    _cmpShowUL = true;   // 「显示上限点」复位为默认勾选（模板中 checked 为静态）
    // 滤波器缓存（波长 / Vega→AB 系数）
    ensureFilterCache(filters);

    app.innerHTML = `
      <div class="page-header"><h4><i class="bi bi-layers"></i> 多源光变对比</h4></div>
      <div class="row g-3 mb-3">
        <div class="col-md-4">
          <div class="card">
            <div class="card-header">选择事件对比</div>
            <div class="card-body p-0">
              <div class="p-2 border-bottom">
                <input type="text" class="form-control form-control-sm" id="cmpSearch"
                       placeholder="按名称或别名筛选…" oninput="filterCompareList(this.value)">
                <small class="text-secondary" id="cmpFilterCount"></small>
              </div>
              <div style="max-height:400px;overflow-y:auto">
                <table class="table table-sm table-hover mb-0">
                  <tbody id="cmpListBody">
                    ${compareRowsHTML(data.items)}
                  </tbody>
                </table>
              </div>
            </div>
            <div class="card-footer d-flex justify-content-between">
              <small class="text-secondary" id="selectedCount">已选 ${selectedTransients.length} 个</small>
              <button class="btn btn-sm btn-primary" onclick="loadCompareData()">绘制对比图</button>
            </div>
          </div>
          <!-- 各源波段选择（绘制后显示，基于所选事件的实际波段） -->
          <div class="card mt-3" id="bandSelectCard" style="display:none">
            <div class="card-header">各源对比波段</div>
            <div class="card-body py-2" id="bandSelectBody" style="max-height:300px;overflow-y:auto"></div>
            <div class="card-footer py-1"><small class="text-secondary">无红移的源不参与静止系改正；绝对星等模式下仅显示有红移的源</small></div>
          </div>
          <!-- 模板口径卡（Δμ 披露，U-08；有模板曲线时由 compare_template.js 填充） -->
          <div class="card mt-3" id="tplMuCard" style="display:none"></div>
        </div>
        <div class="col-md-8">
          <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
              <span><i class="bi bi-graph-up"></i> 对比光变图</span>
              <div class="d-flex gap-2 align-items-center flex-wrap">
                <select class="form-select form-select-sm" style="width:auto" id="cmpXScale" onchange="loadCompareData()">
                  <option value="logarithmic" selected>X: 对数</option>
                  <option value="linear">X: 线性</option>
                </select>
                <select class="form-select form-select-sm" style="width:auto" id="cmpYMode" onchange="renderCompareChart()"
                        title="绝对星等按各源红移计算距离模数（无红移的源不显示）；K 改正绝对星等走模板库引擎（无模板的源逐条标 ✕）">
                  <option value="flux" selected>Y: 流量密度</option>
                  <option value="absmag">Y: 绝对星等</option>
                  <option value="kcorr">Y: K 改正绝对星等</option>
                </select>
                <div class="form-check form-check-inline mb-0" title="是否绘制数据点的星等/流量密度误差棒">
                  <input class="form-check-input" type="checkbox" id="cmpShowErr" checked onchange="cmpErrToggle(this.checked)">
                  <label class="form-check-label small" for="cmpShowErr">误差棒</label>
                </div>
                <div class="form-check form-check-inline mb-0" title="有红移的源时间轴除以 (1+z) 改正到静止系">
                  <input class="form-check-input" type="checkbox" id="cmpRestFrame" onchange="renderCompareChart()">
                  <label class="form-check-label small" for="cmpRestFrame">静止系 t/(1+z)</label>
                </div>
                <div class="form-check form-check-inline mb-0" title="是否在图上显示上限点（倒三角）；取消勾选只显示探测点">
                  <input class="form-check-input" type="checkbox" id="cmpShowUL" checked onchange="cmpULToggle(this.checked)">
                  <label class="form-check-label small" for="cmpShowUL">显示上限点</label>
                </div>
                <button class="btn btn-sm btn-outline-secondary" onclick="resetCmpZoom()" title="恢复默认范围"><i class="bi bi-arrows-expand"></i></button>
                <span class="text-secondary small" title="在图上按住左键拖出矩形框可放大该区域，左上角按钮恢复默认"><i class="bi bi-info-circle"></i> 可框选缩放</span>
              </div>
            </div>
            <!-- 模板层（U-02..U-07）与域判定条（IA-10）：compare_template.js 填充；无模板行时零视觉噪音 -->
            <div id="tplPanel"></div>
            <div id="tplDomainBar" class="alert alert-warning small mx-3 mt-2 mb-0 py-1" style="display:none"></div>
            <!-- Y 模式标注行：absmag ⇒ TXT-9；kcorr ⇒ F-06' 剔除计数 + ✕ 列表；flux ⇒ 隐藏 -->
            <div id="cmpYModeNote" class="small mx-3 mt-2 mb-0 py-1" style="display:none"></div>
            <div class="card-body"><div class="chart-container" style="height:auto;aspect-ratio:3/2;min-height:0;overflow:hidden"><canvas id="compareChart"></canvas></div></div>
          </div>
        </div>
      </div>
    `;

    window.toggleCompareSelect = (id, checked) => {
      const idx = selectedTransients.indexOf(id);
      if (checked && idx < 0) selectedTransients.push(id);
      if (!checked && idx >= 0) selectedTransients.splice(idx, 1);
      document.getElementById('selectedCount').textContent = `已选 ${selectedTransients.length} 个`;
    };

    // 行点击/勾选（事件委托，避免内联 onclick 拼接待转义 ID）
    document.getElementById('cmpListBody').addEventListener('click', (e) => {
      const row = e.target.closest('tr[data-tid]');
      if (!row) return;
      const cb = row.querySelector('.cmp-cb');
      if (e.target !== cb) cb.checked = !cb.checked;
      window.toggleCompareSelect(row.dataset.tid, cb.checked);
    });

    // ── 按名称或别名筛选事件列表（大小写不敏感子串匹配） ──
    window.filterCompareList = (q) => {
      const query = q.trim().toLowerCase();
      const body = document.getElementById('cmpListBody');
      if (!body) return;
      const items = !query ? allTransients : allTransients.filter(t =>
        t.id.toLowerCase().includes(query) ||
        (t.aliases || []).some(a => String(a).toLowerCase().includes(query)));
      body.innerHTML = compareRowsHTML(items);
      const hint = document.getElementById('cmpFilterCount');
      if (hint) hint.textContent = query ? `筛选出 ${items.length} / ${allTransients.length} 个` : '';
    };

    window.loadCompareData = async () => {
      if (selectedTransients.length === 0) { alert('请至少选择一个事件'); return; }
      _cmpReqId++;
      const myReq = _cmpReqId;
      // 销毁旧图
      if (compareChart) { compareChart.destroy(); compareChart = null; }
      try {
        const allLC = await Promise.all(
          selectedTransients.map(id => getLightcurves({ transient_id: id, per_page: 9999 }))
        );
        if (myReq !== _cmpReqId) return; // 已有新请求，丢弃旧结果
        lastAllLC = allLC;
        lastKcorr = null;    // 源数据重拉 ⇒ kcorr 缓存一并作废（下次切 kcorr 重取）
        buildBandSelect(allLC);
        renderCompareChart();
      } catch (err) { alert(`加载数据失败: ${err.message}`); }
    };

    // ── 各源波段选择块：按所选事件的实际波段生成勾选框，默认全选 ──
    function buildBandSelect(allLC) {
      const card = document.getElementById('bandSelectCard');
      const body = document.getElementById('bandSelectBody');
      if (!card || !body) return;
      const newSel = {};
      const parts = [];
      selectedTransients.forEach((id, idx) => {
        const bands = [...new Set((allLC[idx].items || []).map(p => p.band).filter(Boolean))].sort();
        // 保留该源之前的勾选（波段仍存在时），否则默认全选
        const prev = (bandSel[id] || []).filter(b => bands.includes(b));
        newSel[id] = prev.length ? prev : [...bands];
        const z = transientMeta[id]?.z;
        parts.push(`
          <div class="mb-2">
            <div class="small fw-bold d-flex align-items-center gap-1">${esc(id)} <span class="text-secondary fw-normal">z=${z != null ? z : '?'}</span>
              ${bands.length ? `<span class="fw-normal ms-1">
                <button class="btn btn-sm btn-outline-secondary py-0 px-1 cmp-band-all" data-tid="${esc(id)}" data-on="1" style="font-size:0.72rem">全选</button>
                <button class="btn btn-sm btn-outline-secondary py-0 px-1 cmp-band-all" data-tid="${esc(id)}" data-on="0" style="font-size:0.72rem">全不选</button>
              </span>` : ''}
            </div>
            <div class="d-flex flex-wrap gap-2 ms-2">
              ${bands.map(b => `
                <div class="form-check form-check-inline mb-0">
                  <input class="form-check-input cmp-band-cb" type="checkbox" id="bb_${esc(id)}_${esc(b)}"
                         data-tid="${esc(id)}" data-band="${esc(b)}" ${newSel[id].includes(b) ? 'checked' : ''}>
                  <label class="form-check-label small" for="bb_${esc(id)}_${esc(b)}">${esc(b)}</label>
                </div>`).join('') || '<span class="small text-secondary">（无波段数据）</span>'}
            </div>
            <div class="d-flex align-items-center gap-1 ms-2 mt-1" title="该源横轴零点：可填 MJD 数字或 UTC 时间（如 2022-10-09T13:16:59）；留空或填 t0 = 该源 T0">
              <span class="text-secondary small">基准时刻:</span>
              <input type="text" class="form-control form-control-sm cmp-ref-epoch" data-tid="${esc(id)}" style="width:150px"
                     placeholder="留空=该源 T0；MJD 或 UTC" value="${cmpRefMJD[id] != null ? cmpRefMJD[id] : ''}">
            </div>
          </div>`);
      });
      bandSel = newSel;
      body.innerHTML = parts.join('');
      card.style.display = '';
      body.querySelectorAll('.cmp-band-cb').forEach(cb => {
        cb.addEventListener('change', () => {
          const tid = cb.dataset.tid, b = cb.dataset.band;
          const cur = new Set(bandSel[tid] || []);
          if (cb.checked) cur.add(b); else cur.delete(b);
          bandSel[tid] = [...cur];
          renderCompareChart();
        });
      });
      // 每源全选/全不选
      body.querySelectorAll('.cmp-band-all').forEach(btn => {
        btn.addEventListener('click', () => {
          const tid = btn.dataset.tid, on = btn.dataset.on === '1';
          const cbs = [...body.querySelectorAll('.cmp-band-cb')].filter(c => c.dataset.tid === tid);
          cbs.forEach(c => { c.checked = on; });
          bandSel[tid] = on ? cbs.map(c => c.dataset.band) : [];
          renderCompareChart();
        });
      });
      // 每源基准时刻：留空/t0 = 该源 T0（默认）；解析失败报错并保持原值
      body.querySelectorAll('.cmp-ref-epoch').forEach(inp => {
        inp.addEventListener('change', () => {
          const tid = inp.dataset.tid;
          const r = parseRefEpoch(inp.value);
          if (r.err) {
            alert('基准时刻无法解析：请填 MJD 数字或 UTC 时间（留空/t0 = 该源 T0）');
            inp.value = cmpRefMJD[tid] != null ? String(cmpRefMJD[tid]) : '';
            return;
          }
          if (r.mjd === (cmpRefMJD[tid] ?? null)) return;
          if (r.mjd == null) delete cmpRefMJD[tid]; else cmpRefMJD[tid] = r.mjd;
          inp.value = r.mjd != null ? String(r.mjd) : '';   // 归一化显示为 MJD
          renderCompareChart();
        });
      });
    }

    window.renderCompareChart = renderCompareChart;

    // 模板层挂载（U-02）：面板骨架 + Δμ 卡占位；行状态由 compare_template.js 模块级持有，
    // 页面重渲染（如换路由回来）后行会按状态重画并触发取数刷新
    document.getElementById('tplMuCard').innerHTML = tplMuCardHTML();
    bindTplPanel({
      isStale: () => navStale(seq),
      onChange: () => {
        if (typeof window.renderCompareChart === 'function') window.renderCompareChart();
      },
      getExportSources: () => selectedTransients.slice(),   // API-9 导出的源列表
    });

    window.cmpULToggle = (on) => {   // 上限点开关：只切换上限点数据集可见性，无动画重绘即可
      _cmpShowUL = on;
      if (!compareChart) return;
      compareChart.data.datasets.forEach((ds, i) => {
        if (ds._isUpperLimit) compareChart.setDatasetVisibility(i, on);
      });
      compareChart.update('none');
    };

    window.cmpErrToggle = (on) => {   // 误差棒开关：只影响绘制，无动画重绘即可
      _cmpShowErr = on;
      if (compareChart) compareChart.update('none');
    };

    window.resetCmpZoom = () => {
      cmpAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
      if (compareChart) compareChart.update();
    };

    // 复制对比图到剪贴板（F-40：沿用 detail_lcchart.js:901 copyLCChart 的离屏合成
    // 方式 —— 临时开图例/标题同步重绘，离屏 canvas 铺底色后写剪贴板，失败回退下载 PNG）
    window.copyCompareChart = async () => {
      const chart = cmpChartHolder.chart;
      if (!chart) { showToast('对比图尚未生成，请先绘制', 'warning'); return; }
      const cc = chartColors();
      const plg = chart.options.plugins;
      const legend = plg.legend, title = plg.title || (plg.title = {});
      const prev = { lg: legend.display, ti: title.display, text: title.text };
      legend.display = true;
      legend.position = 'top';
      title.display = true;
      title.text = '多源光变对比';
      title.color = cc.legend;
      title.font = academicFonts().title;
      chart.update('none');
      const restore = () => {
        legend.display = prev.lg;
        title.display = prev.ti;
        title.text = prev.text;
        chart.update('none');
      };
      try {
        const src = chart.canvas;
        const off = document.createElement('canvas');
        off.width = src.width;
        off.height = src.height;
        const octx = off.getContext('2d');
        octx.fillStyle = cc.canvasBg;
        octx.fillRect(0, 0, off.width, off.height);
        octx.drawImage(src, 0, 0);
        const blob = await new Promise(r => off.toBlob(r, 'image/png'));
        if (!blob) throw new Error('图像导出失败');
        if (navigator.clipboard && window.isSecureContext && typeof ClipboardItem !== 'undefined') {
          try {
            await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
            showToast('对比图已复制到剪贴板', 'success');
            return;
          } catch { /* 剪贴板写图失败，回退下载 */ }
        }
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'compare_lc.png';
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

  } catch (err) {
    if (navStale(seq)) return;  // 已离开本页，错误提示不覆盖新页面
    showError(`加载事件列表失败: ${err.message}`);
  }
}

// ─── 误差棒插件：数据点带 err 字段时绘制竖直误差棒（画在数据点下层） ───
let _cmpShowErr = true;   // 是否绘制误差棒（图头「误差棒」开关）
let _cmpShowUL = true;    // 是否显示上限点（图头「显示上限点」开关，默认勾选）
const _cmpErrorBarPlugin = createYErrBarPlugin({
  enabled: () => _cmpShowErr,
  errOf: (ds, raw) => raw.err,
});
// 包装层：手绘误差棒前把画布裁剪到 chartArea（框选放大后误差棒不越界；
// 共享实现 chart_plugins.js 不动，clip 只加在本页）
const cmpErrorBarClipPlugin = {
  id: 'cmpYErrBarClip',
  beforeDatasetsDraw(chart, args, opts) {
    const area = chart.chartArea;
    if (!area) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.left, area.top, area.width, area.height);
    ctx.clip();
    try {
      _cmpErrorBarPlugin.beforeDatasetsDraw(chart, args, opts);
    } finally {
      ctx.restore();
    }
  },
};

function renderCompareChart() {
  const allLC = lastAllLC;
  if (!allLC) return;
  if (typeof Chart === 'undefined') { console.error('Chart.js not loaded'); return; }
  const ctx = document.getElementById('compareChart');
  if (!ctx) return;
  if (compareChart) { compareChart.destroy(); compareChart = null; }

  const xType = document.getElementById('cmpXScale')?.value || 'logarithmic';
  const restFrame = document.getElementById('cmpRestFrame')?.checked || false;
  // Y 三态：flux(默认) / absmag(旧，μ-only) / kcorr(S3，API-8)；F-23 互斥切换，切换只重绘（U-01）
  const yMode = document.getElementById('cmpYMode')?.value || 'flux';
  const absMag = yMode === 'absmag';   // 绝对星等模式：y = m_AB − μ(z)，仅显示有红移的源；线性反向轴
  const kcorr = yMode === 'kcorr';
  const magLike = absMag || kcorr;     // 线性反向星等轴（旧 absmag 行为逐位不变，T-29）
  const cc = chartColors();
  const fonts = academicFonts();
  const colors = cc.compare;
  const bandStyles = ['circle', 'rectRot', 'triangle', 'rect', 'star', 'crossRot', 'cross', 'dash'];

  // kcorr：选择集变化才重新取数（key 相同 = 只重绘，U-01）
  if (kcorr) {
    const key = JSON.stringify(selectedTransients.slice().sort());
    if (!lastKcorr || lastKcorr.key !== key) _fetchKcorr(key);
  }

  // 任一源设了自定义基准时，横轴可出现负值（基准前的点）；模板曲线的自定义基准同样
  const anyRef = Object.values(cmpRefMJD).some(v => v != null) || tplHasCustomRef();

  const datasets = [];
  if (kcorr) {
    // S3 数据来自 API-8（服务端剔 discard/上限单列/Vega→AB，ST-9）；旧分支不执行
    datasets.push(..._kcorrDatasets(restFrame, colors, bandStyles));
  } else selectedTransients.forEach((id, idx) => {
    const color = colors[idx % colors.length];
    const meta = transientMeta[id] || {};
    if (absMag && meta.dm == null) return; // 无红移的源无法计算距离模数
    const zfac = (restFrame && meta.z != null && meta.z > -1) ? (1 + meta.z) : 1;
    const t0mjd = t0ToMJD(meta.t0);   // 该源 T0（自定义基准时对无 mjd 的点兜底）
    const refMJD = cmpRefMJD[id] ?? null;   // 该源基准时刻（MJD；null = 该源 T0，即默认行为）
    const wanted = bandSel[id] ? new Set(bandSel[id]) : null;
    // 按 (源, 波段, 探测/上限) 拆数据集：同源同色，不同波段不同点形，上限点倒三角
    const byBand = {};
    for (const p of allLC[idx].items) {
      const b = p.band || '?';
      if (wanted && !wanted.has(b)) continue;
      const g = byBand[b] = byBand[b] || { det: [], ul: [] };
      (p.upperlimit ? g.ul : g.det).push(p);
    }
    Object.keys(byBand).sort().forEach((band, bi) => {
      [{ arr: byBand[band].det, isUL: false }, { arr: byBand[band].ul, isUL: true }].forEach(({ arr, isUL }) => {
        if (arr.length === 0) return;
        // 统一绘到 mJy 空间（bands.js pointToMJy；星等转 AB，Vega/ST 系统先改正）
        const pts = arr.map(p => {
          const conv = pointToMJy(p, false);
          if (!conv) return null;
          const { y, err, clipped } = conv;
          // 绝对星等模式下原始值≤0 的点无对应星等，不绘制（流量模式截断到 log 轴底部并在 tooltip 标注）
          if (absMag && clipped) return null;
          const tObs = p.time;
          // 各源基准：x = ((p.mjd ?? 该源T0 + time/86400) − 该源ref)×86400 / zfac；
          // 源无 T0 且该点无 mjd：无法换算到该源基准，跳过该点；
          // 默认基准（=源 T0）下 time 缺失的点（如无 T0 源只录 mjd 的行）同样跳过
          let x;
          if (refMJD != null) {
            const mjd = (p.mjd != null) ? p.mjd : (t0mjd != null ? t0mjd + p.time / 86400 : null);
            if (mjd == null) return null;
            x = (mjd - refMJD) * 86400 / zfac;
          } else {
            if (tObs == null) return null;
            x = tObs / zfac;
          }
          if (absMag) {
            // 绝对星等模式:误差换算到星等空间 σ_m = (2.5/ln10)·σ_F/F
            const errMag = !isUL && err != null && err > 0 ? (2.5 / Math.LN10) * err / y : null;
            return { x, y: mJyToMagAB(y) - meta.dm, err: errMag, tObs, clipped };
          }
          return { x, y, err: isUL ? null : err, tObs, clipped };
        }).filter(d => d && isFinite(d.x) && isFinite(d.y));
        if (pts.length > 0) {
          datasets.push({
            label: `${id} · ${band}${isUL ? ' ↑' : ''}`, data: pts,
            backgroundColor: color, borderColor: color,
            showLine: false, pointRadius: isUL ? 5 : 3, pointHoverRadius: 5,
            pointStyle: isUL ? 'triangle' : bandStyles[bi % bandStyles.length],
            pointRotation: isUL ? 180 : 0,
            clip: true,   // 框选放大后把散点裁剪在 chartArea 内（Chart.js 默认对有点半径的散点不裁剪）
            _isUpperLimit: isUL,
            hidden: isUL && !_cmpShowUL,
          });
        }
      });
    });
  });

  // 模板曲线叠绘（U-04，虚线 ⌁，画在散点下层）；无模板行时返回空数组，既有两模式行为不变（T-29）
  datasets.push(...tplDatasets({ absMag: magLike, restFrame }));

  // Y 模式标注行：TXT-9（absmag）/ F-06' 计数 + ✕ 列表（kcorr）
  const noteEl = document.getElementById('cmpYModeNote');
  if (noteEl) {
    if (absMag) {
      noteEl.style.display = '';
      noteEl.className = 'small mx-3 mt-2 mb-0 py-1 alert alert-secondary';
      noteEl.textContent = TXT_ABSMAG;
    } else if (kcorr) {
      noteEl.style.display = '';
      noteEl.className = 'small mx-3 mt-2 mb-0 py-1 alert alert-secondary';
      noteEl.innerHTML = (lastKcorr && lastKcorr.key === JSON.stringify(selectedTransients.slice().sort()))
        ? (_kcorrNoteHTML() || '<span class="text-secondary">K 改正数据加载中…</span>')
        : '<span class="text-secondary">K 改正数据加载中…</span>';
    } else {
      noteEl.style.display = 'none';
    }
  }

  if (datasets.length === 0) return;

  // ── 范围计算函数 ──
  function computeAxisRange(chart, mode) {
    const allX = [], allY = [];
    chart.data.datasets.forEach((ds, i) => {
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden) return;
      ds.data.forEach(p => {
        // 自定义基准下线性轴的 x 可为负（基准前的点）；log 轴仍只取正值
        if (isFinite(p.x) && (p.x > 0 || (anyRef && xType !== 'logarithmic'))) allX.push(p.x);
        // 绝对星等/K 改正模式 y 可为负（线性轴）；流量模式仅取正值（log 轴）
        if (magLike ? isFinite(p.y) : (isFinite(p.y) && p.y > 0)) allY.push(p.y);
      });
    });
    if (mode === 'x') {
      if (allX.length === 0) return { min: 0.1, max: 1000 };
      const mn = minOf(allX), mx = maxOf(allX);
      let r;
      if (xType === 'logarithmic') {
        r = { min: mn * 0.8, max: mx * 1.5 };
      } else {
        const pad = (mx - mn) * 0.1;
        // 任一源设了自定义基准时 x 可为负，不做 ≥0 钳制
        r = { min: anyRef ? mn - pad : Math.max(0, mn - pad), max: mx + pad };
      }
      // 框选手动范围覆盖（null 端自动）
      if (cmpAxisRange.xmin != null) r.min = cmpAxisRange.xmin;
      if (cmpAxisRange.xmax != null) r.max = cmpAxisRange.xmax;
      return r;
    } else {
      if (allY.length === 0) return magLike ? { min: -30, max: -10 } : { min: 1e-13, max: 1 };
      const mn = minOf(allY), mx = maxOf(allY);
      const r = magLike
        ? { min: mn - Math.max(0.3, (mx - mn) * 0.08), max: mx + Math.max(0.3, (mx - mn) * 0.08) }
        : { min: Math.max(1e-13, mn * 0.5), max: mx * 2 };
      if (cmpAxisRange.ymin != null) r.min = cmpAxisRange.ymin;
      if (cmpAxisRange.ymax != null) r.max = cmpAxisRange.ymax;
      return r;
    }
  }

  compareChart = new Chart(ctx, {
    type: 'scatter',
    data: { datasets },
    plugins: [dragRectPlugin, cmpErrorBarClipPlugin],
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: true },
      plugins: {
        legend: { position: 'bottom', labels: { color: cc.tick, boxWidth: 14, padding: 12, font: fonts.legend, usePointStyle: true } },
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          callbacks: {
            label: (ctx) => {
              const p = ctx.parsed;
              const raw = ctx.raw || {};
              const tTxt = (restFrame && raw.tObs != null && raw.tObs !== p.x)
                ? `t_rest=${sciFmt(p.x)}s (t_obs=${sciFmt(raw.tObs)}s)`
                : `t=${sciFmt(p.x)}s`;
              const yTxt = magLike
                ? `M=${p.y.toFixed(2)}${raw.err != null ? `±${raw.err.toFixed(2)}` : ''}`
                : `${sciFmt(p.y)}${raw.err != null ? `±${sciFmt(raw.err)}` : ''} mJy (AB=${mJyToMagAB(p.y).toFixed(2)})${raw.clipped ? ' [原始值≤0，已截断]' : ''}`;
              return `${ctx.dataset.label}: ${tTxt}, ${yTxt}`;
            },
          },
        },
      },
      scales: {
        x: {
          type: xType,
          title: {
            display: true,
            text: anyRef
              ? (restFrame ? '静止系 t/(1+z)，各源基准 (s)' : 'time since 各源基准 (s)')
              : (restFrame ? '静止系时间 t/(1+z) (s)' : '时间 (s)'),
            color: cc.tick, font: fonts.title,
          },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: { color: cc.tick, font: fonts.tick, callback: v => sciFmt(v) },
          afterDataLimits(scale) {
            const r = computeAxisRange(scale.chart, 'x');
            scale.min = r.min; scale.max = r.max;
          },
        },
        // 左纵轴：流量密度 (mJy, log)；绝对星等/K 改正模式为线性反向星等轴
        y: {
          type: magLike ? 'linear' : 'logarithmic',
          reverse: magLike,
          title: { display: true, text: kcorr ? 'K 改正绝对星等 M (AB)' : (absMag ? '绝对星等 M (AB)' : '流量密度 (mJy)'), color: cc.tick, font: fonts.title },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: { color: cc.tick, font: fonts.tick, callback: v => magLike ? Number(v).toFixed(1) : sciFmt(v) },
          afterDataLimits(scale) {
            const r = computeAxisRange(scale.chart, 'y');
            scale.min = r.min; scale.max = r.max;
          },
        },
        // 右纵轴：AB 星等，与左轴 mJy 物理对应（m = 16.4 − 2.5·log10(F_mJy)）；星等模式下隐藏
        y2: {
          display: !magLike,
          type: 'logarithmic',
          position: 'right',
          title: { display: true, text: '星等 (AB)', color: cc.tick, font: fonts.title },
          grid: { drawOnChartArea: false },
          border: { color: cc.tick },
          ticks: { color: cc.tick, font: fonts.tick, callback: v => mJyToMagAB(v).toFixed(1) },
          afterDataLimits(scale) {
            const ys = scale.chart.scales.y;
            if (ys) { scale.min = ys.min; scale.max = ys.max; }
          },
        },
      },
    },
  });
  cmpChartHolder.chart = compareChart;
  tplRenderSidecars();   // Δμ 卡与域判定条随图刷新（U-08 / IA-10）
  if (kcorr) _kcorrMuCard();   // 无模板行时 K 改正曲线的 μ 也要披露（POS-9/W-28，TXT-18 只此一处）
  attachDragZoom(cmpChartHolder, ctx, (range) => {
    cmpAxisRange = range;
    compareChart.update('none');
  }, { allowNonPositive: () => (document.getElementById('cmpYMode')?.value || 'flux') !== 'flux' });  // IA-11
}
