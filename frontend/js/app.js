// === App Shell + Hash Router ===
import { checkAuth, login, logout, showToast, exportTransients, exportLightcurves } from './api.js';
import { getTheme, toggleTheme } from './theme.js';

const app = document.getElementById('app');

// 导出按钮全局入口（列表页/详情页共用，避免各页重复赋值互相覆盖）
window.APIImport = { exportTransients, exportLC: exportLightcurves };

// ─── 主题切换按钮（图标随当前主题：深色显示太阳、浅色显示月亮） ───
const themeBtn = document.getElementById('themeToggle');
if (themeBtn) {
  const icon = themeBtn.querySelector('i');
  if (icon) icon.className = getTheme() === 'dark' ? 'bi bi-sun' : 'bi bi-moon';
  themeBtn.addEventListener('click', () => toggleTheme());
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
};
// Dynamic route: /transient/<id>
const detailRe = /^\/transient\/(.+)$/;

function getRoute() {
  const hash = location.hash.replace(/^#/, '') || '/';
  const path = hash.split('?')[0] || '/';  // 去掉查询串（列表页把筛选/排序/页码同步进 URL）
  const m = path.match(detailRe);
  if (m) return { page: 'detail', params: { id: m[1] } };
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
    if (route.page === 'detail') {
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
