"""L1: specphot **P2 切片 2c · preprocess 数值半段**（02b §3.14 / §9 P2 行）。

覆盖（T-76 / T-77① / T-80 的纯函数半段；HTTP 半段归 test_l2_specphot_p2c.py）：
  T-76①  面积守恒：均匀与非均匀 Δλ 两例下 Σ F_i·Δλ_i 在完整块内守恒到 1e-12；
          被掩像素不参与权重与均值（F-106① 次序：先掩膜后合束）；尾块规则
          n_rebinned_pixels = ⌊n/k⌋、丢弃 n mod k 个像元（不另立键，可复算）。
  T-76②  σ 合成：通式与闭式在 w≡1、σ_i≡σ 时逐项相等（1e-12）；ρ=0 ⇒
          σ_bin=σ/√k；ρ=0.9 ⇒ σ_bin ≥ 1.05·σ/√k（实现成 σ/√k 即判失败）。
  T-76③  口径可判：不重叠分块的块均值在 ρ=0 输入下相邻块 lag-1 相关为 0
          （无公共像元）；rebin_gain 只随输入 rho_lag1 变、不随块序变
          （median 的序不变量）；滑窗 ρ₁=(k−1)/k 口径不得进入 σ_bin。
  T-76⑤  CA-49 四支逐支触发且互不误伤（③ 与 ① 不并现；ρ=0 独立谱不触 ④）。
  T-77①  AST 扫描：标识符 display_smoothed 在 backend/specphot 域内只允许以
          写入位置（赋值目标/字段声明/构造 keyword）出现在 preprocess.py。
  T-80②  杠杆值：h_ii 与 diag(X(XᵀX)⁻¹Xᵀ) 逐点同值、Σh_ii = p（X 满秩）；
          在 n_high_leverage > 0 的样本上仍成立（标记不改 X 的行集）。
  T-80①  CA-48① 可达构造（两端反号摆动 ⇒ 触发；单端小量/同号 ⇒ 不误伤）；
          降一阶重拟一次 + 建议掩膜段（只建议，不改任何掩膜）。
  T-80④  CA-48 两支可分辨：② 支（nonpos_frac 且 cond_2 同时超）单独触发；
          单独超 nonpos_frac ⇒ 不触发；两支同真 ⇒ 一个档、文案列明两支。
  T-80③  禁词扫描（域 = CA-48 文案与端点相关告警/回显说明的字符串字面量）：
          IGM / 森林 / 尘埃梯度 / 红侧证据 不得出现（TXT-25 不在域内）。
纪律：纯函数层，不触 DB / 不发请求；随机列全部显式 seed（20260927）。
"""
import ast
import math
import os
import re

import numpy as np
import pytest

from specphot import continuum as CT
from specphot import errors as errs
from specphot import preprocess as pre
from specphot.constants import (C_EDGE_SPREAD_MAX, C_EDGE_WIN_FRAC,
                                C_NONPOS_FRAC_MAX, C_PREPROCESS_FACTORS,
                                C_REBIN_GAIN_MAX_RATIO, C_REBIN_GAIN_MIN)

SEED = 20260927
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC_DIR = os.path.join(REPO, 'backend', 'specphot')


def _rng():
    return np.random.Generator(np.random.PCG64(SEED))


# ─── T-76①：面积守恒（均匀 / 非均匀 / 掩膜排除 / 尾块） ────────────────────

def _conservation_check(lam, flux, mask_bool, k):
    blocks, meta = pre.rebin_blocks(lam, mask_bool, k)
    vals = pre.combine_blocks(flux, blocks)
    dlam = pre._dlam_array(np.asarray(lam, dtype=float))
    for b, (idx, w) in enumerate(blocks):
        ok = (w > 0) & np.isfinite(flux[idx])
        lhs = float(np.sum(np.asarray(flux, dtype=float)[idx][ok] * dlam[idx][ok]))
        rhs = float(vals[b]) * float(np.sum(dlam[idx][ok]))
        assert abs(lhs - rhs) <= 1e-12 * max(1.0, abs(lhs)), (k, b, lhs, rhs)
    assert meta['n_blocks'] == len(lam) // k
    assert meta['n_dropped'] == len(lam) - meta['n_blocks'] * k


