// === 光变数据表 CSV 上传（一次性列映射导入） ===
// 流程：选文件解析 → 指定「上传表列 → 数据库列」映射（或用固定值填充）→ 预览校验 → 批量导入
// 约定与「添加记录」一致：时间统一换算为秒入库；流量保留原始单位；空字符串 = null
import { createLightcurves, showToast } from '../api.js';

// ─── 可映射的数据库列（与 detail.js「添加记录」字段集一致，另加 time_unit） ───
const DB_FIELDS = [
  { key: 'time',                    label: '时间 time',                 type: 'float',   required: true },
  { key: 'time_err',                label: '时间误差 time_err',         type: 'float' },
  { key: 'time_unit',               label: '时间单位 time_unit',        type: 'timeunit' },
  { key: 'band',                    label: '波段 band',                 type: 'str',     required: true },
  { key: 'flux_density',            label: '流量/星等 flux_density',    type: 'float',   required: true },
  { key: 'flux_density_err',        label: '误差 flux_density_err',     type: 'float' },
  { key: 'flux_density_unit',       label: '单位 flux_density_unit',    type: 'str',     required: true },
  { key: 'mag_system',              label: '星等系统 mag_system',       type: 'magsys' },
  { key: 'gext_corr',               label: '已银消 gext_corr',          type: 'bool' },
  { key: 'upperlimit',              label: '上限 upperlimit',           type: 'bool' },
  { key: 'host_subtracted',         label: '扣宿主 host_subtracted',    type: 'bool' },
  { key: 'gext_Alambda',            label: '银消量 gext_Alambda',       type: 'float' },
  { key: 'mag_gextcor',             label: '银消后AB星等 mag_gextcor',  type: 'float' },
  { key: 'mag_gextcor_err',         label: '银消星等误差 mag_gextcor_err', type: 'float' },
  { key: 'flux_density_gextcor',    label: '银消后流量 flux_density_gextcor', type: 'float' },
  { key: 'flux_density_gextcor_err', label: '银消流量误差 flux_density_gextcor_err', type: 'float' },
  { key: 'weights',                 label: '权重 weights',              type: 'float' },
  { key: 'discard',                 label: '丢弃 discard',              type: 'bool' },
  { key: 'telescope',               label: '望远镜 telescope',          type: 'str' },
  { key: 'instrument',              label: '仪器 instrument',           type: 'str' },
  { key: 'reference',               label: '引用 reference',            type: 'str' },
  { key: 'comment',                 label: '备注 comment',              type: 'str' },
];

// ─── 表头自动猜测同义词（归一化后精确匹配：小写、去空格/下划线/连字符/括号） ───
const HEADER_SYNONYMS = {
  time: ['time', 't', 'dt', 'times', 'timesincetrigger', 'tt0', 'timesinceburst', 'obstime', 'deltat', 'mjdobs'],
  time_err: ['timeerr', 'terr', 'dtime', 'timeerror', 'sigmat', 'deltaterr', 'exptime', 'exposure'],
  time_unit: ['timeunit', 'tunit'],
  band: ['band', 'filter', 'filt', 'passband', 'bandname', 'energy', 'freq', 'frequency', 'channel'],
  flux_density: ['fluxdensity', 'flux', 'mag', 'magnitude', 'fluxmjy', 'fnu', 'countrate', 'rate', 'fluxnu', 'fd', 'value'],
  flux_density_err: ['fluxdensityerr', 'fluxerr', 'fluxerror', 'magerr', 'magerror', 'err', 'error', 'ferr', 'eflux', 'sigma', 'fderr', 'fluxerror', 'dflux'],
  flux_density_unit: ['fluxdensityunit', 'unit', 'fluxunit', 'units'],
  mag_system: ['magsystem', 'magsys', 'system', 'photsystem', 'magstd'],
  gext_corr: ['gextcorr', 'extcorr', 'galacticext', 'dereddened'],
  upperlimit: ['upperlimit', 'ul', 'lim', 'limit', 'isupperlimit', 'upperlim'],
  host_subtracted: ['hostsubtracted', 'hostsub', 'subtracted', 'hostcorr', 'hostcorrected', 'nohost'],
  gext_Alambda: ['gextalambda', 'alambda', 'extinction'],
  mag_gextcor: ['maggextcor', 'magcorr', 'deredmag'],
  mag_gextcor_err: ['maggextcorerr', 'magcorrerr'],
  flux_density_gextcor: ['fluxdensitygextcor', 'fluxcorr', 'deredflux'],
  flux_density_gextcor_err: ['fluxdensitygextcorerr', 'fluxcorrerr'],
  weights: ['weights', 'weight', 'w'],
  discard: ['discard', 'discarded', 'drop', 'flag'],
  telescope: ['telescope', 'tel', 'observatory', 'obs', 'site'],
  instrument: ['instrument', 'inst', 'camera', 'detector'],
  reference: ['reference', 'ref', 'source', 'citation', 'bibcode', 'ads'],
  comment: ['comment', 'comments', 'note', 'notes', 'remark', 'remarks'],
};

