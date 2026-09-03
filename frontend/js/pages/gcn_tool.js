// === GCN 阅读工具（工具箱子页面） ===
// 左栏：GCN circular 浏览器（跳转/翻页/JSON 数字高亮/在线更新/时间计算器）
// 右上：源信息卡（读写 transients 表）
// 右下：测光录入卡（写 lightcurves 表，字段映射见 saveObs）
import { app, showLoading, showError } from './layout.js';
import {
  getGcnIds, getGcnCircular, getGcnRelated,
  getTransients, getTransient, createTransient, updateTransient,
  getLightcurves, createLightcurves, updateLightcurve, isAuthed, showToast,
} from '../api.js';

// ─── 模块状态 ───
let _ids = [];            // 当前窗口期号（一页最多 100，降序：index 0 = 最新一期；第 1 页 = 最新）
let _total = 0;           // GCN 总期数（来自列表 API 的 totalItems）
let _windowPage = 1;      // _ids 对应的页码
let _idx = -1;            // 当前在 _ids 中的位置（-1 = 列表外）
let _cid = null;          // 当前 circular id
let _tid = null;          // 当前源 id（源信息卡 ↔ 测光录入卡联动）
let _editLcId = null;     // 测光录入卡正在编辑的已有记录 id（null = 新增模式）
let _editTid = null;      // 被编辑记录所属的源 id
let _relatedResp = null;  // 当前 circular 的关联记录原始响应
let _relSort = { key: null, dir: 1 };          // 关联表排序状态
let _relFilter = { tid: '', band: '', ref: '', comment: '' };  // 关联表筛选状态

const TIME_FACTOR = { s: 1, m: 60, h: 3600, d: 86400 };

