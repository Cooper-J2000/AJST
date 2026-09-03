// === 波段工具（详情页光变图 / 多源对比 / 宿主统计图共用） ===
// 滤波器缓存由 ensureFilterCache(GET /api/filters 结果) 填充；
// 颜色规则：波段按频率升序（低频红、高频蓝），排序序号归一化映射色阶。

// ─── AB magnitude ↔ mJy（AB 零点 3631 Jy） ───
// m_AB = 16.4 − 2.5·log10(F_mJy)；F_mJy = 10^((16.4−m_AB)/2.5)
export function magABtoMJy(mag) {
  return Math.pow(10, (16.4 - mag) / 2.5);
}

export function mJyToMagAB(mJy) {
  return 16.4 - 2.5 * Math.log10(mJy);
}

// ─── 波段名归一化（"1.0keV" → "1keV", "9.0GHz" → "9GHz"） ───
export function normBand(band) {
  return band.replace(/(\d+)\.0(?=[A-Za-z])/g, '$1');
}

// ─── 滤波器缓存（波长 Å / Vega→AB 星等差） ───
const _filterWavelengths = {}; // band → wavelength (Å)
const _filterVega2ab = {};     // band → Vega→AB 星等差

export function ensureFilterCache(filtersData) {
  for (const f of filtersData || []) {
    _filterWavelengths[f.id] = f.wavelength;
    _filterVega2ab[f.id] = f.vega2ab || 0;
  }
}

// 波段名的候选滤波器键（波段名变体归一：R_{c}→R、r'→r、UVW2→uvot-uvw2 等）
function _bandKeys(band) {
  const b = normBand(band);
  const keys = [b];
  const base = b.replace(/_{.*}$/, '').replace(/'+$/, '');
  keys.push(base);
  const base2 = b.split('_')[0];
  if (base2) keys.push(base2);
  const low = b.toLowerCase();
  keys.push(low, 'uvot-' + low);
  return keys;
}

// 波段的 Vega→AB 星等差（无对应滤波器时返回 0）
export function getVega2ab(band) {
  for (const k of _bandKeys(band)) {
    if (_filterVega2ab[k] != null) return _filterVega2ab[k];
  }
  return 0;
}

export function getWavelength(band) {
  // 光学：查 filters 表
  for (const k of _bandKeys(band)) {
    if (_filterWavelengths[k] != null) return _filterWavelengths[k];
  }
  const b = normBand(band);
  // X 射线：λ(Å) = 12.3984 / E(keV)
  const keV = b.match(/^(\d+(?:\.\d+)?)\s*keV$/i);
  if (keV) return 12.3984 / parseFloat(keV[1]);
  // 射电：λ(Å) = c(m/s) * 1e10 / (f(Hz)) = 2.9979e9 / f(GHz)
  const GHz = b.match(/^(\d+(?:\.\d+)?)\s*GHz$/i);
  if (GHz) return 2.99792458e9 / parseFloat(GHz[1]);
  return null;
}

// ─── 波段按频率升序排序（低频在前；无频率的按名称排最后） ───
export function sortBandsByFreq(bandNames) {
  const c = 2.998e8;
  return [...bandNames].sort((a, b) => {
    const wa = getWavelength(a), wb = getWavelength(b);
    if (wa && wb) return (c / (wa * 1e-10)) - (c / (wb * 1e-10));
    if (wa) return -1;
    if (wb) return 1;
    return a.localeCompare(b);
  });
}

// ─── 光谱色阶（按频率排序序号归一化） ───
// 用排序序号归一化映射到色阶，保证任意频率分布下相邻波段都有可区分的颜色
export function buildSpectralColors(bandNames) {
  const c = 2.998e8; // m/s
  const withFreq = [], withoutFreq = [];
  for (const b of bandNames) {
    const wl = getWavelength(b); // Å
    if (wl && wl > 0) withFreq.push([b, c / (wl * 1e-10)]);
    else withoutFreq.push(b);
  }
  // 按频率升序排序（低频=红，高频=蓝）
  withFreq.sort((a, b) => a[1] - b[1]);
  const result = {};
  const n = withFreq.length;
  withFreq.forEach(([b], i) => {
    const t = n > 1 ? i / (n - 1) : 0.5;
    const hue = t * 240;                       // 0°(红) → 240°(蓝)
    const light = n > 8 && i % 2 === 1 ? 65 : 50; // 波段多时任奇偶交替亮度增强区分
    result[b] = `hsl(${hue}, 75%, ${light}%)`;
  });
  // 无频率的波段用默认颜色
  const defaultColors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#56d4dd'];
  withoutFreq.forEach((b, i) => {
    result[b] = defaultColors[i % defaultColors.length];
  });
  return result;
}