const TIME_UNIT_ALIASES = {
  s: 's', sec: 's', secs: 's', second: 's', seconds: 's',
  m: 'm', min: 'm', mins: 'm', minute: 'm', minutes: 'm',
  h: 'h', hr: 'h', hrs: 'h', hour: 'h', hours: 'h',
  d: 'd', day: 'd', days: 'd',
};
const TIME_FACTOR = { s: 1, m: 60, h: 3600, d: 86400 };

// ─── 模块状态（每次 showLcUpload 重置） ───
let _tid = null;
let _onDone = null;
let _file = null;          // File 对象
let _rawText = '';
let _columns = [];         // 上传表列名
let _rows = [];            // 数据行（字符串二维数组）
let _modalEl = null;

// ─── HTML 转义（预览表格用） ───
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function normHeader(h) {
  return String(h || '').toLowerCase().replace(/[\s_\-()[\]{}./]/g, '');
}

// ─── 解析：分隔符检测 + 引号感知的行切分 ───
function detectDelimiter(line, choice) {
  if (choice !== 'auto') {
    return { comma: ',', tab: '\t', semicolon: ';', space: null }[choice];
  }
  const counts = { ',': 0, '\t': 0, ';': 0 };
  let inQ = false;
  for (const ch of line) {
    if (ch === '"') inQ = !inQ;
    else if (!inQ && ch in counts) counts[ch]++;
  }
  let best = null, bestN = 0;
  for (const [d, n] of Object.entries(counts)) {
    if (n > bestN) { best = d; bestN = n; }
  }
  return best; // null = 按连续空白切分
}

function splitLine(line, delim) {
  if (delim === null) return line.trim().split(/\s+/);
  const cells = [];
  let cur = '', inQ = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQ) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; }
        else inQ = false;
      } else cur += ch;
    } else if (ch === '"') {
      inQ = true;
    } else if (ch === delim) {
      cells.push(cur.trim()); cur = '';
    } else cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}

function isNumericCell(s) {
  return s !== '' && !isNaN(Number(s));
}

// 解析文本 → { columns, rows, hasHeader }
function parseTable(text, delimChoice, forceHeader) {
  const lines = text.split(/\r\n|\r|\n/)
    .map(l => l.trim())
    .filter(l => l !== '' && !l.startsWith('#'));
  if (!lines.length) return { columns: [], rows: [], hasHeader: false };
  const delim = detectDelimiter(lines[0], delimChoice);
  const parsed = lines.map(l => splitLine(l, delim));
  // 表头判定：手动指定优先；自动 = 首行超过半数单元格非数值
  let hasHeader;
  if (forceHeader === 'yes') hasHeader = true;
  else if (forceHeader === 'no') hasHeader = false;
  else {
    const nonNum = parsed[0].filter(c => !isNumericCell(c)).length;
    hasHeader = parsed[0].length > 0 && nonNum > parsed[0].length / 2;
  }
  const nCols = Math.max(...parsed.map(r => r.length));
  let columns, dataRows;
  if (hasHeader) {
    columns = parsed[0].map((c, i) => c || `第${i + 1}列`);
    dataRows = parsed.slice(1);
  } else {
    columns = Array.from({ length: nCols }, (_, i) => `第${i + 1}列`);
    dataRows = parsed;
  }
  // 补齐短行
  dataRows = dataRows.map(r => r.concat(Array(Math.max(0, nCols - r.length)).fill('')));
  return { columns, rows: dataRows, hasHeader };
}

