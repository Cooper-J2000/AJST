// === 抠图取数（工具箱子页面） ===
// 从光变图截图提取数据点：标定（线性/对数轴）→ 手动/颜色自动取点 → 导出 CSV 或直写 AJST 数据库。
// 点一律以图像像素坐标存储，数据坐标经标定变换实时换算（重新标定即整体修正）。
import { app, showLoading } from './layout.js';
import { getTransients, createTransient, createLightcurves, uploadSpectrum, isAuthed, showToast } from '../api.js';
import { parseRA, parseDec } from '../coords.js';
import {
  makeCalibTransform, rgbToLab, avgColorLab, buildMask, traceLine, detectSymbols,
  sampleLine, sampleSpline,
} from '../digitizer_core.js';

// ─── 模块状态（会话内跨页面保留，离开不丢工作） ───
let _img = null;             // HTMLImageElement
let _imgData = null;         // { pixels, width, height }
let _view = { scale: 1, ox: 0, oy: 0 };
let _mode = 'calib';         // calib | manual | auto | delete
let _calibPts = [];          // [{px,py}]，顺序 X① X② Y① Y②
let _transform = null;
let _datasets = [];          // {id,name,color,visible,points:[{px,py}]}
let _curDsId = null;
let _dsSeq = 0;
let _undoStack = [];         // [{dsId, points}] 操作前快照，深 20
let _targetLab = null;       // 自动提取目标色
let _roi = null;             // 框选范围 {x0,y0,x1,y1}（图像像素，已规范化）
let _ctrlPts = [];           // 插值控制点 [{px,py}]
let _src = null;             // 写库目标源 {id, t0}
let _pendingRecords = null;  // 预览生成的待写记录
let _drag = null;            // {type:'pan'|'point', ...}
let _canvas = null, _ctx = null;

const DS_COLORS = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
                   '#42d4f4', '#f032e6', '#bfef45', '#469990', '#9a6324'];
const TIME_FACTOR = { s: 1, m: 60, h: 3600, d: 86400 };
const CALIB_LABELS = ['X①', 'X②', 'Y①', 'Y②'];
const MJD_EPOCH_MS = 40587 * 86400000;  // MJD 40587 = 1970-01-01T00:00:00Z
const C_ANG_PER_S = 2.99792458e18;      // 光速，Å/s（AB 星等 → F_λ 换算用）

// AB 星等 → 绝对流量 F_λ (erg/s/cm²/Å)：
// F_ν = 10^(-(m_AB+48.60)/2.5) [erg/s/cm²/Hz]；F_λ = F_ν·c/λ²（λ 单位 Å）
function abMagToFlambda(mag, wlAng) {
  return Math.pow(10, -(mag + 48.60) / 2.5) * C_ANG_PER_S / (wlAng * wlAng);
}

