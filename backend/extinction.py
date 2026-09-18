"""
银河系消光改正（光学波段）

数据链路（对每个光变数据点，遵循 catadata/galaxy_extinction.py 的描述）：
  1. 数据库 flux_density 保存**原始值**：星等（flux_density_unit=mag）直接用，
     流量密度（mJy/uJy/Jy/cgs）先按 AB 零点转星等: mag = 16.4 - 2.5·log10(f_mJy)
  2. 若 mag_system == 'Vega':  AB星等 = 观测星等 + vega2ab（滤波器表）
  3. 查询 CSFD(2023) 尘埃图得 E(B-V)，Av = Rv·E(B-V)（Rv 固定 3.1），
     用 Pei (1992) 消光曲线计算该波段消光量 A_λ = Av · P92(λ)
  4. 改正后 AB 星等 = AB星等 - A_λ   （写入 mag_gextcor）
  5. 改正后 AB 星等转为 mJy:  flux = 3.631 × 10^(6 - mag/2.5)
     （写入 flux_density_gextcor，供余辉拟合等下游功能统一使用）

误差传播:
  σ_mag  = (2.5/ln10) · σ_flux / flux        （加减常数不改变星等误差）
  σ_flux = (ln10/2.5) · flux · σ_mag

依赖（burst_advocate conda 环境已安装）: astropy, dustmaps(CSFD), dust_extinction(P92)
CSFD 尘埃图首次使用前需 dustmaps.csfd.fetch()（本机已完成）。
"""
import math

from sqlalchemy import select

RV = 3.1                # 银河系平均 Rv，固定 3.1
LN10 = math.log(10)

# 流量单位 → mJy 换算因子
FLUX_UNIT_TO_MJY = {
    'mjy': 1.0, 'ujy': 1e-3, 'jy': 1e3,
    'cgs': 1e26, 'erg/cm2/s/hz': 1e26, 'cgs(erg/cm2/s/hz)': 1e26,
}
MAG_UNITS = ('mag', 'magnitude')

# ─── 惰性加载的重型依赖 ───
_csfd_query = None
_p92_model = None
_import_error = None


def _load():
    """惰性加载 astropy / dustmaps / dust_extinction（仅首次调用时）"""
    global _csfd_query, _p92_model, _import_error
    if _csfd_query is not None or _import_error is not None:
        return _import_error is None
    try:
        from dustmaps.csfd import CSFDQuery
        from dust_extinction.shapes import P92
        _csfd_query = CSFDQuery()
        _p92_model = P92()
        return True
    except Exception as e:  # 缺包 / 尘埃图未下载
        _import_error = str(e)
        _csfd_query = None
        return False


def status():
    """返回功能可用性与说明（供 API 状态查询）"""
    ok = _load()
    return {
        'available': ok,
        'error': None if ok else _import_error,
        'dust_map': 'CSFD (Liu et al. 2023)',
        'extinction_law': 'Pei (1992), Rv=3.1',
    }


# E(B-V) 进程内缓存：CSFD 尘埃图是静态数据，同一坐标（约 11m 精度分桶）
# 全目录只需查一次；消光批量改正与宿主统计共用
_ebv_cache = {}


def get_ebv(ra, dec):
    """查询 CSFD 尘埃图，返回 E(B-V)（mag）；结果按坐标缓存"""
    key = (round(float(ra), 4), round(float(dec), 4))
    if key not in _ebv_cache:
        from astropy.coordinates import SkyCoord
        import astropy.units as u
        _load()
        coords = SkyCoord(ra, dec, unit=(u.deg, u.deg), frame='icrs')
        _ebv_cache[key] = float(_csfd_query(coords))
    return _ebv_cache[key]


# ─── 消光系数 k_λ = A_λ/E(B-V) = Rv·P92(λ) ───
# P92 只在 1/λ ∈ x_range（缺省 [0.001, 1000] µm⁻¹，即 10 Å – 1e7 Å）内有定义；
# 域外波段（射电/X 射线）尘埃消光可忽略，系数取 0（= 不做改正）。
_coeff_cache = {}


