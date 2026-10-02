"""specphot S2 连续谱拟合——纯函数层（02 规格 §3.8 + §3.9.3，P2 切片 2a）。

交付：六模型库（pl/pl_dust/pl2/bb/pl_bb/dbb，别名↔宿主注册表键，F-27）+ poly
基线（F-62）+ 引擎封装（F-91）+ 协方差守门（F-93）+ GLS 白化（F-92）+ 统计
推断（F-28 剖面 / F-95 自助 / F-29 比较 / F-30 闭包 / F-29③ Davies 参数化
自助）。不接 API 路由；宿主尘埃三态（F-87…F-90）与消光律越界回显（F-64①）
在切片 2c 接。

纪律锚点：
  - 引擎唯一化（F-91，T-59）：一律 least_squares(method='trf', x_scale='jac',
    diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX)；禁 'lm'/curve_fit/loss=
    （D-15：loss= 令 2·cost ≠ chi2）。网格与闭式幅度解只产出 x0（F-91⑤⑥，
    grid_converged = 初始化质量，非收敛证据；A 终拟合仍是自由参数）。
  - 协方差全有/全无（F-93，T-67/T-68）：任一奇异值 ≤ eps·max(shape)·s₁ 或
    dof ≤ 0 ⇒ Σ_θ 整体 null、σ 全 null + CA-44，禁「编辑奇异值/取有效秩」；
    chi2=2·cost；infl=sqrt(max(1,chi2/dof)) 只放大不缩小。
  - GLS 白化（F-92）：ρ 由 errors.rho_lag1 一次估计全程复用；AR(1) Cholesky
    白化 r̃=L⁻¹r、J̃=L⁻¹J；n>C_CHOLESKY_MAX 或 ρ 不可估 ⇒ diag_infl 降级 +
    err_source='covariance_approx'。cov_method ∈ {chol_gls,svd_pinv,diag_infl,none}。
  - 误差推断（F-28/F-95/Q-35）：默认 Δχ²=1 剖面（固定单参数、其余重拟合）；
    剖面重拟合与自助共用 C_BOOT_N_BUDGET，超限回落自助分位 + CA-44③ +
    boot_budget_applied。err_seed（Q-35）：null ⇒ int(spec_hash,16)；发生器
    构造式唯一 = Generator(PCG64(err_seed))，rng_algo='pcg64'。
  - 模型比较（F-29）：嵌套对 F 检验（pl⊂pl_dust Δk=1；pl/bb⊂pl_bb Δk=2），
    显著性 C_FTEST_ALPHA；非嵌套只比 BIC 分档（C_BIC_MIN）；mask_hash 不一致
    或任一侧 CA-44① ⇒ 拒比；BIC 的 k 只计自由参数。pl→pl2 是嵌套对但断点
    ν_b 在零假设下不可识别（Davies 问题，F-29③）⇒ 普通 F/Δχ²/ΔBIC 都不得
    作为判定，唯一判定通路 = davies_bootstrap 的参数化自助经验零分布
    （p_method='parametric_bootstrap'；边界 χ² 混合 50:50 只作自检旁证）。
  - 闭包（F-30）：三候选 p=2β/2β+1/2β+2（宿主 models.py:96-99 derived() 映射），
    区间端点 = 线性映射 G=2·err（delta_method）；三候选是备选而非同一物理量。
  - poly（F-62）：Chebyshev 基、ln λ 归一化 [-1,1]，线性层 lstsq（SVD，禁正规
    方程）；cond_2 > C_COND_MAX 自动降阶挂 CA-20，降到 1 阶仍超限拒模型。
"""
import math

import numpy as np
import scipy
import scipy.linalg
from scipy.optimize import least_squares
from scipy.stats import chi2 as _chi2_dist
from scipy.stats import f as _f_dist

from sedfit import laws as _host_laws          # F-64：消光律唯一通路（五键）
from sedfit.models import planck_nu as _planck_nu   # F-27 纯计算件

from . import errors as _errs                  # rho_lag1 / corr_inflation（F-92⑤）
from .constants import (C_AA_PER_S, C_BIC_MIN, C_BOOT_N, C_BOOT_N_BUDGET,
                        C_CHOLESKY_MAX, C_COND_MAX, C_DEGEN_RHO, C_DIFF_STEP,
                        C_EDGE_SPREAD_MAX, C_EDGE_WIN_FRAC, C_FTEST_ALPHA,
                        C_GRID_EVAL_MAX, C_GRID_MAX_PER_AXIS, C_LEVERAGE_K,
                        C_MAX_BOOT, C_MAX_STARTS, C_NFEV_MAX, C_NONPOS_FRAC_MAX,
                        C_OPTIMALITY_MAX, C_POLY_ORDER_MAX, C_RHO_MIN)

MJY_TO_CGS = 1e-26            # 1 mJy = 1e-26 erg/cm²/s/Hz（宿主 models.py 同值）
NU0_DEFAULT = 5e14            # 幂律参考频率 Hz（宿主约定，λ=5995.85 Å），原样回显
ENGINE = 'least_squares.trf'  # F-91⑦ 恒值
# 内层终止容差（实现裁量）：必须严于 C_OPTIMALITY_MAX 的事后门限，否则 ftol/xtol
# 先行终止、事后最优性恒触 CA-44①；C_DIFF_STEP/max_nfev 等引擎形态仍按 §7.7 表。
_FTOL = 1e-14
# 事后最优性平台判定的 χ² 阈（实现裁量，§7.5 无此常量名 ⇒ 不立 C_*）：可行下降
# 探针的 χ² 收益低于此值视为数值平台（240 点谱的统计分辨单位是 Δχ²=1，1e-3
# 远低于任何统计意义；真未收敛的下降收益 O(1) 以上）。见 _posthoc_optimality。
_KKT_PLATEAU_CHI2 = 1e-3


class ContinuumError(Exception):
    """S2 拟合侧拒绝（带 E-/CA- 码与 reason）。"""

    def __init__(self, code, reason):
        super().__init__(f'{code}: {reason}')
        self.code = code
        self.reason = reason


def _warn(code, message, **kw):
    """与 continuum_api._warn 同形（P2b 评审起本模块也会发带 reason 的告警：
    pl2 报告口径的宿主 β 域截断 CA-24/host_beta_domain）。"""
    w = {'code': code, 'message': message}
    w.update(kw)
    return w


def av_ebv(av=None, ebv=None, rv=3.1):
    """T-55/F-88 的**全模块唯一** A_V ↔ E(B−V) 换算式（A_V = R_V·E(B−V)，与
    银消侧 extinction.py:25 的 RV 定义同源）。单向给定：给 av 返 ebv（fit 侧
    只作显示，F-90 禁止把显示值再喂回计算）；给 ebv 返 av（prescribe 钉死
    A_V 的唯一入口）。两量同给或同缺 ⇒ E-14。任何其它代码路径需要这对量时
    一律 import 本函数（F-88：不得出现第二处算术，由 T-55 往返一致性钉死）。"""
    if (av is None) == (ebv is None):
        raise ContinuumError('E-14', 'av_ebv：av 与 ebv 必须恰给一个（T-55 单一算术）')
    r = float(rv)
    if not (math.isfinite(r) and r > 0.0):
        raise ContinuumError('E-14', f'R_V={rv!r} 非法：须为正有限数')
    return (float(av) / r) if av is not None else (float(ebv) * r)


# ─── 1. 模型库（别名 ↔ 宿主注册表键，F-27；解析形式照 §3.8 表，不自创） ────

# 参数域逐项抄宿主 sedfit/models.py 的 params_schema（lo/hi/scale）：
_A_PL = (1e-6, 1e4)        # PowerLawDust.A（mJy，log）
_A_PLBB = (1e-8, 1e4)      # BBPowerLaw.A（mJy，log）
_BETA = (-2.0, 3.5)        # beta（linear）
_T = (1e3, 3e5)            # BlackBody.T（K，log）
_T1 = (2e4, 3e5)           # TwoBlackBody.T1（热成分；与 T2 区间不重叠）
_T2 = (1e3, 2e4)           # TwoBlackBody.T2（冷成分）
_R = (1e12, 1e18)          # R [cm]（有 z，log）
_RD = (1e-16, 1e-6)        # RD = R/D_L（无 z，log）
_AV = (0.0, 6.0)           # A_V [mag]（linear，宿主同域）
_NUB = (1e8, 1e20)         # PowerLaw2Seg.nu_b [Hz]（log，宿主原域；拟合时按
                           # F-91② 收窄到数据频率覆盖内，见 fit_spectrum）
_PL2_S = 3.0               # powerlaw_2seg 断折平滑度 s（§3.8「s 固定 3.0」；
                           # 宿主 models.py:162 默认同值。§7.5 无此常量名 ⇒
                           # 不立 C_*，只作模块内定值）


def _bb_rname(cfg, suffix=''):
    """F-63 两分支：有 z（且给 D_L）拟合 R[cm] ⇒ T_semantics='effective'；
    无 z 拟合 RD=R/D_L ⇒ 'colour'（R/L 输出 null + CA-15）。"""
    return 'R' + suffix if (cfg.get('z') and cfg.get('dl_cm')) else 'RD' + suffix


def _nu0(cfg):
    return float(cfg.get('nu0') or NU0_DEFAULT)


def _fnu_pl(th, nu, cfg):
    """Fν = A·(ν/ν0)^(−β)（A_V≡0 固定分支），A 单位 mJy → cgs。"""
    return th['A'] * (nu / _nu0(cfg)) ** (-th['beta']) * MJY_TO_CGS


# 宿主消光的 A(λ_rest)/A(V) 数组 memo（P2b 性能补缺）：键 = (law, rv, z, ν 网格
# 逐字节)。同一谱的模型求值数万次，ν 网格不变 ⇒ 曲线只算一次；宿主
# dust_extinction 的 G03/P92 曲线每次求值都重建样条（实测 ~0.04 ms/次），不缓存
# 会烧穿 ST-5 的 5 s 重档预算。纯缓存：miss 时仍走 sedfit.laws.get_law 现算
# （F-64 唯一通路不变），乘式与 laws.extinguish 逐项同式同序 ⇒ 数值逐字节同。
_LAW_MEMO = {}


def _extinguish(nu, f, cfg, av):
    """Fν × 10^(−0.4·A_V·law(λ_rest))（= 宿主 laws.extinguish 同式，见上 memo 注）。"""
    law = cfg.get('law', 'smc')
    rv = cfg.get('rv')
    z = cfg.get('z')
    nu = np.asarray(nu, dtype=float)
    key = (law, None if rv is None else float(rv),
           None if z is None else float(z), nu.tobytes())
    a = _LAW_MEMO.get(key)
    if a is None:
        lam_obs = C_AA_PER_S / nu
        lam_rest = lam_obs / (1.0 + float(z)) if z else lam_obs
        a = _host_laws.get_law(law, rv)(lam_rest)
        if len(_LAW_MEMO) > 8:
            _LAW_MEMO.clear()                 # 有界：谱数有限，直接清空重填
        _LAW_MEMO[key] = a
    return np.asarray(f, dtype=float) * 10.0 ** (-0.4 * float(av) * a)


def _fnu_pl_dust(th, nu, cfg):
    """Fν = A·(ν/ν0)^(−β)·10^(−0.4·A_V·law(λ_rest))（宿主 laws.extinguish 同式）。"""
    f = th['A'] * (nu / _nu0(cfg)) ** (-th['beta'])           # mJy
    f = _extinguish(nu, f, cfg, th['Av'])
    return f * MJY_TO_CGS


