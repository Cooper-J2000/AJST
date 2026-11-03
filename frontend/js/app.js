// === App Shell + Hash Router ===
import { checkAuth, login, logout, showToast, exportTransients, exportLightcurves } from './api.js';
import { getTheme, setTheme, getBgPalette, setBgPalette, BG_PALETTES } from './theme.js';

const app = document.getElementById('app');

// 导出按钮全局入口（列表页/详情页共用，避免各页重复赋值互相覆盖）
window.APIImport = { exportTransients, exportLC: exportLightcurves };

// ─── 主题悬浮窗（单击顶栏按钮 → 按钮就近下方弹出小窗：配色 + 明暗两行） ───
const themeBtn = document.getElementById('themeToggle');
if (themeBtn) initThemePanel(themeBtn);

function initThemePanel(btn) {
  const panel = document.createElement('div');
  panel.id = 'themePanel';
  panel.className = 'theme-panel';
  panel.style.display = 'none';

  // 行 1：配色（7 个马卡龙色点，纯色显示浅色代表色）
  const row1 = document.createElement('div');
  row1.className = 'theme-panel-row';
  const label1 = document.createElement('span');
  label1.className = 'theme-panel-label';
  label1.textContent = '配色';
  const swatches = document.createElement('div');
  swatches.className = 'theme-panel-swatches';
  BG_PALETTES.forEach(p => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'theme-swatch';
    b.title = p.name;
    b.dataset.palette = p.id;
    b.style.background = p.color;
    b.addEventListener('click', () => { setBgPalette(p.id); refreshPanel(); });
    swatches.appendChild(b);
  });
  row1.append(label1, swatches);

  // 行 2：明暗（沿用 data-bs-theme；切换会整页刷新以重绘图表，setTheme 内含抠图页未保存确认）
  const row2 = document.createElement('div');
  row2.className = 'theme-panel-row';
  const label2 = document.createElement('span');
  label2.className = 'theme-panel-label';
  label2.textContent = '明暗';
  [['light', 'bi-sun', '明'], ['dark', 'bi-moon', '暗']].forEach(([mode, icon, text]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'theme-mode-btn';
    b.dataset.mode = mode;
    const i = document.createElement('i');
    i.className = `bi ${icon}`;
    b.append(i, ` ${text}`);
    b.addEventListener('click', () => { setTheme(mode); refreshPanel(); });
    row2.appendChild(b);
  });
  row2.prepend(label2);

  panel.append(row1, row2);
  document.body.appendChild(panel);

  function refreshPanel() {
    const cur = getBgPalette();
    swatches.querySelectorAll('.theme-swatch').forEach(s =>
      s.classList.toggle('active', s.dataset.palette === cur));
    const mode = getTheme();
    panel.querySelectorAll('.theme-mode-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.mode === mode));
  }

  function openPanel() {
    refreshPanel();
    panel.style.display = 'block';
    // 定位到按钮就近下方、右缘与按钮对齐，并夹进视口
    const r = btn.getBoundingClientRect();
    const pw = panel.offsetWidth;
    const left = Math.max(8, Math.min(r.right - pw, window.innerWidth - pw - 8));
    panel.style.left = `${left}px`;
    panel.style.top = `${r.bottom + 8}px`;
  }
  const closePanel = () => { panel.style.display = 'none'; };
  const isOpen = () => panel.style.display !== 'none';

  btn.addEventListener('click', e => {
    e.stopPropagation();
    if (isOpen()) closePanel(); else openPanel();
  });
  panel.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', closePanel);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanel(); });
  window.addEventListener('resize', () => { if (isOpen()) openPanel(); });
}

// Route map
const routes = {
  '/':       () => import('./pages/home.js').then(m => m.render()),
  '/list':   () => import('./pages/list.js').then(m => m.render()),
  '/stats':  () => import('./pages/stats.js').then(m => m.render()),
  '/stats/relations': () => import('./pages/relations.js').then(m => m.render()),
  '/stats/hosts': () => import('./pages/stats_hosts.js').then(m => m.render()),
  '/compare': () => import('./pages/compare.js').then(m => m.render()),
  '/new':     () => import('./pages/create.js').then(m => m.render({})),
  '/filters':  () => import('./pages/filters.js').then(m => m.render()),
  '/tools/gcn': () => import('./pages/gcn_tool.js').then(m => m.render()),
  '/tools/digitizer': () => import('./pages/digitizer.js').then(m => m.render()),
  '/tools/tmplib': () => import('./pages/tmplib.js').then(m => m.render()),
};
// Dynamic route: /transient/<id>
const detailRe = /^\/transient\/(.+)$/;
// 「光谱 × 滤光片」工具：#/tools/specphot（空工作台）与 #/tools/specphot/<spectrum_id> 共用一分支（R-28）
const specphotRe = /^\/tools\/specphot\/?(.*)$/;