// ST 星等（HST 系统）→ 绝对流量 F_λ (erg/s/cm²/Å)：
// 零点 F_λ = 3.6307805e-9 erg/s/cm²/Å（astropy 精确值），直接定义在波长通量上，无需波长参与
function stMagToFlambda(mag) {
  return 3.6307805e-9 * Math.pow(10, -0.4 * mag);
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ═══ 页面渲染 ═══
export async function render() {
  showLoading();
  app.innerHTML = `
    <div class="page-header"><h4><i class="bi bi-vector-pen"></i> 抠图取数</h4></div>
    <div class="row g-3">
      <div class="col-lg-8">
        <div class="card">
          <div class="card-header d-flex flex-wrap gap-1 align-items-center py-1">
            <label class="btn btn-sm btn-outline-primary mb-0">
              <i class="bi bi-image"></i> 加载图片
              <input type="file" id="dgzFile" accept="image/*" hidden>
            </label>
            <div class="btn-group btn-group-sm ms-2" id="dgzModes">
              <button class="btn btn-outline-secondary" data-mode="calib">标定</button>
              <button class="btn btn-outline-secondary" data-mode="manual">手动取点</button>
              <button class="btn btn-outline-secondary" data-mode="auto">自动取色</button>
              <button class="btn btn-outline-secondary" data-mode="rect">框选范围</button>
              <button class="btn btn-outline-secondary" data-mode="interp">插值取点</button>
              <button class="btn btn-outline-secondary" data-mode="delete">删除点</button>
            </div>
            <button class="btn btn-sm btn-outline-warning ms-2" id="dgzUndo"><i class="bi bi-arrow-counterclockwise"></i> 撤销</button>
            <button class="btn btn-sm btn-outline-danger" id="dgzClearDs">清空当前集</button>
            <button class="btn btn-sm btn-outline-secondary ms-auto" id="dgzFit">重置视图</button>
          </div>
          <div class="card-body p-1">
            <canvas id="dgzCanvas" style="width:100%;height:540px;display:block;cursor:crosshair"></canvas>
            <div class="d-flex justify-content-between small text-secondary px-1 pb-1">
              <span id="dgzHint"></span>
              <span id="dgzCursor"></span>
            </div>
          </div>
        </div>
      </div>
      <div class="col-lg-4">
        <!-- 坐标轴标定 -->
        <div class="card mb-3">
          <div class="card-header py-1"><span class="small fw-bold">① 坐标轴标定</span>
            <span class="small float-end" id="dgzCalibStatus"></span></div>
          <div class="card-body py-2" style="font-size:0.85rem">
            <div class="row g-1 align-items-center mb-1">
              <div class="col-2 small">X①</div><div class="col-4"><input class="form-control form-control-sm" id="dgzX1v" placeholder="值"></div>
              <div class="col-2 small">X②</div><div class="col-4"><input class="form-control form-control-sm" id="dgzX2v" placeholder="值"></div>
            </div>
            <div class="row g-1 align-items-center mb-1">
              <div class="col-2 small">Y①</div><div class="col-4"><input class="form-control form-control-sm" id="dgzY1v" placeholder="值"></div>
              <div class="col-2 small">Y②</div><div class="col-4"><input class="form-control form-control-sm" id="dgzY2v" placeholder="值"></div>
            </div>
            <div class="d-flex gap-3 align-items-center mb-2">
              <div class="form-check form-check-inline mb-0 small">
                <input class="form-check-input" type="checkbox" id="dgzXLog"><label class="form-check-label" for="dgzXLog">X 对数轴</label>
              </div>
              <div class="form-check form-check-inline mb-0 small">
                <input class="form-check-input" type="checkbox" id="dgzYLog"><label class="form-check-label" for="dgzYLog">Y 对数轴</label>
              </div>
              <span class="small text-secondary">（星等轴反转：上方点填小值即可）</span>
            </div>
            <div class="d-flex gap-1">
              <button class="btn btn-sm btn-primary" id="dgzCalibApply">应用标定</button>
              <button class="btn btn-sm btn-outline-secondary" id="dgzCalibReset">重新标定</button>
            </div>
          </div>
        </div>
        <!-- 数据集 -->
        <div class="card mb-3">
          <div class="card-header py-1 d-flex justify-content-between align-items-center">
            <span class="small fw-bold">② 数据集</span>
            <button class="btn btn-sm btn-outline-primary py-0" id="dgzDsAdd"><i class="bi bi-plus"></i> 添加</button>
          </div>
          <div class="card-body p-1" id="dgzDsList" style="max-height:150px;overflow-y:auto"></div>
        </div>
        <!-- 框选范围 -->
        <div class="card mb-3">
          <div class="card-header py-1"><span class="small fw-bold">③ 框选范围（ROI）</span></div>
          <div class="card-body py-2" style="font-size:0.85rem">
            <div class="small text-secondary mb-1" id="dgzRoiInfo">未框选：自动提取作用于全图</div>
            <div class="d-flex gap-1">
              <button class="btn btn-sm btn-outline-secondary" id="dgzRoiClear" disabled>取消框选</button>
              <button class="btn btn-sm btn-outline-danger" id="dgzRoiDel" disabled>删除框内点（当前集）</button>
            </div>
          </div>
        </div>
        <!-- 自动提取 -->
        <div class="card mb-3">
          <div class="card-header py-1"><span class="small fw-bold">④ 自动提取（按颜色）</span></div>
          <div class="card-body py-2" style="font-size:0.85rem">
            <div class="d-flex gap-2 align-items-center mb-1">
              <span class="small">目标色</span>
              <span id="dgzSwatch" style="display:inline-block;width:18px;height:18px;border:1px solid var(--border-color);border-radius:3px"></span>
              <span class="small text-secondary" id="dgzSwatchInfo">在「自动取色」模式点击图中曲线/符号取色</span>
            </div>
            <div class="d-flex gap-2 align-items-center mb-1">
              <span class="small" style="width:70px">容差 ΔE</span>
              <input type="range" class="form-range" id="dgzTol" min="5" max="80" value="25" style="width:120px">
              <span class="small" id="dgzTolV">25</span>
            </div>
            <div class="d-flex gap-2 align-items-center mb-1 flex-wrap">
              <select class="form-select form-select-sm" id="dgzAutoMode" style="width:auto">
                <option value="symbol" selected>符号模式（离散点）</option>
                <option value="line">描线模式（连续曲线）</option>
              </select>
              <span class="small" id="dgzParamArea">面积 <input class="form-control form-control-sm d-inline-block" id="dgzMinArea" value="5" style="width:55px"> – <input class="form-control form-control-sm d-inline-block" id="dgzMaxArea" value="500" style="width:65px"> px</span>
              <span class="small" id="dgzParamStride" style="display:none">列步长 <input class="form-control form-control-sm d-inline-block" id="dgzStride" value="1" style="width:55px"></span>
            </div>
            <button class="btn btn-sm btn-primary" id="dgzAutoRun"><i class="bi bi-magic"></i> 执行提取</button>
            <span class="small text-secondary ms-1">结果进入当前数据集</span>
          </div>
        </div>
        <!-- 生成取点（直线/样条） -->
        <div class="card mb-3">
          <div class="card-header py-1"><span class="small fw-bold">⑤ 生成取点（直线/样条）</span></div>
          <div class="card-body py-2" style="font-size:0.85rem">
            <div class="d-flex gap-2 align-items-center mb-1 flex-wrap">
              <select class="form-select form-select-sm" id="dgzInterpMode" style="width:auto">
                <option value="line" selected>直线（2 个端点）</option>
                <option value="spline">样条（≥3 个控制点）</option>
              </select>
              <span class="small">步长 <input class="form-control form-control-sm d-inline-block" id="dgzInterpStep" value="5" style="width:60px"> 图像像素</span>
            </div>
            <div id="dgzSplineOpts" style="display:none">
              <div class="d-flex gap-2 align-items-center mb-1 flex-wrap">
                <span class="small text-secondary">样条参数:</span>
                <select class="form-select form-select-sm" id="dgzSplineType" style="width:auto">
                  <option value="natural" selected>自然三次样条（光滑，可能过冲）</option>
                  <option value="catmull">Catmull-Rom（张力可调，不易过冲）</option>
                </select>
                <span class="small dgz-tension-wrap" style="display:none">张力
                  <input type="range" class="form-range d-inline-block align-middle" id="dgzTension" min="0" max="1" step="0.05" value="0.5" style="width:90px"
                         title="0=最松弛（标准 CR），1=最紧（贴近折线）">
                  <span id="dgzTensionV">0.50</span>
                </span>
              </div>
            </div>
            <div class="small text-secondary mb-1" id="dgzCtrlInfo">在「插值取点」模式点击图上放置控制点（当前 0 个）</div>
            <div class="d-flex gap-1">
              <button class="btn btn-sm btn-primary" id="dgzInterpRun"><i class="bi bi-bezier2"></i> 生成点</button>
              <button class="btn btn-sm btn-outline-secondary" id="dgzCtrlClear">清除控制点</button>
            </div>
          </div>
        </div>
        <!-- 输出 -->
        <div class="card">
          <div class="card-header py-1"><span class="small fw-bold">⑥ 导出 / 写入 AJST</span></div>
          <div class="card-body py-2" style="font-size:0.85rem">
            <button class="btn btn-sm btn-outline-primary mb-2" id="dgzExport"><i class="bi bi-download"></i> 导出 CSV</button>
            <div class="gcn-form-sec">写入数据库</div>
            <div class="d-flex gap-1 mb-1">
              <input class="form-control form-control-sm" id="dgzSrcQ" placeholder="源 ID / 别名">
              <button class="btn btn-sm btn-outline-secondary" id="dgzSrcSearch">搜索</button>
              ${isAuthed() ? `<button class="btn btn-sm btn-outline-primary" id="dgzSrcNewBtn" title="源还不存在？在此新建"><i class="bi bi-plus-lg"></i> 新建源</button>` : ''}
            </div>
            ${isAuthed() ? `
            <div id="dgzSrcNewForm" class="border rounded p-2 mb-1" style="display:none">
              <div class="row g-1">
                <div class="col-12"><input class="form-control form-control-sm" id="dgzNewId" placeholder="源 ID（必填），如 GRB260901A"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzNewRa" placeholder="RA（度 或 08h08m27.4s，可空）"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzNewDec" placeholder="Dec（度 或 +40d36m44.8s，可空）"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzNewT0" placeholder="T0（UTC，可空）"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzNewZ" placeholder="红移 z（可空）"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzNewAliases" placeholder="别名（逗号分隔，可空）"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzNewTags" placeholder="标签（逗号分隔，可空）"></div>
              </div>
              <div class="form-text">T0 格式如 2026-09-01T12:34:56（UTC）；X 轴选 MJD 时需要源有 t0 才能换算，建议填写。</div>
              <div class="d-flex gap-1 mt-1">
                <button class="btn btn-sm btn-primary" id="dgzNewSave">创建并选中</button>
                <button class="btn btn-sm btn-outline-secondary" id="dgzNewCancel">取消</button>
              </div>
            </div>` : ''}
            <select class="form-select form-select-sm mb-1" id="dgzSrcSel" style="display:none"></select>
            <div class="small mb-1" id="dgzSrcInfo">未选择源</div>
            <div class="d-flex gap-2 align-items-center mb-1">
              <span class="small" style="width:70px">数据类型</span>
              <select class="form-select form-select-sm" id="dgzDataType" style="width:auto">
                <option value="lc" selected>光变点（lightcurves 表）</option>
                <option value="spec">光谱（spectra 表）</option>
              </select>
            </div>
            <!-- 光变点映射 -->
            <div id="dgzLcBlock">
              <div class="d-flex gap-2 align-items-center mb-1">
                <span class="small" style="width:70px">X 轴格式</span>
                <select class="form-select form-select-sm" id="dgzXFmt" style="width:auto">
                  <option value="s" selected>相对时间 (s)</option>
                  <option value="m">相对时间 (min)</option>
                  <option value="h">相对时间 (h)</option>
                  <option value="d">相对时间 (d)</option>
                  <option value="mjd">MJD（用源 t0 换算）</option>
                </select>
              </div>
              <div id="dgzDsMap"></div>
              <details class="mb-1">
                <summary class="small text-secondary" style="cursor:pointer">公共字段（reference / telescope / instrument / comment）</summary>
                <div class="row g-1 mt-1">
                  <div class="col-6"><input class="form-control form-control-sm" id="dgzRef" placeholder="reference"></div>
                  <div class="col-6"><input class="form-control form-control-sm" id="dgzTel" placeholder="telescope"></div>
                  <div class="col-6"><input class="form-control form-control-sm" id="dgzInstr" placeholder="instrument"></div>
                  <div class="col-6"><input class="form-control form-control-sm" id="dgzComment" placeholder="comment（默认 Digitizer）"></div>
                </div>
              </details>
            </div>
            <!-- 光谱映射 -->
            <div id="dgzSpecBlock" style="display:none">
              <div class="d-flex gap-2 align-items-center mb-1">
                <span class="small" style="width:70px">X 轴单位</span>
                <select class="form-select form-select-sm" id="dgzSpecXUnit" style="width:auto">
                  <option value="1" selected>Å</option>
                  <option value="10">nm</option>
                  <option value="10000">μm</option>
                </select>
                <span class="small text-secondary">统一转 Å，观测者系</span>
              </div>
              <div class="d-flex gap-2 align-items-center mb-1">
                <span class="small" style="width:70px">流量类型</span>
                <select class="form-select form-select-sm" id="dgzSpecFluxType" style="width:auto">
                  <option value="absolute" selected>绝对流量 (erg/s/cm²/Å)</option>
                  <option value="abmag">AB 星等（转绝对流量入库）</option>
                  <option value="stmag">ST 星等（转绝对流量入库）</option>
                  <option value="normalized">归一化流量</option>
                </select>
              </div>
              <div class="row g-1 mb-1">
                <div class="col-6"><input class="form-control form-control-sm" id="dgzSpecInstr" placeholder="instrument"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzSpecMjd" placeholder="MJD（观测日，可空）"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzSpecObserver" placeholder="observer（可空）"></div>
                <div class="col-6"><input class="form-control form-control-sm" id="dgzSpecReducer" placeholder="reducer（可空）"></div>
              </div>
              <div id="dgzDsMapSpec"></div>
            </div>
            <div class="d-flex gap-1">
              <button class="btn btn-sm btn-outline-primary" id="dgzPreview">预览入库数据</button>
              <button class="btn btn-sm btn-success" id="dgzWrite" disabled>写入 AJST</button>
            </div>
            <div id="dgzPreviewBox" class="small mt-2" style="max-height:180px;overflow:auto"></div>
          </div>
        </div>
      </div>
    </div>
  `;

  // ─── 事件绑定 ───
  const $ = (id) => document.getElementById(id);
  $('dgzFile').addEventListener('change', (e) => { if (e.target.files[0]) loadImageFile(e.target.files[0]); });
  $('dgzModes').querySelectorAll('button').forEach(b =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));
  $('dgzUndo').addEventListener('click', undo);
  $('dgzClearDs').addEventListener('click', clearCurrentDs);
  $('dgzFit').addEventListener('click', () => { fitView(); redraw(); });
  $('dgzCalibApply').addEventListener('click', applyCalib);
  $('dgzCalibReset').addEventListener('click', resetCalib);
  $('dgzDsAdd').addEventListener('click', () => { addDataset(); renderDsList(); redraw(); });
  $('dgzTol').addEventListener('input', (e) => { $('dgzTolV').textContent = e.target.value; });
  $('dgzAutoMode').addEventListener('change', (e) => {
    const sym = e.target.value === 'symbol';
    $('dgzParamArea').style.display = sym ? '' : 'none';
    $('dgzParamStride').style.display = sym ? 'none' : '';
  });
  $('dgzAutoRun').addEventListener('click', runAutoExtract);
  $('dgzExport').addEventListener('click', exportCsv);
  $('dgzSrcSearch').addEventListener('click', searchSource);
  $('dgzSrcQ').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchSource(); });
  $('dgzSrcSel').addEventListener('change', onSourcePicked);
  $('dgzSrcNewBtn')?.addEventListener('click', () => toggleNewSrcForm());
  $('dgzNewSave')?.addEventListener('click', createNewSource);
  $('dgzNewCancel')?.addEventListener('click', () => toggleNewSrcForm(false));
  $('dgzPreview').addEventListener('click', previewRecords);
  $('dgzWrite').addEventListener('click', writeRecords);
  $('dgzRoiClear').addEventListener('click', () => { _roi = null; updateRoiUI(); redraw(); });
  $('dgzRoiDel').addEventListener('click', deleteRoiPoints);
  $('dgzInterpRun').addEventListener('click', runInterp);
  $('dgzCtrlClear').addEventListener('click', () => { _ctrlPts = []; updateCtrlUI(); redraw(); });
  $('dgzInterpMode').addEventListener('change', (e) => {
    $('dgzSplineOpts').style.display = e.target.value === 'spline' ? '' : 'none';
  });
  $('dgzSplineType').addEventListener('change', (e) => {
    document.querySelectorAll('.dgz-tension-wrap').forEach(el =>
      el.style.display = e.target.value === 'catmull' ? '' : 'none');
  });
  $('dgzTension').addEventListener('input', (e) => {
    $('dgzTensionV').textContent = parseFloat(e.target.value).toFixed(2);
  });
  $('dgzDataType').addEventListener('change', (e) => {
    const spec = e.target.value === 'spec';
    $('dgzLcBlock').style.display = spec ? 'none' : '';
    $('dgzSpecBlock').style.display = spec ? '' : 'none';
    _pendingRecords = null;
    $('dgzWrite').disabled = true;
    $('dgzPreviewBox').innerHTML = '';
    _dsMapSig = '';
    ensureDsMap();
  });

  initCanvas();
  // 会话内再次进入：恢复已有工作状态
  if (_img) { fitView(); redraw(); }
  if (!_datasets.length) addDataset();
  renderDsList();
  updateCalibUI();
  updateRoiUI();
  updateCtrlUI();
  setMode(_mode);
  updateHint();
}

