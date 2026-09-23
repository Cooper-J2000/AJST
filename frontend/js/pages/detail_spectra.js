// === 光谱数据标签页（详情页内嵌：单条/多条、绝对/归一化流量、双横轴） ===
// 由 detail.js 在切换到「光谱数据」标签时调用 initSpectraTab(tid, redshift)，
// 页面重建前调用 resetSpectra() 销毁图表、清加载标记。
import {
  getSpectra, getSpectrum, uploadSpectrum, updateSpectrum, deleteSpectrumApi,
  correctSpectrumGext, isAuthed, isAdmin, showToast, API_BASE,
} from '../api.js';
import { SPEC_LINE_GROUPS, createSpecLinesPlugin, buildMarkingsPanelHTML } from '../spec_lines.js';
import { dragRectPlugin, attachDragZoom } from '../dragzoom.js';
import { esc, escAttr, sciFormat } from '../utils.js';
import { currentTid } from './detail.js';

let specChartInstance = null;
const specChartHolder = { chart: null };  // 框选缩放用的当前图表引用
// 光谱图手动坐标范围（null = 该端自动；x 为观测者系波长 Å）
let specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
// 光谱图坐标轴类型（linear / logarithmic）
let specAxisType = { x: 'linear', y: 'linear' };
// detail.js 模板渲染时读取当前轴类型（跨 render 保持用户选择）
export function getSpecAxisType() { return specAxisType; }
// TNS 风格谱线标记状态：组key → {on, z, v, wl}
let _specMarkings = {};
function resetSpecMarkings() {
  _specMarkings = {};
  for (const g of SPEC_LINE_GROUPS) _specMarkings[g.key] = { on: false, z: _specZ || 0, v: 0, wl: null };
}
const specLinesPlugin = createSpecLinesPlugin(() => _specMarkings);
let _spectraLoadedFor = null;
let _specMode = 'absolute';            // 'absolute' | 'relative'
let _specZ = null;                     // 当前源红移（用于静止系横轴）
const _specSelected = new Set();   // 选中的 spectrum id
const _specCache = new Map();      // id → {meta, objName, sp, pts, errs}
const _specOffsets = {};           // 相对流量模式下每条光谱的纵向偏移
const _specListMeta = new Map();   // id → {flux_type, mjd}
const SPEC_COLORS = ['#79b8ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#56d4dd', '#ff7b72', '#e3b341'];
// 波长类型显示名（null=留空，按空气波长处理但不显示为空气）
const WAVE_TYPE_LABELS = { vacuum: '真空', air: '空气' };
const waveTypeLabel = (wt) => WAVE_TYPE_LABELS[wt] || '-';

// 光谱误差条插件（绝对流量模式 + 开关开启时）
let _specShowErr = true;
window.toggleSpecErr = (on) => { _specShowErr = on; renderSpectraPlot(); };

const specErrBarPlugin = {
  id: 'specErrBar',
  afterDatasetsDraw(chart) {
    if (!_specShowErr) return;
    const { ctx, scales: { x: xs, y: ys } } = chart;
    chart.data.datasets.forEach((ds, di) => {
      if (!ds._errs) return;
      const meta = chart.getDatasetMeta(di);
      ctx.save();
      ctx.strokeStyle = ds.borderColor || '#fff';
      ctx.lineWidth = 0.8;
      ds._errs.forEach((e, i) => {
        if (e == null || e <= 0) return;
        const el = meta.data[i];
        if (!el || el.skip || !el.parsed) return;
        const yT = ys.getPixelForValue(el.parsed.y + e);
        const yB = ys.getPixelForValue(el.parsed.y - e);
        if (!isFinite(yT) || !isFinite(yB)) return;
        ctx.beginPath(); ctx.moveTo(el.x, yT); ctx.lineTo(el.x, yB); ctx.stroke();
      });
      ctx.restore();
    });
  },
};

