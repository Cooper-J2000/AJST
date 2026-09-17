"""prospector 宿主星系拟合执行器（与 runner.py 的 pcigale 执行器并列）。

run(job_id, config, log, workdir=None, filters=None)：
  1. config['photometry']（[{band, mag, mag_err, mag_sys, source}]）→ maggies 观测表
     （maggies = mJy / 3.631e6；1 maggie = 3631 Jy）
  2. FilterDef.extra_data['transmission']（wl[Å]/tr 峰值归一）→ sedpy Filter 列表；
     无曲线的波段视为不可用并跳过
  3. TemplateLibrary['parametric_sfh']（delayed-tau：mass/tage/tau/logzsol/dust2）
     组合模型，nebular / dust_emission / IGM 吸收由 use_nebular / use_duste /
     use_igm 开关启用；先验范围由 config['priors'] 覆盖
  4. dynesty（默认）或 emcee 采样
  5. 写出 result.json / results.txt / sed.png / corner.png；run.log 由 jobs 层维护

config = {'mode': 'fixed'|'photoz',
          'redshift': float|None,              # fixed 模式必填
          'z_min': .., 'z_max': ..,            # photoz 模式必填（zred 自由，TopHat 先验）
          'photometry': [{band, mag, mag_err, mag_sys, source, gext_corr?}],
          'use_nebular': bool, 'use_duste': bool, 'use_igm': bool（默认 True）,
          'sampler': 'dynesty'|'emcee',
          'dynesty': {'nlive': int},           # 默认 100，上限 500（routes 校验）
          'emcee': {'nwalkers': int, 'niter': int, 'nburn': int},
          'priors': {'mass': [lo, hi], 'tage': [lo, hi], 'tau': [lo, hi],
                     'dust2': [lo, hi], 'logzsol': [lo, hi]}}
          # mass 单位 M☉（LogUniform 先验）；tage/tau 单位 Gyr；
          # dust2 为 5500Å V 带光学深度；logzsol 为 log10(Z/Z☉)

prospect/sedpy/fsps 在 run() 内惰性 import：缺包时任务报清晰中文错误，
不影响服务启动（burst_advocate 环境已装 astro-prospector 1.4.1 等）。
"""
import json
import math
import os

import numpy as np

from app import get_session
from models import FilterDef
from hostfit.runner import _apply_gext_correction, _mag_to_mjy

# FSPS 数据目录解析优先级：已设 SPS_HOME > AJST_SPS_HOME > 本机硬编码回退
# （对照 runner.py 的 pcigale 二进制解析模式；换机器时必须设环境变量）。
# 必须在 import prospect/fsps 之前设置，故放在模块级。
os.environ.setdefault('SPS_HOME',
                      os.environ.get('AJST_SPS_HOME') or '/home/ajst/local/fsps')

_MJY_PER_MAGGIE = 3.631e6   # 1 maggie = 3631 Jy
_GYR_PER_YR = 1e9
_AV_PER_DUST2 = 1.086       # dust2 是 5500Å V 带光学深度，Av = 1.086 × dust2
_POSTERIOR_DERIVED_MAX = 200  # 派生量后验子样本上限
_CORNER_SAMPLE_MAX = 5000     # corner 图抽样上限

_DEFAULT_PRIORS = {
    'mass': [1e8, 1e12],
    'tage': [0.05, 13.0],
    'tau': [0.1, 30.0],
    'dust2': [0.0, 2.0],
    'logzsol': [-2.0, 0.19],
}


