"""拟合任务系统。

- 单 worker 线程池串行执行（MCMC 重负载，排队即可）。
- 任务记录落在 fitting_results 表：
    model_name  = 'vegas_unified:<case>'（历史任务为 'vegas_fs:<jet>-<medium>'）
    parameters  = 最佳参数 {名: {v, err}}
    chi_squared = chi2（未除 dof）
    extra_data  = {engine, config, status, error, runtime_s, dof, bic, aic,
                   warnings, files: {h5, corner, lc_model}, created_by}
    status ∈ pending | running | done | failed | interrupted
- 产物文件存 backend/fitting_store/<transient_id>/<job_id>/。
"""
import math
import os
import re
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor

from app import get_session
from models import Transient, Lightcurve, FilterDef, FittingResult
from fitting.engines import get_engine

_STORE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'fitting_store')

# 单 worker 串行队列
_pool = ThreadPoolExecutor(max_workers=1)

# 流量单位 → mJy 换算因子（与 extinction.py 保持一致）
_FLUX_UNIT_TO_MJY = {
    'mjy': 1.0, 'ujy': 1e-3, 'jy': 1e3,
    'cgs': 1e26, 'erg/cm2/s/hz': 1e26, 'cgs(erg/cm2/s/hz)': 1e26,
}
# 时间单位 → s
_TIME_UNIT_TO_S = {'s': 1.0, 'min': 60.0, 'hr': 3600.0, 'h': 3600.0,
                   'd': 86400.0, 'day': 86400.0}
_C_AA_PER_S = 2.99792458e18  # 光速 [Å/s]，ν = c / λ(Å)
_LN10 = math.log(10)

# 波段名中的频率/能量标注：'4.8GHz' / '250MHz'（射电频率）或 '10keV'（X 射线光子能量）→ ν [Hz]
_FREQ_BAND_RE = re.compile(
    r'^\s*(\d+(?:\.\d+)?)\s*(Hz|kHz|MHz|GHz|THz|eV|keV|MeV|GeV)\s*$', re.IGNORECASE)
_FREQ_UNIT_TO_HZ = {'hz': 1.0, 'khz': 1e3, 'mhz': 1e6, 'ghz': 1e9, 'thz': 1e12}
# 光子能量 → 频率：ν = E / h（h = 4.135667696e-15 eV·s）
_ENERGY_UNIT_TO_HZ = {'ev': 2.417989242e14, 'kev': 2.417989242e17,
                      'mev': 2.417989242e20, 'gev': 2.417989242e23}


def _parse_freq_band(band):
    """从波段名解析频率（Hz）：射电为频率标注（'4.8GHz'），X 射线为光子能量标注
    （'10keV'，按 ν = E/h 换算）；都不是则返回 None"""
    if not band:
        return None
    m = _FREQ_BAND_RE.match(str(band))
    if not m:
        return None
    unit = m.group(2).lower()
    factor = _ENERGY_UNIT_TO_HZ.get(unit) or _FREQ_UNIT_TO_HZ[unit]
    return float(m.group(1)) * factor


# ─── 数据准备 ───

