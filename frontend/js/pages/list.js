// === Transient List Page ===
import { app, showLoading, showError } from './layout.js';
import {
  getTransients, deleteTransient, showToast, isAuthed, isAdmin, runExtinction
} from '../api.js';
import { esc } from '../utils.js';

let currentState = { page: 1, sort: 't0', order: 'desc' };
let _listReqId = 0;     // 异步请求令牌（竞态防护）
let _applyTimer = null; // 筛选输入防抖
let _pgData = { page: 1, totalPages: 1 }; // 分页条状态（拖动/滚轮/输入跳转共用）
let _pgSuppressClick = false; // 拖动翻页后抑制紧随的 click，避免误触页码
let _wheelAcc = 0;      // 滚轮翻页：滚动量累积（节流用）
let _wheelFlipAt = 0;   // 滚轮翻页：上次翻页时刻（节流用）

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
            <label class="form-label small">T0 起始</label>
            <input type="date" class="form-control form-control-sm" id="fT0From" onchange="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">T0 截止（含当天）</label>
            <input type="date" class="form-control form-control-sm" id="fT0To" onchange="applyFilter()">
          </div>
          <div class="col-md-2">
            <label class="form-label small">标签</label>
            <select class="form-select form-select-sm" id="fTag" onchange="applyFilter()">
              <option value="">全部</option>
            </select>
          </div>
          <div class="col-md-2">
            <label class="form-label small">副标签</label>
            <select class="form-select form-select-sm" id="fSubTag" onchange="applyFilter()">
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
              <option value="t0|desc" selected>T0 ↓</option>
              <option value="t0|asc">T0 ↑</option>
            </select>
          </div>
          <div class="col-md-2">
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="fHasZ" onchange="applyFilter()">
              <label class="form-check-label small">仅显示有红移</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="fHasHost" onchange="applyFilter()">
              <label class="form-check-label small">仅显示有宿主信息</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="fHasSpectra" onchange="applyFilter()">
              <label class="form-check-label small">仅显示有光谱数据</label>
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
                <th class="sort-header" data-sort="id">ID</th>
                <th class="sort-header" data-sort="ra">RA</th>
                <th class="sort-header" data-sort="dec">Dec</th>
                <th class="sort-header" data-sort="redshift">z</th>
                <th class="sort-header sort-desc" data-sort="t0">T0</th>
                <th>标签</th>
                <th>别名</th>
                <th>触发仪器</th>
                <th>数据点</th>
                <th>光谱</th>
                <th>宿主</th>
                <th id="listDelHeader" style="display:none">操作</th>
              </tr>
            </thead>
            <tbody id="tableBody">
              <tr><td colspan="12" class="text-center text-secondary py-4">加载中...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <!-- Pagination（居中滚轮式分页条：左右拖动/滚轮翻页，点当前页码可输入跳转） -->
    <nav class="mt-3 d-flex align-items-center">
      <small class="text-secondary" id="pageInfo"></small>
      <div class="d-flex justify-content-center align-items-center flex-grow-1" id="pagination">
        <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-2" id="pgFirst" title="第一页">«</button>
        <div id="pgViewport" title="按住左右拖动或滚动滚轮翻页"
             style="max-width:360px;overflow:hidden;margin:0 6px;user-select:none;cursor:grab;touch-action:pan-y">
          <div id="pgTrack" class="d-flex align-items-center"></div>
        </div>
        <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-2" id="pgLast" title="最后一页">»</button>
      </div>
    </nav>
  `;

  // Make filter & sort funcs globally accessible
  window.toggleFilter = () => {
    const el = document.getElementById('filterPanel');
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
  };
  window.applyFilter = () => {
    currentState.page = 1;
    clearTimeout(_applyTimer);
    _applyTimer = setTimeout(loadData, 300);
  };
  window.clearFilter = () => {
    document.querySelectorAll('#filterPanel input').forEach(i => i.value = '');
    document.getElementById('fHasZ').checked = false;
    document.getElementById('fHasHost').checked = false;
    document.getElementById('fHasSpectra').checked = false;
    document.getElementById('fTag').value = '';
    document.getElementById('fSubTag').value = '';
    document.getElementById('fSort').value = 't0|desc';
    currentState.page = 1;
    loadData();
  };

  // 行点击导航 + 删除按钮（事件委托，避免内联 onclick 拼接待转义 ID）
  document.getElementById('tableBody').addEventListener('click', (e) => {
    const delBtn = e.target.closest('[data-del]');
    if (delBtn) {
      e.stopPropagation();
      window.confirmDelete(delBtn.dataset.del);
      return;
    }
    const row = e.target.closest('tr.row-link');
    if (row && row.dataset.tid) location.hash = '#/transient/' + encodeURIComponent(row.dataset.tid);
  });

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
        (st.skipped_band ? `, ${st.skipped_band} 点波段不支持` : '') +
        (st.skipped_not_optical ? `, ${st.skipped_not_optical} 点非光学波段跳过` : '') +
        (st.note ? `。${st.note}` : ''), 'success');
    } catch (err) {
      showToast(`全局银消改正失败: ${err.message}`, 'danger');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-moon-stars"></i> 全局银消改正';
    }
  };

  // Populate tag filter（专用轻量接口，不整表拉取）
  import('../api.js').then(m => m.getTransientTags()).then(data => {
    const sel = document.getElementById('fTag');
    if (sel && data.tags) {
      data.tags.forEach(tag => {
        const opt = document.createElement('option');
        opt.value = tag;
        opt.textContent = tag;
        sel.appendChild(opt);
      });
    }
  });

  // Populate sub-tag filter（同上，副标签选项）
  import('../api.js').then(m => m.getTransientSubTags()).then(data => {
    const sel = document.getElementById('fSubTag');
    if (sel && data.sub_tags) {
      data.sub_tags.forEach(tag => {
        const opt = document.createElement('option');
        opt.value = tag;
        opt.textContent = tag;
        sel.appendChild(opt);
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

  initPager();
  loadData();
}

async function loadData() {
  if (!document.getElementById('tableBody')) return;  // 已离开列表页（防抖回调迟到）
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
  const subTagVal = document.getElementById('fSubTag')?.value;
  if (subTagVal) params.sub_tag = subTagVal;
  const t0From = document.getElementById('fT0From')?.value;
  if (t0From) params.t0_from = t0From;
  const t0To = document.getElementById('fT0To')?.value;
  if (t0To) params.t0_to = t0To;
  if (document.getElementById('fHasZ')?.checked) params.has_z = 'true';
  if (document.getElementById('fHasHost')?.checked) params.has_host = 'true';
  if (document.getElementById('fHasSpectra')?.checked) params.has_spectra = 'true';

  try {
    const reqId = ++_listReqId;
    const data = await getTransients(params);
    if (reqId !== _listReqId) return;  // 已有更新的请求发出，丢弃过期响应
    if (!document.getElementById('tableBody')) return;  // 请求期间已离开列表页
    renderTable(data);
    renderPagination(data);
    // 登录后显示删除按钮
    if (isAdmin()) {
      document.getElementById('listDelHeader').style.display = '';
      document.querySelectorAll('#tableBody [data-delbtn]').forEach(el => el.style.display = '');
    }
  } catch (err) {
    const tbody = document.getElementById('tableBody');
    if (!tbody) return;  // 已离开列表页
    tbody.innerHTML =
      `<tr><td colspan="12" class="text-center text-danger py-4">加载失败: ${esc(err.message)}</td></tr>`;
  }
}

function renderTable(data) {
  const tbody = document.getElementById('tableBody');
  if (!tbody) return;
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="text-center text-secondary py-4">没有匹配的事件</td></tr>';
    return;
  }
  tbody.innerHTML = data.items.map(t => `
    <tr class="row-link" data-tid="${esc(t.id)}">
      <td><strong>${esc(t.id)}</strong></td>
      <td>${t.ra != null ? t.ra.toFixed(4) : '-'}</td>
      <td>${t.dec != null ? t.dec.toFixed(4) : '-'}</td>
      <td>${t.redshift != null ? t.redshift.toFixed(3) : '<span class="text-secondary">-</span>'}</td>
      <td class="small">${t.t0 ? esc(t.t0.replace('T', ' ').substring(0, 19)) : '-'}</td>
      <td>${(t.tags || []).map(tag => `<span class="badge-tag">${esc(tag)}</span>`).join('')}${(t.sub_tag || []).map(tag => `<span class="badge-neutral" style="margin-right:4px">${esc(tag)}</span>`).join('')}</td>
      <td class="small">${esc((t.aliases || []).join(', ')) || '-'}</td>
      <td class="small">${esc(t.trigger_instrument) || '-'}</td>
      <td>${t.lc_count || 0}</td>
      <td>${t.spectra_count ? `<span class="badge-tag" style="background:rgba(188,140,255,0.15);color:#bc8cff">${t.spectra_count}</span>` : '-'}</td>
      <td>${t.has_host ? `<span class="badge-tag" style="background:rgba(63,185,80,0.15);color:#3fb950">宿主</span>` : ''}</td>
      <td data-delbtn style="display:none">
        <button class="btn btn-sm btn-outline-danger py-0 px-1" title="删除" data-del="${esc(t.id)}">
          <i class="bi bi-trash"></i>
        </button>
      </td>
    </tr>
  `).join('');
}

function renderPagination(data) {
  const { total, page, per_page } = data;
  _pgData = { page, totalPages: Math.max(1, Math.ceil(total / per_page)) };
  document.getElementById('pageInfo').textContent = `共 ${total} 条，第 ${page}/${_pgData.totalPages} 页`;
  renderPager();
}

// 页码窗口（当前页 ±3）+ 首尾快跳按钮状态；翻页/取消输入后据此重绘
function renderPager() {
  const track = document.getElementById('pgTrack');
  if (!track) return;  // 已离开列表页
  const { page, totalPages } = _pgData;
  document.getElementById('pgFirst').disabled = page <= 1;
  document.getElementById('pgLast').disabled = page >= totalPages;
  if (totalPages <= 1) { track.innerHTML = ''; return; }
  const start = Math.max(1, page - 3);
  const end = Math.min(totalPages, page + 3);
  let html = '';
  for (let i = start; i <= end; i++) {
    html += `<span class="pg-num" data-page="${i}" style="${pgNumStyle(i === page)}">${i}</span>`;
  }
  track.innerHTML = html;
}

// 页码样式（当前页高亮）；无构建步骤且不动 style.css，故用内联样式 + 主题变量
function pgNumStyle(cur) {
  const base = 'display:inline-block;min-width:28px;padding:2px 8px;margin:0 2px;text-align:center;' +
    'font-size:0.85em;border-radius:4px;cursor:pointer;';
  return cur
    ? base + 'background:var(--accent-blue-soft);color:var(--accent-blue);' +
      'border:1px solid var(--accent-blue-border);font-weight:600'
    : base + 'color:var(--text-secondary);border:1px solid var(--border-color)';
}

// 分页条交互：点击页码/首尾快跳、按住左右拖动翻页、滚轮翻页、点当前页输入跳转。
// 容器在静态模板里，事件只在 render() 挂一次；页码用事件委托，避免内联 onclick
function initPager() {
  const strip = document.getElementById('pagination');
  const viewport = document.getElementById('pgViewport');
  const track = document.getElementById('pgTrack');
  if (!strip || !viewport || !track) return;

  // 翻页入口（clamp 到 [1, totalPages]，复用现有 loadData 流程）
  window.goPage = (p) => {
    p = Math.trunc(Number(p));
    if (!isFinite(p)) return false;
    p = Math.max(1, Math.min(p, _pgData.totalPages));
    if (p === currentState.page) return false;
    currentState.page = p;
    loadData();
    return false;
  };

  strip.addEventListener('click', (e) => {
    if (_pgSuppressClick) { _pgSuppressClick = false; return; }
    const btn = e.target.closest('#pgFirst, #pgLast');
    if (btn) {
      if (!btn.disabled) window.goPage(btn.id === 'pgFirst' ? 1 : _pgData.totalPages);
      return;
    }
    const num = e.target.closest('.pg-num');
    if (!num) return;
    const p = Number(num.dataset.page);
    if (p === _pgData.page) beginPageInput(num);
    else window.goPage(p);
  });

  // 按住鼠标左右拖动：滑满一个页码宽度翻一页（可连续多页），松手吸附
  viewport.addEventListener('mousedown', (e) => {
    e.preventDefault();
    _pgSuppressClick = false;
    const startX = e.clientX;
    const itemW = (track.firstElementChild?.getBoundingClientRect().width || 28) + 4;
    let dx = 0;
    viewport.style.cursor = 'grabbing';
    const onMove = (ev) => {
      dx = ev.clientX - startX;
      track.style.transform = `translateX(${dx}px)`;
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      viewport.style.cursor = 'grab';
      track.style.transform = '';
      if (Math.abs(dx) > 5) _pgSuppressClick = true;
      const steps = Math.floor(Math.abs(dx) / itemW);
      if (steps > 0) window.goPage(_pgData.page + (dx < 0 ? steps : -steps));
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });

  // 滚轮翻页：横向/纵向滚动都算；累积滚动量 + 最小间隔节流，避免一次滚很多页
  viewport.addEventListener('wheel', (e) => {
    e.preventDefault();
    const now = Date.now();
    if (now - _wheelFlipAt > 600) _wheelAcc = 0;  // 停顿后重新累积
    _wheelAcc += Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    if (Math.abs(_wheelAcc) >= 60 && now - _wheelFlipAt >= 250) {
      const step = _wheelAcc > 0 ? 1 : -1;
      _wheelAcc = 0;
      _wheelFlipAt = now;
      window.goPage(_pgData.page + step);
    }
  }, { passive: false });
}

// 点击当前页码 → 替换为数字输入框：Enter 校验 1..totalPages 后跳转，Esc/失焦取消
function beginPageInput(span) {
  const input = document.createElement('input');
  input.type = 'number';
  input.min = 1;
  input.max = _pgData.totalPages;
  input.value = _pgData.page;
  input.className = 'form-control form-control-sm';
  input.style.cssText = 'width:4.5em;padding:0 4px;margin:0 2px;text-align:center;font-size:0.85em';
  span.replaceWith(input);
  // 输入框内的鼠标事件不传给分页条（避免触发拖动/点击翻页）
  input.addEventListener('mousedown', (ev) => ev.stopPropagation());
  input.addEventListener('click', (ev) => ev.stopPropagation());
  input.focus();
  input.select();
  let done = false;
  const finish = (jump) => {
    if (done) return;
    done = true;
    if (jump) {
      const v = parseInt(input.value, 10);
      if (v >= 1 && v <= _pgData.totalPages) { window.goPage(v); return; }
      showToast(`页码需在 1 到 ${_pgData.totalPages} 之间`, 'warning');
    }
    renderPager();
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') finish(true);
    else if (ev.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(false));
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