function getRoute() {
  const hash = location.hash.replace(/^#/, '') || '/';
  const path = hash.split('?')[0] || '/';  // 去掉查询串（列表页把筛选/排序/页码同步进 URL）
  const m = path.match(detailRe);
  if (m) return { page: 'detail', params: { id: m[1] } };
  const sp = path.match(specphotRe);   // 空串 = 空工作台（/(.*) 允许空匹配）
  if (sp) return { page: 'specphot', params: { spectrum_id: sp[1] } };
  const handler = routes[path];
  if (handler) return { page: 'static', handler };
  return { page: 'static', handler: routes['/'] };
}

// 全局导航计数：每次路由切换 +1，异步页面 await 后比对（layout.js navSeq/navStale），丢弃过期渲染
let _navSeq = 0;

async function navigate() {
  window._ajstNavSeq = ++_navSeq;
  const route = getRoute();
  try {
    if (route.page === 'specphot') {
      await import('./specphot/tool_specphot.js').then(m => m.render(route.params.spectrum_id));
    } else if (route.page === 'detail') {
      const mod = await import('./pages/detail.js');
      await mod.render(route.params.id);
    } else {
      await route.handler();
    }
  } catch (err) {
    console.error('Route error:', err);
    app.innerHTML = '';
    const div = document.createElement('div');
    div.className = 'alert alert-danger m-3';
    div.textContent = `加载页面出错: ${err.message}`;
    app.appendChild(div);
  }
  // Update active nav link（比较时忽略查询串，避免 #/list?tag=... 匹配不上 #/list）
  const curHash = '#' + (location.hash.replace(/^#/, '').split('?')[0] || '/');
  document.querySelectorAll('#navLinks .nav-link').forEach(a => {
    const href = a.getAttribute('href');
    // /stats 的子路由（如 /stats/relations）也高亮"全局统计"；#/tools 高亮"工具箱"下拉
    const active = href === curHash ||
      (href === '#/stats' && curHash.startsWith('#/stats')) ||
      (a.id === 'toolsDropdown' && curHash.startsWith('#/tools'));
    a.classList.toggle('active', active);
  });
}

// Listen for hash changes
window.addEventListener('hashchange', navigate);

// Bootstrap
checkAuthStatus();
navigate();

// ─── 全局鉴权函数（被 index.html 中 onclick 调用） ───
window.showLoginModal = () => {
  const modal = new bootstrap.Modal(document.getElementById('loginModal'));
  document.getElementById('loginUsername').value = '';
  document.getElementById('loginPassword').value = '';
  document.getElementById('loginError').style.display = 'none';
  modal.show();
  setTimeout(() => document.getElementById('loginUsername').focus(), 300);
};

window.doLogin = async () => {
  const username = document.getElementById('loginUsername').value.trim();
  const pwd = document.getElementById('loginPassword').value;
  if (!username || !pwd) return;
  try {
    await login(username, pwd);
    bootstrap.Modal.getInstance(document.getElementById('loginModal')).hide();
    showToast('登录成功', 'success');
    setTimeout(() => location.reload(), 300);
  } catch (err) {
    document.getElementById('loginError').textContent = '用户名或密码错误';
    document.getElementById('loginError').style.display = 'block';
  }
};

window.doLogout = async () => {
  await logout();
  showToast('已退出', 'info');
  setTimeout(() => location.reload(), 300);
};

async function checkAuthStatus() {
  try {
    const status = await checkAuth();
    const authed = status.authenticated;
    const admin = authed && status.role === 'admin';
    document.getElementById('authStatus').style.display = authed ? 'inline' : 'none';
    if (authed) {
      document.getElementById('authStatus').textContent =
        `已登录: ${status.username}${admin ? '（管理员）' : ''}`;
    }
    document.getElementById('adminBtn').style.display = admin ? 'inline-block' : 'none';
    document.getElementById('loginBtn').style.display = authed ? 'none' : 'inline-block';
    document.getElementById('logoutBtn').style.display = authed ? 'inline-block' : 'none';
  } catch {}
}

// 任意 API 请求返回 401 且此前处于登录态时（会话过期）：同步顶栏登录状态并提示重新登录
// （api.js 仅在 _authed 翻转时派发一次；toast 再做短时间去重兜底）
let _authExpiredToastAt = 0;
document.addEventListener('ajst:auth-expired', () => {
  const now = Date.now();
  if (now - _authExpiredToastAt > 3000) {
    _authExpiredToastAt = now;
    showToast('登录已过期，请重新登录', 'warning');
  }
  checkAuthStatus();
});
