"""specphot 读侧：S0 输入装载（02 §3.11 F-73…F-80）与读/上传校验（§4.1 V-1…V-20）。

两条入口同一产物（F-73 的 LoadedSpectrum dict）：
  load_catalog_spectrum(payload, source_record=None)  —— 库内谱（V-1…V-17）。
      payload 是宿主 GET /api/spectra/<id> 的响应体（{'meta':..., 'data':...}）：
      宿主已清洗字符串行并把波长列转成真空 ⇒ 本模块绝不再做空气→真空（F-79① / T-53）。
  load_upload_text(text, ...)  —— 上传/粘贴件（V-18…V-20 + F-75 文本语法）。

波长换算一律 import wavconvert（禁行线 5）；数值判定前先 float() 规范化（V-3a，
97/113 库内谱整谱是字符串行）。解析只产出内存数组：不落盘、不写库（RO-5 / F-75①）。
"""
import hashlib
import io
import json
import math
import re
import statistics

import wavconvert

from .constants import (
    C_MAX_SPECPOINTS, C_MIN_UPLOAD_POINTS, C_MAX_UPLOAD_BYTES,
    C_GAP_FACTOR, C_FLUX_MEDIAN_MAX_CGS,
)

# λ 合法域（Å），与宿主 spectra.py:32 的 WL_MIN, WL_MAX 同值（V-20 / T-44）
WL_MIN, WL_MAX = 100.0, 1e7

DEFAULT_FLUX_UNIT = 'erg/s/cm^2/Angstrom'


