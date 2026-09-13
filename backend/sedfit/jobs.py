"""SED 拟合异步任务系统（克隆 fitting/jobs.py 模式）。

- 单 worker 线程池串行执行；任务记录落在 fitting_results 表：
    model_name  = 'sed_<model>'（sed_powerlaw_dust / sed_powerlaw_xray /
                  sed_blackbody / sed_2blackbody / sed_bb_powerlaw /
                  sed_blackbody_series；不含冒号，不串入 fitting/hostfit 列表）
    parameters  = 最大似然参数 {名: {v, err}}（series 模式为汇总信息）
    chi_squared = chi2_min（未除 dof；series 模式为 None）
    extra_data  = {engine: 'sed_mcmc', model, config, status, error, runtime_s,
                   dof, bic, aic, warnings, files, epochs, lambda_coverage,
                   created_by}
- 产物文件存 backend/fitting_store/<transient_id>/sed_<job_id>/。
- custom_mcmc / VegasAfterglow 为可选依赖，一律在 worker 内惰性 import，
  保证 build/epochs 等同步端点在无拟合依赖的环境中仍可用。
- 任务中断（2026-09-13）：create_job 时在 _cancel_events 注册 threading.Event；
  stop_job 置位并立即把状态写为 interrupted（不续算、产物保留）；worker
  开跑前与写 done 前各查一次，series 模式每个历元开始前也查；run_mcmc 内
  每步检查（协程式取消）；任务终结后弹出注册表条目。
"""
import json
import math
import os
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app import get_session
from models import Transient, FilterDef, FittingResult
from sedfit import builder as sed_builder
from sedfit import laws as sed_laws
from sedfit import models as sed_models

_STORE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'fitting_store')

# 单 worker 串行队列
_pool = ThreadPoolExecutor(max_workers=1)

# 任务中断注册表：job_id → threading.Event（create_job 注册，任务终结弹出）
_cancel_events = {}
_cancel_lock = threading.Lock()

_MJY_CGS = 1e-26
_C_AA_PER_S = 2.99792458e18
_VERSION = 'ajst-sedfit 1.0'


def job_dir(transient_id, job_id):
    return os.path.join(_STORE_ROOT, str(transient_id), f'sed_{job_id}')


def _set_status(sess, row, status, **extra):
    """更新 extra_data（JSONB 需整体重赋值才会被跟踪）"""
    ed = dict(row.extra_data or {})
    ed['status'] = status
    ed.update(extra)
    row.extra_data = ed
    sess.commit()