// ─── 坐标解析：十进制 = 度；sexagesimal（hh:mm:ss / 12h34m56s / 空格分隔）RA 按小时角 ×15、Dec 按度 ───
// 算法与 astropy.coordinates.Angle 一致：val = sign * (d + m/60 + s/3600)（RA 再 ×15）
// 返回 { deg } 或 { err }；空串返回 { deg: null }（不填）
function parseCoord(str, isRA) {
  const s0 = String(str).trim();
  if (!s0) return { deg: null };
  // 纯十进制 → 度
  if (/^[+-]?\d+(?:\.\d+)?$/.test(s0)) {
    return checkCoordRange(parseFloat(s0), s0, isRA);
  }
  // sexagesimal：h/d/°/m/′ → ':'；s/″ 去掉；空白 → ':'
  const sign = s0.startsWith('-') ? -1 : 1;
  const body = s0.replace(/^[+-]/, '');
  const norm = body
    .replace(/[hd°m′']/gi, ':')
    .replace(/[s″]/gi, '')
    .replace(/\s+/g, ':')
    .replace(/:+/g, ':')
    .replace(/^:|:$/g, '');
  const parts = norm.split(':');
  if (parts.length < 2 || parts.length > 3 || parts.some(p => !/^\d+(?:\.\d+)?$/.test(p))) {
    return { err: `无法解析坐标 '${s0}'（支持十进制度或 hh:mm:ss / dd:mm:ss）` };
  }
  const [a, b, c = 0] = parts.map(parseFloat);
  if (b >= 60 || c >= 60) return { err: `坐标分/秒应小于 60: '${s0}'` };
  let deg = sign * (a + b / 60 + c / 3600);
  if (isRA) deg *= 15;  // 小时角 → 度
  return checkCoordRange(deg, s0, isRA);
}

function checkCoordRange(deg, raw, isRA) {
  if (!isFinite(deg)) return { err: `无法解析坐标 '${raw}'` };
  if (isRA && (deg < 0 || deg >= 360)) return { err: `RA 超出 [0, 360) 度范围: '${raw}'` };
  if (!isRA && (deg < -90 || deg > 90)) return { err: `Dec 超出 [-90, 90] 度范围: '${raw}'` };
  return { deg };
}

// 输入框下方实时显示换算结果（或错误）
function coordFeedback(inputId, isRA) {
  const inp = document.getElementById(inputId);
  const fb = document.getElementById(inputId + 'Fb');
  if (!inp || !fb) return;
  const r = parseCoord(inp.value, isRA);
  if (r.err) {
    fb.innerHTML = `<span class="text-danger">${esc(r.err)}</span>`;
  } else if (r.deg != null && !/^[+-]?\d+(?:\.\d+)?$/.test(inp.value.trim())) {
    fb.textContent = `= ${r.deg.toFixed(6)}°`;
  } else {
    fb.textContent = '';
  }
}

// ─── HTML 转义 ───
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ─── JSON 展示：\n 展开 + 数字/key/bool 高亮（算法复刻自 tkinter 版工具） ───
function expandEscapedNewlines(text) {
  const out = [];
  for (const line of text.split('\n')) {
    const stripped = line.replace(/^ +/, '');
    const leading = line.slice(0, line.length - stripped.length);
    if (stripped.includes('\\n')) {
      const indent = leading + '    ';
      const parts = stripped.split('\\n');
      out.push(parts[0] + parts.slice(1).map(p => '\n' + indent + p).join(''));
    } else {
      out.push(line);
    }
  }
  return out.join('\n');
}

function formatGcnHtml(data) {
  let text = expandEscapedNewlines(JSON.stringify(data, null, 2));
  text = esc(text);
  // key 高亮（行首缩进后的 "key":）
  text = text.replace(/^(\s*)"([^"\n]+)":/gm, '$1<span class="gcn-key">"$2"</span>:');
  // 数字高亮（整数/小数/科学计数法；span 标签与 class 名中不含数字，不会误伤）
  text = text.replace(/-?\d+\.?\d*(?:[eE][+-]?\d+)?/g, '<span class="gcn-num">$&</span>');
  // true/false/null 高亮
  text = text.replace(/\b(true|false|null)\b/g, '<span class="gcn-bool">$1</span>');
  return text;
}

// ─── 从 circular 提取暴名 token（正则与 backend/tools/gcn_index.py 一致） ───
function extractNameTokens(data) {
  const text = (data.subject || '') + '\n' + (data.body || '');
  const tokens = new Set();
  for (const m of text.matchAll(/\bGRB\s?(\d{6})([A-Z])?\b/gi)) {
    tokens.add(('GRB' + m[1] + (m[2] || '')).toUpperCase());
  }
  for (const m of text.matchAll(/\bEP\s?(2\d{5}[a-z])\b/gi)) {
    tokens.add('EP' + m[1].toLowerCase());
  }
  return [...tokens];
}

// ═══ 页面渲染 ═══
export async function render() {
  showLoading();
  _idx = -1; _cid = null; _tid = null; _editLcId = null; _editTid = null;

  app.innerHTML = `
    <div class="page-header"><h4><i class="bi bi-journal-text"></i> GCN 阅读工具</h4></div>
    <div class="row g-3">
      <!-- ─── 左栏：GCN 浏览器 + 时间计算器 ─── -->
      <div class="col-lg-5">
        <div class="card mb-3">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="bi bi-journal-text"></i> GCN Circular 浏览器</span>
            <span class="small text-secondary" id="gcnCurLabel">未选择</span>
          </div>
          <div class="card-body">
            <div class="d-flex gap-1 mb-2 flex-wrap align-items-center">
              <input type="text" class="form-control form-control-sm" id="gcnJump" placeholder="期号 如 40689" style="width:130px">
              <button class="btn btn-sm btn-outline-primary" id="gcnJumpBtn">Go</button>
              <button class="btn btn-sm btn-outline-secondary" id="gcnFirst" title="跳到最早一期">最早</button>
              <button class="btn btn-sm btn-outline-secondary" id="gcnPrev">&lt;&lt; Prev</button>
              <button class="btn btn-sm btn-outline-secondary" id="gcnNext">Next &gt;&gt;</button>
              <button class="btn btn-sm btn-outline-secondary" id="gcnLast" title="跳到最新一期">最新</button>
            </div>
            <div class="small text-secondary mb-2" id="gcnStatusLine">加载中...</div>
            <div id="gcnChips" class="mb-2 d-flex flex-wrap gap-1 align-items-center"></div>
            <div class="gcn-view border rounded p-2" id="gcnViewer" style="max-height:460px;overflow:auto">
              <span class="text-secondary small">输入期号跳转，或用 Prev/Next 翻页浏览</span>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-header py-1"><span class="small fw-bold"><i class="bi bi-calculator"></i> 时间计算器</span></div>
          <div class="card-body py-2">
            <div class="d-flex gap-1 mb-1 align-items-center">
              <span class="small text-secondary" style="width:48px">time 1:</span>
              <input type="text" class="form-control form-control-sm" id="gcnCalcT1" placeholder="2025-06-10T12:34:56 或 MJD">
              <select class="form-select form-select-sm" id="gcnCalcF1" style="width:80px"><option value="UTC" selected>UTC</option><option value="MJD">MJD</option></select>
            </div>
            <div class="d-flex gap-1 mb-1 align-items-center">
              <span class="small text-secondary" style="width:48px">time 2:</span>
              <input type="text" class="form-control form-control-sm" id="gcnCalcT2" placeholder="2025-06-10T12:34:56 或 MJD">
              <select class="form-select form-select-sm" id="gcnCalcF2" style="width:80px"><option value="UTC" selected>UTC</option><option value="MJD">MJD</option></select>
            </div>
            <div class="d-flex gap-1 align-items-center">
              <button class="btn btn-sm btn-outline-primary" id="gcnCalcBtn">Calculate Δt</button>
              <input type="text" class="form-control form-control-sm" id="gcnCalcResult" readonly value="Δt =">
            </div>
          </div>
        </div>
      </div>

      <!-- ─── 右栏：源信息 + 测光录入 ─── -->
      <div class="col-lg-7">
        <div class="card mb-3">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="bi bi-star"></i> 源信息（AJST 数据库）</span>
            <a href="#" class="small" id="gcnDetailLink" style="display:none">打开详情页 →</a>
          </div>
          <div class="card-body">
            <div class="row g-2" style="font-size:0.9rem">
              <div class="col-md-4">
                <label class="form-label small mb-0">源 ID <span class="text-danger">*</span></label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcId" placeholder="GRB250610B / EP250108a">
              </div>
              <div class="col-md-8">
                <label class="form-label small mb-0">别名（逗号分隔）</label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcAliases">
              </div>
              <div class="col-md-3">
                <label class="form-label small mb-0">RA（度 或 hh:mm:ss）</label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcRa" placeholder="187.25 或 12:29:00">
                <div class="small text-secondary" id="gcnSrcRaFb"></div>
              </div>
              <div class="col-md-3">
                <label class="form-label small mb-0">Dec（度 或 dd:mm:ss）</label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcDec" placeholder="-11.5 或 -11:30:00">
                <div class="small text-secondary" id="gcnSrcDecFb"></div>
              </div>
              <div class="col-md-6">
                <label class="form-label small mb-0">T0 (UTC)</label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcT0" placeholder="2025-06-10T12:34:56">
              </div>
              <div class="col-md-4">
                <label class="form-label small mb-0">触发仪器</label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcInstr">
              </div>
              <div class="col-md-2">
                <label class="form-label small mb-0">红移</label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcZ">
              </div>
              <div class="col-md-6">
                <label class="form-label small mb-0">标签（逗号分隔）</label>
                <input type="text" class="form-control form-control-sm" id="gcnSrcTags" placeholder="grb, lgrb ...">
              </div>
            </div>
            <div class="d-flex gap-1 mt-3 flex-wrap">
              <button class="btn btn-sm btn-outline-primary" id="gcnSrcLoad"><i class="bi bi-search"></i> 加载所选</button>
              <button class="btn btn-sm btn-primary" id="gcnSrcCreate"><i class="bi bi-plus-circle"></i> 新建源</button>
              <button class="btn btn-sm btn-success" id="gcnSrcSave"><i class="bi bi-check-lg"></i> 保存修改</button>
              <button class="btn btn-sm btn-outline-secondary" id="gcnSrcClear">清空表单</button>
            </div>
            <div class="small text-secondary mt-2" id="gcnSrcStatus"></div>
          </div>
        </div>

        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="bi bi-pencil-square"></i> 测光录入（写入光变数据表）</span>
            <span class="small" id="gcnObsTarget" style="color:var(--accent-green)">未选择源</span>
          </div>
          <div class="card-body" style="font-size:0.9rem">
            <!-- ── 来源 ── -->
            <div class="gcn-form-sec">来源</div>
            <div class="row g-2 mb-2">
              <div class="col-md-2">
                <label class="form-label small mb-0">GCN 期号</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsGcnId">
              </div>
              <div class="col-md-4">
                <label class="form-label small mb-0">引用 reference</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsRef">
              </div>
              <div class="col-md-3">
                <label class="form-label small mb-0">望远镜 telescope</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsTel" placeholder="如 LDT / NOT">
              </div>
              <div class="col-md-3">
                <label class="form-label small mb-0">仪器 instrument</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsInstr">
              </div>
            </div>
            <!-- ── 时间 ── -->
            <div class="gcn-form-sec">时间（相对 T0）</div>
            <div class="row g-2 mb-1">
              ${[['start', '开始 start'], ['mid', '中间 mid'], ['end', '结束 end']].map(([k, label]) => `
              <div class="col-md-3">
                <label class="form-label small mb-0">${label}</label>
                <div class="input-group input-group-sm">
                  <input type="text" class="form-control" id="gcnObsT_${k}">
                  <select class="form-select" id="gcnObsU_${k}" style="max-width:64px">
                    <option value="s" selected>s</option><option value="m">min</option><option value="h">h</option><option value="d">d</option>
                  </select>
                </div>
              </div>`).join('')}
              <div class="col-md-3">
                <label class="form-label small mb-0">曝光 exposure</label>
                <div class="input-group input-group-sm">
                  <input type="text" class="form-control" id="gcnObsExpo">
                  <select class="form-select" id="gcnObsExpoU" style="max-width:64px">
                    <option value="s" selected>s</option><option value="m">min</option><option value="h">h</option><option value="d">d</option>
                  </select>
                </div>
              </div>
            </div>
            <div class="small text-secondary mb-2">time 取数优先级：mid → (start+end)/2 → start+曝光/2（三种组合至少填一种）</div>
            <!-- ── 波段与流量 ── -->
            <div class="gcn-form-sec">波段与流量</div>
            <div class="row g-2 mb-1">
              <div class="col-md-3">
                <label class="form-label small mb-0">波段 band <span class="text-danger">*</span></label>
                <input type="text" class="form-control form-control-sm" id="gcnObsBand" placeholder="r / 1keV / 9GHz ...">
              </div>
              <div class="col-md-3">
                <label class="form-label small mb-0">上限/探测</label>
                <select class="form-select form-select-sm" id="gcnObsUl">
                  <option value="detection" selected>detection</option>
                  <option value="upperlimit">upperlimit</option>
                </select>
              </div>
              <div class="col-md-2">
                <label class="form-label small mb-0">星等 mag</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsMag">
              </div>
              <div class="col-md-2">
                <label class="form-label small mb-0">星等误差</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsMagErr">
              </div>
              <div class="col-md-2">
                <label class="form-label small mb-0">星等系统</label>
                <select class="form-select form-select-sm" id="gcnObsMagSys">
                  <option value="AB" selected>AB</option><option value="Vega">Vega</option>
                </select>
              </div>
            </div>
            <div class="row g-2 mb-1">
              <div class="col-md-3">
                <label class="form-label small mb-0">流量密度 flux_density</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsFlux">
              </div>
              <div class="col-md-3">
                <label class="form-label small mb-0">流量密度误差</label>
                <input type="text" class="form-control form-control-sm" id="gcnObsFluxErr">
              </div>
              <div class="col-md-3">
                <label class="form-label small mb-0">流量单位</label>
                <select class="form-select form-select-sm" id="gcnObsFluxUnit">
                  <option value="mJy" selected>mJy</option><option value="uJy">uJy</option>
                  <option value="Jy">Jy</option><option value="cgs(erg/cm2/s/Hz)">cgs(erg/cm2/s/Hz)</option>
                </select>
              </div>
              <div class="col-md-3 d-flex align-items-end">
                <span class="small text-secondary">星等与流量密度二选一（都填时取星等）</span>
              </div>
            </div>
            <!-- ── 备注 ── -->
            <div class="gcn-form-sec">备注</div>
            <div class="row g-2">
              <div class="col-12">
                <input type="text" class="form-control form-control-sm" id="gcnObsComment" placeholder="comment">
              </div>
            </div>
            <div class="d-flex gap-1 mt-3 flex-wrap align-items-center">
              <button class="btn btn-sm btn-primary" id="gcnObsSave"><i class="bi bi-check-lg"></i> 保存测光记录</button>
              <button class="btn btn-sm btn-outline-primary" id="gcnObsSaveNew" style="display:none"><i class="bi bi-plus-lg"></i> 作为新记录保存</button>
              <button class="btn btn-sm btn-outline-secondary" id="gcnObsCancelEdit" style="display:none">取消编辑</button>
              <button class="btn btn-sm btn-outline-secondary" id="gcnObsClear">清空字段</button>
              <span class="small ms-1" id="gcnObsEditBadge" style="color:var(--accent-orange);display:none"></span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ─── 关联光变记录（与当前浏览的 GCN 对照核对） ─── -->
    <div class="row g-3" id="gcnRelatedRow" style="display:none">
      <div class="col-12">
        <div class="card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="bi bi-link-45deg"></i> 库中关联光变记录</span>
            <span class="small text-secondary" id="gcnRelatedSummary">加载中...</span>
          </div>
          <div class="card-body py-2">
            <div class="d-flex gap-1 mb-2 flex-wrap align-items-center">
              <select class="form-select form-select-sm" id="gcnRelFTid" style="width:auto"></select>
              <select class="form-select form-select-sm" id="gcnRelFBand" style="width:auto"></select>
              <input type="text" class="form-control form-control-sm" id="gcnRelFRef" placeholder="reference 包含…" style="width:150px">
              <input type="text" class="form-control form-control-sm" id="gcnRelFCmt" placeholder="comment 包含…" style="width:150px">
              <button class="btn btn-sm btn-outline-secondary" id="gcnRelFReset">重置筛选</button>
            </div>
            <div class="table-scroll" style="max-height:320px">
              <table class="table table-sm table-hover mb-0" style="font-size:0.85rem">
                <thead><tr>
                  <th class="lc-sortable" data-key="_kind">匹配</th>
                  <th class="lc-sortable" data-key="transient_id">源</th>
                  <th class="lc-sortable" data-key="band">band</th>
                  <th class="lc-sortable" data-key="time">time (s)</th>
                  <th class="lc-sortable" data-key="flux_density">流量/星等</th>
                  <th class="lc-sortable" data-key="upperlimit">上限</th>
                  <th class="lc-sortable" data-key="telescope">telescope</th>
                  <th class="lc-sortable" data-key="reference">reference</th>
                  <th class="lc-sortable" data-key="comment">comment</th>
                  <th></th>
                </tr></thead>
                <tbody id="gcnRelatedBody"></tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;

  // ─── 事件绑定 ───
  const $ = (id) => document.getElementById(id);
  $('gcnJumpBtn').addEventListener('click', onJump);
  $('gcnJump').addEventListener('keydown', (e) => { if (e.key === 'Enter') onJump(); });
  $('gcnFirst').addEventListener('click', gotoFirst);
  $('gcnPrev').addEventListener('click', () => step(-1));
  $('gcnNext').addEventListener('click', () => step(1));
  $('gcnLast').addEventListener('click', gotoLast);
  $('gcnCalcBtn').addEventListener('click', calcDelta);
  $('gcnCalcT1').addEventListener('keydown', (e) => { if (e.key === 'Enter') calcDelta(); });
  $('gcnCalcT2').addEventListener('keydown', (e) => { if (e.key === 'Enter') calcDelta(); });
  $('gcnSrcLoad').addEventListener('click', onSrcLoad);
  $('gcnSrcCreate').addEventListener('click', () => saveSource(true));
  $('gcnSrcSave').addEventListener('click', () => saveSource(false));
  $('gcnSrcClear').addEventListener('click', clearSrcForm);
  $('gcnSrcId').addEventListener('keydown', (e) => { if (e.key === 'Enter') onSrcLoad(); });
  $('gcnObsSave').addEventListener('click', () => saveObs());
  $('gcnObsSaveNew').addEventListener('click', () => saveObs('new'));
  $('gcnObsCancelEdit').addEventListener('click', () => { exitEditMode(); clearObsForm(true); });
  $('gcnObsClear').addEventListener('click', () => { exitEditMode(); clearObsForm(true); });
  $('gcnSrcRa').addEventListener('input', () => coordFeedback('gcnSrcRa', true));
  $('gcnSrcDec').addEventListener('input', () => coordFeedback('gcnSrcDec', false));
  // 关联记录面板：筛选与排序
  $('gcnRelFTid').addEventListener('change', (e) => { _relFilter.tid = e.target.value; applyRelatedView(); });
  $('gcnRelFBand').addEventListener('change', (e) => { _relFilter.band = e.target.value; applyRelatedView(); });
  $('gcnRelFRef').addEventListener('input', (e) => { _relFilter.ref = e.target.value.trim().toLowerCase(); applyRelatedView(); });
  $('gcnRelFCmt').addEventListener('input', (e) => { _relFilter.comment = e.target.value.trim().toLowerCase(); applyRelatedView(); });
  $('gcnRelFReset').addEventListener('click', () => {
    _relFilter = { tid: '', band: '', ref: '', comment: '' };
    $('gcnRelFTid').value = ''; $('gcnRelFBand').value = '';
    $('gcnRelFRef').value = ''; $('gcnRelFCmt').value = '';
    applyRelatedView();
  });
  document.querySelectorAll('#gcnRelatedRow th.lc-sortable').forEach(th => {
    th.dataset.label = th.textContent;
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (_relSort.key === key) {
        if (_relSort.dir === 1) _relSort.dir = -1;
        else _relSort = { key: null, dir: 1 };   // 第三次点击取消排序
      } else {
        _relSort = { key, dir: 1 };
      }
      applyRelatedView();
    });
  });

  await loadIds();
}

// 跳转到列表外期号时，后台定位包含它的页作为当前窗口（成功后 Prev/Next 直接可用）
async function locatePage(cid) {
  try {
    const r = await getGcnIds(null, cid);
    if (r && r.pos >= 0 && _cid === cid) {   // _cid 已变则丢弃（用户已跳到别处）
      _ids = r.ids; _total = r.total; _windowPage = r.page; _idx = r.pos;
      const label = document.getElementById('gcnCurLabel');
      if (label) label.textContent = `#${cid}（${_idx + 1}/${_ids.length}）`;
    }
  } catch { /* 定位失败仅失去翻页定位，不打扰阅读 */ }
}