class SpecLoadError(Exception):
    """读侧/计算侧拒绝。code ∈ §5.3 合并表（E-01…E-14）；reason 为机读细分。"""

    def __init__(self, code, message, reason=None, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status = status
        self.details = details or {}


def _warn(code, message):
    return {'code': code, 'message': message}


def _to_float(v):
    """V-3a 类型规范化：接受 int/float 与字符串（前后空白、科学计数法大写 E）。

    成功返回 float（含 NaN/Inf —— 有限性由 V-11 单独判）；失败返回 None。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


# ─── F-80 规范化序列化与 spec_hash ────────────────────────────────────

def _canon_value(v):
    """F-80 ①③④：值的规范化序列化（哈希载体的唯一实现）。"""
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return '"NaN"'
        if math.isinf(v):
            return '"Infinity"' if v > 0 else '"-Infinity"'
        # 先 %.12g 舍入到 12 位有效数字，再 repr 最短往返式输出
        return repr(float('%.12g' % v))
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return '[' + ','.join(_canon_value(x) for x in v) + ']'
    raise TypeError(f'不可序列化的值类型: {type(v).__name__}')


def canonical_bytes(values):
    """F-80 ①：值序列 → UTF-8 无 BOM 字节串（键名不写入，键序即语义）。"""
    return _canon_value(list(values)).encode('utf-8')


def spec_hash(lam_aa, flux):
    """F-80 ⑤：sha256(规范化序列化 [λ 升序 lam, 同序 flux])[:12]。

    谱点先按 λ 升序（稳定排序 ⇒ 等 λ 保持原首次出现序）；本模块的 lam 在
    V-9 聚合后已唯一且升序，这里再排一次是为 T-43 的乱序等价性兜底。
    """
    pairs = sorted(zip(lam_aa, flux), key=lambda p: p[0])
    lam = [p[0] for p in pairs]
    flx = [p[1] for p in pairs]
    return hashlib.sha256(canonical_bytes([lam, flx])).hexdigest()[:12]


# ─── F-75 上传文本语法（与宿主 spectra.py:181-201 同词法） ─────────────
#
# 与宿主的差异（规格钉死）：① 只返回内存数组，不落盘不写库；
# ② 重复 λ 聚合（V-9）而非覆盖、乱序拒绝（V-4）而非代排序；
# ③ V-18 列数必须全件一致（宿主容忍 2/3 列混排，本模块按 V-18 拒绝）；
# ④ F-75「表头分隔行跳过」：首 token 非数值的行按表头跳过并计数
#    （宿主对这类行直接报错 —— 两者只在这类行上结论不同，T-44 语料回避之）。

_HEADER_RE = re.compile(r'#+\s*([A-Za-z_ ]+)\s*[:=]\s*(.+)')
_SPLIT_RE = re.compile(r'[,\s]+')


def parse_text_spectrum(text):
    """解析两/三列文本 + # 头键。返回 (headers, rows, n_skipped_header)。

    rows = [(lineno_1based, [lam, flux] 或 [lam, flux, err], n_tokens), ...]，
    数值已 V-3a 规范化；n_tokens 是该行的原始列数（V-18 按它判全件一致）。
    词法与宿主 _parse_text_spectrum 逐条对齐（F-75）：
    # 起头为注释/头键（key: value 或 key=value，键小写、空格转下划线）；
    数据行逗号或空白分隔；λ/F 非数值 ⇒ E-14（同宿主）。
    """
    headers, rows = {}, []
    n_skipped_header = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            m = _HEADER_RE.match(line)
            if m:
                headers[m.group(1).strip().lower().replace(' ', '_')] = m.group(2).strip()
            continue
        toks = _SPLIT_RE.split(line)
        if len(toks) < 2:
            raise SpecLoadError('E-14', f'第 {lineno} 行无法解析（需两列或三列）: {line[:60]}',
                                reason='bad_row', details={'line': lineno})
        vals = [_to_float(t) for t in toks[:3]]
        if vals[0] is None:
            # 首 token 非数值 ⇒ F-75 的表头分隔行（wavelength flux [flux_err] 之类），跳过
            n_skipped_header += 1
            continue
        if vals[1] is None:
            raise SpecLoadError('E-14', f'第 {lineno} 行数值无法解析: {line[:60]}',
                                reason='bad_row', details={'line': lineno})
        row = [vals[0], vals[1]]
        if len(toks) >= 3 and vals[2] is not None:
            # 第三列非数值时按宿主口径当作无误差列（宿主 _to_float 返回 None 即不存）
            row.append(vals[2])
        rows.append((lineno, row, len(toks)))
    return headers, rows, n_skipped_header


def _check_uniform_width(rows):
    """V-18：全件列数一致（按 token 数计，2 或 3 列）。

    不一致 ⇒ E-14，列出前 3 处不合格行号（不做"按多数列数截断"）。"""
    if not rows:
        return
    base = rows[0][2]
    bad = [ln for ln, _, ntok in rows if ntok != base or ntok not in (2, 3)]
    if not bad:
        return
    raise SpecLoadError(
        'E-14',
        f'列数逐行不一致（需全件统一为 2 或 3 列），首处不合格行: {", ".join(map(str, bad[:3]))}',
        reason='ragged_columns', details={'bad_lines': bad[:3], 'first_bad_line': bad[0]})


# ─── 公共规范化核心（V-4/V-5/V-6/V-8/V-9/V-10/V-11/V-17） ─────────────

def _normalize_rows(rows, stats, warnings):
    """rows = [[lam, flux, err?], ...]（V-3a 已 float 化）。返回 (lam, flux, err_or_None)。

    判序：V-11 非有限值 → V-5 点数闸 → V-4 单调性 → V-9 重复 λ 聚合 →
    V-8 行宽 → V-10 间隙 → V-6 非正计数 → V-17 量级带。
    """
    # V-11：非有限值逐值过滤
    finite_rows = []
    for r in rows:
        if all(math.isfinite(v) for v in r):
            finite_rows.append(r)
        else:
            stats['n_nonfinite'] += 1
    if stats['n_nonfinite']:
        warnings.append(_warn('CA-10', f"丢弃 {stats['n_nonfinite']} 个含非有限值（NaN/Inf）的数据点"))
    rows = finite_rows

    # V-5：点数闸（库内谱与上传件同判）
    if len(rows) > C_MAX_SPECPOINTS:
        raise SpecLoadError('E-07',
                            f'点数 {len(rows)} 超过上限 {C_MAX_SPECPOINTS}，请在前端显式降采样后重试',
                            reason='too_many_points')

    # V-4：相邻严格递减 = 真乱序 ⇒ 拒绝（不代排序）；相邻相等 ⇒ V-9
    for i in range(len(rows) - 1):
        if rows[i + 1][0] < rows[i][0]:
            raise SpecLoadError('E-02',
                                f'波长轴非单调（第 {i + 1}→{i + 2} 点：'
                                f'{rows[i][0]} → {rows[i + 1][0]} Å），请自行排序后重试',
                                reason='non_monotonic')

    # V-9：重复 λ 聚合（有 σ 且全组 σ>0 用逆方差加权，否则等权；不整谱拒绝）
    lam, flux, ferr = [], [], []
    spreads = []
    i = 0
    n = len(rows)
    while i < n:
        j = i + 1
        while j < n and rows[j][0] == rows[i][0]:
            j += 1
        group = rows[i:j]
        if j - i > 1:
            stats['n_dup_lam'] += j - i - 1
            fvals = [g[1] for g in group]
            denom = max(abs(f) for f in fvals)
            if denom > 0:
                spreads.append((max(fvals) - min(fvals)) / denom)
        if all(len(g) > 2 and g[2] is not None and g[2] > 0 for g in group):
            w = [1.0 / (g[2] ** 2) for g in group]
            sw = sum(w)
            lam.append(group[0][0])
            flux.append(sum(wi * g[1] for wi, g in zip(w, group)) / sw)
            ferr.append(math.sqrt(1.0 / sw))
        elif all(len(g) > 2 for g in group):
            # P1b（F-110/T-78②③）：σ≤0 的列值**保留**进 ferr —— 逆方差聚合只认
            # σ>0（上行），但"列在而不可信"必须让 errcol_verdict 看到（all_zero/
            # negative 的判定域），V-8 的行宽语义（三列即有列）也由此成立。σ≤0
            # 像素对 σ 通路仍按缺 σ 处理（usable_err_column/_sigma_px_array 的
            # >0 闸不变）。组内取首行原值（可能是 0 / 负值）。
            lam.append(group[0][0])
            flux.append(sum(g[1] for g in group) / len(group))
            ferr.append(group[0][2])
        else:
            lam.append(group[0][0])
            flux.append(sum(g[1] for g in group) / len(group))
            ferr.append(None)
        i = j
    if stats['n_dup_lam']:
        stats['dup_lam_rel_spread'] = statistics.median(spreads) if spreads else None
        warnings.append(_warn('CA-10',
                              f"重复波长已按 λ 聚合（多余 {stats['n_dup_lam']} 行，"
                              f"聚合是有损操作）"))
    stats['n_points'] = len(lam)

    # V-8：has_err 由本次实际行宽逐行判定（不采信 extra_data.has_err）
    n_with_err = sum(1 for e in ferr if e is not None)
    stats['n_err_missing'] = len(ferr) - n_with_err
    if n_with_err == len(ferr) and ferr:
        stats['has_err'] = True
        stats['sigma_method'] = 'column'
        flux_err = ferr
    elif n_with_err == 0:
        stats['has_err'] = False
        stats['sigma_method'] = 'proxy'
        flux_err = None
    else:
        stats['has_err'] = False
        stats['sigma_method'] = 'mixed'
        flux_err = ferr

    # V-10：间隙（Δλ > C_GAP_FACTOR × 本文件中位 Δλ）
    if len(lam) > 2:
        dlam = [lam[k + 1] - lam[k] for k in range(len(lam) - 1)]
        med = statistics.median(dlam)
        if med > 0:
            stats['gaps'] = [[lam[k], lam[k + 1]]
                             for k, d in enumerate(dlam) if d > C_GAP_FACTOR * med]
        else:
            stats['gaps'] = []
    else:
        stats['gaps'] = []
    stats['n_gaps'] = len(stats['gaps'])

    # V-6：非正流量像素只计数回显（剔除发生在通带内积分时，不整体拒绝）
    stats['n_nonpos'] = sum(1 for f in flux if f <= 0)
    stats['nonpos_frac'] = (stats['n_nonpos'] / len(flux)) if flux else 0.0
    if stats['n_nonpos']:
        warnings.append(_warn('CA-10',
                              f"非正流量像素 {stats['n_nonpos']} 个"
                              f"（占 {stats['nonpos_frac']:.1%}），将在通带积分时局部剔除"))

    # V-17：流量量级带（F-14 第一道闸门的输入）
    mags = [abs(f) for f in flux if f != 0]
    stats['flux_median_cgs'] = statistics.median(mags) if mags else None
    stats['flux_scale_high_cluster'] = bool(
        stats['flux_median_cgs'] is not None
        and stats['flux_median_cgs'] > C_FLUX_MEDIAN_MAX_CGS)
    if stats['flux_scale_high_cluster']:
        warnings.append(_warn('CA-03',
                              f"流量中位数 |F| = {stats['flux_median_cgs']:.3g} 超出 cgs 合理域"
                              f"（> {C_FLUX_MEDIAN_MAX_CGS:.0e}）：疑似归一化或另有刻度"
                              f"（如 mJy/Jy），本模块不自动换算单位"))
    return lam, flux, flux_err


def _time_precision_d(raw):
    """V-15 + F-61：时刻精度只有两档 —— 天级 0.5（整数 MJD，实测 100/113）、
    秒级 1e-5（含小数，实测 13/113）。小数位数不再折算 10^-n。"""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if _to_float(s) is None:
        return None
    if '.' not in s:
        return 0.5                       # F-61：天级
    frac = s.split('.', 1)[1]
    if not frac.isdigit():
        return None
    return 1e-5                          # F-61：秒级


def _new_stats():
    return {'n_points': 0, 'n_dropped': 0, 'n_nonfinite': 0, 'n_dup_lam': 0,
            'dup_lam_rel_spread': None, 'n_nonpos': 0, 'nonpos_frac': 0.0,
            'gaps': [], 'n_gaps': 0, 'has_err': False, 'sigma_method': 'proxy',
            'n_err_missing': 0, 'flux_median_cgs': None,
            'flux_scale_high_cluster': False, 'n_skipped_header': 0}


def _assemble(source, spectrum_id, tid, lam, flux, flux_err, meta, provenance,
              stats, warnings, lambda_frame_converted, n_below_convert):
    return {
        'source': source,
        'spectrum_id': spectrum_id,
        'spec_hash': spec_hash(lam, flux),
        'tid': tid,
        'lam_aa': lam,
        'flux_flambda_cgs': flux,
        'flux_err': flux_err,
        'flux_median_cgs': stats['flux_median_cgs'],
        'meta': meta,
        'meta_provenance': provenance,
        'read_stats': stats,
        'warnings': warnings,
        'lambda_frame_converted': lambda_frame_converted,
        'n_below_convert': n_below_convert,
    }


# ─── 库内谱装载（V-1…V-17 + F-74/F-79①） ──────────────────────────────

def load_catalog_spectrum(payload, source_record=None):
    """从宿主 GET /api/spectra/<id> 的响应体装配 LoadedSpectrum。

    payload['data'] 的波长列已被宿主转成真空（F-79①）⇒ 本函数对波长零换算。
    source_record（可选）= 源记录 dict（ra / dec / redshift / category / tid），
    元数据优先级：谱文件内头键 > 源记录 > 兜底默认（F-74），逐项记 meta_provenance。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict) \
            or not payload['data']:
        raise SpecLoadError('E-01', '光谱不存在或文件不可读', status=404)
    data = payload['data']
    meta_rec = payload.get('meta') or {}
    warnings, stats = [], _new_stats()
    provenance = {}

    # V-2：一文件一谱；多对象只处理第一个
    names = list(data)
    if len(names) > 1:
        warnings.append(_warn('CA-10',
                              f'文件含 {len(names)} 个顶层对象，只处理第一个（{names[0]}）'))
    obj = data[names[0]]
    sp = obj.get('spectra') if isinstance(obj, dict) else None
    if not isinstance(sp, dict) or 'data' not in sp:
        raise SpecLoadError('E-01', '光谱文件缺少 spectra.data 段', status=404)

    # V-3/V-3a：与宿主 _coerce_spec_data 同口径清洗（float 化，失败丢弃计数）
    rows = []
    for d in sp['data']:
        try:
            lam = _to_float(d[0])
            flx = _to_float(d[1])
            if lam is None or flx is None:
                raise ValueError
            row = [lam, flx]
            if len(d) > 2 and d[2] not in (None, ''):
                err = _to_float(d[2])
                if err is None:
                    raise ValueError
                row.append(err)
            rows.append(row)
        except (TypeError, ValueError, IndexError, KeyError):
            stats['n_dropped'] += 1
    if stats['n_dropped']:
        warnings.append(_warn('CA-10', f"丢弃 {stats['n_dropped']} 个非数值数据行"))
    if not rows:
        raise SpecLoadError('E-01', '光谱文件无可用数据行', status=404)

    # V-14：波长单位键必须读并核对（实测 113/113 为 Angstrom）；缺键/非 Å ⇒ 拒
    u_wl = sp.get('u_wavelengths')
    if u_wl is None or 'angstrom' not in str(u_wl).strip().lower():
        raise SpecLoadError('E-14',
                            f'波长单位不可确认（u_wavelengths={u_wl!r}），不按 Å 猜',
                            reason='bad_wavelength_unit')

    lam, flux, flux_err = _normalize_rows(rows, stats, warnings)

    # F-79①：宿主已转真空 ⇒ lambda_frame='vacuum'、converted='host'；
    # wavelength_type 为 NULL 时该 vacuum 是宿主假定 ⇒ CA-34
    wavelength_type = meta_rec.get('wavelength_type')
    if wavelength_type in (None, ''):
        warnings.append(_warn('CA-34',
                              'wavelength_type 未声明：真空波长是宿主按空气假定的换算结果'))
        provenance['lambda_frame'] = 'default'
    else:
        provenance['lambda_frame'] = 'source'

    # V-16：已改银河消光的二级谱不得双改（跳过 F-25/F-59 的施加，由下游读这两个键）
    gext_corr = bool(sp.get('gext_corr') or meta_rec.get('gext_corr'))
    mw_corrected_ebv = None
    parent_filename = None
    if gext_corr:
        mw_corrected_ebv = _to_float(sp.get('gext_ebv'))
        if mw_corrected_ebv is None:
            mw_corrected_ebv = _to_float(meta_rec.get('gext_ebv'))
        parent_filename = sp.get('parent_filename')
        warnings.append(_warn('CA-30',
                              '本谱已做银河消光改正（gext_corr），不会重复施加'
                              + (f'；父谱 {parent_filename} 可改选' if parent_filename else '')))

    # V-15 + F-74：time / redshift / 类别，文件头键 > 源记录 > 默认
    src = source_record or {}
    mjd = _to_float(sp.get('time'))
    if mjd is not None:
        provenance['mjd'] = 'file'
        time_precision_d = _time_precision_d(sp.get('time'))
    else:
        mjd = _to_float(src.get('mjd'))
        if mjd is not None:
            provenance['mjd'] = 'source'
            time_precision_d = _time_precision_d(src.get('mjd'))
        else:
            # V-15：time 不可解析/缺失 ⇒ 时刻类比较降级为无实测时刻可比（不拒）
            mjd, time_precision_d = None, None
            provenance['mjd'] = 'default'

    z = _to_float(sp.get('redshift'))
    if z is not None:
        provenance['z'] = 'file'
    elif sp.get('redshift') not in (None, ''):
        warnings.append(_warn('CA-03', f"文件头 redshift={sp.get('redshift')!r} 无法数值化，已忽略"))
        z = _to_float(src.get('redshift'))
        provenance['z'] = 'source' if z is not None else 'default'
    else:
        z = _to_float(src.get('redshift'))
        provenance['z'] = 'source' if z is not None else 'default'
    if z is None:
        z = 0.0  # F-77：缺 z 取 0 按观测系处理（不拒）

    ra = _to_float(sp.get('ra'))
    dec = _to_float(sp.get('dec'))
    if ra is not None and dec is not None:
        provenance['ra_deg'] = provenance['dec_deg'] = 'file'
    else:
        ra = _to_float(src.get('ra'))
        dec = _to_float(src.get('dec'))
        provenance['ra_deg'] = provenance['dec_deg'] = 'source' if ra is not None and dec is not None else 'default'
    if ra is None or dec is None:
        warnings.append(_warn('CA-23', '缺坐标：银河消光只能按未改正出数'))
        ra = dec = None

    category = src.get('category') or 'other'
    provenance['category'] = 'source' if src.get('category') else 'default'

    # M-3：定标判定键只读文件侧（绝不读 extra_data —— 宿主两处默认会污染它，T-54）
    meta = {
        'ra_deg': ra, 'dec_deg': dec, 'z': z,
        'category': category,
        'mjd': mjd, 'time_precision_d': time_precision_d,
        'm_obs_kind': 'timed' if mjd is not None else 'no_time',
        'lambda_frame': 'vacuum',
        'already_gext_corrected': gext_corr,
        'law_used': 'pei1992' if gext_corr else None,
        'flux_type': sp.get('flux_type'),
        'u_fluxes': sp.get('u_fluxes'),
        'instrument': sp.get('instrument'),
        'name': names[0],
        'flux_unit_assumed': False,
    }
    if gext_corr:
        meta['mw_already_corrected'] = True
        meta['mw_corrected_ebv'] = mw_corrected_ebv
        meta['parent_filename'] = parent_filename
        meta['parent_id'] = meta_rec.get('parent_id')   # W-22：前端「↩ 父谱」链接
    stats['wavelength_type'] = wavelength_type

    return _assemble('catalog', meta_rec.get('id'), meta_rec.get('transient_id'),
                     lam, flux, flux_err, meta, provenance, stats, warnings,
                     'host', 0)