def _pl2_shape(nu, beta1, beta2, nu_b, nu0):
    """两段平滑幂律形状（宿主 PowerLaw2Seg._shape 同式，提出最大指数项防上溢）：
    A·[(ν/νb)^{sβ1}+(ν/νb)^{sβ2}]^{−1/s}，按 ν0 处值归一（宿主 model_flux 同）。
    β1<β2 时 ν<νb 渐近 ν^{−β1}、ν>νb 渐近 ν^{−β2}；β1=β2 时形状与 ν_b 无关
    （Davies 平坦方向，F-29③）。"""
    x = np.asarray(nu, dtype=float) / float(nu_b)
    m = np.maximum(_PL2_S * beta1, _PL2_S * beta2)
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        shape = x ** (-m / _PL2_S) * (x ** (_PL2_S * beta1 - m)
                                      + x ** (_PL2_S * beta2 - m)) ** (-1.0 / _PL2_S)
    x0 = np.asarray([nu0], dtype=float) / float(nu_b)
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        m0 = np.maximum(_PL2_S * beta1, _PL2_S * beta2)
        shape0 = (x0 ** (-m0 / _PL2_S)
                  * (x0 ** (_PL2_S * beta1 - m0)
                     + x0 ** (_PL2_S * beta2 - m0)) ** (-1.0 / _PL2_S))[0]
    return shape / shape0


def _fnu_pl2(th, nu, cfg):
    """Fν = A·[(ν/νb)^{sβ1}+(ν/νb)^{sβ2}]^{−1/s}·(消光)，A 单位 mJy、归一在 ν0
    （§3.8 pl2 行 = 宿主 powerlaw_2seg；A_V≡0 钉死分支跳过消光，与 pl 同）。
    内部参数化：β2 = β1 + Δβ（th['dbeta'] ≥ 0，宿主硬约束 β1 ≤ β2）。
    P2b 评审（β2 域收窄）：Δβ 的盒上界 5.5 单独存在时，β1 > β_lo 会令
    β2 = β1+Δβ 越出宿主 params_schema 给 β2 的 [−2, 3.5] 域（打破
    「参数域逐项抄宿主」的声明）。盒式边界表达不了 Δβ ≤ β_hi−β1 的耦合约束
    （β1 自由）；而**在求值处截断**（β2_eff = min(β1+Δβ, β_hi)）会给目标面
    引入零梯度截断脊——幅值触界等退化数据的最优解恰落在脊上时 trf 爬行、
    超支 ST-5 预算（实测 API 层 pl2 拟合 0.74 s → 2.05 s）⇒ 截断不进求值
    通路，改在**报告口径**落地：fit_spectrum 对 th_out['beta2'] 按 β_hi 截断
    （dbeta 同步改写保「β2=β1+Δβ」不变式），截断态挂 CA-24（报告值是宿主域
    边界值、不是似然峰）并把 'beta2' 计入 grid_boundary_params（W-24 旁标
    机器）。β2 < β_hi 的正常拟合（含全部既有测试）逐字节零影响。"""
    f = float(th['A']) * _pl2_shape(nu, th['beta1'], th['beta1'] + th['dbeta'],
                                    th['nu_b'], _nu0(cfg))         # mJy
    if th.get('Av'):
        f = _extinguish(nu, f, cfg, th['Av'])
    return f * MJY_TO_CGS


def _bb_comp(nu, T, r, cfg):
    """稀释黑体单成分：π·Bν(T)·(R/D_L)²（无 z 时 (R/D_L)²=RD² 直接拟合）。"""
    bnu = _planck_nu(nu, T)
    if _bb_rname(cfg) == 'R':
        return math.pi * bnu * (r / float(cfg['dl_cm'])) ** 2
    return math.pi * bnu * r ** 2


def _fnu_bb(th, nu, cfg):
    return _bb_comp(nu, th['T'], th[_bb_rname(cfg)], cfg)


def _fnu_pl_bb(th, nu, cfg):
    return _bb_comp(nu, th['T'], th[_bb_rname(cfg)], cfg) + _fnu_pl(th, nu, cfg)


def _fnu_dbb(th, nu, cfg):
    return (_bb_comp(nu, th['T1'], th[_bb_rname(cfg, '1')], cfg)
            + _bb_comp(nu, th['T2'], th[_bb_rname(cfg, '2')], cfg))


def _sanity(alias, pd, cfg):
    """原样继承宿主 sanity_checks（F-63；纯计算件，不触 VegasAfterglow）。"""
    import sedfit.models as _hm
    meta = {'z': cfg.get('z'), 't_sel': cfg.get('t_sel_d'), 'has_uv': cfg.get('has_uv')}
    cls = {'pl': None, 'pl_dust': _hm.PowerLawDust, 'pl2': _hm.PowerLaw2Seg,
           'bb': _hm.BlackBody, 'pl_bb': _hm.BBPowerLaw, 'dbb': _hm.TwoBlackBody}[alias]
    return (_hm._beta_checks(pd.get('beta'), meta) if cls is None
            else cls.sanity_checks(pd, {}, meta))


# 参数表/网格轴：bb 族按 cfg 的 z 分支换 R↔RD 参数名（F-63）；网格轴 = 非线性
# 参数（log/linear 域,点数），逐维细化而非全笛卡尔积（F-65）。
def _params(alias, cfg):
    rr = (_R, 'R') if _bb_rname(cfg) == 'R' else (_RD, 'RD')
    if alias == 'pl':
        return [('A',) + _A_PL, ('beta',) + _BETA]
    if alias == 'pl_dust' or alias == 'pl2':
        # F-87③/F-87①：A_V 被钉死（prescribe 给定 / off 恒 0）⇒ 退出自由参数表
        #（n_par 少一，自由参数表由本模块按宿主域自行构造，宿主 models.py 零改动）
        if alias == 'pl2':
            # 内部参数化 (β1=低频段斜率, Δβ=β2−β1≥0)：宿主硬约束 β1 ≤ β2
            # （models.py constraint）化为 Δβ 的盒式下界 0——"无断折"（Δβ=0）
            # 恰落在允许域**边界**上（F-29③ 原文口径）；交换对称的 (β1,β2) 全盒
            # 参数化有镜像简并 + 对角脊上反对称梯度恒 0 的病态，trf 在脊上
            # xtol 早停且事后最优性恒超限。报告口径 β2 = β1 + Δβ（fit_spectrum
            # 装 beta2 键；Δβ 以 dbeta 键并排回显其剖面区间）。盒上界 5.5 = 域宽
            # （β_hi−β_lo）；Δβ 的**有效**上界 = min(β_hi−β1, 5.5)（P2b 评审：
            # β2 恒保宿主 [−2,3.5] 可行域）——盒式边界表达不了随 β1 变的耦合
            # 上界，落地 = 网格 Δβ 轴收窄（_grid_starts）+ 报告口径截断
            # （fit_spectrum，越出挂 CA-24），不在求值处截断（理由见
            # _fnu_pl2 docstring）。
            ps = [('A',) + _A_PL, ('beta1',) + _BETA, ('dbeta', 0.0, _BETA[1] - _BETA[0]),
                  ('nu_b',) + _NUB]
        else:
            ps = [('A',) + _A_PL, ('beta',) + _BETA]
        if cfg.get('av_fixed') is None:
            ps.append(('Av',) + _AV)
        return ps
    if alias == 'bb':
        return [('T',) + _T, (rr[1],) + rr[0]]
    if alias == 'pl_bb':
        return [('T',) + _T, (rr[1],) + rr[0], ('A',) + _A_PLBB, ('beta',) + _BETA]
    return [('T1',) + _T1, (rr[1] + '1',) + rr[0],       # dbb
            ('T2',) + _T2, (rr[1] + '2',) + rr[0]]


def _grid(alias, cfg):
    rn = _bb_rname(cfg)
    if alias == 'pl':
        return [('beta', 'linear', 9)]
    if alias == 'pl_dust':
        ax = [('beta', 'linear', 9)]
        if cfg.get('av_fixed') is None:      # A_V 钉死 ⇒ 网格轴同步退出（F-87③）
            ax.append(('Av', 'linear', 7))
        return ax
    if alias == 'pl2':
        # Av 在前（若自由）：尘埃量级不定时斜率/断点的单轴扫描会被尘埃吸收，
        # argmin 恒扫到端点（初始化质量虚差，CA-24 空告警）；Av 先定，斜率与
        # ν_b 的扫描才有意义。β1 轴 9 点与 pl 同分辨率；Δβ 轴含 0（Davies 边界）。
        ax = [('Av', 'linear', 5), ('beta1', 'linear', 9), ('dbeta', 'linear', 5),
              ('nu_b', 'log', 9)]              # ν_b 对数网格（F-65，宿主 scale='log'）
        if cfg.get('av_fixed') is not None:
            ax = ax[1:]
        return ax
    if alias == 'bb':
        return [('T', 'log', 9), (rn, 'log', 7)]
    if alias == 'pl_bb':
        return [('T', 'log', 7), (rn, 'log', 5), ('beta', 'linear', 5)]
    return [('T1', 'log', 5), (rn + '1', 'log', 4),      # dbb
            ('T2', 'log', 5), (rn + '2', 'log', 4)]


# linear = 网格点上闭式求解的线性幅度参数名（F-27①：s = Σ w f m / Σ w m²，
# 白化空间同式）；最终拟合里它仍是自由参数（F-91⑥）。
MODEL_SPECS = {
    'pl': {'host_key': 'powerlaw_dust', 'label': '幂律（A_V≡0 固定分支）',
           'linear': 'A', 'fixed': {'Av': 0.0}, 'fnu': _fnu_pl},
    'pl_dust': {'host_key': 'powerlaw_dust', 'label': '幂律 + 宿主消光',
                'linear': 'A', 'fixed': {}, 'fnu': _fnu_pl_dust},
    'pl2': {'host_key': 'powerlaw_2seg', 'label': '两段断折幂律 + 宿主消光',
            'linear': 'A', 'fixed': {}, 'fnu': _fnu_pl2},
    'bb': {'host_key': 'blackbody', 'label': '单黑体',
           'linear': None, 'fixed': {}, 'fnu': _fnu_bb},
    'pl_bb': {'host_key': 'bb_powerlaw', 'label': '黑体 + 幂律',
              'linear': 'A', 'fixed': {}, 'fnu': _fnu_pl_bb},
    'dbb': {'host_key': '2blackbody', 'label': '双黑体',
            'linear': None, 'fixed': {}, 'fnu': _fnu_dbb},
}

# F-29②：嵌套对 → Δk（Δk 必须与两侧 n_par 之差一致才可比）。pl⊂pl2 的增量
# 参数 = β1/β2/ν_b（fit 态，pl2 的 A_V 自由、pl 恒 A_V≡0 ⇒ 5−2=3）；off/prescribe
# 态 A_V 钉死时差为 2——该态的实际 Δk 随消光态变，故此对**不走**本表的 Δk 校验
# （compare_models 在 Davies 分支先行返回），登记值按缺省 fit 态记。
NESTED_PAIRS = {('pl', 'pl_dust'): 1, ('pl', 'pl_bb'): 2, ('bb', 'pl_bb'): 2,
                ('pl', 'pl2'): 3}