def create_job(transient_id, model_key, config, warnings=None, created_by=None):
    """建任务（pending）并入队，返回任务 id。调用方需已完成校验。"""
    config = dict(config or {})
    config['model'] = model_key
    sess = get_session()
    try:
        row = FittingResult(
            transient_id=transient_id,
            model_name=sed_models.model_name_of(model_key),
            parameters={},
            chi_squared=None,
            extra_data={
                'engine': 'sed_mcmc',
                'model': model_key,
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
    行读-改-写用 FOR UPDATE 行锁串行化，与 worker 写 done 互斥
    （先 interrupted 后 done 覆盖不可能）。"""
    with _cancel_lock:
        ev = _cancel_events.get(job_id)
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id, with_for_update=True)
        if row is None:
            return False
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
        pass


# ─── 数据组装与模型包装 ───

def _build_dataframe(sed):
    """SED 点 → custom_mcmc 约定的 DataFrame。

    列 t_sec/nu_hz/band_label/f_nu_cgs/f_nu_err_cgs/weights/upperlimit；
    单历元拟合 t_sec 全填 t_sel 秒；上限点编码 f=0、err=上限值（单侧罚内置）。
    """
    import pandas as pd

    t_sec = float(sed['t_sel']) * 86400.0
    rows = []
    for p in sed['points']:
        if p['is_ul']:
            f_c, fe_c = 0.0, p['f_mjy'] * _MJY_CGS
        else:
            f_c, fe_c = p['f_mjy'] * _MJY_CGS, p['ferr_mjy'] * _MJY_CGS
        rows.append({'t_sec': t_sec, 'nu_hz': p['nu_hz'], 'band_label': p['band'],
                     'f_nu_cgs': f_c, 'f_nu_err_cgs': fe_c,
                     'weights': p.get('weights', 1.0),
                     'upperlimit': bool(p['is_ul'])})
    return pd.DataFrame(rows).sort_values('t_sec').reset_index(drop=True)


def _bandpass_map(sess, sed):
    """各波段有效频率 → filters 表透过率曲线（有缓存的波段才收）"""
    filters = {f.id: f for f in sess.query(FilterDef).all()}
    bp = {}
    for p in sed['points']:
        nu = p['nu_hz']
        if nu in bp:
            continue
        filt = filters.get(p['band'])
        tr = (filt.extra_data or {}).get('transmission') if filt else None
        if tr and tr.get('wl') and tr.get('tr'):
            bp[nu] = tr
    return bp


def _wrap_model_flux(model, mcfg, bp_map):
    """把模型包成 custom_mcmc 的 (params, t, nu) 形式；可选通带综合。

    有透过率缓存的波段用 ∫Fν·T dlnν/∫T dlnν 合成（宽通带下比有效波长
    直接求值更公平）；无缓存波段直接用有效频率处值。"""
    def base(params, t, nu):
        return model.model_flux(params, t, nu, mcfg)

    if not bp_map:
        return base
    # 每个波段预生成覆盖通带的模型网格（ν 范围取透过率曲线两端）
    grids = {}
    for nu_eff, tr in bp_map.items():
        wl = [w for w in tr['wl'] if w and w > 0]
        if not wl:
            continue
        grids[nu_eff] = np.logspace(np.log10(_C_AA_PER_S / max(wl)),
                                    np.log10(_C_AA_PER_S / min(wl)), 256)

    def wrapped(params, t, nu):
        f = base(params, t, nu)
        f = np.asarray(f, dtype=float)
        for nu_eff, grid in grids.items():
            idx = np.where(nu == nu_eff)[0]
            if len(idx):
                fg = base(params, t, grid)
                f[idx] = sed_laws.bandpass_flux(grid, fg, bp_map[nu_eff],
                                                nu_eff=nu_eff)
        return f

    return wrapped


def _jsonable(obj):
    """numpy 标量/数组 → Python 原生类型（JSONB 列与 json.dump 用）"""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def _posterior_summary(flat, flat_logp, defs):
    """后验中值 ±1σ（16/84 分位）与最大似然参数（物理空间）"""
    from VegasAfterglow import Scale  # 惰性 import（worker 内调用）

    out = {}
    ml_theta = flat[int(np.argmax(flat_logp))]
    for i, d in enumerate(defs):
        col = (10.0 ** flat[:, i]) if d.scale == Scale.log else flat[:, i]
        p16, p50, p84 = np.percentile(col, [16, 50, 84])
        ml = (10.0 ** ml_theta[i]) if d.scale == Scale.log else ml_theta[i]
        out[d.name] = {'median': float(p50), 'err': float((p84 - p16) / 2.0),
                       'err16': float(p50 - p16), 'err84': float(p84 - p50),
                       'ml': float(ml)}
    return out


# ─── 绘图 ───

def _plot_sed(sed, model_flux, base_flux, ml_params, flat, flat_logp, defs,
              config, outpath):
    """log-log SED 图：数据点+误差棒+上限箭头+最佳模型线+68% 可信带+残差子图。

    MW/LMC 律（含 2175 Å bump）时在观测系 bump 窗口画高亮带。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    det = [p for p in sed['points'] if not p['is_ul']]
    uls = [p for p in sed['points'] if p['is_ul']]
    nus = np.array([p['nu_hz'] for p in sed['points']])
    grid = np.logspace(np.log10(nus.min() / 3), np.log10(nus.max() * 3), 300)
    t_grid = np.full_like(grid, sed['t_sel'] * 86400.0)

    best = base_flux(ml_params, t_grid, grid) / _MJY_CGS  # → mJy
    # 68% 可信带：随机抽 ≤200 个后验样本
    from VegasAfterglow import Scale
    rng = np.random.default_rng(0)
    idx = rng.choice(len(flat), size=min(200, len(flat)), replace=False)
    samples = []
    for i in idx:
        p = {d.name: (10.0 ** flat[i, j] if d.scale == Scale.log else flat[i, j])
             for j, d in enumerate(defs)}
        samples.append(base_flux(p, t_grid, grid) / _MJY_CGS)
    lo_b, hi_b = np.percentile(np.array(samples), [16, 84], axis=0)

    fig, (ax, axr) = plt.subplots(
        2, 1, figsize=(7, 6), sharex=True,
        gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.05})
    ax.fill_between(grid, lo_b, hi_b, color='C0', alpha=0.25,
                    label='68% credible band')
    ax.plot(grid, best, color='C0', lw=1.5, label='best-fit model')
    # 有透过率缓存的波段叠加通带合成模型点（与数据公平比较）
    bp_map = config.get('_bp_map') or {}
    if bp_map:
        bp_nu, bp_f = [], []
        for nu_eff, tr in bp_map.items():
            wl = [w for w in tr['wl'] if w and w > 0]
            g = np.logspace(np.log10(_C_AA_PER_S / max(wl)),
                            np.log10(_C_AA_PER_S / min(wl)), 256)
            bp_nu.append(nu_eff)
            bp_f.append(sed_laws.bandpass_flux(
                g, base_flux(ml_params, np.full_like(g, sed['t_sel'] * 86400.0), g),
                tr, nu_eff=nu_eff) / _MJY_CGS)
        ax.plot(bp_nu, bp_f, 's', color='C0', mfc='none', ms=7,
                label='bandpass-synth model')

    if det:
        ax.errorbar([p['nu_hz'] for p in det], [p['f_mjy'] for p in det],
                    yerr=[p['ferr_mjy'] for p in det], fmt='o', color='k',
                    ms=4, capsize=2, label='data')
        for p in det:
            if p.get('interp'):
                ax.plot(p['nu_hz'], p['f_mjy'], 'o', mfc='none', mec='C3',
                        ms=8)
    if uls:
        ax.errorbar([p['nu_hz'] for p in uls], [p['f_mjy'] for p in uls],
                    yerr=[0.25 * p['f_mjy'] for p in uls], fmt='v', color='gray',
                    uplims=True, ms=5, label='upper limits')
    # 2175 Å bump 窗口高亮（MW/LMC 律，观测系 λ=2175(1+z) Å）
    law = (config.get('law') or 'smc').lower()
    z = sed.get('z')
    if law in ('mw', 'lmc', 'mw_f99', 'mw_ccm'):
        lam_obs = 2175.0 * (1.0 + z) if z else 2175.0
        nu_bump = _C_AA_PER_S / lam_obs
        if nus.min() <= nu_bump <= nus.max():
            for a in (ax, axr):
                a.axvspan(nu_bump / 1.1, nu_bump * 1.1, color='orange',
                          alpha=0.15, lw=0)
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_ylabel(r'$F_\nu$ [mJy]')
    ax.set_title(f"{config.get('_transient_id', '')}  {config['model']}  "
                 f"t={sed['t_sel']:.4g} d"
                 + (f'  z={z:.4g}' if z else '  no z'))
    ax.legend(fontsize=8)

    # 残差子图：χ = (f_obs − f_mod)/σ（上限点不画）
    if det:
        dn = np.array([p['nu_hz'] for p in det])
        df_ = np.array([p['f_mjy'] for p in det])
        de = np.array([p['ferr_mjy'] for p in det])
        fmod = model_flux(ml_params, np.full_like(dn, sed['t_sel'] * 86400.0),
                          dn) / _MJY_CGS
        axr.axhline(0, color='k', lw=0.8)
        axr.errorbar(dn, (df_ - fmod) / de, fmt='o', color='k', ms=3, capsize=2)
    axr.set_xscale('log')
    axr.set_xlabel(r'$\nu$ [Hz]')
    axr.set_ylabel(r'$\chi$')
    fig.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close(fig)


def _plot_trl(epoch_rows, z, outpath):
    """T/R/L(t) 三联图（series 模式）"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    t = [e['t_days'] for e in epoch_rows]
    fig, axes = plt.subplots(3, 1, figsize=(7, 8), sharex=True,
                             gridspec_kw={'hspace': 0.08})
    for ax, key, ylabel in (
            (axes[0], 'T', 'T [K]'),
            (axes[1], 'R', 'R [cm]'),
            (axes[2], 'L', 'L [erg/s]')):
        y = [e.get(key) for e in epoch_rows]
        ye = [e.get(f'{key}_err') for e in epoch_rows]
        ok = [i for i, v in enumerate(y) if v is not None]
        if ok:
            errs = [ye[i] for i in ok]
            ax.errorbar([t[i] for i in ok], [y[i] for i in ok],
                        yerr=None if any(e is None for e in errs) else errs,
                        fmt='o-', ms=4, capsize=2)
            ax.set_yscale('log')
        else:
            ax.text(0.5, 0.5,
                    'no z: R/L unavailable' if z is None else 'no valid epochs',
                    transform=ax.transAxes, ha='center', va='center')
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.3)
    axes[-1].set_xlabel('t [days]')
    axes[-1].set_xscale('log')
    fig.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close(fig)


def _plot_series(epoch_fits, outpath):
    """series 模式全历元 SED 叠图（按 log t 着色）"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 6))
    ts = [e['t_days'] for e in epoch_fits]
    tmin, tmax = min(ts), max(ts)
    span = max(math.log10(tmax) - math.log10(tmin), 1e-9)
    cmap = matplotlib.colormaps['viridis']
    for e in sorted(epoch_fits, key=lambda x: x['t_days']):
        c = cmap((math.log10(e['t_days']) - math.log10(tmin)) / span)
        det = [p for p in e['sed_points'] if not p['is_ul']]
        if det:
            ax.errorbar([p['nu_hz'] for p in det], [p['f_mjy'] for p in det],
                        yerr=[p['ferr_mjy'] for p in det], fmt='o', ms=3,
                        color=c, capsize=1,
                        label=f"t={e['t_days']:.3g} d")
        grid = e.get('model_grid')
        if grid:
            ax.plot(grid[0], grid[1], color=c, lw=1, alpha=0.7)
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel(r'$\nu$ [Hz]')
    ax.set_ylabel(r'$F_\nu$ [mJy]')
    ax.legend(fontsize=7, ncol=2)
    fig.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close(fig)


# ─── worker：单历元拟合 ───

def _fit_one(transient_id, config, workdir, make_plots=True, cancel_event=None):
    """单历元构建 + MCMC 拟合。返回结果 dict（供单历元与 series 复用）。

    cancel_event：可选 threading.Event，用户中断标志（透传 run_mcmc 逐步检查）。
    模型定义了 constraint 静态方法（如 powerlaw_3seg 的 nu_b1<nu_b2）时
    作为硬约束传给 run_mcmc。"""
    from fitting.vegas_unified.custom_mcmc import (  # 惰性 import
        run_mcmc, compute_metrics, plot_corner)

    t0 = time.time()
    sess = get_session()
    try:
        sed = sed_builder.build_sed(
            sess, transient_id, config.get('t_sel'), dt=config.get('dt'),
            mode=config.get('mode', 'window'), bands=config.get('bands'),
            use_gext=config.get('use_gext', True),
            upperlimits=config.get('upperlimits', 'exclude'),
            k_correct=config.get('k_correct', False),
            z_override=config.get('z_override'))
        bp_map = (_bandpass_map(sess, sed)
                  if config.get('bandpass', True) else {})
    finally:
        sess.close()

    det = [p for p in sed['points'] if not p['is_ul']]
    det_bands = {p['band'] for p in det}
    if not det:
        raise ValueError('该历元全部为上限点，拒绝拟合')
    if len(det_bands) < 3:
        raise ValueError(f'有效探测波段不足 3 个（当前 {len(det_bands)} 个），'
                         '请调整 t_sel/dt 或波段选择')

    model_key = config['model']
    model = sed_models.MODELS[model_key]
    mcfg = dict(config)
    mcfg['z'] = sed['z']
    defs = model.param_defs(mcfg)
    base_flux = lambda p, t, nu: model.model_flux(p, t, nu, mcfg)
    model_flux = _wrap_model_flux(model, mcfg, bp_map)
    df = _build_dataframe(sed)

    sampler, flat, flat_logp, names, defs = run_mcmc(
        model_flux, defs, df, workdir,
        nsteps=int(config.get('nsteps', 5000)),
        nburn=int(config.get('nburn', 2000)),
        n_workers=int(config.get('n_workers', 4)),
        constraint=getattr(model, 'constraint', None),
        cancel_event=cancel_event,
        config={'sed_model': model_key, 't_sel_days': sed['t_sel'], 'z': sed['z'],
                'law': mcfg.get('law', 'smc'), 'version': _VERSION})

    n_free = len(defs)
    # 口径说明：n_data = len(df) 含上限点（单侧罚项也携带信息），
    # 故 dof = n_data − n_free 在含上限点时略虚高；属有意口径，不改行为
    metrics = compute_metrics(flat_logp, len(df), n_free)
    post = _posterior_summary(flat, flat_logp, defs)
    ml_params = {k: v['ml'] for k, v in post.items()}
    derived = _jsonable(model.derived(ml_params, mcfg))
    check_meta = dict(sed['meta'])
    check_meta['t_sel'] = sed['t_sel']
    check_meta['z'] = sed['z']
    warnings = list(sed['meta']['warnings'])
    warnings += model.sanity_checks(ml_params, derived, check_meta)

    if make_plots:
        config2 = dict(config)
        config2['_bp_map'] = bp_map
        config2['_transient_id'] = transient_id
        _plot_sed(sed, model_flux, base_flux, ml_params, flat, flat_logp, defs,
                  config2, os.path.join(workdir, 'sed.png'))
        plot_corner(flat, defs, names, os.path.join(workdir, 'corner.png'))
        with open(os.path.join(workdir, 'sed.csv'), 'w', encoding='utf-8') as f:
            f.write(sed_builder.sed_to_csv(sed))

    result = {
        'sed': sed,
        'params': post,           # 含 median/err/ml
        'ml_params': ml_params,
        'metrics': metrics,
        'derived': derived,
        'warnings': warnings,
        'runtime_s': time.time() - t0,
        'meta': {
            'lambda_min_a': sed['meta']['lambda_min_a'],
            'lambda_max_a': sed['meta']['lambda_max_a'],
            'has_uv': sed['meta']['has_uv'],
            'has_ir': sed['meta']['has_ir'],
            'n_bands': sed['meta']['n_bands'],
            'n_points': sed['meta']['n_points'],
            'mode': sed['meta']['mode'],
            'gext_used': sed['meta']['gext_used'],
            'host_law': mcfg.get('law', 'smc'),
            'rv': mcfg.get('rv_free') and 'free' or (mcfg.get('rv') or 'nominal'),
            'z_assumption': {'z': sed['z'], 'source': sed['z_source']},
            # window 模式为取数窗口；interp 模式仅作用于上限点纳入范围
            'window_days': [sed['t_sel'] - sed['dt'],
                            sed['t_sel'] + sed['dt']],
            'bandpass_synth': bool(bp_map),
            'version': _VERSION,
        },
    }
    return result, flat, flat_logp, defs, names


def _run_single(transient_id, config, workdir, cancel_event=None):
    """单历元任务：写全部产物并返回写库字段"""
    result, flat, flat_logp, defs, names = _fit_one(
        transient_id, config, workdir, make_plots=True,
        cancel_event=cancel_event)
    sed = result['sed']

    result_json = {
        'transient_id': transient_id,
        'model': config['model'],
        't_sel_days': sed['t_sel'], 'dt_days': sed['dt'],
        'z': sed['z'], 'z_source': sed['z_source'],
        'k_corrected': sed['k_corrected'],
        'params': result['params'],
        'metrics': result['metrics'],
        'derived': result['derived'],
        'meta': result['meta'],
        'warnings': result['warnings'],
    }
    with open(os.path.join(workdir, 'result.json'), 'w', encoding='utf-8') as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2, default=float)

    files = {}
    for kind, fname in (('sed_png', 'sed.png'), ('corner', 'corner.png'),
                        ('result', 'result.json'), ('sed_csv', 'sed.csv'),
                        ('h5', 'chain_record.h5')):
        if os.path.exists(os.path.join(workdir, fname)):
            files[kind] = fname
    m = result['metrics']
    return {
        'parameters': {k: {'v': v['ml'], 'err': v['err']}
                       for k, v in result['params'].items()},
        'chi_squared': m['chi2_min'],
        'extra': {
            'runtime_s': result['runtime_s'],
            'dof': m['dof'], 'bic': m['BIC'], 'aic': m['AIC'],
            'warnings': result['warnings'],
            'files': files,
            'lambda_coverage': [sed['meta']['lambda_min_a'],
                                sed['meta']['lambda_max_a']],
            'result_meta': result['meta'],
            'derived': result['derived'],
        },
    }