// 静止系波长顶部副轴插件：每帧直接从主横轴刻度换算 λ/(1+z) 绘制，保证严格实时对应
const restAxisPlugin = {
  id: 'restAxis',
  afterDraw(chart) {
    const z = _specZ;
    if (!z || z <= 0) return;
    const x = chart.scales.x;
    if (!x) return;
    const area = chart.chartArea;
    if (!area) return;
    const ctx = chart.ctx;
    const fmt = (v) => {
      const a = Math.abs(v);
      if (a === 0) return '0';
      if (a >= 1e4 || a < 1e-2) return v.toExponential(1);
      return Number(v.toPrecision(4)).toString();
    };
    ctx.save();
    ctx.strokeStyle = '#8b949e';
    ctx.fillStyle = '#8b949e';
    // 轴线
    ctx.beginPath();
    ctx.moveTo(area.left, area.top);
    ctx.lineTo(area.right, area.top);
    ctx.stroke();
    // 刻度（与观测者系主横轴刻度逐点对应）
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    for (const t of x.ticks) {
      const px = x.getPixelForValue(t.value);
      if (px < area.left - 1 || px > area.right + 1) continue;
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.top - 5);
      ctx.stroke();
      ctx.fillText(fmt(t.value / (1 + z)), px, area.top - 7);
    }
    // 标题
    ctx.fillText('静止系波长 (Å)', (area.left + area.right) / 2, area.top - 22);
    ctx.restore();
  },
};

// detail.js render() 重建 DOM 前调用：销毁图表、清加载标记与手动坐标范围
export function resetSpectra() {
  if (specChartInstance) { try { specChartInstance.destroy(); } catch {} specChartInstance = null; }
  _spectraLoadedFor = null;
  specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
}

