// === 宿主星系统计子页（#/stats/hosts） ===
// 数据源 GET /api/stats/hosts：{n_hosts, n_with_spec_z, n_with_phot_z,
//   m_star: [...], sfr: [...], coverage: 0.xx,
//   abs_mag_points: [{tid, band, z, mag(AB), abs_mag, mag_err, err_assumed, upperlimit}]}
// 字段缺失时相应卡片/图显示「暂无数据」。
import { app, showLoading, showError, statsTabs } from './layout.js';
import { getHostStats, getOverview, getFilters } from '../api.js';
import { chartColors, academicFonts } from '../theme.js';
import {
  ensureFilterCache, buildSpectralColors, sortBandsByFreq, magABtoMJy,
} from '../bands.js';

function sciFmt(v) {
  if (v == null || !isFinite(v)) return '0';
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e-2 && a < 1e4) return String(parseFloat(Number(v).toPrecision(2)));
  return v.toExponential(1);
}

// 对数 bin 直方图：返回 {labels, counts}；无正值时返回 null
function logHist(values, nBins = 12) {
  const logs = (values || []).filter(v => isFinite(v) && v > 0).map(v => Math.log10(v));
  if (!logs.length) return null;
  const min = Math.min(...logs), max = Math.max(...logs);
  const w = (max - min) / nBins || 1;
  const counts = new Array(nBins).fill(0);
  for (const v of logs) counts[Math.min(Math.floor((v - min) / w), nBins - 1)]++;
  const labels = counts.map((_, i) => `${sciFmt(10 ** (min + i * w))}–${sciFmt(10 ** (min + (i + 1) * w))}`);
  return { labels, counts };
}

function makeHist(canvasId, hist, label, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  if (typeof Chart === 'undefined' || !hist) {
    canvas.parentElement.innerHTML = '<div class="text-secondary small p-3">暂无数据</div>';
    return;
  }
  const cc = chartColors();
  new Chart(canvas, {
    type: 'bar',
    data: { labels: hist.labels, datasets: [{ label, data: hist.counts, backgroundColor: color, borderWidth: 1 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: cc.tick, maxRotation: 90, font: { size: 10 } }, grid: { color: cc.grid } },
        y: { ticks: { color: cc.tick }, grid: { color: cc.grid }, title: { display: true, text: '数量', color: cc.tick } },
      },
    },
  });
}

// ─── 宿主绝对星等 vs 红移图 ───
// 与光变图同一套规则：波段按频率排序、光谱色阶、探测点圆点 / 上限点倒三角、误差棒开关。
let _absChart = null;
let _absPoints = [];
let _absColors = {};
let _absBands = [];
let _absBandVisible = {};
let _absShowErr = true;

// 误差棒插件（beforeDatasetsDraw：画在数据点下层）
const _absErrPlugin = {
  id: 'absErrBar',
  beforeDatasetsDraw(chart) {
    if (!_absShowErr) return;
    const ctx = chart.ctx;
    const yScale = chart.scales.y;
    if (!ctx || !yScale) return;
    chart.data.datasets.forEach((ds, dsIdx) => {
      if (!ds._errorValues || ds._isUpperLimit) return;
      if (!chart.isDatasetVisible(dsIdx)) return;
      const meta = chart.getDatasetMeta(dsIdx);
      if (!meta || !meta.data) return;
      ctx.save();
      ctx.strokeStyle = ds.borderColor || '#fff';
      ctx.lineWidth = 1;
      const n = Math.min(meta.data.length, ds._errorValues.length);
      for (let i = 0; i < n; i++) {
        const err = ds._errorValues[i];
        if (err == null || err <= 0) continue;
        const el = meta.data[i];
        const raw = ds.data[i];
        if (!el || el.skip || !raw || raw.y == null || !isFinite(raw.y)) continue;
        const cx = el.x;
        const yTop = yScale.getPixelForValue(raw.y + err);
        const yBot = yScale.getPixelForValue(raw.y - err);
        if (!isFinite(yTop) || !isFinite(yBot)) continue;
        ctx.beginPath();
        ctx.moveTo(cx, Math.min(yTop, yBot));
        ctx.lineTo(cx, Math.max(yTop, yBot));
        ctx.stroke();
        ctx.beginPath(); ctx.moveTo(cx - 3, yTop); ctx.lineTo(cx + 3, yTop); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(cx - 3, yBot); ctx.lineTo(cx + 3, yBot); ctx.stroke();
      }
      ctx.restore();
    });
  },
};

function _absToY(p, mJyMode) {
  // 星等模式：y = M（线性反向轴）；mJy 模式：y = 10pc 处流量密度（对数轴）
  return mJyMode ? magABtoMJy(p.abs_mag) : p.abs_mag;
}

function _absToYerr(p, mJyMode) {
  if (p.mag_err == null) return null;
  if (!mJyMode) return p.mag_err;
  const f = magABtoMJy(p.abs_mag);
  return f > 0 ? (Math.LN10 / 2.5) * f * p.mag_err : null;  // σ_F = F·ln10·σ_m/2.5
}

