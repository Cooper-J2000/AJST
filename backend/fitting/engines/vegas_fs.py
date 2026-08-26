"""VegasAfterglow 正向激波（+ 可选反向激波/磁陀星/宿主消光）余辉拟合引擎。

config 结构：
{
  jet: 'tophat' | 'gaussian' | 'powerlaw',
  medium: 'ism' | 'wind',
  rvs_shock: bool,
  magnetar: bool,
  extinction: 'none' | 'smc' | 'lmc' | 'mw',
  priors: {参数名: {min, max, scale: 'log' | 'linear' | 'fixed'}},   # 缺省用模板
  sampler: {nsteps, nburn, top_k, npool}
}
数据约定：data 为 jobs.prepare_data() 的输出，流量单位 mJy，时间 s，频率 Hz。
"""
import json
import math
import os
import time

import numpy as np

from .base import BaseEngine, register

MJY_TO_CGS = 1e-26   # mJy → erg/cm^2/s/Hz
CGS_TO_MJY = 1e26

# ─── 默认先验模板（与用户 notebook 一致；log = log10 空间均匀，fixed = 固定值） ───
_BASE_PRIORS = {
    'E_iso':   {'min': 1e51, 'max': 1e54, 'scale': 'log'},
    'Gamma0':  {'min': 50,   'max': 1000, 'scale': 'log'},
    'theta_c': {'min': 0.01, 'max': 0.3,  'scale': 'linear'},
    'theta_v': {'min': 0.0,  'max': 0.0,  'scale': 'fixed'},
    'p':       {'min': 2.1,  'max': 2.8,  'scale': 'linear'},
    'eps_e':   {'min': 1e-3, 'max': 0.5,  'scale': 'log'},
    'eps_B':   {'min': 1e-5, 'max': 0.1,  'scale': 'log'},
    'xi_e':    {'min': 0.1,  'max': 1.0,  'scale': 'linear'},
}
_MEDIUM_PRIORS = {
    'ism':  {'n_ism':  {'min': 1e-3, 'max': 10.0, 'scale': 'log'}},
    'wind': {'A_star': {'min': 1e-3, 'max': 10.0, 'scale': 'log'}},
}
_RVS_PRIORS = {
    'tau':     {'min': 0.1,  'max': 1000.0, 'scale': 'log'},
    'p_r':     {'min': 2.1,  'max': 2.8,    'scale': 'linear'},
    'eps_e_r': {'min': 1e-3, 'max': 0.5,    'scale': 'log'},
    'eps_B_r': {'min': 1e-5, 'max': 0.1,    'scale': 'log'},
    'xi_e_r':  {'min': 0.1,  'max': 1.0,    'scale': 'linear'},
}
_MAGNETAR_PRIORS = {
    'L0': {'min': 1e47, 'max': 1e50,  'scale': 'log'},
    't0': {'min': 100,  'max': 10000, 'scale': 'log'},
    'q':  {'min': 1.5,  'max': 3.0,   'scale': 'linear'},
}
_EXTINCTION_PRIOR = {'A_V': {'min': 0.0, 'max': 3.0, 'scale': 'linear'}}
# 结构化喷流的额外参数（按 model_configurations.rst 补默认值）
_JET_EXTRA_PRIORS = {
    'tophat':   {},
    'gaussian': {},
    'powerlaw': {
        'k_e': {'min': 1.5, 'max': 3.0, 'scale': 'linear'},
        'k_g': {'min': 1.5, 'max': 3.0, 'scale': 'linear'},
    },
}

_JETS = sorted(_JET_EXTRA_PRIORS)
_MEDIA = sorted(_MEDIUM_PRIORS)
_EXTINCTIONS = ['none', 'smc', 'lmc', 'mw']

_SAMPLER_DEFAULTS = {'nsteps': 2000, 'nburn': 1000, 'top_k': 10, 'npool': 4}
_NPOOL_MAX = 8


def default_priors(jet, medium, rvs_shock, magnetar, extinction):
    """按模型组合拼出默认先验字典"""
    priors = dict(_BASE_PRIORS)
    priors.update(_JET_EXTRA_PRIORS.get(jet, {}))
    priors.update(_MEDIUM_PRIORS.get(medium, {}))
    if rvs_shock:
        priors.update(_RVS_PRIORS)
    if magnetar:
        priors.update(_MAGNETAR_PRIORS)
    if extinction and extinction != 'none':
        priors.update(_EXTINCTION_PRIOR)
    return priors


