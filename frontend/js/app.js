// === App Shell + Hash Router ===
import { api, getOverview, checkAuth, login, logout, showToast } from './api.js';
import { getTheme, toggleTheme } from './theme.js';

const app = document.getElementById('app');

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
  const m = hash.match(detailRe);
  if (m) return { page: 'detail', params: { id: m[1] } };
  const handler = routes[hash];
  if (handler) return { page: 'static', handler };
  return { page: 'static', handler: routes['/'] };
}

async function navigate() {
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
    app.innerHTML = `<div class="alert alert-danger m-3">加载页面出错: ${err.message}</div>`;
  }
  // Update active nav link
  document.querySelectorAll('#navLinks .nav-link').forEach(a => {
    const href = a.getAttribute('href');
    // /stats 的子路由（如 /stats/relations）也高亮"全局统计"；#/tools 高亮"工具箱"下拉
    const active = href === location.hash ||
      (href === '#/stats' && location.hash.startsWith('#/stats')) ||
      (a.id === 'toolsDropdown' && location.hash.startsWith('#/tools'));
    a.classList.toggle('active', active);
  });
}

// Listen for hash changes
window.addEventListener('hashchange', navigate);

// Load nav stats
async function loadNavStats() {
  try {
    const s = await getOverview();
    document.getElementById('navStats').textContent =
      `${s.n_transients} 事件 · ${s.n_lightcurves} 数据点`;
  } catch {}
}

// Bootstrap
loadNavStats();
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
