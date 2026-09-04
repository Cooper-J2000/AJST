// === Statistics Page ===
import { app, showLoading, showError, statsTabs } from './layout.js';
import { getOverview, getRedshiftDist, getBandCoverage, getTransientMeta, getFilters } from '../api.js';
import { chartColors } from '../theme.js';

// ─── 工具 ───
const C = 2.998e8;
const SQRT2 = Math.SQRT2 || 1.41421356237;
const TWO_SQRT2 = 2 * SQRT2;
function normBand(b) { return b.replace(/(\d+)\.0(?=[A-Za-z])/g, '$1'); }
// 波段名变体归一（R_{c}→R、r'→r、UVW2→uvot-uvw2、UVOT-white→uvot-white 等）
function normalizeBandForWl(b, wlMap) {
  const nb = normBand(b);
  if (wlMap[nb] != null) return nb;
  const base = nb.replace(/_{.*}$/, '').replace(/'+$/, '');
  if (wlMap[base] != null) return base;
  const base2 = nb.split('_')[0];
  if (base2 && wlMap[base2] != null) return base2;
  const low = nb.toLowerCase();
  if (wlMap[low] != null) return low;
  const uvot = 'uvot-' + low;
  if (wlMap[uvot] != null) return uvot;
  return nb;
}
function bandToWl(b, wlMap) {
  const nb = normalizeBandForWl(b, wlMap);
  if (wlMap[nb] != null) return wlMap[nb];
  const keV = nb.match(/^(\d+(?:\.\d+)?)\s*keV$/i);
  if (keV) return 12.3984 / parseFloat(keV[1]);
  const GHz = nb.match(/^(\d+(?:\.\d+)?)\s*GHz$/i);
  if (GHz) return 2.99792458e9 / parseFloat(GHz[1]);
  return null;
}

function sciFmt(v) {
  if (v == null || !isFinite(v)) return '0';
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e-2 && a < 1e4) return v.toFixed(1);
  return v.toExponential(2);
}

// Mollweide 投影: RA(deg), Dec(deg) → {x,y} ∈ [-2√2,2√2]×[-√2,√2]
function mollweide(raDeg, decDeg) {
  if (raDeg == null || decDeg == null) return null;
  const ra = raDeg * Math.PI / 180;
  const dec = decDeg * Math.PI / 180;
  let raNorm = ra;
  while (raNorm > Math.PI) raNorm -= 2 * Math.PI;
  while (raNorm < -Math.PI) raNorm += 2 * Math.PI;
  const target = Math.PI * Math.sin(dec);
  let theta = dec;
  for (let iter = 0; iter < 30; iter++) {
    const sin2t = Math.sin(2 * theta);
    const f = 2 * theta + sin2t - target;
    if (Math.abs(f) < 1e-12) break;
    const fp = 4 * Math.cos(theta) * Math.cos(theta);
    if (fp === 0) break;
    theta -= f / fp;
  }
  let x = (TWO_SQRT2 / Math.PI) * raNorm * Math.cos(theta);
  let y = SQRT2 * Math.sin(theta);
  return { x, y };
}

function buildSpectralColors(bandNames, wlMap) {
  // 按频率排序序号归一化映射色阶（与详情页一致：低频=红，高频=蓝，
  // 避免频率跨度大时颜色挤在一端）
  const withFreq = [], withoutFreq = [];
  for (const b of bandNames) {
    const wl = bandToWl(b, wlMap);
    if (wl && wl > 0) withFreq.push([b, C / (wl * 1e-10)]);
    else withoutFreq.push(b);
  }
  withFreq.sort((a, b) => a[1] - b[1]);
  const result = {};
  const n = withFreq.length;
  withFreq.forEach(([b], i) => {
    const t = n > 1 ? i / (n - 1) : 0.5;
    const light = n > 8 && i % 2 === 1 ? 65 : 50;
    result[b] = `hsl(${t * 240}, 75%, ${light}%)`;
  });
  const def = ['#58a6ff','#3fb950','#d29922','#f85149','#bc8cff','#56d4dd'];
  withoutFreq.forEach((b, i) => { result[b] = def[i % def.length]; });
  return result;
}