def _import_prospector():
    """惰性导入 prospector 依赖栈；缺包时报清晰中文错误。"""
    try:
        from prospect.models.templates import TemplateLibrary
        from prospect.models.sedmodel import SpecModel
        from prospect.models import priors
        from prospect.sources.galaxy_basis import CSPSpecBasis
        from prospect.fitting import fit_model
        from prospect.utils.obsutils import fix_obs
        from sedpy.observate import Filter as SedpyFilter
    except ImportError as e:
        raise RuntimeError(
            'prospector 拟合环境缺失（需要 astro-prospector / astro-sedpy / '
            'python-fsps / dynesty / emcee，并设 SPS_HOME 指向 FSPS 数据目录；'
            f'本机对应 burst_advocate conda 环境）: {e}')
    return {'TemplateLibrary': TemplateLibrary, 'SpecModel': SpecModel,
            'priors': priors, 'CSPSpecBasis': CSPSpecBasis,
            'fit_model': fit_model, 'fix_obs': fix_obs,
            'SedpyFilter': SedpyFilter}


# ─── 测光 → prospector obs ───

def build_obs(config, filters):
    """测光点列表 → prospector obs 字典。

    filters: {band_id: FilterDef}
    返回 (obs, points, warnings)
      obs:    {'filters': [sedpy Filter], 'maggies', 'maggies_unc',
               'phot_mask', 'wavelength': None, 'spectrum': None}（未 fix_obs）
      points: [{band, wave_nm, f_mjy, ferr_mjy}]（已成功转换，供 SED 图用）
    无可用点时抛 ValueError。
    """
    SedpyFilter = _import_prospector()['SedpyFilter']
    warnings = []
    points = []
    sed_filters = []
    seen = set()
    for p in config.get('photometry') or []:
        band = p.get('band')
        if p.get('upperlimit'):
            warnings.append(f'波段 {band}: 上限点不参与拟合，已跳过')
            continue
        filt = filters.get(band)
        curve = ((filt.extra_data or {}).get('transmission')
                 if filt is not None else None) or {}
        wl, tr = curve.get('wl'), curve.get('tr')
        if not wl or not tr:
            warnings.append(f'波段 {band}: filters 表无透过率曲线，已跳过')
            continue
        if band in seen:
            warnings.append(f'波段 {band}: 重复，仅取第一个点')
            continue
        conv = _mag_to_mjy(p, filt, warnings)
        if conv is None:
            continue
        f_mjy, ferr_mjy = conv
        lam = getattr(filt, 'wavelength', None)
        points.append({'band': band,
                       'wave_nm': (lam / 10.0) if lam else None,
                       'f_mjy': f_mjy, 'ferr_mjy': ferr_mjy})
        # sedpy Filter 可从 (波长[Å], 透过率) 数组直接构造
        sed_filters.append(SedpyFilter(
            kname=f'ajst_{band}',
            data=(np.asarray(wl, dtype=float), np.asarray(tr, dtype=float))))
        seen.add(band)
    if not points:
        raise ValueError('无可用测光点（全部缺透过率曲线或无法换算）')
    obs = {'filters': sed_filters,
           'maggies': np.array([pt['f_mjy'] for pt in points]) / _MJY_PER_MAGGIE,
           'maggies_unc': np.array([pt['ferr_mjy'] for pt in points]) / _MJY_PER_MAGGIE,
           'phot_mask': np.ones(len(points), dtype=bool),
           'wavelength': None,
           'spectrum': None}
    return obs, points, warnings


# ─── 模型 ───

def _prior_pair(config, name):
    pr = (config.get('priors') or {}).get(name)
    if pr:
        return float(pr[0]), float(pr[1])
    lo, hi = _DEFAULT_PRIORS[name]
    return float(lo), float(hi)