export async function initSpectraTab(tid, redshift) {
  if (_spectraLoadedFor === tid) return;
  _spectraLoadedFor = tid;
  _specZ = redshift || null;
  specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  resetSpecMarkings();
  const mkBody = document.getElementById('specMarkingsBody');
  if (mkBody) mkBody.innerHTML = buildMarkingsPanelHTML(_specZ);
  _specSelected.clear();
  _specCache.clear();
  _specListMeta.clear();
  for (const k of Object.keys(_specOffsets)) delete _specOffsets[k];
  const tbody = document.getElementById('spectraListBody');
  try {
    const list = await getSpectra(tid);
    if (currentTid !== tid || !tbody.isConnected) {   // 请求期间已切换源/页面重建
      if (_spectraLoadedFor === tid) _spectraLoadedFor = null;
      return;
    }
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-secondary py-3">暂无光谱数据</td></tr>';
      document.getElementById('specTitle').textContent = '光谱';
      document.getElementById('specMeta').textContent = '';
      return;
    }
    const admin = isAdmin();
    const authed = isAuthed();
    // 父子分组渲染：原始谱正常行，其改正子谱缩进紧随（缺失父的孤儿子谱排最后）
    const childrenByParent = new Map();
    list.filter(s => s.parent_id).forEach(s => {
      if (!childrenByParent.has(s.parent_id)) childrenByParent.set(s.parent_id, []);
      childrenByParent.get(s.parent_id).push(s);
    });
    const ordered = [];
    list.filter(s => !s.parent_id).forEach(p => {
      ordered.push(p);
      (childrenByParent.get(p.id) || []).forEach(c => ordered.push(c));
    });
    list.filter(s => s.parent_id && !list.some(p => p.id === s.parent_id))
      .forEach(s => ordered.push(s));
    tbody.innerHTML = ordered.map(s => {
      const ft = (s.extra_data && s.extra_data.flux_type) || 'absolute';
      const mjd = (s.extra_data && s.extra_data.mjd) || (s.observation_date ? s.observation_date.substring(0, 10) : '-');
      const remarks = (s.extra_data && s.extra_data.remarks) || '';
      const isChild = !!s.parent_id;
      _specListMeta.set(s.id, { flux_type: ft, mjd, parent_id: s.parent_id || null });
      return `
      <tr class="row-link" id="specRow_${s.id}" onclick="toggleSpectrum(${s.id})">
        <td>${isChild ? '<span class="text-secondary" style="padding-left:2px">└</span> ' : ''}<i class="bi bi-check-lg text-primary" id="specChk_${s.id}" style="visibility:hidden"></i> ${esc(mjd)}</td>
        <td>${esc(s.instrument) || '-'} ${ft === 'normalized' ? '<span class="badge-tag" style="background:rgba(210,153,34,0.15);color:#d29922" title="归一化流量">归一</span>' : ''}${isChild ? ` <span class="badge-tag" style="background:rgba(86,212,221,0.15);color:#56d4dd" title="银河系消光改正谱：E(B-V)=${s.gext_ebv != null ? Number(s.gext_ebv).toFixed(4) : '?'}，Rv=3.1（CSFD 尘埃图 + Pei1992），源文件 ${escAttr(s.filename)}">银消改正</span>` : ''}</td>
        <td onclick="event.stopPropagation()">${admin
          ? `<select class="form-select form-select-sm spec-type-sel" style="width:auto;font-size:0.8rem;padding:1px 4px" onchange="specTypeChange(${s.id}, this.value)">
              ${['transient','host','mix'].map(v => `<option value="${v}" ${((s.spec_type||'transient')===v)?'selected':''}>${({transient:'Transient',host:'Host',mix:'Mix'})[v]}</option>`).join('')}
            </select>`
          : (({transient:'Transient',host:'Host',mix:'Mix'})[s.spec_type] || 'Transient')}</td>
        <td onclick="event.stopPropagation()">${admin
          ? `<select class="form-select form-select-sm spec-wave-type-sel" style="width:auto;font-size:0.8rem;padding:1px 4px" onchange="specWaveTypeChange(${s.id}, this.value)">
              <option value="" ${!s.wavelength_type ? 'selected' : ''}>留空</option>
              ${['vacuum','air'].map(v => `<option value="${v}" ${s.wavelength_type===v?'selected':''}>${WAVE_TYPE_LABELS[v]}</option>`).join('')}
            </select>`
          : waveTypeLabel(s.wavelength_type)}</td>
        <td class="small">${esc((s.extra_data && s.extra_data.observer)) || '-'}</td>
        <td class="text-nowrap" onclick="event.stopPropagation()">
          ${remarks ? `<button class="btn btn-sm btn-outline-info py-0 px-1" title="${escAttr(remarks)}" onclick="toggleSpecRemarks(${s.id})"><i class="bi bi-info-circle"></i></button>` : ''}
          <input type="number" class="form-control form-control-sm d-inline-block spec-offset" data-id="${s.id}"
                 style="width:60px;display:${_specMode === 'relative' ? 'inline-block' : 'none'};font-size:0.75rem;padding:1px 4px"
                 step="0.1" value="${_specOffsets[s.id] || 0}" title="纵向偏移（相对流量模式）" onchange="setSpecOffset(${s.id}, this.value)">
          <a class="btn btn-sm btn-outline-secondary py-0 px-1" href="${API_BASE}/spectra/${s.id}/download" download
             title="下载光谱文本（波长Å 流量 [误差]）" onclick="event.stopPropagation()"><i class="bi bi-download"></i></a>
          ${authed && !isChild ? `<button class="btn btn-sm btn-outline-warning py-0 px-1" title="生成/重新生成银河系消光改正谱" onclick="gextCorrectSpectrum(${s.id})"><i class="bi bi-stars"></i></button>` : ''}
          ${admin ? `<button class="btn btn-sm btn-outline-danger py-0 px-1" title="删除该光谱" onclick="deleteSpectrum(${s.id})"><i class="bi bi-trash"></i></button>` : ''}
        </td>
      </tr>${remarks ? `
      <tr id="specRem_${s.id}" style="display:none">
        <td colspan="6" class="small text-secondary" style="white-space:normal"><i class="bi bi-chat-left-text"></i> ${escAttr(remarks)}</td>
      </tr>` : ''}`;
    }).join('');
    const firstParent = list.find(s => !s.parent_id);
    if (firstParent) toggleSpectrum(firstParent.id);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger py-3">加载失败: ${esc(err.message)}</td></tr>`;
  }
}

window.toggleSpecRemarks = (id) => {
  const el = document.getElementById(`specRem_${id}`);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
};

window.toggleSpectrum = async (id) => {
  if (!_specCache.has(id)) {
    try {
      const resp = await getSpectrum(id);
      const objName = Object.keys(resp.data)[0];
      const sp = resp.data[objName].spectra;
      _specCache.set(id, {
        meta: resp.meta, objName, sp,
        pts: sp.data.map(d => ({ x: Number(d[0]), y: Number(d[1]) }))
          .filter(d => isFinite(d.x) && isFinite(d.y)),
        errs: sp.data.map(d => (d.length > 2 && isFinite(Number(d[2]))) ? Number(d[2]) : null),
      });
    } catch (err) {
      showToast(`加载光谱失败: ${err.message}`, 'danger');
      return;
    }
  }
  if (_specSelected.has(id)) _specSelected.delete(id);
  else _specSelected.add(id);
  document.getElementById(`specChk_${id}`).style.visibility = _specSelected.has(id) ? 'visible' : 'hidden';
  document.getElementById(`specRow_${id}`)?.classList.toggle('table-active', _specSelected.has(id));
  renderSpectraPlot();
};