def merge_priors(config):
    """默认模板 + 用户覆盖，返回完整先验字典"""
    priors = default_priors(
        config.get('jet', 'tophat'), config.get('medium', 'ism'),
        bool(config.get('rvs_shock')), bool(config.get('magnetar')),
        config.get('extinction', 'none'))
    for name, p in (config.get('priors') or {}).items():
        priors[name] = dict(p)
    return priors


def _build_param_defs(priors):
    """先验字典 → VegasAfterglow ParamDef 列表（fixed 用 min 作固定值）"""
    from VegasAfterglow import ParamDef, Scale

    scale_map = {'log': Scale.log, 'linear': Scale.linear, 'fixed': Scale.fixed}
    defs = []
    for name, p in priors.items():
        sc = scale_map[p['scale']]
        lo, hi = float(p['min']), float(p['max'])
        if sc is Scale.fixed:
            hi = lo  # 固定值：上下界同取 min
        defs.append(ParamDef(name, lo, hi, sc))
    return defs


class _redirect_stdio:
    """把 fd 级的 stdout/stderr（含 C++/tqdm 输出）重定向到日志文件"""

    def __init__(self, fp):
        self._fp = fp

    def __enter__(self):
        self._saved = [os.dup(1), os.dup(2)]
        os.dup2(self._fp.fileno(), 1)
        os.dup2(self._fp.fileno(), 2)
        return self

    def __exit__(self, *exc):
        for fd, saved in zip((1, 2), self._saved):
            os.dup2(saved, fd)
            os.close(saved)
        return False


