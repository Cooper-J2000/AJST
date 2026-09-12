// === Shared layout helpers ===
export const app = document.getElementById('app');

export function showLoading() {
  app.innerHTML = `<div class="d-flex justify-content-center align-items-center" style="min-height: 60vh;">
    <div class="spinner-border" role="status"><span class="visually-hidden">加载中...</span></div>
  </div>`;
}

export function showError(msg) {
  app.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'alert alert-danger m-3';
  div.textContent = msg;
  app.appendChild(div);
}

// 全局导航序号（app.js 每次路由切换递增）。异步页在 render 开头取 navSeq()，
// await 完成后用 navStale(seq) 比对，true 表示用户已切换到其它路由，应丢弃过期渲染
export function navSeq() { return window._ajstNavSeq || 0; }
export function navStale(seq) { return navSeq() !== seq; }

// 全局统计页子页面 tab 栏（概览 / 统计关系 / 宿主星系）
export function statsTabs(active) {
  return `<ul class="nav nav-tabs mb-3">
    <li class="nav-item"><a class="nav-link ${active === 'overview' ? 'active' : ''}" href="#/stats">概览</a></li>
    <li class="nav-item"><a class="nav-link ${active === 'relations' ? 'active' : ''}" href="#/stats/relations">统计关系</a></li>
    <li class="nav-item"><a class="nav-link ${active === 'hosts' ? 'active' : ''}" href="#/stats/hosts">宿主星系</a></li>
  </ul>`;
}