// ═══ 画布 ═══
function initCanvas() {
  _canvas = document.getElementById('dgzCanvas');
  _ctx = _canvas.getContext('2d');
  _canvas.width = _canvas.clientWidth;
  _canvas.height = _canvas.clientHeight;
  _canvas.addEventListener('mousedown', onMouseDown);
  _canvas.addEventListener('mousemove', onMouseMove);
  if (!initCanvas._bound) {   // window 级监听只绑一次（render 重入会重建 canvas 本身）
    window.addEventListener('mouseup', onMouseUp);
    initCanvas._bound = true;
  }
  _canvas.addEventListener('wheel', onWheel, { passive: false });
  _canvas.addEventListener('contextmenu', (e) => e.preventDefault());
}

function fitView() {
  if (!_img || !_canvas) return;
  const cw = _canvas.width, ch = _canvas.height;
  _view.scale = Math.min(cw / _imgData.width, ch / _imgData.height) * 0.95;
  _view.ox = (cw - _imgData.width * _view.scale) / 2;
  _view.oy = (ch - _imgData.height * _view.scale) / 2;
}

function imgFromEvent(e) {
  const r = _canvas.getBoundingClientRect();
  const mx = (e.clientX - r.left) * (_canvas.width / r.width);
  const my = (e.clientY - r.top) * (_canvas.height / r.height);
  return { mx, my, px: (mx - _view.ox) / _view.scale, py: (my - _view.oy) / _view.scale };
}