@register
class VegasFSEngine(BaseEngine):
    name = 'vegas_fs'
    label = 'VegasAfterglow 正向激波'

    @property
    def version(self):
        """已安装的 VegasAfterglow 包版本（未安装返回 None）"""
        try:
            from importlib.metadata import version as _v
            return _v('VegasAfterglow')
        except Exception:
            return None

    # ── 配置模式（供前端渲染表单） ──
    def config_schema(self):
        return {
            'name': self.name,
            'label': self.label,
            'options': {
                'jet': _JETS,
                'medium': _MEDIA,
                'rvs_shock': [True, False],
                'magnetar': [True, False],
                'extinction': _EXTINCTIONS,
            },
            'default_config': {
                'jet': 'tophat', 'medium': 'ism',
                'rvs_shock': False, 'magnetar': False,
                'extinction': 'none',
                'priors': default_priors('tophat', 'ism', False, False, 'none'),
                'sampler': dict(_SAMPLER_DEFAULTS),
            },
            # 各开关打开时需要追加的先验模板，前端按组合拼装
            'priors_by_option': {
                'medium': {m: p for m, p in _MEDIUM_PRIORS.items()},
                'jet_extra': {j: p for j, p in _JET_EXTRA_PRIORS.items()},
                'rvs_shock': _RVS_PRIORS,
                'magnetar': _MAGNETAR_PRIORS,
                'extinction': _EXTINCTION_PRIOR,
            },
            'sampler': {**_SAMPLER_DEFAULTS, 'npool_max': _NPOOL_MAX},
        }

    # ── 配置校验 ──
    def validate_config(self, config):
        errors = []
        jet = config.get('jet', 'tophat')
        medium = config.get('medium', 'ism')
        extinction = config.get('extinction', 'none')
        if jet not in _JETS:
            errors.append(f'未知 jet: {jet}')
        if medium not in _MEDIA:
            errors.append(f'未知 medium: {medium}')
        if extinction not in _EXTINCTIONS:
            errors.append(f'未知 extinction: {extinction}')
        if errors:
            return errors

        priors = merge_priors(config)
        for name, p in priors.items():
            if p.get('scale') not in ('log', 'linear', 'fixed'):
                errors.append(f'{name}: scale 须为 log/linear/fixed')
                continue
            lo, hi = p.get('min'), p.get('max')
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
                errors.append(f'{name}: min/max 缺失或非法')
                continue
            if p['scale'] == 'log' and lo <= 0:
                errors.append(f'{name}: log 先验要求 min > 0')
            if p['scale'] != 'fixed' and hi <= lo:
                errors.append(f'{name}: max 须大于 min')
        if errors:
            return errors

        # 交给包内规则做模型组合校验（缺参数/多余参数会 raise）
        from VegasAfterglow import Fitter

        fitter = Fitter(z=1.0, lumi_dist=1e28, jet=jet, medium=medium,
                        rvs_shock=bool(config.get('rvs_shock')),
                        magnetar=bool(config.get('magnetar')),
                        extinction=None if extinction == 'none' else extinction)
        try:
            fitter.validate_parameters(_build_param_defs(priors))
        except (ValueError, AttributeError) as e:
            errors.append(str(e))
        # 宿主消光开启时必须有 A_V 先验（包内不强制，静默不改正）
        if extinction != 'none' and 'A_V' not in priors:
            errors.append('extinction 开启时缺少 A_V 先验')
        return errors

    # ── 执行拟合 ──
    def run(self, config, data, workdir, log=None):
        os.makedirs(workdir, exist_ok=True)
        log_path = os.path.join(workdir, 'run.log')
        t_start = time.time()
        with open(log_path, 'w', encoding='utf-8') as lf:
            def _log(msg):
                """写一行到 run.log，并转发给调用方回调（如有）"""
                lf.write(str(msg) + '\n')
                lf.flush()
                if log:
                    log(msg)
            with _redirect_stdio(lf):
                return self._run_inner(config, data, workdir, _log, t_start)

    def _run_inner(self, config, data, workdir, log, t_start):
        from astropy.cosmology import Planck18
        import astropy.units as u
        from VegasAfterglow import Fitter

        jet = config.get('jet', 'tophat')
        medium = config.get('medium', 'ism')
        rvs = bool(config.get('rvs_shock'))
        mag = bool(config.get('magnetar'))
        ext = config.get('extinction', 'none')
        samp = {**_SAMPLER_DEFAULTS, **(config.get('sampler') or {})}
        npool = max(1, min(int(samp['npool']), _NPOOL_MAX))

        z = float(data['z'])
        lumi_dist = Planck18.luminosity_distance(z).to(u.cm).value
        log(f'z={z}, lumi_dist={lumi_dist:.4e} cm, '
            f'jet={jet}, medium={medium}, rvs_shock={rvs}, magnetar={mag}, extinction={ext}')

        fitter = Fitter(z=z, lumi_dist=lumi_dist, jet=jet, medium=medium,
                        rvs_shock=rvs, magnetar=mag,
                        extinction=None if ext == 'none' else ext)

        # 喂数据（mJy → CGS；上限点按 dataloader 惯例 f=0、err=上限值）
        for band in data['bands']:
            f_cgs = np.asarray(band['f'], dtype=float) * MJY_TO_CGS
            ferr_cgs = np.asarray(band['ferr'], dtype=float) * MJY_TO_CGS
            fitter.add_flux_density(
                float(band['nu']), np.asarray(band['t'], dtype=float),
                f_cgs, ferr_cgs,
                weights=np.asarray(band['weights'], dtype=float),
                label=band['band'])
        log(f'数据: {data["n_points"]} 点 / {len(data["bands"])} 波段')

        priors = merge_priors(config)
        param_defs = _build_param_defs(priors)
        log('先验: ' + ', '.join(f'{p.name}[{p.lower:.3g},{p.upper:.3g},{p.scale.name}]'
                                 for p in param_defs))

        result = fitter.fit(param_defs, sampler='emcee',
                            nsteps=int(samp['nsteps']), nburn=int(samp['nburn']),
                            top_k=int(samp['top_k']), npool=npool)
        log(str(result))

        # 全量快照
        fitter.save(os.path.join(workdir, 'chain_record.h5'))

        # 角图（Agg 后端，无需显示环境；失败不拖垮整个任务）
        try:
            self._make_corner(result, os.path.join(workdir, 'corner.png'))
        except Exception as e:
            log(f'角图生成失败: {e}')

        # 参数汇总：top-1 ± 后验 1σ（16/84 分位，物理空间）
        params_out = self._summarize_params(result, param_defs)

        # 模型光变（68% 可信带）
        self._make_lc_model(fitter, result, data['bands'], npool,
                            os.path.join(workdir, 'lc_model.json'), log)

        chi2 = float(-2.0 * result.top_k_log_probs[0])
        n_data, n_free = int(result.n_data), int(result.n_free_params)
        dof = n_data - n_free
        bic = chi2 + n_free * math.log(n_data) if n_data > 0 else None
        aic = chi2 + 2 * n_free
        runtime = time.time() - t_start
        log(f'chi2={chi2:.3g}, dof={dof}, BIC={bic:.3g}, AIC={aic:.3g}, 耗时 {runtime:.1f}s')

        return {
            'params': params_out,
            'chi2': chi2,
            'dof': dof,
            'bic': bic,
            'aic': aic,
            'n_steps': int(samp['nsteps']),
            'runtime_s': runtime,
        }

    # ── 角图 ──
    @staticmethod
    def _make_corner(result, path):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import corner

        # FitResult.samples 为 (n, 1, ndim)，corner 需要 2-D 的 flat_samples
        fig = corner.corner(result.flat_samples, labels=list(result.labels),
                            show_titles=True, quantiles=[0.16, 0.5, 0.84])
        fig.savefig(path, dpi=150, bbox_inches='tight')
        plt.close(fig)

    # ── 参数汇总 ──
    @staticmethod
    def _summarize_params(result, param_defs):
        """labels 为采样空间名（log 参数带 log10_ 前缀），统一换算回物理值"""
        params_out = {}
        flat = result.flat_samples
        top1 = result.top_k_params[0]
        for j, label in enumerate(result.labels):
            is_log = label.startswith('log10_')
            name = label[len('log10_'):] if is_log else label
            phys = (lambda x: 10.0 ** x) if is_log else (lambda x: x)
            p16, p84 = np.percentile(flat[:, j], [16, 84])
            params_out[name] = {
                'v': float(phys(top1[j])),
                'err': float((phys(p84) - phys(p16)) / 2.0),
            }
        # 固定参数一并给出，便于前端展示完整模型
        for pd in param_defs:
            if pd.name not in params_out:
                params_out[pd.name] = {'v': float(pd.lower), 'err': 0.0}
        return params_out

    # ── 模型光变 lc_model.json ──
    @staticmethod
    def _make_lc_model(fitter, result, bands, npool, path, log):
        """每个波段在自身数据时间范围内取 ~60 个 log 间隔时刻，
        用 flux_density_credible(ci=0.68) 算中值与上下界（CGS → mJy）。
        失败则退化为 top-1 参数单线（lo/hi 为 null）。"""
        out_bands = []
        for band in bands:
            t_data = np.asarray(band['t'], dtype=float)
            tmin, tmax = float(t_data.min()), float(t_data.max())
            if tmin <= 0:
                tmin = max(tmax * 1e-3, 1.0)
            if tmax <= tmin:
                tmin, tmax = tmin * 0.8, tmax * 1.2
            t_grid = np.logspace(math.log10(tmin), math.log10(tmax), 60)
            nu = np.array([float(band['nu'])])
            entry = {'band': band['band'], 'nu': float(band['nu']),
                     't': t_grid.tolist()}
            try:
                cred = fitter.flux_density_credible(
                    t_grid, nu, ci=0.68, n_samples=100, n_workers=npool)
                entry['f_med'] = (np.asarray(cred.median)[0] * CGS_TO_MJY).tolist()
                entry['f_lo'] = (np.asarray(cred.lower)[0] * CGS_TO_MJY).tolist()
                entry['f_hi'] = (np.asarray(cred.upper)[0] * CGS_TO_MJY).tolist()
            except Exception as e:
                log(f'flux_density_credible 失败（{band["band"]}）: {e}，退化为 top-1 单线')
                grid = fitter.flux_density_grid(result.top_k_params[0], t_grid, nu)
                f = (np.asarray(grid.total)[0] * CGS_TO_MJY).tolist()
                entry['f_med'] = f
                entry['f_lo'] = None
                entry['f_hi'] = None
            out_bands.append(entry)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'ci': 0.68, 'unit': 'mJy', 'bands': out_bands}, f)
