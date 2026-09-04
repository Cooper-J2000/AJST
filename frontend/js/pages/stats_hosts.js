// === 宿主星系统计子页（#/stats/hosts） ===
// 数据源 GET /api/stats/hosts：{n_hosts, n_with_spec_z, n_with_phot_z,
//   m_star: [...], sfr: [...], coverage: 0.xx,
//   m_star_points: [{tid, z, m_star}],  sfr_points: [{tid, z, sfr}],   ← z 可为 null
//   abs_mag_points: [{tid, band, z, mag(AB), abs_mag, mag_err, err_assumed, upperlimit,
//     gext_applied(是否已应用银消改正), gext_Alambda,
//     mag_raw(库中原始星等), mag_corr(银消改正后/星等系统换算前), mag_sys, gext_corr}]}
// 字段缺失时相应卡片/图显示「暂无数据」。
import { app, showLoading, showError, statsTabs } from './layout.js';
import { getHostStats, getOverview, getFilters, showToast } from '../api.js';
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

// ─── 距离模数 μ(z)：Planck18（H0=67.66, Om0=0.30966，平直 ΛCDM）， ───
// 与后端 models.distance_modulus（astropy Planck18）同参数；Simpson 数值积分。
// 光子与无质量中微子按辐射项、0.06 eV 中微子按物质项（Om_nu = m/(93.14 h² eV)），
// 与 astropy Planck18.distmod 偏差 <0.001 mag（z≤8 实测）。
function planck18Distmod(z) {
  if (z == null || !(z > 0)) return null;
  const H0 = 67.66;
  const h = H0 / 100;
  const OmNu = 0.06 / (93.14 * h * h);          // 大质量中微子物质密度
  const Om = 0.30966 + OmNu;
  const Or = 5.402015137139353e-05 + Math.max(0.0014396743040845244 - OmNu, 0);
  const Ode = 1 - Om - Or;
  const C_KM_S = 299792.458;
  const n = 2000, dz = z / n;
  let s = 0;
  for (let i = 0; i <= n; i++) {
    const zi = i * dz;
    const invE = 1 / Math.sqrt(Om * (1 + zi) ** 3 + Or * (1 + zi) ** 4 + Ode);
    s += (i === 0 || i === n) ? invE : (i % 2 ? 4 : 2) * invE;
  }
  const dc = (C_KM_S / H0) * (dz / 3) * s;   // 共动距离 Mpc
  const dl = dc * (1 + z);
  return 5 * Math.log10(dl) + 25;
}

