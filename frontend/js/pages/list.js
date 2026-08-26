// === Transient List Page ===
import { app, showLoading, showError } from './layout.js';
import {
  getTransients, deleteTransient, exportTransients, showToast, isAuthed, isAdmin, runExtinction
} from '../api.js';

let currentState = { page: 1, sort: 'id', order: 'asc' };

export async function render() {
  app.innerHTML = `
    <div class="page-header d-flex justify-content-between align-items-center">
      <h4 class="mb-0"><i class="bi bi-list-ul"></i> 暂现源事件列表</h4>
      <div>
        <button class="btn btn-sm btn-outline-secondary me-1" onclick="toggleFilter()">
          <i class="bi bi-funnel"></i> 筛选
        </button>
        <button class="btn btn-sm btn-outline-secondary me-1" onclick="APIImport.exportTransients('csv')">
          <i class="bi bi-download"></i> 导出CSV
        </button>
        <button class="btn btn-sm btn-outline-warning me-1" id="gextAllBtn" style="display:none" onclick="runGextAll()">
          <i class="bi bi-moon-stars"></i> 全局银消改正
        </button>
        <a href="#/new" class="btn btn-sm btn-outline-primary">
          <i class="bi bi-plus-circle"></i> 新建
        </a>
      </div>
    </div>
    <!-- Filter panel -->
    <div id="filterPanel" class="card mb-3" style="display:none">
      <div class="card-body">
        <div class="row g-2 align-items-end">
          <div class="col-md-3">
            <label class="form-label small">搜索 (ID/别名/引用)</label>
            <input type="text" class="form-control form-control-sm" id="fSearch" placeholder="EP251202a / GRB..." oninput="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">红移 ≥</label>
            <input type="number" class="form-control form-control-sm" id="fZMin" step="0.01" oninput="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">红移 ≤</label>
            <input type="number" class="form-control form-control-sm" id="fZMax" step="0.01" oninput="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">RA ≥</label>
            <input type="number" class="form-control form-control-sm" id="fRAMin" step="0.1" placeholder="0" oninput="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">RA ≤</label>
            <input type="number" class="form-control form-control-sm" id="fRAMax" step="0.1" placeholder="360" oninput="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">Dec ≥</label>
            <input type="number" class="form-control form-control-sm" id="fDecMin" step="0.1" placeholder="-90" oninput="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">Dec ≤</label>
            <input type="number" class="form-control form-control-sm" id="fDecMax" step="0.1" placeholder="90" oninput="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">标签</label>
            <select class="form-select form-select-sm" id="fTag" onchange="applyFilter()">
              <option value="">全部</option>
            </select>
          </div>
          <div class="col-md-2">
            <label class="form-label small">排序</label>
            <select class="form-select form-select-sm" id="fSort" onchange="applyFilter()">
              <option value="id|asc">ID ↑</option>
              <option value="id|desc">ID ↓</option>
              <option value="redshift|desc">红移 ↓</option>
              <option value="redshift|asc">红移 ↑</option>
              <option value="t0|desc">T0 ↓</option>
              <option value="t0|asc">T0 ↑</option>
            </select>
          </div>
          <div class="col-md-2">
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="fHasZ" onchange="applyFilter()">
              <label class="form-check-label small">仅显示有红移</label>
            </div>
          </div>
          <div class="col-md-2">
            <button class="btn btn-sm btn-outline-secondary w-100" onclick="clearFilter()">清除筛选</button>
          </div>
        </div>
      </div>
    </div>
    <!-- Table -->
    <div class="card">
      <div class="card-body p-0">
        <div class="table-responsive">
          <table class="table table-hover table-sm mb-0" id="transientTable">
            <thead>
              <tr>
                <th class="sort-header sort-asc" data-sort="id">ID</th>
                <th class="sort-header" data-sort="ra">RA</th>
                <th class="sort-header" data-sort="dec">Dec</th>
                <th class="sort-header" data-sort="redshift">z</th>
                <th class="sort-header" data-sort="t0">T0</th>
                <th>标签</th>
                <th>别名</th>
                <th>触发仪器</th>
                <th>数据点</th>
                <th>光谱</th>
                <th id="listDelHeader" style="display:none">操作</th>
              </tr>
            </thead>
            <tbody id="tableBody">
              <tr><td colspan="10" class="text-center text-secondary py-4">加载中...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <!-- Pagination -->
    <nav class="mt-3 d-flex justify-content-between align-items-center">
      <small class="text-secondary" id="pageInfo"></small>
      <ul class="pagination pagination-sm mb-0" id="pagination"></ul>
    </nav>
  `;

  // Make filter & sort funcs globally accessible
  window.toggleFilter = () => {
    const el = document.getElementById('filterPanel');
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
  };
  window.applyFilter = () => { currentState.page = 1; loadData(); };
  window.clearFilter = () => {
    document.querySelectorAll('#filterPanel input').forEach(i => i.value = '');
    document.getElementById('fHasZ').checked = false;
    document.getElementById('fTag').value = '';
    document.getElementById('fSort').value = 'id|asc';
    currentState.page = 1;
    loadData();
  };
  window.APIImport = { exportTransients };

  // 全局银消改正（登录后显示按钮）
  if (isAdmin()) {
    document.getElementById('gextAllBtn').style.display = '';
  }
  window.runGextAll = async () => {
    if (!isAdmin()) { showToast('仅管理员可执行银消改正', 'warning'); return; }
    if (!confirm('对数据库中所有源的全部光学数据执行银河系消光改正？\n（CSFD 尘埃图 + P92 消光曲线，Rv=3.1；无坐标的源自动跳过，可能需要几分钟）')) return;
    const btn = document.getElementById('gextAllBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 正在全局改正...';
    try {
      const st = await runExtinction({});
      showToast(`全局银消改正完成: ${st.corrected}/${st.total} 点已改正` +
        (st.skipped_no_coords ? `, ${st.skipped_no_coords} 点无坐标跳过` : '') +
        (st.skipped_band ? `, ${st.skipped_band} 点波段不支持` : ''), 'success');
    } catch (err) {
      showToast(`全局银消改正失败: ${err.message}`, 'danger');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-moon-stars"></i> 全局银消改正';
    }
  };

  // Populate tag filter
  import('../api.js').then(m => m.getTransients({ per_page: 10000 })).then(data => {
    const allTags = new Set();
    data.items.forEach(t => (t.tags || []).forEach(tag => allTags.add(tag)));
    const sel = document.getElementById('fTag');
    if (sel) {
      const sorted = [...allTags].sort();
      sorted.forEach(tag => {
        sel.innerHTML += `<option value="${tag}">${tag}</option>`;
      });
    }
  });

  // Sort click handlers
  document.querySelectorAll('.sort-header').forEach(th => {
    th.addEventListener('click', () => {
      const sort = th.dataset.sort;
      if (currentState.sort === sort) {
        currentState.order = currentState.order === 'asc' ? 'desc' : 'asc';
      } else {
        currentState.sort = sort;
        currentState.order = 'asc';
      }
      document.querySelectorAll('.sort-header').forEach(s => {
        s.classList.remove('sort-asc', 'sort-desc');
      });
      th.classList.add(currentState.order === 'asc' ? 'sort-asc' : 'sort-desc');
      // 同步到下拉选择器
      const sortEl = document.getElementById('fSort');
      if (sortEl) sortEl.value = `${currentState.sort}|${currentState.order}`;
      loadData();
    });
  });

  loadData();
}

async function loadData() {
  const params = {
    page: currentState.page,
    per_page: 50,
  };

  // Read sort
  const sortEl = document.getElementById('fSort');
  if (sortEl) {
    const v = sortEl.value.split('|');
    params.sort = v[0];
    params.order = v[1];
  }

  // Read filter values
  const search = document.getElementById('fSearch')?.value?.trim();
  if (search) params.search = search;
  const zMin = document.getElementById('fZMin')?.value;
  if (zMin) params.z_min = zMin;
  const zMax = document.getElementById('fZMax')?.value;
  if (zMax) params.z_max = zMax;
  const raMin = document.getElementById('fRAMin')?.value;
  if (raMin) params.ra_min = raMin;
  const raMax = document.getElementById('fRAMax')?.value;
  if (raMax) params.ra_max = raMax;
  const decMin = document.getElementById('fDecMin')?.value;
  if (decMin) params.dec_min = decMin;
  const decMax = document.getElementById('fDecMax')?.value;
  if (decMax) params.dec_max = decMax;
  const tagVal = document.getElementById('fTag')?.value;
  if (tagVal) params.tag = tagVal;
  if (document.getElementById('fHasZ')?.checked) params.has_z = 'true';

  try {
    const data = await getTransients(params);
    renderTable(data);
    renderPagination(data);
    // 登录后显示删除按钮
    if (isAdmin()) {
      document.getElementById('listDelHeader').style.display = '';
      data.items.forEach(t => {
        const el = document.getElementById(`delBtn_${t.id}`);
        if (el) el.style.display = '';
      });
    }
  } catch (err) {
    document.getElementById('tableBody').innerHTML =
      `<tr><td colspan="10" class="text-center text-danger py-4">加载失败: ${err.message}</td></tr>`;
  }
}

function renderTable(data) {
  const tbody = document.getElementById('tableBody');
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="text-center text-secondary py-4">没有匹配的事件</td></tr>';
    return;
  }
  tbody.innerHTML = data.items.map(t => `
    <tr class="row-link" onclick="location.href='#/transient/${t.id}'">
      <td><strong>${t.id}</strong></td>
      <td>${t.ra != null ? t.ra.toFixed(4) : '-'}</td>
      <td>${t.dec != null ? t.dec.toFixed(4) : '-'}</td>
      <td>${t.redshift != null ? t.redshift.toFixed(3) : '<span class="text-secondary">-</span>'}</td>
      <td class="small">${t.t0 ? t.t0.substring(0, 10) : '-'}</td>
      <td>${(t.tags || []).map(tag => `<span class="badge-tag">${tag}</span>`).join('')}</td>
      <td class="small">${(t.aliases || []).join(', ') || '-'}</td>
      <td class="small">${t.trigger_instrument || '-'}</td>
      <td>${t.lc_count || 0}</td>
      <td>${t.spectra_count ? `<span class="badge-tag" style="background:rgba(188,140,255,0.15);color:#bc8cff">${t.spectra_count}</span>` : '-'}</td>
      <td id="delBtn_${t.id}" style="display:none">
        <button class="btn btn-sm btn-outline-danger py-0 px-1" title="删除"
          onclick="event.stopPropagation(); confirmDelete('${t.id}')">
          <i class="bi bi-trash"></i>
        </button>
      </td>
    </tr>
  `).join('');
}

