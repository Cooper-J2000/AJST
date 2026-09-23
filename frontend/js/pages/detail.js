// === Transient Detail Page（编排层） ===
// 职责：拉取数据、拼页面模板、Tab 切换调度、向各功能区模块分发初始化。
// 功能区实现（概览/数据表/光变图/derived/外部目录/光谱/Aladin）在 detail_*.js 子模块；
// 子模块经 { render, currentTid } 循环引用回调本模块（仅函数声明导出 + 运行时读 let
// 实时绑定，模块求值期不访问，ESM 循环安全）。
import { app, showLoading, showError, navSeq, navStale } from './layout.js';
import {
  getTransient, getLightcurves, getFilters, getArticles, getSpectra,
  isAuthed, isAdmin,
} from '../api.js';
import { initFittingTab, destroyFittingTab } from './fitting_tab.js';
import { initHostfitTab, destroyHostfitTab } from './hostfit_tab.js';
import { initSedTab, destroySedTab } from './sed_tab.js';
import { ensureFilterCache, buildSpectralColors } from '../bands.js';
import { esc, escAttr } from '../utils.js';
import { initOverview, articlesHTML, fillHostSummary, attachEditCoordHints, attachEditTagInputs } from './detail_overview.js';
import { LC_COLS, lcRowHTML, setLCItems, buildLcColPanel, applyLcColVis } from './detail_lctable.js';
import { setLCSourceParams, resetLCChart, initLCPlot, wireLCChartGlobals, t0ToMJD, setLCSpectra } from './detail_lcchart.js';
import { renderDerivedCard, initDerived, resetDerived } from './detail_derived.js';
import { renderCatalogData } from './detail_catalog.js';
import { initSpectraTab, resetSpectra, getSpecAxisType } from './detail_spectra.js';
import { initAladin, resetAladin } from './detail_aladin.js';

export let currentTid = null;

// ─── 当前激活的 tab（render 整页重建后恢复，如添加数据后停留在数据表页；切换源时复位概览） ───
let activeTab = 'overview';
let activeTabTid = null;

