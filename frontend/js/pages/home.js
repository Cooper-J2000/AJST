// === Home Page (Swiss International Typographic Style) ===
import { app, showLoading, showError } from './layout.js';
import { api, getOverview, getFittingEngines } from '../api.js';

// 各标签页入口（编号列表式导航）
const ENTRIES = [
  { no: '01', href: '#/list',            title: '事件列表',   desc: '浏览、筛选与编辑暂现源及其多波段光变数据' },
  { no: '02', href: '#/stats',           title: '全局统计',   desc: '全天分布、红移直方图与波段覆盖情况' },
  { no: '03', href: '#/stats/relations', title: '统计关系',   desc: 'Amati 等 6 个 GRB 瞬时辐射关系与分组拟合' },
  { no: '04', href: '#/compare',         title: '多源对比',   desc: '多事件光变叠绘、静止系时间与波段筛选' },
  { no: '05', href: '#/filters',         title: '光学滤光片', desc: '滤光片波长参数与 Vega → AB 星等换算' },
  { no: '06', href: '#/new',             title: '新建事件',   desc: '录入新的暂现源事件' },
];

const fmt = (v) => (v == null ? '—' : Number(v).toLocaleString('en-US'));

function statBlock(value, label, en) {
  return `<div class="swiss-stat">
    <div class="swiss-stat-value">${value}</div>
    <div class="swiss-stat-label">${label}<span class="swiss-stat-en">${en}</span></div>
  </div>`;
}

export async function render() {
  showLoading();
  let s, tagStats = null;
  try {
    [s, tagStats] = await Promise.all([
      getOverview(),
      api('GET', '/stats/tags').catch(() => null),  // 标签统计失败不阻塞主页
    ]);
  } catch (err) {
    showError(`加载统计数据失败: ${err.message}`);
    return;
  }

  // 拟合引擎版本（接口失败时静默不显示）
  let engineVer = null;
  try {
    const engines = await getFittingEngines();
    const v = engines && engines[0] && engines[0].version;
    if (v) engineVer = String(v).replace(/[<>&"']/g, '');
  } catch {}
  // 版本号跟在包名后（小字号、次要色）；接口失败只显示包名
  const fittingValue = engineVer
    ? `VegasAfterglow <span class="text-secondary" style="font-size:0.7rem;font-weight:400;text-transform:none">v${engineVer}</span>`
    : 'VegasAfterglow';

  const entriesHtml = ENTRIES.map(e => `
    <a class="swiss-entry" href="${e.href}">
      <span class="swiss-entry-no">${e.no}</span>
      <span class="swiss-entry-title">${e.title}</span>
      <span class="swiss-entry-desc">${e.desc}</span>
      <span class="swiss-entry-arrow">→</span>
    </a>`).join('');

  // ── 标签分布：每行一个 tag + 目标数，点击展开该 tag 的 sub_tag 明细 ──
  let tagsHtml = '';
  if (tagStats && tagStats.tags && tagStats.tags.length) {
    tagsHtml = `
    <hr class="swiss-rule">
    <section>
      <div class="swiss-kicker swiss-kicker-block">Tags — 标签分布<small class="swiss-kicker-hint">（点击展开子标签）</small></div>
      <div class="swiss-tags">
        ${tagStats.tags.map(t => {
          const subs = (tagStats.sub_by_tag || {})[t.tag] || [];
          const noSub = (tagStats.no_sub || {})[t.tag] || 0;
          const rows = [...subs.map(x => ({ name: x.sub_tag, count: x.count })),
                        ...(noSub ? [{ name: '未标注', count: noSub }] : [])];
          return `
        <div class="swiss-tag-item">
          <div class="swiss-tag-row" onclick="this.parentElement.classList.toggle('open')">
            <span class="swiss-tag-name">${t.tag}</span>
            <span></span>
            <span class="swiss-tag-count">${fmt(t.count)}</span>
            <span class="swiss-tag-toggle">+</span>
          </div>
          <div class="swiss-tag-subs">
            ${rows.length ? rows.map(r => `
            <div class="swiss-tag-subrow"><span>${r.name}</span><span>${fmt(r.count)}</span></div>`).join('')
            : '<div class="swiss-tag-subrow text-secondary"><span>（无子标签）</span><span></span></div>'}
          </div>
        </div>`;
        }).join('')}
      </div>
    </section>`;
  }

  app.innerHTML = `
  <div class="swiss-home">
    <!-- Hero：非对称网格，左 7 列超大标题，右 4 列统计 -->
    <section class="row swiss-hero">
      <div class="col-lg-7">
        <div class="swiss-kicker">AJST — Transient Lightcurve Catalog</div>
        <h1 class="swiss-title">暂现源<br>光变目录</h1>
        <p class="swiss-lede">多波段暂现源光变数据库：汇聚外部文献目录的 GRB 等暂现源光变，支持全天统计、瞬时辐射统计关系分析与 VegasAfterglow 余辉拟合。</p>
      </div>
      <div class="col-lg-4 offset-lg-1">
        <div class="swiss-kicker swiss-kicker-block">Overview — 基础统计</div>
        ${statBlock(fmt(s.n_transients), '暂现源事件', 'Transients')}
        ${statBlock(fmt(s.n_lightcurves), '光变数据点', 'Data Points')}
        ${statBlock(fmt(s.n_with_redshift), '含红移', 'With Redshift')}
        ${statBlock(fmt(s.n_bands), '波段', 'Bands')}
        ${statBlock(fmt(s.n_telescopes), '望远镜', 'Telescopes')}
      </div>
    </section>

    <hr class="swiss-rule">

    <!-- 三个静态事实 -->
    <section class="row swiss-facts">
      <div class="col-md-4 swiss-fact">
        <div class="swiss-fact-value">14</div>
        <div class="swiss-stat-label">外部文献目录<span class="swiss-stat-en">External Catalogs</span></div>
      </div>
      <div class="col-md-4 swiss-fact">
        <div class="swiss-fact-value">6</div>
        <div class="swiss-stat-label">统计关系<span class="swiss-stat-en">Correlations</span></div>
      </div>
      <div class="col-md-4 swiss-fact">
        <div class="swiss-fact-value">${fittingValue}</div>
        <div class="swiss-stat-label">余辉拟合<span class="swiss-stat-en">Afterglow Fitting</span></div>
      </div>
    </section>
    ${tagsHtml}

    <hr class="swiss-rule">

    <!-- 编号入口列表 -->
    <section>
      <div class="swiss-kicker swiss-kicker-block">Index — 各标签页入口</div>
      <nav class="swiss-entries">${entriesHtml}</nav>
    </section>
  </div>`;
}