def p92_wavelength_range():
    """P92 有效波长范围（Å，含端点），由模型 x_range（µm⁻¹）换算。"""
    lo, hi = getattr(_p92_model, 'x_range', (0.001, 1000.0))
    return 1.0e4 / float(hi), 1.0e4 / float(lo)


def dust_coeff(wavelength_a):
    """银河系尘埃消光系数 k = A_λ/E(B-V)（mag）。

    P92 域内 → Rv·P92(λ)；域外（射电/X 射线）→ 0.0（尘埃消光可忽略，
    即不做改正）；波长缺失/非法 → None（调用方应跳过）。按波长缓存
    （P92 数值求值约 0.4 ms/次，而全域滤波器只有几十个波长）。
    """
    import astropy.units as u
    try:
        wl = float(wavelength_a)
    except (TypeError, ValueError):
        return None
    if not (wl > 0):
        return None
    key = round(wl, 6)
    if key not in _coeff_cache:
        lo, hi = p92_wavelength_range()
        if not (lo <= wl <= hi):
            _coeff_cache[key] = 0.0        # 域外：尘埃消光可忽略，不做改正
        else:
            if _p92_model is None and not _load():
                return None                # 依赖不可用：不缓存，待可算时再试
            _coeff_cache[key] = float(RV * _p92_model(wl * u.Angstrom))
    return _coeff_cache[key]


def compute_alambda(ebv, wavelength_a=None, coeff=None):
    """消光量 A_λ = k·E(B-V)。

    k 优先取持久化的 filters.gext_coeff（免 P92 求值），否则由波长现算
    （dust_coeff）；两者都得 None 时返回 None（该点无法改正）。
    """
    if coeff is None:
        coeff = dust_coeff(wavelength_a)
    if coeff is None:
        return None
    return float(coeff) * float(ebv)


# ─── 滤波器元数据进程内缓存 ───
# correct_host_phot 等热路径过去每次都 sess.query(FilterDef).all()，要解码
# 129 行的 transmission JSONB（实测 76 ms/次，宿主统计里重复 299 次 ≈ 31.8 s）；
# 这里只取所需 4 列并做进程内缓存（带 TTL，兜底 CLI/脚本改库的情况）。
_filter_meta_cache = None       # (timestamp, {band: {...}})
_FILTER_META_TTL = 60.0         # 秒


def filter_meta(sess, refresh=False):
    """{band: {'wavelength','vega2ab','gext_coeff'}}（只取所需列，进程内缓存）。"""
    global _filter_meta_cache
    import time as _time
    now = _time.time()
    if (refresh or _filter_meta_cache is None
            or now - _filter_meta_cache[0] > _FILTER_META_TTL):
        from models import FilterDef
        _filter_meta_cache = (now, {
            r.id: {'wavelength': r.wavelength, 'vega2ab': r.vega2ab,
                   'gext_coeff': r.gext_coeff}
            for r in sess.execute(
                select(FilterDef.id, FilterDef.wavelength,
                       FilterDef.vega2ab, FilterDef.gext_coeff)).all()
        })
    return _filter_meta_cache[1]


def invalidate_filter_meta():
    """滤波器定义（波长/Vega2AB/系数）变更后调用，使进程内缓存失效。"""
    global _filter_meta_cache
    _filter_meta_cache = None


def refresh_ebv(row):
    """坐标建立/变更后刷新 row.gext_ebv（E(B-V) 缓存）。

    无坐标 → 置 None；尘埃图依赖不可用 → 保留旧值（不写坏数据）。
    只赋值、不 commit，由调用方负责。
    """
    ra = getattr(row, 'ra', None)
    dec = getattr(row, 'dec', None)
    if ra is None or dec is None:
        row.gext_ebv = None
        return None
    if not _load():
        return row.gext_ebv
    row.gext_ebv = get_ebv(ra, dec)
    return row.gext_ebv