// ─── 表头 → 数据库列自动猜测（每列最多猜给一个字段，按 DB_FIELDS 顺序抢占） ───
function guessMapping() {
  const mapping = {}; // fieldKey -> columnIndex
  const used = new Set();
  const normCols = _columns.map(normHeader);
  for (const f of DB_FIELDS) {
    const cands = [normHeader(f.key), ...(HEADER_SYNONYMS[f.key] || [])];
    for (let ci = 0; ci < normCols.length; ci++) {
      if (used.has(ci)) continue;
      if (cands.includes(normCols[ci])) {
        mapping[f.key] = ci;
        used.add(ci);
        break;
      }
    }
  }
  return mapping;
}

// ─── 模态框 HTML（注入一次） ───
function ensureModal() {
  if (_modalEl) return;
  const div = document.createElement('div');
  div.innerHTML = `
  <div class="modal fade" id="lcUploadModal" tabindex="-1">
    <div class="modal-dialog modal-xl">
      <div class="modal-content">
        <div class="modal-header">
          <h6 class="mb-0"><i class="bi bi-upload"></i> 上传光变数据表 → <span id="lcUpTid"></span></h6>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
        </div>
        <div class="modal-body">
          <!-- 步骤指示 -->
          <div class="d-flex gap-3 mb-3 small" id="lcUpSteps">
            <span data-step="1"><i class="bi bi-1-circle"></i> 选择文件</span>
            <span data-step="2" class="text-secondary"><i class="bi bi-2-circle"></i> 列映射</span>
            <span data-step="3" class="text-secondary"><i class="bi bi-3-circle"></i> 预览导入</span>
          </div>

          <!-- 第 1 步：文件与解析 -->
          <div id="lcUpStep1">
            <div class="row g-2 align-items-end">
              <div class="col-md-5">
                <label class="form-label small">数据表文件（CSV / TSV / 空白分隔文本）</label>
                <input type="file" class="form-control form-control-sm" id="lcUpFile" accept=".csv,.txt,.tsv,.dat">
              </div>
              <div class="col-md-3">
                <label class="form-label small">分隔符</label>
                <select class="form-select form-select-sm" id="lcUpDelim">
                  <option value="auto" selected>自动检测</option>
                  <option value="comma">逗号 ,</option>
                  <option value="tab">制表符 Tab</option>
                  <option value="semicolon">分号 ;</option>
                  <option value="space">空白</option>
                </select>
              </div>
              <div class="col-md-3">
                <label class="form-label small">首行为表头</label>
                <select class="form-select form-select-sm" id="lcUpHeader">
                  <option value="auto" selected>自动判断</option>
                  <option value="yes">是</option>
                  <option value="no">否</option>
                </select>
              </div>
            </div>
            <div class="small text-secondary mt-2">空行与 <code>#</code> 开头的注释行会被忽略；文件按 UTF-8 读取。</div>
            <div id="lcUpParseInfo" class="small mt-2"></div>
            <div id="lcUpRawPreview" class="table-scroll mt-1" style="max-height:300px;overflow:auto"></div>
          </div>

          <!-- 第 2 步：列映射 -->
          <div id="lcUpStep2" style="display:none">
            <div class="small text-secondary mb-2">
              为每个数据库列选择数据来源：<strong>不导入</strong> / <strong>上传表的某列</strong> / <strong>固定值</strong>（用指定内容填充所有行）。
              带 <span class="text-danger">*</span> 为必填列。映射仅本次有效，不会保存。
            </div>
            <div class="table-scroll" style="max-height:420px;overflow:auto">
              <table class="table table-sm mb-0" style="font-size:0.85rem">
                <thead><tr><th style="width:260px">数据库列</th><th style="width:260px">数据来源</th><th>固定值</th></tr></thead>
                <tbody id="lcUpMapBody"></tbody>
              </table>
            </div>
          </div>

          <!-- 第 3 步：预览与校验 -->
          <div id="lcUpStep3" style="display:none">
            <div id="lcUpValidInfo" class="small mb-2"></div>
            <div id="lcUpErrList" class="small text-danger mb-2" style="max-height:120px;overflow:auto"></div>
            <div class="small text-secondary mb-1">映射结果预览（前 10 行，时间已换算为秒）：</div>
            <div class="table-scroll" style="max-height:300px;overflow:auto" id="lcUpMappedPreview"></div>
          </div>

          <div class="small text-danger mt-2" id="lcUpError" style="display:none"></div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-sm btn-outline-secondary" id="lcUpBack" style="display:none">上一步</button>
          <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">取消</button>
          <button class="btn btn-sm btn-primary" id="lcUpNext" disabled>下一步</button>
          <button class="btn btn-sm btn-success" id="lcUpSubmit" style="display:none">导入</button>
        </div>
      </div>
    </div>
  </div>`;
  document.body.appendChild(div.firstElementChild);
  _modalEl = document.getElementById('lcUploadModal');

  // 事件绑定（模块内 addEventListener，不走全局 onclick）
  _modalEl.querySelector('#lcUpFile').addEventListener('change', onFileChange);
  _modalEl.querySelector('#lcUpDelim').addEventListener('change', reparse);
  _modalEl.querySelector('#lcUpHeader').addEventListener('change', reparse);
  _modalEl.querySelector('#lcUpNext').addEventListener('click', onNext);
  _modalEl.querySelector('#lcUpBack').addEventListener('click', onBack);
  _modalEl.querySelector('#lcUpSubmit').addEventListener('click', onSubmit);
}

