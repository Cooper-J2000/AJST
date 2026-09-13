"""SED 拟合模型注册表（M2 幂律系列 + M4 黑体系列）。

每个模型类提供统一接口：
  param_defs(config)            -> VegasAfterglow.ParamDef 列表（惰性 import，
                                   无拟合依赖时其余接口仍可用）
  model_flux(params, t, nu, config) -> Fν [erg/cm²/s/Hz]（custom_mcmc 约定；SED
                                   单历元拟合与 t 无关，t 形参仅为接口对齐）
  derived(params, config)       -> 派生量 dict（p、L_BB、β_ox 等）
  sanity_checks(params, derived, meta) -> 物理有效性警告列表（§4.3）
  describe()                    -> /api/sed/models 的 schema（不依赖 VegasAfterglow）

单位约定：幂律归一化 A 单位为 mJy（在 config['nu0'] 处取值），模型内部乘
1e-26 转 cgs；黑体直接输出 cgs。config 公共键：z（红移，None 表示未知）、
law（宿主消光律名）、rv / rv_free、nu0（幂律参考频率 Hz）。
"""
import math

import numpy as np

from sedfit import laws

MJY_TO_CGS = 1e-26          # 1 mJy = 1e-26 erg/cm²/s/Hz
_C_CGS = 2.99792458e10      # 光速 [cm/s]
_C_AA_PER_S = 2.99792458e18
_H = 6.62607015e-27         # 普朗克常数 [erg·s]
_K_B = 1.380649e-16         # 玻尔兹曼常数 [erg/K]
_SIGMA_SB = 5.670374419e-5  # 斯特藩-玻尔兹曼常数 [erg/cm²/s/K⁴]
_PC_CM = 3.085677581e18     # 1 pc [cm]
_NU_1KEV = 2.417989242e17   # 1 keV 光子对应频率 [Hz]


def planck_nu(nu, T):
    """普朗克函数 Bν [erg/cm²/s/Hz/sr]（cgs）"""
    nu = np.asarray(nu, dtype=float)
    x = np.minimum(_H * nu / (_K_B * T), 700.0)  # 防 expm1 上溢
    return (2.0 * _H * nu ** 3 / _C_CGS ** 2) / np.expm1(x)


def _dl_cm(z):
    """光度距离 [cm]（Planck18，复用 models.distance_modulus 的分桶缓存）"""
    from models import distance_modulus
    mu = distance_modulus(z)
    if mu is None:
        return None
    return 10.0 ** (mu / 5.0 + 1.0) * _PC_CM


def _param_defs(specs):
    """schema 列表 → ParamDef 列表（惰性 import，VegasAfterglow 为可选依赖）"""
    from VegasAfterglow import ParamDef, Scale
    return [ParamDef(n, lo, hi, Scale.log if s == 'log' else Scale.linear)
            for n, lo, hi, s in specs]


# ─── M2: 幂律 + 宿主消光 ───