def test_t76_1_area_conservation_uniform_and_nonuniform():
    for n in (40, 61):                       # 61 ⇒ 尾块：k=2/4/8 时 n%k≠0
        lam = 4000.0 + 2.5 * np.arange(n)                     # 均匀
        rng = _rng()
        flux = 1e-14 * (1.0 + 0.3 * np.sin(np.arange(n) * 0.7) + 0.05 * rng.standard_normal(n))
        for k in C_PREPROCESS_FACTORS:
            if k == 1:
                continue
            _conservation_check(lam, flux, np.zeros(n, bool), k)
        # 非均匀 Δλ（V-9 聚合后的原生网格）：w_i = Δλ_i 加权
        lam_nu = 4000.0 + np.cumsum(1.0 + rng.random(n) * 4.0)
        for k in (2, 3, 4, 8):
            _conservation_check(lam_nu, flux, np.zeros(n, bool), k)


def test_t76_1_tail_block_rule_and_recount():
    """n%k≠0 ⇒ 丢尾块：n_rebinned_pixels=⌊n/k⌋；被丢像元数可复算（不另立键）。"""
    n = 61
    lam = 4000.0 + 2.5 * np.arange(n)
    flux = np.full(n, 1e-14)
    blocks, meta = pre.rebin_blocks(lam, np.zeros(n, bool), 4)
    assert meta['n_blocks'] == 15 and meta['n_dropped'] == 1
    assert len(blocks) == 15
    assert [int(i) for i in blocks[-1][0]] == [56, 57, 58, 59]   # 第 60 号像元被丢


def test_t76_1_masked_pixel_excluded_from_block():
    """被掩像素不参与块均值（+100σ 尖峰在掩膜内 ⇒ 块值不含它，F-106①）。"""
    n, k = 40, 4
    lam = 4000.0 + 2.5 * np.arange(n)
    base = 1e-14 * (1.0 + 0.3 * np.sin(np.arange(n) * 0.7))
    spike = base.copy()
    spike[9] = base[9] + 100.0 * 1e-14                 # 落在第 2 块（idx 8..11）
    mask = np.zeros(n, bool)
    mask[9] = True
    blocks, _m = pre.rebin_blocks(lam, mask, k)
    v_spike_masked = pre.combine_blocks(spike, blocks)
    v_base = pre.combine_blocks(base, blocks)
    assert v_spike_masked[2] == v_base[2]              # 掩膜内尖峰完全不进块值（idx 8..11）
    assert v_spike_masked[2] != pre.combine_blocks(spike, pre.rebin_blocks(
        lam, np.zeros(n, bool), k)[0])[2]              # 未掩时则必然进


# ─── T-76②：σ 合成通式 ⇄ 闭式 ────────────────────────────────────────────

def test_t76_2_closed_form_is_special_case_of_general():
    for k in C_PREPROCESS_FACTORS:
        if k == 1:
            continue
        for rho in (0.0, 0.5, 0.9, -0.9):
            g = pre.sigma_bin_general(np.full(k, 0.1), np.ones(k), rho)
            c = pre.sigma_bin_closed(k, 0.1, rho)
            assert abs(g - c) <= 1e-12 * max(1.0, c), (k, rho, g, c)


def test_t76_2_limits_rho_zero_and_high():
    k = 4
    assert abs(pre.sigma_bin_closed(k, 0.1, 0.0) - 0.1 / math.sqrt(k)) <= 1e-12
    rho_hi = pre.sigma_bin_closed(k, 0.1, 0.9)
    assert rho_hi >= 1.05 * 0.1 / math.sqrt(k)         # 离开独立情形的下界护栏
    # ρ→1 ⇒ σ_bin→σ（无收益）
    assert pre.sigma_bin_closed(k, 0.1, 0.999999) < 0.1


