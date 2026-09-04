// === Filters Page ===
import { app, showLoading, showError } from './layout.js';
import { isAuthed, isAdmin, showToast, deleteFilter } from '../api.js';
import { chartColors } from '../theme.js';

const API_BASE = '/api';

async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    credentials: 'same-origin',
  };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API_BASE + path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ message: r.statusText }));
    throw new Error(err.message || r.statusText);
  }
  return r.json();
}

let _sort = 'wavelength', _order = 'asc';
let _filters = [];                  // 最近一次加载的滤光片列表
const _unchecked = new Set();       // 行首未勾选的 id（默认全选；勾选态同时驱动总图显示）
const _highlight = new Set();       // 总图中加粗突出的 id
let _overviewChart = null, _curveChart = null;
let _wlMin = 0, _wlSpan = 0;

// matplotlib Spectral 色带锚点（ColorBrewer 9 色），取样 1-t 使短波→蓝/紫、长波→红
const SPECTRAL = [[158,1,66],[213,62,79],[244,109,67],[253,174,97],[254,224,139],
                  [230,245,152],[171,221,164],[102,194,165],[50,136,189]];
function spectralColor(t) {
  t = Math.min(1, Math.max(0, t));
  const x = t * (SPECTRAL.length - 1);
  const i = Math.min(Math.floor(x), SPECTRAL.length - 2);
  const f = x - i;
  const c = SPECTRAL[i].map((v, k) => Math.round(v + (SPECTRAL[i + 1][k] - v) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function wlColor(f) {
  const t = _wlSpan > 0 && f.wavelength != null ? (f.wavelength - _wlMin) / _wlSpan : 0.5;
  return spectralColor(1 - t);
}

function hasCurve(f) {
  return f.extra_data && f.extra_data.transmission && f.extra_data.transmission.wl;
}

// ─── 透过率总图（显示由行首勾选驱动；点击图例或表格 ID 加粗突出） ───
function rebuildOverview() {
  const canvas = document.getElementById('filterOverviewChart');
  if (!canvas) return;
  if (_overviewChart) { _overviewChart.destroy(); _overviewChart = null; }
  if (typeof Chart === 'undefined') return;
  const cc = chartColors();
  const shown = _filters.filter(f => hasCurve(f) && !_unchecked.has(f.id));
  const mk = (f, hl) => ({
    label: f.id,
    data: f.extra_data.transmission.wl.map((x, i) => ({ x, y: f.extra_data.transmission.tr[i] })),
    borderColor: wlColor(f),
    borderWidth: hl ? 3 : 1.3,
    pointRadius: 0,
    pointHitRadius: 5,
    tension: 0,
    fill: false,
  });
  // 加粗的排最后绘制（置顶）
  const normal = shown.filter(f => !_highlight.has(f.id));
  const hl = shown.filter(f => _highlight.has(f.id));
  _overviewChart = new Chart(canvas, {
    type: 'line',
    data: { datasets: [...normal.map(f => mk(f, false)), ...hl.map(f => mk(f, true))] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          labels: { color: cc.legend, boxWidth: 10, font: { size: 10 } },
          // 点击图例 = 加粗/取消加粗（隐藏由行首勾选框控制）
          onClick: (e, item) => toggleHighlight(item.text),
        },
        tooltip: { backgroundColor: cc.tooltipBg, titleColor: cc.tooltipText, bodyColor: cc.tooltipText },
      },
      scales: {
        x: { type: 'linear', title: { display: true, text: '波长 (Å)', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
        y: { min: 0, suggestedMax: 1, title: { display: true, text: '透过率', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
      },
    },
  });
}

function toggleHighlight(id) {
  if (_highlight.has(id)) _highlight.delete(id); else _highlight.add(id);
  rebuildOverview();
}

function syncCheckAll() {
  const all = document.getElementById('filterCheckAll');
  if (all) all.checked = _filters.length > 0 && _filters.every(f => !_unchecked.has(f.id));
}

export async function render() {
  showLoading();
  app.innerHTML = `
    <div class="page-header d-flex justify-content-between align-items-center">
      <h4 class="mb-0"><i class="bi bi-funnel"></i> 光学滤光片</h4>
      <div>
        <button class="btn btn-sm btn-outline-danger" id="filterDelBtn" style="display:none" onclick="filterDeleteSelected()"><i class="bi bi-trash"></i> 删除所选</button>
        <button class="btn btn-sm btn-outline-primary ms-2" id="filterAddBtn" style="display:none" onclick="filterShowAdd()"><i class="bi bi-plus-circle"></i> 添加滤光片</button>
        <small class="text-secondary ms-2" id="filterCount"></small>
      </div>
    </div>
    <!-- 透过率总图 -->
    <div class="card mb-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-graph-up"></i> 透过率总览</span>
        <small class="text-secondary">行首勾选控制显示；点击图例或表格 ID 加粗突出</small>
      </div>
      <div class="card-body">
        <div class="chart-container" style="height:240px"><canvas id="filterOverviewChart"></canvas></div>
      </div>
    </div>
    <div class="card">
      <div class="card-body p-0">
        <div class="table-responsive">
          <table class="table table-sm table-hover mb-0" style="font-size:0.85rem">
            <thead>
              <tr>
                <th style="width:30px"><input type="checkbox" id="filterCheckAll" checked title="全选/全不选（同步总图显示）"></th>
                <th class="f-sort" data-sort="id" style="cursor:pointer">ID <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="wavelength" style="cursor:pointer">波长 (Å) <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="filter_type" style="cursor:pointer">类型 <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="vega2ab" style="cursor:pointer">Vega→AB <span class="sort-icon"></span></th>
                <th>透过率</th>
                <th>说明</th>
                <th id="filterEditHeader" style="display:none">编辑</th>
              </tr>
            </thead>
            <tbody id="filterBody">
              <tr><td colspan="8" class="text-center text-secondary py-4">加载中...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <!-- 添加弹窗 -->
    <div class="modal fade" id="addFilterModal" tabindex="-1">
      <div class="modal-dialog modal-sm">
        <div class="modal-content">
          <div class="modal-header"><h6 class="mb-0">添加滤光片</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <div class="mb-2"><label class="form-label small">ID</label><input class="form-control form-control-sm" id="afId"></div>
            <div class="mb-2"><label class="form-label small">波长 (Å)</label><input class="form-control form-control-sm" id="afWl" type="number" step="any"></div>
            <div class="mb-2"><label class="form-label small">类型</label>
              <select class="form-select form-select-sm" id="afType"><option value="">-</option><option value="mean">mean</option><option value="ref">ref</option><option value="eff">eff</option><option value="guess">guess</option></select>
            </div>
            <div class="mb-2"><label class="form-label small">Vega→AB</label><input class="form-control form-control-sm" id="afVega" type="number" step="any" value="0"></div>
            <div class="mb-2"><label class="form-label small">说明</label><input class="form-control form-control-sm" id="afDesc"></div>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
            <button class="btn btn-sm btn-primary" onclick="filterAddSave()">保存</button>
          </div>
        </div>
      </div>
    </div>
    <!-- 单条透过率曲线弹窗 -->
    <div class="modal fade" id="curveModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <div class="modal-header"><h6 class="mb-0" id="curveTitle">透过率曲线</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <div style="position:relative;width:100%;aspect-ratio:1/1"><canvas id="curveCanvas"></canvas></div>
          </div>
        </div>
      </div>
    </div>
  `;

  // Sort click handlers
  document.querySelectorAll('.f-sort').forEach(th => {
    th.addEventListener('click', () => {
      const sort = th.dataset.sort;
      if (_sort === sort) {
        _order = _order === 'asc' ? 'desc' : 'asc';
      } else {
        _sort = sort;
        _order = 'asc';
      }
      updateSortIcons();
      loadFilters();
    });
  });

  // 曲线弹窗关闭时销毁图表
  document.getElementById('curveModal').addEventListener('hidden.bs.modal', () => {
    if (_curveChart) { _curveChart.destroy(); _curveChart = null; }
  });

  // 表头全选/全不选（同步总图显示）
  document.getElementById('filterCheckAll').addEventListener('change', (e) => {
    _unchecked.clear();
    if (!e.target.checked) _filters.forEach(f => _unchecked.add(f.id));
    document.querySelectorAll('.filter-check').forEach(cb => { cb.checked = e.target.checked; });
    rebuildOverview();
  });

  // 新增滤波器登录即可；编辑/删除仅管理员
  if (isAuthed()) {
    document.getElementById('filterAddBtn').style.display = 'inline-block';
  }
  if (isAdmin()) {
    document.getElementById('filterEditHeader').style.display = '';
    document.getElementById('filterDelBtn').style.display = 'inline-block';
  }

  updateSortIcons();
  await loadFilters();
}

function updateSortIcons() {
  document.querySelectorAll('.f-sort').forEach(th => {
    const icon = th.querySelector('.sort-icon');
    const sort = th.dataset.sort;
    if (sort === _sort) {
      icon.textContent = _order === 'asc' ? ' ▲' : ' ▼';
    } else {
      icon.textContent = '';
    }
  });
}

async function loadFilters() {
  try {
    const filters = await api('GET', `/filters?sort=${_sort}&order=${_order}`);
    _filters = filters;
    // 清理已不存在条目的勾选/加粗状态
    const ids = new Set(filters.map(f => f.id));
    for (const id of [..._unchecked]) if (!ids.has(id)) _unchecked.delete(id);
    for (const id of [..._highlight]) if (!ids.has(id)) _highlight.delete(id);
    const wls = filters.map(f => f.wavelength).filter(w => w != null);
    _wlMin = wls.length ? Math.min(...wls) : 0;
    _wlSpan = wls.length ? Math.max(...wls) - _wlMin : 0;

    document.getElementById('filterCount').textContent = `共 ${filters.length} 个`;
    const tbody = document.getElementById('filterBody');
    tbody.innerHTML = filters.map(f => `
      <tr id="filterRow_${f.id}">
        <td><input type="checkbox" class="filter-check" data-id="${f.id}" ${_unchecked.has(f.id) ? '' : 'checked'}></td>
        <td><strong style="cursor:pointer;color:${wlColor(f)}" title="点击在总图中加粗/取消加粗" onclick="filterToggleHl('${f.id}')">${f.id}</strong></td>
        <td class="fv" data-field="wavelength">${f.wavelength != null ? f.wavelength.toFixed(2) : '-'}</td>
        <td class="fv" data-field="filter_type">${f.filter_type || '-'}</td>
        <td class="fv" data-field="vega2ab">${f.vega2ab != null ? f.vega2ab.toFixed(3) : '0.000'}</td>
        <td>${hasCurve(f)
          ? `<span class="badge bg-success" style="cursor:pointer" title="点击查看透过率曲线" onclick="filterShowCurve('${f.id}')">曲线 ${f.extra_data.transmission.wl.length} 点</span>`
          : '<span class="badge bg-secondary">—</span>'}</td>
        <td class="fv" data-field="description" style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${f.description || '-'}</td>
        <td class="filter-edit-cell" style="display:${isAdmin() ? '' : 'none'}">
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="filterEditStart('${f.id}')" title="编辑"><i class="bi bi-pencil"></i></button>
        </td>
      </tr>
    `).join('');

    tbody.querySelectorAll('.filter-check').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) _unchecked.delete(cb.dataset.id); else _unchecked.add(cb.dataset.id);
        syncCheckAll();
        rebuildOverview();
      });
    });
    syncCheckAll();
    rebuildOverview();

    // ── 添加 ──
    window.filterShowAdd = () => {
      new bootstrap.Modal(document.getElementById('addFilterModal')).show();
    };
    window.filterAddSave = async () => {
      const id = document.getElementById('afId').value.trim();
      if (!id) { showToast('请输入 ID', 'warning'); return; }
      const body = {
        id, wavelength: parseFloat(document.getElementById('afWl').value) || 0,
        filter_type: document.getElementById('afType').value || null,
        vega2ab: parseFloat(document.getElementById('afVega').value) || 0,
        description: document.getElementById('afDesc').value.trim() || null,
      };
      try {
        await api('POST', '/filters', body);
        bootstrap.Modal.getInstance(document.getElementById('addFilterModal')).hide();
        showToast('已添加', 'success');
        loadFilters();
      } catch (err) {
        showToast(`添加失败: ${err.message}`, 'danger');
      }
    };

    // ── 单条透过率曲线弹窗 ──
    window.filterShowCurve = (id) => {
      const f = _filters.find(x => x.id === id);
      if (!f || !hasCurve(f) || typeof Chart === 'undefined') return;
      if (_curveChart) { _curveChart.destroy(); _curveChart = null; }
      const cc = chartColors();
      const tr = f.extra_data.transmission;
      document.getElementById('curveTitle').textContent = `透过率曲线 — ${f.id}`;
      _curveChart = new Chart(document.getElementById('curveCanvas'), {
        type: 'line',
        data: { datasets: [{
          label: f.id,
          data: tr.wl.map((x, i) => ({ x, y: tr.tr[i] })),
          borderColor: wlColor(f),
          borderWidth: 2,
          pointRadius: 0,
          pointHitRadius: 5,
          tension: 0,
          fill: false,
        }] },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          interaction: { mode: 'nearest', intersect: false },
          plugins: { legend: { display: false } },
          scales: {
            x: { type: 'linear', title: { display: true, text: '波长 (Å)', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
            y: { min: 0, suggestedMax: 1, title: { display: true, text: '透过率', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
          },
        },
      });
      new bootstrap.Modal(document.getElementById('curveModal')).show();
    };

    // ── 加粗突出（总图图例点击或表格 ID 点击均走这里） ──
    window.filterToggleHl = (id) => toggleHighlight(id);

    // ── 删除所选（仅管理员；勾选框同时驱动总图显示） ──
    window.filterDeleteSelected = async () => {
      if (!isAdmin()) { showToast('仅管理员可删除数据', 'warning'); return; }
      const ids = _filters.filter(f => !_unchecked.has(f.id)).map(f => f.id);
      if (!ids.length) { showToast('未勾选任何滤光片', 'warning'); return; }
      if (!confirm(`确定删除所选 ${ids.length} 个滤光片？\n${ids.join(', ')}\n\n注意：若有数据使用此波段，统计与消光改正将受影响。`)) return;
      let ok = 0;
      const fails = [];
      for (const id of ids) {
        try { await deleteFilter(id); ok++; }
        catch (err) { fails.push(`${id}: ${err.message}`); }
      }
      showToast(fails.length
        ? `已删除 ${ok} 个；失败 ${fails.length} 个: ${fails.join('; ')}`
        : `已删除 ${ok} 个滤光片`, fails.length ? 'warning' : 'success');
      loadFilters();
    };

    // ── 编辑 ──
    window.filterEditStart = (id) => {
      if (!isAdmin()) { showToast('仅管理员可编辑数据', 'warning'); return; }
      const row = document.getElementById(`filterRow_${id}`);
      if (!row) return;
      const cells = row.querySelectorAll('.fv');
      cells.forEach(cell => {
        const field = cell.dataset.field;
        const val = cell.textContent.trim();
        if (field === 'filter_type') {
          cell.innerHTML = `<select class="form-select form-select-sm fi" data-field="filter_type" style="width:90px">
            <option value="mean" ${val === 'mean' ? 'selected' : ''}>mean</option>
            <option value="ref" ${val === 'ref' ? 'selected' : ''}>ref</option>
            <option value="eff" ${val === 'eff' ? 'selected' : ''}>eff</option>
            <option value="guess" ${val === 'guess' ? 'selected' : ''}>guess</option>
            <option value="" ${val === '-' ? 'selected' : ''}>-</option>
          </select>`;
        } else {
          cell.innerHTML = `<input type="text" class="form-control form-control-sm fi" data-field="${field}" value="${val === '-' ? '' : val}" style="width:${field === 'description' ? 280 : 120}px">`;
        }
      });
      const editCell = row.querySelector('.filter-edit-cell');
      if (editCell) {
        editCell.innerHTML = `
          <button class="btn btn-sm btn-primary py-0 px-1" onclick="filterEditSave('${id}')" title="保存"><i class="bi bi-check-lg"></i></button>
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="loadFilters()" title="取消"><i class="bi bi-x-lg"></i></button>
        `;
      }
    };

    window.filterEditSave = async (id) => {
      const row = document.getElementById(`filterRow_${id}`);
      if (!row) return;
      const inputs = row.querySelectorAll('.fi');
      const body = {};
      inputs.forEach(inp => {
        const field = inp.dataset.field;
        let val = inp.value.trim();
        if (field === 'wavelength') {
          body[field] = val ? parseFloat(val) : null;
        } else if (field === 'vega2ab') {
          body[field] = val !== '' ? parseFloat(val) : 0;
        } else {
          body[field] = val || null;
        }
      });
      try {
        await api('PUT', `/filters/${encodeURIComponent(id)}`, body);
        showToast('已更新', 'success');
        loadFilters();
      } catch (err) {
        showToast(`更新失败: ${err.message}`, 'danger');
      }
    };

  } catch (err) {
    document.getElementById('filterBody').innerHTML =
      `<tr><td colspan="8" class="text-center text-danger py-4">加载失败: ${err.message}</td></tr>`;
  }
}