# ─── 上传/粘贴件装载（V-18…V-20 + F-75/F-77/F-79②） ───────────────────

_LAMBDA_FRAMES = ('vacuum', 'air', 'unknown')


def _norm_lambda_frame(v):
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    return s if s in _LAMBDA_FRAMES else None


def _load_upload_content(content, what='上传件'):
    """三种载体共用的空件/体积闸（F-76/Q-28：与请求体 32 MB 闸分列，超了是 413）。

    content: str（按 UTF-8 计字节）或 bytes（FITS 原始字节）。
    """
    if content is None:
        raise SpecLoadError('E-14', '内容为空', reason='empty')
    if isinstance(content, str):
        n_bytes = len(content.encode('utf-8'))
        if not content.strip():
            raise SpecLoadError('E-14', '内容为空', reason='empty')
    else:
        n_bytes = len(content)
        if n_bytes == 0:
            raise SpecLoadError('E-14', '内容为空', reason='empty')
    if n_bytes > C_MAX_UPLOAD_BYTES:
        raise SpecLoadError('E-07',
                            f'{what} {n_bytes} B 超过上限 {C_MAX_UPLOAD_BYTES} B',
                            reason='body_too_big')
    return n_bytes


def load_upload_text(text, lambda_frame=None, source='upload'):
    """上传/粘贴文本 → LoadedSpectrum（不落盘、不写库，F-75① / RO-5）。

    lambda_frame = 用户选择（U-39），头键 lambda_frame 优先于它（V-19）。
    source ∈ {'upload', 'paste'}。
    """
    if source not in ('upload', 'paste'):
        raise ValueError(f"source 只能是 'upload'/'paste'，收到 {source!r}")
    _load_upload_content(text, what='上传文本')
    if text.strip().startswith('{'):
        raise SpecLoadError('E-14', 'JSON 载体不受支持（只收文本网格/FITS/ECSV）',
                            reason='unsupported_format')

    headers, lineno_rows, n_skipped = parse_text_spectrum(text)
    warnings, stats = [], _new_stats()
    stats['n_skipped_header'] = n_skipped
    # V-18：列数全件一致（2 或 3）
    _check_uniform_width(lineno_rows)
    rows = [r for _, r, _ in lineno_rows]
    return _finalize_upload(rows, headers, stats, warnings,
                            lambda_frame=lambda_frame, source=source)