function renderPagination(data) {
  const { total, page, per_page } = data;
  const totalPages = Math.ceil(total / per_page);
  document.getElementById('pageInfo').textContent = `共 ${total} 条，第 ${page}/${totalPages} 页`;

  const ul = document.getElementById('pagination');
  if (totalPages <= 1) { ul.innerHTML = ''; return; }

  let html = '';
  html += `<li class="page-item ${page <= 1 ? 'disabled' : ''}">
    <a class="page-link" href="#" onclick="return goPage(${page - 1})">‹</a></li>`;

  const start = Math.max(1, page - 2);
  const end = Math.min(totalPages, page + 2);
  for (let i = start; i <= end; i++) {
    html += `<li class="page-item ${i === page ? 'active' : ''}">
      <a class="page-link" href="#" onclick="return goPage(${i})">${i}</a></li>`;
  }
  html += `<li class="page-item ${page >= totalPages ? 'disabled' : ''}">
    <a class="page-link" href="#" onclick="return goPage(${page + 1})">›</a></li>`;
  ul.innerHTML = html;
  window.goPage = (p) => { currentState.page = p; loadData(); return false; };
}

window.confirmDelete = async (id) => {
  if (!isAdmin()) { showToast('仅管理员可删除数据', 'warning'); return; }
  // 第一关：确认意图
  if (!confirm(`你是否确认要删除 ${id} 整个条目？此操作不可撤销。`)) return;
  // 第二关：输入确认码
  const code = prompt(`请输入 DELETE CONFIRM 以确认删除 ${id}：`, '');
  if (code !== 'DELETE CONFIRM') {
    showToast('取消删除：确认码不匹配', 'warning');
    return;
  }
  try {
    await deleteTransient(id);
    showToast(`已删除 ${id}`, 'success');
    loadData();
  } catch (err) {
    showToast(`删除失败: ${err.message}`, 'danger');
  }
};