// ─── 最早 / 最新 一键定位（分别走后端 page=last / page=first） ───
async function gotoFirst() {
  try {
    const r = await getGcnIds('last');   // 最后一页 = 最老一页
    _ids = r.ids; _total = r.total; _windowPage = r.page;
    showCircular(_ids[_ids.length - 1]); // 该页最旧 = 全列表最早一期
  } catch (err) {
    showToast(`获取最早一期失败: ${err.message}`, 'danger');
  }
}

async function gotoLast() {
  try {
    const r = await getGcnIds();         // 第 1 页 = 最新一页
    _ids = r.ids; _total = r.total; _windowPage = r.page;
    showCircular(_ids[0]);               // 该页最新 = 最新一期
  } catch (err) {
    showToast(`获取最新一期失败: ${err.message}`, 'danger');
  }
}

// ═══ 左栏：浏览器 ═══
async function loadIds() {
  const line = document.getElementById('gcnStatusLine');
  try {
    const idsResp = await getGcnIds();
    _ids = idsResp.ids;
    _total = idsResp.total;
    _windowPage = 1;
    // /ids 响应已含 totalItems 与最新一期，无需再单独请求 /status
    updateStatusLine({ count: _total, latest_id: _ids[0] ?? null });
  } catch (err) {
    if (line) line.textContent = `GCN 加载失败: ${err.message}`;
  }
}

