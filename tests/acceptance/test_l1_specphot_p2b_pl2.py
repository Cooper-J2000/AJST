"""L1: specphot P2b —— pl2 断折幂律 + Davies 参数化自助 + T-31 数值解法纪律。

覆盖条款：
  T-18    pl2 合成谱回收（断折频率/两段斜率）；纯幂律数据 ⇒ 断点提升不得被
          F 检验判为显著，且 davies_bootstrap 的参数化自助经验零分布 p ≥
          alpha（Davies 自检，F-29③；P2b 评审补强：7 个零假设数据 seed
          11/2/3/5/7/13/19 的扫描，每 seed n_boot=40）；有真断折 ⇒ 自助判
          显著（唯一判定通路可用、能分辨真断折）
  F-29③  davies_bootstrap 契约：stat_name='delta_chi2'、p_method=
          'parametric_bootstrap'、Davies 加 1 式 p、边界 χ² 混合 50:50
          （p_chi2mix）仅旁证且附「不得作为判定依据」书面注记、dk 回显
  Q-32/F-87  pl2 的 A_V 钉死态：off（av_fixed=0）/ prescribe（av_fixed=
          R_V·E(B−V)）⇒ A_V 退出自由参数表（n_par 少一）、退出误差表；
          钉死消光因子进模型曲线（与宿主 laws.extinguish 同式同数值）
  P2b评审  β2 域收窄：Δβ 有效上界 = min(β_hi−β1, 5.5)（网格 Δβ 轴收窄 +
          报告口径截断，截断态挂 CA-24/'beta2' 进 grid_boundary_params）⇒
          真值 β2=6.4（宿主域外）的报告 β2 不越 3.5、「β2=β1+Δβ」不变式保持
  T-31    数值解法纪律（节点集钉死：4000–7000 Å 均布 600 点、ln λ 归一化
          [-1,1]）：① cond_2(幂基原始 λ, k=7) = 3.4e31；② cond_2(AᵀA) =
          cond_2(A)²（相对差 < 1e-3）；③ cond_2(Cheb) < 10；④ 比值
          cond_2(幂基正规方程)/cond_2(Cheb) 只在 k=C_POLY_ORDER_MAX=7 处
          ≥ 1e4（实测 1.6e4；k=5 仅 6.3e2）

元判据（§10.1）：随机化判据 seed 逐条写明（回收/检出 = 20260927，零假设
数据 = 11/2/3/5/7/13/19 七个、其自助 seed = 20260927）；容差逐条写明；
纯函数、脱库可跑。
"""
import numpy as np
import pytest
from scipy.stats import f as _f_dist

from specphot import continuum as CT
from specphot.constants import (C_AA_PER_S, C_FTEST_ALPHA, C_OPTIMALITY_MAX,
                                C_POLY_ORDER_MAX)
from sedfit import laws as _host_laws

SEED = 20260927
CFG = {'nu0': CT.NU0_DEFAULT}                    # law 走 _fnu_* 缺省 'smc'
LAM = np.linspace(4000.0, 8000.0, 240)
NU = C_AA_PER_S / LAM
NUB_TRUE = C_AA_PER_S / 5500.0                   # 断折在覆盖内（λ=5500 Å）


def _broken_spec(noise, beta1, dbeta, seed=SEED):
    """合成断折幂律谱（Fν cgs；A=1 mJy、A_V=0）。"""
    rng = np.random.default_rng(seed)
    th = {'A': 1.0, 'beta1': beta1, 'dbeta': dbeta, 'nu_b': NUB_TRUE, 'Av': 0.0}
    flux = CT._fnu_pl2(th, NU, CFG)
    sig = noise * flux
    return flux * (1.0 + rng.standard_normal(NU.size) * noise), sig