// ─── 纯 Canvas 全天图绘制 ───
function drawSkyChart(canvas, sources, tagColors) {
  const cc = chartColors();
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  // 计算天球椭圆在画布中的尺寸（留边距给标签）
  const margin = 55;
  const plotW = W - margin * 2;
  const plotH = H - margin * 2;
  // Mollweide: x/y 比例 = 2:1
  const mwAspect = TWO_SQRT2 / SQRT2; // = 2
  let rx, ry;
  if (plotW / plotH > mwAspect) {
    ry = plotH / 2;
    rx = ry * mwAspect;
  } else {
    rx = plotW / 2;
    ry = rx / mwAspect;
  }
  const cx = W / 2;
  const cy = H / 2 + 5;

  // 辅助：Mollweide 坐标 → 画布像素
  function toPx(x, y) {
    return {
      x: cx + (x / TWO_SQRT2) * rx,
      y: cy + (y / SQRT2) * ry,
    };
  }

  // 清除
  ctx.clearRect(0, 0, W, H);

  // ── 裁剪蒙版（数据点不超出椭圆） ──
  ctx.save();
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx, ry, 0, 0, 2 * Math.PI);
  ctx.clip();

  // ── 绘制 Dec 纬线 ──
  ctx.strokeStyle = cc.grid;
  ctx.lineWidth = 0.5;
  for (const dec of [-80, -60, -40, -20, 0, 20, 40, 60, 80]) {
    const pts = [];
    for (let ra = 0; ra < 360; ra += 1) {
      const p = mollweide(ra, dec);
      if (p) pts.push(toPx(p.x, p.y));
    }
    if (pts.length > 2) {
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.stroke();
    }
  }

  // ── 绘制 RA 经线（每 2h） ──
  ctx.strokeStyle = cc.grid;
  ctx.lineWidth = 0.5;
  for (const h of [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]) {
    const raDeg = h * 15;
    const pts = [];
    for (let dec = -88; dec <= 88; dec += 1) {
      const p = mollweide(raDeg, dec);
      if (p) pts.push(toPx(p.x, p.y));
    }
    if (pts.length > 2) {
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.stroke();
    }
  }

  // ── 绘制数据点 ──
  const defColors = ['#e74c3c','#3498db','#2ecc71','#9b59b6','#58a6ff','#f39c12','#1abc9c'];
  for (const s of sources) {
    const p = mollweide(s.ra, s.dec);
    if (!p) continue;
    const pp = toPx(p.x, p.y);
    const tag = (s.tags && s.tags.length > 0) ? s.tags[0] : '__none__';
    const color = tagColors[tag] || defColors[Object.keys(tagColors).indexOf(tag) % defColors.length] || '#58a6ff';
    ctx.beginPath();
    ctx.arc(pp.x, pp.y, 3.5, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
  }

  ctx.restore(); // 移除 clip

  // ── 边界椭圆 ──
  ctx.strokeStyle = cc.tick;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx, ry, 0, 0, 2 * Math.PI);
  ctx.stroke();

  // ── RA 标签 ──
  ctx.fillStyle = cc.tick;
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const h of [22, 20, 18, 16, 14, 12, 10, 8, 6, 4, 2, 0]) {
    const p = mollweide(h * 15, 0);
    if (!p) continue;
    const pp = toPx(p.x, p.y);
    if (pp.x >= margin && pp.x <= W - margin) {
      ctx.fillText(`${h}h`, pp.x, cy + ry + 6);
    }
  }
  ctx.fillStyle = cc.tickSub;
  ctx.font = '10px sans-serif';
  ctx.fillText('→ RA', cx + rx + 16, cy + ry - 10);

  // ── Dec 标签 ──
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const dec of [-80, -60, -40, -20, 0, 20, 40, 60, 80]) {
    const p = mollweide(0, dec);
    if (!p) continue;
    const pp = toPx(p.x, p.y);
    if (pp.y >= margin && pp.y <= H - margin) {
      ctx.fillText(`${dec}°`, cx - rx - 8, pp.y);
    }
  }
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  ctx.fillStyle = cc.tickSub;
  ctx.font = '10px sans-serif';
  ctx.fillText('Dec', cx - rx - 4, cy - ry - 2);
}