function updateStatusLine(st) {
  const line = document.getElementById('gcnStatusLine');
  if (!line) return;
  let text = `在线直连 GCN，共 ${st.count} 期` + (st.latest_id != null ? `（最新 #${st.latest_id}）` : '');
  line.textContent = text;
}

function onJump() {
  const raw = document.getElementById('gcnJump').value.trim();
  if (!raw) return;
  const cid = parseInt(raw, 10);
  if (!Number.isInteger(cid)) { showToast('期号应为整数', 'warning'); return; }
  showCircular(cid);
  document.getElementById('gcnJump').value = '';
}

async function step(dir) {
  if (!_ids.length) return;
  if (_idx < 0) {
    // 未定位（如直接跳转了列表外期号）：从当前窗口两端开始
    showCircular(dir > 0 ? _ids[0] : _ids[_ids.length - 1]);
    return;
  }
  // 列表为降序（index 0 = 最新一期）：Prev（更旧）→ 索引 +1；Next（更新）→ 索引 -1
  const next = _idx - dir;
  if (next >= 0 && next < _ids.length) { showCircular(_ids[next]); return; }
  // 窗口边界：按需取相邻页（第 1 页 = 最新一期；更旧的页号更大）
  const targetPage = _windowPage - dir;
  const maxPage = Math.ceil(_total / 100);
  if (targetPage < 1 || targetPage > maxPage) {
    showToast(dir > 0 ? '已是最后一期' : '已是第一期', 'info');
    return;
  }
  try {
    const r = await getGcnIds(targetPage);
    _ids = r.ids; _windowPage = r.page;
    // 相邻页中与当前最接近的一期：Prev → 新页第一条（最新）；Next → 新页最后一条（最旧）
    showCircular(dir > 0 ? _ids[_ids.length - 1] : _ids[0]);
  } catch (err) {
    showToast(`获取第 ${targetPage} 页失败: ${err.message}`, 'danger');
  }
}