# 光学/紫外/红外波长窗口（Å）：排除 P92 曲线不覆盖的射电与 X 射线波段
OPTICAL_WL_MIN_A = 1000.0
OPTICAL_WL_MAX_A = 1.0e7


def _optical_alambda(wavelength, ebv, coeff=None):
    """
    统一光学窗口判定 + A_λ 计算：
    波长缺失/非法/超出光学窗口(1000 Å–1e7 Å) → 返回 None，否则返回 A_λ
    （优先用持久化的 filters.gext_coeff，免 P92 求值）。
    """
    try:
        wl = float(wavelength)
    except (TypeError, ValueError):
        return None
    if wl <= 0 or not (OPTICAL_WL_MIN_A <= wl <= OPTICAL_WL_MAX_A):
        return None
    try:
        return compute_alambda(ebv, wl, coeff=coeff)
    except ValueError:
        return None


# ─── 单点改正 ───

def obs_mag(lc):
    """
    由原始数据得到观测星等及误差。
      星等数据（mag/magnitude）→ 直接使用原值（原星等系统由 mag_system 记录）
      流量数据（mJy/uJy/Jy/cgs）→ 按 AB 零点换算: mag = 16.4 - 2.5·log10(f_mJy)
    返回 (mag, mag_err)；无法处理（未知单位 / 流量非正）返回 None。
    """
    if lc.flux_density is None:
        return None
    unit = (lc.flux_density_unit or '').lower().strip()
    if unit in MAG_UNITS:
        mag_err = lc.flux_density_err if lc.flux_density_err and lc.flux_density_err > 0 else None
        return lc.flux_density, mag_err
    factor = FLUX_UNIT_TO_MJY.get(unit)
    if factor is None:
        return None
    flux_mjy = lc.flux_density * factor
    if flux_mjy <= 0:
        return None
    mag = 16.4 - 2.5 * math.log10(flux_mjy)
    mag_err = None
    if lc.flux_density_err is not None and lc.flux_density_err > 0:
        mag_err = (2.5 / LN10) * lc.flux_density_err / lc.flux_density
    return mag, mag_err


def correct_point(lc, alambda, vega2ab=0.0):
    """
    对单个 Lightcurve 行执行改正并写回字段（不 commit）。
    返回 True；数据不足/单位不支持返回 False。
    """
    om = obs_mag(lc)
    if om is None:
        return False
    mag_obs, mag_err = om
    # Vega → AB
    mag_ab = mag_obs + (vega2ab or 0.0) if (lc.mag_system or '').strip().lower() == 'vega' else mag_obs
    # 减去消光量
    mag_corr = mag_ab - alambda
    # 转为 mJy（改正后的统一流量列）
    flux_corr = 3.631 * 10 ** (6 - mag_corr / 2.5)
    flux_corr_err = (LN10 / 2.5) * flux_corr * mag_err if mag_err is not None else None

    lc.gext_corr = True
    lc.gext_Alambda = alambda
    lc.mag_gextcor = mag_corr
    lc.mag_gextcor_err = mag_err
    lc.flux_density_gextcor = flux_corr
    lc.flux_density_gextcor_err = flux_corr_err
    lc.flux_density_gextcor_unit = 'mJy'
    return True


def clear_point(lc):
    """清除单点的改正结果（不 commit）"""
    lc.gext_corr = False
    lc.gext_Alambda = None
    lc.mag_gextcor = None
    lc.mag_gextcor_err = None
    lc.flux_density_gextcor = None
    lc.flux_density_gextcor_err = None
    lc.flux_density_gextcor_unit = None


# ─── 批量执行 ───