def prepare_data(transient_id, selection=None):
    """从 Lightcurve 表取某源的拟合用数据。

    优先银消改正列（gext_corr=true 时 flux_density_gextcor，单位 mJy），
    否则原始 flux_density 换算（mag→mJy 用 AB 零点 16.4；Vega 星等先加
    Filter 表 Vega2AB；mJy/uJy/Jy/cgs 按单位换算）。
    上限点按 dataloader 惯例 f=0、err=上限值；排除 discard；
    误差缺失/为 0 的探测点跳过。
    selection（可选的用户数据选取）: {'bands': [波段名...], 'tmin': 秒|None,
      'tmax': 秒|None, 'band_ranges': {波段: [tmin, tmax]}（精细时段，优先于
      全局 tmin/tmax）, 'exclude_ids': [lightcurve id...]}，缺省=不限制。
    返回:
      {z, bands: [{band, nu, t[], f[], ferr[], weights[], is_ul[]}],
       warnings: [...], n_points}
    """
    selection = selection or {}
    sel_bands = set(selection.get('bands') or []) or None
    sel_tmin, sel_tmax = selection.get('tmin'), selection.get('tmax')
    # 分波段精细时段：{'R': [tmin, tmax], ...}，未设的波段用全局 sel_tmin/sel_tmax
    band_ranges = selection.get('band_ranges') or {}
    sel_excl = set(selection.get('exclude_ids') or [])
    sess = get_session()
    try:
        t = sess.get(Transient, transient_id)
        if t is None:
            raise ValueError(f'暂现源不存在: {transient_id}')
        if t.redshift is None:
            raise ValueError(f'{transient_id} 缺少红移，无法拟合')
        filters = {f.id: f for f in sess.query(FilterDef).all()}
        rows = (sess.query(Lightcurve)
                .filter_by(transient_id=transient_id, discard=False)
                .order_by(Lightcurve.time).all())

        warnings = []
        bands = {}  # band -> 累积字典
        skip_no_filter, skip_bad_t, skip_bad_flux, skip_no_err = set(), 0, 0, 0
        n_gext, n_raw = 0, 0  # 银消改正/未改正点数统计
        n_sel_skip = 0        # 用户数据选取剔除的点数

        for lc in rows:
            # 用户数据选取：单点排除 / 波段 / 时段
            if lc.id in sel_excl or (sel_bands is not None and lc.band not in sel_bands):
                n_sel_skip += 1
                continue
            filt = filters.get(lc.band)
            if filt is not None and filt.wavelength:
                nu = _C_AA_PER_S / filt.wavelength
            else:
                # filters 表无定义：尝试从波段名解析频率（射电 '4.8GHz' / X 射线 '10keV'）
                nu = _parse_freq_band(lc.band)
                if nu is None:
                    skip_no_filter.add(lc.band)
                    continue
            # 时间 → s，模型要求 t > 0
            factor = _TIME_UNIT_TO_S.get((lc.time_unit or 's').lower())
            t_s = lc.time * factor if factor else None
            if t_s is None or t_s <= 0:
                skip_bad_t += 1
                continue
            # 时段：该波段的精细范围优先，否则用全局范围
            bmin, bmax = band_ranges.get(lc.band, (None, None))
            eff_tmin = bmin if bmin is not None else sel_tmin
            eff_tmax = bmax if bmax is not None else sel_tmax
            if (eff_tmin is not None and t_s < eff_tmin) or (eff_tmax is not None and t_s > eff_tmax):
                n_sel_skip += 1
                continue
            # 流量 → mJy：优先银消改正列
            if lc.gext_corr and lc.flux_density_gextcor is not None:
                f_mjy = lc.flux_density_gextcor
                ferr_mjy = lc.flux_density_gextcor_err
                n_gext += 1
            else:
                conv = _raw_to_mjy(lc, filt)
                if conv is None:
                    skip_bad_flux += 1
                    continue
                f_mjy, ferr_mjy = conv
                n_raw += 1

            if lc.upperlimit:
                # 上限点：f=0，err=上限流量本身
                if f_mjy is None or f_mjy <= 0:
                    skip_bad_flux += 1
                    continue
                f_mjy, ferr_mjy = 0.0, f_mjy
            else:
                if f_mjy is None or f_mjy <= 0:
                    skip_bad_flux += 1
                    continue
                if ferr_mjy is None or ferr_mjy <= 0:
                    skip_no_err += 1
                    continue

            b = bands.setdefault(lc.band, {
                'band': lc.band, 'nu': nu,
                't': [], 'f': [], 'ferr': [], 'weights': [], 'is_ul': []})
            b['t'].append(float(t_s))
            b['f'].append(float(f_mjy))
            b['ferr'].append(float(ferr_mjy))
            b['weights'].append(float(lc.weights if lc.weights is not None else 1.0))
            b['is_ul'].append(bool(lc.upperlimit))

        if skip_no_filter:
            warnings.append(f'filters 表无定义，已跳过波段: {sorted(skip_no_filter)}')
        if n_sel_skip:
            parts = []
            if sel_bands is not None:
                parts.append(f'波段 {sorted(sel_bands)}')
            if sel_tmin is not None or sel_tmax is not None:
                parts.append(f'全局时段 [{sel_tmin if sel_tmin is not None else "−∞"}, {sel_tmax if sel_tmax is not None else "+∞"}]s')
            if band_ranges:
                rng = ', '.join(f'{b}:[{r[0] if r[0] is not None else "−∞"},{r[1] if r[1] is not None else "+∞"}]'
                                for b, r in sorted(band_ranges.items()))
                parts.append(f'分波段时段 {rng}')
            if sel_excl:
                parts.append(f'单点排除 {len(sel_excl)} 个')
            warnings.append(f'数据选取（{"，".join(parts)}）：剔除 {n_sel_skip} 点')
        if n_raw:
            warnings.append(
                f'{n_raw} 个点未做银河系消光改正（使用原始流量；{n_gext} 个点已用银消改正值）。'
                f'如需全部改正，请先在详情页执行"银消改正"')
        if skip_bad_t:
            warnings.append(f'{skip_bad_t} 点时间非法（t<=0 或未知时间单位），已跳过')
        if skip_bad_flux:
            warnings.append(f'{skip_bad_flux} 点流量非法/单位不支持，已跳过')
        if skip_no_err:
            warnings.append(f'{skip_no_err} 个探测点误差缺失或为 0，已跳过')

        band_list = [b for b in bands.values() if b['t']]
        n_points = sum(len(b['t']) for b in band_list)
        return {'z': float(t.redshift), 'bands': band_list,
                'warnings': warnings, 'n_points': n_points}
    finally:
        sess.close()