async function showCircular(cid) {
  const viewer = document.getElementById('gcnViewer');
  const label = document.getElementById('gcnCurLabel');
  const idx = _ids.indexOf(cid);
  _idx = idx; _cid = cid;
  // 反代模式下任意期号均可按需获取；不在当前窗口中的仅失去 Prev/Next 定位
  label.textContent = idx >= 0
    ? `#${cid}（${_idx + 1}/${_ids.length}）`
    : `#${cid}（列表外，共 ${_total} 期）`;
  viewer.innerHTML = '<span class="text-secondary small">加载中...</span>';
  if (idx < 0) locatePage(cid);  // 后台定位所在页，让 Prev/Next 立即可用
  try {
    const data = await getGcnCircular(cid);
    viewer.innerHTML = formatGcnHtml(data);
    renderChips(data);
    exitEditMode();
    // 联动：自动填充测光录入卡的 gcn_id 与 reference
    const gcnIdInp = document.getElementById('gcnObsGcnId');
    if (gcnIdInp) gcnIdInp.value = String(data.circularId ?? cid);
    const refInp = document.getElementById('gcnObsRef');
    if (refInp) refInp.value = `GCN ${data.circularId ?? cid}`;
    loadRelated(cid);
  } catch (err) {
    viewer.innerHTML = `<span class="text-danger small">加载失败: ${esc(err.message)}</span>`;
    const row = document.getElementById('gcnRelatedRow');
    if (row) row.style.display = 'none';
  }
}

// ═══ 关联光变记录面板 ═══
async function loadRelated(cid) {
  const row = document.getElementById('gcnRelatedRow');
  const summary = document.getElementById('gcnRelatedSummary');
  const body = document.getElementById('gcnRelatedBody');
  if (!row || !summary || !body) return;
  row.style.display = '';
  summary.textContent = '加载中...';
  body.innerHTML = '';
  try {
    const resp = await getGcnRelated(cid);
    renderRelated(resp);
    // 命中的记录只涉及一个源时，自动加载源信息卡（GCN 上下文一目了然）
    const tids = new Set([...resp.exact, ...resp.fuzzy].map(r => r.transient_id));
    if (tids.size === 1) {
      const tid = [...tids][0];
      if (tid !== _tid) {
        document.getElementById('gcnSrcId').value = tid;
        await loadSource(tid);
      }
    }
  } catch (err) {
    summary.innerHTML = `<span class="text-danger">查询失败: ${esc(err.message)}</span>`;
  }
}

function fluxCell(r) {
  const fmt = (v) => v == null ? '' : Number(v.toPrecision ? v.toPrecision(6) : v);
  const err = r.flux_density_err != null ? `±${fmt(r.flux_density_err)}` : '';
  if (r.flux_density_unit === 'magnitude') {
    return `${fmt(r.flux_density)}${err} mag${r.mag_system ? ' ' + esc(r.mag_system) : ''}`;
  }
  return `${fmt(r.flux_density)}${err} ${esc(r.flux_density_unit || '')}`;
}

// 合并 exact/fuzzy 并标注匹配方式
function relatedAll() {
  if (!_relatedResp) return [];
  return [
    ..._relatedResp.exact.map(r => ({ ...r, _kind: 'exact' })),
    ..._relatedResp.fuzzy.map(r => ({ ...r, _kind: 'fuzzy' })),
  ];
}

function renderRelated(resp) {
  _relatedResp = resp;
  _relSort = { key: null, dir: 1 };
  _relFilter = { tid: '', band: '', ref: '', comment: '' };
  // 筛选下拉选项取自本次结果集的 distinct 值
  const all = relatedAll();
  const fillSel = (id, values, label) => {
    const sel = document.getElementById(id);
    sel.innerHTML = `<option value="">${label}</option>` +
      values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  };
  fillSel('gcnRelFTid', [...new Set(all.map(r => r.transient_id))].sort(), '全部源');
  fillSel('gcnRelFBand', [...new Set(all.map(r => r.band).filter(Boolean))].sort(), '全部 band');
  document.getElementById('gcnRelFRef').value = '';
  document.getElementById('gcnRelFCmt').value = '';
  applyRelatedView();
}