def run(sess, transient_id=None, lightcurve_id=None):
    """
    执行银河系消光改正。
      transient_id=None 且 lightcurve_id=None → 全部源
      transient_id 指定 → 该源全部数据点
      lightcurve_id 指定 → 单个数据点
    返回统计信息 dict（调用方负责 commit）。
    """
    from models import Transient, Lightcurve

    if not _load():
        return {'ok': False, 'error': f'消光计算依赖不可用: {_import_error}'}

    # 滤波器元数据（进程内缓存，只取 id/wavelength/vega2ab/gext_coeff）
    filters = filter_meta(sess)

    q = sess.query(Lightcurve)
    if lightcurve_id is not None:
        q = q.filter(Lightcurve.id == lightcurve_id)
    elif transient_id is not None:
        q = q.filter(Lightcurve.transient_id == transient_id)
    rows = q.all()

    stats = {
        'ok': True,
        'total': len(rows),
        'corrected': 0,
        'skipped_no_coords': 0,
        'skipped_band': 0,
        'skipped_not_optical': 0,
        'skipped_flux': 0,
    }
    ebv_cache = {}   # transient_id → E(B-V)
    alam_cache = {}  # (transient_id, band) → A_λ

    # 一次性预取涉及源的坐标与缓存 E(B-V)（只取所需列，避免逐行 query）
    t_map = {r.id: r for r in sess.execute(
        select(Transient.id, Transient.ra, Transient.dec, Transient.gext_ebv)
        .where(Transient.id.in_({lc.transient_id for lc in rows}))).all()}

    for lc in rows:
        t = t_map.get(lc.transient_id)
        if t is None or t.ra is None or t.dec is None:
            stats['skipped_no_coords'] += 1
            continue
        if (lc.extra_data or {}).get('ext_corrected_by_source'):
            # 原作者已做过消光改正的数据（如 GRBSNWebtool 部分来源），不重复改正
            stats['skipped_flux'] += 1
            continue
        meta = filters.get(lc.band)
        if meta is None or not meta['wavelength'] or meta['wavelength'] <= 0:
            stats['skipped_band'] += 1
            continue
        if obs_mag(lc) is None:
            stats['skipped_flux'] += 1
            continue

        key = (lc.transient_id, lc.band)
        if key not in alam_cache:
            if lc.transient_id not in ebv_cache:
                ebv_cache[lc.transient_id] = (t.gext_ebv if t.gext_ebv is not None
                                              else get_ebv(t.ra, t.dec))
            alam_cache[key] = _optical_alambda(meta['wavelength'],
                                               ebv_cache[lc.transient_id],
                                               coeff=meta['gext_coeff'])
        if alam_cache[key] is None:
            stats['skipped_not_optical'] += 1
            continue

        if correct_point(lc, alam_cache[key], meta['vega2ab']):
            stats['corrected'] += 1
        else:
            stats['skipped_flux'] += 1

    if transient_id is not None and transient_id in ebv_cache:
        stats['ebv'] = ebv_cache[transient_id]
    return stats


# ─── 宿主星系测光改正（只算不写） ───