# ─── worker：多历元黑体批量（series） ───

def _run_series(transient_id, config, workdir, cancel_event=None):
    """逐历元构建 + 单黑体短 MCMC，汇总 T/R/L(t)。

    config.epochs 为 t_days 列表或 'auto'（suggest_epochs 后对数均取
    n_epochs 个）；单历元失败记 warning 继续。cancel_event 置位时
    每个历元开始前抛 McmcInterrupted（历元间中断）。"""
    from fitting.vegas_unified.custom_mcmc import McmcInterrupted  # 惰性 import
    sess = get_session()
    try:
        t = sess.get(Transient, transient_id)
        # z 来源与单历元一致：config.z_override 优先于目录值
        if config.get('z_override') is not None:
            z = float(config['z_override'])
        elif t is not None and t.redshift:
            z = float(t.redshift)
        else:
            z = None
        epochs = config.get('epochs')
        if not epochs or epochs == 'auto':
            sugg = sed_builder.suggest_epochs(
                sess, transient_id,
                min_bands=int(config.get('min_bands', 3)),
                dt_frac=float(config.get('dt_frac', 0.1)),
                max_epochs=int(config.get('max_epochs', 20)))
            ts = sorted(e['t_days'] for e in sugg)
            n_want = int(config.get('n_epochs', 12))
            if len(ts) > n_want:
                # 对数均匀选取 n_want 个历元
                targets = np.logspace(np.log10(ts[0]), np.log10(ts[-1]), n_want)
                sel = sorted({min(ts, key=lambda x: abs(x - tg)) for tg in targets})
                ts = sel
            epochs = ts
    finally:
        sess.close()
    epochs = [float(e) for e in epochs if float(e) > 0]
    if not epochs:
        raise ValueError('无可用历元（epochs 为空且 auto 建议为空）')

    warnings = []
    epoch_rows = []   # series.csv / trl.png 用
    epoch_fits = []   # series.png 用
    subdir = os.path.join(workdir, 'epochs')
    for i, t_days in enumerate(epochs):
        if cancel_event is not None and cancel_event.is_set():
            raise McmcInterrupted('用户手动中断')
        econfig = dict(config)
        econfig['model'] = 'blackbody'
        econfig['t_sel'] = t_days
        econfig.setdefault('mode', 'window')
        econfig['nsteps'] = int(config.get('series_nsteps', 2000))
        econfig['nburn'] = int(config.get('series_nburn', 800))
        try:
            result, flat, flat_logp, defs, names = _fit_one(
                transient_id, econfig, os.path.join(subdir, f'epoch_{i:03d}'),
                make_plots=False, cancel_event=cancel_event)
            sed = result['sed']
            post = result['params']
            # T/R/L 统一取后验中位数（16/84 分位为 1σ），口径一致；
            # L 不再取 ML 点的 derived 值
            row = {
                't_days': t_days,
                'T': post['T']['median'], 'T_err': post['T']['err'],
                'R': post['R']['median'] if 'R' in post else None,
                'R_err': post['R']['err'] if 'R' in post else None,
                'L': None, 'L_err': None,
                'n_bands': sed['meta']['n_bands'],
                'lambda_min_a': sed['meta']['lambda_min_a'],
                'lambda_max_a': sed['meta']['lambda_max_a'],
                'chi2': result['metrics']['chi2_min'],
                'dof': result['metrics']['dof'],
            }
            if 'R' in post:
                # L 由后验样本传播：L = 4πR²σT⁴，取中位数 ± 16/84 分位
                from VegasAfterglow import Scale
                cs = {dd.name: (10.0 ** flat[:, j] if dd.scale == Scale.log
                                else flat[:, j]) for j, dd in enumerate(defs)}
                Ls = 4.0 * math.pi * cs['R'] ** 2 * 5.670374419e-5 * cs['T'] ** 4
                p16, p50, p84 = np.percentile(Ls, [16, 50, 84])
                row['L'] = float(p50)
                row['L_err'] = float((p84 - p16) / 2.0)
            epoch_rows.append(row)
            # series.png 用的模型曲线
            nus = np.array([p['nu_hz'] for p in sed['points']])
            grid = np.logspace(np.log10(nus.min() / 3), np.log10(nus.max() * 3),
                               200)
            mcfg = dict(econfig)
            mcfg['z'] = sed['z']
            curve = sed_models.MODELS['blackbody'].model_flux(
                result['ml_params'], np.full_like(grid, t_days * 86400.0),
                grid, mcfg) / _MJY_CGS
            epoch_fits.append({'t_days': t_days, 'sed_points': sed['points'],
                               'model_grid': (grid.tolist(), curve.tolist())})
            warnings += [f'[t={t_days:.4g} d] {w}' for w in result['warnings']]
        except McmcInterrupted:
            raise   # 用户中断不是单历元失败，直接中止整个 series
        except Exception as e:
            warnings.append(f'历元 t={t_days:.4g} d 拟合失败，已跳过: {e}')
            epoch_rows.append({'t_days': t_days, 'T': None, 'T_err': None,
                               'R': None, 'R_err': None, 'L': None, 'L_err': None,
                               'n_bands': None, 'lambda_min_a': None,
                               'lambda_max_a': None, 'chi2': None, 'dof': None,
                               'error': str(e)})
    if not epoch_fits:
        raise ValueError('全部历元拟合失败')

    # series.csv（csv 模块 QUOTE_MINIMAL：字段含逗号/引号/换行时自动加引号）
    import csv as _csv
    with open(os.path.join(workdir, 'series.csv'), 'w', encoding='utf-8',
              newline='') as f:
        w = _csv.writer(f, lineterminator='\n')
        w.writerow(['t_days', 'T_K', 'T_err', 'R_cm', 'R_err', 'L_erg_s',
                    'n_bands', 'lambda_min_a', 'lambda_max_a', 'chi2', 'dof',
                    'error'])
        for e in epoch_rows:
            w.writerow([e.get(k) if e.get(k) is not None else ''
                        for k in ('t_days', 'T', 'T_err', 'R', 'R_err', 'L',
                                  'n_bands', 'lambda_min_a', 'lambda_max_a',
                                  'chi2', 'dof')] + [e.get('error', '')])
    _plot_trl(epoch_rows, z, os.path.join(workdir, 'trl.png'))
    _plot_series(epoch_fits, os.path.join(workdir, 'series.png'))

    result_json = {
        'transient_id': transient_id,
        'model': 'blackbody_series',
        'z': z,
        'epochs': epoch_rows,
        'warnings': warnings,
        'meta': {'n_epochs_total': len(epochs), 'n_epochs_done': len(epoch_fits),
                 'version': _VERSION},
    }
    with open(os.path.join(workdir, 'result.json'), 'w', encoding='utf-8') as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2, default=float)

    files = {}
    for kind, fname in (('series_csv', 'series.csv'), ('trl_png', 'trl.png'),
                        ('series_png', 'series.png'), ('result', 'result.json')):
        if os.path.exists(os.path.join(workdir, fname)):
            files[kind] = fname
    return {
        'parameters': {'n_epochs_total': len(epochs),
                       'n_epochs_done': len(epoch_fits)},
        'chi_squared': None,
        'extra': {'warnings': warnings, 'files': files, 'epochs': epoch_rows},
    }