function applyRelatedView() {
  const summary = document.getElementById('gcnRelatedSummary');
  const body = document.getElementById('gcnRelatedBody');
  if (!summary || !body || !_relatedResp) return;
  const all = relatedAll();
  const f = _relFilter;
  let rows = all.filter(r =>
    (!f.tid || r.transient_id === f.tid) &&
    (!f.band || r.band === f.band) &&
    (!f.ref || (r.reference || '').toLowerCase().includes(f.ref)) &&
    (!f.comment || (r.comment || '').toLowerCase().includes(f.comment)));
  // 排序（null 值排最后）
  if (_relSort.key) {
    const k = _relSort.key, d = _relSort.dir;
    rows = rows.slice().sort((a, b) => {
      let va = k === '_kind' ? (a._kind === 'exact' ? 0 : 1) : a[k];
      let vb = k === '_kind' ? (b._kind === 'exact' ? 0 : 1) : b[k];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * d;
      return String(va).localeCompare(String(vb)) * d;
    });
  }
  // 排序指示箭头
  document.querySelectorAll('#gcnRelatedRow th.lc-sortable').forEach(th => {
    const arrow = th.dataset.key === _relSort.key ? (_relSort.dir === 1 ? ' ▲' : ' ▼') : '';
    th.textContent = (th.dataset.label || th.textContent) + arrow;
  });
  // 摘要行
  const mtNames = Object.keys(_relatedResp.matched_transients || {});
  const nExact = _relatedResp.exact.length;
  summary.textContent =
    `reference 精确命中 ${nExact} 条 ｜ 暴名匹配 ${mtNames.length ? mtNames.join(', ') : '（无）'} ` +
    `共 ${_relatedResp.fuzzy.length} 条（已排除 X 射线等高能波段）` +
    (rows.length !== all.length ? ` ｜ 当前显示 ${rows.length}/${all.length} 条` : '');
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="10" class="text-secondary small">${
      all.length ? '当前筛选条件下无记录' : '库中暂无与本 GCN 相关的光变记录（光学/射电波段）'}</td></tr>`;
    return;
  }
  const rowHtml = (r) => `<tr data-lcid="${r.id}" style="cursor:pointer" title="点击载入到测光录入卡">
    <td>${r._kind === 'exact'
      ? '<span class="badge" style="background:var(--accent-green)">reference</span>'
      : '<span class="badge" style="background:var(--accent-blue)">暴名</span>'}</td>
    <td>${esc(r.transient_id)}</td>
    <td>${esc(r.band || '')}</td>
    <td>${r.time != null ? r.time : ''}</td>
    <td>${fluxCell(r)}</td>
    <td>${r.upperlimit ? 'UL' : ''}</td>
    <td>${esc(r.telescope || '')}</td>
    <td class="small">${esc(r.reference || '')}</td>
    <td class="small text-secondary">${esc(r.comment || '')}</td>
    <td><button class="btn btn-sm btn-outline-primary py-0 px-1 gcn-rel-load" data-lcid="${r.id}">载入</button></td>
  </tr>`;
  body.innerHTML = rows.map(rowHtml).join('');
  const byId = new Map(all.map(r => [r.id, r]));
  body.querySelectorAll('tr[data-lcid]').forEach(tr => {
    tr.addEventListener('click', () => {
      const rec = byId.get(Number(tr.dataset.lcid));
      if (rec) enterEditMode(rec);
    });
  });
}

// ═══ 测光录入卡：编辑已有记录模式 ═══
// 把库中记录回填到录入表单；优先用 extra_data 中的原始 start/end/expo 及其单位还原
function enterEditMode(rec) {
  const $ = (id) => document.getElementById(id);
  _editLcId = rec.id;
  _editTid = rec.transient_id;
  const ed = rec.extra_data || {};
  // 源联动（异步加载源信息卡，不阻塞表单回填）
  if (rec.transient_id && rec.transient_id !== _tid) {
    $('gcnSrcId').value = rec.transient_id;
    loadSource(rec.transient_id).catch(() => {});
  }
  // ── 时间 ──
  const setT = (k, val, unit) => {
    $(`gcnObsT_${k}`).value = val != null ? val : '';
    if (unit && $(`gcnObsU_${k}`)) $(`gcnObsU_${k}`).value = unit;
  };
  setT('start', ed.obs_time_start, ed.obs_time_start_unit);
  setT('end', ed.obs_time_end, ed.obs_time_end_unit);
  if (ed.obs_exposure != null) {
    $('gcnObsExpo').value = ed.obs_exposure;
    if (ed.obs_exposure_unit) $('gcnObsExpoU').value = ed.obs_exposure_unit;
  } else if (ed.obs_time_start == null && rec.time_err != null) {
    // 无原始曝光记录时用 time_err 反推（timeErr = expo/2）
    $('gcnObsExpo').value = 2 * rec.time_err;
    $('gcnObsExpoU').value = 's';
  } else {
    $('gcnObsExpo').value = '';
  }
  setT('mid', ed.obs_time_start == null ? rec.time : null, 's');
  // ── 流量 ──
  const ensureOption = (selId, val) => {
    const sel = $(selId);
    if (val && ![...sel.options].some(o => o.value === val)) {
      sel.add(new Option(val, val));
    }
    if (val) sel.value = val;
  };
  if (rec.flux_density_unit === 'magnitude') {
    $('gcnObsMag').value = rec.flux_density;
    $('gcnObsMagErr').value = rec.flux_density_err != null ? rec.flux_density_err : '';
    ensureOption('gcnObsMagSys', rec.mag_system);
    $('gcnObsFlux').value = ''; $('gcnObsFluxErr').value = '';
  } else {
    $('gcnObsFlux').value = rec.flux_density;
    $('gcnObsFluxErr').value = rec.flux_density_err != null ? rec.flux_density_err : '';
    ensureOption('gcnObsFluxUnit', rec.flux_density_unit);
    $('gcnObsMag').value = ''; $('gcnObsMagErr').value = '';
  }
  // ── 其余字段 ──
  $('gcnObsBand').value = rec.band || '';
  $('gcnObsUl').value = rec.upperlimit ? 'upperlimit' : 'detection';
  $('gcnObsTel').value = rec.telescope || '';
  $('gcnObsInstr').value = rec.instrument || '';
  $('gcnObsRef').value = rec.reference || '';
  $('gcnObsComment').value = rec.comment || '';
  $('gcnObsGcnId').value = ed.gcn_id != null ? ed.gcn_id : '';
  // ── 按钮/徽标切换 ──
  $('gcnObsSave').innerHTML = `<i class="bi bi-check-lg"></i> 更新记录 #${rec.id}`;
  $('gcnObsSaveNew').style.display = '';
  $('gcnObsCancelEdit').style.display = '';
  const badge = $('gcnObsEditBadge');
  badge.style.display = '';
  badge.textContent = `正在编辑已有记录 #${rec.id}（${rec.transient_id}）；「保存测光记录」将覆盖更新该条，或选「作为新记录保存」`;
  showToast(`已载入记录 #${rec.id}，可修改后更新或另存为新记录`, 'info');
}

function exitEditMode() {
  _editLcId = null;
  _editTid = null;
  const $ = (id) => document.getElementById(id);
  const save = $('gcnObsSave');
  if (save) save.innerHTML = '<i class="bi bi-check-lg"></i> 保存测光记录';
  ['gcnObsSaveNew', 'gcnObsCancelEdit', 'gcnObsEditBadge'].forEach(id => {
    const el = $(id);
    if (el) el.style.display = 'none';
  });
}

function renderChips(data) {
  const box = document.getElementById('gcnChips');
  const tokens = extractNameTokens(data).slice(0, 12);
  if (!tokens.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<span class="small text-secondary">候选源:</span>' + tokens.map(t =>
    `<button class="btn btn-sm btn-outline-info py-0 px-1 gcn-chip" data-name="${esc(t)}" title="查库加载 / 预填新建">${esc(t)}</button>`
  ).join('');
  box.querySelectorAll('.gcn-chip').forEach(chip => {
    chip.addEventListener('click', () => onChip(chip.dataset.name));
  });
}