def build_model(config):
    """组合 prospector 模型（delayed-tau parametric_sfh + 可选组件）。

    返回 SpecModel。自由参数：mass, tage, tau, logzsol, dust2
    （photoz 模式再加 zred）。
    """
    lib = _import_prospector()
    TemplateLibrary, priors, SpecModel = (lib['TemplateLibrary'],
                                          lib['priors'], lib['SpecModel'])
    mp = TemplateLibrary['parametric_sfh']

    # 先验覆盖（mass/tau 用 LogUniform——对数空间均匀，对应"LogMass"语义；
    # tage/dust2/logzsol 用 TopHat）
    lo, hi = _prior_pair(config, 'mass')
    mp['mass']['prior'] = priors.LogUniform(mini=lo, maxi=hi)
    mp['mass']['init'] = math.sqrt(lo * hi)
    lo, hi = _prior_pair(config, 'tau')
    mp['tau']['prior'] = priors.LogUniform(mini=lo, maxi=hi)
    mp['tau']['init'] = math.sqrt(lo * hi)
    lo, hi = _prior_pair(config, 'tage')
    mp['tage']['prior'] = priors.TopHat(mini=lo, maxi=hi)
    mp['tage']['init'] = 0.5 * (lo + hi)
    lo, hi = _prior_pair(config, 'dust2')
    mp['dust2']['prior'] = priors.TopHat(mini=lo, maxi=hi)
    mp['dust2']['init'] = 0.5 * (lo + hi)
    lo, hi = _prior_pair(config, 'logzsol')
    mp['logzsol']['prior'] = priors.TopHat(mini=lo, maxi=hi)
    mp['logzsol']['init'] = 0.5 * (lo + hi)

    mode = config.get('mode', 'fixed')
    if mode == 'photoz':
        z_min, z_max = float(config['z_min']), float(config['z_max'])
        mp['zred']['isfree'] = True
        mp['zred']['init'] = 0.5 * (z_min + z_max)
        # 距离按 prospector 默认宇宙学处理：zred 自由时 sedmodel.flux_norm
        # 用 cosmo.luminosity_distance(zred)（WMAP9），无需 lumdist 参数
        mp['zred']['prior'] = priors.TopHat(mini=z_min, maxi=z_max)
    else:
        mp['zred']['isfree'] = False
        mp['zred']['init'] = float(config['redshift'])

    if config.get('use_nebular'):
        mp.update(TemplateLibrary['nebular'])
    if config.get('use_duste'):
        mp.update(TemplateLibrary['dust_emission'])
    # IGM 吸收（FSPS 参数，Madau 1995，默认开）
    mp['add_igm_absorption'] = {'N': 1, 'isfree': False,
                                'init': bool(config.get('use_igm', True))}
    return SpecModel(mp)


# ─── 采样结果提取 ───

def _samples_from_output(output, sampler_kind, model):
    """fit_model 输出 → (samples[ns, ndim], logprob[ns], 采样耗时 s)。"""
    sresult, ts = output['sampling']
    if sresult is None:
        raise RuntimeError('采样未运行（fit_model 未返回采样结果）')
    if sampler_kind == 'dynesty':
        from dynesty.utils import resample_equal
        weights = np.exp(sresult['logwt'] - sresult['logz'][-1])
        samples = resample_equal(sresult['samples'], weights)
        logprob = np.asarray(sresult['logl'])
        raw = np.asarray(sresult['samples'])
        # best 用原始链上最大似然点（resample 后对应关系丢失）
        return samples, raw, logprob, ts
    # emcee：生产链（burn-in 已被 prospector 流程丢弃）
    chain = sresult.get_chain(flat=True)
    logprob = sresult.get_log_prob(flat=True)
    return chain, chain, np.asarray(logprob), ts


def _scalar(v):
    """model.params 值（np.atleast_1d 数组）→ 标量 float。"""
    return float(np.atleast_1d(v).ravel()[0])


def _delayed_tau_sfr(mass_formed, tage, tau):
    """delayed-tau 解析 SFR(tage) = M × (tage/τ²) e^(−tage/τ) [M☉/yr]；
    M 为形成质量，tage/τ 单位 Gyr。"""
    if tau <= 0:
        return 0.0
    return mass_formed * (tage / tau**2) * math.exp(-tage / tau) / _GYR_PER_YR


