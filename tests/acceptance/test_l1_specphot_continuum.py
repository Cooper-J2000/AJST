"""L1: specphot continuum.py（S2 连续谱拟合纯函数层，02 §3.8 + §3.9.3）。

覆盖条款：
  T-59  引擎唯一化：源码扫描禁 lm/curve_fit/loss=/dogbox/matrix_rank；响应带
        engine='least_squares.trf' + engine_status 九键；trf 解上报（非网格内插）
  T-67  协方差截断全有/全无：秩亏 ⇒ Σ 整体 null + σ 全 null（无部分截断）；
        active_mask 触界 ⇒ 该参数 σ null + CA-44
  T-68  infl 只放大不缩小：chi2/dof=0.25 ⇒ infl=1 且 σ=σ_raw；=4 ⇒ infl=2
  F-28/F-95/Q-35  profile 与 bootstrap 同线一致性（方向）、预算回落、seed 契约
  F-29  嵌套 F 检验（pl⊂pl_dust）p 小；非嵌套拒 F 只比 BIC；mask_hash 拒比
  F-30  闭包三候选映射 p=2β / 2β+1 / 2β+2，区间端点 = 2·err（delta_method）
  F-62  poly：Chebyshev·lnλ 基、cond_2 降阶、降到 1 阶仍超限拒（CA-20）

元判据（§10.1）：随机化判据 seed = 20260927，容差逐条写明；纯函数、脱库可跑。
"""
import numpy as np
import pytest

from specphot import continuum as CT
from specphot.constants import C_BIC_MIN, C_FTEST_ALPHA, C_OPTIMALITY_MAX

SEED = 20260927
ENGINE_KEYS = {'scipy_version', 'x_scale', 'diff_step', 'nfev', 'njev',
               'optimality', 'status', 'active_mask', 'n_starts'}   # F-91⑦ 九键


def _pl_spec(n=300, snr=20.0, A=0.5, beta=0.8, seed=SEED):
    """合成纯幂律谱（Fν cgs；A mJy、ν0=5e14 宿主约定）。"""
    rng = np.random.default_rng(seed)
    lam = np.linspace(4000.0, 9000.0, n)
    nu = 2.99792458e18 / lam
    flux = A * (nu / 5e14) ** (-beta) * 1e-26
    sig = flux / snr
    return nu, flux + rng.normal(0.0, sig), sig


def _dusty_spec(n=300, snr=20.0, A=0.5, beta=0.8, av=1.0, seed=SEED):
    """合成幂律×宿主消光谱（law 走宿主 sedfit.laws，F-64 唯一通路）。"""
    from sedfit import laws as _hl
    rng = np.random.default_rng(seed)
    lam = np.linspace(4000.0, 9000.0, n)
    nu = 2.99792458e18 / lam
    law = _hl.get_law('smc')(lam)
    flux = A * (nu / 5e14) ** (-beta) * 10.0 ** (-0.4 * av * law) * 1e-26
    sig = flux / snr
    return nu, flux + rng.normal(0.0, sig), sig


# ─── ① T-59：合成谱解析解回收 + engine 回显 ──────────────────────────────

def test_t59_powerlaw_recovery_and_engine_echo():
    nu, flux, sig = _pl_spec()
    fit = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    assert fit['engine'] == 'least_squares.trf'
    assert abs(fit['params']['beta'] / 0.8 - 1.0) < 0.05      # 相对差 < 5%
    assert abs(fit['params']['A'] / 0.5 - 1.0) < 0.05
    assert 0.7 < fit['chi2'] / fit['dof'] < 1.4
    assert fit['comparable'] and fit['grid_converged']        # 初始化质量在线
    assert fit['engine_status']['optimality'] <= C_OPTIMALITY_MAX
    assert not CT.compare_models(fit, fit)['reason']          # 自比不因引擎拒


def test_t59_bb_recovery_colour_branch():
    nu, _, _ = _pl_spec()
    rng = np.random.default_rng(SEED)
    flux = CT.MODEL_SPECS['bb']['fnu']({'T': 8000.0, 'RD': 5e-12}, nu, {})
    sig = flux / 20.0
    flux = flux + rng.normal(0.0, sig)
    fit = CT.fit_spectrum('bb', nu, flux, sig, err_seed=SEED)
    assert abs(fit['params']['T'] / 8000.0 - 1.0) < 0.05      # 相对差 < 5%
    assert abs(fit['params']['RD'] / 5e-12 - 1.0) < 0.10
    assert fit['T_semantics'] == 'colour'                     # 无 z：F-63