// ─── 时间计算器 ───
function parseTimeInput(val, fmt) {
  const s = val.trim();
  if (!s) return { ms: null };
  if (fmt === 'MJD') {
    const mjd = parseFloat(s);
    if (!isFinite(mjd)) return { ms: null, err: `无法把 '${s}' 解析为 MJD` };
    return { ms: (mjd - 40587) * 86400000 };  // MJD 40587 = 1970-01-01T00:00:00Z
  }
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?$/);
  if (!m) return { ms: null, err: `无法把 '${s}' 解析为 UTC（YYYY-MM-DD[T ]HH:MM:SS[.fff]）` };
  const ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) + (m[7] ? parseFloat(m[7]) * 1000 : 0);
  return { ms };
}

function calcDelta() {
  const $ = (id) => document.getElementById(id);
  const r1 = parseTimeInput($('gcnCalcT1').value, $('gcnCalcF1').value);
  const r2 = parseTimeInput($('gcnCalcT2').value, $('gcnCalcF2').value);
  const out = $('gcnCalcResult');
  if (r1.err) { out.value = `time 1 error: ${r1.err}`; return; }
  if (r2.err) { out.value = `time 2 error: ${r2.err}`; return; }
  if (r1.ms == null || r2.ms == null) { out.value = 'Δt = (请输入两个时间)'; return; }
  const ds = (r2.ms - r1.ms) / 1000;
  out.value = `Δt = ${ds.toLocaleString('en-US', { maximumFractionDigits: 6 })} s ｜ ` +
    `${(ds / 60).toLocaleString('en-US', { maximumFractionDigits: 6 })} min ｜ ` +
    `${(ds / 3600).toLocaleString('en-US', { maximumFractionDigits: 6 })} h ｜ ` +
    `${(ds / 86400).toLocaleString('en-US', { maximumFractionDigits: 8 })} day`;
}

// ═══ 右上：源信息卡 ═══
function srcStatus(msg, isErr) {
  const el = document.getElementById('gcnSrcStatus');
  if (el) el.innerHTML = isErr ? `<span class="text-danger">${esc(msg)}</span>` : esc(msg);
}

async function onChip(name) {
  try {
    const resp = await getTransients({ search: name, per_page: 20 });
    const low = name.toLowerCase();
    const hit = resp.items.find(t =>
      t.id.toLowerCase() === low ||
      (t.aliases || []).some(a => String(a).toLowerCase() === low));
    if (hit) {
      document.getElementById('gcnSrcId').value = hit.id;
      await loadSource(hit.id);
    } else {
      clearSrcForm();
      document.getElementById('gcnSrcId').value = name;
      srcStatus(`库中未找到 ${name}，可补全字段后点击「新建源」`);
    }
  } catch (err) {
    showToast(`查询失败: ${err.message}`, 'danger');
  }
}

async function onSrcLoad() {
  const id = document.getElementById('gcnSrcId').value.trim();
  if (!id) { srcStatus('请先输入源 ID'); return; }
  // 先精确取；404 则走 search 容错（别名命中）
  try {
    await loadSource(id);
  } catch {
    try {
      const resp = await getTransients({ search: id, per_page: 20 });
      const low = id.toLowerCase();
      const hit = resp.items.find(t =>
        t.id.toLowerCase() === low ||
        (t.aliases || []).some(a => String(a).toLowerCase() === low));
      if (hit) {
        document.getElementById('gcnSrcId').value = hit.id;
        await loadSource(hit.id);
      } else {
        srcStatus(`库中未找到 ${id}，可补全字段后点击「新建源」`, true);
      }
    } catch (err) {
      srcStatus(`查询失败: ${err.message}`, true);
    }
  }
}

async function loadSource(tid) {
  const t = await getTransient(tid);
  _tid = t.id;
  const $ = (id) => document.getElementById(id);
  $('gcnSrcId').value = t.id;
  $('gcnSrcAliases').value = (t.aliases || []).join(', ');
  $('gcnSrcRa').value = t.ra != null ? t.ra : '';
  $('gcnSrcDec').value = t.dec != null ? t.dec : '';
  $('gcnSrcT0').value = t.t0 ? t.t0.replace(' ', 'T').slice(0, 26) : '';
  $('gcnSrcInstr').value = t.trigger_instrument || '';
  $('gcnSrcZ').value = t.redshift != null ? t.redshift : '';
  $('gcnSrcTags').value = (t.tags || []).join(', ');
  const link = $('gcnDetailLink');
  link.style.display = '';
  link.href = `#/transient/${encodeURIComponent(t.id)}`;
  srcStatus(`已加载 ${t.id}（${t.lc_count != null ? t.lc_count + ' 条测光记录' : ''}）`);
  updateObsTarget(t.id, t.lc_count);
}

function collectSrcBody() {
  const $ = (id) => document.getElementById(id);
  const raR = parseCoord($('gcnSrcRa').value, true);
  const decR = parseCoord($('gcnSrcDec').value, false);
  if (raR.err) return { err: raR.err };
  if (decR.err) return { err: decR.err };
  const zs = $('gcnSrcZ').value.trim();
  const z = zs ? parseFloat(zs) : null;
  if (zs && !isFinite(z)) return { err: '红移必须是数值或留空' };
  const splitList = (v) => v.split(/[,;\n]/).map(x => x.trim()).filter(Boolean);
  return {
    body: {
      aliases: splitList($('gcnSrcAliases').value),
      ra: raR.deg, dec: decR.deg, redshift: z,
      t0: $('gcnSrcT0').value.trim() || null,
      trigger_instrument: $('gcnSrcInstr').value.trim() || null,
      tags: splitList($('gcnSrcTags').value),
    },
  };
}

async function saveSource(isNew) {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  const id = document.getElementById('gcnSrcId').value.trim();
  if (!id) { srcStatus('源 ID 必填', true); return; }
  if (/\s/.test(id)) { srcStatus('源 ID 不能含空白字符', true); return; }
  const { body, err } = collectSrcBody();
  if (err) { srcStatus(err, true); return; }
  try {
    let t;
    if (isNew) {
      t = await createTransient({ id, ...body });
      showToast(`已新建源 ${t.id}`, 'success');
    } else {
      if (!_tid) { srcStatus('请先「加载所选」再保存修改', true); return; }
      t = await updateTransient(_tid, body);
      showToast(`已保存 ${t.id}`, 'success');
    }
    await loadSource(t.id);
  } catch (e) {
    srcStatus(`${isNew ? '新建' : '保存'}失败: ${e.message}`, true);
  }
}