def correct_host_phot(sess, ra, dec, phot_rows, ebv=None):
    """
    宿主星系测光行的银河系消光改正计算（只算不写）。

    phot_rows: host_galaxies.photometry 的 JSONB 行 [{band, mag, gext_corr, ...}]。
    改正方向与光变点一致：改正后星等 = mag − A_λ（更亮；A_λ 是星等加性量，
    与 Vega→AB / ST→流量的换算可交换，故直接在原星等系统上减）。
    ebv: 调用方预先取到的 E(B-V)（如行上的 gext_ebv 缓存）；None 时按 (ra, dec)
         查 CSFD 尘图。A_λ 用持久化的 filters.gext_coeff（射电等域外波段为 0），
         因此 ebv + 系数齐备时本函数不再需要尘图/P92 依赖。
    返回 {'ok', 'error', 'ebv', 'rows'}；rows 与输入等长对齐，每项：
      applied   — True 表示该行标记为未改正（gext_corr 非真）且已成功计算
      A_lambda  — 该行波段的银消量（mag，射电等域外波段为 0）
      mag_corr  — 改正后星等（float(mag) − A_λ）
      reason    — 未改正的原因（already_corrected / bad_mag / band_no_wavelength /
                  not_optical（波长超出光学窗口或 P92 计算失败）/ not_dict）
    无坐标或依赖不可用（尘埃图缺失等）时整体 ok=False，调用方应回退原始值并注明。
    """
    def _fail(msg):
        return {'ok': False, 'error': msg, 'ebv': None,
                'rows': [{'applied': False, 'A_lambda': None, 'mag_corr': None,
                          'reason': 'unavailable'} for _ in (phot_rows or [])]}

    filters = filter_meta(sess)
    if ebv is None:
        if ra is None or dec is None:
            return _fail('缺少宿主/源坐标，无法查询尘埃图')
        if not _load():
            return _fail(f'消光计算依赖不可用: {_import_error}')
        ebv = get_ebv(ra, dec)

    alam_cache = {}   # band → A_λ
    rows = []
    for p in phot_rows or []:
        item = {'applied': False, 'A_lambda': None, 'mag_corr': None, 'reason': None}
        rows.append(item)
        if not isinstance(p, dict):
            item['reason'] = 'not_dict'
            continue
        if p.get('gext_corr', False):
            item['reason'] = 'already_corrected'   # 已改正数据原样使用
            continue
        band = p.get('band')
        try:
            mag = float(p.get('mag'))
        except (TypeError, ValueError):
            item['reason'] = 'bad_mag'
            continue
        meta = filters.get(band)
        if meta is None or not meta['wavelength'] or meta['wavelength'] <= 0:
            item['reason'] = 'band_no_wavelength'
            continue
        if band not in alam_cache:
            alm = _optical_alambda(meta['wavelength'], ebv, coeff=meta['gext_coeff'])
            if alm is None and meta['gext_coeff'] is None \
                    and not _load():
                item['reason'] = 'unavailable'
                continue
            alam_cache[band] = alm
        if alam_cache[band] is None:
            item['reason'] = 'not_optical'
            continue
        item['A_lambda'] = alam_cache[band]
        item['mag_corr'] = mag - alam_cache[band]
        item['applied'] = True
    return {'ok': True, 'error': None, 'ebv': ebv, 'rows': rows}


# ─── 数据变动时的自动重算（供其他路由调用） ───

def recompute_point(sess, lc):
    """
    单个数据点变动后调用：若该点已做改正则重算；条件不再满足（无坐标/波段不支持）
    则清除改正结果。调用方负责 commit。
    """
    if not lc.gext_corr:
        return
    from models import Transient, FilterDef
    t = sess.query(Transient).filter(Transient.id == lc.transient_id).first()
    filt = sess.query(FilterDef).filter(FilterDef.id == lc.band).first()
    if (t is None or t.ra is None or t.dec is None
            or obs_mag(lc) is None):
        clear_point(lc)
        return
    if not _load():
        return  # 依赖不可用时保留旧值，不破坏数据
    ebv = t.gext_ebv if t.gext_ebv is not None else get_ebv(t.ra, t.dec)
    alambda = _optical_alambda(filt.wavelength if filt is not None else None, ebv,
                               coeff=(filt.gext_coeff if filt is not None else None))
    if alambda is None:
        clear_point(lc)
        return
    correct_point(lc, alambda, filt.vega2ab)


def recompute_transient(sess, transient_id):
    """源坐标变动后：重算该源所有已改正的数据点；坐标被清除则全部清除。"""
    from models import Transient, Lightcurve
    rows = sess.query(Lightcurve).filter(
        Lightcurve.transient_id == transient_id,
        Lightcurve.gext_corr.is_(True),
    ).all()
    if not rows:
        return
    t = sess.query(Transient).filter(Transient.id == transient_id).first()
    if t is None or t.ra is None or t.dec is None:
        for lc in rows:
            clear_point(lc)
        return
    if not _load():
        return
    filters = filter_meta(sess)
    ebv = t.gext_ebv if t.gext_ebv is not None else get_ebv(t.ra, t.dec)
    alam_cache = {}
    for lc in rows:
        meta = filters.get(lc.band)
        if meta is None or obs_mag(lc) is None:
            clear_point(lc)
            continue
        if lc.band not in alam_cache:
            alam_cache[lc.band] = _optical_alambda(meta['wavelength'], ebv,
                                                   coeff=meta['gext_coeff'])
        if alam_cache[lc.band] is None:
            clear_point(lc)
            continue
        correct_point(lc, alam_cache[lc.band], meta['vega2ab'])


