"""VegasAfterglow 组合模型引擎（修改版拟合程序接入）。

对应作者修改版拟合工作区（Vegas_run_unified.ipynb / run_batch_fit.py）的网页化：
模型组合按"情形"(case) 选择，先验模板来自随代码分发的
fitting/vegas_unified/prior_configs/<case>.json（先验的唯一来源），带联合物理约束
的情形（frs_plus_fs / two_comp_fs）自动改走自定义 MCMC 外壳
（fitting/vegas_unified/custom_mcmc.py，内置 Fitter 不支持联合先验）。

config 结构：
{
  case: prior_configs/ 下的任一情形名（fs / fs_rs / fs_inject / frs_plus_fs /
        two_comp_fs / fs_gaussian / fs_wind / fs_smc），
  priors: {参数名: {min, max, scale: 'log' | 'linear' | 'fixed'}},   # 缺省用模板
  sampler: {nsteps, nburn, seed, top_k, npool}
}
数据约定：data 为 jobs.prepare_data() 的输出，流量单位 mJy，时间 s，频率 Hz。

通用约定（与修改版工作区一致）：xi_e=1、on-axis（theta_v 固定 0，可在 JSON 放开）；
喷流结构 / 环境介质 / 宿主消光三个物理轴由 prior JSON 顶层字段
jet / medium / extinction 选择（缺省 tophat / ism / 无消光），成分开关
（rvs_shock / magnetar）由 fitter_kwargs 字段给出。
"""
import json
import math
import os
import time

import numpy as np

from .base import BaseEngine, register
from ..vegas_unified import (CONSTRAINTS, clean_dataframe, compute_metrics,
                             make_flux_functions, plot_corner, required_params,
                             run_mcmc, save_products)
from ..vegas_unified.custom_mcmc import _to_physical

MJY_TO_CGS = 1e-26   # mJy → erg/cm^2/s/Hz
CGS_TO_MJY = 1e26

_PRIOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'vegas_unified', 'prior_configs')

_SAMPLER_DEFAULTS = {'nsteps': 20000, 'nburn': 6000, 'seed': 42, 'top_k': 10,
                     'npool': 4}
_NPOOL_MAX = 8

# 展示用模型情形标签（未列出的新情形回退为情形名本身）
_CASE_LABELS = {
    'fs': '单成分正向激波 FS',
    'fs_rs': '正向+反向激波 FS+RS',
    'fs_inject': '正向激波+磁星注入 FS+inject',
    'frs_plus_fs': '正反激波对+独立正向激波 FS+RS+FS',
    'two_comp_fs': '双成分正向激波 two-component FS',
    'fs_gaussian': '单成分正向激波 FS（gaussian 喷流）',
    'fs_wind': '单成分正向激波 FS（wind 星风介质）',
    'fs_smc': '单成分正向激波 FS + 宿主消光 SMC',
}


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


def _cases():
    """全部模型情形：枚举 prior_configs/*.json（与 run_batch_fit.py 做法一致）"""
    return sorted(os.path.splitext(f)[0]
                  for f in os.listdir(_PRIOR_DIR) if f.endswith('.json'))


def _load_prior_file(case):
    """读取 prior_configs/<case>.json（随代码分发，只读）"""
    with open(os.path.join(_PRIOR_DIR, f'{case}.json'), encoding='utf-8') as f:
        return json.load(f)


def _template_priors(case):
    """该情形的默认先验字典 {名: {min, max, scale}}，保持 JSON 内参数顺序"""
    return {p['name']: {'min': p['lower'], 'max': p['upper'], 'scale': p['scale']}
            for p in _load_prior_file(case)['params']}


def _fitter_kwargs(case):
    """由先验 JSON 组装内置 Fitter 构造参数：jet/medium/extinction + 成分开关
    （fs_rs 的 rvs_shock、fs_inject 的 magnetar 在 fitter_kwargs 字段中给出）"""
    cfg = _load_prior_file(case)
    kw = {'jet': cfg.get('jet', 'tophat'), 'medium': cfg.get('medium', 'ism')}
    if cfg.get('extinction'):
        kw['extinction'] = cfg['extinction']
    kw.update(cfg.get('fitter_kwargs') or {})
    return kw


def _constraint_for(case):
    """该情形的联合物理约束 (callable, 描述)：按 case 名或喷流结构名命中"""
    jet = _load_prior_file(case).get('jet', 'tophat')
    return CONSTRAINTS.get(case) or CONSTRAINTS.get(jet) or (None, None)