function redraw() {
  if (!_ctx) return;
  const cw = _canvas.width, ch = _canvas.height;
  _ctx.clearRect(0, 0, cw, ch);
  if (!_img) {
    _ctx.fillStyle = '#888';
    _ctx.font = '14px sans-serif';
    _ctx.fillText('加载图片后开始：标定 → 取点 → 导出/写库', 20, 40);
    return;
  }
  _ctx.imageSmoothingEnabled = _view.scale < 3;
  _ctx.drawImage(_img, _view.ox, _view.oy, _imgData.width * _view.scale, _imgData.height * _view.scale);
  // 标定标记
  _calibPts.forEach((p, i) => {
    const sx = p.px * _view.scale + _view.ox, sy = p.py * _view.scale + _view.oy;
    _ctx.beginPath();
    _ctx.arc(sx, sy, 9, 0, 2 * Math.PI);
    _ctx.fillStyle = 'rgba(88,166,255,0.9)';
    _ctx.fill();
    _ctx.fillStyle = '#fff';
    _ctx.font = 'bold 10px sans-serif';
    _ctx.textAlign = 'center';
    _ctx.textBaseline = 'middle';
    _ctx.fillText(CALIB_LABELS[i], sx, sy);
  });
  // 数据点
  for (const ds of _datasets) {
    if (!ds.visible) continue;
    _ctx.fillStyle = ds.color;
    _ctx.strokeStyle = 'rgba(255,255,255,0.8)';
    _ctx.lineWidth = 1;
    for (const p of ds.points) {
      const sx = p.px * _view.scale + _view.ox, sy = p.py * _view.scale + _view.oy;
      _ctx.beginPath();
      _ctx.arc(sx, sy, 3.5, 0, 2 * Math.PI);
      _ctx.fill();
      _ctx.stroke();
    }
  }
  _ctx.textAlign = 'start';
  _ctx.textBaseline = 'alphabetic';
  // ROI 框选（虚线框；框选模式下显示四角调节柄）
  if (_roi) {
    const rx = Math.min(_roi.x0, _roi.x1), ry = Math.min(_roi.y0, _roi.y1);
    const rw = Math.abs(_roi.x1 - _roi.x0), rh = Math.abs(_roi.y1 - _roi.y0);
    _ctx.save();
    _ctx.strokeStyle = '#f0ad4e';
    _ctx.lineWidth = 1.5;
    _ctx.setLineDash([6, 4]);
    _ctx.strokeRect(rx * _view.scale + _view.ox, ry * _view.scale + _view.oy,
                    rw * _view.scale, rh * _view.scale);
    _ctx.setLineDash([]);
    if (_mode === 'rect') {
      _ctx.fillStyle = '#f0ad4e';
      for (const [cx, cy] of [[rx, ry], [rx + rw, ry], [rx, ry + rh], [rx + rw, ry + rh]]) {
        _ctx.fillRect(cx * _view.scale + _view.ox - 4, cy * _view.scale + _view.oy - 4, 8, 8);
      }
    }
    _ctx.restore();
  }
  // 插值控制点（黄色菱形 + 序号）
  _ctrlPts.forEach((p, i) => {
    const sx = p.px * _view.scale + _view.ox, sy = p.py * _view.scale + _view.oy;
    _ctx.beginPath();
    _ctx.moveTo(sx, sy - 6); _ctx.lineTo(sx + 6, sy); _ctx.lineTo(sx, sy + 6); _ctx.lineTo(sx - 6, sy);
    _ctx.closePath();
    _ctx.fillStyle = '#ffd76a';
    _ctx.fill();
    _ctx.font = '10px sans-serif';
    _ctx.fillText(String(i + 1), sx + 8, sy - 4);
  });
}

// ─── 鼠标交互 ───
function onMouseDown(e) {
  if (!_img) return;
  const { mx, my, px, py } = imgFromEvent(e);
  if (e.button === 2 || e.button === 1) {   // 右键/中键平移
    _drag = { type: 'pan', startMx: mx, startMy: my, ox: _view.ox, oy: _view.oy };
    return;
  }
  if (e.button !== 0) return;
  if (_mode === 'calib') {
    if (_calibPts.length >= 4) { showToast('已有 4 个标定点，可「重新标定」后重打', 'warning'); return; }
    _calibPts.push({ px, py });
    updateCalibUI();
    redraw();
  } else if (_mode === 'manual') {
    const ds = currentDs();
    if (!ds) return;
    const hit = findPointAt(ds, mx, my);
    if (hit >= 0) {
      pushUndo(ds);
      _drag = { type: 'point', ds, index: hit };
    } else {
      if (!_transform) { showToast('请先完成坐标轴标定', 'warning'); return; }
      pushUndo(ds);
      ds.points.push({ px, py });
      renderDsList();
      redraw();
    }
  } else if (_mode === 'delete') {
    const ds = currentDs();
    if (!ds) return;
    const hit = findPointAt(ds, mx, my);
    if (hit >= 0) {
      pushUndo(ds);
      ds.points.splice(hit, 1);
      renderDsList();
      redraw();
    }
  } else if (_mode === 'auto') {
    const lab = avgColorLab(_imgData.pixels, _imgData.width, _imgData.height, px, py);
    if (!lab) return;
    _targetLab = lab;
    const o = (Math.min(_imgData.height - 1, Math.max(0, Math.round(py))) * _imgData.width
      + Math.min(_imgData.width - 1, Math.max(0, Math.round(px)))) * 4;
    const sw = document.getElementById('dgzSwatch');
    sw.style.background = `rgb(${_imgData.pixels[o]},${_imgData.pixels[o + 1]},${_imgData.pixels[o + 2]})`;
    document.getElementById('dgzSwatchInfo').textContent = '已取色（可重复点击修正）';
  } else if (_mode === 'rect') {
    if (_roi) {
      const corner = hitRoiCorner(mx, my);
      if (corner) { _drag = { type: 'roi-corner', corner }; return; }
      if (px >= _roi.x0 && px <= _roi.x1 && py >= _roi.y0 && py <= _roi.y1) {
        _drag = { type: 'roi-move', startPx: px, startPy: py, orig: { ..._roi } };
        return;
      }
    }
    _roi = { x0: px, y0: py, x1: px, y1: py };
    _drag = { type: 'roi-new' };
    redraw();
  } else if (_mode === 'interp') {
    _ctrlPts.push({ px, py });
    updateCtrlUI();
    redraw();
  }
}

function onMouseMove(e) {
  if (!_img || !_canvas) return;
  const { mx, my, px, py } = imgFromEvent(e);
  if (_drag) {
    if (_drag.type === 'pan') {
      _view.ox = _drag.ox + (mx - _drag.startMx);
      _view.oy = _drag.oy + (my - _drag.startMy);
    } else if (_drag.type === 'point') {
      _drag.ds.points[_drag.index] = { px, py };
    } else if (_drag.type === 'roi-new') {
      _roi.x1 = px; _roi.y1 = py;
    } else if (_drag.type === 'roi-move') {
      const dx = px - _drag.startPx, dy = py - _drag.startPy;
      _roi = {
        x0: _drag.orig.x0 + dx, x1: _drag.orig.x1 + dx,
        y0: _drag.orig.y0 + dy, y1: _drag.orig.y1 + dy,
      };
    } else if (_drag.type === 'roi-corner') {
      const c = _drag.corner;
      if (c.includes('w')) _roi.x0 = px; else _roi.x1 = px;
      if (c.includes('n')) _roi.y0 = py; else _roi.y1 = py;
    }
    redraw();
    return;
  }
  // 光标数据坐标
  const cur = document.getElementById('dgzCursor');
  if (cur) {
    if (_transform && px >= 0 && py >= 0 && px <= _imgData.width && py <= _imgData.height) {
      const d = _transform.toData(px, py);
      cur.textContent = `x=${fmtNum(d.x)}  y=${fmtNum(d.y)}`;
    } else {
      cur.textContent = `px=(${px.toFixed(0)}, ${py.toFixed(0)})`;
    }
  }
}

function onMouseUp() {
  if (_drag && _drag.type && _drag.type.startsWith('roi') && _roi) {
    // 规范化（角点可反向拖拽）；过小视为误触取消
    const r = {
      x0: Math.min(_roi.x0, _roi.x1), x1: Math.max(_roi.x0, _roi.x1),
      y0: Math.min(_roi.y0, _roi.y1), y1: Math.max(_roi.y0, _roi.y1),
    };
    _roi = (r.x1 - r.x0 >= 3 && r.y1 - r.y0 >= 3) ? r : null;
    updateRoiUI();
    redraw();
  }
  _drag = null;
}

// ROI 角柄命中检测（屏幕坐标，10px 容差）；返回 'nw'/'ne'/'sw'/'se' 或 null
function hitRoiCorner(mx, my) {
  if (!_roi) return null;
  for (const [name, cx, cy] of [
    ['nw', _roi.x0, _roi.y0], ['ne', _roi.x1, _roi.y0],
    ['sw', _roi.x0, _roi.y1], ['se', _roi.x1, _roi.y1],
  ]) {
    const sx = cx * _view.scale + _view.ox, sy = cy * _view.scale + _view.oy;
    if (Math.abs(sx - mx) <= 10 && Math.abs(sy - my) <= 10) return name;
  }
  return null;
}