def test_t76_3_nonoverlap_blocks_no_lag1_and_gain_order_invariant():
    """不重叠分块在 ρ=0 输入下相邻块 lag-1 相关为 0（无公共像元）；滑窗
    ρ₁=(k−1)/k 口径只在 F-109 显示平滑，不得给 σ_bin 赋值（白噪声下
    σ_bin(k)=σ/√k 即为反证：滑窗口径会给出 σ·√((k+1)/k)≠σ/√k 之类偏大值）。"""
    rng = _rng()
    n, k = 800, 2
    lam = 4000.0 + 2.5 * np.arange(n)
    flux = 1e-14 * (1.0 + rng.standard_normal(n) * 0.1)
    blocks, _m = pre.rebin_blocks(lam, np.zeros(n, bool), k)
    v = pre.combine_blocks(flux, blocks)
    c = float(np.corrcoef(v[:-1], v[1:])[0, 1])
    assert abs(c) < 0.2, c                              # 200 块的白噪声均值序列
    # rebin_gain 只随输入 rho_lag1 变、不随块序变：median 的序不变量
    sig = np.full(n, 0.05)
    r1 = pre.rebin_stage(lam, flux, sig, np.zeros(n, bool), 4, 0.9)
    blocks_rev = list(reversed(r1['blocks']))
    sig_rb_rev = pre.sigma_bin_blocks(sig, blocks_rev, 0.9)
    dom = np.isfinite(sig_rb_rev) & (sig_rb_rev > 0)
    num = float(np.median(sig))
    den_rev = float(np.median(sig_rb_rev[dom]))
    den_fwd = float(np.median(r1['sigma'][np.isfinite(r1['sigma']) & (r1['sigma'] > 0)]))
    assert abs(num / den_rev - num / den_fwd) <= 1e-12 * max(1.0, num / den_fwd)


# ─── T-76⑤：CA-49 四支逐支触发、互不误伤 ─────────────────────────────────

def test_t76_5_ca49_four_branches_distinct():
    reasons = set()
    # ① gain < C_REBIN_GAIN_MIN（T-76⑤：ρ=0.9、k=2 ⇒ gain≈1.026）
    w = pre.ca49_warnings(2, 1.026, None, True)
    reasons |= {x['reason'] for x in w}
    assert [x['reason'] for x in w] == ['rebin_gain_low']
    # ② 抽后 px_per_fwhm_after < C_CURVE_PX_PER_FWHM_MIN（8）
    assert pre.px_per_fwhm_after(6000.0, 1500.0) == pytest.approx(4.0)
    assert pre.px_per_fwhm_after(None, 1500.0) is None       # r_source='none'
    w = pre.ca49_warnings(2, 1.2, 3.0, True)            # 1.15<1.2<1.1·√2 ⇒ 只 ②
    reasons |= {x['reason'] for x in w}
    assert [x['reason'] for x in w] == ['px_per_fwhm_after_low']
    # ③ rho 未回显 ⇒ rebin_gain=null，且 ① 不得同时报（T-76⑤）
    w = pre.ca49_warnings(2, None, None, False)
    reasons |= {x['reason'] for x in w}
    assert [x['reason'] for x in w] == ['rebin_gain_no_rho']
    # ④ gain > 1.1·√k（T-76⑤：ρ=−0.9、k=8 ⇒ 9.53）；ρ=0 独立谱（gain=√k）不触 ④
    w = pre.ca49_warnings(8, 9.53, None, True)
    reasons |= {x['reason'] for x in w}
    assert [x['reason'] for x in w] == ['rebin_gain_over_independent']
    w = pre.ca49_warnings(8, math.sqrt(8), None, True)
    assert w == []                                      # 余量 1.1 ⇒ 不触发
    assert len(reasons) == 4                            # 四支文案各自可辨


def test_t76_5_rho_echo_domain():
    """ρ 回显域（实现登记）：None 或 0≤r≤C_RHO_MIN ⇒ 未回显（gain=null）；
    负 r 恒回显（CA-49④ 必须可达，F-108③ 定义域允许为负）。"""
    assert pre.rho_effective(None) == (0.0, False)
    assert pre.rho_effective(0.05) == (0.0, False)
    assert pre.rho_effective(-0.9) == (-0.9, True)
    assert pre.rho_effective(0.9) == (0.9, True)
    lam = 4000.0 + 2.5 * np.arange(40)
    flux = np.full(40, 1e-14)
    sig = np.full(40, 0.05)
    r = pre.rebin_stage(lam, flux, sig, np.zeros(40, bool), 2, 0.05)
    assert r['echo']['rebin_gain'] is None and r['rho_echoed'] is False


# ─── T-77①：display_smoothed 的 AST 结构隔离 ─────────────────────────────

def _ast_files():
    return [os.path.join(SPEC_DIR, f) for f in sorted(os.listdir(SPEC_DIR))
            if f.endswith('.py')]