window.setSpecMode = (mode) => {
  _specMode = mode;
  document.querySelectorAll('.spec-offset').forEach(el => {
    el.style.display = mode === 'relative' ? 'inline-block' : 'none';
  });
  renderSpectraPlot();
};

window.setSpecOffset = (id, val) => {
  _specOffsets[id] = parseFloat(val) || 0;
  renderSpectraPlot();
};

window.specTypeChange = async (id, val) => {
  if (!isAdmin()) { showToast('仅管理员可修改光谱类型', 'warning'); return; }
  try {
    await updateSpectrum(id, { spec_type: val });
    showToast(`光谱类型已改为 ${({transient:'Transient',host:'Host',mix:'Mix'})[val] || val}`, 'success');
  } catch (err) {
    showToast(`修改失败: ${err.message}`, 'danger');
    _spectraLoadedFor = null;   // 重载列表以还原下拉显示
    initSpectraTab(currentTid);
  }
};

window.specWaveTypeChange = async (id, val) => {
  if (!isAdmin()) { showToast('仅管理员可修改波长类型', 'warning'); return; }
  try {
    await updateSpectrum(id, { wavelength_type: val || null });
    showToast(`波长类型已改为 ${val ? WAVE_TYPE_LABELS[val] : '留空'}`, 'success');
  } catch (err) {
    showToast(`修改失败: ${err.message}`, 'danger');
    _spectraLoadedFor = null;   // 重载列表以还原下拉显示
    initSpectraTab(currentTid);
  }
};

window.gextCorrectSpectrum = async (id) => {
  if (!isAuthed()) { showToast('请登录后执行消光改正', 'warning'); return; }
  const hasChild = [..._specListMeta.values()].some(m => m.parent_id === id);
  if (hasChild && !confirm('已存在银河系消光改正谱，从原始光谱重新生成并覆盖？')) return;
  try {
    const resp = await correctSpectrumGext(id);
    let msg = `银河系消光改正完成：E(B-V)=${resp.ebv != null ? Number(resp.ebv).toFixed(4) : '?'}，改正谱 ${resp.spectrum.filename}`;
    showToast(resp.warnings && resp.warnings.length ? `${msg}；注意：${resp.warnings.join('；')}` : msg,
      resp.warnings && resp.warnings.length ? 'warning' : 'success');
    _spectraLoadedFor = null;   // 重载列表以显示改正子谱
    initSpectraTab(currentTid, _specZ);
  } catch (err) {
    showToast(`改正失败: ${err.message}`, 'danger');
  }
};

window.deleteSpectrum = async (id) => {
  if (!isAdmin()) { showToast('仅管理员可删除数据', 'warning'); return; }
  const m = _specCache.get(id);
  const name = m ? `${m.objName} ${m.meta.filename}` : `#${id}`;
  const childIds = [..._specListMeta.entries()].filter(([, v]) => v.parent_id === id).map(([k]) => k);
  const cascadeHint = childIds.length ? '，其银河系消光改正谱将一并删除' : '';
  if (!confirm(`确认删除光谱 ${name}？（数据库记录与文件一并删除${cascadeHint}）`)) return;
  try {
    await deleteSpectrumApi(id);
    showToast('已删除', 'success');
    for (const rid of [id, ...childIds]) {
      _specSelected.delete(rid);
      _specCache.delete(rid);
      _specListMeta.delete(rid);
      document.getElementById(`specRow_${rid}`)?.remove();
      document.getElementById(`specRem_${rid}`)?.remove();
    }
    renderSpectraPlot();
    if (!document.querySelector('#spectraListBody tr.row-link')) {
      _spectraLoadedFor = null;
      initSpectraTab(currentTid, _specZ);
    }
  } catch (err) {
    showToast(`删除失败: ${err.message}`, 'danger');
  }
};

function _median(arr) {
  const a = arr.filter(v => isFinite(v)).sort((x, y) => x - y);
  if (!a.length) return 1;
  const m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}