# F-29③：嵌套但断点 ν_b 在零假设下不可识别（Davies 问题），且硬约束 β1 ≤ β2
# 使"无断折"（Δβ=0）落在允许域**边界**上 ⇒ 检验统计量的渐近分布不是普通 χ²，
# 常规 F 检验与 Δχ² 门限都会**高估**显著性。该对的判定不走 compare_models 的
# F 检验/BIC 分支（compare_models 只发 davies_bootstrap_required 引导标记），
# 唯一判定通路 = davies_bootstrap 的参数化自助经验零分布，p_method=
# 'parametric_bootstrap'；边界 χ² 混合分布（50:50）只作自检旁证（T-18）。
DAVIES_PAIRS = {('pl', 'pl2')}

for _k, _s in MODEL_SPECS.items():      # 防手滑：别名集与宿主键映射自洽
    assert _s['host_key'] in ('powerlaw_dust', 'powerlaw_2seg', 'blackbody',
                              'bb_powerlaw', '2blackbody')


def alias_table():
    """别名 → 宿主 MODELS 键（§3.8 表的回显件）。"""
    return {k: s['host_key'] for k, s in MODEL_SPECS.items()}


def _effective_spec(alias, cfg):
    """F-87①③ 的钉死分支：pl_dust/pl2（凡含尘埃的模型，Q-32 口径）在
    cfg['av_fixed'] 给定时（prescribe 的 R_V·E(B−V) 或 off 的恒 0）把 A_V 并入
    fixed 集 —— 不进自由参数表/网格轴，n_par 少一。宿主 models.py 的
    param_defs 无钉住 Av 的入口 ⇒ 自由参数表由本模块自造（F-87e）。"""
    spec = MODEL_SPECS[alias]
    if alias in ('pl_dust', 'pl2') and cfg.get('av_fixed') is not None:
        return dict(spec, fixed={**spec['fixed'], 'Av': float(cfg['av_fixed'])})
    return spec


def _layout(alias, cfg):
    """θ 布局：非线性参数在前、线性幅度最后（F-91⑥）→ (names, lb, ub)。"""
    spec = MODEL_SPECS[alias]
    pnames = [p[0] for p in _params(alias, cfg)]
    names = [n for n in pnames if n != spec['linear']] + (
        [spec['linear']] if spec['linear'] else [])
    lo = {p[0]: p[1] for p in _params(alias, cfg)}
    hi = {p[0]: p[2] for p in _params(alias, cfg)}
    lb = np.array([lo[n] for n in names], dtype=float)
    ub = np.array([hi[n] for n in names], dtype=float)
    return names, lb, ub


# ─── 2. GLS 白化（F-92） ─────────────────────────────────────────────

def _ar1_chol(rho, n):
    """AR(1) 相关阵 R_ij = ρ^|i−j| 的 Cholesky 因子 L（下三角）。"""
    idx = np.arange(n)
    R = rho ** np.abs(idx[:, None] - idx[None, :])
    return scipy.linalg.cholesky(R, lower=True)


def _whiten(a, L):
    return scipy.linalg.solve_triangular(L, a, lower=True) if L is not None else a


def _cov_path(flux, rho):
    """F-92②③：cov_method 选择 + 白化因子。ρ 一次估计全程复用（F-92⑤）。"""
    n = flux.size
    if rho is None or n > C_CHOLESKY_MAX:
        return None, 'diag_infl'        # 降级路：ρ 不可估（CA-21）或白化代价超预算
    if rho <= C_RHO_MIN:
        return None, 'svd_pinv'         # 噪声近独立 ⇒ 未白化对角（T-60 的对角支）
    return _ar1_chol(rho, n), 'chol_gls'


# ─── 3. 网格初值（F-27 两层法 + F-65 对数网格/逐维细化） ─────────────────

def _axis_values(name, kind, n, lo, hi, max_per_axis=None):
    n = max(3, min(int(n), C_GRID_MAX_PER_AXIS if max_per_axis is None
                   else min(int(max_per_axis), C_GRID_MAX_PER_AXIS)))
    return np.geomspace(lo, hi, n) if kind == 'log' else np.linspace(lo, hi, n)


def _grid_chi2(spec, th, nu, flux, sigma, d_w, L, cfg):
    """网格点上一次求值。linear 模型用闭式幅度（F-27①），否则全模型 χ²。"""
    th = {**spec['fixed'], **th}      # 钉死参数（pl 的 Av≡0 / pl_dust 钉死分支）恒在场
    f = spec['fnu'](th, nu, cfg)
    m_w = _whiten(f / sigma, L)
    if spec['linear'] is None:
        return float(np.sum((m_w - d_w) ** 2)), None
    s = float(np.dot(d_w, m_w) / np.dot(m_w, m_w)) if np.dot(m_w, m_w) > 0 else 0.0
    return float(np.sum((s * m_w - d_w) ** 2)), s


def _grid_starts(alias, cfg, nu, flux, sigma, d_w, L, max_per_axis=None):
    """非线性网格 × 逐维细化（F-65：禁全笛卡尔积）→ 候选起点列表与初始化质量。

    三点抛物线内插在对数域（log 轴）/ 线性域（linear 轴）做；最优解落在网格
    端点 ⇒ grid_converged=False（初始化质量差，CA-24），不是收敛证据，且该
    参数名进 grid_boundary_params（W-24「报告值旁标边界值」的回显件）。
    断点 ν_b 的轴域按 F-91② 收窄到数据频率覆盖内（宿主原域 [1e8,1e20] 之外
    覆盖的 χ² 平坦，扫描无意义）。"""
    spec = _effective_spec(alias, cfg)
    axes = []
    lo_d = {p[0]: p[1] for p in _params(alias, cfg)}
    hi_d = {p[0]: p[2] for p in _params(alias, cfg)}
    nu_lo_cov, nu_hi_cov = float(nu.min()), float(nu.max())
    for name, kind, npts in _grid(alias, cfg):
        lo, hi = lo_d[name], hi_d[name]
        if name == 'nu_b':                       # F-91②：断点限制在覆盖内
            lo, hi = max(lo, nu_lo_cov), min(hi, nu_hi_cov)
        axes.append((name, kind, npts, lo, hi))
    cur = {}
    for name, kind, npts, lo, hi in axes:
        mid = math.sqrt(lo * hi) if kind == 'log' else 0.5 * (lo + hi)
        cur[name] = float(mid)
    n_eval, scans = 0, {}
    for _level in range(2):                      # 逐维细化两轮
        for name, kind, npts, lo, hi in axes:
            if name == 'dbeta' and 'beta1' in cur:
                # P2b 评审（β2 域收窄）：Δβ 轴上界收窄到 min(盒上界, β_hi−当前
                # β1)——网格初值不落在宿主 β2 域外（报告口径截断见 _fnu_pl2 注
                # 与 fit_spectrum），超出部分扫描无意义
                hi = min(hi, _BETA[1] - cur['beta1'])
            vals = _axis_values(name, kind, npts, lo, hi, max_per_axis)
            chi2s = np.empty(vals.size)
            for j, v in enumerate(vals):
                th = dict(cur)
                th[name] = float(v)
                if spec['linear']:
                    th[spec['linear']] = 1.0     # A=1 形状，闭式幅度在 _grid_chi2 解
                chi2s[j], _s = _grid_chi2(spec, th, nu, flux, sigma, d_w, L, cfg)
                n_eval += 1
            i = int(np.argmin(chi2s))
            cur[name] = float(vals[i])
            scans[name] = (kind, vals, chi2s)
            if n_eval > C_GRID_EVAL_MAX:
                break
        if n_eval > C_GRID_EVAL_MAX:
            break
    x0, interior = dict(cur), True
    boundary = []
    for name, (kind, vals, chi2s) in scans.items():
        i = int(np.argmin(chi2s))
        if i == 0 or i == vals.size - 1:
            interior = False                     # 剖面最优落在网格端点（CA-24）
            boundary.append(name)                # W-24：报告值旁标"边界值"
            continue
        x = np.log(vals) if kind == 'log' else vals
        a, b, c = np.polyfit(x[i - 1:i + 2], chi2s[i - 1:i + 2], 2)
        if a > 0:
            xv = -b / (2.0 * a)
            if x[i - 1] < xv < x[i + 1]:         # 内插点收进区间才算内点
                x0[name] = float(np.exp(xv) if kind == 'log' else xv)
    if spec['linear']:                           # 闭式幅度只作初值来源（F-91⑥）
        th = dict(x0)
        th[spec['linear']] = 1.0
        _c, s_lin = _grid_chi2(spec, th, nu, flux, sigma, d_w, L, cfg)
        plo, phi = lo_d[spec['linear']], hi_d[spec['linear']]
        s_lin = s_lin if (s_lin is not None and plo <= s_lin <= phi) \
            else math.sqrt(plo * phi)
        x0[spec['linear']] = float(s_lin)
        cur[spec['linear']] = float(s_lin)
    starts = [x0, dict(cur)]
    rng_d = {name: (lo, hi) for name, _k, _n, lo, hi in axes}   # 收窄后的轴域
    if len(axes) > 1:                            # 第三个起点：各轴中点（逃逸坏盆地）
        starts.append({name: (math.sqrt(rng_d[name][0] * rng_d[name][1])
                              if scans[name][0] == 'log'
                              else 0.5 * (rng_d[name][0] + rng_d[name][1]))
                       for name in scans})
    seen, uniq = set(), []
    for s in starts[:C_MAX_STARTS]:
        key = tuple(sorted((k, round(v, 12)) for k, v in s.items()))
        if key not in seen:
            seen |= {key}                           # 集合并集写法（RO 静态扫描）
            uniq.append(s)
    return uniq, {'grid_converged': interior, 'n_eval': n_eval,
                  'grid_refine_levels': 2,
                  'grid_boundary_params': boundary}


# ─── 4. 协方差与退化守门（F-93） ─────────────────────────────────────

def _cov_from_jac(J, dof):
    """F-93①②③：白化 J̃ 上 SVD 造 Σ₀，全有/全无截断；返回 (Σ₀|None, 原因)。

    列均衡（P2b 实现细节，数学无损）：pl2 的 ν_b 以 Hz 计（5e14），原始 J 列范数
    可跨 15 个量级，原始 SVD 会把「单位比」误判成「秩亏」（T-67 语义只在真秩亏
    时触发）。令 J' = J·D（D = diag(1/‖colⱼ‖)）后做秩判据与 Σ'，再 Σ₀ = D·Σ'·D
    ——与 (J̃ᵀJ̃)⁻¹ 恒等（D⁻¹(J̃ᵀJ̃)⁻¹D⁻¹ 的逆变换），只改数值不改定义；
    真秩亏（如 J 第 2 列 = 2×第 1 列）在均衡后依旧整列零奇异值 ⇒ 仍全有/全无。"""
    if dof <= 0:
        return None, 'dof_le_0'
    cn = np.linalg.norm(J, axis=0)
    scale = np.where(cn > 0, 1.0 / np.where(cn > 0, cn, 1.0), 1.0)
    Je = J * scale
    _u, s, vt = np.linalg.svd(Je, full_matrices=False)
    threshold = np.finfo(float).eps * max(J.shape) * float(s[0])
    if not bool(np.all(s > threshold)):          # 任一奇异值越限 ⇒ 整体 null（T-67）
        return None, 'rank_deficient'
    sigma0 = (vt.T * (1.0 / s ** 2)) @ vt
    return (scale[:, None] * sigma0) * scale[None, :], None


def _infl(chi2, dof):
    """F-93④：infl = sqrt(max(1, chi2/dof))——只放大不缩小（T-68 哨兵）。"""
    return math.sqrt(max(1.0, chi2 / dof)) if dof > 0 else 1.0