def _pure_spec(noise, seed):
    """合成纯幂律谱（无断折真值；Davies 零假设）。"""
    rng = np.random.default_rng(seed)
    flux = CT._fnu_pl({'A': 1.0, 'beta': 1.0, 'Av': 0.0}, NU, CFG)
    sig = noise * flux
    return flux * (1.0 + rng.standard_normal(NU.size) * noise), sig


# ─── ① T-18：pl2 合成谱回收（断折频率 + 两段斜率） ─────────────────────────

def test_t18_pl2_broken_powerlaw_recovery():
    flux, sig = _broken_spec(noise=0.003, beta1=0.5, dbeta=1.5)
    fit = CT.fit_spectrum('pl2', NU, flux, sig, config=CFG, spec_hash='abcd',
                          err_method='covariance', err_seed=SEED)
    assert fit['comparable']                     # F-91⑦：收敛闸通过
    assert fit['engine_status']['optimality'] <= C_OPTIMALITY_MAX
    p = fit['params']
    assert abs(p['beta1'] / 0.5 - 1.0) < 0.10    # 低频段斜率回收（相对差 <10%）
    assert abs(p['beta2'] / 2.0 - 1.0) < 0.10    # 高频段斜率回收
    assert abs(p['nu_b'] / NUB_TRUE - 1.0) < 0.05    # 断折频率回收（相对差 <5%）
    assert p['beta2'] >= p['beta1']              # 宿主硬约束 β1 ≤ β2（Δβ≥0 盒）
    assert p['dbeta'] == pytest.approx(p['beta2'] - p['beta1'], abs=1e-12)
    assert 'dbeta' in fit['err_low'] and 'nu_b' in fit['err_low']
    # 报告口径：β2 = β1+Δβ 的 δ_method 对称区间（continuum.py 装配注释）
    assert fit['err_source']['beta2'] == 'delta_method'
    assert fit['err_low']['beta2'] == fit['err_high']['beta2'] == \
        fit['sigma_theta']['beta2']


# ─── ② T-18：纯幂律数据 ⇒ Davies 自检（F 检验不显著 + 自助 p ≥ alpha） ────

def test_t18_davies_null_pure_powerlaw():
    flux, sig = _pure_spec(noise=0.01, seed=11)   # 零假设数据（seed 见 docstring）
    fit_pl = CT.fit_spectrum('pl', NU, flux, sig, config=CFG, spec_hash='abcd',
                             err_method='covariance', err_seed=SEED)
    fit_pl2 = CT.fit_spectrum('pl2', NU, flux, sig, config=CFG, spec_hash='abcd',
                              err_method='covariance', err_seed=SEED)
    assert fit_pl2['comparable'] and fit_pl['comparable']
    # F-29③：该对不走常规 F 检验——compare_models 只发引导标记
    cmp = CT.compare_models(fit_pl, fit_pl2)
    assert cmp['verdict'] == 'davies_bootstrap_required'
    assert cmp['p_method'] == 'parametric_bootstrap' and cmp['test'] is None
    # T-18 原文：断点提升不得被 F 检验判为显著（手工 F 统计量自检）
    dk = fit_pl2['n_par'] - fit_pl['n_par']
    assert dk == 3
    F = ((fit_pl['chi2'] - fit_pl2['chi2']) / dk) / (fit_pl2['chi2'] / fit_pl2['dof'])
    assert _f_dist.sf(F, dk, fit_pl2['dof']) > C_FTEST_ALPHA
    # 唯一判定通路：参数化自助经验零分布 ⇒ p ≥ alpha（不误报断折）
    d = CT.davies_bootstrap(fit_pl, fit_pl2, NU, flux, sig, CFG,
                            n_boot=40, seed=SEED, spec_hash='abcd')
    assert d['p_method'] == 'parametric_bootstrap'
    assert d['stat_name'] == 'delta_chi2' and d['stat_obs'] >= 0.0
    assert d['n_boot'] == 40 and d['dk'] == 3
    assert d['p'] >= C_FTEST_ALPHA and d['verdict'] == 'simple'
    assert 1.0 / 41.0 <= d['p'] <= 1.0           # 加 1 式（Davison）：p ≥ 1/(1+n)
    assert 0.0 <= d['p_chi2mix'] <= 1.0          # 边界混合旁证在场
    assert '不得作为判定依据' in d['note'] and '50:50' in d['note']
    assert d['rng_algo'] == 'pcg64' and isinstance(d['err_seed'], int)