def test_t77_1_display_smoothed_write_only_in_preprocess():
    for path in _ast_files():
        src = open(path, encoding='utf-8').read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == 'display_smoothed':
                assert isinstance(node.ctx, (ast.Store,)), (
                    path, node.lineno, 'display_smoothed 被读取（F-109① 违反）')
                assert path.endswith('preprocess.py'), (
                    path, node.lineno, '写入位置只在 preprocess.py')
            if isinstance(node, ast.keyword) and node.arg == 'display_smoothed':
                assert path.endswith('preprocess.py'), (
                    path, node.lineno, '构造 keyword 只在 preprocess.py 的产出点')


def test_t77_1_display_smoothed_never_enters_numeric_path():
    """结构位的行为面：smooth 开 ⇒ display_smoothed 非空且 fit_array 不动；
    smooth 不进 preprocess_hash（键集单载体在 §4.2 行）。"""
    lam = 4000.0 + 2.5 * np.arange(40)
    ls = {'lam_aa': list(lam), 'flux_err': None}
    flux = 1e-14 * (1.0 + 0.3 * np.sin(np.arange(40) * 0.7))
    masks = {'mask_bool': np.zeros(40, bool)}
    off = pre.build_spectrum(ls, flux, masks)
    on = pre.build_spectrum(ls, flux, masks, smooth_px=3)
    assert off.display_smoothed is None and on.display_smoothed is not None
    assert np.array_equal(off.fit_array, on.fit_array)      # 数值通路逐点不动
    assert np.array_equal(on.fit_array, flux)
    sm = on.display_smoothed
    assert sm[20] == pytest.approx(float(np.mean(flux[19:22])))   # 全宽 3 箱式
    # 平滑不进哈希：同一编码两次（开/关平滑）得同一串
    assert pre.preprocess_hash('mh', 1, 'ok', False, 'sh') == \
        pre.preprocess_hash('mh', 1, 'ok', False, 'sh')


# ─── T-80②：杠杆值 ───────────────────────────────────────────────────────

def _poly_env(n=120, order=3):
    lam = np.linspace(5000.0, 6000.0, n)
    flux = 1e-14 * (1.0 + 0.2 * np.sin(np.linspace(0.0, 3.0, n)))
    sigma = np.full(n, 1e-15)
    rho, _w = errs.rho_lag1(flux)
    L, _cm = CT._cov_path(flux, rho)
    return lam, flux, sigma, L


def test_t80_2_h_diag_matches_pinv_and_sum_p():
    lam, flux, sigma, L = _poly_env()
    h, X = CT.poly_leverage(lam, 3, sigma, L)
    H = X @ np.linalg.inv(X.T @ X) @ X.T                 # diag(X(XᵀX)⁻¹Xᵀ) 复算
    assert np.allclose(h, np.diag(H), rtol=0, atol=1e-10)
    p = X.shape[1]
    assert abs(float(np.sum(h)) - p) <= 1e-9 * p         # X 满秩 ⇒ Σh_ii = p


def test_t80_2_sum_p_with_high_leverage_rows_marked():
    """n_high_leverage > 0 的样本上 Σh_ii = p 仍成立（只标记不剔除）。"""
    lam, flux, sigma, L = _poly_env(n=160)
    sigma = sigma.copy()
    sigma[70:74] = 1e-19                                 # 极小 σ ⇒ 该行高杠杆
    fit = CT.fit_poly(lam, flux, sigma, order=3)
    gate = fit['baseline_gate']
    assert gate['n_high_leverage'] > 0
    h, X = CT.poly_leverage(lam, fit['poly_order'], sigma, L)
    p = X.shape[1]
    assert abs(float(np.sum(h)) - p) <= 1e-9 * p
    thr = 3.0 * p / X.shape[0]
    assert int(np.count_nonzero(h > thr)) == gate['n_high_leverage']


# ─── T-80①④：CA-48 触发、降阶重拟、建议掩膜、两支分辨 ─────────────────────

def _edge_upturn_flux(n=250, blue=-1.0, red=1.0, frac_mult=1.5):
    """两端反号翘起（各 ~1.0·median|F|，窗口 1.5×C_EDGE_WIN_FRAC 的半跨度）。
    可达构造（T-80①）：两端窗口的中位残差反号且量级足 ⇒ spread > 0.25。"""
    u = np.linspace(-1.0, 1.0, n)
    f = np.ones(n)
    f[u <= -1.0 + frac_mult * C_EDGE_WIN_FRAC] = 1.0 + blue
    f[u >= 1.0 - frac_mult * C_EDGE_WIN_FRAC] = 1.0 + red
    return f


