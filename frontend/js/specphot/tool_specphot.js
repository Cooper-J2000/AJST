// === 「光谱 × 滤光片」工具入口（工具箱子页面，#/tools/specphot[/<spectrum_id>]） ===
// F-0i1：唯一宿主页面；无参 = 空工作台，带参 = 装载该库内谱。两入口共用本模块。
// 装载外壳见 workbench.js（来源条 / 徽章条 / 波段与参数区 / 锚点表 / 结果区 / 页签）。
// 本文件只做：DOM 容器装配 + 路由参数解析 + 生命周期（render/destroy，照 sed_tab 约定）。
import { app } from '../pages/layout.js';
import { mountWorkbench, unmountWorkbench } from './workbench.js';

let _root = null;

export async function render(spectrumId = '') {
  // F-0i4：URL 只承载 spectrum_id 一个量；空串 = 空工作台（specphotRe 用 (.*)）
  unmountWorkbench();                 // 重入安全：先清理上一轮的定时器/在途请求
  app.innerHTML = '';
  _root = document.createElement('div');
  _root.id = 'specphotRoot';
  _root.className = 'specphot-root';
  app.appendChild(_root);
  const sid = String(spectrumId || '').replace(/\/+$/, '').trim();
  const asNum = /^\d+$/.test(sid) ? Number(sid) : null;
  mountWorkbench(_root, asNum);       // 非法 id 按空工作台处理，由来源条提示
}

// R-16 销毁约定：收起工作台时停定时器、abort 在途请求、解除 DOM 引用，防泄漏。
// F-0i8：模块级状态（参数 / 手加锚点行 / 上传件数组）按宿主惯例跨路由保留在内存，
// 但绝不写 sessionStorage 之外任何介质，上传谱数组本身连 sessionStorage 都不写（IA-11）。
export function destroy() {
  unmountWorkbench();
  _root = null;
}