def recompute_band(sess, band):
    """滤波器定义（波长/Vega2AB）变动后：重算该波段所有已改正的数据点。"""
    from models import Transient, Lightcurve
    rows = sess.query(Lightcurve).filter(
        Lightcurve.band == band,
        Lightcurve.gext_corr.is_(True),
    ).all()
    if not rows:
        return
    # 滤波器元数据强制刷新（本函数由滤波器变更触发，且变更尚未 commit）
    meta = filter_meta(sess, refresh=True).get(band)
    if meta is None or not meta['wavelength'] or meta['wavelength'] <= 0:
        for lc in rows:
            clear_point(lc)
        return
    if not _load():
        return
    ebv_cache = {}
    # 一次性预取涉及源的坐标与缓存 E(B-V)（只取所需列，避免逐行 query）
    t_map = {r.id: r for r in sess.execute(
        select(Transient.id, Transient.ra, Transient.dec, Transient.gext_ebv)
        .where(Transient.id.in_({lc.transient_id for lc in rows}))).all()}
    for lc in rows:
        if obs_mag(lc) is None:
            clear_point(lc)
            continue
        t = t_map.get(lc.transient_id)
        if t is None or t.ra is None or t.dec is None:
            clear_point(lc)
            continue
        if lc.transient_id not in ebv_cache:
            ebv_cache[lc.transient_id] = (t.gext_ebv if t.gext_ebv is not None
                                          else get_ebv(t.ra, t.dec))
        alambda = _optical_alambda(meta['wavelength'], ebv_cache[lc.transient_id],
                                   coeff=meta['gext_coeff'])
        if alambda is None:
            clear_point(lc)
            continue
        correct_point(lc, alambda, meta['vega2ab'])


# ─── 光谱银河系消光改正（二级产物） ───

class SpectrumGextError(ValueError):
    """光谱银消改正的业务错误（已改正谱 / 无坐标 / 依赖不可用 / 数据不可用）"""