function buildAbsMagChart() {
  const canvas = document.getElementById('hostAbsMagChart');
  if (!canvas) return;
  if (_absChart) { _absChart.destroy(); _absChart = null; }
  const pts = (_absPoints || []).filter(p => isFinite(p.z) && isFinite(p.abs_mag));
  if (typeof Chart === 'undefined' || !pts.length) {
    canvas.parentElement.innerHTML = '<div class="text-secondary small p-3">暂无数据（需宿主红移 + 宿主测光）</div>';
    return;
  }
  const cc = chartColors();
  const fonts = academicFonts();
  const mJyMode = document.getElementById('hostAbsYMode')?.value === 'mjy';
  const datasets = [];
  for (const band of _absBands) {
    const bp = pts.filter(p => p.band === band);
    if (!bp.length) continue;
    const color = _absColors[band] || '#58a6ff';
    const det = bp.filter(p => !p.upperlimit);
    const ul = bp.filter(p => p.upperlimit);
    if (det.length) {
      datasets.push({
        label: band,
        data: det.map(p => ({ x: p.z, y: _absToY(p, mJyMode) })),
        backgroundColor: color, borderColor: color,
        pointBackgroundColor: color, pointBorderColor: color,
        showLine: false, pointRadius: 3, pointHoverRadius: 5,
        _errorValues: det.map(p => _absToYerr(p, mJyMode)),
        _raw: det,
        _isUpperLimit: false,
        _band: band,
      });
    }
    if (ul.length) {
      datasets.push({
        label: `${band} ↑`,
        data: ul.map(p => ({ x: p.z, y: _absToY(p, mJyMode) })),
        backgroundColor: color, borderColor: color,
        pointBackgroundColor: color, pointBorderColor: color,
        showLine: false, pointStyle: 'triangle', pointRadius: 5, pointRotation: 180,
        _errorValues: [],
        _raw: ul,
        _isUpperLimit: true,
        _band: band,
      });
    }
  }
  _absChart = new Chart(canvas, {
    type: 'scatter',
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          callbacks: {
            label(item) {
              const ds = item.dataset;
              const p = ds._raw && ds._raw[item.dataIndex];
              if (!p) return `${ds.label}`;
              const errTxt = ds._isUpperLimit ? '' :
                (p.mag_err != null ? ` ± ${p.mag_err}${p.err_assumed ? '（缺省0.2）' : ''}` : '');
              const mTxt = mJyMode
                ? `F(10pc)=${sciFmt(item.parsed.y)} mJy`
                : `M=${p.abs_mag}${errTxt} AB`;
              return `${ds.label} · ${p.tid}: ${mTxt}, m=${p.mag}, z=${p.z}`;
            },
          },
        },
      },
      scales: {
        x: {
          type: 'linear', min: 0,
          title: { display: true, text: '宿主星系红移 z', color: cc.tick, font: fonts.title },
          ticks: { color: cc.tick, font: fonts.tick }, grid: { color: cc.grid },
        },
        y: mJyMode ? {
          type: 'logarithmic',
          title: { display: true, text: '流量密度 @10pc (mJy)', color: cc.tick, font: fonts.title },
          ticks: { color: cc.tick, font: fonts.tick, callback: (v) => sciFmt(v) }, grid: { color: cc.grid },
        } : {
          type: 'linear', reverse: true,
          title: { display: true, text: '绝对星等 M (AB)', color: cc.tick, font: fonts.title },
          ticks: { color: cc.tick, font: fonts.tick }, grid: { color: cc.grid },
        },
      },
    },
    plugins: [_absErrPlugin],
  });
  applyAbsBandVisibility();
}

function applyAbsBandVisibility() {
  if (!_absChart) return;
  _absChart.data.datasets.forEach((ds, i) => {
    if (ds._band == null) return;
    _absChart.setDatasetVisibility(i, _absBandVisible[ds._band] !== false);
  });
  _absChart.update();
}

function buildAbsBandPanel() {
  const el = document.getElementById('hostAbsBandPanel');
  if (!el) return;
  const escA = (s) => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  el.innerHTML = `<span class="text-secondary">波段:</span>` + _absBands.map((b, i) => {
    const vis = _absBandVisible[b] !== false;
    const c = _absColors[b] || '#58a6ff';
    return `<span class="form-check form-check-inline mb-0">
      <input class="form-check-input host-abs-band-chk" type="checkbox" id="hostAbsBandChk_${i}" data-band="${escA(b)}" ${vis ? 'checked' : ''}>
      <label class="form-check-label" for="hostAbsBandChk_${i}"><span style="color:${c}">●</span> ${escA(b)}</label>
    </span>`;
  }).join('') + `
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hostAbsBandAll">全选</button>
    <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hostAbsBandNone">全不选</button>`;
  el.querySelectorAll('.host-abs-band-chk').forEach(chk => {
    chk.addEventListener('change', () => {
      _absBandVisible[chk.dataset.band] = chk.checked;
      applyAbsBandVisibility();
    });
  });
  document.getElementById('hostAbsBandAll')?.addEventListener('click', () => {
    for (const b of _absBands) _absBandVisible[b] = true;
    el.querySelectorAll('.host-abs-band-chk').forEach(c => { c.checked = true; });
    applyAbsBandVisibility();
  });
  document.getElementById('hostAbsBandNone')?.addEventListener('click', () => {
    for (const b of _absBands) _absBandVisible[b] = false;
    el.querySelectorAll('.host-abs-band-chk').forEach(c => { c.checked = false; });
    applyAbsBandVisibility();
  });
}

