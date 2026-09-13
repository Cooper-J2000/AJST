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
- 任务中断（2026-09-13）：create_job 时在 _cancel_events 注册 threading.Event；
  stop_job 置位并立即把状态写为 interrupted（不续算、产物保留）；worker 在
  开跑前、engine.run 返回后各查一次标志/行状态，中断不写 done；任务终结后
  弹出注册表条目。
"""
import math
import os
import re
import shutil
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from app import get_session
from models import Transient, Lightcurve, FilterDef, FittingResult
from fitting.engines import get_engine
from fitting.vegas_unified.custom_mcmc import McmcInterrupted

_STORE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'fitting_store')

# 单 worker 串行队列
_pool = ThreadPoolExecutor(max_workers=1)

# 任务中断注册表：job_id → threading.Event（create_job 注册，任务终结弹出）
_cancel_events = {}
_cancel_lock = threading.Lock()

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
        skip_no_vega2ab = set()  # Vega 星等但缺转换系数的波段
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
            t_s = (lc.time * factor
                   if (factor is not None and lc.time is not None) else None)
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
                conv = _raw_to_mjy(lc, filt, skip_no_vega2ab)
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
        if skip_no_vega2ab:
            warnings.append(f'Vega 星等但缺 vega2ab 转换系数，已跳过波段: '
                            f'{sorted(skip_no_vega2ab)}')
        if skip_no_err:
            warnings.append(f'{skip_no_err} 个探测点误差缺失或为 0，已跳过')

        band_list = [b for b in bands.values() if b['t']]
        n_points = sum(len(b['t']) for b in band_list)
        return {'z': float(t.redshift), 'bands': band_list,
                'warnings': warnings, 'n_points': n_points}
    finally:
        sess.close()


def _raw_to_mjy(lc, filt, vega_missing=None):
    """原始（未银消改正）flux_density → (f_mjy, ferr_mjy)；无法换算返回 None。

    vega_missing: 可选的集合，Vega 星等但缺 vega2ab 转换系数时把波段名
    收集进去（不再静默按 AB 处理）。"""
    unit = (lc.flux_density_unit or '').strip().lower()
    if unit in ('mag', 'magnitude'):
        mag = lc.flux_density
        if mag is None:
            return None
        if (lc.mag_system or '').strip().lower() == 'vega':
            v2ab = getattr(filt, 'vega2ab', None) if filt is not None else None
            if v2ab is None:  # 缺转换系数（真值为 0.0 的滤光片正常换算）
                if vega_missing is not None:
                    vega_missing.add(lc.band)
                return None
            mag = mag + v2ab  # Vega → AB
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
    with _cancel_lock:
        _cancel_events[job_id] = threading.Event()
    _pool.submit(_run_job, job_id)
    return job_id


def _pop_cancel_event(job_id):
    """任务终结后弹出中断注册表条目"""
    with _cancel_lock:
        _cancel_events.pop(job_id, None)


def stop_job(job_id):
    """用户中断：行存在且状态 pending/running → 置位中断标志并立即把状态写为
    interrupted（协程式取消：采样循环随后自行退出，不续算、产物文件保留）。
    返回 True 表示已中断；行不存在或状态非 pending/running 返回 False。
    model_name 不含冒号的行（sed_*/pcigale_host 等非余辉任务）一律拒绝，
    避免误中断其他子系统的任务。行读-改-写用 FOR UPDATE 行锁串行化，
    与 worker 写 done 互斥（先 interrupted 后 done 覆盖不可能）。"""
    with _cancel_lock:
        ev = _cancel_events.get(job_id)
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id, with_for_update=True)
        if row is None:
            return False
        if ':' not in (row.model_name or ''):
            return False  # 非余辉拟合任务（sed_*/pcigale_host），拒绝
        if (row.extra_data or {}).get('status') not in ('pending', 'running'):
            return False
        if ev is not None:
            ev.set()
        _set_status(sess, row, 'interrupted', error='用户手动中断')
        return True
    finally:
        sess.close()


def _update_status(job_id, status, **extra):
    """用全新 session 更新任务状态（短 session 模式：不在 MCMC 期间持有连接）。
    行锁串行化（与 stop_job 互斥），避免写 done 与写 interrupted 竞态。"""
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id, with_for_update=True)
        if row is not None:
            _set_status(sess, row, status, **extra)
    finally:
        sess.close()


def _fail_job(job_id, err):
    """失败回写：全新 session 写 failed；再失败则兜底写 interrupted"""
    try:
        _update_status(job_id, 'failed', error=err)
        return
    except Exception:
        pass
    try:
        _update_status(job_id, 'interrupted',
                       error=err + '（failed 状态回写失败，按 interrupted 兜底）')
    except Exception:
        pass  # 数据库持续不可用，只能等服务重启时 mark_interrupted 收拾


def _run_job(job_id):
    """worker：pending → running → done/failed/interrupted

    短 session 模式：运行前读配置即关 session；MCMC 期间不持有 DB 连接
    （可达数小时，长持有会被 idle 超时/池回收断掉，导致结束时写结果失败、
    任务永久卡 running）；结束后新开 session 写结果，失败回写双层兜底。
    中断：开跑前查一次 cancel_event（pending 任务可能在队列里已被中断）；
    engine.run 期间由引擎协程式响应；engine.run 返回后再查一次标志与行状态，
    已中断则不写 done，只合并已存在的产物文件（保留 interrupted 状态）。
    """
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None:
            _pop_cancel_event(job_id)
            return
        ed = dict(row.extra_data or {})
        transient_id = row.transient_id
    finally:
        sess.close()
    with _cancel_lock:
        ev = _cancel_events.get(job_id)
    engine = get_engine(ed.get('engine'))
    workdir = job_dir(transient_id, job_id)
    try:
        if engine is None:
            raise ValueError(f"未知引擎: {ed.get('engine')}")
        if ev is not None and ev.is_set():
            # 排队期间已被用户中断（stop_job 已写 interrupted，此处兜底确认）
            _update_status(job_id, 'interrupted', error='用户手动中断')
            return
        _update_status(job_id, 'running')
        data = prepare_data(transient_id, (ed.get('config') or {}).get('data_selection'))
        if data['n_points'] == 0:
            raise ValueError('无可用数据点')
        result = engine.run(ed.get('config') or {}, data, workdir, log=None,
                            cancel_event=ev)
        files = {}
        for kind, fname in (('h5', 'chain_record.h5'), ('corner', 'corner.png'),
                            ('lc_model', 'lc_model.json'),
                            ('lc_plot', 'lc_plot.png'),
                            ('lc_ratio', 'lc_ratio_plot.png'),
                            ('metrics', 'metrics.txt')):
            if os.path.exists(os.path.join(workdir, fname)):
                files[kind] = fname
        sess = get_session()
        try:
            # 行锁串行化：与 stop_job 的 interrupted 回写互斥，
            # 使「先 interrupted 后 done 覆盖」不可能
            row = sess.get(FittingResult, job_id, with_for_update=True)
            if row is None:
                return
            # engine.run 返回后再查中断：不写 done，只把已存在的产物文件
            # 合并进 extra_data.files（保留 interrupted 状态，产物由用户决定删留）
            if (ev is not None and ev.is_set()) or \
                    (row.extra_data or {}).get('status') == 'interrupted':
                ed_now = dict(row.extra_data or {})
                merged = dict(ed_now.get('files') or {})
                merged.update(files)
                ed_now['files'] = merged
                row.extra_data = ed_now
                sess.commit()
                return
            row.parameters = result['params']
            row.chi_squared = result['chi2']
            _set_status(sess, row, 'done',
                        runtime_s=result['runtime_s'],
                        dof=result['dof'], bic=result['bic'], aic=result['aic'],
                        warnings=(data['warnings']
                                  + (result.get('warnings') or [])),
                        files=files, error=None)
        finally:
            sess.close()
    except McmcInterrupted as e:
        # 用户中断：不追加失败 traceback，run.log 只记一行
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, 'run.log'), 'a', encoding='utf-8') as lf:
            lf.write('\n===== 任务被用户中断 =====\n')
        _update_status(job_id, 'interrupted', error=str(e) or '用户手动中断')
    except Exception as e:
        # 失败详情同时落 run.log
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, 'run.log'), 'a', encoding='utf-8') as lf:
            lf.write('\n===== 任务失败 =====\n' + traceback.format_exc())
        _fail_job(job_id, str(e))
    finally:
        _pop_cancel_event(job_id)


def mark_interrupted():
    """服务启动时调用：残留的 running/pending 余辉拟合任务一律标记 interrupted。

    只处理余辉拟合任务（model_name 形如 engine:...，含冒号）；sed_* 任务由
    sedfit.jobs.mark_interrupted、pcigale 宿主任务由 hostfit.jobs.mark_interrupted
    各自处理（按 model_name 区分，互不越界）。"""
    sess = get_session()
    try:
        n = 0
        for row in (sess.query(FittingResult)
                    .filter(FittingResult.model_name.like('%:%')).all()):
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
    """删除任务（仅 done/failed/interrupted 的余辉拟合任务）。
    model_name 不含冒号的行（sed_*/pcigale_host 等）按「任务不存在」拒绝，
    避免误删其他子系统任务并留下孤儿产物目录。返回 (ok, message)。"""
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id, with_for_update=True)
        if row is None:
            return False, '任务不存在'
        if ':' not in (row.model_name or ''):
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