def _derived_at_theta(model, sps, theta, compute_mfrac=False):
    """单点后验派生量。compute_mfrac=True 时调一次 sps 拿存活质量比例
    （mfrac = sps 存活质量/形成质量，来自 get_galaxy_spectrum 的第三返回值）。"""
    model.set_parameters(theta)
    p = model.params
    mass_formed = float(np.sum(p['mass']))
    tage = _scalar(p['tage'])
    tau = _scalar(p['tau'])
    d = {'mass_formed': mass_formed,
         'sfr': _delayed_tau_sfr(mass_formed, tage, tau),
         'Av': _scalar(p['dust2']) * _AV_PER_DUST2,
         'tage': tage}
    if compute_mfrac:
        _wave, _spec, mfrac = sps.get_galaxy_spectrum(**p)
        d['m_star'] = mass_formed * float(np.squeeze(mfrac))
    return d


def _free_param_dict(model, theta):
    """theta 向量 → {自由参数名: 值}（标量）。"""
    out = {}
    for name, sl in model.theta_index.items():
        v = np.atleast_1d(theta[sl])
        out[name] = float(v[0]) if v.size == 1 else [float(x) for x in v]
    return out


# ─── 画图 ───

def plot_sed(model, obs, points, out_png):
    """最佳模型 SED（log-log，观测点带误差棒，风格对齐 runner.py plot_sed）。

    模型曲线用 predict() 之后缓存的 rest-frame 谱：model._wave（Å，静止系）
    × (1+zred) → 观测波长，model._norm_spec（maggies，含流量归一）→ mJy。
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    zred = _scalar(model.params.get('zred', 0))
    wave_nm = np.asarray(model._wave) * (1.0 + zred) / 10.0
    fnu_mjy = np.asarray(model._norm_spec) * _MJY_PER_MAGGIE

    fig, ax = plt.subplots(figsize=(7, 5))
    m = fnu_mjy > 0
    if not m.any():
        plt.close(fig)
        raise ValueError('best model 流量全为 <= 0，无法绘图')
    ax.plot(wave_nm[m], fnu_mjy[m], '-', color='0.3', lw=1.2,
            label='prospector best model')
    xs = [pt['wave_nm'] for pt in points if pt['wave_nm']]
    if xs:
        ax.set_xlim(min(min(xs), wave_nm[m].min()) * 0.5,
                    max(max(xs), wave_nm[m].max()) * 2.0)
    for pt in points:
        if pt['wave_nm']:
            ax.errorbar(pt['wave_nm'], pt['f_mjy'], yerr=pt['ferr_mjy'],
                        fmt='o', ms=6, capsize=3, label=pt['band'])
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel('wavelength [nm]')
    ax.set_ylabel(r'$F_\nu$ [mJy]')
    ax.title.set_text('Host galaxy SED (prospector)')
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)


def plot_corner(samples, labels, out_png):
    """后验角图（corner 包；样本数超上限时随机抽稀）。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import corner

    if len(samples) > _CORNER_SAMPLE_MAX:
        idx = np.random.default_rng(42).choice(len(samples),
                                               _CORNER_SAMPLE_MAX,
                                               replace=False)
        samples = samples[idx]
    fig = corner.corner(samples, labels=labels, show_titles=True,
                        title_fmt='.3g', quantiles=[0.16, 0.5, 0.84])
    fig.savefig(out_png, dpi=120)
    plt.close(fig)


# ─── 文本摘要 ───

