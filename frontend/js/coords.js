// === 坐标解析（前端即时反馈用；入库转换以后端 backend/coords.py 为准） ===
// 支持十进制度与时分秒：RA 如 122.1142 / 08h08m27.4s / 8:08:27.4 / 8 08 27.4；
// Dec 如 40.61244 / +40d36m44.8s / -15:18:30 / 40°36'44.8"
// 约定：空串 → null（清空字段）；无法解析或超范围 → NaN；合法 → 度（RA 归一化到 [0,360)）

const _NUM_RE = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;
const _SEX_RE = /^([+-]?)\s*(\d+(?:\.\d*)?|\.\d+)\s*(?:[hHdD°:]|\s)\s*(?:(\d+(?:\.\d*)?|\.\d+)\s*(?:[mM'′:]|\s)?\s*)?(?:(\d+(?:\.\d*)?|\.\d+)\s*(?:[sS"″])?)?$/;

function _parse(str, isRA) {
  if (str == null) return null;
  const s = String(str).trim();
  if (s === '') return null;
  if (_NUM_RE.test(s)) {
    let v = parseFloat(s);
    if (isRA) return ((v % 360) + 360) % 360;
    return Math.abs(v) <= 90 ? v : NaN;
  }
  const m = s.match(_SEX_RE);
  if (!m) return NaN;
  const sign = m[1] === '-' ? -1 : 1;
  const a = parseFloat(m[2]);
  const b = m[3] !== undefined ? parseFloat(m[3]) : 0;
  const c = m[4] !== undefined ? parseFloat(m[4]) : 0;
  if (b >= 60 || c >= 60) return NaN;
  if (isRA) {
    if (a >= 24) return NaN;
    const v = (a + b / 60 + c / 3600) * 15 * sign;
    return ((v % 360) + 360) % 360;
  }
  const v = sign * (a + b / 60 + c / 3600);
  return Math.abs(v) <= 90 ? v : NaN;
}

export function parseRA(str) { return _parse(str, true); }
export function parseDec(str) { return _parse(str, false); }

// 在坐标输入框后挂一个即时解析提示（→ 度；非法时红字）
export function attachCoordHint(input, isRA) {
  if (!input) return;
  const hint = document.createElement('div');
  hint.className = 'form-text coord-hint';
  input.insertAdjacentElement('afterend', hint);
  const update = () => {
    const s = input.value.trim();
    if (s === '') {
      hint.textContent = '';
      hint.classList.remove('text-danger');
      return;
    }
    const v = isRA ? parseRA(s) : parseDec(s);
    if (v == null || Number.isNaN(v)) {
      hint.textContent = '无法解析：支持十进制度或时分秒（如 12h34m56.7s / 12:34:56.7）';
      hint.classList.add('text-danger');
    } else {
      hint.textContent = `→ ${v.toFixed(6)}°`;
      hint.classList.remove('text-danger');
    }
  };
  input.addEventListener('input', update);
  update();
}