def _finalize_upload(rows, headers, stats, warnings, lambda_frame=None,
                     source='upload', u_wavelength=None):
    """三种载体（文本网格 F-75 / FITS / ECSV，F-76）共用的规范化管线。

    rows = [[lam, flux, err?], ...]（已 float 化、λ 已是 Å）；headers = 头键 dict
    （键小写，与文本路径的 # key: value 头同集）。u_wavelength 非空 ⇒ 该载体
    波长单位已在装载侧核对/换算成 Å（FITS TUNIT / ECSV col.unit），跳过文本
    路径的 wavelength_unit 头键闸（V-19），但 V-20 的 λ 合法域同判。
    """
    provenance = {}

    # 最少点数（= 宿主 MIN_POINTS，F-75；Q-26 三格式同判）
    if len(rows) < C_MIN_UPLOAD_POINTS:
        raise SpecLoadError('E-14',
                            f'数据点太少（{len(rows)} < {C_MIN_UPLOAD_POINTS}）',
                            reason='too_few_points')
    # V-20：λ 合法域（与宿主 spectra.py:32 同值同口径，越界拒而非静默裁剪）
    for r in rows:
        if not (WL_MIN < r[0] < WL_MAX):
            raise SpecLoadError('E-14',
                                f'波长 {r[0]} Å 超出合理范围 ({WL_MIN}-{WL_MAX} Å)',
                                reason='wavelength_out_of_range')

    # V-19：单位与元数据头键
    if u_wavelength is None:
        u_wl = headers.get('wavelength_unit') or headers.get('u_wavelengths')
        if u_wl is None:
            raise SpecLoadError('E-14', '缺 wavelength_unit 头键，不按 Å 猜',
                                reason='bad_wavelength_unit')
        if u_wl.strip().lower().replace(' ', '') not in ('aa', 'angstrom'):
            raise SpecLoadError('E-14',
                                f'wavelength_unit={u_wl!r} 不支持（只接受 AA/angstrom）',
                                reason='bad_wavelength_unit')
    flux_unit = headers.get('flux_unit') or headers.get('u_fluxes')
    flux_unit_assumed = flux_unit is None
    if flux_unit_assumed:
        flux_unit = DEFAULT_FLUX_UNIT

    def _num_header(*keys):
        """V-3a 头键数值化；失败 ⇒ 忽略该键 + CA-10（不拒整谱，V-19）。"""
        for k in keys:
            if k in headers:
                v = _to_float(headers[k])
                if v is None:
                    warnings.append(_warn('CA-10', f"头键 {k}={headers[k]!r} 无法数值化，已忽略"))
                    return None, False
                return v, True
        return None, False

    ra, ok_ra = _num_header('ra')
    dec, ok_dec = _num_header('dec')
    z, ok_z = _num_header('redshift')
    mjd, ok_mjd = _num_header('time', 'mjd')

    # F-79②：lambda_frame 头键 > 用户选择 > 默认 vacuum（假定 ⇒ CA-34）
    frame = _norm_lambda_frame(headers.get('lambda_frame'))
    if frame is not None:
        provenance['lambda_frame'] = 'file'
    elif headers.get('lambda_frame') is not None:
        warnings.append(_warn('CA-10',
                              f"lambda_frame={headers['lambda_frame']!r} 无法识别"
                              f"（取值 {list(_LAMBDA_FRAMES)}），已忽略"))
        frame = _norm_lambda_frame(lambda_frame)
        provenance['lambda_frame'] = 'user' if frame else 'default'
    else:
        frame = _norm_lambda_frame(lambda_frame)
        provenance['lambda_frame'] = 'user' if frame else 'default'
    if frame is None:
        frame = 'vacuum'

    lam, flux, flux_err = _normalize_rows(rows, stats, warnings)

    # F-79②：air ⇒ 换真空恰好一次（< CONVERT_MIN_A 的点不转，计 n_below_convert）
    lambda_frame_converted = None
    n_below_convert = 0
    if frame == 'air':
        out = []
        for x in lam:
            if x >= wavconvert.CONVERT_MIN_A:
                out.append(wavconvert.air_to_vacuum(x))
            else:
                out.append(x)
                n_below_convert += 1
        lam = out
        lambda_frame_converted = 'air_to_vac'
        if n_below_convert:
            warnings.append(_warn('CA-34',
                                  f'{n_below_convert} 个点低于 {wavconvert.CONVERT_MIN_A:.0f} Å '
                                  '未做空气→真空转换：本谱为混合帧'))
    elif frame == 'unknown':
        warnings.append(_warn('CA-34',
                              'lambda_frame=unknown：不做任何框架假设，'
                              '速度类输出（vel_* / z_fit）将被禁用'))
    elif provenance['lambda_frame'] == 'default':
        warnings.append(_warn('CA-34', 'lambda_frame 未声明：按 vacuum 处理（假定值）'))

    # F-77 缺省矩阵（功能仍在、只是该项不出数，禁止整谱拒绝）
    if not (ok_ra and ok_dec):
        ra = dec = None
        warnings.append(_warn('CA-23', '缺坐标：银河消光只能按未改正出数'))
    provenance['ra_deg'] = provenance['dec_deg'] = 'file' if (ok_ra and ok_dec) else 'default'
    provenance['z'] = 'file' if ok_z else 'default'
    provenance['mjd'] = 'file' if ok_mjd else 'default'
    provenance['category'] = 'default'

    meta = {
        'ra_deg': ra, 'dec_deg': dec,
        'z': z if ok_z else 0.0,
        'category': 'other',
        'mjd': mjd if ok_mjd else None,
        'time_precision_d': _time_precision_d(headers.get('time') or headers.get('mjd')) if ok_mjd else None,
        'm_obs_kind': 'timed' if ok_mjd else 'no_time',
        'lambda_frame': 'vacuum' if frame == 'air' else frame,
        'already_gext_corrected': False,
        'law_used': None,
        'flux_type': None,
        'u_fluxes': flux_unit,
        'instrument': headers.get('instrument'),
        'name': headers.get('name'),
        'flux_unit_assumed': flux_unit_assumed,
    }

    return _assemble(source, None, None, lam, flux, flux_err, meta, provenance,
                     stats, warnings, lambda_frame_converted, n_below_convert)