def _effective_engine(case):
    """实际拟合引擎：先验 JSON 的 fit_engine，但带联合约束的情形必须走自定义外壳"""
    engine = _load_prior_file(case).get('fit_engine', 'fitter')
    if _constraint_for(case)[0] is not None:
        return 'custom'
    return engine


def merge_priors(config):
    """模板 + 用户覆盖（仅覆盖已有参数的值，不改变参数集合与顺序）"""
    case = config.get('case', 'fs')
    priors = _template_priors(case)
    for name, p in (config.get('priors') or {}).items():
        if name in priors:
            priors[name] = {**priors[name], **p}
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


def _bands_to_dataframe(bands):
    """prepare_data() 的 bands → custom_mcmc 约定的 DataFrame（CGS，按时间升序）"""
    import pandas as pd

    rows = []
    for band in bands:
        nu = float(band['nu'])
        for t, f, fe, w, ul in zip(band['t'], band['f'], band['ferr'],
                                   band['weights'], band['is_ul']):
            rows.append({'t_sec': float(t), 'nu_hz': nu,
                         'band_label': str(band['band']),
                         'f_nu_cgs': float(f) * MJY_TO_CGS,
                         'f_nu_err_cgs': float(fe) * MJY_TO_CGS,
                         'weights': float(w), 'upperlimit': bool(ul)})
    return clean_dataframe(pd.DataFrame(rows))