def _write_results_txt(path, params, chi2, chi2_reduced, n_data, sampler_kind,
                       config):
    lines = ['# AJST hostfit — prospector 拟合结果摘要', '']
    lines.append(f'sampler      = {sampler_kind}')
    lines.append(f'mode         = {config.get("mode", "fixed")}')
    lines.append(f'use_nebular  = {bool(config.get("use_nebular"))}')
    lines.append(f'use_duste    = {bool(config.get("use_duste"))}')
    lines.append(f'use_igm      = {bool(config.get("use_igm", True))}')
    lines.append(f'chi2         = {chi2:.6g}  (n_data={n_data})')
    lines.append(f'reduced_chi2 = {chi2_reduced:.6g}')
    lines.append('')
    lines.append(f'{"param":<14} {"best":>14} {"bayes(median)":>16} {"+/-1sigma":>14}')
    names = sorted(set(params['best']) | set(params['bayes']))
    for n in names:
        best = params['best'].get(n)
        bayes = params['bayes'].get(n)
        err = params['bayes_err'].get(n)
        lines.append(f'{n:<14} {best if best is not None else "-":>14} '
                     f'{bayes if bayes is not None else "-":>16} '
                     f'{err if err is not None else "-":>14}')
    lines.append('')
    lines.append('# 备注: mass/mass_formed/m_star 单位 M☉; sfr 单位 M☉/yr; '
                 'tage/tau 单位 Gyr; dust2 为 5500Å V 带光学深度, '
                 'Av = 1.086 × dust2 [mag]; logzsol = log10(Z/Z☉)')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


# ─── 主入口 ───