function updateRoiUI() {
  const info = document.getElementById('dgzRoiInfo');
  const btnClear = document.getElementById('dgzRoiClear');
  const btnDel = document.getElementById('dgzRoiDel');
  if (!info) return;
  if (_roi) {
    info.textContent = `已框选：x∈[${_roi.x0.toFixed(0)}, ${_roi.x1.toFixed(0)}]，y∈[${_roi.y0.toFixed(0)}, ${_roi.y1.toFixed(0)}]（图像像素）；自动提取仅作用于框内`;
    btnClear.disabled = false;
    btnDel.disabled = false;
  } else {
    info.textContent = '未框选：自动提取作用于全图';
    btnClear.disabled = true;
    btnDel.disabled = true;
  }
}

function updateCtrlUI() {
  const el = document.getElementById('dgzCtrlInfo');
  if (el) el.textContent = `在「插值取点」模式点击图上放置控制点（当前 ${_ctrlPts.length} 个）`;
}

// 删除当前数据集在 ROI 内的点
function deleteRoiPoints() {
  const ds = currentDs();
  if (!ds || !_roi) return;
  const inside = ds.points.filter(p =>
    p.px >= _roi.x0 && p.px <= _roi.x1 && p.py >= _roi.y0 && p.py <= _roi.y1);
  if (!inside.length) { showToast('框内没有当前数据集的点', 'info'); return; }
  if (!confirm(`删除「${ds.name}」在框选范围内的 ${inside.length} 个点？`)) return;
  pushUndo(ds);
  ds.points = ds.points.filter(p =>
    !(p.px >= _roi.x0 && p.px <= _roi.x1 && p.py >= _roi.y0 && p.py <= _roi.y1));
  renderDsList();
  redraw();
  showToast(`已删除 ${inside.length} 个点`, 'success');
}

// 直线/样条生成取点
function runInterp() {
  if (!_img) { showToast('请先加载图片', 'warning'); return; }
  if (!_transform) { showToast('请先完成坐标轴标定', 'warning'); return; }
  const ds = currentDs();
  if (!ds) return;
  const mode = document.getElementById('dgzInterpMode').value;
  const step = parseFloat(document.getElementById('dgzInterpStep').value) || 5;
  let pts;
  try {
    if (mode === 'line') {
      if (_ctrlPts.length !== 2) { showToast(`直线模式需要恰好 2 个端点（当前 ${_ctrlPts.length} 个）`, 'warning'); return; }
      pts = sampleLine(_ctrlPts[0], _ctrlPts[1], step);
    } else {
      if (_ctrlPts.length < 3) { showToast(`样条模式需要至少 3 个控制点（当前 ${_ctrlPts.length} 个）`, 'warning'); return; }
      pts = sampleSpline(_ctrlPts, step, {
        method: document.getElementById('dgzSplineType').value,
        tension: parseFloat(document.getElementById('dgzTension').value),
      });
    }
  } catch (err) {
    showToast(`生成失败: ${err.message}`, 'danger');
    return;
  }
  if (!pts.length) { showToast('未生成点', 'warning'); return; }
  pushUndo(ds);
  ds.points.push(...pts);
  renderDsList();
  redraw();
  showToast(`已生成 ${pts.length} 个点（加入「${ds.name}」）`, 'success');
}

function onWheel(e) {
  if (!_img) return;
  e.preventDefault();
  const { mx, my } = imgFromEvent(e);
  const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  const newScale = Math.min(50, Math.max(0.01, _view.scale * factor));
  _view.ox = mx - (mx - _view.ox) * (newScale / _view.scale);
  _view.oy = my - (my - _view.oy) * (newScale / _view.scale);
  _view.scale = newScale;
  redraw();
}

function findPointAt(ds, mx, my) {
  const tol = 8;  // 屏幕像素
  for (let i = ds.points.length - 1; i >= 0; i--) {
    const p = ds.points[i];
    const sx = p.px * _view.scale + _view.ox, sy = p.py * _view.scale + _view.oy;
    if (Math.abs(sx - mx) <= tol && Math.abs(sy - my) <= tol) return i;
  }
  return -1;
}

function fmtNum(v) {
  if (v == null || !isFinite(v)) return '?';
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.01 || a >= 1e5)) return v.toExponential(3);
  return String(parseFloat(v.toPrecision(5)));
}

// ─── 图片加载 ───
function loadImageFile(file) {
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.onload = () => {
    const off = document.createElement('canvas');
    off.width = img.naturalWidth;
    off.height = img.naturalHeight;
    const octx = off.getContext('2d', { willReadFrequently: true });
    octx.drawImage(img, 0, 0);
    _img = img;
    _imgData = {
      pixels: octx.getImageData(0, 0, off.width, off.height).data,
      width: off.width,
      height: off.height,
    };
    // 新图重置标定与数据点
    _calibPts = [];
    _transform = null;
    _targetLab = null;
    _roi = null;
    _ctrlPts = [];
    _undoStack = [];
    _datasets = [];
    _dsSeq = 0;
    addDataset();
    URL.revokeObjectURL(url);
    fitView();
    renderDsList();
    updateCalibUI();
    updateRoiUI();
    updateCtrlUI();
    setMode('calib');
    redraw();
    showToast(`已加载 ${file.name}（${off.width}×${off.height}）`, 'success');
  };
  img.onerror = () => showToast('图片加载失败', 'danger');
  img.src = url;
}

// ─── 模式 ───
function setMode(m) {
  _mode = m;
  document.querySelectorAll('#dgzModes button').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === m);
  });
  updateHint();
}

function updateHint() {
  const el = document.getElementById('dgzHint');
  if (!el) return;
  const hints = {
    calib: _calibPts.length < 4
      ? `标定模式：点击图上 ${CALIB_LABELS[_calibPts.length]} 参考点（顺序 X① → X② → Y① → Y②），并在右侧填入对应数值`
      : '4 个标定点已齐，在右侧填入数值后点「应用标定」',
    manual: '手动取点：单击加点，拖拽移动已有点（右键拖拽平移，滚轮缩放）',
    auto: '自动取色：点击图中目标曲线/符号取色，然后点「执行提取」',
    rect: '框选范围：拖拽绘制矩形；拖四角柄调整大小，拖框内移动位置',
    interp: '插值取点：点击图上放置控制点（直线 2 个 / 样条 ≥3 个），然后点「生成点」',
    delete: '删除模式：单击当前数据集的点删除',
  };
  el.textContent = _img ? hints[_mode] : '先加载图片';
}

// ─── 标定 ───
function updateCalibUI() {
  const st = document.getElementById('dgzCalibStatus');
  if (st) st.innerHTML = _transform
    ? '<span style="color:var(--accent-green)">已标定 ✓</span>'
    : `<span class="text-secondary">未标定（${_calibPts.length}/4 点）</span>`;
  updateHint();
}

function applyCalib() {
  const $ = (id) => document.getElementById(id);
  if (_calibPts.length < 4) { showToast('请先在图上打齐 4 个标定点', 'warning'); return; }
  const v = (id) => {
    const s = $(id).value.trim();
    return s === '' ? null : parseFloat(s);
  };
  try {
    _transform = makeCalibTransform({
      px1: _calibPts[0].px, px2: _calibPts[1].px, x1: v('dgzX1v'), x2: v('dgzX2v'),
      xLog: $('dgzXLog').checked,
      py1: _calibPts[2].py, py2: _calibPts[3].py, y1: v('dgzY1v'), y2: v('dgzY2v'),
      yLog: $('dgzYLog').checked,
    });
  } catch (err) {
    showToast(`标定失败: ${err.message}`, 'danger');
    return;
  }
  updateCalibUI();
  showToast('标定已应用', 'success');
}

