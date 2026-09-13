"""宿主消光律模板与通带综合工具（M2/M4 共用）。

消光律实现基于 dust_extinction 包（项目已有依赖，零新增）：
  'smc'     —— Gordon+2003 SMC bar 平均曲线（dust_extinction.averages.G03_SMCBar；
               设计文档的 Pei92 三律同义模板，dust_extinction 的 P92 类只内置 MW
               参数集，SMC/LMC 用 G03 平均曲线实现）
  'lmc'     —— Gordon+2003 LMC 平均曲线（G03_LMCAvg）
  'mw'      —— Pei 1992 MW 曲线（shapes.P92，与 AJST 银消改正所用同律）
  'mw_f99'  —— Fitzpatrick 1999（parameter_averages.F99，Rv 直接传参）
  'mw_ccm'  —— Cardelli+1989（parameter_averages.CCM89，Rv 直接传参）
Calzetti+2000 在 dust_extinction 中无对应类，按设计说明略去（提交时校验报错）。

返回值约定：law(wave_A) -> A(λ)/A(V)，wave_A 单位 Å。
Rv 覆盖：F99/CCM89 原生支持；P92/G03 曲线无 Rv 参数，用解析重标定
  A_new/A(V) = ((Rv0+1)·A_old/A(V) + (Rv_new−Rv0)) / (Rv_new+1)
（由 A(λ)/A(V) = (ξ(λ)+Rv)/(Rv+1) 推出，ξ 为选择性消光曲线）。
"""
import warnings

import numpy as np

_C_AA_PER_S = 2.99792458e18  # 光速 [Å/s]

# 各律的标称 Rv（展示用默认值）
_NOMINAL_RV = {'smc': 2.74, 'lmc': 3.16, 'mw': 3.1, 'mw_f99': 3.1, 'mw_ccm': 3.1}
# 模板曲线内禀 Rv（覆盖 Rv 时重标定的基准）
_INTRINSIC_RV = {'smc': 2.74, 'lmc': 3.41, 'mw': 3.1}
# 2175 Å bump（用于残差图高亮提示）
_HAS_BUMP = {'smc': False, 'lmc': True, 'mw': True, 'mw_f99': True, 'mw_ccm': True}

_LAW_LABELS = {
    'smc': 'SMC（Gordon+2003 bar 平均）',
    'lmc': 'LMC（Gordon+2003 平均）',
    'mw': 'MW（Pei 1992）',
    'mw_f99': 'MW（Fitzpatrick 1999）',
    'mw_ccm': 'MW（Cardelli+1989）',
}


def list_laws():
    """/api/sed/models 用的消光律清单"""
    return [{'name': k, 'label': _LAW_LABELS[k], 'rv_default': _NOMINAL_RV[k],
             'has_bump': _HAS_BUMP[k]} for k in ('smc', 'lmc', 'mw', 'mw_f99', 'mw_ccm')]


def get_law(name, rv=None):
    """返回 callable(wave_A) -> A(λ)/A(V)。wave_A 单位 Å（标量或数组）。

    超出曲线定义域的波长按最近边界值平坦外推（射电端消光≈边界极小值，
    避免 dust_extinction 对定义域外 x 抛 ValueError）。
    """
    key = str(name).strip().lower()
    if key not in _NOMINAL_RV:
        raise ValueError(f'未知消光律: {name!r}（可用: {sorted(_NOMINAL_RV)}）')
    if key in ('mw_f99', 'mw_ccm'):
        from dust_extinction.parameter_averages import F99, CCM89
        model = (F99 if key == 'mw_f99' else CCM89)(Rv=float(rv or 3.1))
        rv0 = None
    else:
        if key == 'smc':
            from dust_extinction.averages import G03_SMCBar
            model = G03_SMCBar()
        elif key == 'lmc':
            from dust_extinction.averages import G03_LMCAvg
            model = G03_LMCAvg()
        else:
            from dust_extinction.shapes import P92
            model = P92()
        rv0 = _INTRINSIC_RV[key]
    x_lo, x_hi = float(model.x_range[0]), float(model.x_range[1])

    def law(wave_A):
        arr = np.asarray(wave_A, dtype=float)
        x = 1e4 / arr  # Å → μm⁻¹
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # 抑制 units 警告
            y = np.asarray(model(np.clip(x, x_lo, x_hi)), dtype=float)
        if rv is not None and rv0 is not None and abs(float(rv) - rv0) > 1e-9:
            y = ((rv0 + 1.0) * y + (float(rv) - rv0)) / (float(rv) + 1.0)
        return float(y) if arr.ndim == 0 else y

    return law


