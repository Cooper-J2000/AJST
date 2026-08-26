// === Compare Page (multi-source overlay) ===
import { app, showLoading, showError } from './layout.js';
import { getTransients, getLightcurves, getFilters } from '../api.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { chartColors, academicFonts } from '../theme.js';

// ─── 工具函数 ───
const C = 2.998e8;
const AB_MJY_ZP = 3.631;
function magToMJy(m) { return 3.631 * Math.pow(10, 6 - m / 2.5); }
function mJyToMag(f) { return 16.4 - 2.5 * Math.log10(f); }
function toMJy(v, u) {
  if (v == null) return null;
  switch (u) {
    case 'mJy': return v;
    case 'uJy': return v * 1e-3;
    case 'Jy':  return v * 1e3;
    case 'cgs': case 'erg/cm2/s/Hz': case 'cgs(erg/cm2/s/Hz)': return v * 1e26;
    case 'erg/cm2/s/keV': return v * 4.1357e-18 * 1e26; // 每 keV → 每 Hz → mJy
    default: return v;
  }
}
function normBand(b) { return b.replace(/(\d+)\.0(?=[A-Za-z])/g, '$1'); }
// 波段 → Vega→AB 星等差（绘图统一转 AB 星等用）
let _v2aMap = {};
function getV2A(band) {
  const b = normBand(band || '');
  const keys = [b, b.replace(/_{.*}$/, '').replace(/'+$/, ''), b.split('_')[0], b.toLowerCase(), 'uvot-' + b.toLowerCase()];
  for (const k of keys) { if (k && _v2aMap[k] != null) return _v2aMap[k]; }
  return 0;
}
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
let transientMeta = {};    // id → { z, dm }（红移 / 距离模数）
let lastAllLC = null;      // 最近一次拉取的光变数据（与 selectedTransients 对齐）
let bandSel = {};          // id → 已勾选波段数组（默认全选）
let allTransients = [];    // 全部事件（供名称/别名筛选）

// ─── 事件列表行 HTML（首绘与筛选重绘共用；勾选状态以 selectedTransients 为准） ───
function compareRowsHTML(items) {
  return items.map(t => `
    <tr class="row-link" onclick="toggleCompareSelect('${t.id}')">
      <td style="width:30px"><input type="checkbox" class="form-check-input" id="cb_${t.id}" ${selectedTransients.includes(t.id) ? 'checked' : ''}></td>
      <td>${t.id}${(t.aliases && t.aliases.length) ? `<div class="small text-secondary">${t.aliases.join(', ')}</div>` : ''}</td>
      <td class="small text-secondary">z=${t.redshift != null ? t.redshift.toFixed(2) : '?'}</td>
    </tr>
  `).join('');
}

export async function render() {
  showLoading();
  try {
    const [data, filters] = await Promise.all([
      getTransients({ per_page: 10000 }),
      getFilters(),
    ]);
    filtersCache = filters;
    transientMeta = {};
    allTransients = data.items;
    for (const t of data.items) transientMeta[t.id] = { z: t.redshift, dm: t.distmod ?? null };
    // 建立波长映射与 Vega→AB 系数映射
    const wlMap = {};
    _v2aMap = {};
    for (const f of filters) { wlMap[f.id] = f.wavelength; _v2aMap[f.id] = f.vega2ab || 0; }

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
                <button class="btn btn-sm btn-outline-secondary" onclick="resetCmpZoom()" title="恢复默认范围"><i class="bi bi-arrows-expand"></i></button>
                <span class="text-secondary small" title="在图上按住左键拖出矩形框可放大该区域，左上角按钮恢复默认"><i class="bi bi-info-circle"></i> 可框选缩放</span>
              </div>
            </div>
            <div class="card-body"><div class="chart-container" style="height:auto;aspect-ratio:3/2;min-height:0;overflow:hidden"><canvas id="compareChart"></canvas></div></div>
          </div>
        </div>
      </div>
    `;

    window.toggleCompareSelect = (id) => {
      const idx = selectedTransients.indexOf(id);
      if (idx >= 0) selectedTransients.splice(idx, 1);
      else selectedTransients.push(id);
      document.getElementById('selectedCount').textContent = `已选 ${selectedTransients.length} 个`;
      const cb = document.getElementById(`cb_${id}`);
      if (cb) cb.checked = idx < 0;
    };

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
            <div class="small fw-bold">${id} <span class="text-secondary fw-normal">z=${z != null ? z : '?'}</span></div>
            <div class="d-flex flex-wrap gap-2 ms-2">
              ${bands.map(b => `
                <div class="form-check form-check-inline mb-0">
                  <input class="form-check-input cmp-band-cb" type="checkbox" id="bb_${id}_${b}"
                         data-tid="${id}" data-band="${b}" ${newSel[id].includes(b) ? 'checked' : ''}>
                  <label class="form-check-label small" for="bb_${id}_${b}">${b}</label>
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
    }

    window.renderCompareChart = renderCompareChart;

    window.cmpErrToggle = (on) => {   // 误差棒开关：只影响绘制，无动画重绘即可
      _cmpShowErr = on;
      if (compareChart) compareChart.update('none');
    };

    window.resetCmpZoom = () => {
      cmpAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
      if (compareChart) compareChart.update();
    };

  } catch (err) {
    showError(`加载事件列表失败: ${err.message}`);
  }
}

// ─── 误差棒插件：数据点带 err 字段时绘制竖直误差棒（画在数据点下层） ───
let _cmpShowErr = true;   // 是否绘制误差棒（图头「误差棒」开关）
const _cmpErrorBarPlugin = {
  id: 'errorBar',
  beforeDatasetsDraw(chart) {
    if (!_cmpShowErr) return;
    try {
    const ctx = chart.ctx, yScale = chart.scales.y;
    if (!ctx || !yScale) return;
    chart.data.datasets.forEach((ds, dsIdx) => {
      if (!chart.isDatasetVisible(dsIdx)) return;   // 图例取消勾选时不画其误差棒
      const meta = chart.getDatasetMeta(dsIdx);
      if (!meta || !meta.data) return;
      ctx.save();
      ctx.strokeStyle = ds.borderColor || '#fff';
      ctx.lineWidth = 1;
      const n = Math.min(meta.data.length, ds.data.length);
      for (let i = 0; i < n; i++) {
        const raw = ds.data[i];
        const err = raw && raw.err;
        if (err == null || !(err > 0)) continue;
        const el = meta.data[i];
        if (!el || el.skip || raw.y == null || !isFinite(raw.y)) continue;
        const yTop = yScale.getPixelForValue(raw.y + err);
        const yBot = yScale.getPixelForValue(raw.y - err);
        if (!isFinite(yTop) || !isFinite(yBot)) continue;
        const cx = el.x;
        ctx.beginPath();
        ctx.moveTo(cx, Math.min(yTop, yBot));
        ctx.lineTo(cx, Math.max(yTop, yBot));
        ctx.moveTo(cx - 3, yTop);
        ctx.lineTo(cx + 3, yTop);
        ctx.moveTo(cx - 3, yBot);
        ctx.lineTo(cx + 3, yBot);
        ctx.stroke();
      }
      ctx.restore();
    });
    } catch (e) { console.error('errorBar plugin:', e); }
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
      // 统一绘到 mJy 空间（星等数据转 AB 后按 AB 零点换算；Vega 系统先加 vega2ab）
      const pts = byBand[band].map(p => {
        const unit = p.flux_density_unit;
        let y, err = null;
        if (unit === 'mag' || unit === 'magnitude') {
          let mag = p.flux_density;
          if ((p.mag_system || '').trim().toLowerCase() === 'vega') mag += getV2A(band);
          y = magToMJy(mag);
          // 星等误差换算到流量空间: σ_F = F·ln10·σ_m/2.5
          err = p.flux_density_err != null ? (Math.LN10 / 2.5) * y * p.flux_density_err : null;
        } else {
          y = toMJy(p.flux_density, unit);
          err = p.flux_density_err != null ? toMJy(p.flux_density_err, unit) : null;
        }
        if (y == null || !(y > 0)) return null;
        const tObs = p.time;
        if (absMag) {
          // 绝对星等模式:误差换算到星等空间 σ_m = (2.5/ln10)·σ_F/F
          const errMag = err != null && err > 0 ? (2.5 / Math.LN10) * err / y : null;
          return { x: tObs / zfac, y: mJyToMag(y) - meta.dm, err: errMag, tObs };
        }
        return { x: tObs / zfac, y, err, tObs };
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
        if (p.x > 0 && isFinite(p.x)) allX.push(p.x);
        // 绝对星等模式 y 可为负（线性轴）；流量模式仅取正值（log 轴）
        if (absMag ? isFinite(p.y) : (isFinite(p.y) && p.y > 0)) allY.push(p.y);
      });
    });
    if (mode === 'x') {
      if (allX.length === 0) return { min: 0.1, max: 1000 };
      const mn = Math.min(...allX), mx = Math.max(...allX);
      let r;
      if (xType === 'logarithmic') {
        r = { min: mn * 0.8, max: mx * 1.5 };
      } else {
        const pad = (mx - mn) * 0.1;
        r = { min: Math.max(0, mn - pad), max: mx + pad };
      }
      // 框选手动范围覆盖（null 端自动）
      if (cmpAxisRange.xmin != null) r.min = cmpAxisRange.xmin;
      if (cmpAxisRange.xmax != null) r.max = cmpAxisRange.xmax;
      return r;
    } else {
      if (allY.length === 0) return absMag ? { min: -30, max: -10 } : { min: 1e-13, max: 1 };
      const mn = Math.min(...allY), mx = Math.max(...allY);
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
                : `${sciFmt(p.y)}${raw.err != null ? `±${sciFmt(raw.err)}` : ''} mJy (AB=${mJyToMag(p.y).toFixed(2)})`;
              return `${ctx.dataset.label}: ${tTxt}, ${yTxt}`;
            },
          },
        },
      },
      scales: {
        x: {
          type: xType,
          title: { display: true, text: restFrame ? '静止系时间 t/(1+z) (s)' : '时间 (s)', color: cc.tick, font: fonts.title },
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
          ticks: { color: cc.tick, font: fonts.tick, callback: v => mJyToMag(v).toFixed(1) },
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