export async function render() {
  showLoading();
  app.innerHTML = `
    <div class="page-header d-flex justify-content-between align-items-center">
      <h4 class="mb-0"><i class="bi bi-houses"></i> 宿主星系统计</h4>
    </div>
    ${statsTabs('hosts')}
    <div class="row g-3 mb-4" id="hostStatCards"></div>
    <div class="row g-3">
      <div class="col-md-6">
        <div class="card">
          <div class="card-header">恒星质量 M* 分布（M☉，对数分箱）</div>
          <div class="card-body"><div class="chart-container" style="height:320px"><canvas id="hostMstarHist"></canvas></div></div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card">
          <div class="card-header">恒星形成率 SFR 分布（M☉/yr，对数分箱）</div>
          <div class="card-body"><div class="chart-container" style="height:320px"><canvas id="hostSfrHist"></canvas></div></div>
        </div>
      </div>
      <div class="col-12">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
            <span>宿主星系绝对星等 — 红移 <small class="text-secondary">M = m_AB − μ(z_host)，Planck18</small></span>
            <div class="d-flex gap-2 align-items-center flex-wrap">
              <select class="form-select form-select-sm" style="width:auto" id="hostAbsYMode">
                <option value="mag" selected>Y: 星等（线性）</option>
                <option value="mjy">Y: 流量密度 mJy（对数）</option>
              </select>
              <div class="form-check form-check-inline mb-0" title="是否绘制误差棒（缺省误差按 0.2 mag 计）">
                <input class="form-check-input" type="checkbox" id="hostAbsShowErr" checked>
                <label class="form-check-label small" for="hostAbsShowErr">误差棒</label>
              </div>
            </div>
          </div>
          <div class="card-body">
            <div class="chart-container" style="height:420px"><canvas id="hostAbsMagChart"></canvas></div>
            <div id="hostAbsBandPanel" class="d-flex flex-wrap gap-2 align-items-center mt-2 small"></div>
          </div>
        </div>
      </div>
    </div>
  `;

  let st = null, overview = null;
  try {
    [st, overview] = await Promise.all([getHostStats(), getOverview().catch(() => null)]);
  } catch (err) {
    showError(`加载宿主统计失败: ${err.message}`);
    return;
  }

  // ── 覆盖率卡片（字段缺失时容错） ──
  const nHosts = st.n_hosts ?? 0;
  const total = (overview && overview.n_transients) || null;
  const coverage = st.coverage != null
    ? `${(st.coverage * 100).toFixed(1)}%`
    : (total ? `${(nHosts / total * 100).toFixed(1)}%` : '—');
  const card = (value, label) => `
    <div class="col-md-3 col-6"><div class="card"><div class="card-body stat-card">
      <div class="stat-value">${value}</div><div class="stat-label">${label}</div></div></div></div>`;
  document.getElementById('hostStatCards').innerHTML =
    card(total != null ? `${nHosts} / ${total}` : nHosts, '有宿主的源 / 总数') +
    card(coverage, '宿主覆盖率') +
    card(st.n_with_spec_z ?? '—', '宿主 spec-z') +
    card(st.n_with_phot_z ?? '—', '宿主 phot-z');

  // ── 分布图 ──
  makeHist('hostMstarHist', logHist(st.m_star), 'M*', '#58a6ff');
  makeHist('hostSfrHist', logHist(st.sfr), 'SFR', '#3fb950');

  // ── 绝对星等 — 红移图（波段色阶需要滤波器波长表） ──
  if (_absChart) { _absChart.destroy(); _absChart = null; }
  _absPoints = st.abs_mag_points || [];
  _absBandVisible = {};
  _absShowErr = true;
  try {
    ensureFilterCache(await getFilters());
  } catch { /* 无滤波器表时波段退化为默认色 */ }
  _absBands = sortBandsByFreq([...new Set(_absPoints.map(p => p.band))]);
  _absColors = buildSpectralColors(_absBands);
  buildAbsBandPanel();
  buildAbsMagChart();
  document.getElementById('hostAbsYMode')?.addEventListener('change', buildAbsMagChart);
  document.getElementById('hostAbsShowErr')?.addEventListener('change', (e) => {
    _absShowErr = e.target.checked;
    if (_absChart) _absChart.update();
  });
}