class PowerLawDust:
    """Fν = A·(ν/ν0)^(−β)·10^(−0.4·A_host)（Kann+2006 / Schady+2010 标准形式）"""
    key = 'powerlaw_dust'
    label = '幂律 + 宿主消光'
    params_schema = [
        {'name': 'A', 'lo': 1e-6, 'hi': 1e4, 'scale': 'log', 'unit': 'mJy',
         'desc': 'ν0 处归一化流量（未消光）'},
        {'name': 'beta', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': '谱指数 β（Fν∝ν^(−β)）'},
        {'name': 'Av', 'lo': 0.0, 'hi': 6.0, 'scale': 'linear', 'unit': 'mag',
         'desc': '宿主 V 带消光'},
    ]
    rv_schema = {'name': 'Rv', 'lo': 1.5, 'hi': 6.0, 'scale': 'linear', 'unit': '',
                 'desc': '总选消光比（rv_free=true 时自由）'}

    @staticmethod
    def param_defs(config):
        specs = [(p['name'], p['lo'], p['hi'], p['scale'])
                 for p in PowerLawDust.params_schema]
        if config.get('rv_free'):
            specs.append(('Rv', 1.5, 6.0, 'linear'))
        return _param_defs(specs)

    @staticmethod
    def model_flux(params, t, nu, config):
        nu = np.asarray(nu, dtype=float)
        nu0 = float(config.get('nu0') or 5e14)
        f_mjy = params['A'] * (nu / nu0) ** (-params['beta'])
        f_mjy = laws.extinguish(nu, f_mjy, config.get('z'),
                                config.get('law', 'smc'), params['Av'],
                                rv=params.get('Rv') or config.get('rv'))
        return f_mjy * MJY_TO_CGS

    @staticmethod
    def derived(params, config):
        beta = params['beta']
        return {
            'beta': beta,
            'Av_host': params['Av'],
            'law': config.get('law', 'smc'),
            # p 的两种常见对应（Granot & Sari 2002；M3 闭包诊断联动）
            'p_if_nu_above_nu_c': 2.0 * beta,
            'p_if_nu_m_below_nu_below_nu_c': 2.0 * beta + 1.0,
            'p_alt_2beta_plus_2': 2.0 * beta + 2.0,
        }

    @staticmethod
    def sanity_checks(params, derived, meta):
        return _beta_checks(params.get('beta'), meta)


class PowerLawXray:
    """光学+X 双幂律扩展：ν<ν_split 为光学段（含宿主消光），ν≥ν_split 为 X 段。

    两段各自归一化、不强制连续；输出 β_ox 与暗暴判据
    （β_ox < β_X − 0.5，Jakobsson+2004 / van der Horst+2009）。"""
    key = 'powerlaw_xray'
    label = '光学+X 双幂律（β_ox/β_X 暗暴判据）'
    params_schema = [
        {'name': 'A_o', 'lo': 1e-8, 'hi': 1e4, 'scale': 'log', 'unit': 'mJy',
         'desc': '光学段 ν0 处归一化（未消光）'},
        {'name': 'beta_o', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': '光学段谱指数'},
        {'name': 'A_X', 'lo': 1e-10, 'hi': 1e2, 'scale': 'log', 'unit': 'mJy',
         'desc': 'X 段 1keV 处归一化'},
        {'name': 'beta_X', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': 'X 段谱指数'},
        {'name': 'Av', 'lo': 0.0, 'hi': 6.0, 'scale': 'linear', 'unit': 'mag',
         'desc': '宿主 V 带消光（仅作用光学段）'},
    ]
    rv_schema = PowerLawDust.rv_schema

    @staticmethod
    def param_defs(config):
        specs = [(p['name'], p['lo'], p['hi'], p['scale'])
                 for p in PowerLawXray.params_schema]
        if config.get('rv_free'):
            specs.append(('Rv', 1.5, 6.0, 'linear'))
        return _param_defs(specs)

    @staticmethod
    def model_flux(params, t, nu, config):
        nu = np.asarray(nu, dtype=float)
        nu0 = float(config.get('nu0') or 5e14)
        nu_split = float(config.get('nu_split') or 1e17)
        f_mjy = np.empty_like(nu)
        opt = nu < nu_split
        f_mjy[opt] = params['A_o'] * (nu[opt] / nu0) ** (-params['beta_o'])
        f_mjy[opt] = laws.extinguish(nu[opt], f_mjy[opt], config.get('z'),
                                     config.get('law', 'smc'), params['Av'],
                                     rv=params.get('Rv') or config.get('rv'))
        f_mjy[~opt] = params['A_X'] * (nu[~opt] / _NU_1KEV) ** (-params['beta_X'])
        return f_mjy * MJY_TO_CGS

    @staticmethod
    def derived(params, config):
        nu0 = float(config.get('nu0') or 5e14)
        nu_opt, nu_x = 6e14, _NU_1KEV  # 5000 Å / 1 keV 参考点
        # β_ox 用观测（含消光）光学流量；按文献惯例取正值定义
        # β_ox = ln(F_opt/F_X)/ln(ν_X/ν_opt)（Fν∝ν^(−β_ox)，Jakobsson+2004；
        # 设计方案公式分母写 ln(ν_opt/ν_X)，会差一个负号，此处按文献惯例实现）
        f_o = laws.extinguish(np.array([nu_opt]),
                              np.array([params['A_o'] * (nu_opt / nu0) ** (-params['beta_o'])]),
                              config.get('z'), config.get('law', 'smc'), params['Av'],
                              rv=params.get('Rv') or config.get('rv'))[0]
        f_x = params['A_X'] * (nu_x / _NU_1KEV) ** (-params['beta_X'])
        beta_ox = (math.log(f_o / f_x) / math.log(nu_x / nu_opt)
                   if f_o > 0 and f_x > 0 else float('nan'))
        beta_x = params['beta_X']
        same_seg = abs(params['beta_o'] - beta_x) < 0.1
        return {
            'beta_o': params['beta_o'], 'beta_X': beta_x,
            'beta_ox': beta_ox,
            'dark_burst': bool(np.isfinite(beta_ox) and beta_ox < beta_x - 0.5),
            'nu_c_note': ('β_o ≈ β_X：光学与 X 射线同谱段（ν_c 在 X 之上）'
                          if same_seg else
                          'β_o ≠ β_X：谱 break 位于光学—X 之间（ν_c 或 ν_m 介于其间）'),
            'p_if_nu_above_nu_c': 2.0 * beta_x,
            'p_if_nu_m_below_nu_below_nu_c': 2.0 * beta_x + 1.0,
        }

    @staticmethod
    def sanity_checks(params, derived, meta):
        warns = _beta_checks(params.get('beta_o'), meta, name='β_o')
        warns += _beta_checks(params.get('beta_X'), meta, name='β_X')
        if derived.get('dark_burst'):
            warns.append(f"β_ox={derived['beta_ox']:.2f} < β_X−0.5："
                         '满足暗暴判据（Jakobsson+2004）')
        return warns


class PowerLaw2Seg:
    """两段平滑连接幂律 + 宿主消光（单断折余辉同步辐射谱，如只有 ν_m
    或只有 ν_c 落入观测窗口的情形，Granot & Sari 2002）。

    Fν = A · [(ν/νb)^{s·β1} + (ν/νb)^{s·β2}]^{−1/s} · 10^(−0.4·A_host)
    （β1<β2 时 ν<νb 渐近 ν^{−β1}、ν>νb 渐近 ν^{−β2}）；s 为平滑度
    （越大越锐利），固定值从 config 读（默认 3.0，高级选项）。
    硬约束 β1 ≤ β2（constraint()，走 custom_mcmc 的硬约束先验）。"""
    key = 'powerlaw_2seg'
    label = '两段平滑幂律 + 宿主消光'
    params_schema = [
        {'name': 'A', 'lo': 1e-6, 'hi': 1e4, 'scale': 'log', 'unit': 'mJy',
         'desc': 'ν0 处归一化流量（未消光）'},
        {'name': 'beta1', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': 'ν<νb 段谱指数'},
        {'name': 'beta2', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': 'ν>νb 段谱指数'},
        {'name': 'nu_b', 'lo': 1e8, 'hi': 1e20, 'scale': 'log', 'unit': 'Hz',
         'desc': '断折频率（如 ν_m 或 ν_c）'},
        {'name': 'Av', 'lo': 0.0, 'hi': 6.0, 'scale': 'linear', 'unit': 'mag',
         'desc': '宿主 V 带消光'},
    ]
    rv_schema = PowerLawDust.rv_schema

    @staticmethod
    def param_defs(config):
        specs = [(p['name'], p['lo'], p['hi'], p['scale'])
                 for p in PowerLaw2Seg.params_schema]
        if config.get('rv_free'):
            specs.append(('Rv', 1.5, 6.0, 'linear'))
        return _param_defs(specs)

    @staticmethod
    def constraint(params):
        """硬约束（custom_mcmc 硬约束先验；先验外样本为零）：β1 ≤ β2。
        平滑连接幂律的渐近斜率取 max(β1, β2)（ν<νb 渐近 ν^{−β1}、
        ν>νb 渐近 ν^{−β2} 仅在 β1<β2 时成立），β 乱序时参数语义与
        「ν<νb 段 / ν>νb 段」的标签脱节，故排序作为硬约束。"""
        return params['beta1'] <= params['beta2']

    @staticmethod
    def _shape(nu, params, s):
        """两段平滑幂律形状（未归一化）：提出最大指数项防上溢（同 3seg 写法）"""
        b1, b2 = params['beta1'], params['beta2']
        with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
            x = nu / params['nu_b']
            m = np.maximum(s * b1, s * b2)
            shape = x ** (-m / s) * (x ** (s * b1 - m)
                                     + x ** (s * b2 - m)) ** (-1.0 / s)
        return shape

    @staticmethod
    def model_flux(params, t, nu, config):
        nu = np.asarray(nu, dtype=float)
        nu0 = float(config.get('nu0') or 5e14)
        s = float(config.get('s') or 3.0)
        # A 为 ν0 处（未消光）流量：形状函数按 ν0 处值归一
        shape0 = PowerLaw2Seg._shape(np.array([nu0]), params, s)[0]
        f_mjy = params['A'] * PowerLaw2Seg._shape(nu, params, s) / shape0
        f_mjy = laws.extinguish(nu, f_mjy, config.get('z'),
                                config.get('law', 'smc'), params['Av'],
                                rv=params.get('Rv') or config.get('rv'))
        return f_mjy * MJY_TO_CGS

    @staticmethod
    def derived(params, config):
        b2 = params['beta2']
        return {
            'beta1': params['beta1'], 'beta2': b2,
            'nu_b_hz': params['nu_b'],
            'Av_host': params['Av'],
            'law': config.get('law', 'smc'),
            # p 候选按高频段 β2 的两种常见映射（Granot & Sari 2002；M3 联动）
            'p_if_nu_above_nu_c': 2.0 * b2,
            'p_if_nu_m_below_nu_below_nu_c': 2.0 * b2 + 1.0,
        }

    @staticmethod
    def sanity_checks(params, derived, meta):
        warns = []
        for k in ('beta1', 'beta2'):
            warns += _beta_checks(params.get(k), meta, name=k.replace('beta', 'β'))
        # 断折频率超出数据频率覆盖范围时不可约束
        lam_min = (meta or {}).get('lambda_min_a')
        lam_max = (meta or {}).get('lambda_max_a')
        if lam_min and lam_max:
            nu_hi = _C_AA_PER_S / lam_min   # λ 越小 ν 越大
            nu_lo = _C_AA_PER_S / lam_max
            nub = params.get('nu_b')
            if nub is not None and not nu_lo <= nub <= nu_hi:
                warns.append(f'nu_b={nub:.3g} Hz 超出数据频率覆盖范围 '
                             f'[{nu_lo:.3g}, {nu_hi:.3g}] Hz：'
                             '该断折不可约束，谨慎解读')
        return warns


def _beta_checks(beta, meta, name='β'):
    """β 物理域检查（§4.3）：合理域 −1.5<β<1.5；β<0 提示谱反转/数据问题"""
    warns = []
    if beta is None:
        return warns
    if not -1.5 <= beta <= 1.5:
        warns.append(f'{name}={beta:.2f} 超出余辉合理域 [−1.5, 1.5]，请检查数据')
    if beta < 0:
        warns.append(f'{name}={beta:.2f} < 0（Fν 随频率上升）：'
                     '可能是谱反转、宿主消光不足或数据问题')
    return warns


class PowerLaw3Seg:
    """三段平滑连接幂律 + 宿主消光（ν_m 与 ν_c 双断折的余辉同步辐射谱，
    Granot & Sari 2002 各谱段）。

    Fν = A · BPL(ν; νb1, β1, β2, s1) · [1 + (ν/νb2)^{s2·(β3−β2)}]^{−1/s2}
         · 10^(−0.4·A_host)
    其中 BPL(ν; νb, βa, βb, s) = [(ν/νb)^{s·βa} + (ν/νb)^{s·βb}]^{−1/s}
    （βa<βb 时 ν<νb 渐近 ν^{−βa}、ν>νb 渐近 ν^{−βb}）；第二因子使 ν>νb2 后
    总斜率过渡到 β3。s1/s2 为平滑度（越大越锐利），固定值从 config 读
    （默认 3.0，高级选项）。硬约束 nu_b1 < nu_b2 且 β1 ≤ β2 ≤ β3
    （constraint()，走 custom_mcmc 的硬约束先验）。"""
    key = 'powerlaw_3seg'
    label = '三段平滑幂律 + 宿主消光'
    params_schema = [
        {'name': 'A', 'lo': 1e-6, 'hi': 1e4, 'scale': 'log', 'unit': 'mJy',
         'desc': 'ν0 处归一化流量（未消光）'},
        {'name': 'beta1', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': 'ν<νb1 段谱指数'},
        {'name': 'beta2', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': 'νb1<ν<νb2 段谱指数'},
        {'name': 'beta3', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': 'ν>νb2 段谱指数'},
        {'name': 'nu_b1', 'lo': 1e8, 'hi': 1e20, 'scale': 'log', 'unit': 'Hz',
         'desc': '第一断折频率（如 ν_m；硬约束 nu_b1 < nu_b2）'},
        {'name': 'nu_b2', 'lo': 1e8, 'hi': 1e20, 'scale': 'log', 'unit': 'Hz',
         'desc': '第二断折频率（如 ν_c）'},
        {'name': 'Av', 'lo': 0.0, 'hi': 6.0, 'scale': 'linear', 'unit': 'mag',
         'desc': '宿主 V 带消光'},
    ]
    rv_schema = PowerLawDust.rv_schema

    @staticmethod
    def param_defs(config):
        specs = [(p['name'], p['lo'], p['hi'], p['scale'])
                 for p in PowerLaw3Seg.params_schema]
        if config.get('rv_free'):
            specs.append(('Rv', 1.5, 6.0, 'linear'))
        return _param_defs(specs)

    @staticmethod
    def constraint(params):
        """硬约束（custom_mcmc 硬约束先验；先验外样本为零）：
        nu_b1 < nu_b2 且 β1 ≤ β2 ≤ β3。BPL 的渐近斜率取 max(βa, βb)
        （低频侧 ∝ ν^(−βa)、高频侧 ∝ ν^(−βb) 仅在 βa<βb 时成立），β 乱序时
        参数语义与「ν<νb1 段 / νb1<ν<νb2 段 / ν>νb2 段」的标签脱节，故排序
        一并作为硬约束。"""
        return (params['nu_b1'] < params['nu_b2']
                and params['beta1'] <= params['beta2'] <= params['beta3'])

    @staticmethod
    def _shape(nu, params, s1, s2):
        """三段平滑幂律形状（未归一化）：BPL(ν; νb1, β1, β2, s1) × 第二断折因子"""
        b1, b2, b3 = params['beta1'], params['beta2'], params['beta3']
        with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
            x1 = nu / params['nu_b1']
            # BPL(ν; νb1, β1, β2, s1)：提出最大指数项防上溢
            m1 = np.maximum(s1 * b1, s1 * b2)
            bpl = x1 ** (-m1 / s1) * (x1 ** (s1 * b1 - m1)
                                      + x1 ** (s1 * b2 - m1)) ** (-1.0 / s1)
            x2 = nu / params['nu_b2']
            seg2 = (1.0 + x2 ** (s2 * (b3 - b2))) ** (-1.0 / s2)
        return bpl * seg2

    @staticmethod
    def model_flux(params, t, nu, config):
        nu = np.asarray(nu, dtype=float)
        nu0 = float(config.get('nu0') or 5e14)
        s1 = float(config.get('s1') or 3.0)
        s2 = float(config.get('s2') or 3.0)
        # A 为 ν0 处（未消光）流量：形状函数按 ν0 处值归一
        shape0 = PowerLaw3Seg._shape(np.array([nu0]), params, s1, s2)[0]
        f_mjy = params['A'] * PowerLaw3Seg._shape(nu, params, s1, s2) / shape0
        f_mjy = laws.extinguish(nu, f_mjy, config.get('z'),
                                config.get('law', 'smc'), params['Av'],
                                rv=params.get('Rv') or config.get('rv'))
        return f_mjy * MJY_TO_CGS

    @staticmethod
    def derived(params, config):
        b2 = params['beta2']
        return {
            'beta1': params['beta1'], 'beta2': b2, 'beta3': params['beta3'],
            'nu_b1_hz': params['nu_b1'], 'nu_b2_hz': params['nu_b2'],
            'Av_host': params['Av'],
            'law': config.get('law', 'smc'),
            # p 候选按中段 β2 的两种常见映射（Granot & Sari 2002；M3 联动）
            'p_if_nu_above_nu_c': 2.0 * b2,
            'p_if_nu_m_below_nu_below_nu_c': 2.0 * b2 + 1.0,
        }

    @staticmethod
    def sanity_checks(params, derived, meta):
        warns = []
        for k in ('beta1', 'beta2', 'beta3'):
            warns += _beta_checks(params.get(k), meta, name=k.replace('beta', 'β'))
        # 断折频率超出数据频率覆盖范围时不可约束
        lam_min = (meta or {}).get('lambda_min_a')
        lam_max = (meta or {}).get('lambda_max_a')
        if lam_min and lam_max:
            nu_hi = _C_AA_PER_S / lam_min   # λ 越小 ν 越大
            nu_lo = _C_AA_PER_S / lam_max
            for name in ('nu_b1', 'nu_b2'):
                nub = params.get(name)
                if nub is not None and not nu_lo <= nub <= nu_hi:
                    warns.append(f'{name}={nub:.3g} Hz 超出数据频率覆盖范围 '
                                 f'[{nu_lo:.3g}, {nu_hi:.3g}] Hz：'
                                 '该断折不可约束，谨慎解读')
        return warns


# ─── M4: 黑体系列 ───

class _BBBase:
    """黑体公共部分：z 已知拟合 R [cm]；无 z 退化为 R/D（无量纲），R/L 不可算。

    suffix 用于多成分参数名（'T1'/'R1' 等），单成分为 ''。"""

    @staticmethod
    def _r_specs(config, suffix=''):
        if config.get('z'):
            return [(f'R{suffix}', 1e12, 1e18, 'log')]
        return [(f'RD{suffix}', 1e-16, 1e-6, 'log')]

    @staticmethod
    def _bb_flux(params, nu, config, suffix=''):
        T = params[f'T{suffix}']
        bnu = planck_nu(nu, T)
        if f'R{suffix}' in params:
            dl = _dl_cm(config.get('z'))
            if dl is None:
                raise ValueError('无红移，无法计算 D_L')
            return math.pi * bnu * (params[f'R{suffix}'] / dl) ** 2
        return math.pi * bnu * params[f'RD{suffix}'] ** 2

    @staticmethod
    def _bb_derived(params, config, suffix=''):
        T = params[f'T{suffix}']
        d = {f'T{suffix}_K': T, f'nu_peak{suffix}_hz': 5.879e10 * T}
        if f'R{suffix}' in params:
            R = params[f'R{suffix}']
            d[f'R{suffix}_cm'] = R
            d[f'L{suffix}_erg_s'] = 4.0 * math.pi * R ** 2 * _SIGMA_SB * T ** 4
        else:
            d[f'note{suffix}'] = '无红移：R、L 不可算，仅拟合 T 与 R/D'
        return d

    @staticmethod
    def _bb_checks(params, derived, meta, suffixes=('',)):
        warns = []
        for s in suffixes:
            T = params.get(f'T{s}')
            if T is not None and not 3e3 <= T <= 5e4:
                warns.append(f'T={T:.0f} K 超出 SN 文献常见域 3e3–5e4 K'
                             '（kilonova/早期冷却相等例外请自行判断）')
            R = params.get(f'R{s}')
            t_days = (meta or {}).get('t_sel')
            if R is not None and t_days:
                v = R / (t_days * 86400.0)
                if v > _C_CGS:
                    warns.append(f'R/t = {v:.2e} cm/s 超光速（R={R:.2e} cm, '
                                 f't={t_days:.3g} d）：黑体解释存疑')
        if (meta or {}).get('z') is None:
            warns.append('无红移：R、L 不可算（仅 T 与 R/D），建议录入宿主红移')
        elif not (meta or {}).get('has_uv') and any(
                (params.get(f'T{s}') or 0) > 2e4 for s in suffixes):
            warns.append('λ 覆盖无 UV（<3000 Å）而 T>2e4 K：'
                         '温度主要靠瑞利-金斯侧外推，建议开启 T 下限先验并谨慎解读')
        return warns


class BlackBody(_BBBase):
    """稀释黑体：Fν = π·Bν(T)·(R/D_L)²（Nicholl 2018 / van Velzen+2021 标准形式）"""
    key = 'blackbody'
    label = '单黑体'
    params_schema = [
        {'name': 'T', 'lo': 1e3, 'hi': 3e5, 'scale': 'log', 'unit': 'K',
         'desc': '黑体温度'},
        {'name': 'R', 'lo': 1e12, 'hi': 1e18, 'scale': 'log', 'unit': 'cm',
         'desc': '黑体半径（有 z 时）；无 z 时为 RD=R/D_L（1e-16–1e-6）'},
    ]

    @staticmethod
    def param_defs(config):
        return _param_defs([('T', 1e3, 3e5, 'log')]
                           + BlackBody._r_specs(config))

    @staticmethod
    def model_flux(params, t, nu, config):
        return BlackBody._bb_flux(params, np.asarray(nu, dtype=float), config)

    @staticmethod
    def derived(params, config):
        return BlackBody._bb_derived(params, config)

    @staticmethod
    def sanity_checks(params, derived, meta):
        return BlackBody._bb_checks(params, derived, meta)


class TwoBlackBody(_BBBase):
    """双黑体：T1（热成分，先验 T>2e4 K 防互换）+ T2（冷成分）"""
    key = '2blackbody'
    label = '双黑体（TDE UV/光学、FBOT 冷热成分）'
    params_schema = [
        {'name': 'T1', 'lo': 2e4, 'hi': 3e5, 'scale': 'log', 'unit': 'K',
         'desc': '热成分温度（先验 >2e4 K，防参数互换）'},
        {'name': 'T2', 'lo': 1e3, 'hi': 2e4, 'scale': 'log', 'unit': 'K',
         'desc': '冷成分温度'},
        {'name': 'R1', 'lo': 1e12, 'hi': 1e18, 'scale': 'log', 'unit': 'cm',
         'desc': '热成分半径（无 z 时为 RD1）'},
        {'name': 'R2', 'lo': 1e12, 'hi': 1e18, 'scale': 'log', 'unit': 'cm',
         'desc': '冷成分半径（无 z 时为 RD2）'},
    ]

    @staticmethod
    def param_defs(config):
        specs = [('T1', 2e4, 3e5, 'log'), ('T2', 1e3, 2e4, 'log')]
        if config.get('z'):
            specs += [('R1', 1e12, 1e18, 'log'), ('R2', 1e12, 1e18, 'log')]
        else:
            specs += [('RD1', 1e-16, 1e-6, 'log'), ('RD2', 1e-16, 1e-6, 'log')]
        return _param_defs(specs)

    @staticmethod
    def model_flux(params, t, nu, config):
        nu = np.asarray(nu, dtype=float)
        return (TwoBlackBody._bb_flux(params, nu, config, '1')
                + TwoBlackBody._bb_flux(params, nu, config, '2'))

    @staticmethod
    def derived(params, config):
        d = TwoBlackBody._bb_derived(params, config, '1')
        d.update(TwoBlackBody._bb_derived(params, config, '2'))
        return d

    @staticmethod
    def sanity_checks(params, derived, meta):
        return TwoBlackBody._bb_checks(params, derived, meta, ('1', '2'))

class BBPowerLaw(_BBBase):
    """黑体 + 幂律叠加（FBOT 晚期红外超 / 非热成分；GRB 早期光学黑体+X 幂律展示）"""
    key = 'bb_powerlaw'
    label = '黑体 + 幂律'
    params_schema = [
        {'name': 'T', 'lo': 1e3, 'hi': 3e5, 'scale': 'log', 'unit': 'K',
         'desc': '黑体温度'},
        {'name': 'R', 'lo': 1e12, 'hi': 1e18, 'scale': 'log', 'unit': 'cm',
         'desc': '黑体半径（无 z 时为 RD）'},
        {'name': 'A', 'lo': 1e-8, 'hi': 1e4, 'scale': 'log', 'unit': 'mJy',
         'desc': '幂律 ν0 处归一化'},
        {'name': 'beta', 'lo': -2.0, 'hi': 3.5, 'scale': 'linear', 'unit': '',
         'desc': '幂律谱指数'},
    ]

    @staticmethod
    def param_defs(config):
        specs = [('T', 1e3, 3e5, 'log')] + BBPowerLaw._r_specs(config)
        specs += [('A', 1e-8, 1e4, 'log'), ('beta', -2.0, 3.5, 'linear')]
        return _param_defs(specs)

    @staticmethod
    def model_flux(params, t, nu, config):
        nu = np.asarray(nu, dtype=float)
        nu0 = float(config.get('nu0') or 5e14)
        f_pl = params['A'] * (nu / nu0) ** (-params['beta']) * MJY_TO_CGS
        return BBPowerLaw._bb_flux(params, nu, config) + f_pl

    @staticmethod
    def derived(params, config):
        d = BBPowerLaw._bb_derived(params, config)
        d['beta'] = params['beta']
        return d

    @staticmethod
    def sanity_checks(params, derived, meta):
        return (BBPowerLaw._bb_checks(params, derived, meta)
                + _beta_checks(params.get('beta'), meta))


MODELS = {m.key: m for m in (PowerLawDust, PowerLawXray, PowerLaw2Seg,
                             PowerLaw3Seg, BlackBody, TwoBlackBody, BBPowerLaw)}

# 任务 model_name 前缀（fitting_results 表，禁止含冒号以免串入 fitting 列表）
MODEL_NAME_PREFIX = 'sed_'
SERIES_KEY = 'blackbody_series'


def model_name_of(key):
    return MODEL_NAME_PREFIX + key


def describe(key):
    """/api/sed/models 的模型 schema（不依赖 VegasAfterglow）"""
    m = MODELS[key]
    params = list(m.params_schema)
    rv = getattr(m, 'rv_schema', None)
    if rv is not None:
        params = params + [rv]
    return {
        'key': key,
        'model_name': model_name_of(key),
        'label': m.label,
        'params': params,
        'config_options': {
            'law': [l['name'] for l in laws.list_laws()],
            'nu0': '幂律参考频率 Hz（默认 5e14）',
            'nu_split': '双幂律分段频率 Hz（默认 1e17）',
            's': 'powerlaw_2seg 断折平滑度（高级选项，默认 3.0，越大越锐利）',
            's1': 'powerlaw_3seg 第一断折平滑度（高级选项，默认 3.0，越大越锐利）',
            's2': 'powerlaw_3seg 第二断折平滑度（高级选项，默认 3.0）',
            'rv_free': 'R_V 是否自由（默认 false）',
            'rv': '固定 R_V 值（缺省用各律标称值）',
            'z_override': '红移覆盖（缺省用目录值）',
        },
    }