def run(job_id, config, log, workdir=None, filters=None):
    """执行一次 prospector 拟合。返回 {'params', 'chi2', 'warnings'}。

    workdir/filters 默认自行获取（jobs 层会显式传 workdir；filters 为 None 时查库）。
    """
    lib = _import_prospector()

    if workdir is None:
        from hostfit.jobs import job_dir
        from models import FittingResult
        sess = get_session()
        try:
            row = sess.get(FittingResult, job_id)
            transient_id = row.transient_id
        finally:
            sess.close()
        workdir = job_dir(transient_id, job_id)
    os.makedirs(workdir, exist_ok=True)

    if filters is None:
        sess = get_session()
        try:
            filters = {f.id: f for f in sess.query(FilterDef).all()}
        finally:
            sess.close()

    # 1. 观测表（先对 gext_corr 非真的测光行做银消改正）
    config = _apply_gext_correction(job_id, config, log)
    obs, points, warnings = build_obs(config, filters)
    for w in warnings:
        log('警告: ' + w)
    log(f'观测表: {len(points)} 个波段: {[pt["band"] for pt in points]}')
    obs = lib['fix_obs'](obs)

    # 2. 模型 + SPS
    model = build_model(config)
    log('自由参数: ' + ', '.join(model.free_params))
    sps = lib['CSPSpecBasis'](zcontinuous=1)
    log(f'FSPS 数据目录 SPS_HOME={os.environ.get("SPS_HOME")}')

    # 3. 采样
    sampler_kind = config.get('sampler') or 'dynesty'
    if sampler_kind == 'emcee':
        emcee_cfg = config.get('emcee') or {}
        nwalkers = int(emcee_cfg.get('nwalkers') or 32)
        niter = int(emcee_cfg.get('niter') or 3000)
        nburn = int(emcee_cfg.get('nburn') or 500)
        log(f'开始 emcee 采样: nwalkers={nwalkers}, nburn={nburn}, niter={niter}')
        output = lib['fit_model'](obs, model, sps, emcee=True, dynesty=False,
                                  nwalkers=nwalkers, nburn=[nburn], niter=niter,
                                  progress=False)
    else:
        dyn_cfg = config.get('dynesty') or {}
        nlive = int(dyn_cfg.get('nlive') or 100)
        log(f'开始 dynesty 采样: nlive={nlive}')
        output = lib['fit_model'](obs, model, sps, dynesty=True,
                                  nested_nlive_init=nlive,
                                  nested_nlive_batch=nlive,
                                  nested_dlogz_init=0.1,
                                  # 默认 10000 会把动态批次阶段拖到 10 分钟以上；
                                  # 1000 个有效样本对后验中位数/1σ 已足够
                                  nested_target_n_effective=1000,
                                  print_progress=False)
    samples, raw_samples, logprob, ts = _samples_from_output(
        output, sampler_kind, model)
    log(f'采样完成，耗时 {ts:.1f}s，后验样本 {len(samples)} 个')

    labels = model.theta_labels()
    n_free = len(labels)

    # 4. best（最大似然）+ bayes（后验中位数 ± 1σ）
    i_best = int(np.argmax(logprob))
    theta_best = np.asarray(raw_samples[i_best])
    best = _free_param_dict(model, theta_best)
    bayes, bayes_err = {}, {}
    for j, name in enumerate(labels):
        col = samples[:, j]
        q16, q50, q84 = np.percentile(col, [16, 50, 84])
        bayes[name] = float(q50)
        bayes_err[name] = float(0.5 * (q84 - q16))

    # 5. 派生量：best 点 + 后验子样本逐个算（≤200 抽）再取中位/1σ
    best.update(_derived_at_theta(model, sps, theta_best, compute_mfrac=True))
    n_sub = min(len(samples), _POSTERIOR_DERIVED_MAX)
    idx = np.random.default_rng(42).choice(len(samples), n_sub, replace=False)
    derived_draws = {'mass_formed': [], 'm_star': [], 'sfr': [], 'Av': [],
                     'tage': []}
    for i in idx:
        d = _derived_at_theta(model, sps, np.asarray(samples[i]),
                              compute_mfrac=True)
        for k in derived_draws:
            derived_draws[k].append(d[k])
    for k, vals in derived_draws.items():
        q16, q50, q84 = np.percentile(vals, [16, 50, 84])
        bayes[k] = float(q50)
        bayes_err[k] = float(0.5 * (q84 - q16))

    # 红移输出键统一为 redshift（photoz 时 zred 为自由参数）
    if config.get('mode') == 'photoz':
        best['redshift'] = best.pop('zred')
        bayes['redshift'] = bayes.pop('zred')
        bayes_err['redshift'] = bayes_err.pop('zred')
    else:
        best['redshift'] = float(config['redshift'])
        bayes['redshift'] = float(config['redshift'])
    params = {'best': best, 'bayes': bayes, 'bayes_err': bayes_err}

    # 6. best 模型测光残差 χ²（同时把内部谱缓存好供 SED 图用）
    _spec, phot_best, _mfrac = model.predict(theta_best, obs=obs, sps=sps)
    phot_best = np.atleast_1d(phot_best)
    mask = obs['phot_mask']
    resid = (obs['maggies'][mask] - phot_best[mask]) / obs['maggies_unc'][mask]
    chi2 = float(np.sum(resid**2))
    n_data = int(mask.sum())
    dof = max(n_data - n_free, 1)
    chi2_reduced = chi2 / dof
    log(f'chi2 = {chi2:.4g} (n_data={n_data}, n_free={n_free}), '
        f'reduced = {chi2_reduced:.4g}')
    log(f'best = {best}')

    # 7. 产物
    result_json = {'engine': 'prospector', 'sampler': sampler_kind,
                   'params': params, 'chi2': chi2,
                   'chi2_reduced': chi2_reduced, 'n_data': n_data,
                   'n_free': n_free, 'sampling_seconds': round(ts, 2),
                   'config': config, 'warnings': warnings}
    with open(os.path.join(workdir, 'result.json'), 'w', encoding='utf-8') as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)
    _write_results_txt(os.path.join(workdir, 'results.txt'),
                       params, chi2, chi2_reduced, n_data, sampler_kind, config)

    try:
        plot_sed(model, obs, points, os.path.join(workdir, 'sed.png'))
        log('SED 图已生成: sed.png')
    except Exception as e:
        warnings.append(f'SED 图生成失败: {e}')
        log(f'警告: SED 图生成失败: {e}')
    try:
        plot_corner(samples, labels, os.path.join(workdir, 'corner.png'))
        log('corner 图已生成: corner.png')
    except Exception as e:
        warnings.append(f'corner 图生成失败: {e}')
        log(f'警告: corner 图生成失败: {e}')

    return {'params': params, 'chi2': chi2_reduced, 'warnings': warnings}