function renderSpectraPlot() {
  const selected = [..._specSelected];
  const titleEl = document.getElementById('specTitle');
  const metaEl = document.getElementById('specMeta');
  const datasets = [];
  let skippedNorm = 0;

  selected.forEach((id, i) => {
    const c = _specCache.get(id);
    if (!c) return;
    const ft = (_specListMeta.get(id) || {}).flux_type || 'absolute';
    const color = SPEC_COLORS[i % SPEC_COLORS.length];
    const label = `${c.objName} · ${c.meta.filename} · MJD ${c.sp.time || '-'}`;
    if (_specMode === 'absolute') {
      if (ft !== 'absolute') { skippedNorm++; return; }
      datasets.push({
        label, data: c.pts, borderColor: color, backgroundColor: color,
        borderWidth: 1.2, pointRadius: 0, tension: 0.15,
        _errs: c.errs.some(e => e != null) ? c.errs : null,
      });
    } else {
      // 相对流量：每条按自身中位数归一 + 用户偏移
      const med = _median(c.pts.map(p => p.y)) || 1;
      const off = _specOffsets[id] || 0;
      datasets.push({
        label: label + (off ? ` (偏移${off >= 0 ? '+' : ''}${off})` : ''),
        data: c.pts.map(p => ({ x: p.x, y: p.y / med + off })),
        borderColor: color, backgroundColor: color,
        borderWidth: 1.2, pointRadius: 0, tension: 0.15,
      });
    }
  });

  if (!selected.length) {
    titleEl.textContent = '光谱（未选择）';
    metaEl.textContent = '';
  } else if (_specMode === 'absolute') {
    titleEl.textContent = selected.length === 1
      ? `${_specCache.get(selected[0]).objName} — ${_specCache.get(selected[0]).meta.filename}`
      : `光谱对比（${datasets.length} 条，绝对流量）`;
    metaEl.textContent = '横轴为观测者系波长' + (_specZ ? `；上横轴为静止系 (z=${_specZ})` : '')
      + (skippedNorm ? `；${skippedNorm} 条归一化光谱未显示` : '');
  } else {
    titleEl.textContent = `光谱对比（${datasets.length} 条，归一化流量）`;
    metaEl.textContent = '横轴为观测者系波长；各谱按自身中位数归一，可用输入框调整纵向偏移';
  }

  const xLabel = '观测者系波长 (Å)';
  const yLabel = _specMode === 'absolute'
    ? (selected.length ? (_specCache.get(selected[0]).sp.u_fluxes || '') : '')
    : '归一化流量';
  plotSpectrum(datasets, xLabel, yLabel, datasets.length > 1 || _specMode === 'relative');
}

function plotSpectrum(datasets, xLabel, yLabel, showLegend) {
  const ctx = document.getElementById('specChart');
  if (!ctx || typeof Chart === 'undefined') return;
  if (specChartInstance) { specChartInstance.destroy(); specChartInstance = null; }
  // 对数 Y 轴：滤除非正流量点（对数轴无法表示），误差数组同步过滤保持索引对齐
  if (specAxisType.y === 'logarithmic') {
    datasets = datasets.map(ds => {
      const keep = ds.data.map((p, i) => (p.y > 0 ? i : -1)).filter(i => i >= 0);
      return {
        ...ds,
        data: keep.map(i => ds.data[i]),
        _errs: ds._errs ? keep.map(i => ds._errs[i]) : ds._errs,
      };
    });
  }
  specChartInstance = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    plugins: [specErrBarPlugin, specLinesPlugin, restAxisPlugin, dragRectPlugin],
    options: {
      responsive: true, maintainAspectRatio: false,
      layout: { padding: { top: 34 } },   // 给顶部静止系副轴留位
      plugins: {
        legend: { display: !!showLegend, position: 'bottom', labels: { color: '#8b949e', boxWidth: 12, font: { size: 10 } } },
        tooltip: { callbacks: { label: c => `${c.dataset.label ? c.dataset.label.split('·')[0].trim() + ' ' : ''}λ=${c.parsed.x.toFixed(1)} Å, F=${c.parsed.y.toExponential(2)}` } },
      },
      scales: {
        x: { type: specAxisType.x, position: 'bottom', title: { display: true, text: xLabel, color: '#8b949e' },
             grid: { color: '#30363d' }, ticks: { color: '#8b949e' },
             min: specAxisRange.xmin ?? undefined, max: specAxisRange.xmax ?? undefined },
        y: { type: specAxisType.y, title: { display: true, text: yLabel, color: '#8b949e' },
             grid: { color: '#30363d' }, ticks: { color: '#8b949e', callback: v => v.toExponential(1) },
             min: specAxisRange.ymin ?? undefined, max: specAxisRange.ymax ?? undefined },
      },
    },
  });
  specChartHolder.chart = specChartInstance;
  attachDragZoom(specChartHolder, ctx, (range) => {
    specAxisRange = range;
    const ids = { xmin: 'specAxXmin', xmax: 'specAxXmax', ymin: 'specAxYmin', ymax: 'specAxYmax' };
    for (const [k, id] of Object.entries(ids)) {
      const el = document.getElementById(id);
      if (el) el.value = sciFormat(range[k]);
    }
    const o = specChartInstance.options.scales;
    o.x.min = range.xmin; o.x.max = range.xmax;
    o.y.min = range.ymin; o.y.max = range.ymax;
    specChartInstance.update('none');
  }, { allowNonPositive: true });
}

