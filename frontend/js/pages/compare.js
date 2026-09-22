// === Compare Page (multi-source overlay) ===
import { app, showLoading, showError, navSeq, navStale } from './layout.js';
import { getTransientMeta, getLightcurves, getFilters } from '../api.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { chartColors, academicFonts } from '../theme.js';
import { ensureFilterCache, mJyToMagAB, pointToMJy } from '../bands.js';
import { esc, minOf, maxOf } from '../utils.js';
import { createYErrBarPlugin } from '../chart_plugins.js';
import { t0ToMJD, parseRefEpoch } from './detail_lcchart.js';

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
let _cmpRefMJD = null;   // 统一基准时刻（MJD；null = 各源 T0，即默认行为）
let transientMeta = {};    // id → { z, dm }（红移 / 距离模数）
let lastAllLC = null;      // 最近一次拉取的光变数据（与 selectedTransients 对齐）
let bandSel = {};          // id → 已勾选波段数组（默认全选）
let allTransients = [];    // 全部事件（供名称/别名筛选）

// ─── 事件列表行 HTML（首绘与筛选重绘共用；勾选状态以 selectedTransients 为准） ───
function compareRowsHTML(items) {
  return items.map(t => `
    <tr class="row-link" data-tid="${esc(t.id)}">
      <td style="width:30px"><input type="checkbox" class="form-check-input cmp-cb" ${selectedTransients.includes(t.id) ? 'checked' : ''}></td>
      <td>${esc(t.id)}${(t.aliases && t.aliases.length) ? `<div class="small text-secondary">${esc(t.aliases.join(', '))}</div>` : ''}</td>
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
    _cmpRefMJD = null;   // 页面重渲染后输入框清空，基准同步复位为各源 T0
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
                        title="绝对星等按各源红移计算距离模数（无红移的源不显示）">
                  <option value="flux" selected>Y: 流量密度</option>
                  <option value="absmag">Y: 绝对星等</option>
                </select>
                <div class="form-check form-check-inline mb-0" title="是否绘制数据点的星等/流量密度误差棒">
                  <input class="form-check-input" type="checkbox" id="cmpShowErr" checked onchange="cmpErrToggle(this.checked)">
                  <label class="form-check-label small" for="cmpShowErr">误差棒</label>
                </div>
                <div class="form-check form-check-inline mb-0" title="有红移的源时间轴除以 (1+z) 改正到静止系">
                  <input class="form-check-input" type="checkbox" id="cmpRestFrame" onchange="renderCompareChart()">
                  <label class="form-check-label small" for="cmpRestFrame">静止系 t/(1+z)</label>
                </div>
                <span class="d-inline-flex align-items-center gap-1" title="统一横轴零点：填 MJD 数字或 UTC 时间（如 2022-10-09T13:16:59）；留空或填 t0 = 各源 T0">
                  <span class="text-secondary small">基准时刻:</span>
                  <input type="text" class="form-control form-control-sm" id="cmpRefEpoch" style="width:160px"
                         placeholder="留空=各源 T0；MJD 或 UTC">
                </span>
                <button class="btn btn-sm btn-outline-secondary" onclick="resetCmpZoom()" title="恢复默认范围"><i class="bi bi-arrows-expand"></i></button>
                <span class="text-secondary small" title="在图上按住左键拖出矩形框可放大该区域，左上角按钮恢复默认"><i class="bi bi-info-circle"></i> 可框选缩放</span>
              </div>
            </div>
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
    }

    window.renderCompareChart = renderCompareChart;

    // 统一基准时刻：留空/t0 = 各源 T0（默认）；否则按 mjd 换算横轴并重绘
    document.getElementById('cmpRefEpoch').addEventListener('change', (e) => {
      const r = parseRefEpoch(e.target.value);
      if (r.err) {
        alert('基准时刻无法解析：请填 MJD 数字或 UTC 时间（留空/t0 = 各源 T0）');
        e.target.value = _cmpRefMJD != null ? String(_cmpRefMJD) : '';
        return;
      }
      if (r.mjd === _cmpRefMJD) return;
      _cmpRefMJD = r.mjd;
      e.target.value = r.mjd != null ? String(r.mjd) : '';   // 归一化显示为 MJD
      renderCompareChart();
    });

    window.cmpErrToggle = (on) => {   // 误差棒开关：只影响绘制，无动画重绘即可
      _cmpShowErr = on;
      if (compareChart) compareChart.update('none');
    };

    window.resetCmpZoom = () => {
      cmpAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
      if (compareChart) compareChart.update();
    };

  } catch (err) {
    if (navStale(seq)) return;  // 已离开本页，错误提示不覆盖新页面
    showError(`加载事件列表失败: ${err.message}`);
  }
}

// ─── 误差棒插件：数据点带 err 字段时绘制竖直误差棒（画在数据点下层） ───
let _cmpShowErr = true;   // 是否绘制误差棒（图头「误差棒」开关）
const _cmpErrorBarPlugin = createYErrBarPlugin({
  enabled: () => _cmpShowErr,
  errOf: (ds, raw) => raw.err,
});

function renderCompareChart() {
  const allLC = lastAllLC;
  if (!allLC) return;
  if (typeof Chart === 'undefined') { console.error('Chart.js not loaded'); return; }
  const ctx = document.getElementById('compareChart');
  if (!ctx) return;
  if (compareChart) { compareChart.destroy(); compareChart = null; }

  const xType = document.getElementById('cmpXScale')?.value || 'logarithmic';
  const restFrame = document.getElementById('cmpRestFrame')?.checked || false;
  // 绝对星等模式：y = m_AB − μ(z)，仅显示有红移的源；线性反向轴
  const absMag = document.getElementById('cmpYMode')?.value === 'absmag';
  const cc = chartColors();
  const fonts = academicFonts();
  const colors = cc.compare;
  const bandStyles = ['circle', 'rectRot', 'triangle', 'rect', 'star', 'crossRot', 'cross', 'dash'];

  const datasets = [];
  selectedTransients.forEach((id, idx) => {
    const color = colors[idx % colors.length];
    const meta = transientMeta[id] || {};
    if (absMag && meta.dm == null) return; // 无红移的源无法计算距离模数
    const zfac = (restFrame && meta.z != null && meta.z > -1) ? (1 + meta.z) : 1;
    const t0mjd = t0ToMJD(meta.t0);   // 该源 T0（统一基准模式下对无 mjd 的点兜底）
    const wanted = bandSel[id] ? new Set(bandSel[id]) : null;
    // 按 (源, 波段) 拆数据集：同源同色，不同波段不同点形
    const byBand = {};
    for (const p of allLC[idx].items) {
      if (p.upperlimit) continue;
      const b = p.band || '?';
      if (wanted && !wanted.has(b)) continue;
      (byBand[b] = byBand[b] || []).push(p);
    }
    Object.keys(byBand).sort().forEach((band, bi) => {
      // 统一绘到 mJy 空间（bands.js pointToMJy；星等转 AB，Vega/ST 系统先改正）
      const pts = byBand[band].map(p => {
        const conv = pointToMJy(p, false);
        if (!conv) return null;
        const { y, err, clipped } = conv;
        // 绝对星等模式下原始值≤0 的点无对应星等，不绘制（流量模式截断到 log 轴底部并在 tooltip 标注）
        if (absMag && clipped) return null;
        const tObs = p.time;
        // 统一基准模式：x = ((p.mjd ?? 该源T0 + time/86400) − ref)×86400 / zfac；
        // 源无 T0 且该点无 mjd：无法换算到统一基准，跳过该点
        let x;
        if (_cmpRefMJD != null) {
          const mjd = (p.mjd != null) ? p.mjd : (t0mjd != null ? t0mjd + p.time / 86400 : null);
          if (mjd == null) return null;
          x = (mjd - _cmpRefMJD) * 86400 / zfac;
        } else {
          x = tObs / zfac;
        }
        if (absMag) {
          // 绝对星等模式:误差换算到星等空间 σ_m = (2.5/ln10)·σ_F/F
          const errMag = err != null && err > 0 ? (2.5 / Math.LN10) * err / y : null;
          return { x, y: mJyToMagAB(y) - meta.dm, err: errMag, tObs, clipped };
        }
        return { x, y, err, tObs, clipped };
      }).filter(d => d && isFinite(d.x) && isFinite(d.y));
      if (pts.length > 0) {
        datasets.push({
          label: `${id} · ${band}`, data: pts,
          backgroundColor: color, borderColor: color,
          showLine: false, pointRadius: 3, pointHoverRadius: 5,
          pointStyle: bandStyles[bi % bandStyles.length],
        });
      }
    });
  });

  if (datasets.length === 0) return;

  // ── 范围计算函数 ──
  function computeAxisRange(chart, mode) {
    const allX = [], allY = [];
    chart.data.datasets.forEach((ds, i) => {
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden) return;
      ds.data.forEach(p => {
        // 统一基准下线性轴的 x 可为负（基准前的点）；log 轴仍只取正值
        if (isFinite(p.x) && (p.x > 0 || (_cmpRefMJD != null && xType !== 'logarithmic'))) allX.push(p.x);
        // 绝对星等模式 y 可为负（线性轴）；流量模式仅取正值（log 轴）
        if (absMag ? isFinite(p.y) : (isFinite(p.y) && p.y > 0)) allY.push(p.y);
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
        // 统一基准时 x 可为负，不做 ≥0 钳制
        r = { min: _cmpRefMJD != null ? mn - pad : Math.max(0, mn - pad), max: mx + pad };
      }
      // 框选手动范围覆盖（null 端自动）
      if (cmpAxisRange.xmin != null) r.min = cmpAxisRange.xmin;
      if (cmpAxisRange.xmax != null) r.max = cmpAxisRange.xmax;
      return r;
    } else {
      if (allY.length === 0) return absMag ? { min: -30, max: -10 } : { min: 1e-13, max: 1 };
      const mn = minOf(allY), mx = maxOf(allY);
      const r = absMag
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
    plugins: [dragRectPlugin, _cmpErrorBarPlugin],
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
              const yTxt = absMag
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
            text: _cmpRefMJD != null
              ? (restFrame ? '静止系 t/(1+z)，统一基准 (s)' : 'time since 统一基准 (s)')
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
        // 左纵轴：流量密度 (mJy, log)；绝对星等模式为线性反向星等轴
        y: {
          type: absMag ? 'linear' : 'logarithmic',
          reverse: absMag,
          title: { display: true, text: absMag ? '绝对星等 M (AB)' : '流量密度 (mJy)', color: cc.tick, font: fonts.title },
          grid: { color: cc.gridSoft },
          border: { color: cc.tick },
          ticks: { color: cc.tick, font: fonts.tick, callback: v => absMag ? Number(v).toFixed(1) : sciFmt(v) },
          afterDataLimits(scale) {
            const r = computeAxisRange(scale.chart, 'y');
            scale.min = r.min; scale.max = r.max;
          },
        },
        // 右纵轴：AB 星等，与左轴 mJy 物理对应（m = 16.4 − 2.5·log10(F_mJy)）；绝对星等模式下隐藏
        y2: {
          display: !absMag,
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
  attachDragZoom(cmpChartHolder, ctx, (range) => {
    cmpAxisRange = range;
    compareChart.update('none');
  });
}