function resetCalib() {
  _calibPts = [];
  _transform = null;
  updateCalibUI();
  setMode('calib');
  redraw();
}

// ─── 数据集 ───
function addDataset() {
  const ds = {
    id: ++_dsSeq,
    name: `数据集 ${_dsSeq}`,
    color: DS_COLORS[(_dsSeq - 1) % DS_COLORS.length],
    visible: true,
    points: [],
  };
  _datasets.push(ds);
  _curDsId = ds.id;
}

function currentDs() {
  return _datasets.find(d => d.id === _curDsId) || null;
}

function renderDsList() {
  const box = document.getElementById('dgzDsList');
  if (!box) return;
  box.innerHTML = _datasets.map(ds => `
    <div class="d-flex align-items-center gap-1 px-1 py-1 dgz-ds-row ${ds.id === _curDsId ? 'dgz-ds-active' : ''}"
         data-id="${ds.id}" style="cursor:pointer;border-radius:4px">
      <input type="checkbox" class="form-check-input dgz-ds-vis" data-id="${ds.id}" ${ds.visible ? 'checked' : ''} title="显示/隐藏" onclick="event.stopPropagation()">
      <span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:${ds.color}"></span>
      <input class="form-control form-control-sm dgz-ds-name" data-id="${ds.id}" value="${esc(ds.name)}" style="width:90px" onclick="event.stopPropagation()">
      <span class="small text-secondary">${ds.points.length} 点</span>
      <button class="btn btn-sm btn-outline-danger py-0 px-1 ms-auto dgz-ds-del" data-id="${ds.id}" title="删除该数据集" onclick="event.stopPropagation()"><i class="bi bi-x"></i></button>
    </div>`).join('');
  box.querySelectorAll('.dgz-ds-row').forEach(row => {
    row.addEventListener('click', () => {
      _curDsId = parseInt(row.dataset.id);
      renderDsList();
      redraw();
    });
  });
  box.querySelectorAll('.dgz-ds-vis').forEach(cb => cb.addEventListener('change', () => {
    const ds = _datasets.find(d => d.id === parseInt(cb.dataset.id));
    if (ds) { ds.visible = cb.checked; redraw(); }
  }));
  box.querySelectorAll('.dgz-ds-name').forEach(inp => inp.addEventListener('change', () => {
    const ds = _datasets.find(d => d.id === parseInt(inp.dataset.id));
    if (ds) ds.name = inp.value.trim() || ds.name;
  }));
  box.querySelectorAll('.dgz-ds-del').forEach(btn => btn.addEventListener('click', () => {
    const id = parseInt(btn.dataset.id);
    const ds = _datasets.find(d => d.id === id);
    if (!ds) return;
    if (ds.points.length && !confirm(`删除「${ds.name}」（${ds.points.length} 点）？`)) return;
    _datasets = _datasets.filter(d => d.id !== id);
    if (_curDsId === id) _curDsId = _datasets.length ? _datasets[0].id : null;
    if (!_datasets.length) addDataset();
    renderDsList();
    redraw();
  }));
  ensureDsMap();
}

// ─── 撤销 / 清空 ───
function pushUndo(ds) {
  _undoStack.push({ dsId: ds.id, points: ds.points.map(p => ({ ...p })) });
  if (_undoStack.length > 20) _undoStack.shift();
  _pendingRecords = null;
  const w = document.getElementById('dgzWrite');
  if (w) w.disabled = true;
}

function undo() {
  const snap = _undoStack.pop();
  if (!snap) { showToast('没有可撤销的操作', 'info'); return; }
  const ds = _datasets.find(d => d.id === snap.dsId);
  if (!ds) { showToast('对应数据集已删除', 'warning'); return; }
  ds.points = snap.points;
  renderDsList();
  redraw();
}

function clearCurrentDs() {
  const ds = currentDs();
  if (!ds || !ds.points.length) return;
  if (!confirm(`清空「${ds.name}」的 ${ds.points.length} 个点？`)) return;
  pushUndo(ds);
  ds.points = [];
  renderDsList();
  redraw();
}

// ─── 自动提取 ───
function runAutoExtract() {
  if (!_img) { showToast('请先加载图片', 'warning'); return; }
  if (!_transform) { showToast('请先完成坐标轴标定', 'warning'); return; }
  if (!_targetLab) { showToast('请先在「自动取色」模式下点击图像取色', 'warning'); return; }
  const ds = currentDs();
  if (!ds) return;
  const $ = (id) => document.getElementById(id);
  const tol = parseFloat($('dgzTol').value);
  const mode = $('dgzAutoMode').value;
  const mask = buildMask(_imgData.pixels, _imgData.width, _imgData.height, _targetLab, tol);
  // ROI：掩膜裁掉框外像素，描线范围收窄到框内
  let x0 = 0, x1 = _imgData.width - 1;
  if (_roi) {
    const W = _imgData.width, H = _imgData.height;
    const rx0 = Math.max(0, Math.floor(Math.min(_roi.x0, _roi.x1)));
    const rx1 = Math.min(W - 1, Math.ceil(Math.max(_roi.x0, _roi.x1)));
    const ry0 = Math.max(0, Math.floor(Math.min(_roi.y0, _roi.y1)));
    const ry1 = Math.min(H - 1, Math.ceil(Math.max(_roi.y0, _roi.y1)));
    for (let y = 0; y < H; y++) {
      if (y >= ry0 && y <= ry1) {
        mask.fill(0, y * W, y * W + rx0);
        mask.fill(0, y * W + rx1 + 1, (y + 1) * W);
      } else {
        mask.fill(0, y * W, (y + 1) * W);
      }
    }
    x0 = rx0; x1 = rx1;
  }
  let pts;
  if (mode === 'line') {
    const stride = Math.max(1, parseInt($('dgzStride').value) || 1);
    pts = traceLine(mask, _imgData.width, _imgData.height, { stride, x0, x1 });
  } else {
    const minA = parseInt($('dgzMinArea').value) || 5;
    const maxA = parseInt($('dgzMaxArea').value) || 500;
    pts = detectSymbols(mask, _imgData.width, _imgData.height, { minArea: minA, maxArea: maxA });
  }
  if (!pts.length) { showToast('未提取到点：可调大容差或检查目标色', 'warning'); return; }
  pushUndo(ds);
  ds.points.push(...pts.map(p => ({ px: p.px, py: p.py })));
  renderDsList();
  redraw();
  showToast(`提取到 ${pts.length} 个点（已加入「${ds.name}」，可用删除模式修整）`, 'success');
}