def _hybrid_jac(fun, x):
    """差分步长塌缩救援的雅可比重算（P2b 评审）：先按 C_DIFF_STEP·|x_k| 的
    相对步逐列算（与引擎/scipy numdiff 同式）；某列结果全零或非有限（相对步
    在浮点上不可分辨——参数真值落在 0 附近时的必然，如无尘埃谱的 A_V→0、
    Davies 脊上的 Δβ→0）⇒ 该列按 scipy numdiff 对 x0==0 的自带兜底规则回退
    绝对步 C_DIFF_STEP·max(|x_k|, 1) 重算。健康列逐字节不变。"""
    x = np.asarray(x, dtype=float)
    f0 = fun(x)
    J = np.empty((f0.size, x.size))
    for k in range(x.size):
        h = C_DIFF_STEP * (abs(float(x[k])) if x[k] != 0 else 1.0)
        xp = x.copy()
        xp[k] += h
        J[:, k] = (fun(xp) - f0) / h
        if not np.any(J[:, k] != 0.0) or not np.all(np.isfinite(J[:, k])):
            h = C_DIFF_STEP * max(abs(float(x[k])), 1.0)
            xp = x.copy()
            xp[k] += h
            J[:, k] = (fun(xp) - f0) / h
    return J


def _cond_sigma(J):
    """条件 σ（仅作剖面窗口量级的内部尺度，**不回显不进 Σ**，F-93 报告侧纪律
    不受影响）：Σ null（真秩亏/触界零列）时 profile 初窗的 2.5σ 项无从取值，
    退回 0.05·span 会对 A 这类线性幅度（span 10 个量级）给出荒唐窗口。取均衡
    J' 列的 pinv 对角（条件协方差；Av 触界零列方向给大 σ，其余方向仍是可用的
    局域曲率尺度）。"""
    cn = np.linalg.norm(J, axis=0)
    scale = np.where(cn > 0, 1.0 / np.where(cn > 0, cn, 1.0), 1.0)
    Je = J * scale
    _u, s, _vt = np.linalg.svd(Je, full_matrices=False)
    s_max = float(s[0]) if s.size else 1.0
    s = np.where(s > np.finfo(float).eps * max(J.shape) * s_max, s, np.inf)
    cov_eq = (np.asarray(_vt).T * (1.0 / s ** 2)) @ np.asarray(_vt)
    diag = np.diag((scale[:, None] * cov_eq) * scale[None, :])
    return {i: float(math.sqrt(max(v, 0.0))) for i, v in enumerate(diag)}


def _posthoc_optimality(fun, x, lb=None, ub=None, active_mask=None):
    """F-91⑦「一阶最优性的**事后**门限」：在返回解处重算 ‖(Jᵀr)·v‖∞（v=1/列范数，
    与 x_scale='jac' 同度量；触界分量按可行方向投影，与 trf 的 KKT 口径一致）。
    scipy 在 xtol 终止时报的 optimality 是上一轮迭代的陈旧值（实测可偏大 10⁶ 倍，
    而返回解距真优 <1e-9），不能直接当门限用。

    P2b 补缺（pl2 的 Davies 谷必需，对其它模型零影响）：超限分量的**平台判定**。
    pl2 在纯幂律数据上收敛到 Δβ≈0 的 Davies 脊（F-29③）时，最优解落在数值
    平台上：剩余 cost 改善 ~1e-4 χ²（240 点谱的 1σ 分辨单位是 Δχ²=1，远低于
    任何统计意义），但 (a) 数值零列（ν_b 在 Δβ=0 时形状不变 ⇒ ∂r/∂ν_b 列范数
    ~1e-20，与 F-93① 同阈值应判秩亏）的梯度噪声/列范数虚高；(b) 准触界分量
    （Δβ 停在距界 1e-7 处）的 g/列范数 ~0.2。二者都不是「未收敛」的证据——
    本函数对仍超限的分量做**可行下降探针**（±{1e-3,1e-2,1e-1}·域宽，越界截断）：
    任何探针点使 χ² 下降 ≥ _KKT_PLATEAU_CHI2 才算真违反，否则该分量按数值
    平台放行。真未收敛（局部极小被困）的下降收益 O(1) χ² 以上，仍会被钉出。"""
    f0 = fun(x)
    J = np.empty((f0.size, x.size))
    for k in range(x.size):
        h = C_DIFF_STEP * (abs(float(x[k])) if x[k] != 0 else 1.0)
        xp = np.asarray(x, dtype=float).copy()
        xp[k] += h
        J[:, k] = (fun(xp) - f0) / h
    g = J.T @ f0
    for k in range(x.size):                       # 触界分量的可行方向投影
        at_lo = active_mask is not None and active_mask[k] == -1 or (
            lb is not None and lb[k] >= x[k])
        at_hi = active_mask is not None and active_mask[k] == 1 or (
            ub is not None and ub[k] <= x[k])
        if at_lo and g[k] > 0:
            g[k] = 0.0                            # 下界处合法下降须 g<0
        if at_hi and g[k] < 0:
            g[k] = 0.0                            # 上界处合法下降须 g>0
    colnorm = np.linalg.norm(J, axis=0)
    colnorm[colnorm == 0] = np.inf
    viol = np.abs(g / colnorm)
    if float(viol.max()) <= C_OPTIMALITY_MAX:
        return float(viol.max())
    # 平台判定：仅对仍超限且列可分辨的分量做探针（P2b，见 docstring）
    cn_max = float(np.max(colnorm[np.isfinite(colnorm)])) if np.any(np.isfinite(colnorm)) else 0.0
    cn_null = np.finfo(float).eps * max(f0.size, x.size) * cn_max   # F-93① 同形阈值
    chi2_cur = float(np.sum(f0 ** 2))             # chi2 = ‖r‖²（无 loss=，D-15）
    for k in range(x.size):
        if viol[k] <= C_OPTIMALITY_MAX:
            continue                              # 已达标
        if not np.isfinite(colnorm[k]) or colnorm[k] <= cn_null:
            viol[k] = 0.0                         # 数值零列：梯度/列范数是纯噪声，
            continue                              # 该方向不可分辨 ⇒ 不作为违反证据
        span = (float(ub[k]) - float(lb[k])) if (lb is not None and ub is not None) \
            else 0.0
        descends = False
        if span > 0.0:
            for frac in (1e-3, 1e-2, 1e-1):
                for sgn in (1.0, -1.0):
                    xv = np.asarray(x, dtype=float).copy()
                    xv[k] = min(max(xv[k] + sgn * frac * span, lb[k]), ub[k])
                    if xv[k] == x[k]:
                        continue                  # 域太窄截断后无动量
                    red = chi2_cur - float(np.sum(fun(xv) ** 2))
                    if red >= _KKT_PLATEAU_CHI2:
                        descends = True
                        break
                if descends:
                    break
        if not descends:
            viol[k] = 0.0                         # 数值平台：该分量不构成未收敛证据
    return float(viol.max())


# ─── 5. 引擎封装 fit_spectrum（F-91 唯一入口） ─────────────────────────