def _raw_to_mjy(lc, filt):
    """原始（未银消改正）flux_density → (f_mjy, ferr_mjy)；无法换算返回 None"""
    unit = (lc.flux_density_unit or '').strip().lower()
    if unit in ('mag', 'magnitude'):
        mag = lc.flux_density
        if (lc.mag_system or '').strip().lower() == 'vega':
            mag = mag + ((filt.vega2ab or 0.0) if filt else 0.0)  # Vega → AB
        f_mjy = 10.0 ** ((16.4 - mag) / 2.5)   # AB 零点 16.4
        ferr_mjy = None
        if lc.flux_density_err and lc.flux_density_err > 0:
            ferr_mjy = (_LN10 / 2.5) * f_mjy * lc.flux_density_err
        return f_mjy, ferr_mjy
    factor = _FLUX_UNIT_TO_MJY.get(unit)
    if factor is None or lc.flux_density is None:
        return None
    f_mjy = lc.flux_density * factor
    ferr_mjy = lc.flux_density_err * factor if lc.flux_density_err else None
    return f_mjy, ferr_mjy


# ─── 任务生命周期 ───

def job_dir(transient_id, job_id):
    return os.path.join(_STORE_ROOT, str(transient_id), str(job_id))


def _set_status(sess, row, status, **extra):
    """更新 extra_data（JSONB 需整体重赋值才会被跟踪）"""
    ed = dict(row.extra_data or {})
    ed['status'] = status
    ed.update(extra)
    row.extra_data = ed
    sess.commit()


def create_job(transient_id, engine_name, config, warnings=None, created_by=None):
    """建任务（pending）并入队，返回任务 id。调用方需已完成校验。"""
    engine = get_engine(engine_name)
    model_name = engine.model_label(config) or \
        f"{engine_name}:{config.get('jet', 'tophat')}-{config.get('medium', 'ism')}"
    sess = get_session()
    try:
        row = FittingResult(
            transient_id=transient_id,
            model_name=model_name,
            parameters={},
            chi_squared=None,
            extra_data={
                'engine': engine_name,
                'config': config,
                'status': 'pending',
                'error': None,
                'runtime_s': None,
                'dof': None, 'bic': None, 'aic': None,
                'warnings': warnings or [],
                'files': {},
                'created_by': created_by,
            })
        sess.add(row)
        sess.commit()
        job_id = row.id
    finally:
        sess.close()
    _pool.submit(_run_job, job_id)
    return job_id


def _run_job(job_id):
    """worker：pending → running → done/failed"""
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None:
            return
        ed = row.extra_data or {}
        engine = get_engine(ed.get('engine'))
        if engine is None:
            _set_status(sess, row, 'failed', error=f"未知引擎: {ed.get('engine')}")
            return
        _set_status(sess, row, 'running')
        transient_id = row.transient_id
        workdir = job_dir(transient_id, job_id)
        try:
            data = prepare_data(transient_id, (ed.get('config') or {}).get('data_selection'))
            if data['n_points'] == 0:
                raise ValueError('无可用数据点')
            result = engine.run(ed.get('config') or {}, data, workdir, log=None)
            files = {}
            for kind, fname in (('h5', 'chain_record.h5'), ('corner', 'corner.png'),
                                ('lc_model', 'lc_model.json'),
                                ('lc_plot', 'lc_plot.png'),
                                ('lc_ratio', 'lc_ratio_plot.png'),
                                ('metrics', 'metrics.txt')):
                if os.path.exists(os.path.join(workdir, fname)):
                    files[kind] = fname
            row.parameters = result['params']
            row.chi_squared = result['chi2']
            _set_status(sess, row, 'done',
                        runtime_s=result['runtime_s'],
                        dof=result['dof'], bic=result['bic'], aic=result['aic'],
                        warnings=data['warnings'], files=files, error=None)
        except Exception as e:
            # 失败详情同时落 run.log
            os.makedirs(workdir, exist_ok=True)
            with open(os.path.join(workdir, 'run.log'), 'a', encoding='utf-8') as lf:
                lf.write('\n===== 任务失败 =====\n' + traceback.format_exc())
            _set_status(sess, row, 'failed', error=str(e))
    finally:
        sess.close()


def mark_interrupted():
    """服务启动时调用：残留的 running/pending 一律标记 interrupted"""
    sess = get_session()
    try:
        n = 0
        for row in sess.query(FittingResult).all():
            ed = row.extra_data or {}
            if ed.get('status') in ('running', 'pending'):
                ed = dict(ed)
                ed['status'] = 'interrupted'
                ed['error'] = ed.get('error') or '服务重启，任务中断'
                row.extra_data = ed
                n += 1
        if n:
            sess.commit()
        return n
    finally:
        sess.close()


def delete_job(job_id):
    """删除任务（仅 done/failed/interrupted）。返回 (ok, message)。"""
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None:
            return False, '任务不存在'
        status = (row.extra_data or {}).get('status')
        if status in ('pending', 'running'):
            return False, '任务正在排队/运行，不能删除'
        transient_id = row.transient_id
        sess.delete(row)
        sess.commit()
    finally:
        sess.close()
    d = job_dir(transient_id, job_id)
    shutil.rmtree(d, ignore_errors=True)
    parent = os.path.dirname(d)
    try:
        os.rmdir(parent)  # 源目录已空则一并清理；非空则跳过
    except OSError:
        pass
    return True, '已删除'
