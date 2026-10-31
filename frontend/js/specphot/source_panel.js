// === specphot 来源条（常驻输入面，IA golden path 第一步 / F-0i7、F-74、F-77） ===
// 三来源：库内谱（U-34，R-32 宿主 GET /api/spectra，全库一次 GET）/ 上传文件 / 粘贴文本
// （U-35 → API-7 parse 预检，只解析不落盘，F-0i6）。元数据四项 ra/dec/z/mjd（U-36/37/38）
// + 波长框架显示（U-39，F-79）+ 银河消光（U-33 → API-8 ebv）。
import { api } from '../api.js';
import { esc, escAttr } from '../utils.js';
import { parseCoord, attachCoordHint } from '../coords.js';

const C_MAX_UPLOAD_BYTES = 2_000_000;   // F-76（与后端 constants.py 同值）
// W-28③：meta_provenance 值 → 中文标签（API-1/API-7 回传键；file = 谱文件头，按 F-74 归「回填自库内记录」一档）
const PROV_LABEL = { source: '源表', file: '源表', user: '已手改', default: '默认' };
const TXT = {
  t18: '本次上传只在本页内存中参与计算，未写入光谱库、未落盘；要入库请改用「光谱数据」页的上传对话框。', // TXT-18
  t19: 'E(B−V) 按你填写的坐标现算（宿主 CSFD 图，R_V=3.1），不是库内该源的记录值；改坐标即重算。图不可用时按「未改正」出数并附告警。', // TXT-19
  t21: '上传谱的波长未带框架标记，本模块按真空处理；若实际是空气波长，窄带偏移即达 0.92–2.20 Å（≈82–87 km/s），会同时污染通带边界与线位。库内谱相反：它已由宿主换到真空，但 wavelength_type 留空时「真空」是推定值。', // TXT-21
};

let _el = null, _ctx = null;

// ─── 库内谱清单（U-34：无 tid 时不带过滤参数 ⇒ 一次 GET 列出全库，禁分页新建端点） ───
// 谱数上百后平铺下拉无法翻找 ⇒ 可输入筛选（input + datalist，与标签/波段输入同一 idioms）；
// 清单按页面挂载缓存（renderSourcePanel 进页时作废重取），渲染轮次不再每轮重 GET。
let _specItems = null;      // [{id, label}]
function specLabel(s) {
  const date = s.observation_date ? String(s.observation_date).slice(0, 10) : '';
  return [`#${s.id}`, s.transient_id || '?', s.instrument || s.filename || '', date]
    .filter(Boolean).join(' · ');
}
async function ensureSpecItems() {
  if (!_specItems) {
    const list = await api('GET', '/spectra');
    _specItems = (list || []).map(s => ({ id: s.id, label: specLabel(s) }));
  }
  return _specItems;
}
// 填充 datalist 候选 + 回填当前选中谱的标签（renderPanel 重画后 input 是空的）
function fillSpecCandidates() {
  const input = _el.querySelector('.sp-selspec');
  const dl = _el.querySelector('#spSpecList');
  if (!input || !dl) return;
  ensureSpecItems().then(items => {
    dl.innerHTML = items.map(x => `<option value="${escAttr(x.label)}"></option>`).join('');
    const cur = items.find(x => String(x.id) === String(_ctx.S.spectrumId));
    if (cur && !input.value) input.value = cur.label;
  }).catch(e => {
    const hint = _el.querySelector('.sp-selhint');
    if (hint) { hint.hidden = false; hint.textContent = `谱清单加载失败：${e.message}`; }
  });
}

// ─── 上传/粘贴 → API-7 解析（只解析不落盘；成功 adopt，失败内联报错且不清空已填参数） ───
async function parseUpload(payload, hint, kind, nbytes) {
  if (nbytes > C_MAX_UPLOAD_BYTES) { _ctx.S.parseError = `超过单件体积上限 ${C_MAX_UPLOAD_BYTES} 字节`; renderPanel(); return; }
  _ctx.S.parseError = null;
  renderPanel();
  try {
    const parsed = await _ctx.spPost('/specphot/parse',
      Object.assign({ format_hint: hint, source: kind }, payload), null);
    _ctx.adoptUpload(parsed, kind, nbytes);
  } catch (e) {
    const S = _ctx.S;
    S.upload = null;
    S.parseError = e.message + (e.payload && e.payload.first_bad_line
      ? `（第一处不合格行：${e.payload.first_bad_line}）` : '');
  }
  renderPanel(); _ctx.refreshChrome();
}