// ─── CSV 下载 ───
function downloadCsv(filename, rows) {
  const esc = (v) => {
    const s = v == null ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const blob = new Blob([rows.map(r => r.map(esc).join(',')).join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function dateTag() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
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

// ─── 参数 — 红移散点图（M* / SFR，纵轴对数） ───
function buildZScatter(canvasId, points, valKey, yLabel, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const pts = (points || []).filter(p =>
    p.z != null && isFinite(p.z) && p[valKey] != null && isFinite(p[valKey]) && p[valKey] > 0);
  if (typeof Chart === 'undefined' || !pts.length) {
    canvas.parentElement.innerHTML = '<div class="text-secondary small p-3">暂无数据（需宿主红移 + 拟合参数）</div>';
    return;
  }
  const cc = chartColors();
  const fonts = academicFonts();
  new Chart(canvas, {
    type: 'scatter',
    data: {
      datasets: [{
        label: yLabel,
        data: pts.map(p => ({ x: p.z, y: p[valKey], _tid: p.tid })),
        backgroundColor: color, borderColor: color,
        pointRadius: 3, pointHoverRadius: 5, showLine: false,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText,
          callbacks: {
            label(item) {
              const tid = item.raw && item.raw._tid;
              return `${tid ? tid + ': ' : ''}z=${item.parsed.x}, ${yLabel}=${sciFmt(item.parsed.y)}`;
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
        y: {
          type: 'logarithmic',
          title: { display: true, text: yLabel, color: cc.tick, font: fonts.title },
          ticks: { color: cc.tick, font: fonts.tick, callback: (v) => sciFmt(v) }, grid: { color: cc.grid },
        },
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
let _mstarPoints = [];
let _sfrPoints = [];

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
  // 等视星等参考虚线：M(z) = m − μ(z)，z 取当前勾选波段数据点（含上限）的最大红移
  const limitOn = document.getElementById('hostAbsLimitMagOn')?.checked;
  const limitMag = parseFloat(document.getElementById('hostAbsLimitMag')?.value);
  if (limitOn && isFinite(limitMag)) {
    const visPts = pts.filter(p => _absBandVisible[p.band] !== false);
    const zmax = visPts.length ? Math.max(...visPts.map(p => p.z)) : 0;
    if (zmax > 0) {
      const data = [];
      const N = 100;
      for (let i = 1; i <= N; i++) {
        const z = (zmax * i) / N;
        const M = limitMag - planck18Distmod(z);
        data.push({ x: z, y: mJyMode ? magABtoMJy(M) : M });
      }
      datasets.push({
        label: `m=${limitMag}`,
        data,
        showLine: true, pointRadius: 0, pointHoverRadius: 0,
        borderColor: '#f85149', backgroundColor: '#f85149',
        borderDash: [6, 4], borderWidth: 1.5,
        _isLimitCurve: true,
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
              if (ds._isLimitCurve) {
                const yTxt = mJyMode ? `F(10pc)=${sciFmt(item.parsed.y)} mJy` : `M=${item.parsed.y.toFixed(2)}`;
                return `${ds.label}: z=${item.parsed.x.toFixed(3)}, ${yTxt}`;
              }
              const p = ds._raw && ds._raw[item.dataIndex];
              if (!p) return `${ds.label}`;
              const errTxt = ds._isUpperLimit ? '' :
                (p.mag_err != null ? ` ± ${p.mag_err}${p.err_assumed ? '（缺省0.2）' : ''}` : '');
              const mTxt = mJyMode
                ? `F(10pc)=${sciFmt(item.parsed.y)} mJy`
                : `M=${p.abs_mag}${errTxt} AB`;
              const gextTxt = p.gext_applied ? '（已银消改正）' : '';
              return `${ds.label} · ${p.tid}: ${mTxt}${gextTxt}, m=${p.mag}, z=${p.z}`;
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
  // 波段勾选变化会改变等星等虚线的最大 z，整图重建
  el.querySelectorAll('.host-abs-band-chk').forEach(chk => {
    chk.addEventListener('change', () => {
      _absBandVisible[chk.dataset.band] = chk.checked;
      buildAbsMagChart();
    });
  });
  document.getElementById('hostAbsBandAll')?.addEventListener('click', () => {
    for (const b of _absBands) _absBandVisible[b] = true;
    el.querySelectorAll('.host-abs-band-chk').forEach(c => { c.checked = true; });
    buildAbsMagChart();
  });
  document.getElementById('hostAbsBandNone')?.addEventListener('click', () => {
    for (const b of _absBands) _absBandVisible[b] = false;
    el.querySelectorAll('.host-abs-band-chk').forEach(c => { c.checked = false; });
    buildAbsMagChart();
  });
}

// ─── 各图 CSV 导出 ───
function downloadAbsMagCsv() {
  const bands = _absBands.filter(b => _absBandVisible[b] !== false);
  const pts = (_absPoints || []).filter(p => bands.includes(p.band));
  if (!pts.length) { showToast('没有可导出的数据点（当前勾选波段下为空）', 'warning'); return; }
  const rows = [[
    'id', 'band', 'redshift', 'mag_raw', 'mag_sys', 'gext_corr', 'gext_Alambda',
    'mag_corr', 'mag_ab', 'abs_mag', 'mag_err', 'err_assumed', 'upperlimit',
  ]];
  for (const p of pts) {
    rows.push([
      p.tid, p.band, p.z, p.mag_raw, p.mag_sys, p.gext_corr,
      p.gext_Alambda ?? '', p.mag_corr, p.mag, p.abs_mag,
      p.mag_err ?? '', p.err_assumed, p.upperlimit,
    ]);
  }
  downloadCsv(`host_absmag_z_${dateTag()}.csv`, rows);
}

function downloadPointsCsv(points, valKey, name) {
  const pts = points || [];
  if (!pts.length) { showToast('没有可导出的数据', 'warning'); return; }
  const rows = [['id', 'redshift', valKey]];
  for (const p of pts) rows.push([p.tid, p.z ?? '', p[valKey]]);
  downloadCsv(`host_${name}_${dateTag()}.csv`, rows);
}

function downloadZScatterCsv(points, valKey, name) {
  const pts = (points || []).filter(p => p.z != null && isFinite(p.z) && p[valKey] != null);
  if (!pts.length) { showToast('没有可导出的数据点（需宿主红移）', 'warning'); return; }
  const rows = [['id', 'redshift', valKey]];
  for (const p of pts) rows.push([p.tid, p.z, p[valKey]]);
  downloadCsv(`host_${name}_z_${dateTag()}.csv`, rows);
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
              <div class="d-flex align-items-center gap-1" title="绘制等视星等参考虚线：M(z) = m − μ(z)，Planck18">
                <div class="form-check form-check-inline mb-0">
                  <input class="form-check-input" type="checkbox" id="hostAbsLimitMagOn">
                  <label class="form-check-label small" for="hostAbsLimitMagOn">等星等线</label>
                </div>
                <span class="small text-secondary">m=</span>
                <input type="number" class="form-control form-control-sm" id="hostAbsLimitMag"
                  step="0.1" style="width:80px" placeholder="如 24" disabled>
              </div>
              <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hostAbsMagCsv"
                title="下载当前勾选波段的绘图数据（CSV，含银消改正前后星等）">
                <i class="bi bi-download"></i> 数据
              </button>
            </div>
          </div>
          <div class="card-body">
            <div class="chart-container" style="height:420px"><canvas id="hostAbsMagChart"></canvas></div>
            <div id="hostAbsBandPanel" class="d-flex flex-wrap gap-2 align-items-center mt-2 small"></div>
          </div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span>恒星质量 M* 分布（M☉，对数分箱）</span>
            <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hostMstarHistCsv"
              title="下载原始数据（每宿主一行）"><i class="bi bi-download"></i> 数据</button>
          </div>
          <div class="card-body"><div class="chart-container" style="height:320px"><canvas id="hostMstarHist"></canvas></div></div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span>恒星形成率 SFR 分布（M☉/yr，对数分箱）</span>
            <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hostSfrHistCsv"
              title="下载原始数据（每宿主一行）"><i class="bi bi-download"></i> 数据</button>
          </div>
          <div class="card-body"><div class="chart-container" style="height:320px"><canvas id="hostSfrHist"></canvas></div></div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span>恒星质量 — 红移</span>
            <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hostMstarZCsv"
              title="下载绘图数据（需宿主红移）"><i class="bi bi-download"></i> 数据</button>
          </div>
          <div class="card-body"><div class="chart-container" style="height:320px"><canvas id="hostMstarZ"></canvas></div></div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span>恒星形成率 — 红移</span>
            <button class="btn btn-sm btn-outline-secondary py-0 px-1" id="hostSfrZCsv"
              title="下载绘图数据（需宿主红移）"><i class="bi bi-download"></i> 数据</button>
          </div>
          <div class="card-body"><div class="chart-container" style="height:320px"><canvas id="hostSfrZ"></canvas></div></div>
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

  // ── 参数 — 红移散点图 ──
  _mstarPoints = st.m_star_points || [];
  _sfrPoints = st.sfr_points || [];
  buildZScatter('hostMstarZ', _mstarPoints, 'm_star', 'M* (M☉)', '#58a6ff');
  buildZScatter('hostSfrZ', _sfrPoints, 'sfr', 'SFR (M☉/yr)', '#3fb950');

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
  const limitMagInput = document.getElementById('hostAbsLimitMag');
  document.getElementById('hostAbsLimitMagOn')?.addEventListener('change', (e) => {
    if (limitMagInput) limitMagInput.disabled = !e.target.checked;
    buildAbsMagChart();
  });
  limitMagInput?.addEventListener('input', buildAbsMagChart);

  // ── CSV 下载按钮 ──
  document.getElementById('hostAbsMagCsv')?.addEventListener('click', downloadAbsMagCsv);
  document.getElementById('hostMstarHistCsv')?.addEventListener('click',
    () => downloadPointsCsv(_mstarPoints, 'm_star', 'mstar'));
  document.getElementById('hostSfrHistCsv')?.addEventListener('click',
    () => downloadPointsCsv(_sfrPoints, 'sfr', 'sfr'));
  document.getElementById('hostMstarZCsv')?.addEventListener('click',
    () => downloadZScatterCsv(_mstarPoints, 'm_star', 'mstar'));
  document.getElementById('hostSfrZCsv')?.addEventListener('click',
    () => downloadZScatterCsv(_sfrPoints, 'sfr', 'sfr'));
}