// ─── 步骤切换 ───
let _step = 1;
function showStep(n) {
  _step = n;
  for (let i = 1; i <= 3; i++) {
    _modalEl.querySelector(`#lcUpStep${i}`).style.display = i === n ? '' : 'none';
  }
  _modalEl.querySelectorAll('#lcUpSteps span').forEach(sp => {
    sp.classList.toggle('text-secondary', parseInt(sp.dataset.step) !== n);
  });
  _modalEl.querySelector('#lcUpBack').style.display = n > 1 ? '' : 'none';
  _modalEl.querySelector('#lcUpNext').style.display = n < 3 ? '' : 'none';
  _modalEl.querySelector('#lcUpSubmit').style.display = n === 3 ? '' : 'none';
  hideError();
}

function showError(msg) {
  const el = _modalEl.querySelector('#lcUpError');
  el.textContent = msg;
  el.style.display = 'block';
}
function hideError() {
  _modalEl.querySelector('#lcUpError').style.display = 'none';
}

// ─── 第 1 步：文件读取与解析 ───
async function onFileChange(e) {
  _file = e.target.files[0] || null;
  if (!_file) { reparse(); return; }
  _rawText = await _file.text();
  reparse();
}

function reparse() {
  const info = _modalEl.querySelector('#lcUpParseInfo');
  const prev = _modalEl.querySelector('#lcUpRawPreview');
  const nextBtn = _modalEl.querySelector('#lcUpNext');
  if (!_file || !_rawText) {
    _columns = []; _rows = [];
    info.innerHTML = ''; prev.innerHTML = '';
    nextBtn.disabled = true;
    return;
  }
  const delimChoice = _modalEl.querySelector('#lcUpDelim').value;
  const headerChoice = _modalEl.querySelector('#lcUpHeader').value;
  const { columns, rows, hasHeader } = parseTable(_rawText, delimChoice, headerChoice);
  _columns = columns; _rows = rows;
  if (!rows.length) {
    info.innerHTML = '<span class="text-danger">未解析到数据行</span>';
    prev.innerHTML = '';
    nextBtn.disabled = true;
    return;
  }
  info.innerHTML = `文件 <strong>${esc(_file.name)}</strong>：${rows.length} 行数据 × ${columns.length} 列${hasHeader ? '（首行识别为表头）' : '（无表头）'}`;
  prev.innerHTML = previewTable(columns, rows.slice(0, 10));
  nextBtn.disabled = false;
}

function previewTable(columns, rows) {
  return `<table class="table table-sm table-striped mb-0" style="font-size:0.8rem">
    <thead><tr>${columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody>
  </table>`;
}