async function parseText(text, kind) {
  if (!text || !text.trim()) { _ctx.S.parseError = '内容为空：没有数据行'; renderPanel(); return; }
  await parseUpload({ text }, 'txt', kind, text.length);
}

// ─── 渲染（整条重画；状态全在 S） ───
export function renderSourcePanel(el, ctx) {
  _el = el; _ctx = ctx;
  _specItems = null;   // 进页重取清单（它页新上传的谱可见）
  renderPanel();
}

function renderPanel() {
  if (!_el) return;
  const S = _ctx.S, ctx = _ctx;
  const kind = S.sourceKind;
  const up = S.upload;
  const prov = S.spectrumInfo || null;
  const gext = prov && prov.meta && prov.meta.already_gext_corrected;
  const parentId = prov && prov.meta ? prov.meta.parent_id : null;   // W-22：改正二级谱的父谱
  const frame = (up && up.lambda_frame) || (prov && prov.meta && prov.meta.lambda_frame) || (kind === 'catalog' ? 'vacuum' : null);
  // W-30①：框架「默认 vacuum（未声明）」判定——库内谱恒为宿主推定真空；上传/粘贴件以用户是否
  // 显式声明过为准（声明后不挂）。命中 ⇒ TXT-21 全文常驻（非 hover title，CA-34）。
  const declared = (kind === 'upload' || kind === 'paste') && S.uploadFrameDeclared;
  const frameDefaultVacuum = !declared && (frame == null || frame === 'vacuum');
  // W-28③：元数据四项的 meta_provenance（API-1 spectrum.meta_provenance / API-7 parse 回传）；
  // 用户手改项在前端侧覆盖为 user（F-74：后端 provenance 只描述装载时来源）。
  const mp = (up && up.meta_provenance) || (prov && prov.meta_provenance) || {};
  const provTag = (key) => {
    const v = S.metaUserEdits && S.metaUserEdits[key] ? 'user' : (mp[key] || '');
    if (!v) return '';
    const label = PROV_LABEL[v] || esc(v);
    return `<span class="sp-prov" title="meta_provenance=${escAttr(v)}">${label}</span>`;
  };
  const ebvTxt = S.ebv.value != null
    ? `E(B−V)=${S.ebv.value}（R_V=${S.ebv.rv}，${S.ebv.law}）` : '未查询';

  _el.innerHTML = `
  <div class="card mb-3">
    <div class="card-header d-flex flex-wrap gap-3 align-items-center">
      来源
      <span class="ms-auto sp-seg">
        ${[['catalog', '库内谱'], ['upload', '上传'], ['paste', '粘贴']].map(([v, label]) => `
          <label><input class="sp-src" type="radio" name="spSrc" value="${v}" ${kind === v ? 'checked' : ''}>
          <span class="sp-seg-item">${label}</span></label>`).join('')}
      </span>
    </div>
    <div class="card-body">
    ${kind === 'catalog' ? `
      <div class="row g-2 align-items-center mt-1">
        <div class="col-md-6">
          <input class="form-control form-control-sm sp-selspec" list="spSpecList" autocomplete="off"
            placeholder="输入源名 / #id / 仪器 / 日期筛选库内谱…"
            title="输入即筛选候选，选中（或直填 #id）后装载">
          <datalist id="spSpecList"></datalist>
          <div class="small text-danger sp-selhint mt-1" hidden></div>
        </div>
        <div class="col-md-6 sp-muted">
          ${prov ? `#${esc(prov.spectrum_id)} · ${prov.n_points} 点 · 可配对锚点 ${prov.n_catalog_anchors} · `
            + `median|F|=${prov.flux_median_cgs != null ? prov.flux_median_cgs.toExponential(3) : '—'} cgs · `
            + `flux_type=${esc(prov.flux_type || '—')}/u_fluxes=${esc(prov.u_fluxes || '—')}` : ''}
        </div>
      </div>` : ''}

    ${kind === 'upload' ? `
      <div class="mt-1">
        <input type="file" class="form-control form-control-sm sp-file" accept=".txt,.csv,.dat,.fits,.fit,.ecsv">
        <div class="sp-muted mt-1">文本（λ Å, Fλ[, 误差]）/ .fits / .ecsv
          <i class="bi bi-info-circle" title="2/3 列文本，逗号或空白分隔，支持 # 注释与 # key: value 头；.fits 收 BINTABLE 表或一维线性 WCS 谱，.ecsv 收 astropy ECSV；λ 单位按 TUNIT/列单位换算成 Å"></i></div>
      </div>` : ''}
    ${kind === 'paste' ? `
      <div class="mt-1">
        <textarea class="form-control form-control-sm sp-paste" rows="4" placeholder="粘贴谱文本（语法同上传）"
          ${S.parseError ? 'aria-invalid="true" aria-describedby="spParseError"' : ''}></textarea>
        <button class="sp-btn-ghost mt-1 sp-parsepaste">解析粘贴文本</button>
      </div>` : ''}

    ${up ? `
      <div class="small mt-2">
        <span class="sp-dot sp-dot-green"></span>解析成功：<strong>${up.n_points}</strong> 点 · 覆盖 ${up.lam_min ?? '—'}–${up.lam_max ?? '—'} Å ·
        ${up.columns} 列 · 单位 ${esc(up.flux_unit || '—')}${up.flux_unit_assumed ? '（假定）' : ''} ·
        spec_hash <code>${esc(String(up.spec_hash || '').slice(0, 8))}</code>
      </div>
      ${(up.warnings || []).length ? `<div class="mt-1">解析告警：${up.warnings.map(w =>
        `<span class="badge bg-warning text-dark me-1" title="${escAttr(w.message || '')}"
          data-code="${escAttr(w.code || '')}">${esc(w.code || '')}${w.reason ? '·' + esc(w.reason) : ''}</span>`).join('')}</div>` : ''}
      <div class="sp-warnbox mt-2">⚠ ${TXT.t18}</div>`
      : (kind === 'upload' || kind === 'paste') && S.parseError ? `<div class="small text-danger mt-2" id="spParseError" role="alert">${esc(S.parseError)}</div>` : ''}

    <div class="row g-2 mt-1 small">
      <div class="col-md-3">
        <label for="spRa">RA${provTag('ra_deg')}</label>
        <input id="spRa" class="form-control form-control-sm sp-ra" value="${escAttr(S.metaForm.ra)}"
          placeholder="12:34:56.7 或十进制度">
      </div>
      <div class="col-md-3">
        <label for="spDec">Dec${provTag('dec_deg')}</label>
        <input id="spDec" class="form-control form-control-sm sp-dec" value="${escAttr(S.metaForm.dec)}"
          placeholder="-05:06:07 或十进制度">
      </div>
      <div class="col-md-2">
        <label for="spZ">红移 z${provTag('z')}</label>
        <input id="spZ" type="number" step="any" min="0" class="form-control form-control-sm sp-z"
          value="${escAttr(S.metaForm.z)}" placeholder="0">
      </div>
      <div class="col-md-2">
        <label for="spMjd">谱时刻 MJD${provTag('mjd')}</label>
        <input id="spMjd" type="number" step="any" class="form-control form-control-sm sp-mjd"
          value="${escAttr(S.metaForm.mjd)}" placeholder="无">
      </div>
      <div class="col-md-2">
        <label for="spCat">源类别
          <i class="bi bi-info-circle text-secondary" title="${kind === 'catalog'
            ? '以上为库内记录回填值，可改，改动按用户填报口径生效'
            : '缺项按默认处理：z=0 按观测系、类别 other、MJD 缺 ⇒ 锚点只做幅值比对'}"></i></label>
        <select id="spCat" class="form-select form-select-sm sp-cat">
          ${['grb_early', 'grb_afterglow', 'sn_platform', 'other'].map(c =>
            `<option value="${c}" ${S.metaForm.category === c ? 'selected' : ''}>${c}</option>`).join('')}
        </select>
      </div>
      ${S.zStaleNote ? `<div class="col-12 text-warning small" id="spZStaleNote">
        z 已改动：静止系相关字段（红移改正/静止系量）已过期，重算后更新；本改动不触发请求。</div>` : ''}
    </div>

    <div class="row g-2 mt-1 small align-items-end">
      <div class="col-md-4">
        <label for="spFrame">波长框架</label>
        ${kind === 'upload' || kind === 'paste' ? `
          <select id="spFrame" class="form-select form-select-sm sp-frame">
            ${[['vacuum', 'vacuum（真空）'], ['air', 'air（空气，积分前换真空恰好一次）'], ['unknown', 'unknown（不做框架假设）']].map(([v, l]) =>
              `<option value="${v}" ${((S.uploadFrame) || 'vacuum') === v ? 'selected' : ''}>${l}</option>`).join('')}
          </select>`
        : `<input id="spFrame" class="form-control form-control-sm" value="${esc(frame || 'vacuum')}" readonly title="库内谱框架，只读回显">`}
        ${frameDefaultVacuum
          ? `<div class="sp-warnbox sp-txt21 mt-2"><strong>⚠ 波长框架未声明，按真空处理：</strong>${TXT.t21}</div>`
          : `<div class="sp-muted mt-1" title="${escAttr(TXT.t21)}">框架口径 ⚠（悬停看全文）</div>`}
      </div>
      <div class="col-md-8">
        <label for="spMwSwitch">银河消光</label>
        <div class="d-flex gap-2 align-items-center">
          <div class="form-check form-switch mb-0">
            <input class="form-check-input sp-mw" type="checkbox" role="switch" id="spMwSwitch"
              ${S.params.mw_correct ? 'checked' : ''} ${gext ? 'disabled' : ''}
              title="${gext ? '该谱已带银河消光改正，不再二次施加' : '关闭 ⇒ 结果按未改正的观测谱给出'}">
            <label class="form-check-label" for="spMwSwitch">改正</label>
          </div>
          <input type="number" step="any" min="0" class="form-control form-control-sm sp-ebv sp-w110"
            value="${escAttr(S.params.ebv_override)}" placeholder="E(B−V) 手填覆盖" aria-label="E(B−V) 手填覆盖">
          <button class="sp-btn-ghost sp-ebvq"
            title="${escAttr(TXT.t19)}">查询 E(B−V)</button>
        </div>
        <div class="sp-muted mt-1">${gext ? '⚠ 该谱已带银消改正，不再二次施加。' : esc(ebvTxt)}</div>
        ${gext && parentId ? `<div class="sp-parent-link sp-muted">↩ 这条谱已改过（gext_corr），父谱为
          <a href="#/tools/specphot/${escAttr(parentId)}" title="装载父谱 #${escAttr(parentId)} 到本模块（原始谱未做银消改正）">父谱 #${escAttr(parentId)}</a>
          ——需要自行定消光口径时请改用父谱。</div>` : ''}
      </div>
    </div>
  </div></div>`;

  wire(ctx);
}