# ─── FITS / ECSV 上传解析（P2+，F-76 / T-81） ────────────────────────────
#
# F-76 纪律：多扩展 / 多列 / 带 WCS 而无法唯一判定 ⇒ E-14 点名待指定的扩展与
# 列名，绝不"猜最好的那个"；λ 单位换算一律走 astropy.units（禁行线 5，不自写
# 公式）；astropy 惰性 import（M-7 已确认在栈内，但文本路径不为其付 import 时延）。

_LAMBDA_NAME_HINTS = ('wave', 'lam', 'wl', 'disp')
_FLUX_NAME_HINTS = ('flux', 'flam', 'fnu', 'intens')
_ERR_NAME_HINTS = ('err', 'sigma', 'unc', 'noise', 'rms', 'dev')

# FITS/ECSV 头键别名 → 文本路径头键集（F-76：对齐文本路径的头键集；仅此两张
# 常见 FITS 惯用别名，登记的裁量），其余键一律按小写原名直查。
_HEADER_ALIASES = {'z': 'redshift', 'object': 'name'}


def _col_role(colname):
    """按列名给角色（err 优先 —— 'flux_err' 同时命中 flux 与 err 提示词）。"""
    n = colname.lower()
    if any(h in n for h in _ERR_NAME_HINTS):
        return 'err'
    if any(h in n for h in _LAMBDA_NAME_HINTS):
        return 'lam'
    if any(h in n for h in _FLUX_NAME_HINTS):
        return 'flux'
    return None