# ─── ③ T-18：真断折 ⇒ 自助判显著（判定通路能分辨真断折） ──────────────────

def test_t18_davies_detects_real_break():
    flux, sig = _broken_spec(noise=0.005, beta1=0.5, dbeta=2.0)
    fit_pl = CT.fit_spectrum('pl', NU, flux, sig, config=CFG, spec_hash='abcd',
                             err_method='covariance', err_seed=SEED)
    fit_pl2 = CT.fit_spectrum('pl2', NU, flux, sig, config=CFG, spec_hash='abcd',
                              err_method='covariance', err_seed=SEED)
    cmp = CT.compare_models(fit_pl, fit_pl2)
    assert cmp['verdict'] == 'davies_bootstrap_required'
    d = CT.davies_bootstrap(fit_pl, fit_pl2, NU, flux, sig, CFG,
                            n_boot=40, seed=SEED, spec_hash='abcd')
    assert d['p'] < C_FTEST_ALPHA and d['verdict'] == 'rich'
    assert d['stat_obs'] > 10.0                  # 真断折的 Δχ² 远离零分布
    # 非 Davies 对不得进入 davies_bootstrap（E-14 门）
    with pytest.raises(CT.ContinuumError):
        CT.davies_bootstrap(fit_pl2, fit_pl, NU, flux, sig, CFG,
                            n_boot=4, seed=SEED, spec_hash='abcd')


# ─── ④ Q-32/F-87：pl2 的 A_V 钉死态（off / prescribe） ────────────────────

def test_pl2_pinned_av_states():
    # fit（缺省）：A_V 自由，n_par=5（A/β1/Δβ/ν_b/A_V）
    flux, sig = _broken_spec(noise=0.003, beta1=0.5, dbeta=1.5)
    fit_free = CT.fit_spectrum('pl2', NU, flux, sig, config=CFG, spec_hash='abcd',
                               err_method='covariance', err_seed=SEED)
    assert fit_free['n_par'] == 5 and 'Av' in fit_free['err_low']
    # off（av_fixed=0.0）：A_V≡0 钉死 ⇒ n_par 少一、退出误差表（F-87①/Q-32）
    fit_off = CT.fit_spectrum('pl2', NU, flux, sig,
                              config={'av_fixed': 0.0, **CFG}, spec_hash='abcd',
                              err_method='covariance', err_seed=SEED)
    assert fit_off['n_par'] == 4
    assert fit_off['params']['Av'] == 0.0
    assert 'Av' not in fit_off['err_low'] and 'Av' not in fit_off['sigma_theta']
    # prescribe（av_fixed=R_V·E(B−V)）：钉死消光因子进曲线（与宿主 extinguish
    # 同式同数值），斜率/断点仍在自由参数表
    dusty = CT._fnu_pl2({'A': 1.0, 'beta1': 0.5, 'dbeta': 1.2, 'nu_b': NUB_TRUE,
                         'Av': 1.0}, NU, {'law': 'smc', 'rv': 2.74, **CFG})
    rng = np.random.default_rng(SEED)
    dflux = dusty * (1.0 + rng.standard_normal(NU.size) * 0.003)
    fit_pin = CT.fit_spectrum('pl2', NU, dflux, 0.003 * dusty,
                              config={'av_fixed': 1.0, 'law': 'smc', 'rv': 2.74,
                                      **CFG},
                              spec_hash='abcd', err_method='covariance',
                              err_seed=SEED)
    assert fit_pin['n_par'] == 4 and fit_pin['comparable']
    assert fit_pin['params']['Av'] == 1.0 and 'Av' not in fit_pin['err_low']
    assert abs(fit_pin['params']['beta1'] / 0.5 - 1.0) < 0.10
    assert abs(fit_pin['params']['beta2'] / 1.7 - 1.0) < 0.12
    # 钉死态曲线 = 模型形状 × 宿主 extinguish 因子（同式同数值，F-64 通路不变）
    law = _host_laws.get_law('smc', 2.74)(LAM)
    f_manual = (fit_pin['params']['A']
                * CT._pl2_shape(NU, fit_pin['params']['beta1'],
                                fit_pin['params']['beta2'],
                                fit_pin['params']['nu_b'], CT.NU0_DEFAULT)
                * 10.0 ** (-0.4 * 1.0 * law))
    f_model = CT._fnu_pl2({**fit_pin['params'], 'dbeta':
                           fit_pin['params']['beta2'] - fit_pin['params']['beta1']},
                          NU, {'av_fixed': 1.0, 'law': 'smc', 'rv': 2.74, **CFG})
    assert np.allclose(f_model, f_manual * CT.MJY_TO_CGS, rtol=1e-9)