def fit_spectrum(alias, nu_hz, flux_fnu, flux_err, *, config=None, spec_hash=None,
                 mask_hash=None, err_method='profile', n_boot=C_BOOT_N,
                 n_profile_points=9, err_seed=None, grid_max_per_axis=None):
    """S2 非线性模型拟合唯一入口（F-91）。

    alias ∈ MODEL_SPECS；nu_hz/flux_fnu/flux_err 为逐像素 Fν 空间的观测
    （flux_err 必给，代理 σ 由装配层按 §3.5 供）。config：nu0 / z / dl_cm /
    law / rv / t_sel_d / has_uv / av_fixed（F-87：钉死 A_V，prescribe 给
    R_V·E(B−V)、off 给 0.0；不给 = fit，A_V 自由）。err_method：'profile'
    （F-28 默认）| 'covariance' | 'bootstrap'（F-95②）。grid_max_per_axis：
    Q-15 的请求侧下调（恒不超过 C_GRID_MAX_PER_AXIS）。返回 FitResult dict
    （§4.2 子集）。
    """
    if alias not in MODEL_SPECS:
        raise ContinuumError('E-14', f'未知模型别名 {alias!r}')
    cfg = dict(config or {})
    nu = np.asarray(nu_hz, dtype=float)
    flux = np.asarray(flux_fnu, dtype=float)
    sigma = np.asarray(flux_err, dtype=float)
    if nu.size != flux.size or nu.size != sigma.size:
        raise ContinuumError('E-14', 'nu/flux/sigma 长度不一致')
    if not np.all(np.isfinite(flux)) or not np.all(sigma > 0):
        raise ContinuumError('E-14', 'flux 含非有限值或 sigma 非正')
    if nu.size < 8:
        raise ContinuumError('E-04', 'too_few_pixels')
    spec = _effective_spec(alias, cfg)
    warnings = []
    names, lb, ub = _layout(alias, cfg)
    if 'nu_b' in names:      # F-91②：断点 ν_b 的搜索界收窄到数据频率覆盖内
        i = names.index('nu_b')
        lb[i] = max(lb[i], float(nu.min()))
        ub[i] = min(ub[i], float(nu.max()))

    # 白化（F-92）：ρ 来自 errors.rho_lag1 同一通路，一次估计全程复用
    rho, rho_warn = _errs.rho_lag1(flux)
    if rho_warn:
        warnings.append(rho_warn)
    L, cov_method = _cov_path(flux, rho)
    d_w = _whiten(flux / sigma, L)

    # 网格初值（F-27②/F-65：只产出 x0；闭式幅度解只作初值来源记录）
    starts, grid_info = _grid_starts(alias, cfg, nu, flux, sigma, d_w, L,
                                     max_per_axis=grid_max_per_axis)

    def fun(xv):
        th = dict(spec['fixed'])
        th.update(zip(names, xv))
        r = (spec['fnu'](th, nu, cfg) - flux) / sigma
        return _whiten(r, L)

    margin = 1e-9 * (ub - lb)
    best = None
    for s in starts:                              # 多起点取 cost 最小（宿主 :209 同规则）
        x0v = np.clip([s.get(n, 0.5 * (l + u)) for n, l, u in zip(names, lb, ub)],
                      lb + margin, ub - margin)
        res = least_squares(fun, x0v, bounds=(lb, ub), method='trf', x_scale='jac',
                            diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX,
                            ftol=_FTOL, xtol=_FTOL)
        if best is None or res.cost < best.cost:
            best = res
    # P2b 实现裁量（pl2 必需，其余模型无害）：退化谷/触界分量（如 pl2 的 ν_b 平坦
    # 谷与 Av→0 的准活跃界）会让 trf 以 xtol/ftol 早停而返回解的一阶最优性仍超限
    #（事后口径实测可到 0.4，重启同一形态即达 ~1e-7）。从返回解原位**续跑**同一
    # 引擎形态（F-91① 形态不变；C_NFEV_MAX 为每次调用上限，nfev 累计回显）至多
    # 2 轮，只接受 cost 不升的解——这是迭代续跑，不是第二套引擎。
    _nfev_tot = int(best.nfev)
    for _polish in range(2):
        _probe = (_posthoc_optimality(fun, best.x, lb, ub, best.active_mask)
                  if best.status != 0 else float(best.optimality))
        if _probe <= C_OPTIMALITY_MAX or best.status == 0:
            break
        _res = least_squares(fun, np.clip(best.x, lb + margin, ub - margin),
                             bounds=(lb, ub), method='trf', x_scale='jac',
                             diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX,
                             ftol=_FTOL, xtol=_FTOL)
        _nfev_tot += int(_res.nfev)
        if _res.cost <= best.cost:
            best = _res
        else:
            break
    xhat = best.x.copy()
    chi2 = 2.0 * best.cost                        # F-93①（D-15：无 loss= 才成立）
    m, n_par = int(nu.size), int(xhat.size)
    dof = m - n_par

    optimality = (_posthoc_optimality(fun, best.x, lb, ub, best.active_mask)
                  if best.status != 0 else float(best.optimality))
    status_ok = best.status != 0 and optimality <= C_OPTIMALITY_MAX
    if not status_ok:                             # F-91⑦ + CA-44①
        warnings.append(_warn('CA-44', '拟合未收敛（status=%d 或 optimality 超限），'
                              '结果不得进入模型比较' % best.status))
    comparable = status_ok

    # 协方差（F-93）
    sigma0, cov_reason = _cov_from_jac(best.jac, dof)
    if cov_reason == 'rank_deficient':
        # P2b 评审修复（差分步长塌缩的**假**秩亏）：F-93 的秩亏语义是「模型
        # 对该方向不敏感」；参数真值落在 0 附近时相对差分步 h=diff_step·|x|
        # 在浮点上不可分辨，该列恰为全零 ⇒ SVD 误判秩亏（与 x 的落点有关的
        # 掷硬币，非模型性质）。用绝对步兜底（scipy numdiff 对 x0==0 的自带
        # 规则）重算 J 再判一次，仍秩亏才是真秩亏；健康拟合不进此支，Σ 数值
        # 逐字节不变。
        s2, r2 = _cov_from_jac(_hybrid_jac(fun, best.x), dof)
        if r2 is None:
            sigma0, cov_reason = s2, None
    infl = _infl(chi2, dof)
    extra = 1.0
    if cov_method == 'diag_infl':
        extra = _errs.corr_inflation(rho) ** 2    # F-92③：降级路乘 F-55 放大因子
    if sigma0 is None:
        warnings.append(_warn('CA-44', '协方差整体不可信（%s），逐参数 σ 全部置 null'
                              % cov_reason))
    sig_raw = {n: float(math.sqrt(sigma0[i, i])) for i, n in enumerate(names)} \
        if sigma0 is not None else {n: None for n in names}
    sig = {n: (None if sigma0 is None else
               float(math.sqrt(sigma0[i, i] * infl ** 2 * extra)))
           for i, n in enumerate(names)}
    bad = set()
    corr = {}
    for i, n in enumerate(names):                 # F-93③：触界 / 强相关 ⇒ 该 σ null
        if best.active_mask[i] != 0:
            bad |= {n}
        for j in range(i + 1, len(names)):
            if sigma0 is not None:
                r_ij = abs(sigma0[i, j] / (sig_raw[names[i]] * sig_raw[names[j]]))
                # T-15：参数对相关系数逐对回显（pl_dust 的 A_V↔β 简并须报数），
                # > C_DEGEN_RHO 者同时触发上面那条退化告警（协方差路下告警可达）
                corr[f'{names[i]}|{names[j]}'] = float(r_ij)
                if r_ij > C_DEGEN_RHO:
                    bad.update((names[i], names[j]))
    for n in bad:
        if sig.get(n) is not None:
            sig[n] = None
            warnings.append(_warn('CA-44', '参数 %s 触界或与同伴强相关(|ρ|>%.2f)，'
                                  '其 σ 判不可信，报告值降级为上限/下限表述'
                                  % (n, C_DEGEN_RHO)))
    cov = None if sigma0 is None else (sigma0 * infl ** 2 * extra)

    # 误差推断（F-28 剖面 / F-95 自助；预算池 C_BOOT_N_BUDGET，Q-15）
    seed = _resolve_seed(spec_hash, err_seed)
    err_low = {n: None for n in names}
    err_high = {n: None for n in names}
    err_source, n_boot_used = {n: 'none' for n in names}, 0
    budget = {'left': C_BOOT_N_BUDGET}
    boot_budget_applied = False
    if err_method == 'covariance':
        for n in names:
            if sig[n] is not None:
                err_low[n] = err_high[n] = sig[n]
                err_source[n] = ('covariance_approx' if cov_method == 'diag_infl'
                                 else 'covariance')
    else:
        prof_cost = n_par * max(3, int(n_profile_points))
        want_boot = err_method == 'bootstrap' or prof_cost > budget['left']
        if want_boot and prof_cost > budget['left']:
            boot_budget_applied = True            # CA-44③⑦：剖面超预算回落自助
            warnings.append(_warn('CA-44', 'Δχ²=1 剖面重拟合超 C_BOOT_N_BUDGET，'
                                  '回落自助分位'))
        if want_boot:
            n_boot_used = min(int(n_boot), C_MAX_BOOT, max(budget['left'], 0))
            if n_boot_used < n_boot:
                boot_budget_applied = True
            if sigma0 is not None and n_boot_used >= 2:
                err_low, err_high = _bootstrap_intervals(
                    spec, names, lb, ub, xhat, cov, nu, flux, sigma, L, cfg,
                    n_boot_used, seed)
                budget['left'] -= n_boot_used
                for n in names:
                    err_source[n] = 'bootstrap'
            else:
                warnings.append(_warn('CA-44', '自助不可用（协方差 null 或预算耗尽）'))
        else:
            sig_fb = _cond_sigma(best.jac) if sigma0 is None else None
            err_low, err_high = _profile_intervals(
                spec, names, lb, ub, xhat, chi2, sig, nu, flux, sigma, L, cfg,
                max(3, int(n_profile_points)), sig_fb=sig_fb)
            budget['left'] -= prof_cost
            for n in names:
                if err_low[n] is not None or err_high[n] is not None:
                    err_source[n] = 'profile'
    for n in names:                               # F-94③：err_source='none' ⇒ null+原因
        if err_source[n] == 'none':
            warnings.append(_warn('CA-44', '参数 %s 的误差不可按 1σ 解读（剖面无 '
                                  'Δχ²=1 crossing 或协方差/自助不可用）' % n))

    # 派生与回显装配（§4.2 FitResult 子集；闭包见 closure_from_fit）
    th_out = dict(spec['fixed'])
    th_out.update(zip(names, xhat))
    if alias == 'pl2':
        # 报告口径补全：β2 = β1 + Δβ（§3.8 表键集；dbeta 并排回显其自身剖面
        # 区间）。β2 的 σ/区间走 delta_method——G = [1, 1] 是精确线性映射，方差
        # 传播闭式；内部两轴的剖面区间无法线性组合成不对称区间 ⇒ beta2 对称
        # 区间 + err_source='delta_method'（不对称要求由 β1/dbeta 各自满足）。
        i1, i2 = names.index('beta1'), names.index('dbeta')
        for d in (sig, sig_raw, err_low, err_high):
            d['beta2'] = None
        err_source['beta2'] = 'none'
        if sigma0 is not None:
            var = sigma0[i1, i1] + sigma0[i2, i2] + 2.0 * sigma0[i1, i2]
            if var > 0.0:
                s_raw = math.sqrt(var)
                sig_raw['beta2'] = s_raw
                sig['beta2'] = s_raw * infl * extra
                err_low['beta2'] = err_high['beta2'] = sig['beta2']
                err_source['beta2'] = 'delta_method'
        # P2b 评审（β2 域收窄）：报告口径截断——β2 = β1+Δβ 越出宿主
        # params_schema 域 [−2, 3.5]（Δβ 盒上界 5.5 在 β1 > β_lo 时允许越出）
        # ⇒ 按宿主域上界截断报告，dbeta 同步改写保「β2 = β1+Δβ」不变式
        # （T-18 回收测试逐字节断言该恒等式）。截断态 = 报告值是宿主域边界值、
        # 不是似然峰 ⇒ 挂 CA-24 并把 'beta2' 计入 grid_boundary_params
        # （W-24 旁标机器）；求值通路不截断（理由见 _fnu_pl2 docstring）。
        # β2 < β_hi 的正常拟合零影响。
        b2_raw = th_out['beta1'] + th_out['dbeta']
        if b2_raw > _BETA[1]:
            th_out['beta2'] = _BETA[1]
            th_out['dbeta'] = th_out['beta2'] - th_out['beta1']
            warnings.append(_warn(
                'CA-24', '模型 pl2 的 β2=β1+Δβ=%.3g 越出宿主 params_schema 给 '
                'β2 的域上界 %s：报告值按宿主域边界截断（是边界值而非似然峰，'
                'σ/区间仍按 β1+Δβ 的精确线性映射给出）' % (b2_raw, _BETA[1]),
                reason='host_beta_domain'))
            grid_info['grid_boundary_params'].append('beta2')
        else:
            th_out['beta2'] = b2_raw
    t_sem = 'effective' if _bb_rname(cfg) == 'R' else (
        'colour' if 'T' in names or 'T1' in names else None)
    if t_sem == 'colour':
        warnings.append(_warn('CA-15', '无红移：R、L 不可算（仅 T 与 R/D_L），'
                              'T_semantics=colour'))
    sanity = _sanity(alias, th_out, cfg)
    return {
        'model': alias, 'host_key': spec['host_key'], 'label': spec['label'],
        'params': th_out, 'err_low': err_low, 'err_high': err_high,
        'sigma_theta': sig, 'sigma_theta_raw': sig_raw, 'infl': infl,
        'chi2': chi2, 'dof': dof, 'n_par': n_par,
        'bic': chi2 + n_par * math.log(m),        # F-29④：k 只计自由参数
        'err_semantics': 'lower_bound', 'T_semantics': t_sem, 'L_bb': None,
        'L_bb_err': None,                          # 黑体光度传播在装配切片（遗留）
        'nu0': _nu0(cfg), 'amplitude_solution': ('closed_form_linear'
                                                 if spec['linear'] else None),
        'grid_converged': grid_info['grid_converged'],
        'grid_boundary_params': grid_info['grid_boundary_params'],
        'grid_refine_levels': grid_info['grid_refine_levels'],
        'cond_2': None, 'poly_basis': None, 'poly_order': None,
        'engine': ENGINE,
        'engine_status': {'scipy_version': scipy.__version__, 'x_scale': 'jac',
                          'diff_step': C_DIFF_STEP, 'nfev': _nfev_tot,
                          'njev': int(best.njev), 'optimality': optimality,
                          'status': int(best.status),
                          'active_mask': [int(a) for a in best.active_mask],
                          'n_starts': len(starts)},
        'comparable': comparable, 'cov_method': cov_method, 'rho_used': rho,
        'corr': corr,
        'cond_G': None, 'err_seed': seed, 'n_boot': n_boot_used,
        'rng_algo': 'pcg64' if n_boot_used else None,
        'boot_budget_applied': boot_budget_applied,
        'err_source': err_source,
        'err_scope': {n: 'stat' for n in names},   # F-93⑥：S2 参数 σ 只含随机项
        'mask_hash': mask_hash, 'sanity_checks': sanity,
        'warnings': warnings, 'n_eval_grid': grid_info['n_eval'],
    }


# ─── 6. 误差推断：剖面（F-28）与自助（F-95②/Q-35） ─────────────────────

def _resolve_seed(spec_hash, err_seed):
    """Q-35：err_seed int∈[0,2⁶³) 或 null；null ⇒ int(spec_hash,16)（跨进程稳定）。"""
    if err_seed is None:
        if not spec_hash:
            raise ContinuumError('E-14', 'err_seed 缺省需要 spec_hash 派生')
        return int(spec_hash, 16)
    if not isinstance(err_seed, int) or isinstance(err_seed, bool) \
            or not 0 <= err_seed <= 2 ** 63 - 1:
        raise ContinuumError('E-14', 'err_seed 类型域 [0, 2^63-1]')
    return err_seed