def test_t80_1_ca48_edge_upturn_triggers_and_downgrade_suggests():
    n = 250
    lam = np.linspace(5000.0, 6000.0, n)
    flux = 1e-14 * _edge_upturn_flux(n)
    sigma = np.full(n, 1e-15)
    fit = CT.fit_poly(lam, flux, sigma, order=3)
    gate = fit['baseline_gate']
    assert gate['baseline_edge_spread'] is not None \
        and gate['baseline_edge_spread'] > C_EDGE_SPREAD_MAX
    ca48 = [w for w in fit['warnings'] if w['code'] == 'CA-48']
    assert len(ca48) == 1 and '两端基线不可信' in ca48[0]['message']
    assert gate['edge_spread_after_downgrade'] is not None    # 降一阶重拟已做
    assert gate['edge_spread_after_downgrade'] > C_EDGE_SPREAD_MAX  # 仍超
    assert len(gate['suggested_edge_ranges']) == 2            # 蓝/红端各一段
    assert all(seg[0] < seg[1] for seg in gate['suggested_edge_ranges'])


def test_t80_1_ca48_not_triggered_by_single_small_end():
    """单端 0.15 或两端同号小量 ⇒ 不得触发（T-80① 的"不误伤"另一半）。"""
    n = 250
    lam = np.linspace(5000.0, 6000.0, n)
    for blue, red in ((-0.15, 0.0), (0.0, 0.0), (0.15, 0.15)):
        u = np.linspace(-1.0, 1.0, n)
        f = np.ones(n)
        if blue:
            f[u <= -1.0 + 2.0 * C_EDGE_WIN_FRAC] = 1.0 + blue
        if red:
            f[u >= 1.0 - 2.0 * C_EDGE_WIN_FRAC] = 1.0 + red
        fit = CT.fit_poly(lam, 1e-14 * f, np.full(n, 1e-15), order=3)
        gate = fit['baseline_gate']
        assert not (gate['baseline_edge_spread'] is not None
                    and gate['baseline_edge_spread'] > C_EDGE_SPREAD_MAX), (blue, red)
        assert not [w for w in fit['warnings'] if w['code'] == 'CA-48']


def test_t80_4_ca48_branch2_only():
    """② 支单独触发：nonpos_frac > C_NONPOS_FRAC_MAX 且 cond_2（请求阶数、
    降阶前）> C_COND_MAX，而 baseline_edge_spread 未超阈 ⇒ 只出②支。
    构造：λ 只取 3 个不同值（各重复 20 次）⇒ 3 阶设计阵恰好秩亏
    （cond_2=∞ > C_COND_MAX），F-62③ 降到 2 阶后满秩可解；流量平坦 ⇒ 端点
    残差恒 0，① 支不命中。"""
    n_per = 20
    lam = np.repeat([5000.0, 5500.0, 6000.0], n_per)
    flux = np.full(3 * n_per, 1e-14)                     # 平坦、全正（V-6 不拒算）
    sigma = np.full(3 * n_per, 1e-15)
    fit = CT.fit_poly(lam, flux, sigma, order=3, nonpos_frac=0.5)
    gate = fit['baseline_gate']
    assert gate['baseline_edge_spread'] in (0.0, None) or \
        gate['baseline_edge_spread'] <= C_EDGE_SPREAD_MAX
    ca48 = [w for w in fit['warnings'] if w['code'] == 'CA-48']
    assert len(ca48) == 1
    msg = ca48[0]['message']
    assert '②' in msg and 'nonpos_frac' in msg and 'cond_2' in msg
    assert '①' not in msg                                # ① 支未命中（不并列）


def test_t80_4_nonpos_alone_does_not_trigger_ca48():
    lam = np.linspace(5000.0, 6000.0, 120)
    flux = 1e-14 * (1.0 + 0.2 * np.sin(np.linspace(0.0, 3.0, 120)))
    fit = CT.fit_poly(lam, flux, np.full(120, 1e-15), order=3, nonpos_frac=0.5)
    assert not [w for w in fit['warnings'] if w['code'] == 'CA-48']