def _unit_role(unit_str):
    """按单位物理量类型给角色；解析失败/缺失 ⇒ None（名字提示词兜底）。"""
    if not unit_str:
        return None
    import astropy.units as u
    try:
        unit = u.Unit(str(unit_str).strip(), parse_strict='silent')
    except Exception:
        return None
    if unit is None or unit == u.dimensionless_unscaled:
        return None
    try:
        ptype = unit.physical_type
    except Exception:
        return None
    if ptype == 'length':
        return 'lam'
    if ptype in ('spectral flux density', 'spectral flux density wav'):
        return 'flux'
    return None


def _lam_to_angstrom(values, unit_str, what='波长列'):
    """λ → Å：一律走 astropy.units（禁行线 5）；不可解析/非长度量纲 ⇒ E-14。"""
    import astropy.units as u
    try:
        unit = u.Unit(str(unit_str).strip())
    except Exception:
        raise SpecLoadError('E-14',
                            f'{what}单位 {unit_str!r} 无法解析，不按 Å 猜',
                            reason='bad_wavelength_unit')
    if not unit.is_equivalent(u.angstrom):
        raise SpecLoadError('E-14',
                            f'{what}单位 {unit_str!r} 不是长度量纲，不按 Å 猜',
                            reason='bad_wavelength_unit')
    try:
        out = unit.to(u.angstrom, list(values))
    except Exception as exc:
        raise SpecLoadError('E-14',
                            f'{what}单位 {unit_str!r} → Å 换算失败: {exc}',
                            reason='bad_wavelength_unit')
    return [float(x) for x in out]


def _resolve_columns(colnames, units, col_lambda=None, col_flux=None, col_err=None):
    """λ/流量/误差列唯一判定（F-76：识别不出来 ⇒ E-14 点名列名，不猜）。

    col_* 为用户显式指定（E-14 的出口）；units: colname → TUNIT/col.unit 字符串。
    返回 (lam_name, flux_name, err_name_or_None)。
    """
    avail = ', '.join(colnames)
    if col_lambda is not None:
        if col_lambda not in colnames:
            raise SpecLoadError('E-14',
                                f'指定的波长列 {col_lambda!r} 不存在；可用列: {avail}',
                                reason='missing_column', details={'columns': colnames})
    if col_flux is not None:
        if col_flux not in colnames:
            raise SpecLoadError('E-14',
                                f'指定的流量列 {col_flux!r} 不存在；可用列: {avail}',
                                reason='missing_column', details={'columns': colnames})
    if col_err is not None:
        if col_err not in colnames:
            raise SpecLoadError('E-14',
                                f'指定的误差列 {col_err!r} 不存在；可用列: {avail}',
                                reason='missing_column', details={'columns': colnames})

    lam_c, flux_c, err_c = [], [], []
    for name in colnames:
        role = _col_role(name)
        if role is None:
            role = _unit_role(units.get(name))
        {'lam': lam_c, 'flux': flux_c, 'err': err_c}.get(role, []).append(name)

    def _pick(cands, what, param, explicit):
        if explicit is not None:
            return explicit
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            raise SpecLoadError(
                'E-14',
                f'{what}有多个候选（{", ".join(cands)}），请指定 {param}；'
                f'可用列: {avail}',
                reason='ambiguous_columns', details={'columns': cands})
        raise SpecLoadError('E-14',
                            f'无法识别{what}；可用列: {avail}',
                            reason='missing_column', details={'columns': colnames})

    lam_name = _pick(lam_c, '波长列', 'col_lambda', col_lambda)
    flux_name = _pick(flux_c, '流量列', 'col_flux', col_flux)
    # 误差列可缺（2 列），多个候选仍拒（不猜）；名字恰好与流量列同列时排除自身
    err_c = [c for c in err_c if c not in (lam_name, flux_name)]
    if col_err is not None:
        err_name = col_err
    elif len(err_c) == 1:
        err_name = err_c[0]
    elif len(err_c) > 1:
        raise SpecLoadError('E-14',
                            f'误差列有多个候选（{", ".join(err_c)}），请指定 col_err；'
                            f'可用列: {avail}',
                            reason='ambiguous_columns', details={'columns': err_c})
    else:
        err_name = None
    return lam_name, flux_name, err_name