// ─── 光谱图坐标轴类型（线性/对数） ───
window.setSpecAxisType = () => {
  const x = document.getElementById('specXTypeSel');
  const y = document.getElementById('specYTypeSel');
  specAxisType = {
    x: x ? x.value : 'linear',
    y: y ? y.value : 'linear',
  };
  renderSpectraPlot();
};

// ─── 光谱图坐标范围（输入框应用 / 恢复默认） ───
window.applySpecAxisRange = () => {
  const v = (id) => {
    const el = document.getElementById(id);
    const n = parseFloat(el && el.value);
    return isFinite(n) ? n : null;
  };
  specAxisRange = { xmin: v('specAxXmin'), xmax: v('specAxXmax'), ymin: v('specAxYmin'), ymax: v('specAxYmax') };
  renderSpectraPlot();
};
window.resetSpecAxisRange = () => {
  specAxisRange = { xmin: null, xmax: null, ymin: null, ymax: null };
  ['specAxXmin', 'specAxXmax', 'specAxYmin', 'specAxYmax'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  renderSpectraPlot();
};

// ─── TNS 风格谱线标记面板事件 ───
window.specMarkingToggle = (key, on) => {
  if (!_specMarkings[key]) return;
  _specMarkings[key].on = on;
  if (specChartInstance) specChartInstance.update('none');
};
window.specMarkingSet = (key, field, val) => {
  if (!_specMarkings[key]) return;
  const n = parseFloat(val);
  _specMarkings[key][field] = isFinite(n) ? n : (field === 'wl' ? null : 0);
  if (specChartInstance) specChartInstance.update('none');
};
window.specMarkingStep = (field, val) => {
  const step = parseFloat(val);
  if (!isFinite(step) || step <= 0) return;
  document.querySelectorAll(`#specMarkingsBody .spec-mk-${field}`).forEach(el => { el.step = step; });
};

// ─── 光谱上传 ───
window.showSpecUpload = () => {
  if (!isAuthed()) {
    const modal = new bootstrap.Modal(document.getElementById('loginModal'));
    modal.show();
    return;
  }
  document.getElementById('specUpError').style.display = 'none';
  new bootstrap.Modal(document.getElementById('specUploadModal')).show();
};

window.doSpecUpload = async () => {
  const file = document.getElementById('specFile').files[0];
  const errEl = document.getElementById('specUpError');
  errEl.style.display = 'none';
  if (!file) {
    errEl.textContent = '请选择光谱文件';
    errEl.style.display = 'block';
    return;
  }
  const content = await file.text();
  const waveType = document.getElementById('specUploadWaveType').value;
  const btn = document.getElementById('specUpSubmit');
  btn.disabled = true;
  try {
    const resp = await uploadSpectrum({
      transient_id: currentTid,
      filename: file.name,
      content,
      instrument: document.getElementById('specUpInstrument').value.trim() || null,
      mjd: document.getElementById('specUpMjd').value.trim() || null,
      observer: document.getElementById('specUpObserver').value.trim() || null,
      reducer: document.getElementById('specUpReducer').value.trim() || null,
      flux_type: document.getElementById('specUpFluxType').value,
      spec_type: document.getElementById('specUpSpecType').value,
      ...(waveType ? { wavelength_type: waveType } : {}),
    });
    bootstrap.Modal.getInstance(document.getElementById('specUploadModal')).hide();
    showToast(`光谱已上传: ${resp.filename}（${resp.n_points} 点）`, 'success');
    _spectraLoadedFor = null;   // 强制重载列表
    _specSelected.clear();
    _specCache.clear();
    initSpectraTab(currentTid);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
};