# ─── 任务生命周期 ───

def _run_job(job_id):
    """worker：pending → running → done/failed/interrupted（短 session 模式，
    同 fitting/jobs）。中断：开跑前查 cancel_event（pending 任务可能在队列里
    已被中断）；运行期间由 run_mcmc/series 历元间协程式响应；写 done 前再查
    标志与行状态，已中断则不写 done，只合并已存在的产物文件。"""
    try:
        from fitting.vegas_unified.custom_mcmc import McmcInterrupted
    except ImportError:
        McmcInterrupted = ()   # 拟合依赖缺失：中断分支永不命中（except 空元组）
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None:
            _pop_cancel_event(job_id)
            return
        ed = dict(row.extra_data or {})
        transient_id = row.transient_id
        config = dict(ed.get('config') or {})
    finally:
        sess.close()
    with _cancel_lock:
        ev = _cancel_events.get(job_id)
    workdir = job_dir(transient_id, job_id)
    try:
        model_key = config.get('model')
        if model_key != sed_models.SERIES_KEY and \
                model_key not in sed_models.MODELS:
            raise ValueError(f'未知 SED 模型: {model_key}')
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, 'run.log'), 'a', encoding='utf-8') as lf:
            lf.write(f'任务 {job_id} 开始: model={model_key} '
                     f'transient={transient_id} config={config}\n')
        if ev is not None and ev.is_set():
            # 排队期间已被用户中断（stop_job 已写 interrupted，此处兜底确认）
            _update_status(job_id, 'interrupted', error='用户手动中断')
            return
        _update_status(job_id, 'running')
        if model_key == sed_models.SERIES_KEY:
            out = _run_series(transient_id, config, workdir, cancel_event=ev)
        else:
            out = _run_single(transient_id, config, workdir, cancel_event=ev)
        # run.log 始终登记（写入于任务开始处）
        if os.path.exists(os.path.join(workdir, 'run.log')):
            out['extra'].setdefault('files', {})['log'] = 'run.log'
        sess = get_session()
        try:
            # 行锁串行化：与 stop_job 的 interrupted 回写互斥
            row = sess.get(FittingResult, job_id, with_for_update=True)
            if row is None:
                return
            # 写 done 前再查中断：保留 interrupted 状态，只合并产物文件
            if (ev is not None and ev.is_set()) or \
                    (row.extra_data or {}).get('status') == 'interrupted':
                ed_now = dict(row.extra_data or {})
                merged = dict(ed_now.get('files') or {})
                merged.update(out['extra'].get('files') or {})
                ed_now['files'] = merged
                row.extra_data = ed_now
                sess.commit()
                return
            row.parameters = out['parameters']
            row.chi_squared = out['chi_squared']
            _set_status(sess, row, 'done', error=None, **out['extra'])
        finally:
            sess.close()
    except McmcInterrupted as e:
        # 用户中断：不追加失败 traceback，run.log 只记一行
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, 'run.log'), 'a', encoding='utf-8') as lf:
            lf.write('\n===== 任务被用户中断 =====\n')
        _update_status(job_id, 'interrupted', error=str(e) or '用户手动中断')
    except Exception as e:
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, 'run.log'), 'a', encoding='utf-8') as lf:
            lf.write('\n===== 任务失败 =====\n' + traceback.format_exc())
        _fail_job(job_id, str(e))
    finally:
        _pop_cancel_event(job_id)


def mark_interrupted():
    """服务启动时调用：残留的 running/pending sed_* 任务标记 interrupted"""
    sess = get_session()
    try:
        n = 0
        for row in (sess.query(FittingResult)
                    .filter(FittingResult.model_name.like('sed\\_%', escape='\\'))
                    .all()):
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