# ─── ② T-67：全有/全无截断 + 触界判不可信 ────────────────────────────────

def test_t67_rank_deficient_cov_is_all_or_nothing():
    # 秩亏 J（第 2 列 = 2×第 1 列）⇒ Σ 整体 null，无「编辑奇异值」后的有限 σ
    J = np.array([[1.0, 2.0], [2.0, 4.0], [3.0, 6.0]])
    s0, reason = CT._cov_from_jac(J, dof=1)
    assert s0 is None and reason == 'rank_deficient'
    # dof ≤ 0 ⇒ 同判（F-93③）
    s0, reason = CT._cov_from_jac(np.eye(3), dof=0)
    assert s0 is None and reason == 'dof_le_0'
    # 满秩对照：Σ 有限
    s0, reason = CT._cov_from_jac(np.array([[1.0, 0.2], [2.0, 1.0], [3.0, 0.4]]), dof=1)
    assert s0 is not None and reason is None


def test_t67_bound_hit_sigma_null_with_ca44():
    nu, flux, sig = _pl_spec(beta=-2.0)                       # 真值落在下界
    fit = CT.fit_spectrum('pl', nu, flux, sig, err_method='covariance',
                          err_seed=SEED, mask_hash='m1')
    assert fit['engine_status']['active_mask'][0] != 0        # beta 触下界
    assert fit['sigma_theta']['beta'] is None                 # 该 σ 判不可信
    assert fit['sigma_theta']['A'] is not None                # 未触界者照出
    assert fit['err_source']['beta'] == 'none'
    assert fit['err_low']['beta'] is None and fit['err_high']['beta'] is None
    codes = [w['code'] for w in fit['warnings']]
    assert 'CA-44' in codes


# ─── ③ F-28 剖面 vs F-95 自助：同线一致、方向与回落 ──────────────────────

def test_f28_profile_asymmetric_default():
    nu, flux, sig = _pl_spec()
    fit = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    assert fit['err_source'] == {'beta': 'profile', 'A': 'profile'}
    for k in ('beta', 'A'):
        assert fit['err_low'][k] is not None and fit['err_high'][k] is not None
    assert fit['err_low']['A'] != fit['err_high']['A']        # F-28：不对称区间


def test_f95_bootstrap_consistent_reproducible_seeded():
    nu, flux, sig = _pl_spec()
    prof = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    boot = CT.fit_spectrum('pl', nu, flux, sig, err_method='bootstrap',
                           n_boot=80, err_seed=SEED, mask_hash='m1')
    assert boot['err_source'] == {'beta': 'bootstrap', 'A': 'bootstrap'}
    assert boot['rng_algo'] == 'pcg64' and boot['err_seed'] == SEED
    for k in ('beta', 'A'):
        assert boot['err_low'][k] > 0 and boot['err_high'][k] > 0
        # 同线方向/量级一致：自助宽度与剖面宽度相对差 < 2 倍（容差）
        ratio = boot['err_high'][k] / prof['err_high'][k]
        assert 0.5 < ratio < 2.0
    # T-66（缩小域）：同 seed 逐字节相同；换 seed 自助变而剖面不变
    boot2 = CT.fit_spectrum('pl', nu, flux, sig, err_method='bootstrap',
                            n_boot=80, err_seed=SEED, mask_hash='m1')
    assert boot['err_low'] == boot2['err_low'] and boot['err_high'] == boot2['err_high']
    prof2 = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED + 1, mask_hash='m1')
    assert prof2['err_low'] == prof['err_low']                # 剖面与 seed 无关


def test_f28_profile_budget_fallback_to_bootstrap():
    nu, flux, sig = _pl_spec()
    fit = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED,
                          n_profile_points=300, mask_hash='m1')  # 600 重拟合 > 500
    assert fit['boot_budget_applied'] is True                 # F-28/CA-44⑦
    assert set(fit['err_source'].values()) == {'bootstrap'}
    assert any(w['code'] == 'CA-44' for w in fit['warnings'])


# ─── ④ F-29：嵌套 F 检验与非嵌套 BIC 分档 ────────────────────────────────