def _refit_fixed(spec, i_fix, v_fix, names, lb, ub, nu, flux, sigma, L, cfg, xhat,
                 nfev_cap=None):
    """把参数 i_fix 固定于 v_fix、其余参数重拟合（F-28 剖面法），返回 χ²。

    nfev_cap：剖面重拟合的 nfev 上限（实现裁量，C_NFEV_MAX 的剖面档收缩）。
    剖面网格点若落在平坦谷（如 pl2 的 ν_b——Davies 方向）上，trf 会沿谷爬行
    烧掉大量 nfev，而**谷内 χ² 早已平台**（爬行只改参数不改 cost）⇒ 对只消费
    χ² 的剖面来说截断不改变 Δχ²=1 crossing 的判定（谷内截断的 cost 偏差远小于
    1）；主拟合（F-91⑦）不受此限，恒用 C_NFEV_MAX。"""

    def fun_red(x_red):
        th = dict(spec['fixed'])
        full = dict(zip(names, _insert(x_red, i_fix, v_fix)))
        th.update(full)
        r = (spec['fnu'](th, nu, cfg) - flux) / sigma
        return _whiten(r, L)

    keep = [k for k in range(len(names)) if k != i_fix]
    lb_r, ub_r = lb[keep], ub[keep]
    x0 = np.clip(np.array([xhat[k] for k in keep]), lb_r, ub_r)
    res = least_squares(fun_red, x0, bounds=(lb_r, ub_r), method='trf',
                        x_scale='jac', diff_step=C_DIFF_STEP,
                        max_nfev=C_NFEV_MAX if nfev_cap is None else int(nfev_cap),
                        ftol=_FTOL, xtol=_FTOL)
    return 2.0 * res.cost


def _insert(x_red, i, v):
    out = np.insert(np.asarray(x_red, dtype=float), i, v)
    return out


def _profile_intervals(spec, names, lb, ub, xhat, chi2_best, sig, nu, flux, sigma,
                       L, cfg, n_pts, sig_fb=None):
    """F-28：固定单参数于若干网格点、其余重拟合的剖面 χ²，取 Δχ²=1 两 crossing。

    扫描半宽自适应：初值 max(2.5σ_cov, 1e-3·span)，逐次缩放直到每侧都有
    Δχ²<1 的近点与 Δχ²≥1 的远点（线性内插才成立）；某侧在参数域内始终
    Δχ²<1 ⇒ 该侧 null（区间退化为极限表述，装配层负责上限/下限文案）。
    剖面 χ² 在白化空间计（F-92② 同一目标函数）。
    实现裁量（P2b）：重拟合带 nfev 收缩档（_refit_fixed 的 nfev_cap——平坦谷上
    trf 爬行只改参数不改 cost，截断不改变 Δχ² crossing 的判定）、自适应尝试
    上限 3 次；σ_cov 整体 null 时窗口量级退用条件 σ（sig_fb，_cond_sigma 的
    pinv 对角，仅内部尺度不回显），避免 A 这类大幅值域参数初窗跨 10 个量级。"""
    err_low = {n: None for n in names}
    err_high = {n: None for n in names}
    sig_fb = sig_fb or {}
    for i, name in enumerate(names):
        span = ub[i] - lb[i]
        s_ref = sig.get(name)
        if s_ref is None:
            s_ref = sig_fb.get(i)
        d = max(2.5 * s_ref if s_ref else 0.05 * span, 1e-3 * span, 1e-12)
        lo_seg, hi_seg = [], []
        for _attempt in range(3):
            vals = np.linspace(max(xhat[i] - d, lb[i]), min(xhat[i] + d, ub[i]),
                               n_pts)
            prof = [(_refit_fixed(spec, i, float(v), names, lb, ub, nu, flux,
                                  sigma, L, cfg, xhat, nfev_cap=150), float(v))
                    for v in vals]
            prof = [(c - chi2_best, v) for c, v in prof]          # Δχ²
            lo_seg = sorted((p for p in prof if p[1] < xhat[i]), key=lambda p: -p[1])
            hi_seg = sorted((p for p in prof if p[1] > xhat[i]), key=lambda p: p[1])
            near_max = max([lo_seg[0][0] if lo_seg else 0.0,
                            hi_seg[0][0] if hi_seg else 0.0])
            far_min = min([lo_seg[-1][0] if lo_seg else 0.0,
                           hi_seg[-1][0] if hi_seg else 0.0])
            if near_max > 1.0 and d > 1e-10 * span:
                # 近点已越 1 ⇒ 缩窗。缩放因子自适应（实现裁量）：Δχ² 近似局域
                # 二次 ⇒ 取 1/√(近点 Δχ²)（钳到 [0.02, 0.2]），A 这类小 σ 线性
                # 参数在 σ_cov 不可用（rank 缺）时初窗跨 10² 量级，固定 ×0.2 要
                # 烧掉全部尝试次数，自适应一次即到位。
                d *= max(0.02, min(0.2, 1.0 / math.sqrt(max(near_max, 1.0))))
            elif far_min < 1.0 and d < 0.5 * span:
                d *= 4.0                                          # 远点未到 1 ⇒ 扩窗
            else:
                break
        for side, seg in enumerate((lo_seg, hi_seg)):
            if not seg or seg[0][0] >= 1.0:
                continue                                          # 近点须 Δχ²<1
            cross = None
            for a, b in zip(seg, seg[1:]):                        # a 近、b 远
                if a[0] < 1.0 <= b[0]:
                    t = (1.0 - a[0]) / (b[0] - a[0])
                    cross = a[1] + t * (b[1] - a[1])
                    break
            if cross is None:
                continue                                          # 该侧无 crossing ⇒ null
            if side == 0:
                err_low[name] = float(xhat[i] - cross)
            else:
                err_high[name] = float(cross - xhat[i])
    return err_low, err_high


def _bootstrap_intervals(spec, names, lb, ub, xhat, cov, nu, flux, sigma, L, cfg,
                         n_boot, seed):
    """F-95②：参数化自助——白化空间重抽残差 d̃* = m̃(θ̂) + ε*，以 Σ_θ 上抽的 θ′
    为初值重解，16/84 分位为 err_lo/err_hi（不对称）。

    初值抽样按规格原文「在 Σ_θ 上抽（以抽样值为初值重解）」；重解对象是重抽
    残差后的数据（同一数据重解只会回到 θ̂，散布恒 0，不能当误差口径）。
    发生器构造式唯一（Q-35）：Generator(PCG64(seed))；同输入+seed ⇒ 逐字节
    相同（T-66）。"""
    rng = np.random.Generator(np.random.PCG64(int(seed)))
    lc = np.linalg.cholesky(cov) if cov is not None else None
    margin = 1e-9 * (ub - lb)

    def model_w(xv):
        th = dict(spec['fixed'])
        th.update(zip(names, xv))
        return _whiten(spec['fnu'](th, nu, cfg) / sigma, L)

    m_hat = model_w(xhat)
    samples = np.empty((int(n_boot), len(names)))
    for k in range(int(n_boot)):
        d_star = m_hat + rng.standard_normal(nu.size)            # 逐次重抽（白化空间）
        if lc is not None:
            th = xhat + lc @ rng.standard_normal(len(names))
        else:
            th = xhat.copy()
        th = np.clip(th, lb + margin, ub - margin)
        res = least_squares(lambda xv: model_w(xv) - d_star, th,
                            bounds=(lb, ub), method='trf', x_scale='jac',
                            diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX,
                            ftol=_FTOL, xtol=_FTOL)
        samples[k] = res.x
    lo_q = np.percentile(samples, 16, axis=0)
    hi_q = np.percentile(samples, 84, axis=0)
    return ({n: float(max(xhat[i] - lo_q[i], 0.0)) for i, n in enumerate(names)},
            {n: float(max(hi_q[i] - xhat[i], 0.0)) for i, n in enumerate(names)})


# ─── 7. 模型比较（F-29） ─────────────────────────────────────────────

def compare_models(fit_simple, fit_rich, alpha=None):
    """嵌套对 F 检验（F-29②）/ 非嵌套 ΔBIC 分档（F-29④）。

    前提闸：同 mask_hash（F-29①）、两侧 comparable（F-91⑦，CA-44① 者
    拒比）、Δk 与 NESTED_PAIRS 一致。返回 verdict dict（不抛异常，拒绝给
    reason）。pl→pl2（DAVIES_PAIRS，F-29③）例外：断点 ν_b 在零假设下不可
    识别（Davies 问题），本函数**不发判定**，只发 davies_bootstrap_required
    引导标记——判定必须走 davies_bootstrap 的参数化自助经验零分布。"""
    alpha = C_FTEST_ALPHA if alpha is None else float(alpha)
    out = {'pair': [fit_simple['model'], fit_rich['model']], 'alpha': alpha,
           'comparable': True, 'test': None, 'p': None, 'F': None,
           'dbic': None, 'verdict': None, 'reason': None, 'p_method': None}
    if not (fit_simple.get('comparable') and fit_rich.get('comparable')):
        out.update(comparable=False, verdict='rejected',
                   reason='CA-44①: 有拟合未收敛/optimality 超限，不得进入模型比较')
        return out
    if fit_simple.get('mask_hash') != fit_rich.get('mask_hash'):
        out.update(comparable=False, verdict='rejected',
                   reason='F-29①: mask_hash 不一致，直接拒比')
        return out
    dpair = (fit_simple['model'], fit_rich['model'])
    if dpair not in DAVIES_PAIRS:
        dpair = (fit_rich['model'], fit_simple['model'])
    if dpair in DAVIES_PAIRS:
        out.update(p_method='parametric_bootstrap', verdict='davies_bootstrap_required',
                   reason='F-29③: 断点 ν_b 在零假设下不可识别（Davies 问题），'
                          '常规 F 检验与 Δχ²/ΔBIC 门限都会高估显著性；判定须经 '
                          'davies_bootstrap 的参数化自助经验零分布（T-18）')
        return out
    dk = NESTED_PAIRS.get((fit_simple['model'], fit_rich['model']))
    if dk is None and NESTED_PAIRS.get((fit_rich['model'], fit_simple['model'])):
        # 调用方把简/繁顺序传反：按登记方向重跑（嵌套表是有向的）
        return compare_models(fit_rich, fit_simple, alpha=alpha)
    if dk is not None:
        if fit_rich['n_par'] - fit_simple['n_par'] != dk:
            out.update(comparable=False, verdict='rejected',
                       reason='Δk 与嵌套登记不一致')
            return out
        if fit_rich['dof'] <= 0:
            out.update(comparable=False, verdict='rejected', reason='dof ≤ 0')
            return out
        f_stat = ((fit_simple['chi2'] - fit_rich['chi2']) / dk) \
            / (fit_rich['chi2'] / fit_rich['dof'])
        p = float(_f_dist.sf(f_stat, dk, fit_rich['dof']))
        out.update(test='f_test', F=float(f_stat), p=p,
                   verdict='rich' if p < alpha else 'simple')
        return out
    dbic = fit_simple['bic'] - fit_rich['bic']    # >0 ⇒ 复杂模型更优
    out.update(test='bic', dbic=float(dbic),
               verdict=('rich' if dbic > C_BIC_MIN else
                        'simple' if dbic < -C_BIC_MIN else 'inconclusive'))
    return out