def extinguish(nu_hz, f_nu, z, law_name, av, rv=None):
    """模型谱乘宿主消光：Fν × 10^(−0.4·A_V·law(λ_rest))。

    λ_rest = (c/ν)/(1+z)；z 为 None 时不红移（λ_rest = λ_obs，
    宿主消光仅为观测系等效值，调用方需在 warnings 中显著标注）。"""
    nu = np.asarray(nu_hz, dtype=float)
    lam_obs = _C_AA_PER_S / nu
    lam_rest = lam_obs / (1.0 + float(z)) if z else lam_obs
    a_over_av = get_law(law_name, rv)(lam_rest)
    return np.asarray(f_nu, dtype=float) * 10.0 ** (-0.4 * float(av) * a_over_av)


def bandpass_flux(nu_hz, f_nu, transmission, nu_eff=None):
    """通带综合：∫Fν·T dlnν / ∫T dlnν。

    nu_hz/f_nu 为模型谱采样（任意网格）；transmission 为 filters 表
    extra_data.transmission 格式 {'wl': [Å 升序], 'tr': [峰值归一]}。
    transmission 为 None 时返回 nu_eff（有效频率，必填）处的线性插值。"""
    nu = np.asarray(nu_hz, dtype=float)
    f = np.asarray(f_nu, dtype=float)
    order = np.argsort(nu)
    nu, f = nu[order], f[order]
    if not transmission:
        if nu_eff is None:
            raise ValueError('transmission 为 None 时必须提供 nu_eff')
        return float(np.interp(nu_eff, nu, f))
    wl = np.asarray(transmission.get('wl') or [], dtype=float)
    tr = np.asarray(transmission.get('tr') or [], dtype=float)
    ok = np.isfinite(wl) & np.isfinite(tr) & (wl > 0) & (tr >= 0)
    wl, tr = wl[ok], tr[ok]
    if wl.size < 2:
        if nu_eff is None:
            raise ValueError('透过率曲线点数不足且无 nu_eff 可回退')
        return float(np.interp(nu_eff, nu, f))
    nu_t = _C_AA_PER_S / wl  # wl 升序 → ν 降序
    o2 = np.argsort(nu_t)
    nu_t, tr = nu_t[o2], tr[o2]
    lo, hi = max(nu[0], nu_t[0]), min(nu[-1], nu_t[-1])
    if hi <= lo:
        # 模型网格与通带无重叠：回退到有效频率处直接值
        return float(np.interp(nu_eff if nu_eff else nu[len(nu) // 2], nu, f))
    grid = np.logspace(np.log10(lo), np.log10(hi), 512)
    # 模型谱在 log-log 空间插值（幂律/黑体在该空间更接近线性）
    fg = 10.0 ** np.interp(np.log10(grid), np.log10(nu),
                           np.log10(np.maximum(f, 1e-300)))
    tg = np.interp(grid, nu_t, tr, left=0.0, right=0.0)
    w = tg / grid  # dlnν = dν/ν
    denom = np.trapezoid(w, grid)
    if denom <= 0:
        return float(np.interp(nu_eff if nu_eff else nu[len(nu) // 2], nu, f))
    return float(np.trapezoid(fg * w, grid) / denom)