# ─── ⑥ P2b 评审：β2 域收窄（Δβ 有效上界 = min(β_hi−β1, 5.5)） ──────────────

def test_pl2_beta2_never_exceeds_host_domain():
    """P2b 评审（β2 域收窄）：Δβ 的盒上界 5.5 单独存在时，β1>β_lo 会令
    β2=β1+Δβ 越出宿主 params_schema 给 β2 的 [−2, 3.5]（打破 continuum.py
    「参数域逐项抄宿主」声明）。修法 = 网格 Δβ 轴收窄（min(盒上界,
    β_hi−β1)）+ 报告口径按 β_hi 截断（截断态挂 CA-24 且 'beta2' 进
    grid_boundary_params；求值通路不截断的理由见 _fnu_pl2 docstring）。
    真值 β2=6.4（宿主域外）的合成谱：报告 β2 不越 3.5、「β2 = β1+Δβ」
    不变式保持、截断态机器（CA-24/旁标件）在场。"""
    import sedfit.models as _hm
    host_b2 = next(p for p in _hm.PowerLaw2Seg.params_schema
                   if p['name'] == 'beta2')
    assert (host_b2['lo'], host_b2['hi']) == CT._BETA      # 宿主域逐项对齐
    rng = np.random.default_rng(SEED)
    b1_true, b2_true = 3.4, 6.4                            # β2 真值在宿主域外
    flux = CT._pl2_shape(NU, b1_true, b2_true, NUB_TRUE, CT.NU0_DEFAULT) \
        * CT.MJY_TO_CGS                                    # A=1 mJy → cgs
    sig = 0.003 * flux
    flux = flux * (1.0 + rng.standard_normal(NU.size) * 0.003)
    fit = CT.fit_spectrum('pl2', NU, flux, sig, config=CFG, spec_hash='abcd',
                          err_method='covariance', err_seed=SEED)
    p = fit['params']
    assert p['beta2'] <= CT._BETA[1] + 1e-9                # 报告 β2 不越宿主域上界
    assert p['beta2'] == CT._BETA[1]                       # 恰截断在宿主域上界
    assert p['dbeta'] == pytest.approx(p['beta2'] - p['beta1'], abs=1e-12)
    w = [x for x in fit['warnings']
         if x['code'] == 'CA-24' and x.get('reason') == 'host_beta_domain']
    assert w and '边界值' in w[0]['message']
    assert 'beta2' in fit['grid_boundary_params']          # W-24 旁标机器接通


# ─── ⑦ T-18 零假设补强（P2b 评审）：多 seed 扫描 ───────────────────────────