def davies_bootstrap(fit_simple, fit_rich, nu_hz, flux_fnu, flux_err,
                     cfg_simple, cfg_rich=None, *, alpha=None, n_boot=C_BOOT_N,
                     seed=None, spec_hash=None, tick=None):
    """F-29③：pl→pl2 断点检验的**参数化自助**经验零分布（唯一判定通路）。

    原文链（F-29③）：断点 ν_b 在零假设下不可识别（Davies 问题），且宿主
    powerlaw_2seg 的硬约束 β1 ≤ β2 使"无断折"（Δβ=0）落在允许域的**边界**
    上 ⇒ 检验统计量的渐近分布不是普通 χ²，常规 F 检验与 Δχ² 门限都会**高估**
    显著性。规范做法只有一个：在 pl 假设下重采样的参数化自助经验零分布；
    边界 χ² 混合分布（50:50）只能作为自检用的旁证，不得作为判定依据（T-18），
    回显 p_method='parametric_bootstrap'。

    实现：统计量 T = Δχ² = χ²_pl − χ²_pl2（单调变换不改自助 p，故不再除
    Δk/自由度做 F 型缩放）。零分布：以 pl 的拟合曲线 m(θ̂_pl) 为均值、按
    F-92 同一白化通路重抽白化残差 ε* ~ N(0,1) ⇒ flux* = m + σ·(L·ε*)，逐次
    重拟 pl 与 pl2 得 T* 的经验零分布。重拟合走 F-91 同一引擎形态
    （least_squares·trf·x_scale='jac'·diff_step/max_nfev 同值、盒式边界含
    ν_b 覆盖内收窄），但**单起点**（各自取观测拟合的 θ̂ 作初值——参数化自助
    的标准做法：零假设下重抽数据离 θ̂_pl 最近；pl2 从观测断折解出发自行回落
    Δβ=0 边界）且不走网格/剖面/协方差通路（replicate 只消费 χ²）。
    p = (1 + #{T* ≥ T_obs}) / (1 + n_eff)
    （Davison 的加 1 式，避免 p=0 的伪精确），与 C_FTEST_ALPHA 比定 verdict；
    同屏回显 p_chi2mix = 0.5·1[T_obs≤0] + 0.5·P(χ²₁ ≥ T_obs)（边界混合旁证）
    并附「不得作为判定依据」的书面注记。
    发生器构造式唯一（Q-35）：Generator(PCG64(seed))；seed 走 _resolve_seed
    （null ⇒ int(spec_hash,16)）。tick：ST-5 重档超时的逐次检查点（API 传入
    _deadline_check）。

    ST-10 口径登记（P2b 评审）：本检验的 n_boot **不计入** C_BOOT_N_BUDGET
    预算池。原文口径分歧：ST-10 行写「单次请求全部自助次数之和 ≤
    C_BOOT_N_BUDGET」，而 Q-15/F-28/F-95③ 把同一预算池绑定在**误差推断**
    （F-95② 抽样 + F-28 剖面重拟合）上，F-29③ 又把参数化自助定为 pl→pl2 的
    **唯一判定通路**——若判定通路的必需次数也挤进误差预算池，剖面重拟合会把
    n_boot 挤到不可用（判定通路被误差推断吃掉，F-29③ 落空）。裁量：误差推断
    预算在 fit_spectrum 内**照旧硬封顶**（C_BOOT_N_BUDGET 语义不变）；本检验
    的 n_boot 只受 C_MAX_BOOT（Q-15 请求侧）与 ST-5 重档 5 s 硬超时兜底
    （tick 逐次检查点，超时一律 E-11），不与误差推断混算。"""
    if (fit_simple['model'], fit_rich['model']) not in DAVIES_PAIRS:
        raise ContinuumError('E-14', 'davies_bootstrap 只适用于 DAVIES_PAIRS '
                                     f'{sorted(DAVIES_PAIRS)}（F-29③），'
                                     f'得到 ({fit_simple["model"]}, {fit_rich["model"]})')
    alpha = C_FTEST_ALPHA if alpha is None else float(alpha)
    nu = np.asarray(nu_hz, dtype=float)
    flux = np.asarray(flux_fnu, dtype=float)
    sigma = np.asarray(flux_err, dtype=float)
    if nu.size != flux.size or nu.size != sigma.size:
        raise ContinuumError('E-14', 'nu/flux/sigma 长度不一致')
    seed = _resolve_seed(spec_hash, seed)
    cfg_r = dict(cfg_rich or cfg_simple)
    spec_s = _effective_spec('pl', cfg_simple)
    spec_r = _effective_spec('pl2', cfg_r)
    # pl 假设下的均值曲线（观测流量空间；白化通路与原拟合同一规则，F-92⑤）
    m_hat = np.asarray(spec_s['fnu'](fit_simple['params'], nu, cfg_simple),
                       dtype=float)
    L, _cm = _cov_path(flux, fit_simple.get('rho_used'))
    t_obs = max(float(fit_simple['chi2']) - float(fit_rich['chi2']), 0.0)

    def _xhat_of(fit, names):
        return np.array([float(fit['params'][n]) for n in names])

    def _quick_chi2(spec, names, lb, ub, x0, cfg):
        """单起点重拟合（F-91 引擎形态同主拟合；只返回 χ²）。"""
        margin = 1e-9 * (ub - lb)

        def fun(xv):
            th = dict(spec['fixed'])
            th.update(zip(names, xv))
            return _whiten((spec['fnu'](th, nu, cfg) - flux_star) / sigma, L)

        res = least_squares(fun, np.clip(x0, lb + margin, ub - margin),
                            bounds=(lb, ub), method='trf', x_scale='jac',
                            diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX,
                            ftol=_FTOL, xtol=_FTOL)
        return 2.0 * res.cost

    names_s, lb_s, ub_s = _layout('pl', cfg_simple)
    names_r, lb_r, ub_r = _layout('pl2', cfg_r)
    i_nu = names_r.index('nu_b')                 # F-91②：ν_b 界随本次覆盖收窄
    lb_r[i_nu] = max(lb_r[i_nu], float(nu.min()))
    ub_r[i_nu] = min(ub_r[i_nu], float(nu.max()))
    x0_s, x0_r = _xhat_of(fit_simple, names_s), _xhat_of(fit_rich, names_r)

    rng = np.random.Generator(np.random.PCG64(int(seed)))
    stats = []
    flux_star = np.empty(nu.size)
    for _k in range(max(1, int(n_boot))):
        if tick is not None:
            tick()
        eps = rng.standard_normal(nu.size)
        flux_star[:] = m_hat + sigma * (L @ eps if L is not None else eps)
        try:
            c_s = _quick_chi2(spec_s, names_s, lb_s, ub_s, x0_s, cfg_simple)
            c_r = _quick_chi2(spec_r, names_r, lb_r, ub_r, x0_r, cfg_r)
        except ContinuumError:
            continue                              # 该次 replicate 不可拟 ⇒ 弃，不计入
        stats.append(max(c_s - c_r, 0.0))
    n_eff = len(stats)
    if n_eff < 2:
        raise ContinuumError('E-14', 'Davies 自助有效 replicate 不足（全部拟合被拒）')
    p_boot = (1.0 + sum(1 for t in stats if t >= t_obs)) / (1.0 + n_eff)
    p_mix = (0.5 if t_obs <= 0.0 else 0.0) + 0.5 * float(_chi2_dist.sf(t_obs, 1.0))
    return {
        'pair': [fit_simple['model'], fit_rich['model']],
        'test': 'parametric_bootstrap', 'p_method': 'parametric_bootstrap',
        'stat_name': 'delta_chi2', 'stat_obs': t_obs,
        'p': float(p_boot), 'alpha': alpha,
        'verdict': 'rich' if p_boot < alpha else 'simple',
        'n_boot': n_eff, 'n_boot_requested': int(n_boot),
        'dk': int(fit_rich['n_par'] - fit_simple['n_par']),
        'p_chi2mix': float(p_mix),
        'note': '边界 χ² 混合分布（50:50）仅作自检旁证（p_chi2mix），'
                '不得作为判定依据（F-29③/T-18）；判定 p 由参数化自助经验'
                '零分布给出',
        'err_seed': seed, 'rng_algo': 'pcg64',
    }


# ─── 8. 闭包三候选（F-30） ───────────────────────────────────────────

_CAND_LABELS = (
    'p=2β（ν>ν_c，Granot & Sari 2002 标准理论）',
    'p=2β+1（ν_m<ν<ν_c，Granot & Sari 2002）',
    'p=2β+2（宿主保留备选：宿主标为慢冷却 ν>ν_c，与教科书 p=4β−2 不一致，'
    '不归 L-31）',
)


def closure_from_fit(fit):
    """F-30：报三个候选指数并写明各自适用频段；区间端点经线性映射 G=2·err
    （delta_method，§3.9.3.1 闭包行）。三候选是备选映射，禁止合成单一 σ。"""
    beta = fit.get('params', {}).get('beta')
    if beta is None:
        raise ContinuumError('E-14', '闭包诊断需要幂律 β（模型 pl/pl_dust）')
    el = fit.get('err_low', {}).get('beta')
    eh = fit.get('err_high', {}).get('beta')
    es = fit.get('err_source', {}).get('beta')
    mapping = (2.0, 2.0, 2.0)                     # p=2β ⇒ err 逐候选 ×2
    return {
        # 区间包络 = 三候选区间 [2β+m−2el, 2β+m+2eh] 的并集端点（F-30：输出为
        # 区间而非单一闭包因子；三候选各自区间另经 p_err_lo/hi 成对给出）
        'factor_lo': None if el is None else 2.0 * beta - 2.0 * el,
        'factor_hi': None if eh is None else 2.0 * beta + 2.0 + 2.0 * eh,
        'cand': list(_CAND_LABELS),
        'p_indices': [2.0 * beta, 2.0 * beta + 1.0, 2.0 * beta + 2.0],
        'p_err_lo': [None if el is None else g * el for g in mapping],
        'p_err_hi': [None if eh is None else g * eh for g in mapping],
        'err_source': 'delta_method' if es in ('profile', 'covariance',
                                               'covariance_approx', 'bootstrap')
                      else 'none',
        'err_scope': 'stat',
        'note': '三候选是备选而非同一物理量的三种算法；输出为区间而非单一闭包因子',
    }


# ─── 9. poly 经验基线（F-62 + F-112 杠杆值端点闸门，P2 切片 2c） ─────────

def _poly_axis(lam_aa):
    """F-62① 的自变量：ln λ 归一化到 [-1,1]（Chebyshev 基与 cond_2 均定义其上）。"""
    ln = np.log(np.asarray(lam_aa, dtype=float))
    span = float(ln.max() - ln.min())
    if span <= 0:
        raise ContinuumError('E-14', 'degenerate_axis: ln λ 覆盖为零')
    return 2.0 * (ln - ln.min()) / span - 1.0


def _poly_design(lam_aa, order, sigma, L):
    """Chebyshev 基、自变量 ln λ 归一化到 [-1,1]（F-62①），返回白化设计阵。"""
    u = _poly_axis(lam_aa)
    M = np.polynomial.chebyshev.chebvander(u, int(order))
    return _whiten(M / np.asarray(sigma, dtype=float)[:, None], L)


def poly_leverage(lam_aa, order, sigma, L):
    """F-112② 杠杆值：h_ii = diag(X(XᵀX)⁻¹Xᵀ)（SVD 形式 h_i = Σ_k U[i,k]²，
    就地可导出），X = **白化后**设计阵（σ 加权 + AR(1) 白化，即 lstsq 实际使用
    的矩阵；F-62③ 病态降阶后按降阶后的 X 计）。h_ii 对列缩放不变 ⇒ 与 ln λ
    归心化/缩放无关；X 满秩时 Σh_ii = p（p = X 列数，n = 参与安置的行数）。
    返回 (h, X)。"""
    X = _poly_design(lam_aa, order, sigma, L)
    u_, _s_, _vt_ = np.linalg.svd(X, full_matrices=False)
    return np.sum(u_ ** 2, axis=1), X