@register
class VegasUnifiedEngine(BaseEngine):
    name = 'vegas_unified'
    label = 'VegasAfterglow 组合模型（定制先验）'

    @property
    def version(self):
        """已安装的 VegasAfterglow 包版本（未安装返回 None）"""
        try:
            from importlib.metadata import version as _v
            return _v('VegasAfterglow')
        except Exception:
            return None

    def model_label(self, config):
        return f"{self.name}:{config.get('case', 'fs')}"

    # ── 配置模式（供前端渲染表单） ──
    def config_schema(self):
        case_info = {}
        for case in _cases():
            cfg = _load_prior_file(case)
            constraint_desc = _constraint_for(case)[1]
            case_info[case] = {
                'label': _CASE_LABELS.get(case, case),
                'description': cfg.get('description', ''),
                'engine': _effective_engine(case),
                'constraint': constraint_desc,
            }
        return {
            'name': self.name,
            'label': self.label,
            'options': {'case': _cases()},
            'case_info': case_info,
            'default_config': {
                'case': 'fs_rs',
                'priors': _template_priors('fs_rs'),
                'sampler': dict(_SAMPLER_DEFAULTS),
            },
            # 各情形的完整默认先验，前端切换 case 时整套重建
            'priors_by_case': {case: _template_priors(case) for case in _cases()},
            'sampler': {**_SAMPLER_DEFAULTS, 'npool_max': _NPOOL_MAX},
        }

    # ── 配置校验 ──
    def validate_config(self, config):
        errors = []
        case = config.get('case', 'fs')
        if case not in _cases():
            return [f'未知模型情形: {case}']

        template = _template_priors(case)
        priors = merge_priors(config)
        for name, p in (config.get('priors') or {}).items():
            if name not in template:
                errors.append(f'{name}: 不属于情形 {case} 的参数')
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

        if _effective_engine(case) == 'fitter':
            # 内置 Fitter 路径交给包内规则做模型组合校验
            from VegasAfterglow import Fitter

            try:
                fitter = Fitter(z=1.0, lumi_dist=1e28, **_fitter_kwargs(case))
                fitter.validate_parameters(_build_param_defs(priors))
            except (ValueError, AttributeError, TypeError) as e:
                errors.append(str(e))
        else:
            # 自定义外壳路径：按注册表校验参数集合（theta_v 为可选参数）
            cfg = _load_prior_file(case)
            try:
                required = required_params(cfg.get('model', case),
                                           cfg.get('jet', 'tophat'),
                                           cfg.get('medium', 'ism'),
                                           cfg.get('extinction'))
            except ValueError as e:
                errors.append(str(e))
            else:
                missing = required - set(priors)
                extra = set(priors) - required - {'theta_v'}
                if missing:
                    errors.append(f'缺少必需参数先验: {sorted(missing)}')
                if extra:
                    errors.append(f'多余的参数先验: {sorted(extra)}')
        samp = {**_SAMPLER_DEFAULTS, **(config.get('sampler') or {})}
        for key in ('nsteps', 'nburn', 'seed', 'top_k', 'npool'):
            try:
                if int(samp[key]) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f'sampler.{key} 须为非负整数')
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
        from VegasAfterglow import Scale

        case = config.get('case', 'fs')
        prior_cfg = _load_prior_file(case)
        model_case = prior_cfg.get('model', case)   # 成分拓扑（fs_smc → fs 等）
        jet = prior_cfg.get('jet', 'tophat')
        medium = prior_cfg.get('medium', 'ism')
        extinction = prior_cfg.get('extinction')
        samp = {**_SAMPLER_DEFAULTS, **(config.get('sampler') or {})}
        nsteps, nburn = int(samp['nsteps']), int(samp['nburn'])
        seed = int(samp['seed'])
        npool = max(1, min(int(samp['npool']), _NPOOL_MAX))

        z = float(data['z'])
        lumi_dist = Planck18.luminosity_distance(z).to(u.cm).value

        priors = merge_priors(config)
        param_defs = _build_param_defs(priors)
        free_defs = [d for d in param_defs if d.scale != Scale.fixed]
        fixed_params = {d.name: float(d.lower) for d in param_defs
                        if d.scale == Scale.fixed}
        names = [d.name for d in free_defs]

        df = _bands_to_dataframe(data['bands'])
        if len(df) == 0:
            raise ValueError('清洗后无可用数据点')

        model_flux_raw, comp_fn = make_flux_functions(
            model_case, lumi_dist, z, jet=jet, medium=medium, extinction=extinction)

        def model_flux(p, t, nu):
            """自由参数 + 固定参数（如 theta_v=0）合并后求总流量"""
            return model_flux_raw({**fixed_params, **p}, t, nu)

        constraint, constraint_desc = _constraint_for(case)
        engine_kind = prior_cfg.get('fit_engine', 'fitter')
        if constraint is not None:
            engine_kind = 'custom'   # 内置 Fitter 不支持联合先验约束

        ext_txt = (f'宿主消光 {extinction} (Pei92)' if extinction
                   else '不考虑宿主消光')
        header = [
            f'模型({case}): {prior_cfg.get("description", "")}',
            f'物理轴: jet={jet}, medium={medium}, {ext_txt}; xi_e 固定为 1',
            f'数据: {len(df)} 点 / {df["nu_hz"].nunique()} 个频率'
            f'（单色流量密度, 已剔除 t<=0 点）',
            f'红移 z = {z},  光度距离 = {lumi_dist:.4e} cm',
            f'emcee: nsteps={nsteps}, nburn={nburn}, seed={seed}, 引擎={engine_kind}',
        ]
        if constraint_desc:
            header.append(f'约束: {constraint_desc}（走自定义 MCMC 外壳）')
        for line in header:
            log(line)
        log('先验: ' + ', '.join(f'{d.name}[{d.lower:.3g},{d.upper:.3g},'
                                 f'{d.scale.name}]' for d in param_defs))

        # ── 拟合 ──
        if engine_kind == 'fitter':
            from VegasAfterglow import Fitter

            np.random.seed(seed)   # 控制内置 Fitter 初始 walker 位置
            fitter = Fitter(z=z, lumi_dist=lumi_dist, **_fitter_kwargs(case))
            for band in data['bands']:
                fitter.add_flux_density(
                    float(band['nu']), np.asarray(band['t'], dtype=float),
                    np.asarray(band['f'], dtype=float) * MJY_TO_CGS,
                    np.asarray(band['ferr'], dtype=float) * MJY_TO_CGS,
                    weights=np.asarray(band['weights'], dtype=float),
                    label=band['band'])
            result = fitter.fit(param_defs, sampler='emcee', nsteps=nsteps,
                                nburn=nburn, top_k=int(samp['top_k']), npool=npool)
            log(str(result))
            fitter.save(os.path.join(workdir, 'chain_record.h5'))
            flat = result.samples.reshape(-1, len(free_defs))
            flat_logp = result.log_probs
        else:
            _, flat, flat_logp, _, _ = run_mcmc(
                model_flux, free_defs, df, workdir,
                nsteps=nsteps, nburn=nburn, seed=seed,
                constraint=constraint,
                config=dict(model=prior_cfg.get('description', ''), case=case,
                            jet=jet, medium=medium,
                            constraint=constraint_desc,
                            z=z, lumi_dist=lumi_dist, xi_e=1.0,
                            host_extinction=extinction),
                n_workers=npool)

        # ── 后处理：metrics.txt + corner + 光变图（错位分波段 + 比值子图） ──
        metrics = save_products(workdir, flat, flat_logp, free_defs, names, df,
                                model_flux, header, component_flux_fn=comp_fn,
                                with_ratio=True)
        # 前端契约文件名：corner.png
        os.replace(os.path.join(workdir, 'corner_plot.png'),
                   os.path.join(workdir, 'corner.png'))

        # 模型光变 lc_model.json（每波段 60 个 log 间隔时刻，68% 可信带，mJy）
        self._make_lc_model(model_flux, flat, flat_logp, free_defs, fixed_params,
                            data['bands'], os.path.join(workdir, 'lc_model.json'),
                            log, seed=seed)

        # 参数汇总：最佳（最高后验）± 后验 1σ（16/84 分位，物理空间）
        params_out = self._summarize_params(flat, flat_logp, free_defs, fixed_params)

        chi2 = float(metrics['chi2_min'])
        dof = int(metrics['dof'])
        runtime = time.time() - t_start
        log(f"chi2={chi2:.3g}, dof={dof}, BIC={metrics['BIC']:.3g}, "
            f"AIC={metrics['AIC']:.3g}, 耗时 {runtime:.1f}s")

        return {
            'params': params_out,
            'chi2': chi2,
            'dof': dof,
            'bic': float(metrics['BIC']),
            'aic': float(metrics['AIC']),
            'n_steps': nsteps,
            'runtime_s': runtime,
        }

    # ── 参数汇总 ──
    @staticmethod
    def _summarize_params(flat, flat_logp, free_defs, fixed_params):
        from VegasAfterglow import Scale

        phys = np.column_stack([
            10.0 ** flat[:, i] if d.scale == Scale.log else flat[:, i]
            for i, d in enumerate(free_defs)
        ])
        best = phys[np.argmax(flat_logp)]
        p16 = np.percentile(phys, 16, axis=0)
        p84 = np.percentile(phys, 84, axis=0)
        params_out = {}
        for j, d in enumerate(free_defs):
            params_out[d.name] = {'v': float(best[j]),
                                  'err': float((p84[j] - p16[j]) / 2.0)}
        # 固定参数一并给出，便于前端展示完整模型
        for name, val in fixed_params.items():
            params_out[name] = {'v': float(val), 'err': 0.0}
        return params_out

    # ── 模型光变 lc_model.json ──
    @staticmethod
    def _make_lc_model(model_flux, flat, flat_logp, free_defs, fixed_params,
                       bands, path, log, n_cred=100, seed=0):
        """每个波段在自身数据时间范围内取 60 个 log 间隔时刻，从后验抽样
        （缺省 100 个样本）算中值与 16/84 分位（CGS → mJy）。"""
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(flat), size=min(n_cred, len(flat)), replace=False)
        out_bands = []
        for band in bands:
            t_data = np.asarray(band['t'], dtype=float)
            tmin, tmax = float(t_data.min()), float(t_data.max())
            if tmin <= 0:
                tmin = max(tmax * 1e-3, 1.0)
            if tmax <= tmin:
                tmin, tmax = tmin * 0.8, tmax * 1.2
            t_grid = np.logspace(math.log10(tmin), math.log10(tmax), 60)
            nu_arr = np.full_like(t_grid, float(band['nu']))
            entry = {'band': band['band'], 'nu': float(band['nu']),
                     't': t_grid.tolist()}
            try:
                curves = np.array([
                    np.asarray(model_flux(
                        {**fixed_params, **_to_physical(flat[i], free_defs)},
                        t_grid, nu_arr), dtype=float) * CGS_TO_MJY
                    for i in idx
                ])
                entry['f_med'] = np.percentile(curves, 50, axis=0).tolist()
                entry['f_lo'] = np.percentile(curves, 16, axis=0).tolist()
                entry['f_hi'] = np.percentile(curves, 84, axis=0).tolist()
            except Exception as e:
                log(f'lc_model 生成失败（{band["band"]}）: {e}，退化为 best 单线')
                p_best = {**fixed_params,
                          **_to_physical(flat[np.argmax(flat_logp)], free_defs)}
                f = (np.asarray(model_flux(p_best, t_grid, nu_arr), dtype=float)
                     * CGS_TO_MJY).tolist()
                entry['f_med'] = f
                entry['f_lo'] = None
                entry['f_hi'] = None
            out_bands.append(entry)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'ci': 0.68, 'unit': 'mJy', 'bands': out_bands}, f)
