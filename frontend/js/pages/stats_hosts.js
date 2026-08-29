// === 宿主星系统计子页（#/stats/hosts） ===
// 数据源 GET /api/stats/hosts：{n_hosts, n_with_spec_z, n_with_phot_z,
//   m_star: [...], sfr: [...], z_pairs: [{t_z, h_z}], coverage: 0.xx}
// 字段缺失时相应卡片/图显示「暂无数据」。
import { app, showLoading, showError, statsTabs } from './layout.js';
import { getHostStats, getOverview } from '../api.js';
import { chartColors } from '../theme.js';

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

function makeZScatter(canvasId, pairs) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const pts = (pairs || []).filter(p => isFinite(p.t_z) && isFinite(p.h_z));
  if (typeof Chart === 'undefined' || !pts.length) {
    canvas.parentElement.innerHTML = '<div class="text-secondary small p-3">暂无数据</div>';
    return;
  }
  const cc = chartColors();
  const zMax = Math.max(...pts.map(p => Math.max(p.t_z, p.h_z))) * 1.1;
  new Chart(canvas, {
    type: 'scatter',
    data: {
      datasets: [
        {
          label: '宿主 z vs 暂现源 z',
          data: pts.map(p => ({ x: p.t_z, y: p.h_z })),
          backgroundColor: '#58a6ff', pointRadius: 3.5,
        },
        {
          label: 'y = x',
          data: [{ x: 0, y: 0 }, { x: zMax, y: zMax }],
          type: 'line', borderColor: '#8b949e', borderDash: [6, 4],
          borderWidth: 1, pointRadius: 0, fill: false,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: cc.legend, boxWidth: 12 } } },
      scales: {
        x: { title: { display: true, text: '暂现源红移', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid }, min: 0 },
        y: { title: { display: true, text: '宿主红移', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid }, min: 0 },
      },
    },
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
      <div class="col-md-8 offset-md-2">
        <div class="card">
          <div class="card-header">宿主红移 vs 暂现源红移</div>
          <div class="card-body"><div class="chart-container" style="height:380px"><canvas id="hostZScatter"></canvas></div></div>
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
  makeZScatter('hostZScatter', st.z_pairs);
}