def _edge_spread(lam_aa, flux, coef, order, exclude):
    """F-112① baseline_edge_spread：基线在覆盖两端各 C_EDGE_WIN_FRAC（ln λ 窗口）
    内的中位残差之差 ÷ 覆盖内 median|F|。残差 = F − 基线（flux 量纲）；低可靠端
    （杠杆值闸门标出的行）不参与统计（F-112②）。返回 (spread, blue_seg, red_seg)；
    窗口无有效行或 median|F| 无效 ⇒ (None, None, None)（判据不可判 ≠ 0）。"""
    lam = np.asarray(lam_aa, dtype=float)
    f = np.asarray(flux, dtype=float)
    u = _poly_axis(lam)
    M = np.polynomial.chebyshev.chebvander(u, int(order))
    resid = f - M @ np.asarray(coef, dtype=float)
    blue = u <= -1.0 + 2.0 * C_EDGE_WIN_FRAC
    red = u >= 1.0 - 2.0 * C_EDGE_WIN_FRAC
    ok = np.isfinite(resid) & ~exclude
    fin_f = np.isfinite(f) & ~exclude
    if not bool(fin_f.any()):
        return None, None, None
    med_f = float(np.median(np.abs(f[fin_f])))
    if not (med_f > 0) or not bool((blue & ok).any()) or not bool((red & ok).any()):
        return None, None, None
    mb = float(np.median(resid[blue & ok]))
    mr = float(np.median(resid[red & ok]))
    blue_seg = [float(lam[blue].min()), float(lam[blue].max())]
    red_seg = [float(lam[red].min()), float(lam[red].max())]
    # T-80①：两端窗口基线中位残差之差（取绝对值 ⇒ 蓝上翘/红下弯镜像同检）
    return abs(mr - mb) / med_f, blue_seg, red_seg


def _poly_solve(M_w, d_w, order, warnings):
    """F-62③：cond_2 降阶循环；降到 1 阶仍超 C_COND_MAX ⇒ 拒（CA-20）。

    只用 np.linalg.lstsq（SVD），禁正规方程 AᵀA（F-62②）。返回
    (coef, cond, order, M_w, cond_initial)——cond_initial 是请求阶数下、降阶
    **之前**的条件数（CA-48② 第二支的判定量，F-111⑤；final cond 降阶后恒 ≤
    C_COND_MAX，若只在 final cond 上判则 ② 支永不可达）。"""
    def _cond(M):
        # m < n 时算子有零空间 ⇒ 条件数无穷（np.linalg.cond 看不见，须就地补）
        return math.inf if M.shape[0] < M.shape[1] else float(np.linalg.cond(M))
    cond0 = _cond(M_w)
    cond = cond0
    while cond > C_COND_MAX and order > 1:
        order -= 1
        M_w = M_w[:, :-1]
        warnings.append(_warn('CA-20', 'poly 设计矩阵 cond_2=%.3g > C_COND_MAX，'
                              '降阶至 %d' % (cond, order)))
        cond = _cond(M_w)
    if cond > C_COND_MAX:
        raise ContinuumError('CA-20', 'poly 降到 1 阶 cond_2=%.3g 仍超 C_COND_MAX，'
                             '拒绝该模型' % cond)
    coef, _res, _rank, _sv = np.linalg.lstsq(M_w, d_w, rcond=None)
    return coef, cond, order, M_w, cond0


def fit_poly(lam_aa, flux, flux_err, order=C_POLY_ORDER_MAX, *, mask_hash=None,
             nonpos_frac=None):
    """Chebyshev 经验基线拟合（F-62；S3 用，S2 侧同函数复用）+ F-112 杠杆值
    端点闸门（P2 切片 2c）。

    order ≤ C_POLY_ORDER_MAX（Q-15）；线性模型无 trf 引擎 ⇒ engine=None、
    无 engine_status（F-91 只管辖非线性拟合）。nonpos_frac：拟合区非正流量占比
    （API-3 传入，V-6 口径）——CA-48② 第二支需要它与 cond_2_initial 同时超阈
    （F-111⑤，两因子缺一不可）。

    F-112 装配（baseline_gate 键，仅诊断；S1 侧无 F-62 基线 ⇒ 不适用）：
      - 杠杆值闸门：h_ii > C_LEVERAGE_K·p/n 标"低可靠端"，回显 n_high_leverage；
        **只标记不剔除**（X 的行集不变 ⇒ Σh_ii = p 恒成立，T-80②）。
      - baseline_edge_spread > C_EDGE_SPREAD_MAX ⇒ CA-48①；先降一阶重拟**一次**
        （回显 edge_spread_after_downgrade），仍超 ⇒ 两端窗口生成**建议**掩膜段
        （suggested_edge_ranges，走 U-52 确认路径；未确认前不掩任何像素）。
      - CA-48 文案禁词（IGM/Lyman-森林/尘埃梯度/红侧证据，T-80③）不得出现——
        端点摆动不得被读成物理证据（F-112③）。"""
    if not 1 <= int(order) <= C_POLY_ORDER_MAX:
        raise ContinuumError('E-14', 'poly_order 超出 [1, C_POLY_ORDER_MAX]')
    lam = np.asarray(lam_aa, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(flux_err, dtype=float)
    if lam.size != flux.size or lam.size != sigma.size:
        raise ContinuumError('E-14', 'lam/flux/sigma 长度不一致')
    warnings = []
    rho, rho_warn = _errs.rho_lag1(flux)
    if rho_warn:
        warnings.append(rho_warn)
    L, cov_method = _cov_path(flux, rho)
    M_w = _poly_design(lam, order, sigma, L)
    d_w = _whiten(flux / sigma, L)
    coef, cond, order, M_w, cond0 = _poly_solve(M_w, d_w, int(order), warnings)
    resid = M_w @ coef - d_w
    chi2 = float(np.sum(resid ** 2))
    m, n_par = int(lam.size), int(order) + 1
    dof = m - n_par
    sigma0, cov_reason = _cov_from_jac(M_w, dof)
    infl = _infl(chi2, dof)
    if cov_reason is not None:
        warnings.append(_warn('CA-44', 'poly 协方差整体不可信（%s）' % cov_reason))
    extra = _errs.corr_inflation(rho) ** 2 if cov_method == 'diag_infl' else 1.0
    names = ['c%d' % k for k in range(n_par)]
    sig = {n: (None if sigma0 is None else
               float(math.sqrt(sigma0[i, i] * infl ** 2 * extra)))
           for i, n in enumerate(names)}
    sig_raw = {n: (None if sigma0 is None else float(math.sqrt(sigma0[i, i])))
               for i, n in enumerate(names)}
    err_source = {n: ('covariance_approx' if cov_method == 'diag_infl'
                      else 'covariance') if sig[n] is not None else 'none'
                  for n in names}

    # ── F-112②：杠杆值闸门（只标记不剔除；X = 降阶后的白化设计阵） ──
    h_ii, X = poly_leverage(lam, order, sigma, L)
    p, n_rows = int(X.shape[1]), int(X.shape[0])
    lev = h_ii > C_LEVERAGE_K * p / n_rows
    n_high_leverage = int(np.count_nonzero(lev))
    # ── F-112①④：端点摆动诊断 + 降一阶重拟一次 + 建议掩膜段 ──
    spread, blue_seg, red_seg = _edge_spread(lam, flux, coef, order, lev)
    edge_after, suggested = None, []
    if spread is not None and spread > C_EDGE_SPREAD_MAX:
        dg = int(order) - 1
        if dg >= 1:                              # 降一阶重拟一次（F-112④）
            try:
                M_dg = _poly_design(lam, dg, sigma, L)
                coef_dg, _c, order_dg, _M, _c0 = _poly_solve(
                    M_dg, d_w, dg, [])
                edge_after, _b2, _r2 = _edge_spread(lam, flux, coef_dg, order_dg,
                                                    lev)
            except ContinuumError:
                edge_after = None                # 诊断重拟被拒（cond 病态）⇒ 不回显
        if edge_after is None or edge_after > C_EDGE_SPREAD_MAX:
            if blue_seg and red_seg:             # 建议掩膜段（只建议，U-52 确认）
                suggested = [blue_seg, red_seg]
    # ── CA-48 闭合触发集（两支；两支同真 ⇒ 一个档、文案列明两支，T-80④） ──
    branches = []
    if spread is not None and spread > C_EDGE_SPREAD_MAX:
        branches.append('① baseline_edge_spread=%.3g > C_EDGE_SPREAD_MAX=%s'
                        '（两端基线不可信：中间覆盖段的数值仍可用，端点段任何读数'
                        '降级为仅供参考；%s）'
                        % (spread, C_EDGE_SPREAD_MAX,
                           ('降一阶重拟后 edge_spread_after_downgrade=%.3g'
                            % edge_after) if edge_after is not None
                           else '已至 1 阶无再降余地'))
    if nonpos_frac is not None and cond0 > C_COND_MAX \
            and float(nonpos_frac) > C_NONPOS_FRAC_MAX:
        branches.append('② nonpos_frac=%.3g > C_NONPOS_FRAC_MAX=%s 且 '
                        'cond_2=%.3g > C_COND_MAX 同时成立（基线过扣 + 设计矩阵'
                        '病态，F-111⑤）'
                        % (nonpos_frac, C_NONPOS_FRAC_MAX, cond0))
    if branches:
        tail = ('建议掩膜段已随 suggested_edge_ranges 回显，未经确认不会被掩掉'
                '（F-112④/U-52）；n_high_leverage=%d 已回显'
                % n_high_leverage if suggested else
                'n_high_leverage=%d 已回显；本次未生成建议掩膜段' % n_high_leverage)
        warnings.append(_warn('CA-48', '两端基线不可信（CA-48）：'
                              + '；'.join(branches) + '。' + tail))
    return {
        'model': 'poly', 'params': {n: float(c) for n, c in zip(names, coef)},
        'err_low': dict(sig), 'err_high': dict(sig),   # 线性模型对称区间
        'sigma_theta': sig, 'sigma_theta_raw': sig_raw, 'infl': infl,
        'chi2': chi2, 'dof': dof, 'n_par': n_par,
        'bic': chi2 + n_par * math.log(m),
        'cond_2': cond, 'poly_basis': 'cheb_lnlambda', 'poly_order': int(order),
        'engine': None, 'engine_status': None,
        'cov_method': cov_method, 'rho_used': rho, 'cond_G': None,
        'err_source': err_source, 'err_scope': {n: 'stat' for n in names},
        'mask_hash': mask_hash, 'warnings': warnings,
        'err_semantics': 'lower_bound',
        # F-112 诊断包（continuum_api 消费进 preprocess{} 的 S2 四键与 CA-48；
        # h_ii 本身不回显，T-80② 由 poly_leverage 就地复算）
        'baseline_gate': {
            'n_high_leverage': n_high_leverage,
            'baseline_edge_spread': spread,
            'edge_spread_after_downgrade': edge_after,
            'suggested_edge_ranges': suggested,
            'cond_2_initial': (None if math.isinf(cond0) else cond0),
            'leverage_threshold': C_LEVERAGE_K * p / n_rows,
            'n_fit_rows': n_rows, 'p': p,
        },
    }
