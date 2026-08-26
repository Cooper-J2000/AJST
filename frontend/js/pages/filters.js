// === Filters Page ===
import { app, showLoading, showError } from './layout.js';
import { isAuthed, isAdmin, showToast } from '../api.js';

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

export async function render() {
  showLoading();
  app.innerHTML = `
    <div class="page-header d-flex justify-content-between align-items-center">
      <h4 class="mb-0"><i class="bi bi-funnel"></i> 光学滤光片</h4>
      <div>
        <button class="btn btn-sm btn-outline-primary" id="filterAddBtn" style="display:none" onclick="filterShowAdd()"><i class="bi bi-plus-circle"></i> 添加滤光片</button>
        <small class="text-secondary ms-2" id="filterCount"></small>
      </div>
    </div>
    <div class="card">
      <div class="card-body p-0">
        <div class="table-responsive">
          <table class="table table-sm table-hover mb-0" style="font-size:0.85rem">
            <thead>
              <tr>
                <th class="f-sort" data-sort="id" style="cursor:pointer">ID <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="wavelength" style="cursor:pointer">波长 (Å) <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="filter_type" style="cursor:pointer">类型 <span class="sort-icon"></span></th>
                <th class="f-sort" data-sort="vega2ab" style="cursor:pointer">Vega→AB <span class="sort-icon"></span></th>
                <th>说明</th>
                <th id="filterEditHeader" style="display:none">编辑</th>
              </tr>
            </thead>
            <tbody id="filterBody">
              <tr><td colspan="6" class="text-center text-secondary py-4">加载中...</td></tr>
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

  // 新增滤波器登录即可；编辑仅管理员
  if (isAuthed()) {
    document.getElementById('filterAddBtn').style.display = 'inline-block';
  }
  if (isAdmin()) {
    document.getElementById('filterEditHeader').style.display = '';
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
    document.getElementById('filterCount').textContent = `共 ${filters.length} 个`;
    const tbody = document.getElementById('filterBody');
    tbody.innerHTML = filters.map(f => `
      <tr id="filterRow_${f.id}">
        <td><strong>${f.id}</strong></td>
        <td class="fv" data-field="wavelength">${f.wavelength != null ? f.wavelength.toFixed(2) : '-'}</td>
        <td class="fv" data-field="filter_type">${f.filter_type || '-'}</td>
        <td class="fv" data-field="vega2ab">${f.vega2ab != null ? f.vega2ab.toFixed(3) : '0.000'}</td>
        <td class="fv" data-field="description" style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${f.description || '-'}</td>
        <td class="filter-edit-cell" style="display:${isAdmin() ? '' : 'none'}">
          <button class="btn btn-sm btn-outline-secondary py-0 px-1" onclick="filterEditStart('${f.id}')" title="编辑"><i class="bi bi-pencil"></i></button>
        </td>
      </tr>
    `).join('');

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
      `<tr><td colspan="6" class="text-center text-danger py-4">加载失败: ${err.message}</td></tr>`;
  }
}