def _headers_from_mapping(pairs):
    """FITS header / ECSV meta → 文本路径同集头键 dict（键小写、只留标量）。"""
    out = {}
    for k, v in pairs:
        if k in (None, '', 'COMMENT', 'HISTORY'):
            continue
        if isinstance(v, (str, int, float, bool)):
            out[str(k).strip().lower()] = v
    for alias, canonical in _HEADER_ALIASES.items():
        if alias in out and canonical not in out:
            out[canonical] = out[alias]
    return out


def load_upload_fits(data, lambda_frame=None, source='upload', hdu=None,
                     col_lambda=None, col_flux=None, col_err=None):
    """FITS 上传件 → LoadedSpectrum（P2+，F-76；解析验收 T-81）。

    两种形态：
      ① 表格 HDU（BINTABLE/TABLE）—— 列名/TUNIT 识别 λ/流量/误差列，λ 按 TUNIT
         经 astropy.units 换算成 Å；
      ② 一维谱 ImageHDU —— 线性 WCS（CTYPE1 波长型 + CRVAL1/CRPIX1/CDELT1|CD1_1
         + CUNIT1）生成波长轴，λ 按 CUNIT1 换算成 Å；非线形（LOG）或缺 WCS ⇒ E-14。
    多扩展 / 多列 / WCS 无法唯一判定 ⇒ E-14 点名扩展与列名（hdu/col_lambda/
    col_flux/col_err 是用户指定的出口）。头键映射对齐文本路径头键集。
    不落盘：astropy 只读内存 BytesIO（memmap=False），RO-5。
    """
    if source not in ('upload', 'paste'):
        raise ValueError(f"source 只能是 'upload'/'paste'，收到 {source!r}")
    _load_upload_content(data, what='FITS 上传件')
    headers, rows = _parse_fits(data, hdu=hdu, col_lambda=col_lambda,
                                col_flux=col_flux, col_err=col_err)
    warnings, stats = [], _new_stats()
    return _finalize_upload(rows, headers, stats, warnings,
                            lambda_frame=lambda_frame, source=source,
                            u_wavelength='angstrom')


def load_upload_ecsv(text, lambda_frame=None, source='upload'):
    """ECSV 上传件 → LoadedSpectrum（P2+，F-76；解析验收 T-81）。

    ECSV 是 ASCII 文本（YAML 头 + 定界数据），走 astropy.table.Table.read；
    λ 单位取列 unit（astropy.units 换算成 Å），头键取 YAML meta。判据与 FITS
    表格形态同一套（_resolve_columns）。
    """
    if source not in ('upload', 'paste'):
        raise ValueError(f"source 只能是 'upload'/'paste'，收到 {source!r}")
    _load_upload_content(text, what='ECSV 上传件')
    headers, rows = _parse_ecsv(text)
    warnings, stats = [], _new_stats()
    return _finalize_upload(rows, headers, stats, warnings,
                            lambda_frame=lambda_frame, source=source,
                            u_wavelength='angstrom')


def _parse_fits(data, hdu=None, col_lambda=None, col_flux=None, col_err=None):
    """FITS 字节 → (headers, rows)；rows 的 λ 已换算成 Å。"""
    from astropy.io import fits
    try:
        hdul = fits.open(io.BytesIO(bytes(data)), memmap=False)
    except Exception as exc:
        raise SpecLoadError('E-14', f'FITS 无法打开: {exc}', reason='bad_fits')
    try:
        data_hdus = [(i, h) for i, h in enumerate(hdul)
                     if h.data is not None and getattr(h.data, 'size', 0) > 0]
        if not data_hdus:
            raise SpecLoadError('E-14', 'FITS 内无含数据的 HDU',
                                reason='no_data_hdu')
        if hdu is not None:
            hit = None
            for i, h in data_hdus:
                if (isinstance(hdu, int) and i == hdu) or \
                        (isinstance(hdu, str) and h.name.upper() == hdu.strip().upper()):
                    hit = (i, h)
                    break
            if hit is None:
                names = [h.name or str(i) for i, h in data_hdus]
                raise SpecLoadError('E-14',
                                    f'指定的扩展 {hdu!r} 不存在；含数据的扩展: {", ".join(names)}',
                                    reason='bad_hdu', details={'hdus': names})
            idx, hduobj = hit
        elif len(data_hdus) > 1:
            names = [h.name or str(i) for i, h in data_hdus]
            raise SpecLoadError('E-14',
                                f'FITS 含 {len(data_hdus)} 个含数据的扩展'
                                f'（{", ".join(names)}），请指定 hdu 参数，不代选',
                                reason='ambiguous_hdu', details={'hdus': names})
        else:
            idx, hduobj = data_hdus[0]

        header = _headers_from_mapping(
            (c.keyword, c.value) for c in hduobj.header.cards)

        arr = hduobj.data
        if getattr(arr, 'names', None):                      # ① 表格 HDU（FITS_rec）
            return _rows_from_fits_table(
                hduobj, arr, col_lambda=col_lambda, col_flux=col_flux,
                col_err=col_err)
        import numpy as _np
        if isinstance(arr, _np.ndarray) and arr.ndim == 1:   # ② 一维谱 ImageHDU
            return _rows_from_fits_1d(hduobj, arr)
        raise SpecLoadError('E-14',
                            f'FITS 数据形态不支持（ndim={getattr(arr, "ndim", "?")}，'
                            '需表格 HDU 或一维谱）',
                            reason='unsupported_fits_shape')
    finally:
        hdul.close()