// ─── API-8 E(B−V) 查询（W-28①：RA/Dec 改动后自动重查与手动按钮共用本函数） ───
async function queryEbv(ctx) {
  const S = ctx.S;
  const rra = parseCoord(S.metaForm.ra, true);
  const rdec = parseCoord(S.metaForm.dec, false);
  if (rra.err || rdec.err || rra.deg == null || rdec.deg == null) {
    S.ebv = { value: null, rv: 3.1, law: 'P92', available: null, err: '坐标无法解析，先填 RA/Dec' };
    return;   // 坐标不完整时不发请求，提示留在渲染层
  }
  try {
    const resp = await ctx.spPost('/specphot/ebv', { ra: rra.deg, dec: rdec.deg });
    S.ebv = { value: resp.available ? resp.ebv : null, rv: resp.rv, law: resp.law,
              available: resp.available, err: resp.available ? null : (resp.detail || '尘埃图不可用') };
  } catch (e) {
    S.ebv = { value: null, rv: 3.1, law: 'P92', available: false, err: e.message };
  }
}

// ─── 事件 ───
function wire(ctx) {
  const S = ctx.S;
  _el.querySelectorAll('.sp-src').forEach(r => r.addEventListener('change', () => {
    const kind = r.value;
    if (kind !== S.sourceKind) {
      ctx.switchSource(kind);          // IA-19：换来源比换谱更彻底（元数据/锚点/掩膜/结果全清）
      renderPanel();
      ctx.refreshChrome();
    }
  }));

  const sel = _el.querySelector('.sp-selspec');
  if (sel) {
    fillSpecCandidates();
    sel.addEventListener('change', async () => {
      const v = sel.value.trim();
      if (!v) return;
      const items = _specItems || [];
      let hit = items.find(x => x.label === v);
      if (!hit) {                          // 直填 #id / 纯数字 id 也受理
        const m = v.match(/^#?(\d+)$/);
        if (m) hit = items.find(x => String(x.id) === m[1]);
      }
      const hint = _el.querySelector('.sp-selhint');
      if (!hit) {
        if (hint) { hint.hidden = false; hint.textContent = '未匹配到库内谱：从下拉候选选择，或直接填 #id'; }
        return;
      }
      if (hint) hint.hidden = true;
      if (String(hit.id) === String(S.spectrumId)) return;   // 同一谱不重复装载
      ctx.switchSource('catalog');
      S.sourceKind = 'catalog';
      await ctx.loadCatalogSpectrum(Number(hit.id));
      renderPanel(); ctx.refreshChrome();
    });
  }

  const file = _el.querySelector('.sp-file');
  if (file) file.addEventListener('change', () => {
    const f = file.files && file.files[0];
    if (!f) return;
    if (f.size > C_MAX_UPLOAD_BYTES) {
      _ctx.S.parseError = `超过单件体积上限 ${C_MAX_UPLOAD_BYTES} 字节`; renderPanel(); return;
    }
    const name = (f.name || '').toLowerCase();
    if (name.endsWith('.fits') || name.endsWith('.fit')) {
      // F-76 P2+：FITS 二进制走 content_b64（base64，只在内存，RO-5 不落盘）
      const reader = new FileReader();
      reader.onload = () => {
        const b64 = String(reader.result || '').split(',')[1] || '';
        if (!b64) { _ctx.S.parseError = '文件读取失败'; renderPanel(); return; }
        parseUpload({ content_b64: b64 }, 'fits', 'upload', f.size);
      };
      reader.readAsDataURL(f);
    } else {
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || '');
        const hint = name.endsWith('.ecsv') ? 'ecsv' : 'txt';
        parseUpload({ text }, hint, 'upload', text.length);
      };
      reader.readAsText(f);
    }
  });

  const pasteBtn = _el.querySelector('.sp-parsepaste');
  if (pasteBtn) pasteBtn.addEventListener('click', () => {
    parseText(_el.querySelector('.sp-paste').value, 'paste');
  });

  const ra = _el.querySelector('.sp-ra');
  if (ra) {
    attachCoordHint(ra, true);
    // W-28①/IA-4：改 RA ⇒ E(B−V) 按坐标自动重查（TXT-19「改坐标即重算」）+ 计算状态转 stale
    ra.addEventListener('change', async () => {
      S.metaForm.ra = ra.value;
      if (S.metaUserEdits) S.metaUserEdits.ra_deg = true;
      ctx.dirty();
      await queryEbv(ctx);
      ctx.dirty(); renderPanel();
    });
  }
  const dec = _el.querySelector('.sp-dec');
  if (dec) {
    attachCoordHint(dec, false);
    // W-28①/IA-4：改 Dec ⇒ 同上（自动重查 + stale）
    dec.addEventListener('change', async () => {
      S.metaForm.dec = dec.value;
      if (S.metaUserEdits) S.metaUserEdits.dec_deg = true;
      ctx.dirty();
      await queryEbv(ctx);
      ctx.dirty(); renderPanel();
    });
  }
  const z = _el.querySelector('.sp-z');
  if (z) z.addEventListener('change', () => {
    // W-28②：改 z ⇒ 静止系相关字段转 stale（ctx.dirty：fresh→stale），不发任何请求
    S.metaForm.z = z.value;
    if (S.metaUserEdits) S.metaUserEdits.z = true;
    S.zStaleNote = true;
    ctx.dirty(); renderPanel();
  });
  const mjd = _el.querySelector('.sp-mjd');
  if (mjd) mjd.addEventListener('change', () => {
    S.metaForm.mjd = mjd.value;
    if (S.metaUserEdits) S.metaUserEdits.mjd = true;
    ctx.dirty();
  });
  const cat = _el.querySelector('.sp-cat');
  if (cat) cat.addEventListener('change', () => { S.metaForm.category = cat.value; ctx.dirty(); });
  const frame = _el.querySelector('.sp-frame');
  if (frame) frame.addEventListener('change', () => {
    // W-30①：用户显式声明框架 ⇒ 不再视为「默认 vacuum 未声明」，TXT-21 常驻块撤下
    S.uploadFrame = frame.value;
    S.uploadFrameDeclared = true;
    ctx.dirty(); renderPanel();
  });

  const mw = _el.querySelector('.sp-mw');
  if (mw) mw.addEventListener('change', () => { S.params.mw_correct = mw.checked; ctx.dirty(); });
  const ebvIn = _el.querySelector('.sp-ebv');
  if (ebvIn) ebvIn.addEventListener('change', () => { S.params.ebv_override = ebvIn.value; ctx.dirty(); });

  const ebvBtn = _el.querySelector('.sp-ebvq');
  if (ebvBtn) ebvBtn.addEventListener('click', async () => {
    await queryEbv(ctx);   // W-28①：与 RA/Dec 自动重查共用同一实现
    ctx.dirty(); renderPanel();
  });
}