// ─── 第 2 步：渲染映射表 ───
function renderMapping() {
  const guessed = guessMapping();
  const tbody = _modalEl.querySelector('#lcUpMapBody');
  tbody.innerHTML = DB_FIELDS.map(f => {
    const colOpts = _columns.map((c, i) =>
      `<option value="col:${i}" ${guessed[f.key] === i ? 'selected' : ''}>列 ${i + 1}: ${esc(c)}</option>`).join('');
    let fixedInput;
    if (f.type === 'timeunit') {
      fixedInput = `<select class="form-select form-select-sm lc-up-fixed" data-field="${f.key}" style="display:none;width:120px">
        <option value="s" selected>s（秒）</option><option value="m">m（分）</option>
        <option value="h">h（小时）</option><option value="d">d（天）</option></select>`;
    } else if (f.type === 'magsys') {
      fixedInput = `<select class="form-select form-select-sm lc-up-fixed" data-field="${f.key}" style="display:none;width:120px">
        <option value="AB" selected>AB</option><option value="Vega">Vega</option></select>`;
    } else {
      const ph = f.type === 'bool' ? 'y/n' : (f.type === 'float' ? '数值' : '文本');
      fixedInput = `<input type="text" class="form-control form-control-sm lc-up-fixed" data-field="${f.key}" placeholder="${ph}" style="display:none">`;
    }
    return `<tr>
      <td>${f.label}${f.required ? ' <span class="text-danger">*</span>' : ''}</td>
      <td><select class="form-select form-select-sm lc-up-src" data-field="${f.key}">
        <option value="" ${guessed[f.key] == null ? 'selected' : ''}>不导入</option>
        ${colOpts}
        <option value="fixed">固定值…</option>
      </select></td>
      <td>${fixedInput}</td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('.lc-up-src').forEach(sel => {
    sel.addEventListener('change', () => {
      const inp = tbody.querySelector(`.lc-up-fixed[data-field="${sel.dataset.field}"]`);
      if (inp) inp.style.display = sel.value === 'fixed' ? '' : 'none';
    });
  });
}

// 读取当前映射：{ fieldKey: { col: i } | { fixed: '...' } }
function collectMapping() {
  const mapping = {};
  _modalEl.querySelectorAll('.lc-up-src').forEach(sel => {
    const field = sel.dataset.field;
    const v = sel.value;
    if (v === '') return;
    if (v === 'fixed') {
      const inp = _modalEl.querySelector(`.lc-up-fixed[data-field="${field}"]`);
      const val = inp ? inp.value : '';
      if (inp && inp.tagName === 'SELECT') mapping[field] = { fixed: val };
      else if (val.trim() !== '') mapping[field] = { fixed: val };
      // 固定值输入为空 = 不导入
    } else {
      mapping[field] = { col: parseInt(v.slice(4)) };
    }
  });
  return mapping;
}

// ─── 值转换 ───
function parseBool(s) {
  return ['y', 'yes', 'true', '1', 't'].includes(String(s).trim().toLowerCase());
}

function normTimeUnit(s) {
  const u = TIME_UNIT_ALIASES[String(s).trim().toLowerCase()];
  return u || null;
}

// 按映射构建入库记录，返回 { records, errors: [{row, msg}] }
function buildRecords(mapping) {
  const records = [];
  const errors = [];
  _rows.forEach((cells, ri) => {
    const rowNo = ri + 1;
    const rec = { transient_id: _tid };
    let rowErr = null;
    for (const f of DB_FIELDS) {
      const m = mapping[f.key];
      if (!m) continue;
      const raw = m.col != null ? (cells[m.col] ?? '') : String(m.fixed);
      const val = raw.trim();
      if (f.type === 'float') {
        if (val === '') {
          if (f.required) { rowErr = `${f.key} 为空`; break; }
          rec[f.key] = null;
        } else {
          const num = parseFloat(val);
          if (!isFinite(num)) {
            if (f.required) { rowErr = `${f.key}="${val}" 不是数值`; break; }
            rec[f.key] = null;
          } else rec[f.key] = num;
        }
      } else if (f.type === 'bool') {
        // host_subtracted 空值 = 未知（NULL）；其余布尔列空值 = false
        rec[f.key] = (f.key === 'host_subtracted' && val === '') ? null : parseBool(val);
      } else if (f.type === 'timeunit') {
        const u = normTimeUnit(val);
        if (val !== '' && !u) { rowErr = `time_unit="${val}" 无法识别`; break; }
        rec[f.key] = u || 's';
      } else { // str / magsys
        if (val === '') {
          if (f.required) { rowErr = `${f.key} 为空`; break; }
          rec[f.key] = null;
        } else rec[f.key] = val;
      }
    }
    // 必填列未映射也算行错误
    if (!rowErr) {
      for (const f of DB_FIELDS.filter(x => x.required)) {
        if (rec[f.key] == null) { rowErr = `必填列 ${f.key} 未映射或为空`; break; }
      }
    }
    // 星等数据要求星等系统
    if (!rowErr && rec.flux_density_unit && rec.flux_density_unit.toLowerCase().includes('mag')
        && !rec.mag_system) {
      rowErr = '单位为星等但 mag_system 为空';
    }
    if (rowErr) { errors.push({ row: rowNo, msg: rowErr }); return; }
    // 时间统一换算为秒
    const unit = rec.time_unit || 's';
    const factor = TIME_FACTOR[unit] || 1;
    if (factor !== 1) {
      rec.time *= factor;
      if (rec.time_err != null) rec.time_err *= factor;
    }
    rec.time_unit = 's';
    records.push(rec);
  });
  return { records, errors };
}

// ─── 步骤推进 ───
function onNext() {
  if (_step === 1) {
    if (!_rows.length) { showError('请先选择并解析数据文件'); return; }
    renderMapping();
    showStep(2);
  } else if (_step === 2) {
    const mapping = collectMapping();
    const { records, errors } = buildRecords(mapping);
    if (!records.length && !errors.length) { showError('请至少映射一个数据库列'); return; }
    // 校验统计
    const info = _modalEl.querySelector('#lcUpValidInfo');
    info.innerHTML = `共 ${_rows.length} 行：<span class="text-success">${records.length} 行有效</span>` +
      (errors.length ? `，<span class="text-danger">${errors.length} 行无效（将被跳过）</span>` : '');
    _modalEl.querySelector('#lcUpErrList').innerHTML = errors.slice(0, 20)
      .map(e => `<div>第 ${e.row} 行：${esc(e.msg)}</div>`).join('') +
      (errors.length > 20 ? `<div>… 其余 ${errors.length - 20} 条错误省略</div>` : '');
    // 映射结果预览：仅显示已映射的列
    const mappedFields = DB_FIELDS.filter(f => mapping[f.key]);
    _modalEl.querySelector('#lcUpMappedPreview').innerHTML =
      `<table class="table table-sm table-striped mb-0" style="font-size:0.8rem">
        <thead><tr>${mappedFields.map(f => `<th>${f.key}</th>`).join('')}</tr></thead>
        <tbody>${records.slice(0, 10).map(r =>
          `<tr>${mappedFields.map(f => `<td>${r[f.key] != null ? esc(r[f.key]) : '-'}</td>`).join('')}</tr>`).join('')}</tbody>
      </table>`;
    _modalEl.querySelector('#lcUpSubmit').disabled = records.length === 0;
    showStep(3);
  }
}

function onBack() {
  if (_step > 1) showStep(_step - 1);
}

// ─── 导入执行 ───
async function onSubmit() {
  const mapping = collectMapping();
  const { records, errors } = buildRecords(mapping);
  if (!records.length) { showError('没有可导入的有效行'); return; }
  const skipped = errors.length;
  if (!confirm(`将向 ${_tid} 导入 ${records.length} 条光变记录` +
    (skipped ? `（跳过 ${skipped} 条无效行）` : '') + '，确认？')) return;
  const btn = _modalEl.querySelector('#lcUpSubmit');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 导入中...';
  let created = 0;
  try {
    const CHUNK = 500;
    for (let i = 0; i < records.length; i += CHUNK) {
      const resp = await createLightcurves(records.slice(i, i + CHUNK));
      created += resp.created;
    }
    bootstrap.Modal.getInstance(_modalEl).hide();
    showToast(`已导入 ${created} 条光变记录` + (skipped ? `，跳过 ${skipped} 条无效行` : ''), 'success');
    if (_onDone) _onDone();
  } catch (err) {
    showError(`导入失败（已写入 ${created} 条）: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '导入';
  }
}

// ─── 入口：重置状态并弹出 ───
export function showLcUpload(tid, onDone) {
  ensureModal();
  _tid = tid;
  _onDone = onDone;
  _file = null; _rawText = ''; _columns = []; _rows = [];
  _modalEl.querySelector('#lcUpTid').textContent = tid;
  _modalEl.querySelector('#lcUpFile').value = '';
  _modalEl.querySelector('#lcUpDelim').value = 'auto';
  _modalEl.querySelector('#lcUpHeader').value = 'auto';
  _modalEl.querySelector('#lcUpParseInfo').innerHTML = '';
  _modalEl.querySelector('#lcUpRawPreview').innerHTML = '';
  _modalEl.querySelector('#lcUpNext').disabled = true;
  _modalEl.querySelector('#lcUpSubmit').disabled = false;
  showStep(1);
  new bootstrap.Modal(_modalEl).show();
}