// ─── 导出 CSV ───
function exportCsv() {
  if (!_transform) { showToast('请先完成坐标轴标定', 'warning'); return; }
  const rows = ['dataset,x,y'];
  for (const ds of _datasets) {
    for (const p of ds.points) {
      const d = _transform.toData(p.px, p.py);
      rows.push(`${ds.name},${d.x.toPrecision(10)},${d.y.toPrecision(10)}`);
    }
  }
  if (rows.length === 1) { showToast('没有数据点', 'warning'); return; }
  const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'digitizer_export.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ─── 写入 AJST ───
async function searchSource() {
  const q = document.getElementById('dgzSrcQ').value.trim();
  if (!q) return;
  try {
    const resp = await getTransients({ search: q, per_page: 20 });
    const sel = document.getElementById('dgzSrcSel');
    if (!resp.items.length) { showToast(`未找到匹配「${q}」的源`, 'warning'); return; }
    sel.innerHTML = '<option value="">-- 选择源 --</option>' + resp.items.map(t =>
      `<option value="${esc(t.id)}">${esc(t.id)}${t.aliases && t.aliases.length ? '（' + esc(t.aliases.join(', ')) + '）' : ''}</option>`).join('');
    sel.style.display = '';
    sel.dataset.items = JSON.stringify(resp.items.map(t => ({ id: t.id, t0: t.t0 })));
    if (resp.items.length === 1) { sel.value = resp.items[0].id; onSourcePicked(); }
  } catch (err) {
    showToast(`搜索失败: ${err.message}`, 'danger');
  }
}

function onSourcePicked() {
  const sel = document.getElementById('dgzSrcSel');
  const info = document.getElementById('dgzSrcInfo');
  const items = JSON.parse(sel.dataset.items || '[]');
  const hit = items.find(t => t.id === sel.value);
  if (!hit) { _src = null; info.textContent = '未选择源'; return; }
  _src = hit;
  info.innerHTML = `目标源：<strong>${esc(hit.id)}</strong> ｜ t0 = ${hit.t0 ? esc(hit.t0) + ' UTC' : '<span class="text-danger">无 t0（MJD 轴不可用）</span>'}`;
}

// 将源设为当前写库目标（等同搜索后在下拉中选中）
function pickSource(item) {  // {id, t0}
  const sel = document.getElementById('dgzSrcSel');
  sel.dataset.items = JSON.stringify([item]);
  sel.innerHTML = `<option value="${esc(item.id)}">${esc(item.id)}</option>`;
  sel.value = item.id;
  sel.style.display = '';
  onSourcePicked();
}

// 新建源内联表单展开/收起
function toggleNewSrcForm(show) {
  const f = document.getElementById('dgzSrcNewForm');
  if (!f) return;
  f.style.display = (show === undefined) ? (f.style.display === 'none' ? '' : 'none') : (show ? '' : 'none');
}

// 提交新建源：成功后直接设为当前目标源；409（ID 已存在）时提示并尝试直接选中
async function createNewSource() {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  const v = (id) => document.getElementById(id).value.trim();
  const id = v('dgzNewId');
  if (!id) { showToast('请填写源 ID', 'warning'); return; }
  const payload = { id };
  const raS = v('dgzNewRa'), decS = v('dgzNewDec');
  if (raS) {
    const ra = parseRA(raS);
    if (ra == null || Number.isNaN(ra)) { showToast('RA 无法解析（支持十进制度或时分秒，如 08h08m27.4s）', 'warning'); return; }
    payload.ra = ra;
  }
  if (decS) {
    const dec = parseDec(decS);
    if (dec == null || Number.isNaN(dec)) { showToast('Dec 无法解析（支持十进制度或时分秒，如 +40d36m44.8s）', 'warning'); return; }
    payload.dec = dec;
  }
  const t0 = v('dgzNewT0');
  if (t0) payload.t0 = t0;
  const zS = v('dgzNewZ');
  if (zS) {
    const z = parseFloat(zS);
    if (!isFinite(z)) { showToast('红移格式无效', 'warning'); return; }
    payload.redshift = z;
  }
  const list = (s) => s.split(',').map(x => x.trim()).filter(Boolean);
  if (v('dgzNewAliases')) payload.aliases = list(v('dgzNewAliases'));
  if (v('dgzNewTags')) payload.tags = list(v('dgzNewTags'));
  try {
    const t = await createTransient(payload);
    toggleNewSrcForm(false);
    pickSource({ id: t.id, t0: t.t0 });
    showToast(`已创建源 ${t.id} 并设为当前目标源`, 'success');
  } catch (err) {
    if (/already exists/i.test(err.message)) {
      try {
        const resp = await getTransients({ search: id, per_page: 20 });
        const hit = (resp.items || []).find(x => x.id === id);
        if (hit) {
          toggleNewSrcForm(false);
          pickSource({ id: hit.id, t0: hit.t0 });
          showToast(`源 ${id} 已存在，已直接选中`, 'info');
          return;
        }
      } catch {}
      showToast(`源 ${id} 已存在`, 'warning');
      return;
    }
    showToast(`创建失败: ${err.message}`, 'danger');
  }
}

// 生成待入库记录（同时供预览与写入）
function buildRecords() {
  const $ = (id) => document.getElementById(id);
  if (!_transform) return { err: '请先完成坐标轴标定' };
  if (!_src) return { err: '请先搜索并选择目标源' };
  const xfmt = $('dgzXFmt').value;
  let t0ms = null;
  if (xfmt === 'mjd') {
    if (!_src.t0) return { err: `源 ${_src.id} 没有 t0，MJD 轴无法换算` };
    t0ms = Date.parse(_src.t0.endsWith('Z') ? _src.t0 : _src.t0 + 'Z');  // 库内时间为 naive UTC
  }
  const ref = $('dgzRef').value.trim() || null;
  const tel = $('dgzTel').value.trim() || null;
  const instr = $('dgzInstr').value.trim() || null;
  const comment = $('dgzComment').value.trim() || 'Digitizer';  // 默认标记来源
  const records = [];
  const perDs = [];
  for (const ds of _datasets) {
    if (!ds.points.length) continue;
    const mapRow = document.querySelector(`#dgzDsMap .dgz-map[data-id="${ds.id}"]`);
    const band = mapRow ? mapRow.querySelector('.dgz-map-band').value.trim() : '';
    if (!band) return { err: `数据集「${ds.name}」未填写 band` };
    const ytype = mapRow.querySelector('.dgz-map-ytype').value;
    const ul = mapRow.querySelector('.dgz-map-ul').checked;
    const isMag = ytype.startsWith('mag');
    const fluxUnit = isMag ? 'magnitude' : ytype;
    const magSys = ytype === 'mag_AB' ? 'AB' : (ytype === 'mag_Vega' ? 'Vega' : null);
    const recs = ds.points.map(p => {
      const d = _transform.toData(p.px, p.py);
      const time = xfmt === 'mjd'
        ? (d.x * 86400000 - MJD_EPOCH_MS - t0ms) / 1000
        : d.x * TIME_FACTOR[xfmt];
      return {
        transient_id: _src.id,
        time, band,
        flux_density: d.y, flux_density_unit: fluxUnit, mag_system: magSys,
        upperlimit: ul,
        reference: ref, telescope: tel, instrument: instr, comment,
        extra_data: { digitizer: true },
      };
    });
    records.push(...recs);
    perDs.push({ name: ds.name, band, fluxUnit, n: recs.length, sample: recs.slice(0, 5) });
  }
  if (!records.length) return { err: '没有可写入的数据点' };
  return { records, perDs };
}

// 数据集 → 写库映射行（数据集集合或数据类型变化时才重建，保留已填内容）
let _dsMapSig = '';
function ensureDsMap() {
  const type = document.getElementById('dgzDataType')?.value || 'lc';
  const sig = type + ':' + _datasets.filter(d => d.points.length).map(d => d.id).join(',');
  if (sig !== _dsMapSig) renderDsMap();
}

function renderDsMap() {
  const type = document.getElementById('dgzDataType')?.value || 'lc';
  const box = document.getElementById(type === 'spec' ? 'dgzDsMapSpec' : 'dgzDsMap');
  if (!box) return;
  const withPts = _datasets.filter(d => d.points.length);
  _dsMapSig = type + ':' + withPts.map(d => d.id).join(',');
  if (!withPts.length) {
    box.innerHTML = '<div class="small text-secondary mb-1">（有数据点后在此为每个数据集指定映射）</div>';
    return;
  }
  if (type === 'spec') {
    // 光谱：每个数据集 = 一条光谱，指定文件名（源内唯一）
    box.innerHTML = withPts.map(ds => `
      <div class="d-flex gap-1 align-items-center mb-1 dgz-map" data-id="${ds.id}">
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${ds.color}"></span>
        <span class="small text-truncate" style="max-width:70px" title="${esc(ds.name)}">${esc(ds.name)}</span>
        <input class="form-control form-control-sm dgz-map-fname" value="digitizer_ds${ds.id}" title="光谱文件名（字母数字._-）" style="width:130px">
        <span class="small text-secondary">${ds.points.length} 点</span>
      </div>`).join('');
    return;
  }
  box.innerHTML = withPts.map(ds => `
    <div class="d-flex gap-1 align-items-center mb-1 dgz-map" data-id="${ds.id}">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${ds.color}"></span>
      <span class="small text-truncate" style="max-width:70px" title="${esc(ds.name)}">${esc(ds.name)}</span>
      <input class="form-control form-control-sm dgz-map-band" placeholder="band*" style="width:70px">
      <select class="form-select form-select-sm dgz-map-ytype" style="width:auto">
        <option value="mag_AB" selected>星等 AB</option>
        <option value="mag_Vega">星等 Vega</option>
        <option value="mJy">mJy</option>
        <option value="uJy">uJy</option>
        <option value="Jy">Jy</option>
        <option value="cgs(erg/cm2/s/Hz)">cgs</option>
      </select>
      <div class="form-check mb-0" title="这些点是否为上限">
        <input class="form-check-input dgz-map-ul" type="checkbox"><label class="form-check-label small">上限</label>
      </div>
    </div>`).join('');
}

// 光谱：每个数据集生成一个上传负载（两列文本，Å，按波长排序）
function buildSpectraPayloads() {
  const $ = (id) => document.getElementById(id);
  if (!_transform) return { err: '请先完成坐标轴标定' };
  if (!_src) return { err: '请先搜索并选择目标源' };
  const factor = parseFloat($('dgzSpecXUnit').value);   // → Å
  const fluxTypeRaw = $('dgzSpecFluxType').value;
  const isAbMag = fluxTypeRaw === 'abmag';
  const isStMag = fluxTypeRaw === 'stmag';
  const fluxType = (isAbMag || isStMag) ? 'absolute' : fluxTypeRaw;  // 星等换算后以绝对流量入库
  const convNote = isAbMag ? 'AB 星等→绝对流量已逐点换算' : (isStMag ? 'ST 星等→绝对流量已逐点换算' : '');
  const instrument = $('dgzSpecInstr').value.trim() || null;
  const mjdRaw = $('dgzSpecMjd').value.trim();
  if (mjdRaw && !isFinite(parseFloat(mjdRaw))) return { err: 'MJD 必须是数值或留空' };
  const observer = $('dgzSpecObserver').value.trim() || null;
  const reducer = $('dgzSpecReducer').value.trim() || null;
  const payloads = [];
  const perDs = [];
  for (const ds of _datasets.filter(d => d.points.length)) {
    // 限定在光谱映射容器内查询（光变映射行也用 .dgz-map，DOM 序靠前会误命中）
    const row = document.querySelector(`#dgzDsMapSpec .dgz-map[data-id="${ds.id}"]`);
    const fname = (row ? row.querySelector('.dgz-map-fname').value.trim() : '') || `digitizer_ds${ds.id}`;
    if (!/^[A-Za-z0-9._-]+$/.test(fname)) return { err: `数据集「${ds.name}」文件名只能含字母、数字、. _ -` };
    const pts = ds.points
      .map(p => _transform.toData(p.px, p.py))
      .map(d => {
        const wl = d.x * factor;
        let flux = d.y;
        if (isAbMag) flux = abMagToFlambda(d.y, wl);
        else if (isStMag) flux = stMagToFlambda(d.y);
        return { wl, flux };
      })
      .sort((a, b) => a.wl - b.wl);
    if (pts.length < 10) return { err: `数据集「${ds.name}」只有 ${pts.length} 点，光谱至少需要 10 点` };
    const bad = pts.find(p => !(p.wl > 100 && p.wl < 1e7));
    if (bad) return { err: `数据集「${ds.name}」波长 ${fmtNum(bad.wl)} Å 超出 100–1e7 Å 合理范围` };
    const content = pts.map(p => `${p.wl.toPrecision(10)} ${p.flux.toPrecision(10)}`).join('\n');
    payloads.push({
      transient_id: _src.id, filename: fname, content,
      instrument, mjd: mjdRaw || null, observer, reducer, flux_type: fluxType,
    });
    perDs.push({ name: ds.name, fname, n: pts.length, wlMin: pts[0].wl, wlMax: pts[pts.length - 1].wl,
                 sample: pts.slice(0, 5), convNote });
  }
  if (!payloads.length) return { err: '没有可写入的数据点' };
  return { payloads, perDs };
}

function previewRecords() {
  ensureDsMap();
  const box = document.getElementById('dgzPreviewBox');
  const type = document.getElementById('dgzDataType').value;
  const fail = (err) => {
    box.innerHTML = `<span class="text-danger">${esc(err)}</span>`;
    _pendingRecords = null;
    document.getElementById('dgzWrite').disabled = true;
  };
  if (type === 'spec') {
    const { err, payloads, perDs } = buildSpectraPayloads();
    if (err) { fail(err); return; }
    box.innerHTML = perDs.map(d => `
      <div class="mb-1"><strong>${esc(d.name)}</strong> → ${esc(d.fname)}，${d.n} 点，
        λ ∈ [${fmtNum(d.wlMin)}, ${fmtNum(d.wlMax)}] Å${d.convNote ? `（${d.convNote}）` : ''}
        <table class="table table-sm mb-0" style="font-size:0.75rem">
          <thead><tr><th>波长 (Å)</th><th>流量</th></tr></thead>
          <tbody>${d.sample.map(p => `<tr><td>${fmtNum(p.wl)}</td><td>${fmtNum(p.flux)}</td></tr>`).join('')}</tbody>
        </table>
      </div>`).join('') + `<div class="fw-bold">共 ${payloads.length} 条光谱待写入 ${_src.id}</div>`;
    _pendingRecords = { type, payloads };
    document.getElementById('dgzWrite').disabled = false;
    return;
  }
  const { err, records, perDs } = buildRecords();
  if (err) { fail(err); return; }
  box.innerHTML = perDs.map(d => `
    <div class="mb-1"><strong>${esc(d.name)}</strong> → band=${esc(d.band)}, ${d.n} 点（${esc(d.fluxUnit)}）
      <table class="table table-sm mb-0" style="font-size:0.75rem">
        <thead><tr><th>time (s)</th><th>flux_density</th></tr></thead>
        <tbody>${d.sample.map(r => `<tr><td>${fmtNum(r.time)}</td><td>${fmtNum(r.flux_density)}</td></tr>`).join('')}</tbody>
      </table>
    </div>`).join('') + `<div class="fw-bold">共 ${records.length} 条待写入 ${_src.id}</div>`;
  _pendingRecords = { type, records };
  document.getElementById('dgzWrite').disabled = false;
}

async function writeRecords() {
  if (!isAuthed()) { showToast('请先登录', 'warning'); return; }
  if (!_pendingRecords) { showToast('请先「预览入库数据」', 'warning'); return; }
  const { type, records, payloads } = _pendingRecords;
  const btn = document.getElementById('dgzWrite');
  if (type === 'spec') {
    if (!confirm(`将向 ${_src.id} 写入 ${payloads.length} 条光谱，确认？`)) return;
    btn.disabled = true;
    let ok = 0;
    try {
      for (const p of payloads) {
        await uploadSpectrum(p);
        ok++;
      }
      showToast(`已写入 ${ok} 条光谱到 ${_src.id}，可到详情页「光谱数据」标签核对`, 'success');
      _pendingRecords = null;
    } catch (err) {
      showToast(`光谱写入失败（已成功 ${ok} 条）: ${err.message}`, 'danger');
      btn.disabled = false;
    }
    return;
  }
  if (!confirm(`将向 ${_src.id} 写入 ${records.length} 条测光记录，确认？`)) return;
  btn.disabled = true;
  let created = 0;
  try {
    const CHUNK = 500;
    for (let i = 0; i < records.length; i += CHUNK) {
      const resp = await createLightcurves(records.slice(i, i + CHUNK));
      created += resp.created;
    }
    showToast(`已写入 ${created} 条测光记录到 ${_src.id}，可到详情页核对`, 'success');
    _pendingRecords = null;
  } catch (err) {
    showToast(`写入失败（已写入 ${created} 条）: ${err.message}`, 'danger');
    btn.disabled = false;
  }
}