def test_f29_nested_pair_small_p():
    nu, flux, sig = _dusty_spec()                             # 真值 = pl_dust
    simple = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    rich = CT.fit_spectrum('pl_dust', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    out = CT.compare_models(simple, rich)
    assert out['test'] == 'f_test'
    assert out['p'] < C_FTEST_ALPHA                           # 嵌套真模型 ⇒ p 小
    assert out['verdict'] == 'rich' and out['F'] > 1.0


def test_f29_mask_mismatch_and_nonnested_bic():
    nu, flux, sig = _pl_spec()
    a = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    b = CT.fit_spectrum('pl_dust', nu, flux, sig, err_seed=SEED, mask_hash='m2')
    out = CT.compare_models(a, b)
    assert out['verdict'] == 'rejected' and 'mask_hash' in out['reason']
    bb = CT.fit_spectrum('bb', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    out = CT.compare_models(a, bb)                            # pl vs bb 非嵌套
    assert out['test'] == 'bic' and out['p'] is None
    assert out['verdict'] in ('rich', 'simple', 'inconclusive')
    if out['dbic'] is not None:
        assert abs(out['dbic']) >= C_BIC_MIN or out['verdict'] == 'inconclusive'


# ─── ⑤ F-62：poly 的 Chebyshev 基与 cond_2 降阶 ──────────────────────────

def _poly_data(n=300):
    rng = np.random.default_rng(SEED)
    lam = np.linspace(4000.0, 9000.0, n)
    u = 2.0 * (np.log(lam) - np.log(lam.min())) / (np.log(lam.max()) - np.log(lam.min())) - 1.0
    M = np.polynomial.chebyshev.chebvander(u, 2)
    sig = np.full(n, 0.01)
    return lam, M @ [0.3, -0.8, 0.5] + rng.normal(0.0, sig), sig


def test_f62_poly_recovers_and_echoes_basis():
    lam, flux, sig = _poly_data()
    fit = CT.fit_poly(lam, flux, sig, order=7)
    assert fit['poly_basis'] == 'cheb_lnlambda' and fit['cond_2'] < 10.0
    for k, truth in (('c0', 0.3), ('c1', -0.8), ('c2', 0.5)):
        assert abs(fit['params'][k] / truth - 1.0) < 0.05     # 相对差 < 5%
    assert fit['poly_order'] == 7 and fit['warnings'] == []
    assert fit['engine'] is None                              # 线性层不走 trf


def test_f62_order_reduction_and_rejection():
    lam5 = np.linspace(4000.0, 9000.0, 5)
    u5 = 2.0 * (np.log(lam5) - np.log(lam5.min())) / (np.log(lam5.max()) - np.log(lam5.min())) - 1.0
    f5 = np.polynomial.chebyshev.chebvander(u5, 3) @ [0.1, 0.2, 0.3, 0.1]
    fit = CT.fit_poly(lam5, f5, np.full(5, 0.01), order=7)    # m<n ⇒ cond 无穷
    assert fit['poly_order'] < 7                              # 自动降阶
    assert 'CA-20' in [w['code'] for w in fit['warnings']]
    with pytest.raises(CT.ContinuumError) as ei:              # 降到 1 阶仍超限 ⇒ 拒
        CT._poly_solve(np.ones((3, 2)), np.ones(3), 1, [])    # 秩 1 设计阵
    assert ei.value.code == 'CA-20'
    with pytest.raises(CT.ContinuumError) as ei:              # Q-15：阶数上限
        CT.fit_poly(lam5, f5, np.full(5, 0.01), order=8)
    assert ei.value.code == 'E-14'


# ─── ⑥ F-91⑦/Q-35：engine_status 九键与 err_seed 契约 ────────────────────

def test_q35_engine_status_nine_keys_and_seed_derivation():
    nu, flux, sig = _pl_spec()
    fit = CT.fit_spectrum('pl', nu, flux, sig, spec_hash='abc123456789', mask_hash='m1')
    assert set(fit['engine_status']) == ENGINE_KEYS
    assert fit['engine_status']['x_scale'] == 'jac'
    assert fit['err_seed'] == int('abc123456789', 16)         # Q-35 派生式
    assert fit['err_scope'] == {'beta': 'stat', 'A': 'stat'}  # F-93⑥
    for key in ('cov_method', 'rho_used', 'cond_G', 'n_boot', 'rng_algo', 'infl',
                'sigma_theta', 'sigma_theta_raw', 'amplitude_solution',
                'grid_converged', 'bic', 'err_semantics'):
        assert key in fit
    assert fit['amplitude_solution'] == 'closed_form_linear'  # F-27/F-91⑥
    with pytest.raises(CT.ContinuumError) as ei:
        CT.fit_spectrum('pl', nu, flux, sig, err_seed='x')    # 类型域
    assert ei.value.code == 'E-14'
    with pytest.raises(CT.ContinuumError) as ei:
        CT.fit_spectrum('pl', nu, flux, sig)                  # 无 seed 无 hash
    assert ei.value.code == 'E-14'


# ─── ⑦ T-59 源码级扫描：引擎唯一化与无部分截断 ───────────────────────────

def test_t59_source_scan_no_forbidden_engines():
    """T-59②：AST 级扫描调用点（docstring/注释中的「禁用词」不算调用点）。"""
    import ast
    import os
    src = open(os.path.join(os.path.dirname(CT.__file__), 'continuum.py'),
               encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
        assert name not in ('curve_fit', 'spline', 'matrix_rank', 'effective_rank'), name
        if name == 'least_squares':
            kws = {k.arg: k.value for k in node.keywords}
            if 'method' in kws:
                assert kws['method'].value == 'trf'          # F-91①②：唯一引擎形态
            assert 'loss' not in kws and 'f_scale' not in kws  # D-15：禁稳健损失
    assert "method='trf'" in src                              # 引擎形态在源内在场


# ─── ⑧ F-30：闭包三候选映射 ─────────────────────────────────────────────

def test_f30_closure_three_candidates():
    nu, flux, sig = _pl_spec()
    fit = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    cl = CT.closure_from_fit(fit)
    beta = fit['params']['beta']
    assert len(cl['cand']) == 3 and len(cl['p_indices']) == 3
    assert cl['p_indices'] == pytest.approx(
        [2 * beta, 2 * beta + 1.0, 2 * beta + 2.0], rel=1e-12)
    assert cl['p_err_lo'] == pytest.approx([2 * fit['err_low']['beta']] * 3, rel=1e-12)
    assert cl['p_err_hi'] == pytest.approx([2 * fit['err_high']['beta']] * 3, rel=1e-12)
    assert cl['err_source'] == 'delta_method' and cl['err_scope'] == 'stat'
    assert cl['factor_lo'] <= min(cl['p_indices']) and cl['factor_hi'] >= max(cl['p_indices'])
    assert len(cl['note']) > 0                                # 三候选是备选映射
    with pytest.raises(CT.ContinuumError):                    # 无 β 的模型不适用
        bb = CT.fit_spectrum('bb', nu, flux, sig, err_seed=SEED)
        CT.closure_from_fit(bb)


# ─── T-68：infl 只放大不缩小（F-93④ 哨兵） ───────────────────────────────

def test_t68_infl_only_inflates():
    nu, flux, sig = _pl_spec()
    base = CT.fit_spectrum('pl', nu, flux, sig, err_method='covariance',
                           err_seed=SEED)
    r0 = base['chi2'] / base['dof']                            # ≈1（定标用）
    flat = CT.fit_spectrum('pl', nu, flux, sig * (2.0 * r0 ** 0.5),
                           err_method='covariance', err_seed=SEED)   # ⇒ chi2/dof=0.25
    assert flat['chi2'] / flat['dof'] == pytest.approx(0.25, rel=1e-6)
    assert flat['infl'] == 1.0                                # 只放大不缩小
    for k in flat['sigma_theta']:
        assert flat['sigma_theta'][k] == pytest.approx(
            flat['sigma_theta_raw'][k], rel=1e-12)
    tight = CT.fit_spectrum('pl', nu, flux, sig * (0.5 * r0 ** 0.5),
                            err_method='covariance', err_seed=SEED)  # ⇒ chi2/dof=4
    assert tight['chi2'] / tight['dof'] == pytest.approx(4.0, rel=1e-6)
    assert tight['infl'] == pytest.approx(2.0, rel=1e-9)      # infl = sqrt(chi2/dof)
    for k in tight['sigma_theta']:
        assert tight['sigma_theta'][k] == pytest.approx(
            2.0 * tight['sigma_theta_raw'][k], rel=1e-9)
    assert tight['chi2'] is not None and tight['dof'] is not None  # 同屏回显
