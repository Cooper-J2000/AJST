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


def get_ebv(ra, dec):
    """查询 CSFD 尘埃图，返回 E(B-V)（mag）"""
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    _load()
    coords = SkyCoord(ra, dec, unit=(u.deg, u.deg), frame='icrs')
    return float(_csfd_query(coords))


def compute_alambda(ebv, wavelength_a):
    """由 E(B-V) 和有效波长(Å)计算消光量 A_λ = Rv·E(B-V)·P92(λ)"""
    import astropy.units as u
    av = RV * ebv
    return float(av * _p92_model(wavelength_a * u.Angstrom))


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
    from models import Transient, Lightcurve, FilterDef

    if not _load():
        return {'ok': False, 'error': f'消光计算依赖不可用: {_import_error}'}

    # 滤波器表（光学/红外波段，不在表中的波段如 keV/GHz 不做改正）
    filters = {f.id: f for f in sess.query(FilterDef).all()}

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
        'skipped_flux': 0,
    }
    ebv_cache = {}   # transient_id → E(B-V)
    alam_cache = {}  # (transient_id, band) → A_λ

    for lc in rows:
        t = sess.query(Transient).filter(Transient.id == lc.transient_id).first()
        if t is None or t.ra is None or t.dec is None:
            stats['skipped_no_coords'] += 1
            continue
        if (lc.extra_data or {}).get('ext_corrected_by_source'):
            # 原作者已做过消光改正的数据（如 GRBSNWebtool 部分来源），不重复改正
            stats['skipped_flux'] += 1
            continue
        filt = filters.get(lc.band)
        if filt is None or filt.wavelength is None or filt.wavelength <= 0:
            stats['skipped_band'] += 1
            continue
        if obs_mag(lc) is None:
            stats['skipped_flux'] += 1
            continue

        key = (lc.transient_id, lc.band)
        if key not in alam_cache:
            if lc.transient_id not in ebv_cache:
                ebv_cache[lc.transient_id] = get_ebv(t.ra, t.dec)
            alam_cache[key] = compute_alambda(ebv_cache[lc.transient_id], filt.wavelength)

        if correct_point(lc, alam_cache[key], filt.vega2ab):
            stats['corrected'] += 1
        else:
            stats['skipped_flux'] += 1

    if transient_id is not None and transient_id in ebv_cache:
        stats['ebv'] = ebv_cache[transient_id]
    return stats


# ─── 宿主星系测光改正（只算不写） ───

def correct_host_phot(sess, ra, dec, phot_rows):
    """
    宿主星系测光行的银河系消光改正计算（复用 CSFD+P92 查询，不写库）。

    phot_rows: host_galaxies.photometry 的 JSONB 行 [{band, mag, gext_corr, ...}]。
    改正方向与光变点一致：改正后星等 = mag − A_λ（更亮；A_λ 是星等加性量，
    与 Vega→AB / ST→流量的换算可交换，故直接在原星等系统上减）。
    返回 {'ok', 'error', 'ebv', 'rows'}；rows 与输入等长对齐，每项：
      applied   — True 表示该行标记为未改正（gext_corr 非真）且已成功计算
      A_lambda  — 该行波段的银消量（mag）
      mag_corr  — 改正后星等（float(mag) − A_λ）
      reason    — 未改正的原因（already_corrected / bad_mag / band_no_wavelength / not_dict）
    无坐标或依赖不可用（尘埃图缺失等）时整体 ok=False，调用方应回退原始值并注明。
    """
    def _fail(msg):
        return {'ok': False, 'error': msg, 'ebv': None,
                'rows': [{'applied': False, 'A_lambda': None, 'mag_corr': None,
                          'reason': 'unavailable'} for _ in (phot_rows or [])]}

    if ra is None or dec is None:
        return _fail('缺少宿主/源坐标，无法查询尘埃图')
    if not _load():
        return _fail(f'消光计算依赖不可用: {_import_error}')

    from models import FilterDef
    filters = {f.id: f for f in sess.query(FilterDef).all()}
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
        filt = filters.get(band)
        if filt is None or not filt.wavelength or filt.wavelength <= 0:
            item['reason'] = 'band_no_wavelength'
            continue
        if band not in alam_cache:
            alam_cache[band] = compute_alambda(ebv, filt.wavelength)
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
            or filt is None or filt.wavelength is None or filt.wavelength <= 0
            or obs_mag(lc) is None):
        clear_point(lc)
        return
    if not _load():
        return  # 依赖不可用时保留旧值，不破坏数据
    alambda = compute_alambda(get_ebv(t.ra, t.dec), filt.wavelength)
    correct_point(lc, alambda, filt.vega2ab)


def recompute_transient(sess, transient_id):
    """源坐标变动后：重算该源所有已改正的数据点；坐标被清除则全部清除。"""
    from models import Transient, Lightcurve, FilterDef
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
    filters = {f.id: f for f in sess.query(FilterDef).all()}
    ebv = get_ebv(t.ra, t.dec)
    alam_cache = {}
    for lc in rows:
        filt = filters.get(lc.band)
        if filt is None or filt.wavelength is None or filt.wavelength <= 0 \
                or obs_mag(lc) is None:
            clear_point(lc)
            continue
        if lc.band not in alam_cache:
            alam_cache[lc.band] = compute_alambda(ebv, filt.wavelength)
        correct_point(lc, alam_cache[lc.band], filt.vega2ab)


def recompute_band(sess, band):
    """滤波器定义（波长/Vega2AB）变动后：重算该波段所有已改正的数据点。"""
    from models import Transient, Lightcurve, FilterDef
    rows = sess.query(Lightcurve).filter(
        Lightcurve.band == band,
        Lightcurve.gext_corr.is_(True),
    ).all()
    if not rows:
        return
    filt = sess.query(FilterDef).filter(FilterDef.id == band).first()
    if filt is None or filt.wavelength is None or filt.wavelength <= 0:
        for lc in rows:
            clear_point(lc)
        return
    if not _load():
        return
    ebv_cache = {}
    for lc in rows:
        if obs_mag(lc) is None:
            clear_point(lc)
            continue
        t = sess.query(Transient).filter(Transient.id == lc.transient_id).first()
        if t is None or t.ra is None or t.dec is None:
            clear_point(lc)
            continue
        if lc.transient_id not in ebv_cache:
            ebv_cache[lc.transient_id] = get_ebv(t.ra, t.dec)
        correct_point(lc, compute_alambda(ebv_cache[lc.transient_id], filt.wavelength),
                       filt.vega2ab)