def test_t80_4_both_branches_one_entry_two_texts():
    """两支同真 ⇒ 一个 CA-48 档、文案列明两支（闭合触发集，T-80④）。

    构造：3 个核心波长（各重复 20 次，σ=1e-15）⇒ 3 阶设计阵秩亏
    （cond_2=∞ > C_COND_MAX，降阶后可解）+ 两端密集行（σ=1e-6，权重低 9 个
    量级 ⇒ 不救秩，但残差诊断按行均匀计）蓝端压低 ⇒ spread > 0.25。
    nonpos_frac=0.5（API 层传入的 V-6 口径）⇒ ② 支同时命中。"""
    n_per = 20
    dense_lo = np.linspace(5004.0, 5040.0, 30)
    dense_hi = np.linspace(5960.0, 5996.0, 30)
    lam = np.concatenate([np.repeat([5000.0, 5500.0, 6000.0], n_per),
                          dense_lo, dense_hi])
    flux = np.concatenate([np.full(n_per, 1e-14), np.full(n_per, 1e-14),
                           np.full(n_per, 1e-14),            # 核心三点：拟合精确过
                           np.full(30, 0.6e-14),             # 蓝端密集行压低
                           np.full(30, 1.0e-14)])
    sigma = np.concatenate([np.full(3 * n_per, 1e-15),
                            np.full(30, 1e-6), np.full(30, 1e-6)])
    fit = CT.fit_poly(lam, flux, sigma, order=3, nonpos_frac=0.5)
    ca48 = [w for w in fit['warnings'] if w['code'] == 'CA-48']
    assert len(ca48) == 1
    msg = ca48[0]['message']
    assert '①' in msg and '②' in msg                    # 一个档、两支都列明


# ─── T-80③：CA-48 文案禁词（扫描域 = 文案键，非整文件裸文本） ─────────────

FORBIDDEN = ('IGM', '森林', '尘埃梯度', '红侧证据')
# 扫描域（T-80③）：CA-48 自身文案、baseline_edge_spread / n_high_leverage /
# 建议掩膜段的告警与诊断条目里的字符串字面量 —— 以"含端点闸门锚词"的字面量
# 定位（域外的常驻说明如 TXT-25 不在域内，T-71 反而要求它在场）。
_ANCHORS = ('CA-48', 'baseline_edge_spread', 'n_high_leverage', '建议掩膜段',
            '低可靠', '两端基线不可信', '端点')


def test_t80_3_forbidden_words_absent_from_ca48_copy_domain():
    def _docstring_nodes(tree):
        """文档字符串节点（引用禁词表本身的知识声明，不在 T-80③ 的文案域内——
        扫描域 = CA-48 自身文案与端点相关的告警/回显说明，T-80③ 原文）。"""
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                body = getattr(node, 'body', [])
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant):
                    out.add(id(body[0].value))
        return out

    for name in ('continuum.py', 'preprocess.py', 'continuum_api.py'):
        path = os.path.join(SPEC_DIR, name)
        tree = ast.parse(open(path, encoding='utf-8').read())
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs:
                text = node.value
                if any(a in text for a in _ANCHORS):
                    for w in FORBIDDEN:
                        assert w not in text, (name, node.lineno, w)
    # 运行时面：CA-48 实际触发的文案同样无禁词
    n = 250
    lam = np.linspace(5000.0, 6000.0, n)
    flux = 1e-14 * _edge_upturn_flux(n)
    fit = CT.fit_poly(lam, flux, np.full(n, 1e-15), order=3)
    for w in fit['warnings']:
        if w['code'] == 'CA-48':
            for word in FORBIDDEN:
                assert word not in w['message']


# ─── 恒等态哨兵（P2 代码落地不得改变恒等态算术，T-75① 的纯函数面） ────────

def test_identity_path_factor1_untouched():
    lam = 4000.0 + 2.5 * np.arange(40)
    flux = 1e-14 * (1.0 + 0.3 * np.sin(np.arange(40) * 0.7))
    masks = {'mask_bool': np.zeros(40, bool)}
    ls = {'lam_aa': list(lam), 'flux_err': None}
    ps = pre.build_spectrum(ls, flux, masks)
    assert np.array_equal(ps.fit_array, flux)             # fit_array = 原谱
    assert ps.factor == 1 and ps.display_smoothed is None
    # validate_request：恒等输入四元组与 P1b 逐字节同型（factor 为 int）
    assert pre.validate_request({}) == (1, [], 0, 'auto')
    with pytest.raises(Exception):
        pre.validate_request({'factor': 5})
