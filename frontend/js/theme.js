// === 主题管理（浅色 / 深色双主题） ===
// 持久化: localStorage('ajst-theme')；未设置时跟随 prefers-color-scheme；默认深色。
// 图表颜色在建图时通过 chartColors() 读取当前主题；切换主题后 reload 即生效（hash 路由状态保留）。

const STORAGE_KEY = 'ajst-theme';

export function getTheme() {
  try {
    const t = localStorage.getItem(STORAGE_KEY);
    if (t === 'light' || t === 'dark') return t;
  } catch {}
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) return 'light';
  return 'dark';
}

export function setTheme(t) {
  if (t !== 'light' && t !== 'dark') return;
  try { localStorage.setItem(STORAGE_KEY, t); } catch {}
  document.documentElement.setAttribute('data-bs-theme', t);
  location.reload();
}

export function toggleTheme() {
  setTheme(getTheme() === 'dark' ? 'light' : 'dark');
}

// ─── 图表配色 ───
// 深色沿用 GitHub-dark 色系；浅色为对比度足够的对应色。
const CHART_DARK = {
  tick: '#8b949e',
  tickSub: '#6e7681',
  grid: '#30363d',
  gridSoft: '#21262d',
  legend: '#c9d1d9',
  tooltipBg: '#1c2128',
  tooltipText: '#e6edf3',
  canvasBg: '#161b22',
  errorBar: 'rgba(139,148,158,0.5)',
  selectFill: 'rgba(88,166,255,0.15)',
  selectStroke: 'rgba(88,166,255,0.8)',
  // 详情页叠加拟合线（含近白色，仅深色可用）
  fits: ['#f0f6fc', '#ffa657', '#79c0ff', '#ff7b72', '#d2a8ff', '#7ee787', '#e3b341', '#56d4dd'],
  // 拟合页波段色
  bands: ['#58a6ff', '#f778ba', '#3fb950', '#d29922', '#bc8cff', '#56d4dd',
    '#ffa657', '#ff7b72', '#7ee787', '#e3b341', '#79c0ff', '#d2a8ff'],
  // 对比页源色
  compare: ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#56d4dd',
    '#ff7b72', '#a5d6ff', '#7ee787', '#e3b341', '#ffa198', '#d2a8ff', '#39d2c0'],
  // 关系统计：I 红 / II 蓝 / 未知灰 与各自拟合线
  type: { I: '#f85149', II: '#58a6ff', unknown: '#8b949e' },
  typeFit: { I: '#ffa198', II: '#79c0ff', all: '#3fb950' },
  // 特殊标记层：亮黄实心星 + 白描边（深色底醒目）
  highlight: '#ffd60a',
  highlightEdge: '#ffffff',
};

const CHART_LIGHT = {
  tick: '#57606a',
  tickSub: '#8c959f',
  grid: '#d0d7de',
  gridSoft: '#e7ebef',
  legend: '#24292f',
  tooltipBg: '#24292f',
  tooltipText: '#f6f8fa',
  canvasBg: '#ffffff',
  errorBar: 'rgba(87,96,106,0.65)',
  selectFill: 'rgba(9,105,218,0.12)',
  selectStroke: 'rgba(9,105,218,0.75)',
  fits: ['#24292f', '#bc4c00', '#0969da', '#cf222e', '#8250df', '#1a7f37', '#9a6700', '#0a7ea4'],
  bands: ['#0969da', '#bf3989', '#1a7f37', '#9a6700', '#8250df', '#0a7ea4',
    '#e16f24', '#cf222e', '#116329', '#bf8700', '#0550ae', '#6639ba'],
  compare: ['#0969da', '#1a7f37', '#9a6700', '#cf222e', '#8250df', '#0a7ea4',
    '#e16f24', '#0550ae', '#116329', '#bf8700', '#a40e26', '#6639ba', '#0e8a16'],
  type: { I: '#cf222e', II: '#0969da', unknown: '#6e7781' },
  typeFit: { I: '#a40e26', II: '#0550ae', all: '#1a7f37' },
  // 特殊标记层：亮黄实心星 + 暗金描边（浅色底醒目）
  highlight: '#ffd60a',
  highlightEdge: '#7a5c00',
};

export function chartColors() {
  return getTheme() === 'light' ? CHART_LIGHT : CHART_DARK;
}

// ─── 学术绘图风格 ───
// 衬线字体（西文 Times 系 + 中文衬线回退），供各图表的刻度/轴标题/图例使用
export const ACADEMIC_FONT = "'Times New Roman', 'STIX Two Text', 'Noto Serif SC', 'Songti SC', 'SimSun', serif";
// 图表字体预设：刻度 / 轴标题 / 图例
export function academicFonts() {
  return {
    tick: { family: ACADEMIC_FONT, size: 12 },
    title: { family: ACADEMIC_FONT, size: 14 },
    legend: { family: ACADEMIC_FONT, size: 13 },
  };
}