function clearSrcForm() {
  ['gcnSrcId', 'gcnSrcAliases', 'gcnSrcRa', 'gcnSrcDec', 'gcnSrcT0', 'gcnSrcInstr', 'gcnSrcZ', 'gcnSrcTags']
    .forEach(id => { document.getElementById(id).value = ''; });
  ['gcnSrcRaFb', 'gcnSrcDecFb'].forEach(id => { document.getElementById(id).textContent = ''; });
  _tid = null;
  document.getElementById('gcnDetailLink').style.display = 'none';
  srcStatus('');
  updateObsTarget(null);
}

// ═══ 右下：测光录入卡 ═══
async function updateObsTarget(tid, count) {
  const el = document.getElementById('gcnObsTarget');
  if (!el) return;
  if (!tid) { el.textContent = '未选择源'; return; }
  if (count == null) {
    try {
      const resp = await getLightcurves({ transient_id: tid, per_page: 1 });
      count = resp.total;
    } catch { count = '?'; }
  }
  el.textContent = `→ ${tid}（已有 ${count} 条记录）`;
}

function numOrNull(id) {
  const s = document.getElementById(id).value.trim();
  if (!s) return null;
  const n = parseFloat(s);
  return isFinite(n) ? n : undefined;
}

async function saveObs(mode) {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  const updating = mode !== 'new' && _editLcId != null;
  const tid = _tid || (updating ? _editTid : null);
  if (!tid) { showToast('请先在上方加载或新建源', 'warning'); return; }
  const $ = (id) => document.getElementById(id);

  // ── 时间：各量按各自单位换算为秒 ──
  const tStart = numOrNull('gcnObsT_start');
  const tMid = numOrNull('gcnObsT_mid');
  const tEnd = numOrNull('gcnObsT_end');
  const expo = numOrNull('gcnObsExpo');
  if ([tStart, tMid, tEnd, expo].includes(undefined)) { showToast('时间/曝光字段必须是数值或留空', 'warning'); return; }
  const uStart = TIME_FACTOR[$('gcnObsU_start').value];
  const uMid = TIME_FACTOR[$('gcnObsU_mid').value];
  const uEnd = TIME_FACTOR[$('gcnObsU_end').value];
  const uExpo = TIME_FACTOR[$('gcnObsExpoU').value];
  const startS = tStart != null ? tStart * uStart : null;
  const midS = tMid != null ? tMid * uMid : null;
  const endS = tEnd != null ? tEnd * uEnd : null;
  const expoS = expo != null ? expo * uExpo : null;

  let time = null;
  if (midS != null) time = midS;
  else if (startS != null && endS != null) time = (startS + endS) / 2;
  else if (startS != null && expoS != null) time = startS + expoS / 2;
  else { showToast('请填写 mid，或 start+end，或 start+exposure', 'warning'); return; }
  const timeErr = (startS != null && endS != null) ? Math.abs(endS - startS) / 2
    : (expoS != null ? expoS / 2 : null);

  // ── 流量：星等优先，否则流量密度 ──
  const mag = numOrNull('gcnObsMag');
  const magErr = numOrNull('gcnObsMagErr');
  const flux = numOrNull('gcnObsFlux');
  const fluxErr = numOrNull('gcnObsFluxErr');
  if ([mag, magErr, flux, fluxErr].includes(undefined)) { showToast('星等/流量字段必须是数值或留空', 'warning'); return; }
  let flux_density, flux_density_err, flux_density_unit, mag_system = null;
  if (mag != null) {
    flux_density = mag;
    flux_density_err = magErr;
    flux_density_unit = 'magnitude';
    mag_system = $('gcnObsMagSys').value;
  } else if (flux != null) {
    flux_density = flux;
    flux_density_err = fluxErr;
    flux_density_unit = $('gcnObsFluxUnit').value;
  } else {
    showToast('星等 magnitude 与流量密度 flux_density 至少填一个', 'warning'); return;
  }

  const band = $('gcnObsBand').value.trim();
  if (!band) { showToast('波段 band 必填', 'warning'); return; }

  const gcnId = $('gcnObsGcnId').value.trim();
  const extra_data = {};
  if (gcnId) extra_data.gcn_id = gcnId;
  if (expo != null) { extra_data.obs_exposure = expo; extra_data.obs_exposure_unit = $('gcnObsExpoU').value; }
  if (tStart != null) { extra_data.obs_time_start = tStart; extra_data.obs_time_start_unit = $('gcnObsU_start').value; }
  if (tEnd != null) { extra_data.obs_time_end = tEnd; extra_data.obs_time_end_unit = $('gcnObsU_end').value; }

  const body = {
    transient_id: tid,
    time, time_err: timeErr,
    band, flux_density, flux_density_err, flux_density_unit,
    mag_system,
    upperlimit: $('gcnObsUl').value === 'upperlimit',
    telescope: $('gcnObsTel').value.trim() || null,
    instrument: $('gcnObsInstr').value.trim() || null,
    reference: $('gcnObsRef').value.trim() || null,
    comment: $('gcnObsComment').value.trim() || null,
    extra_data,
  };
  try {
    if (updating) {
      await updateLightcurve(_editLcId, body);
      showToast(`已更新记录 #${_editLcId}（time=${time.toFixed(1)}s）`, 'success');
    } else {
      await createLightcurves([body]);
      showToast(`已保存 1 条测光记录到 ${tid}（time=${time.toFixed(1)}s）`, 'success');
    }
    exitEditMode();
    clearObsForm(true);
    updateObsTarget(tid);
    if (_cid != null) loadRelated(_cid);   // 刷新关联记录面板
  } catch (err) {
    showToast(`保存失败: ${err.message}`, 'danger');
  }
}

function clearObsForm(showTip) {
  ['gcnObsT_start', 'gcnObsT_mid', 'gcnObsT_end', 'gcnObsExpo',
   'gcnObsMag', 'gcnObsMagErr', 'gcnObsFlux', 'gcnObsFluxErr',
   'gcnObsBand', 'gcnObsTel', 'gcnObsInstr', 'gcnObsComment']
    .forEach(id => { document.getElementById(id).value = ''; });
  document.getElementById('gcnObsUl').value = 'detection';
  // gcn_id / reference 跟随当前 circular 重新自动填充
  const gcnIdInp = document.getElementById('gcnObsGcnId');
  if (gcnIdInp) gcnIdInp.value = _cid != null ? String(_cid) : '';
  const refInp = document.getElementById('gcnObsRef');
  if (refInp) refInp.value = _cid != null ? `GCN ${_cid}` : '';
  if (showTip === false) return;
}