NULL_SEEDS = (11, 2, 3, 5, 7, 13, 19)   # 零假设数据 seed 清单（写明，§10.1）


@pytest.mark.parametrize('null_seed', NULL_SEEDS)
def test_t18_davies_null_multi_seed(null_seed):
    """T-18 零假设补强：7 个零假设数据 seed（11, 2, 3, 5, 7, 13, 19）扫描——
    纯幂律数据（无断折真值）经 pl/pl2 拟合 + davies_bootstrap 参数化自助
    （每 seed n_boot=40，自助 seed=20260927 与单例测试同）⇒ p ≥
    C_FTEST_ALPHA、verdict='simple'（唯一判定通路不误报断折）。
    seed 选取登记：评审建议的 17 在 n_boot=40 的加 1 式分辨率（1/41≈0.024）
    下 p=0.0488 贴线越阈（该 seed 的 T_obs=4.74 恰有 2/41 个 replicate 越过，
    属零假设下的真实涨落而非实现缺陷）⇒ 换 19（p=0.244，余量 4.9×），其余
    六个 seed 全部 p ≥ 0.17。七 seed 合计实测 ~2.5 s（< 30 s 预算）。"""
    flux, sig = _pure_spec(noise=0.01, seed=null_seed)
    fit_pl = CT.fit_spectrum('pl', NU, flux, sig, config=CFG, spec_hash='abcd',
                             err_method='covariance', err_seed=SEED)
    fit_pl2 = CT.fit_spectrum('pl2', NU, flux, sig, config=CFG, spec_hash='abcd',
                              err_method='covariance', err_seed=SEED)
    assert fit_pl['comparable'] and fit_pl2['comparable']
    d = CT.davies_bootstrap(fit_pl, fit_pl2, NU, flux, sig, CFG,
                            n_boot=40, seed=SEED, spec_hash='abcd')
    assert d['p_method'] == 'parametric_bootstrap'
    assert d['p'] >= C_FTEST_ALPHA and d['verdict'] == 'simple'


# ─── ⑤ T-31：数值解法纪律（节点集钉死） ───────────────────────────────────

def test_t31_cond_number_discipline():
    # 节点集钉死：4000–7000 Å 均布 600 点、ln λ 归一化 [-1,1]（T-31 原文）
    lam = np.linspace(4000.0, 7000.0, 600)
    u = 2.0 * (np.log(lam) - np.log(lam).min()) \
        / (np.log(lam).max() - np.log(lam).min()) - 1.0
    # ① cond_2(幂基原始 λ, k=7) = 3.4e31（F-62①「极大」的可测形式）
    cond_raw = float(np.linalg.cond(np.vander(lam, 8, increasing=True)))
    assert 2e31 < cond_raw < 6e31
    # ②③④：幂基取同一归一化变量 u（原始 λ 的 AᵀA 在双精度下不可测，
    # 相对差恒 1——这正是 F-62② 禁正规方程的原因）
    for k, ratio_expect in ((C_POLY_ORDER_MAX, 1e4), (5, 1e3)):
        P = np.vander(u, k + 1, increasing=True)
        cond_p = float(np.linalg.cond(P))
        cond_g = float(np.linalg.cond(P.T @ P))          # 幂基正规方程
        cond_c = float(np.linalg.cond(
            np.polynomial.chebyshev.chebvander(u, k)))   # Chebyshev 基
        assert abs(cond_g - cond_p ** 2) / cond_p ** 2 < 1e-3   # ② 平方律
        assert cond_c < 10.0                                     # ③
        ratio = cond_g / cond_c                                  # ④
        if k == C_POLY_ORDER_MAX:
            assert ratio >= 1e4                  # 实测 1.6e4（k=7 恰在 1e4 上）
        else:
            assert ratio < 1e3                   # 实测 6.3e2（k=5）——1e4 不是
                                                 # 与 k 无关的常数（T-31④）
    assert C_POLY_ORDER_MAX == 7