// ─── 主渲染 ───
export async function render() {
  showLoading();
  app.innerHTML = `
    <div class="page-header d-flex justify-content-between align-items-center">
      <h4 class="mb-0"><i class="bi bi-bar-chart-line"></i> 全局统计</h4>
    </div>
    ${statsTabs('overview')}
    <div class="row g-3 mb-4" id="overviewCards"></div>

    <!-- 全天分布（纯 Canvas） -->
    <div class="card mb-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-globe2"></i> 全天分布（Mollweide 投影）</span>
        <div>
          <select class="form-select form-select-sm" style="width:auto;display:inline-block" id="skyTagFilter" onchange="redrawSky()">
            <option value="__all__">全部标签</option>
          </select>
        </div>
      </div>
      <div class="card-body p-0">
        <canvas id="skyCanvas" style="width:100%;height:500px;display:block"></canvas>
      </div>
    </div>

    <div class="row g-3">
      <div class="col-md-6">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span>红移分布</span>
            <div>
              <label class="small text-secondary me-1">标签:</label>
              <select class="form-select form-select-sm" style="width:120px;display:inline-block" id="zTagFilter" onchange="redrawZHist()">
                <option value="__all__">全部</option>
              </select>
              <label class="small text-secondary me-1 ms-2">Bins:</label>
              <input type="number" class="form-control form-control-sm" style="width:70px;display:inline-block" id="zBinCount" value="20" min="3" max="100" onchange="redrawZHist()">
            </div>
          </div>
          <div class="card-body"><div class="chart-container" style="height:350px"><canvas id="zHistogram"></canvas></div></div>
          <div class="card-footer small text-secondary" id="zStats"></div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card">
          <div class="card-header">波段覆盖</div>
          <div class="card-body"><div class="chart-container" style="height:350px"><canvas id="bandChart"></canvas></div></div>
        </div>
      </div>
    </div>
  `;

  try {
    const [overview, redshifts, bandData, transients, filters] = await Promise.all([
      getOverview(), getRedshiftDist(), getBandCoverage(),
      getTransientMeta(),
      getFilters(),
    ]);

    const wlMap = {};
    for (const f of filters) wlMap[f.id] = f.wavelength;

    // ── 概览卡片 ──
    document.getElementById('overviewCards').innerHTML = `
      <div class="col-md-3 col-6"><div class="card"><div class="card-body stat-card"><div class="stat-value">${overview.n_transients}</div><div class="stat-label">暂现源</div></div></div></div>
      <div class="col-md-3 col-6"><div class="card"><div class="card-body stat-card"><div class="stat-value">${overview.n_lightcurves}</div><div class="stat-label">光变数据点</div></div></div></div>
      <div class="col-md-3 col-6"><div class="card"><div class="card-body stat-card"><div class="stat-value">${overview.n_with_redshift}</div><div class="stat-label">已知红移</div></div></div></div>
      <div class="col-md-3 col-6"><div class="card"><div class="card-body stat-card"><div class="stat-value">${overview.n_telescopes}</div><div class="stat-label">望远镜</div></div></div></div>
    `;

    // ── 红移分布（支持按 tag 筛选） ──
    const transientsWithZ = transients.items.filter(t => t.redshift != null);
    const zTagSet = new Set();
    transientsWithZ.forEach(t => (t.tags || []).forEach(tag => zTagSet.add(tag)));
    const zTagSelect = document.getElementById('zTagFilter');
    if (zTagSelect) {
      [...zTagSet].sort().forEach(tag => {
        zTagSelect.innerHTML += `<option value="${tag}">${tag}</option>`;
      });
    }
    let zHistChart = null;
    window.redrawZHist = () => {
      if (typeof Chart === 'undefined') return;
      const canvas = document.getElementById('zHistogram');
      if (!canvas) return;
      if (zHistChart) { zHistChart.destroy(); zHistChart = null; }
      const tagFilter = document.getElementById('zTagFilter')?.value || '__all__';
      let zValues;
      if (tagFilter === '__all__') {
        zValues = transientsWithZ.map(t => t.redshift);
      } else {
        zValues = transientsWithZ.filter(t => (t.tags || []).includes(tagFilter)).map(t => t.redshift);
      }
      if (!zValues.length) return;
      const nBins = Math.max(3, Math.min(100, parseInt(document.getElementById('zBinCount')?.value) || 20));
      const min = Math.min(...zValues), max = Math.max(...zValues);
      const binW = (max - min) / nBins;
      const hist = new Array(nBins).fill(0);
      for (const z of zValues) {
        const idx = Math.min(Math.floor((z - min) / binW), nBins - 1);
        hist[idx]++;
      }
      const labels = hist.map((_, i) => `${(min + i * binW).toFixed(2)}-${(min + (i + 1) * binW).toFixed(2)}`);
      const cc = chartColors();
      zHistChart = new Chart(canvas, {
        type: 'bar',
        data: { labels, datasets: [{ label: '红移分布', data: hist, backgroundColor: '#58a6ff', borderColor: '#1f6feb', borderWidth: 1 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { ticks: { color: cc.tick, maxRotation: 90, font: { size: 10 } }, grid: { color: cc.grid } }, y: { ticks: { color: cc.tick }, grid: { color: cc.grid } } },
        },
      });
      const sorted = [...zValues].sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      document.getElementById('zStats').textContent = `范围: ${min.toFixed(3)} ~ ${max.toFixed(3)}, 中位数: ${median.toFixed(3)}, 显示: ${zValues.length}/${transientsWithZ.length}`;
    };
    window.redrawZHist();

    // ── 波段覆盖 ──
    if (typeof Chart !== 'undefined') {
      // 先按数据点数取前 40（bandData 来自 API 已按 count 降序），再按频率排序展示
      const topBands = bandData.slice(0, 40);
      const colors = buildSpectralColors(topBands.map(b => b.band), wlMap);
      const sortedBands = [...topBands].sort((a, b) => {
        const wa = bandToWl(a.band, wlMap), wb = bandToWl(b.band, wlMap);
        if (wa && wb) return (C / (wa * 1e-10)) - (C / (wb * 1e-10));
        if (wa) return -1; if (wb) return 1;
        return a.band.localeCompare(b.band);
      });
      const cc = chartColors();
      new Chart(document.getElementById('bandChart'), {
        type: 'bar',
        data: {
          labels: sortedBands.map(b => b.band),
          datasets: [{
            label: '数据点数', data: sortedBands.map(b => b.count),
            backgroundColor: sortedBands.map(b => colors[b.band] || '#58a6ff'),
            borderColor: sortedBands.map(b => (colors[b.band] || '#58a6ff').replace('55%', '40%')),
            borderWidth: 1,
          }],
        },
        options: {
          indexAxis: 'y', responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { type: 'logarithmic', min: 1, ticks: { color: cc.tick, callback: v => sciFmt(v) }, grid: { color: cc.grid } },
            y: { ticks: { color: cc.tick, font: { size: 11 } }, grid: { color: cc.grid } },
          },
        },
      });
    }

    // ── 全天分布（纯 Canvas） ──
    const tagColors = { fxt: '#e74c3c', grb: '#3498db', sn: '#2ecc71', tde: '#9b59b6' };
    const allTags = new Set();
    for (const t of transients.items) {
      if (t.tags) for (const tag of t.tags) allTags.add(tag);
    }
    const tagSelect = document.getElementById('skyTagFilter');
    for (const tag of [...allTags].sort()) {
      const opt = document.createElement('option');
      opt.value = tag; opt.textContent = tag;
      tagSelect.appendChild(opt);
    }

    // 获取所有有坐标的源
    const allSources = transients.items.filter(t => t.ra != null && t.dec != null);

    window.redrawSky = () => {
      const selectedTag = document.getElementById('skyTagFilter')?.value || '__all__';
      const filtered = selectedTag === '__all__'
        ? allSources
        : allSources.filter(t => t.tags && t.tags.includes(selectedTag));
      const canvas = document.getElementById('skyCanvas');
      if (canvas) drawSkyChart(canvas, filtered, tagColors);
    };

    // 首次绘制 + 窗口缩放重绘
    window.redrawSky();
    window.addEventListener('resize', () => {
      clearTimeout(window._skyResizeTimer);
      window._skyResizeTimer = setTimeout(window.redrawSky, 200);
    });

  } catch (err) {
    showError(`加载统计数据失败: ${err.message}`);
  }
}