def _rows_from_fits_table(hduobj, arr, col_lambda=None, col_flux=None, col_err=None):
    """表格 HDU → 行数组；λ 按 TUNIT 经 astropy.units 换算成 Å。"""
    colnames = list(arr.names)
    tunits = {name: (col.unit or None) for name, col in zip(colnames, arr.columns)}
    lam_name, flux_name, err_name = _resolve_columns(
        colnames, tunits, col_lambda=col_lambda, col_flux=col_flux,
        col_err=col_err)
    header = _headers_from_mapping((c.keyword, c.value) for c in hduobj.header.cards)
    u_lam = tunits.get(lam_name)
    if not u_lam:
        raise SpecLoadError('E-14',
                            f'波长列 {lam_name} 无 TUNIT，不按 Å 猜（请用 col_lambda '
                            '指定波长列或补 TUNIT）',
                            reason='bad_wavelength_unit')
    lam = _lam_to_angstrom(arr[lam_name], u_lam, what=f'波长列 {lam_name}')
    flux = [float(x) for x in arr[flux_name]]
    if tunits.get(flux_name) and 'flux_unit' not in header:
        header['flux_unit'] = str(tunits[flux_name])
    rows = [[lam[i], flux[i]] for i in range(len(lam))]
    if err_name is not None:
        errs = [float(x) for x in arr[err_name]]
        rows = [rows[i] + [errs[i]] for i in range(len(rows))]
    return header, rows


def _rows_from_fits_1d(hduobj, arr):
    """一维谱 ImageHDU → 行数组；λ 由线性 WCS 生成（astropy.units 换算 Å）。"""
    header = _headers_from_mapping((c.keyword, c.value) for c in hduobj.header.cards)
    ctype = str(header.get('ctype1') or '').upper()
    if not ctype or 'WAVE' not in ctype:
        raise SpecLoadError('E-14',
                            '一维谱 FITS 缺波长型 WCS（CTYPE1 非 WAVE/AWAV 类），'
                            '无法确定波长轴；请改用表格形态（波长列 + TUNIT）',
                            reason='no_lambda_axis')
    if 'LOG' in ctype:
        raise SpecLoadError('E-14',
                            f'WCS 为对数波长轴（CTYPE1={ctype}），非线性不可唯一线性化，'
                            '请改用表格形态（波长列 + TUNIT）',
                            reason='nonlinear_wcs')
    crval = _to_float(header.get('crval1'))
    if crval is None:
        raise SpecLoadError('E-14', 'WCS 缺 CRVAL1，无法确定波长轴',
                            reason='no_lambda_axis')
    cdelt = _to_float(header.get('cdelt1'))
    if cdelt is None:
        cdelt = _to_float(header.get('cd1_1'))
    if cdelt is None:
        raise SpecLoadError('E-14', 'WCS 缺 CDELT1/CD1_1，无法确定波长采样',
                            reason='no_lambda_axis')
    crpix = _to_float(header.get('crpix1'))
    crpix = 1.0 if crpix is None else crpix
    cunit = header.get('cunit1')
    if not cunit:
        raise SpecLoadError('E-14', 'WCS 缺 CUNIT1，波长单位不可确认，不按 Å 猜',
                            reason='bad_wavelength_unit')
    lam = _lam_to_angstrom(
        [crval + (i + 1 - crpix) * cdelt for i in range(arr.size)],
        cunit, what='CUNIT1')
    if header.get('bunit'):
        header['flux_unit'] = header['bunit']
    return header, [[lam[i], float(arr[i])] for i in range(arr.size)]


def _parse_ecsv(text):
    """ECSV 文本 → (headers, rows)；λ 按列 unit 换算成 Å。"""
    from astropy.table import Table
    try:
        tbl = Table.read(io.BytesIO(text.encode('utf-8')), format='ascii.ecsv')
    except Exception as exc:
        raise SpecLoadError('E-14', f'ECSV 无法解析: {exc}', reason='bad_ecsv')
    header = _headers_from_mapping((k, v) for k, v in (tbl.meta or {}).items())
    colnames = tbl.colnames
    units = {name: (str(tbl[name].unit) if tbl[name].unit is not None else None)
             for name in colnames}
    lam_name, flux_name, err_name = _resolve_columns(colnames, units)
    u_lam = units.get(lam_name)
    if not u_lam:
        raise SpecLoadError('E-14',
                            f'波长列 {lam_name} 无单位元数据，不按 Å 猜',
                            reason='bad_wavelength_unit')
    lam = _lam_to_angstrom(tbl[lam_name].tolist(), u_lam, what=f'波长列 {lam_name}')
    flux = [float(x) for x in tbl[flux_name].tolist()]
    if units.get(flux_name):
        header.setdefault('flux_unit', units[flux_name])
    rows = [[lam[i], flux[i]] for i in range(len(lam))]
    if err_name is not None:
        errs = [float(x) for x in tbl[err_name].tolist()]
        rows = [rows[i] + [errs[i]] for i in range(len(rows))]
    return header, rows