def correct_spectrum(sess, spectrum_row):
    """
    对一条原始光谱生成银河系消光改正谱（依附父谱的二级产物，幂等可重算覆盖）。

    改正口径与测光点一致：CSFD 尘埃图 E(B-V) + Pei(1992) 曲线 + Rv=3.1，
    在观测者系波长上逐点改正 f_corr = f_obs × 10^(+0.4·A_λ)
    （对 f_λ/f_ν/归一化流量同为乘性因子；波长列不变，误差列同乘）。

    子文件 <父文件名>_gextcor.json 复制父文件结构，仅替换 data 并补
    gext_corr/parent_filename/gext_ebv/gext_rv/gext_at 字段；
    DB 子行 parent_id 指向父行，幂等覆盖。

    spectrum_row: 父（原始）Spectrum ORM 行；调用方负责 commit。
    返回 {'child_row', 'warnings', 'ebv'}。
    业务错误抛 SpectrumGextError，文件缺失抛 FileNotFoundError。
    """
    import json, os
    from datetime import datetime, timezone
    import astropy.units as u
    from models import Transient, Spectrum

    if spectrum_row.parent_id is not None:
        raise SpectrumGextError('该光谱已是银河系消光改正谱，不能对其再次改正（请对其原始谱执行改正）')
    if not _load():
        raise SpectrumGextError(f'消光计算依赖不可用: {_import_error}')

    t = sess.query(Transient).filter(Transient.id == spectrum_row.transient_id).first()
    if t is None or t.ra is None or t.dec is None:
        raise SpectrumGextError('该暂现源缺少坐标（RA/Dec），无法查询尘埃图')
    ebv = get_ebv(t.ra, t.dec)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent_abs = os.path.normpath(os.path.join(project_root, spectrum_row.file_path))
    if not os.path.exists(parent_abs):
        raise FileNotFoundError(f'光谱文件缺失: {spectrum_row.file_path}')
    with open(parent_abs) as f:
        payload = json.load(f)
    obj_name, obj = next(iter(payload.items()))
    sp = obj.get('spectra', obj) if isinstance(obj, dict) else None
    if not isinstance(sp, dict) or 'data' not in sp:
        raise SpectrumGextError('光谱文件缺少 spectra.data 字段')

    # 存量文件数值可能是字符串，统一 float 化
    points = []
    for d in sp['data']:
        try:
            row = [float(d[0]), float(d[1])]
            if len(d) > 2 and d[2] not in (None, ''):
                row.append(float(d[2]))
            points.append(row)
        except (TypeError, ValueError, IndexError):
            continue
    if not points:
        raise SpectrumGextError('光谱无可用数据点')

    wavs = [p[0] for p in points]
    # P92().extinguish(x, Av) 返回剩余流量比例 10^(-0.4·A_λ)
    try:
        factors = _p92_model.extinguish(u.Quantity(wavs, u.Angstrom), Av=RV * ebv)
    except ValueError:
        raise SpectrumGextError(
            f'光谱波长范围（{min(wavs):.4g}–{max(wavs):.4g} Å）超出 P92 消光曲线有效范围'
            '（10 Å–1 mm），无法执行银河系消光改正')
    corrected = []
    for p, fac in zip(points, factors):
        row = [p[0], p[1] / fac]
        if len(p) > 2:
            row.append(p[2] / fac)
        corrected.append(row)

    warnings = []
    uf = (sp.get('u_fluxes') or '').lower()
    if 'normalized' in uf or 'uncalibrated' in uf:
        warnings.append('该光谱流量为归一化/未校准，改正仅改变谱形，不代表绝对流量定标')

    parent_fn = spectrum_row.filename
    child_fn = parent_fn + '_gextcor'
    child_rel = spectrum_row.file_path.rsplit('/', 1)[0] + '/' + child_fn + '.json'
    child_abs = os.path.join(os.path.dirname(parent_abs), child_fn + '.json')

    child_payload = json.loads(json.dumps(payload))   # 深拷贝父文件结构
    cobj = child_payload[obj_name]
    csp = cobj.get('spectra', cobj) if isinstance(cobj, dict) else cobj
    csp['data'] = corrected
    csp['filename'] = child_fn
    csp['gext_corr'] = True
    csp['parent_filename'] = parent_fn
    csp['gext_ebv'] = ebv
    csp['gext_rv'] = str(RV)
    csp['gext_at'] = datetime.now(timezone.utc).isoformat()

    extra = dict(spectrum_row.extra_data or {})
    extra.update({'gext_corr': True, 'gext_ebv': ebv, 'gext_rv': str(RV),
                  'parent_filename': parent_fn, 'n_points': len(corrected),
                  'source': 'gext_correction'})

    child = sess.query(Spectrum).filter(Spectrum.parent_id == spectrum_row.id).first()
    if child is None:
        child = Spectrum(transient_id=spectrum_row.transient_id,
                         parent_id=spectrum_row.id, file_type='json')
        sess.add(child)
    # 元数据跟随父行（幂等覆盖时一并刷新）
    child.filename = child_fn
    child.wavelength_min = min(wavs)
    child.wavelength_max = max(wavs)
    child.instrument = spectrum_row.instrument
    child.observation_date = spectrum_row.observation_date
    child.file_path = child_rel
    child.spec_type = spectrum_row.spec_type
    child.extra_data = extra

    with open(child_abs, 'w') as f:
        json.dump(child_payload, f)
    return {'child_row': child, 'warnings': warnings, 'ebv': ebv}