// 激活的 tab 同步进 URL（#/transient/<id>?tab=xxx），刷新后可恢复
function readUrlTab() {
  const hash = location.hash.replace(/^#/, '');
  const qi = hash.indexOf('?');
  if (qi < 0) return null;
  return new URLSearchParams(hash.slice(qi + 1)).get('tab');
}
function syncUrlTab(tid, tab) {
  const base = '#/transient/' + tid;
  const url = tab && tab !== 'overview' ? `${base}?tab=${encodeURIComponent(tab)}` : base;
  if (location.hash !== url) history.replaceState(null, '', url);
}

// ─── 主渲染函数 ───
export async function render(tid) {
  currentTid = tid;
  // 切源复位概览；刷新时从 URL 恢复此前激活的 tab
  if (activeTabTid !== tid) { activeTab = readUrlTab() || 'overview'; activeTabTid = tid; }
  window.currentTid = tid;  // 供内联 onclick（如导出按钮/文章编辑取消）引用当前源
  const seq = navSeq();     // 导航序号：请求期间切到其它路由则丢弃本次渲染
  // 重置各功能区状态（DOM 即将重建）
  resetLCChart();
  resetSpectra();
  destroyFittingTab(); // 停止拟合标签页轮询/图表（DOM 即将重建）
  destroyHostfitTab(); // 停止宿主星系标签页轮询（DOM 即将重建）
  destroySedTab(); // 停止 SED 分析标签页轮询/图表（DOM 即将重建）
  resetDerived();
  resetAladin();  // Aladin 容器随 DOM 重建销毁，旧实例引用必须清空（否则切源后全天图空白）
  showLoading();

  try {
    const [transient, lcData, filtersData, articles] = await Promise.all([
      getTransient(tid),
      getLightcurves({ transient_id: tid, per_page: 9999 }),
      getFilters(),
      getArticles(tid),
    ]);
    if (currentTid !== tid || navStale(seq)) return;  // 请求期间已切换源/路由，丢弃过期响应
    setLCItems(lcData.items, tid);
    ensureFilterCache(filtersData);
    initOverview(transient, articles);
    // 光变图用的源级参数（注入 detail_lcchart.js；模板里的开关可用性用本地副本判断）
    setLCSourceParams({ redshift: transient.redshift, t0: transient.t0, distmod: transient.distmod, name: transient.id });
    // 光谱观测时刻注入光变图（独立请求，不阻塞渲染；只取原始谱 parent_id 为空者，
    // mjd 缺失的丢弃；接口失败静默传 null。每次 render 重新注入，重建图表后仍有效）
    getSpectra(tid).then(list => {
      if (currentTid !== tid) return;
      const specPts = (Array.isArray(list) ? list : [])
        .filter(s => !s.parent_id)
        .map(s => ({ mjd: s.extra_data?.mjd, instrument: s.instrument, observation_date: s.observation_date }))
        .filter(s => s.mjd != null);
      setLCSpectra(specPts);
    }).catch(() => { if (currentTid === tid) setLCSpectra(null); });
    const canRestFrame = (transient.redshift != null && transient.redshift > -1);
    const canAbsMag = transient.distmod != null;
    const canMJD = t0ToMJD(transient.t0) != null;

    // 按波段分组
    const bands = {};
    for (const pt of lcData.items) {
      if (!bands[pt.band]) bands[pt.band] = [];
      bands[pt.band].push(pt);
    }
    const bandNames = Object.keys(bands);
    const spectralColors = buildSpectralColors(bandNames);
    const specAxisType = getSpecAxisType();  // 光谱图轴类型跨 render 保持
    const hasCoords = (transient.ra != null && transient.dec != null);
    // 无坐标源：Aladin 照常渲染，指向银河系中心（Sgr A* 方向）作占位展示
    const aladinRa = hasCoords ? transient.ra : 266.405;
    const aladinDec = hasCoords ? transient.dec : -28.936;

    app.innerHTML = `
      <div class="mb-3">
        <a href="#/list" class="text-secondary text-decoration-none small"><i class="bi bi-arrow-left"></i> 返回列表</a>
      </div>

      <!-- Header -->
      <div class="d-flex justify-content-between align-items-start mb-3">
        <div>
          <h4 class="mb-1"><strong>${esc(transient.id)}</strong>
            <small class="text-secondary ms-2 fs-6">
              ${esc((transient.aliases || []).join(', '))}
            </small>
          </h4>
          <div class="text-secondary small">
            ${(transient.tags || []).map(t => `<span class="badge-tag">${esc(t)}</span>`).join('')}
            ${(transient.sub_tag || []).map(t => `<span class="badge-neutral me-1">${esc(t)}</span>`).join('')}
          </div>
        </div>
        <div class="d-flex gap-1">
          <button class="btn btn-sm btn-outline-warning" onclick="runGextSource()" id="gextBtn" title="对本源全部光学数据执行银河系消光改正">
            <i class="bi bi-moon-stars"></i> 银消改正
          </button>
          <button class="btn btn-sm btn-outline-secondary" onclick="handleEditClick()" id="editBtn" title="编辑">
            <i class="bi bi-pencil"></i>
          </button>
          <button class="btn btn-sm btn-outline-secondary" onclick="exportLCWithRef()" title="导出光变CSV（可选基准时刻）">
            <i class="bi bi-download"></i>
          </button>
        </div>
      </div>

      <!-- Tabs -->
      <ul class="nav nav-tabs mb-3" id="detailTabs">
        <li class="nav-item"><a class="nav-link active" href="#" data-tab="overview">概览</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="lc">光变曲线</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="data">数据表</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="fitting">余辉拟合</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="host">宿主星系</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="spectra">光谱数据</a></li>
        <li class="nav-item"><a class="nav-link" href="#" data-tab="sed">SED 分析</a></li>
      </ul>

      <div id="tabContent">
        <!-- ─── 概览页（含 Aladin 方形全天图） ─── -->
        <div id="tab-overview" class="tab-pane active">
          <div class="row g-3 align-items-stretch">
            <div class="col-md-4">
              <div class="card h-100" id="metaCard">
                <div class="card-header"><i class="bi bi-info-circle"></i> 基本信息</div>
                <div class="card-body">
                  <table class="table table-sm table-borderless" id="metaTable" style="table-layout:fixed">
                    <tbody>
                      <tr><td class="text-secondary" style="width:140px">RA</td><td>${transient.ra != null ? transient.ra.toFixed(6) : '-'}</td></tr>
                      <tr><td class="text-secondary">Dec</td><td>${transient.dec != null ? transient.dec.toFixed(6) : '-'}</td></tr>
                      <tr><td class="text-secondary">T0</td><td>${esc(transient.t0) || '-'}</td></tr>
                      <tr><td class="text-secondary">T0 引用</td><td class="small">${esc(transient.t0_ref) || '-'}</td></tr>
                      <tr><td class="text-secondary">T0 偏移量(秒)</td><td>${transient.t0_offset != null ? transient.t0_offset : '-'}</td></tr>
                      <tr><td class="text-secondary">T0 偏移量引用</td><td class="small">${esc(transient.t0_offset_ref) || '-'}</td></tr>
                      <tr><td class="text-secondary">触发仪器</td><td>${esc(transient.trigger_instrument) || '-'}</td></tr>
                      <tr><td class="text-secondary">红移</td><td>${transient.redshift != null ? `${transient.redshift} (${esc(transient.redshift_type) || '?'})` : '<span class="text-secondary">未知</span>'}</td></tr>
                      <tr><td class="text-secondary">红移引用</td><td class="small">${esc(transient.redshift_ref) || '-'}</td></tr>
                      <tr><td class="text-secondary">位置误差</td><td>${transient.pos_error != null ? `${transient.pos_error} ${esc(transient.pos_error_unit) || 'arcsec'}` : '-'}</td></tr>
                      <tr><td class="text-secondary" style="width:140px">位置引用</td><td class="small">${esc(transient.pos_ref) || '-'}</td></tr>
                      <tr><td class="text-secondary">备注</td><td class="small">${esc(transient.comment) || '-'}</td></tr>
                      <tr><td class="text-secondary">研究文章</td><td class="small" id="articleCell">${articlesHTML()}</td></tr>
                      <tr><td class="text-secondary">数据点</td><td>${lcData.total}</td></tr>
                      <tr><td class="text-secondary">宿主星系</td><td class="small" id="hostSummaryCell"><span class="text-secondary">加载中…</span></td></tr>
                    </tbody>
                  </table>
                </div>
              </div>
              <!-- 编辑面板（默认隐藏） -->
              <div class="card" id="editPanel" style="display:none">
                <div class="card-header"><i class="bi bi-pencil"></i> 编辑基本信息</div>
                <div class="card-body">
                  <form onsubmit="return false">
                    <div class="row g-2">
                      <div class="col-6"><label class="form-label small">RA</label><input type="text" class="form-control form-control-sm" id="editRa" value="${escAttr(transient.ra ?? '')}" placeholder="度 或 08h08m27.4s"></div>
                      <div class="col-6"><label class="form-label small">Dec</label><input type="text" class="form-control form-control-sm" id="editDec" value="${escAttr(transient.dec ?? '')}" placeholder="度 或 +40d36m44.8s"></div>
                      <div class="col-6"><label class="form-label small">T0</label><input type="text" class="form-control form-control-sm" id="editT0" value="${escAttr(transient.t0)}"></div>
                      <div class="col-6"><label class="form-label small">T0 引用</label><input type="text" class="form-control form-control-sm" id="editT0Ref" value="${escAttr(transient.t0_ref)}"></div>
                      <div class="col-6"><label class="form-label small">T0 偏移量(秒)</label><input type="number" class="form-control form-control-sm" id="editT0Offset" step="any" value="${transient.t0_offset ?? ''}" title="正=向后，负=提前"></div>
                      <div class="col-6"><label class="form-label small">T0 偏移量引用</label><input type="text" class="form-control form-control-sm" id="editT0OffsetRef" value="${escAttr(transient.t0_offset_ref)}"></div>
                      <div class="col-6"><label class="form-label small">触发仪器</label><input type="text" class="form-control form-control-sm" id="editTrigger" value="${escAttr(transient.trigger_instrument)}"></div>
                      <div class="col-4"><label class="form-label small">红移</label><input type="number" class="form-control form-control-sm" id="editZ" step="any" value="${transient.redshift ?? ''}"></div>
                      <div class="col-4"><label class="form-label small">红移类型</label>
                        <select class="form-select form-select-sm" id="editZType"><option value="">-</option><option value="value" ${transient.redshift_type==='value'?'selected':''}>value</option><option value="phot_z" ${transient.redshift_type==='phot_z'?'selected':''}>phot_z</option><option value="spec" ${transient.redshift_type==='spec'?'selected':''}>spec</option><option value="spec-host" ${transient.redshift_type==='spec-host'?'selected':''}>spec-host</option><option value="upperlimit" ${transient.redshift_type==='upperlimit'?'selected':''}>upperlimit</option></select>
                      </div>
                      <div class="col-4"><label class="form-label small">红移引用</label><input type="text" class="form-control form-control-sm" id="editZRef" value="${escAttr(transient.redshift_ref)}"></div>
                      <div class="col-4"><label class="form-label small">位置误差</label><input type="number" class="form-control form-control-sm" id="editPosErr" step="any" value="${transient.pos_error ?? ''}"></div>
                      <div class="col-8"><label class="form-label small">位置引用</label><input type="text" class="form-control form-control-sm" id="editPosRef" value="${escAttr(transient.pos_ref)}"></div>
                      <div class="col-12"><label class="form-label small">备注</label><textarea class="form-control form-control-sm" id="editComment" rows="2">${esc(transient.comment)}</textarea></div>
                      <div class="col-4"><label class="form-label small" title="输入后按回车或逗号固化；退格删除最后一个">标签</label><div id="editTags"></div></div>
                      <div class="col-4"><label class="form-label small" title="输入后按回车或逗号固化；退格删除最后一个">子标签</label><div id="editSubTags"></div></div>
                      <div class="col-4"><label class="form-label small">别名 (逗号分隔)</label><input type="text" class="form-control form-control-sm" id="editAliases" value="${escAttr((transient.aliases || []).join(', '))}"></div>
                    </div>
                    <div class="mt-3 d-flex gap-2">
                      <button class="btn btn-sm btn-primary" onclick="saveDetailEdit()"><i class="bi bi-check-lg"></i> 保存</button>
                      <button class="btn btn-sm btn-outline-secondary" onclick="handleEditClick()">取消</button>
                    </div>
                  </form>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card h-100">
                <div class="card-header"><i class="bi bi-bar-chart"></i> 光变概览</div>
                <div class="card-body">
                  <div class="row g-2">
                    <div class="col-6"><div class="stat-card"><div class="stat-value">${bandNames.length}</div><div class="stat-label">波段</div></div></div>
                    <div class="col-6"><div class="stat-card"><div class="stat-value">${lcData.total}</div><div class="stat-label">数据点</div></div></div>
                  </div>
                  <div class="mt-2">
                    <strong class="small text-secondary">波段覆盖:</strong>
                    <div class="mt-1 d-flex flex-wrap gap-1">
                      ${bandNames.map(b => `<span class="badge-tag" style="background:${spectralColors[b] || '#58a6ff'}33;color:${spectralColors[b] || '#58a6ff'}">${esc(b)}</span>`).join('')}
                    </div>
                  </div>
                  <div class="mt-3">
                    <strong class="small text-secondary">望远镜:</strong>
                    <div class="mt-1 small text-secondary">
                      ${esc([...new Set(lcData.items.map(p => p.telescope).filter(Boolean))].join(', '))}
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card h-100">
                <div class="card-header"><i class="bi bi-globe2"></i> Aladin 全天图
                  ${hasCoords
                    ? `<small class="text-secondary">RA=${transient.ra.toFixed(4)}° Dec=${transient.dec.toFixed(4)}°</small>`
                    : '<small class="text-warning">该源暂无坐标，图示为银河系中心</small>'}
                </div>
                <div class="card-body p-0 d-flex flex-grow-1">
                  <div id="aladinContainer" style="width:100%;min-height:520px;flex:1;border-radius:0 0 8px 8px;background:#000"></div>
                </div>
              </div>
            </div>
          </div>
          <!-- 外部目录参数（T90/Epeak/fluence/Eiso 等，含来源） -->
          ${renderCatalogData(transient.extra_data)}
          <!-- 派生物理量 (derived) -->
          ${renderDerivedCard(transient.extra_data)}
        </div>

        <!-- ─── 光变曲线 ─── -->
        <div id="tab-lc" class="tab-pane" style="display:none">
          <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
              <span><i class="bi bi-graph-up"></i> 光变曲线</span>
              <div class="d-flex flex-column gap-1">
                <!-- 第一排：数据/Y/X/顶部轴 四个 select + 基准时刻输入（lcchart 注入 #lcRefEpochSlot） + 复制按钮（最右） -->
                <div class="d-flex gap-2 align-items-center flex-wrap justify-content-end">
                  <select class="form-select form-select-sm" style="width:auto" id="gextMode" onchange="rebuildLCPlot()">
                    <option value="raw" selected>数据: 原始</option>
                    <option value="gext">数据: 银消改正后</option>
                  </select>
                  <select class="form-select form-select-sm" style="width:auto" id="yMode" onchange="rebuildLCPlot()"
                          ${canAbsMag ? '' : 'title="该源无红移，无法计算距离模数，绝对星等不可用"'}>
                    <option value="flux" selected>Y: 流量密度</option>
                    <option value="absmag" ${canAbsMag ? '' : 'disabled'}>Y: 绝对星等</option>
                  </select>
                  <select class="form-select form-select-sm" style="width:auto" id="xScale" onchange="rebuildLCPlot()">
                    <option value="logarithmic" selected>X: 对数</option>
                    <option value="linear">X: 线性</option>
                  </select>
                  <select class="form-select form-select-sm" style="width:auto" id="topAxis" onchange="rebuildLCPlot()"
                          ${canMJD ? '' : 'title="该源无 T0，MJD 轴不可用"'}>
                    <option value="day" selected>顶部轴: 天</option>
                    <option value="mjd" ${canMJD ? '' : 'disabled'}>顶部轴: MJD</option>
                    <option value="none">顶部轴: 无</option>
                  </select>
                  <span id="lcRefEpochSlot" class="d-inline-flex align-items-center"></span>
                  <button class="btn btn-sm btn-outline-secondary" onclick="copyLCChart()" title="复制当前光变图（含图例与标题）到剪贴板；不支持时改为下载 PNG"><i class="bi bi-clipboard"></i> 复制光变图</button>
                </div>
                <!-- 第二排：四个显示开关（显示光谱观测由 lcchart 注入 #lcSpecChkSlot） -->
                <div class="d-flex gap-3 align-items-center flex-wrap justify-content-end">
                  <div class="form-check form-check-inline mb-0" title="是否绘制数据点的星等/流量密度误差棒">
                    <input class="form-check-input" type="checkbox" id="lcShowErr" checked onchange="lcShowErrToggle(this.checked)">
                    <label class="form-check-label small" for="lcShowErr">误差棒</label>
                  </div>
                  <div class="form-check form-check-inline mb-0" ${canMJD ? 'title="在图上画一条当前时刻对应的红色竖虚线（位置 = T0 至今的时间差；越界时以红色三角指示方向）"' : 'title="该源无 T0，无法定位当前时刻"'}>
                    <input class="form-check-input" type="checkbox" id="lcShowNow" onchange="lcShowNowToggle(this.checked)" ${canMJD ? '' : 'disabled'}>
                    <label class="form-check-label small" for="lcShowNow">显示当前时刻</label>
                  </div>
                  <span id="lcSpecChkSlot" class="d-inline-flex align-items-center"></span>
                  <div class="form-check form-check-inline mb-0" ${canRestFrame ? 'title="时间轴除以 (1+z) 改正到静止系"' : 'title="该源无红移，静止系不可用"'}>
                    <input class="form-check-input" type="checkbox" id="lcRestFrame" onchange="rebuildLCPlot()" ${canRestFrame ? '' : 'disabled'}>
                    <label class="form-check-label small" for="lcRestFrame">静止系 t/(1+z)</label>
                  </div>
                </div>
              </div>
            </div>
            <div class="card-body">
              <!-- 坐标范围手动调节（留空 = 该端自动） -->
              <div class="d-flex flex-wrap gap-1 align-items-center mb-2 small">
                <span class="text-secondary me-1">坐标范围:</span>
                <input type="text" class="form-control form-control-sm" id="axXmin" placeholder="xmin" title="x 轴最小值（当前 x 轴单位，秒）" style="width:80px">
                <input type="text" class="form-control form-control-sm" id="axXmax" placeholder="xmax" title="x 轴最大值（当前 x 轴单位，秒）" style="width:80px">
                <input type="text" class="form-control form-control-sm" id="axYmin" placeholder="ymin" title="y 轴最小值 (mJy)" style="width:80px">
                <input type="text" class="form-control form-control-sm" id="axYmax" placeholder="ymax" title="y 轴最大值 (mJy)" style="width:80px">
                <button class="btn btn-sm btn-outline-primary" onclick="applyLCAxisRange()">应用</button>
                <button class="btn btn-sm btn-outline-secondary" onclick="resetLCAxisRange()">恢复默认</button>
              </div>
              <div class="chart-container" style="height:auto;aspect-ratio:3/2;width:80%;margin:0 auto;min-height:0;overflow:hidden"><canvas id="lcChart"></canvas></div>
              <!-- 波段显示勾选面板（替代内置图例的划线开关） -->
              <div id="lcBandPanel" class="d-flex flex-wrap gap-2 align-items-center mt-2 small"></div>
              ${lcData.total > 0 ? `
              <!-- 光变曲线拟合 -->
              <div class="mt-3 border-top pt-2" id="lcFitSection">
                <div class="d-flex flex-wrap gap-1 align-items-center small">
                  <span class="text-secondary me-1"><i class="bi bi-bezier2"></i> 添加拟合:</span>
                  <select class="form-select form-select-sm" id="fitModel" style="width:auto" onchange="fitModelChanged()">
                    <option value="pl">powerlaw</option>
                    <option value="bpl">broken-powerlaw</option>
                    <option value="sbpl">smoothly-broken-powerlaw</option>
                    <option value="fred">FRED (Norris+2005)</option>
                  </select>
                  <select class="form-select form-select-sm" id="fitBand" style="width:auto">
                    ${bandNames.map(b => `<option value="${escAttr(b)}">${esc(b)}</option>`).join('')}
                  </select>
                  <input type="text" class="form-control form-control-sm" id="fitTmin" placeholder="拟合 t_min (s)" title="拟合数据时间下限（留空=全范围）" style="width:100px;font-size:0.72rem">
                  <input type="text" class="form-control form-control-sm" id="fitTmax" placeholder="拟合 t_max (s)" title="拟合数据时间上限（留空=全范围）" style="width:100px;font-size:0.72rem">
                  <span id="fitTbRange" class="align-items-center gap-1" style="display:none">
                    <span class="text-secondary">tb∈[</span>
                    <input type="text" class="form-control form-control-sm" id="fitTbMin" placeholder="tb_min" title="拐点 tb 预设下限（秒，留空=数据范围）" style="width:70px;font-size:0.72rem">
                    <span class="text-secondary">,</span>
                    <input type="text" class="form-control form-control-sm" id="fitTbMax" placeholder="tb_max" title="拐点 tb 预设上限（秒，留空=数据范围）" style="width:70px;font-size:0.72rem">
                    <span class="text-secondary">]s</span>
                  </span>
                  <input type="text" class="form-control form-control-sm" id="fitPmin" placeholder="绘制 t_min (s)" title="拟合线绘制范围下限（外推用；留空=与拟合范围一致）" style="width:100px;font-size:0.72rem">
                  <input type="text" class="form-control form-control-sm" id="fitPmax" placeholder="绘制 t_max (s)" title="拟合线绘制范围上限（外推用；留空=与拟合范围一致）" style="width:100px;font-size:0.72rem">
                  <button class="btn btn-sm btn-outline-primary" onclick="addLCFit()"><i class="bi bi-plus-lg"></i> 添加</button>
                </div>
                <div id="lcFitList" class="mt-2 small"></div>
              </div>` : ''}
            </div>
          </div>
        </div>

        <!-- ─── 数据表 ─── -->
        <div id="tab-data" class="tab-pane" style="display:none">
          <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
              <span><i class="bi bi-table"></i> 光变数据表</span>
              <div class="d-flex gap-1">
                <button class="btn btn-sm btn-outline-secondary" onclick="lcColPanelToggle()" title="勾选要显示的列"><i class="bi bi-layout-three-columns"></i> 列显示</button>
                <button class="btn btn-sm btn-outline-primary" id="lcUploadBtn" style="display:none" onclick="lcUploadShow()"><i class="bi bi-upload"></i> 上传数据表</button>
                <button class="btn btn-sm btn-outline-primary" id="lcAddBtn" style="display:none" onclick="lcAddNewRow()"><i class="bi bi-plus-circle"></i> 添加记录</button>
                <button class="btn btn-sm btn-outline-danger" id="lcDelBtn" style="display:none" onclick="lcDeleteSelected()"><i class="bi bi-trash"></i> 删除勾选</button>
              </div>
            </div>
            <div class="card-body p-0">
              <!-- 列显示勾选面板（「列显示」按钮展开） -->
              <div id="lcColPanel" class="px-2 py-2 border-bottom" style="display:none">
                <div class="d-flex flex-wrap gap-1 align-items-center small" id="lcColChecks"></div>
              </div>
              <div class="table-scroll" style="max-height:500px">
                <table class="table table-sm table-hover mb-0" style="font-size:0.8rem">
                  <thead>
                    <tr>
                      <th title="标记/取消标记全部行（整行高亮；管理员可批量删除勾选项）"><input type="checkbox" id="lcMarkAll" onclick="lcMarkToggleAll(this.checked)"></th>
                      ${LC_COLS.map(([k, label]) => `
                      <th class="lc-sortable" data-col="${k}" data-sort="${k}" onclick="lcSort('${k}')" title="点击排序">${label}<span class="lc-sort-ind" data-ind="${k}">${k === 'time' ? ' ▲' : ''}</span></th>`).join('')}
                      <th id="lcEditHeader" style="display:none">编辑</th>
                    </tr>
                  </thead>
                  <tbody id="lcTableBody">
                    ${lcData.items.map(lcRowHTML).join('')}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
        <!-- ─── 余辉拟合（由 fitting_tab.js 填充） ─── -->
        <div id="tab-fitting" class="tab-pane" style="display:none"></div>
        <!-- ─── 宿主星系（由 hostfit_tab.js 填充） ─── -->
        <div id="tab-host" class="tab-pane" style="display:none"></div>
        <!-- ─── 光谱数据（单条查看 / 多条对比） ─── -->
        <div id="tab-spectra" class="tab-pane" style="display:none">
          <div class="row g-3">
            <div class="col-md-5">
              <div class="card h-100">
                <div class="card-header d-flex justify-content-between align-items-center">
                  <span><i class="bi bi-list-ul"></i> 光谱列表</span>
                  <button class="btn btn-sm btn-outline-primary py-0" id="specUploadBtn" style="display:none" onclick="showSpecUpload()" title="上传光谱"><i class="bi bi-upload"></i> 上传</button>
                </div>
                <div class="card-body p-0" style="max-height:560px;overflow-y:auto">
                  <div class="small text-secondary px-2 pt-1">点击行切换选择，可多选对比；留空的光谱按空气波长处理（不自动回填），所有处理默认先转真空波长</div>
                  <table class="table table-sm table-hover mb-0" style="font-size:0.85rem">
                    <thead><tr><th>观测时间 (MJD)</th><th>仪器</th><th>类型</th><th>波长类型</th><th>观测者</th><th>纵向偏移量</th></tr></thead>
                    <tbody id="spectraListBody">
                      <tr><td colspan="6" class="text-center text-secondary py-3">加载中...</td></tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
            <div class="col-md-7">
              <div class="card h-100">
                <div class="card-header d-flex justify-content-between align-items-center">
                  <span><i class="bi bi-activity"></i> <span id="specTitle">光谱</span></span>
                  <div class="d-flex align-items-center gap-2">
                    <small class="text-secondary" id="specMeta"></small>
                    <div class="form-check form-check-inline mb-0" id="specErrChkWrap" title="是否绘制流量误差条">
                      <input class="form-check-input" type="checkbox" id="specErrChk" checked onchange="toggleSpecErr(this.checked)">
                      <label class="form-check-label small text-secondary" for="specErrChk">误差</label>
                    </div>
                    <select class="form-select form-select-sm" style="width:auto" id="specModeSel" onchange="setSpecMode(this.value)">
                      <option value="absolute" selected>绝对流量</option>
                      <option value="relative">相对流量</option>
                    </select>
                  </div>
                </div>
                <div class="card-body">
                  <div class="d-flex flex-wrap gap-1 align-items-center mb-2 small">
                    <span class="text-secondary me-1">坐标范围:</span>
                    <input type="text" class="form-control form-control-sm" id="specAxXmin" placeholder="xmin" title="x 轴最小值（Å，观测者系）" style="width:80px">
                    <input type="text" class="form-control form-control-sm" id="specAxXmax" placeholder="xmax" title="x 轴最大值（Å，观测者系）" style="width:80px">
                    <input type="text" class="form-control form-control-sm" id="specAxYmin" placeholder="ymin" title="y 轴最小值（流量）" style="width:80px">
                    <input type="text" class="form-control form-control-sm" id="specAxYmax" placeholder="ymax" title="y 轴最大值（流量）" style="width:80px">
                    <button class="btn btn-sm btn-outline-primary" onclick="applySpecAxisRange()">应用</button>
                    <button class="btn btn-sm btn-outline-secondary" onclick="resetSpecAxisRange()">恢复默认</button>
                    <select class="form-select form-select-sm" style="width:auto" id="specXTypeSel" onchange="setSpecAxisType()">
                      <option value="linear" ${specAxisType.x === 'linear' ? 'selected' : ''}>X: 线性</option>
                      <option value="logarithmic" ${specAxisType.x === 'logarithmic' ? 'selected' : ''}>X: 对数</option>
                    </select>
                    <select class="form-select form-select-sm" style="width:auto" id="specYTypeSel" onchange="setSpecAxisType()">
                      <option value="linear" ${specAxisType.y === 'linear' ? 'selected' : ''}>Y: 线性</option>
                      <option value="logarithmic" ${specAxisType.y === 'logarithmic' ? 'selected' : ''}>Y: 对数</option>
                    </select>
                    <span class="text-secondary ms-1">（也可在图上拖拽框选缩放）</span>
                  </div>
                  <details class="mb-2">
                    <summary class="small text-secondary" style="cursor:pointer">谱线标记（TNS 风格：H/He/C/N/O… 常见线、自定义波长、Tellurics、星系线、WR 线，z 与 v<sub>exp</sub> 可调）</summary>
                    <div id="specMarkingsBody" class="border rounded p-2 mt-1" style="max-height:260px;overflow-y:auto"></div>
                  </details>
                  <div class="chart-container" style="height:500px"><canvas id="specChart"></canvas></div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <!-- ─── SED 分析（由 sed_tab.js 惰性初始化填充） ─── -->
        <div id="tab-sed" class="tab-pane" style="display:none"></div>
      </div>

      <!-- 导出光变数据弹窗（选基准时刻：源 T0 / MJD / UTC） -->
      <div class="modal fade" id="lcExportModal" tabindex="-1">
        <div class="modal-dialog">
          <div class="modal-content">
            <div class="modal-header"><h6 class="mb-0"><i class="bi bi-download"></i> 导出光变数据 — ${esc(tid)}</h6>
              <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
            <div class="modal-body">
              <label class="form-label small">基准时刻（导出时间列以此为零点）</label>
              <select class="form-select form-select-sm mb-2" id="lcExportMode" onchange="lcExportModeChanged()">
                <option value="t0" selected>源 T0（默认）</option>
                <option value="mjd">自定义 MJD</option>
                <option value="utc">自定义 UTC 时间 (ISO 8601)</option>
              </select>
              <input type="text" class="form-control form-control-sm" id="lcExportCustom"
                     placeholder="如 60600.5 或 2025-12-02T01:48:23Z" style="display:none">
              <div class="small text-secondary mt-2" id="lcExportHint"></div>
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
              <button class="btn btn-sm btn-primary" onclick="doLCExport()"><i class="bi bi-download"></i> 导出 CSV</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 上传光谱弹窗 -->
      <div class="modal fade" id="specUploadModal" tabindex="-1">
        <div class="modal-dialog">
          <div class="modal-content">
            <div class="modal-header"><h6 class="mb-0"><i class="bi bi-upload"></i> 上传光谱到 ${esc(tid)}</h6>
              <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
            <div class="modal-body">
              <div class="mb-2">
                <label class="form-label small">光谱文件（两列文本 或 OpenSNSpectra 风格 JSON）</label>
                <input type="file" class="form-control form-control-sm" id="specFile" accept=".txt,.dat,.csv,.json">
              </div>
              <div class="row g-2">
                <div class="col-6"><label class="form-label small">仪器</label><input class="form-control form-control-sm" id="specUpInstrument" placeholder="如 VLT-FORS2"></div>
                <div class="col-6"><label class="form-label small">MJD（观测日）</label><input class="form-control form-control-sm" id="specUpMjd" placeholder="如 53786.0"></div>
                <div class="col-6"><label class="form-label small">观测者</label><input class="form-control form-control-sm" id="specUpObserver"></div>
                <div class="col-6"><label class="form-label small">归算者(可选)</label><input class="form-control form-control-sm" id="specUpReducer"></div>
                <div class="col-6"><label class="form-label small">流量类型</label>
                  <select class="form-select form-select-sm" id="specUpFluxType">
                    <option value="absolute" selected>绝对流量 (erg/s/cm²/Å)</option>
                    <option value="normalized">归一化流量（谱形保留，无量纲）</option>
                  </select>
                </div>
                <div class="col-6"><label class="form-label small">类型</label>
                  <select class="form-select form-select-sm" id="specUpSpecType">
                    <option value="transient" selected>Transient（暂现源）</option>
                    <option value="host">Host（宿主星系）</option>
                    <option value="mix">Mix（混合）</option>
                  </select>
                </div>
                <div class="col-6"><label class="form-label small">波长类型</label>
                  <select class="form-select form-select-sm" id="specUploadWaveType">
                    <option value="" selected>留空（默认按空气处理）</option>
                    <option value="vacuum">真空波长</option>
                    <option value="air">空气波长</option>
                  </select>
                </div>
              </div>
              <div class="small text-secondary mt-2">留空的光谱按空气波长处理（不自动回填）；所有处理默认先转真空波长。</div>
              <div class="alert alert-dark small mt-3 mb-0" role="note">
                <strong>格式要求：</strong>波长一律为<strong>观测者系</strong>（单位 Å）。
                ① 两列或三列文本：<code>波长 流量 [流量误差]</code>，空格/逗号分隔，<code>#</code> 开头为注释，可用 <code># instrument: xx</code>、<code># mjd: xx</code> 声明元数据；② JSON：<code>{"名称": {"spectra": {"time": MJD, "instrument": "...", "data": [[波长, 流量, (误差)], ...]}}}</code>。
                至少 10 个数据点，波长范围 100–10⁷ Å。流量为绝对流量（erg/s/cm²/Å）或归一化流量（在上方"流量类型"选择）。
              </div>
              <div class="small text-danger mt-2" id="specUpError" style="display:none"></div>
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
              <button class="btn btn-sm btn-primary" id="specUpSubmit" onclick="doSpecUpload()">上传</button>
            </div>
          </div>
        </div>
      </div>
    `;

    // ─── Tab 切换 ───
    document.querySelectorAll('#detailTabs .nav-link').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        if (link.classList.contains('disabled')) return;
        document.querySelectorAll('#detailTabs .nav-link').forEach(l => l.classList.remove('active'));
        link.classList.add('active');
        activeTab = link.dataset.tab;
        syncUrlTab(tid, activeTab);
        document.querySelectorAll('#tabContent .tab-pane').forEach(t => t.style.display = 'none');
        const pane = document.getElementById(`tab-${link.dataset.tab}`);
        if (pane) pane.style.display = 'block';
        // Aladin：概览页首次显示时初始化（无坐标源指向银河系中心占位）
        if (link.dataset.tab === 'overview') {
          setTimeout(() => {
            if (currentTid === tid && document.getElementById('aladinContainer')) {
              initAladin(aladinRa, aladinDec, hasCoords ? transient.id : null);
            }
          }, 200);
        }
        // LC chart
        if (link.dataset.tab === 'lc') {
          setTimeout(() => initLCPlot(bands, bandNames, spectralColors), 100);
        }
        // 余辉拟合：进入时初始化，切走时停止轮询
        if (link.dataset.tab === 'fitting') {
          initFittingTab(pane, tid);
        } else {
          destroyFittingTab();
        }
        // 宿主星系：进入时初始化，切走时停止轮询
        if (link.dataset.tab === 'host') {
          initHostfitTab(pane, tid);
        } else {
          destroyHostfitTab();
        }
        // SED 分析：进入时初始化，切走时停止轮询
        if (link.dataset.tab === 'sed') {
          initSedTab(pane, tid);
        } else {
          destroySedTab();
        }
        // 光谱数据：进入时加载光谱
        if (link.dataset.tab === 'spectra') {
          setTimeout(() => initSpectraTab(tid, transient.redshift), 100);
        }
      });
    });

    // render 重建后恢复此前激活的 tab（click 复用切换逻辑，含各 tab 惰性初始化）
    if (activeTab !== 'overview') {
      const link = document.querySelector(`#detailTabs .nav-link[data-tab="${activeTab}"]`);
      if (link) link.click();
    }

    // 默认初始化 Aladin（概览页默认显示；无坐标源指向银河系中心占位）
    setTimeout(() => {
      if (currentTid === tid && document.getElementById('aladinContainer')) {
        initAladin(aladinRa, aladinDec, hasCoords ? transient.id : null);
      }
    }, 500);

    // ─── derived 卡片初始化 ───
    initDerived(transient.extra_data && transient.extra_data.derived);

    // 概览「宿主星系」摘要行（独立请求，不阻塞主渲染）
    fillHostSummary(tid);

    // 全局函数（APIImport 由 app.js 启动时统一设置；其余 window.* 由 detail_*.js 子模块注册）
    window.render = render;  // 供内联 onclick（文章编辑取消按钮）调用
    wireLCChartGlobals(bands, bandNames, spectralColors);

    // 数据表编辑列（含扣点按钮）登录后显示；删除/编辑本源等仅管理员
    if (isAuthed()) {
      document.getElementById('lcEditHeader').style.display = '';
      document.querySelectorAll('.lc-edit-cell').forEach(el => el.style.display = '');
      const addBtn = document.getElementById('lcAddBtn');
      if (addBtn) addBtn.style.display = 'inline-block';
      const uploadBtn = document.getElementById('lcUploadBtn');
      if (uploadBtn) uploadBtn.style.display = 'inline-block';
      const specUpBtn = document.getElementById('specUploadBtn');
      if (specUpBtn) specUpBtn.style.display = 'inline-block';
    }
    if (isAdmin()) {
      const delBtn = document.getElementById('lcDelBtn');
      if (delBtn) delBtn.style.display = 'inline-block';
    }
    // 列显示面板：生成勾选框并应用当前列可见性
    buildLcColPanel();
    applyLcColVis();

    // 坐标输入即时解析提示（度 ⇄ 时分秒）
    attachEditCoordHints();
    // 主/副标签输入 datalist 自动补全
    attachEditTagInputs();

  } catch (err) {
    if (navStale(seq)) return;  // 已离开本页，错误提示不覆盖新页面
    showError(`加载事件详情失败: ${err.message}`);
  }
}
