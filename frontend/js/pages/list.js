// === Transient List Page ===
import { app, showLoading, showError } from './layout.js';
import {
  getTransients, deleteTransient, showToast, isAuthed, isAdmin, runExtinction
} from '../api.js';
import { esc, escAttr } from '../utils.js';

let currentState = { page: 1, sort: 't0', order: 'desc' };
let _listReqId = 0;     // 异步请求令牌（竞态防护）
let _applyTimer = null; // 筛选输入防抖
let _pgData = { page: 1, totalPages: 1 }; // 分页条状态（滚轮/输入跳转共用）
let _pgOffset = 0;    // 分页条基准平移（当前页居中位）
let _wheelAcc = 0;      // 滚轮翻页：滚动量累积（节流用）
let _wheelFlipAt = 0;   // 滚轮翻页：上次翻页时刻（节流用）
let _urlQuery = {};     // URL 中的筛选/排序/页码初值（render 时解析）

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
        <!-- 第一排：搜索 / 标签 / 副标签 / 排序 -->
        <div class="row g-2 align-items-end">
          <div class="col-md-3">
            <label class="form-label small">搜索 (ID/别名/引用/触发仪器)</label>
            <input type="text" class="form-control form-control-sm" id="fSearch" placeholder="EP251202a / Fermi / GRB..." oninput="applyFilter()">
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
              <option value="ra|asc">RA ↑</option>
              <option value="ra|desc">RA ↓</option>
              <option value="dec|asc">Dec ↑</option>
              <option value="dec|desc">Dec ↓</option>
              <option value="redshift|desc">红移 ↓</option>
              <option value="redshift|asc">红移 ↑</option>
              <option value="t0|desc" selected>T0 ↓</option>
              <option value="t0|asc">T0 ↑</option>
            </select>
          </div>
        </div>
        <!-- 第二排：数值范围（红移/RA/Dec 窄框）+ T0 起止 -->
        <div class="row g-2 align-items-end mt-1">
          <div class="col-md-1">
            <label class="form-label small">红移 ≥</label>
            <input type="number" class="form-control form-control-sm" id="fZMin" step="0.01" oninput="applyFilter()">
          </div>
          <div class="col-md-1">
            <label class="form-label small">红移 ≤</label>
            <input type="number" class="form-control form-control-sm" id="fZMax" step="0.01" oninput="applyFilter()">
          </div>
          <div class="col-md-1">
            <label class="form-label small">RA ≥</label>
            <input type="number" class="form-control form-control-sm" id="fRAMin" step="0.1" placeholder="0" oninput="applyFilter()">
          </div>
          <div class="col-md-1">
            <label class="form-label small">RA ≤</label>
            <input type="number" class="form-control form-control-sm" id="fRAMax" step="0.1" placeholder="360" oninput="applyFilter()">
          </div>
          <div class="col-md-1">
            <label class="form-label small">Dec ≥</label>
            <input type="number" class="form-control form-control-sm" id="fDecMin" step="0.1" placeholder="-90" oninput="applyFilter()">
          </div>
          <div class="col-md-1">
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
        </div>
        <!-- 第三排：勾选框 + 清除筛选 -->
        <div class="row g-2 align-items-center mt-1">
          <div class="col-auto">
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="fHasZ" onchange="applyFilter()">
              <label class="form-check-label small">仅显示有红移</label>
            </div>
          </div>
          <div class="col-auto">
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="fHasHost" onchange="applyFilter()">
              <label class="form-check-label small">仅显示有宿主信息</label>
            </div>
          </div>
          <div class="col-auto">
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="fHasSpectra" onchange="applyFilter()">
              <label class="form-check-label small">仅显示有光谱数据</label>
            </div>
          </div>
          <div class="col-auto ms-auto">
            <button class="btn btn-sm btn-outline-secondary" onclick="clearFilter()">清除筛选</button>
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
    <!-- Pagination（居中分页条：静态渲染当前页 ±5 页码，滚轮翻页，点当前页码可输入跳转） -->
    <nav class="mt-3 d-flex align-items-center">
      <small class="text-secondary" id="pageInfo"></small>
      <div class="d-flex justify-content-center align-items-center flex-grow-1" id="pagination">
        <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-2" id="pgFirst" title="第一页">«</button>
        <div id="pgViewport" title="滚动滚轮翻页；点击当前页码输入跳转"
             style="max-width:360px;overflow:hidden;margin:0 6px;user-select:none;touch-action:pan-y">
          <!-- position:relative 使 track 恒为页码的 offsetParent（否则无 transform 时 offsetLeft 相对 BODY，居中计算会错乱） -->
          <div id="pgTrack" class="d-flex align-items-center" style="position:relative"></div>
        </div>
        <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-2" id="pgLast" title="最后一页">»</button>
      </div>
    </nav>
  `;

  // 从 URL 恢复筛选/排序/页码（刷新、分享链接、浏览器后退均可还原）
  _urlQuery = parseListQuery();
  applyQueryToInputs(_urlQuery);

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
        if (Array.from(sel.options).some(o => o.value === tag)) return; // URL 预置项去重
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
        if (Array.from(sel.options).some(o => o.value === tag)) return; // URL 预置项去重
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

// ─── URL 状态同步：把筛选/排序/页码写进 hash（#/list?tag=fbot&...），刷新/分享/后退可还原 ───
// 用 history.replaceState 而非改 location.hash，避免触发 hashchange 重渲染整个页面
function parseListQuery() {
  const hash = location.hash.replace(/^#/, '');
  const qi = hash.indexOf('?');
  if (qi < 0) return {};
  return Object.fromEntries(new URLSearchParams(hash.slice(qi + 1)));
}

// 把 URL 初值回填到筛选控件（tag/sub_tag 的选项异步加载，见 seedSelect）
function applyQueryToInputs(q) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el && v) el.value = v; };
  setVal('fSearch', q.search);
  setVal('fZMin', q.z_min); setVal('fZMax', q.z_max);
  setVal('fRAMin', q.ra_min); setVal('fRAMax', q.ra_max);
  setVal('fDecMin', q.dec_min); setVal('fDecMax', q.dec_max);
  setVal('fT0From', q.t0_from); setVal('fT0To', q.t0_to);
  const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = (v === 'true'); };
  chk('fHasZ', q.has_z); chk('fHasHost', q.has_host); chk('fHasSpectra', q.has_spectra);
  if (q.sort && q.order) {
    currentState.sort = q.sort;
    currentState.order = q.order;
    const s = document.getElementById('fSort');
    if (s) s.value = `${q.sort}|${q.order}`;
    // 表头排序箭头与 URL 一致（默认 HTML 只标了 T0）
    document.querySelectorAll('.sort-header').forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.sort === q.sort) th.classList.add(q.order === 'asc' ? 'sort-asc' : 'sort-desc');
    });
  }
  const p = parseInt(q.page, 10);
  if (Number.isFinite(p) && p > 0) currentState.page = p;
  seedSelect('fTag', q.tag);
  seedSelect('fSubTag', q.sub_tag);
}

// 标签下拉的选项异步加载：先把 URL 里的值预置成选项，保证首次 loadData 前 value 已生效
function seedSelect(id, val) {
  if (!val) return;
  const sel = document.getElementById(id);
  if (!sel) return;
  if (!Array.from(sel.options).some(o => o.value === val)) {
    const opt = document.createElement('option');
    opt.value = val;
    opt.textContent = val;
    sel.appendChild(opt);
  }
  sel.value = val;
}

// 把当前查询参数写回 URL（省略默认值，保持链接简洁）
function syncListUrl(params) {
  const q = { ...params };
  if (q.page === 1) delete q.page;              // 默认页
  delete q.per_page;                            // 每页条数固定 50
  // 排序缺值/默认值都不写进 URL（避免出现 ?sort=&order=undefined）
  if (!q.sort || !q.order || (q.sort === 't0' && q.order === 'desc')) { delete q.sort; delete q.order; }
  const qs = new URLSearchParams(q).toString();
  const url = '#/list' + (qs ? '?' + qs : '');
  if (location.hash !== url) history.replaceState(null, '', url);
}

async function loadData() {
  if (!document.getElementById('tableBody')) return;  // 已离开列表页（防抖回调迟到）
  const params = {
    page: currentState.page,
    per_page: 50,
  };

  // Read sort
  const sortEl = document.getElementById('fSort');
  if (sortEl && sortEl.value) {
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

  syncListUrl(params);  // 状态写回 URL（刷新/分享/后退可还原）

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

// 标签徽章 → 点击跳转列表并按该标签筛选（#/list?tag=.. / ?sub_tag=..，复用列表页 URL 状态同步）。
// onclick stopPropagation：避免触发行点击（进入详情页）。
function tagBadgeLink(tag, kind) {
  const cls = kind === 'tag' ? 'badge-tag' : 'badge-neutral';
  const q = kind === 'tag' ? 'tag' : 'sub_tag';
  const mr = kind === 'tag' ? '' : ' style="margin-right:4px"';
  return `<a class="${cls} badge-link" href="#/list?${q}=${encodeURIComponent(tag)}"${mr}` +
    ` onclick="event.stopPropagation()" title="筛选${kind === 'tag' ? '标签' : '副标签'}：${escAttr(tag)}">${esc(tag)}</a>`;
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
      <td>${(t.tags || []).map(tag => tagBadgeLink(tag, 'tag')).join('')}${(t.sub_tag || []).map(tag => tagBadgeLink(tag, 'sub_tag')).join('')}</td>
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

// 页码窗口（静态渲染当前页 ±5）+ 首尾快跳按钮状态；翻页/取消输入后据此重绘
function renderPager() {
  const track = document.getElementById('pgTrack');
  if (!track) return;  // 已离开列表页
  const { page, totalPages } = _pgData;
  document.getElementById('pgFirst').disabled = page <= 1;
  document.getElementById('pgLast').disabled = page >= totalPages;
  if (totalPages <= 1) { track.innerHTML = ''; track.style.transform = ''; _pgOffset = 0; return; }
  const start = Math.max(1, page - 5);
  const end = Math.min(totalPages, page + 5);
  let html = '';
  for (let i = start; i <= end; i++) {
    html += `<span class="pg-num" data-page="${i}" style="${pgNumStyle(i === page)}">${i}</span>`;
  }
  track.innerHTML = html;
  centerPager();
}

// 把当前页码平移到视口中央（窗口贴边时收敛到不露空白；内容不足一屏则整体居中），
// 基准偏移记入 _pgOffset
function centerPager() {
  const viewport = document.getElementById('pgViewport');
  const track = document.getElementById('pgTrack');
  if (!viewport || !track) { _pgOffset = 0; return; }
  let off = 0;
  if (track.scrollWidth <= viewport.clientWidth) {
    off = (viewport.clientWidth - track.scrollWidth) / 2;
  } else {
    const cur = track.querySelector(`.pg-num[data-page="${_pgData.page}"]`);
    if (cur) {
      off = viewport.clientWidth / 2 - (cur.offsetLeft + cur.offsetWidth / 2);
      off = Math.min(0, Math.max(viewport.clientWidth - track.scrollWidth, off));
    }
  }
  _pgOffset = off;
  track.style.transform = off ? `translateX(${off}px)` : '';
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

// 分页条交互：点击页码/首尾快跳、滚轮翻页、点当前页输入跳转。
// 容器在静态模板里，事件只在 render() 挂一次；页码用事件委托，避免内联 onclick
function initPager() {
  const strip = document.getElementById('pagination');
  const viewport = document.getElementById('pgViewport');
  if (!strip || !viewport) return;

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
  // 输入框内的鼠标事件不传给分页条（避免触发点击翻页）
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
