"""specphot S3 谱线测量——纯函数层（02 规格 §3.9 + §3.9.3，P3 切片 1）。

交付：选区与双掩膜（F-34…F-36/F-72）+ 基线（F-36，加性/乘性双路径，poly 按
F-62）+ 轮廓拟合（gauss1/gauss2/lorentz/voigt，ln λ 空间 F-70；voigt 属 P3b、
闸门后——全库无 R（r_source='none'，M-4 未完成）⇒ E-14 拒绝维持，F-39）+
EW 闭式三项分解（F-41/F-96）+ line_flux/depth（F-67）+ 生长曲线（F-71/CA-27）
+ snr_res 门（F-68）+ 自助对照（F-95②/F-96⑦/CA-44⑥）+ 天光发射线联合线性
扣除（F-86，U-31='subtract'，P3c 真解锁；CA-37 退回 mask）。API-4（POST /line）
由 lines_api.py 装配（P3 切片 2 上线，meta.py 的 501 占位已删除），本模块保持
纯函数、不接 API 路由。

纪律锚点：
  - 引擎唯一化（F-91）：一律 least_squares(method='trf', x_scale='jac',
    diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX)；禁 'lm'/curve_fit/loss=；
    engine_status 九键回显；协方差经 continuum._cov_from_jac 全有/全无（F-93，
    chi2=2·cost、infl 只放大不缩小）。
  - 联合基线（F-96⑥）：多项式基线系数与线参数进同一个 θ，禁止把基线当无误差
    的已知量——EW/depth 误差若不含基线项 ⇒ E-15（d 类，F-94③）。
  - EW 项分解（F-96②③）：ew_err_terms{photon, continuum, continuum_coherent}
    三键必须齐全；ew_err_form='two_term'；窗口因子 (Δλ−W) 由 EW 定义就地导出
    （δW = −(Δλ−W)·δC/C）；coherent/continuum > 3× ⇒ CA-44⑤（baseline.order、
    Δλ、W 进说明，TXT-23），禁止为消除它改公式或缩窗口而不声明。
  - 符号与空值（F-67）：内部恒算有符号 W_signed，对外 ew_obs_aa=|W_signed| 并
    同时回显 line_kind 与 ew_signed_aa；吸收线 line_flux 恒 null（写 0/负值都
    算实现错误），发射线 depth 恒 null，两者不得互填。
  - 宽度口径（F-39/F-70/TXT-8）：全库无 R ⇒ 宽度只作观测宽度 fwhm_obs_aa 报告
    （TXT-8 常驻脚注）；FWHM 在 u=lnλ 空间解出后乘 λ 换回 Å；EW/线流量积分
    恒在 λ 空间（Jacobian dλ=λ·du 显式在 dλ 数组上，禁把 u 空间面积当 EW）。
  - 速度族留位（F-38②/M-6）：线表帧未复核 ⇒ vel_shift_kms/vel_fwhm_kms/
    fwhm_intr_aa/z_fit 恒 null + 留位理由（E-14 line_frame_unverified 的接口
    位）；本模块不实现速度/红移反推（P3c）。
  - 预算（F-95③/Q-15）：自助重拟合 ≤ min(C_BOOT_N, C_BOOT_N_BUDGET 共享池，
    请求侧经 budget_left 传入余量)，超限回落 + boot_budget_applied + CA-44③；
    闭式 vs 自助差 > 2× ⇒ CA-44⑥ 且两个都列（T-65）。seed 契约同 Q-35
    （Generator(PCG64(seed))，rng_algo='pcg64'；err_seed null ⇒ spec_hash 派生）。
"""
import math

import numpy as np
import scipy
from scipy.optimize import least_squares

from . import errors as _errs                        # rho_lag1 / corr_inflation
from . import continuum as _ct                       # 引擎/协方差/白化复用
from .constants import (C_BASE_ITER, C_BASE_SIDE, C_BOOT_N, C_BOOT_N_BUDGET,
                        C_CLIP_NSIGMA, C_COND_MAX, C_DEGEN_RHO, C_DIFF_STEP,
                        C_DLAM_OVER_FWHM_MAX, C_LINE_SNR_MIN, C_LINE_WIN,
                        C_MAD_SCALE, C_MASK_ABS_TABLE, C_MASK_EMIS_TABLE,
                        C_MAX_COMPONENTS, C_NFEV_MAX, C_OPTIMALITY_MAX,
                        C_REL_NONLIN, C_SKY_RESID_FLOOR, C_SKY_SIDE)
from .continuum import (ENGINE, _cov_from_jac, _cov_path, _FTOL, _infl,
                        _posthoc_optimality, _resolve_seed, _whiten)

# F-71 的柱密度系数 N = K·W[mÅ]/(λ0²[Å]·f)：K = m_e c²/(π e²)（cgs）= 1.1296e12
# cm⁻¹，经 Å↔cm 换算（W_mÅ→1e-11 cm、λ0²→1e-16 cm²）恰乘 1e5 ⇒ 1.1296e17。
# 规格明文「就地导出、不抄二手数值」——tests 由 scipy.constants 逐项复算钉值。
N_COEF_CGS = 1.1296e17

_LINE_KINDS = ('emission', 'absorption')
_PROFILES = ('gauss1', 'gauss2', 'lorentz', 'voigt')  # voigt 闸门后（F-39/U-26）
_NPAR_OF = {'gauss1': 3, 'gauss2': 3, 'lorentz': 3, 'voigt': 4}
_FWHM_U_GAUSS = 2.0 * math.sqrt(2.0 * math.log(2.0))   # 2.3548（F-70）
_TXT8 = ('未提供仪器分辨率：宽度是观测宽度（含仪器卷积），不是本征线宽（TXT-8）')
_VEL_SLOT = ('P3c 留位：线表帧未经 M-6 复核，速度族/去卷积/红移反推禁用'
             '（F-38②/E-14 line_frame_unverified）')


class LineError(Exception):
    """S3 线测量侧拒绝（带 E-/CA- 码与 reason；结构对齐 SpecLoadError）。"""

    def __init__(self, code, reason, details=None):
        super().__init__(f'{code}: {reason}')
        self.code = code
        self.reason = reason
        self.details = details or {}


def _warn(code, message, **kw):
    w = {'code': code, 'message': message}
    w.update(kw)
    return w


# ─── 1. 选区与双掩膜（F-34/F-35/F-72；两类措辞不得混用，T-20） ────────────

def _segs_in(lo, hi, table):
    """与 [lo,hi] 相交的掩膜段（截到窗内）。"""
    out = []
    for row in table:
        a, b = max(float(row[0]), lo), min(float(row[1]), hi)
        if a < b:
            out.append((a, b, row[2]))
    return out


def _excluded_mask(lam, win):
    bad = np.zeros(np.asarray(lam).size, dtype=bool)
    for seg in win['excluded']:
        bad |= (lam >= seg['lo']) & (lam <= seg['hi'])
    return bad


def _window_mask(lam, win):
    """窗内、未掩、有限像素的布尔掩膜（线区拟合/积分像素集）。"""
    fin = np.isfinite(lam)
    ok = fin & (lam >= win['lo']) & (lam <= win['hi']) & ~_excluded_mask(lam, win)
    return ok


def select_window(lam, lam_center, line_kind, *, half_width=C_LINE_WIN,
                  mask_abs=None, mask_emis=None):
    """F-35 步 1 选区 + F-72 两类掩膜（处理方式不同、分开登记）。

    掩膜命中段的 reason 措辞钉死：'因大气吸收排除'（只能剔除，F-72①）/
    '因天光发射排除'（默认剔除；按线形扣除属 diagnostics 的 P3c 入口，F-72②）。
    F-72③：判定为发射线且线心落在大气吸收带内、或判定为吸收线且线心落在气辉
    线上 ⇒ E-14 拒绝该测量并写明理由（红移平移后的候选线同查——平移由调用方
    在观测系给；M-6 未复核前本模块不做 z 平移）。
    """
    if line_kind not in _LINE_KINDS:
        raise LineError('E-14', f'line_kind 必填且 ∈ {_LINE_KINDS}（F-66，无默认）',
                        {'reason': 'line_kind_required'})
    lam = np.asarray(lam, dtype=float)
    center = float(lam_center)
    lo, hi = center - float(half_width), center + float(half_width)
    if lo < float(lam.min()) or hi > float(lam.max()):
        raise LineError('E-04', 'too_few_pixels: 线窗越出谱覆盖',
                        {'window': [lo, hi]})
    m_abs = C_MASK_ABS_TABLE if mask_abs is None else mask_abs
    m_emis = C_MASK_EMIS_TABLE if mask_emis is None else mask_emis
    for row in m_abs:                                 # F-72③ 发射 × 吸收带
        if row[0] <= center <= row[1]:
            raise LineError('E-14', '发射线线心 %.2f Å 落在大气吸收带（%s）内，'
                            '拒绝该测量（F-72③）' % (center, row[2]),
                            {'reason': 'line_center_in_abs_band'})
    if line_kind == 'absorption':                     # F-72③ 吸收 × 气辉线
        for row in m_emis:
            if row[0] <= center <= row[1]:
                raise LineError('E-14', '吸收线线心 %.2f Å 落在天光发射线（%s）上，'
                                '拒绝该测量（F-72③）' % (center, row[2]),
                                {'reason': 'line_center_in_skyline'})
    # Q-20 vs F-72③ 张力登记（本切片不改行为，API-4 切片须显式调和）：
    # Q-20 规定拒测理由为单一 reason='mask_conflict'；F-72③/T-20 则要求两类
    # 掩膜（大气吸收带 / 天光发射线）措辞可分辨。本实现按 F-72③ 取双 reason
    # （line_center_in_abs_band / line_center_in_skyline），与 Q-20 的字面
    # 不一致——API-4 装配层对外暴露 reason 前必须显式调和（映射回
    # mask_conflict 或修订 Q-20），禁止两头都不提。
    excluded = [(a, b, '因大气吸收排除：%s' % name) for a, b, name
                in _segs_in(lo, hi, m_abs)]
    excluded += [(a, b, '因天光发射排除：%s' % name) for a, b, name
                 in _segs_in(lo, hi, m_emis)]
    excl_d = [{'lo': a, 'hi': b, 'reason': r} for a, b, r in excluded]
    n_win = int(np.count_nonzero(
        np.isfinite(lam) & (lam >= lo) & (lam <= hi)
        & ~_excluded_mask(lam, {'excluded': excl_d})))
    if n_win <= 0:
        # F-72③「发射×气辉不拒」的空窗支路：掩膜把整个线窗剔光 ⇒ 测量不可行，
        # 按 E-04 显式拒（too_few_pixels），不得让空窗流入积分层（P3 切片 2
        # 评审修复：此前 IndexError 于 _ew_terms 的 lw[-1]）。
        raise LineError('E-04', 'too_few_pixels: 线窗内无未掩膜的可用像素'
                        '（F-72 掩膜剔除后窗口为空）', {'window': [lo, hi]})
    return {'lo': lo, 'hi': hi, 'center': center, 'line_kind': line_kind,
            'excluded': excl_d, 'n_pix_win': n_win}


def _sideband_idx(lam, win, n_side=C_BASE_SIDE):
    """F-36：窗两侧各 n_side 个基线点（跳过掩膜/非有限像素，向外交替扫描）。

    方向纪律（P0 评审修正）：lo 边从窗外第一点向 −1 方向扫（窗**外**），
    hi 边向 +1 方向扫。此前 lo 边从 searchsorted 的窗内首点向 +1 扫，把
    侧带采进线窗内部、压在线翼上 ⇒ 粗采样+宽线下 EW 系统性偏小可达 −50%
    量级（评审实测 gauss FWHM=30 ⇒ −32.6%、lorentz ⇒ −44.9%），违反 F-36
    「线区两侧各 C_BASE_SIDE 点」与本文档 string 的「向外」承诺。
    """
    idx_all = np.flatnonzero(np.isfinite(lam))
    excl = _excluded_mask(lam, win)
    out = []
    # lo 边：searchsorted(side='left') 给窗内首点，−1 即窗外末点；hi 边：
    # searchsorted 本身就是窗外首点。两支都从窗沿**向外**步进。
    for j0, step in ((int(np.searchsorted(lam[idx_all], win['lo'])) - 1, -1),
                     (int(np.searchsorted(lam[idx_all], win['hi'])), +1)):
        got, j = [], j0
        while len(got) < n_side and 0 <= j < idx_all.size:
            i = int(idx_all[j])
            if np.isfinite(lam[i]) and not excl[i]:
                got.append(i)
            j += step
        if len(got) < n_side:
            raise LineError('E-04', 'too_few_pixels: 基线侧带点数不足（F-36）',
                            {'side_points': len(got), 'need': n_side})
        out.append(got)
    return out[0] + out[1]


# ─── 2. 基线（F-36：poly 按 F-62——Chebyshev + ln λ + cond_2 回显） ────────

def _axis_params(lam_region):
    ln = np.log(np.asarray(lam_region, dtype=float))
    a, b = float(ln.min()), float(ln.max() - ln.min())
    if b <= 0:
        raise LineError('E-04', 'too_few_pixels: ln λ 覆盖为零')
    return a, b


def _u_of(lam, axis):
    a, b = axis
    return 2.0 * (np.log(np.asarray(lam, dtype=float)) - a) / b - 1.0


def _lam_of_u(u, axis):
    a, b = axis
    return np.exp(a + (np.asarray(u, dtype=float) + 1.0) * b / 2.0)


def _cheb_design(u, order):
    return np.polynomial.chebyshev.chebvander(u, int(order))


def baseline_fit(lam, flux, sigma, side_idx, region_idx, order=3):
    """F-36 步 2：侧带 poly 基线（每侧 C_BASE_SIDE 点、C_BASE_ITER 次 σ 裁剪）。

    裁剪在加权残差 (F−C)/σ 上做稳健 σ 裁剪（尺度 s = C_MAD_SCALE·MAD、阈
    C_CLIP_NSIGMA；与 preprocess 离群候选同式，只服务初值与口径回显）。系数
    只是联合拟合的初值来源（F-91⑤）；cond_2 回显按 F-62③、线性层 lstsq(SVD)
    禁正规方程（F-62②）。返回 dict（coef/order/cond_2/poly_basis/axis/n_clip/
    active/ca20）。
    F-62③ 降阶：cond_2 > C_COND_MAX ⇒ 降一阶重拟并挂 CA-20，逐级降到 0 阶
    （常数）；0 阶仍超限 ⇒ 拒测 E-14（reason=baseline_cond_max；0 阶 Cheb
    设计矩阵是全 1 列、cond 恒 1，实际只有活动点不足/权重病态可达）。
    """
    lam = np.asarray(lam, dtype=float)
    f = np.asarray(flux, dtype=float)
    sg = np.asarray(sigma, dtype=float)
    side_idx = np.asarray(side_idx, dtype=int)
    region_idx = np.asarray(region_idx, dtype=int)
    axis = _axis_params(lam[region_idx])
    u_side = _u_of(lam[side_idx], axis)

    def _clip_fit(od):
        """给定阶数的 σ 裁剪 + 加权 lstsq + cond_2（F-62③ 线性层纪律）。"""
        active = np.ones(side_idx.size, dtype=bool)
        n_clip = 0
        for _ in range(C_BASE_ITER):                  # C_BASE_ITER 次迭代裁剪
            coef, *_ = np.linalg.lstsq(_cheb_design(u_side, od)[active],
                                       f[side_idx[active]] / sg[side_idx[active]],
                                       rcond=None)
            resid = f[side_idx] / sg[side_idx] \
                - _cheb_design(u_side, od) @ coef
            med = float(np.median(resid[active]))
            s = C_MAD_SCALE * float(np.median(np.abs(resid[active] - med)))
            if s <= 0:
                break
            newly = active & (np.abs(resid - med) > C_CLIP_NSIGMA * s)
            if not bool(np.any(newly)):
                break
            active &= ~newly
            n_clip += int(np.count_nonzero(newly))
        M_w = _cheb_design(u_side, od)[active] / sg[side_idx[active]][:, None]
        d_w = f[side_idx[active]] / sg[side_idx[active]]
        cond = (math.inf if M_w.shape[0] < M_w.shape[1]
                else float(np.linalg.cond(M_w)))      # F-62③ cond_2 回显
        coef, *_ = np.linalg.lstsq(M_w, d_w, rcond=None)
        return active, n_clip, coef, cond, M_w

    order_try = int(order)
    ca20 = []
    active, n_clip, coef, cond, M_w = _clip_fit(order_try)
    while cond > C_COND_MAX and order_try > 0:        # F-62③ 自动降阶 + CA-20
        ca20.append(_warn(
            'CA-20', 'cond_2=%.3g > C_COND_MAX=%s：基线 poly 自动降阶 '
            '%d→%d 并重拟（F-62③）' % (cond, C_COND_MAX, order_try,
                                       order_try - 1)))
        order_try -= 1
        active, n_clip, coef, cond, M_w = _clip_fit(order_try)
    if cond > C_COND_MAX:                             # 0 阶（常数）仍超 ⇒ 拒测
        raise LineError('E-14', 'cond_2=%.3g 降到 0 阶（常数基线）仍超 '
                        'C_COND_MAX=%s，拒该基线拟合（F-62③/E-14）'
                        % (cond, C_COND_MAX), {'reason': 'baseline_cond_max'})
    # 侧带安置协方差（F-96⑥ 括号支「分块求解后按 F-93 拼接」）：Σ_cc,side =
    # (M̃ᵀM̃)⁻¹·s²，s² = 侧带白化残差方差。EW/线流量的连续谱安置项用它而不是
    # 联合后验——基线安置由 F-36 的设计（每侧 C_BASE_SIDE 点）钉死，与线窗
    # 宽度无关，F-96③ 的窗口因子 (Δλ−W) 才得以如字面成立（T-65）；线参数
    # （λ/FWHM/depth）的误差仍走联合 Σ_θ（F-96⑥ 主支，含非对角）。全有/全无
    # 截断同 F-93②；侧带协方差不可得 ⇒ EW 基线项缺失（E-15 d 类的触发源）。
    dof_side = int(M_w.shape[0]) - M_w.shape[1]
    cov_side, cov_side_reason = _cov_from_jac(M_w, dof_side)
    # cov_side 保持 σ 列直信的确定值（(M̃ᵀM̃)⁻¹，F-41 的 σ_C 即 σ 列）：不放
    # 采样噪声进协方差（T-65 的窗口因子断言才可复现）；过离散由 CA-44⑥/⑤
    # 与联合拟合侧的 infl 纪律（F-93④）兜底，不在安置项里二次放大。
    return {'coef': coef, 'order': int(order_try), 'cond_2': cond,
            'poly_basis': 'cheb_lnlambda', 'axis': axis, 'n_clip': n_clip,
            'side_idx': np.asarray(side_idx, dtype=int),
            'active': np.asarray(active, dtype=bool), 'ca20': ca20,
            'cov': cov_side, 'cov_reason': cov_side_reason}


# ─── 3. 轮廓（F-37/F-70：u=lnλ 空间、峰归一；FWHM 只给观测宽度） ──────────

def _shape_gauss(u, u0, w):
    return np.exp(-0.5 * ((u - u0) / w) ** 2)


def _shape_lorentz(u, u0, w):
    return 1.0 / (1.0 + ((u - u0) / w) ** 2)


# ─── P3b · voigt（F-39/F-40：scipy.special 实现，禁 VoigtFit 本体） ─────────
# F-40 只禁 VoigtFit 本体集成（其 numpy<2 冲突与强制分辨率输入），不禁
# scipy.special.voigt_profile——scipy 是既有依赖（零新增，ST-6/ST-14）。
# 峰归一口径与 gauss/lorentz 同（峰高 1），使 (A,u0) 的语义三族一致；u=lnλ 空间
# 解出后乘 λ 换回 Å（F-70）。宽度参数：w=σ_u（高斯分量）、g=γ_u（洛伦兹分量的
# 半高半宽），θ 每成分 4 参（_NPAR_OF）。

def _voigt_profile(x, sigma, gamma):
    """峰归一 Voigt 轮廓 V(x;σ,γ)/V(0;σ,γ)（scipy.special.voigt_profile 是单位
    面积的 Voigt，除以线心值即峰归一）。纯函数；σ、γ 同量纲（这里 u=lnλ）。"""
    x = np.asarray(x, dtype=float)
    v = scipy.special.voigt_profile(x, float(sigma), float(gamma))
    v0 = scipy.special.voigt_profile(0.0, float(sigma), float(gamma))
    return v / v0


def _shape_voigt(u, u0, w, g):
    return _voigt_profile(np.asarray(u, dtype=float) - float(u0), w, g)


# Olivero & Longbothum 近似：FWHM ≈ 0.5346·fL + √(0.2166·fL² + fG²)
# （fL=2γ、fG=2.3548σ；精度 ~0.02%，数值性质由单测对 _voigt_profile 的
# 数值半高全宽逐点核对，不作为文献值外推——近似式属就地可导出的数值代数）。
_OL_A, _OL_B = 0.5346, 0.2166


def _voigt_fwhm_u(w, g):
    fL, fG = 2.0 * float(g), _FWHM_U_GAUSS * float(w)
    return _OL_A * fL + math.sqrt(_OL_B * fL * fL + fG * fG)


def _voigt_fwhm_u_grad(w, g):
    """∂FWHM_u/∂w、∂FWHM_u/∂g（O-L 式解析；∂/∂u0=0 由调用方处理）。"""
    fL, fG = 2.0 * float(g), _FWHM_U_GAUSS * float(w)
    root = math.sqrt(_OL_B * fL * fL + fG * fG)
    return _FWHM_U_GAUSS * fG / root, \
        2.0 * (_OL_A + _OL_B * fL / root)


def _profile_fn(profile):
    """返回 (shape_fn, FWHM_u 倍数)：gauss FWHM_u=2.3548σ_u、lorentz=2γ（F-70）。
    voigt 的 FWHM_u 依赖 (w,g) 两个参数，不是常数倍数 ⇒ 返回 mult=None，
    调用方（measure_line）走 _voigt_fwhm_u 分支。"""
    if profile == 'lorentz':
        return _shape_lorentz, 2.0
    if profile == 'voigt':
        return _shape_voigt, None
    return _shape_gauss, _FWHM_U_GAUSS


def _ew_analytic_gauss(u0, w, axis):
    """高斯线在 λ 空间的解析积分核 ∫shape·dλ（T-19 闭式对照件）：
    换元 v=u−u0、dλ=λ(u)du=λ0·(b/2)·e^{kv}dv（k=b/2）⇒
    ∫exp[−v²/2w²]·e^{kv}dv = √(2π)·w·exp(k²w²/2)（配方法；峰移 k·w²，
    宽度仍是归一域的 w）。返回 λ0·(b/2)·√(2π)·w·exp((k·w)²/2)。"""
    a, b = axis
    lam0 = math.exp(a + (float(u0) + 1.0) * b / 2.0)
    k = b / 2.0
    return lam0 * k * math.sqrt(2.0 * math.pi) * float(w) \
        * math.exp((k * float(w)) ** 2 / 2.0)


# ─── 4. 联合拟合（F-91/F-93：基线系数 + 线参数同一个 θ，F-96⑥） ───────────

def _seed_line(resid_u, u, du):
    """从基线扣除后的残差给线参数初值（峰位/半高全宽），只产出 x0（F-91⑤）。"""
    k = int(np.argmax(resid_u))
    a0 = float(resid_u[k])
    half = a0 / 2.0
    l = k
    while l > 0 and resid_u[l] > half:
        l -= 1
    r = k
    while r < resid_u.size - 1 and resid_u[r] > half:
        r += 1
    fwhm = max(float(u[r] - u[l]) * 0.5, 2.0 * du)    # 归一域半高全宽（下限兜底）
    return a0, float(u[k]), max(fwhm / 2.0, du / 4.0)   # γ 或 σ_u 档初值


def _bounds_and_x0(win, base, lam, flux, sigma, idx_fit, profile, axis):
    """θ 布局：基线系数在前、线参数 (A,u0,w[,g])×n_comp 在后（F-96⑥ 同一 θ；
    voigt 每成分多一个 γ_u=g，_NPAR_OF）。返回 (x0, (lb,ub), names, shape_fn,
    fwhm_mult)。"""
    shape_fn, mult = _profile_fn(profile)
    n_par = _NPAR_OF[profile]
    u = _u_of(lam[idx_fit], axis)
    f, sg = flux[idx_fit], sigma[idx_fit]
    coef0 = list(base['coef'])
    bl, bu = [], []
    # 基线系数给有限宽盒（初值 ±max(10·|c|,10)）：F-91 的事后最优性探针需要
    # 有限 span 才能定义可行方向；盒宽远超任何合理基线变动，不构成实质约束。
    for c in coef0:
        w_c = max(10.0 * abs(float(c)), 10.0)
        bl.append(float(c) - w_c)
        bu.append(float(c) + w_c)
    names = ['c%d' % k for k in range(len(coef0))]
    du = float(np.median(np.abs(np.diff(u)))) if u.size > 1 else 0.01
    u_lo, u_hi = float(_u_of(win['lo'], axis)), float(_u_of(win['hi'], axis))
    span = u_hi - u_lo
    C_fit = _cheb_design(u, base['order']) @ base['coef']
    if win['line_kind'] == 'emission':
        resid = np.where(np.isfinite(f - C_fit), f - C_fit, 0.0)
    else:
        with np.errstate(divide='ignore', invalid='ignore'):
            resid = np.where(np.isfinite(1.0 - f / C_fit), 1.0 - f / C_fit, 0.0)
    comps = [int(np.argmax(resid))]
    if profile == 'gauss2':                           # 第二成分：次峰（间隔≥3px）
        r = resid.copy()
        k1 = comps[0]
        r[max(0, k1 - 3):k1 + 4] = -np.inf
        k2 = int(np.argmax(r))
        comps.append(k2 if np.isfinite(r[k2]) and r[k2] > 0.1 * resid[k1]
                     else min(k1 + 3, u.size - 1))
    for m, k in enumerate(comps):
        a0, u0, w0 = _seed_line(resid, u, du)
        if m:
            a0 = max(0.2 * abs(resid[comps[0]]), du)
            u0 = float(u[k])
        a0 = max(abs(a0), 1e-12)
        hi_a = (max(3.0 * float(np.nanmax(np.abs(f))), 10.0 * a0)
                if win['line_kind'] == 'emission' else 1.0)   # 吸收 0≤r≤1（F-66）
        bl += [0.0, u_lo, du / 4.0]
        bu += [hi_a, u_hi, span]
        names += ['A%d' % m, 'u0%d' % m, 'w%d' % m]
        coef0 += [a0, u0, w0]
        if n_par == 4:                                # voigt：γ_u（洛伦兹分量）
            bl.append(du / 8.0)
            bu.append(span)
            names.append('g%d' % m)
            coef0.append(0.5 * w0)                    # 初值：洛伦兹分量取 σ 半值
    return np.array(coef0), (np.array(bl), np.array(bu)), names, shape_fn, mult


def _model(theta, base, u, win, shape_fn, n_comp, n_par=3):
    """发射加性 C+L（L≥0）/吸收乘性 C·[1−r]（0≤r≤1，F-66 边界进盒约束）。
    n_par：每成分线参数个数（voigt=4 含 γ_u，其余 3，_NPAR_OF）。"""
    nc = base['order'] + 1
    C = _cheb_design(u, base['order']) @ np.asarray(theta[:nc], dtype=float)
    prof = np.zeros_like(C)
    for m in range(n_comp):
        i = nc + n_par * m
        A, u0, w = theta[i], theta[i + 1], theta[i + 2]
        if n_par == 4:                                # voigt（_shape_voigt）
            prof = prof + float(A) * shape_fn(u, float(u0), float(w),
                                              float(theta[i + 3]))
        else:
            prof = prof + float(A) * shape_fn(u, float(u0), float(w))
    if win['line_kind'] == 'emission':
        return C + prof
    return C * (1.0 - prof)


def _fit_joint(lam, flux, sigma, idx_fit, base, x0, bounds, win, shape_fn,
               n_comp, axis, L, n_par=3):
    """least_squares trf 单引擎（F-91①）：白化残差上收敛；超限续跑至多 2 轮、
    只接受 cost 不升的解（与 continuum.fit_spectrum 同一迭代续跑纪律）。"""
    f, sg = flux[idx_fit], sigma[idx_fit]
    u = _u_of(lam[idx_fit], axis)

    def fun(xv):
        return _whiten((_model(xv, base, u, win, shape_fn, n_comp, n_par) - f)
                       / sg, L)

    lb, ub = bounds
    span = np.where(np.isfinite(ub) & np.isfinite(lb), ub - lb, 1.0)
    # margin 只防 trf 要求 x0 严格内点：基线与线参数在 _bounds_and_x0 里全是
    # 有限盒（基线 coef ±max(10|c|,10)、线参数物理盒），1e-9 的相对缩进不构成
    # 实质约束（旧注「无界 coef 侧 margin=0」已过时——coef 无界形态已废止）。
    margin = 1e-9 * span
    best = least_squares(fun, np.clip(x0, lb + margin, ub - margin),
                         bounds=(lb, ub), method='trf', x_scale='jac',
                         diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX,
                         ftol=_FTOL, xtol=_FTOL)
    nfev = int(best.nfev)
    for _polish in range(2):
        probe = (_posthoc_optimality(fun, best.x, lb, ub, best.active_mask)
                 if best.status != 0 else float(best.optimality))
        if probe <= C_OPTIMALITY_MAX or best.status == 0:
            break
        res = least_squares(fun, np.clip(best.x, lb + margin, ub - margin),
                            bounds=(lb, ub), method='trf', x_scale='jac',
                            diff_step=C_DIFF_STEP, max_nfev=C_NFEV_MAX,
                            ftol=_FTOL, xtol=_FTOL)
        nfev += int(res.nfev)
        if res.cost <= best.cost:
            best = res
        else:
            break
    optimality = (_posthoc_optimality(fun, best.x, lb, ub, best.active_mask)
                  if best.status != 0 else float(best.optimality))
    return best, fun, nfev, optimality


# ─── 5. EW/线流量闭式误差（F-41 两项 + F-96 三键分解） ────────────────────

def _grad(lam):
    lam = np.asarray(lam, dtype=float)
    g = np.zeros_like(lam)
    if lam.size >= 2:
        g[1:-1] = (lam[2:] - lam[:-2]) / 2.0
        g[0], g[-1] = lam[1] - lam[0], lam[-1] - lam[-2]
    return g


def _ew_terms(win, lam, flux, sigma, base, dlam):
    """F-41 闭式两项 + F-96③ 窗口因子（W = Σ(F/C−1)Δλ，λ 空间、Jacobian 显式
    在 dλ 数组上；禁把 u 空间面积当 EW，F-70）。

    photon：Σ Δλᵢ²(σᵢ/Cᵢ)²（F-41 第一项，计数噪声）。
    continuum：F-41 第二项的联合协方差正确算法（F-96②）——G_c=∂W/∂coef 经
    基线块 Σ_cc 传播（含非对角；「基线系数各自有误差」的逐像素项在求和下合
    并，相关结构不丢）。
    continuum_coherent：整体平移极限 |Δλ−W|·δC/C（F-96③，∫(F/C)dλ=Δλ−W 就地
    一行可导；δC/C 取线心处基线安置的相对不确定度）。
    """
    nc = base['order'] + 1
    widx = np.flatnonzero(_window_mask(lam, win))
    lw, fw, sgw = lam[widx], flux[widx], sigma[widx]
    uw = _u_of(lw, base['axis'])
    Mw = _cheb_design(uw, base['order'])
    Cw = Mw @ np.asarray(base['coef'], dtype=float)   # F-36 侧带基线（两步口径）
    dl = dlam[widx]
    with np.errstate(divide='ignore', invalid='ignore'):
        W = float(np.sum((fw / Cw - 1.0) * dl))       # 有符号 W_signed（F-67）
        photon2 = float(np.sum((dl * sgw / Cw) ** 2))
        G = -(dl * fw / Cw ** 2) @ Mw                 # ∂W/∂coef（连续谱项）
    dl_tot = float(lw[-1] - lw[0])
    scc = base.get('cov')                             # 侧带安置协方差（F-96⑥ 括号支）
    if scc is not None:
        cont = math.sqrt(max(float(G @ scc @ G), 0.0))
        u_c = 0.5 * (float(uw[0]) + float(uw[-1]))
        g_c = _cheb_design(np.array([u_c]), base['order'])[0]
        C_c = float(g_c @ np.asarray(base['coef'], dtype=float))
        s_c = math.sqrt(max(float(g_c @ scc @ g_c), 0.0))
        # 窗口因子：δW = −(δC/C)·∫(F/C)dλ = −(Δλ+W_signed)·δC/C（发射 ∫F/C=
        # Δλ+|W|、吸收 = Δλ−|W|；F-96③ 的「Δλ−W」按其 |W| 口径即此式）
        coherent = abs(dl_tot + W) * s_c / abs(C_c) if C_c != 0 else None
    else:
        cont, coherent = None, None
    return {'W_signed': W, 'dl_tot': dl_tot, 'n_win': int(widx.size),
            'photon': math.sqrt(max(photon2, 0.0)), 'continuum': cont,
            'continuum_coherent': coherent,
            'baseline_ok': scc is not None}


def _flux_terms(win, lam, flux, sigma, base, dlam):
    """发射线 line_flux = Σ(F−C)Δλ（加性基线，§3.9.1）及同构三项分解。"""
    nc = base['order'] + 1
    widx = np.flatnonzero(_window_mask(lam, win))
    lw, fw, sgw = lam[widx], flux[widx], sigma[widx]
    uw = _u_of(lw, base['axis'])
    Mw = _cheb_design(uw, base['order'])
    Cw = Mw @ np.asarray(base['coef'], dtype=float)   # F-36 侧带基线（同口径）
    dl = dlam[widx]
    S = float(np.sum((fw - Cw) * dl))
    photon = math.sqrt(max(float(np.sum((dl * sgw) ** 2)), 0.0))
    G = -np.sum(dl[:, None] * Mw, axis=0)             # ∂S/∂coef = −∫∂C/∂c dλ
    scc = base.get('cov')
    if scc is not None:
        cont = math.sqrt(max(float(G @ scc @ G), 0.0))
        u_c = 0.5 * (float(uw[0]) + float(uw[-1]))
        g_c = _cheb_design(np.array([u_c]), base['order'])[0]
        s_c = math.sqrt(max(float(g_c @ scc @ g_c), 0.0))
        coherent = float(lw[-1] - lw[0]) * s_c        # 加性：Δλ·δC
    else:
        cont, coherent = None, None
    return {'line_flux': S, 'photon': photon, 'continuum': cont,
            'continuum_coherent': coherent}


def _depth_of(theta, base, shape_fn, n_comp, n_par=3):
    """吸收线 depth = 第一成分线心处的 r（模型比值量；§3.9.1 depth=1−F/C）。"""
    nc = base['order'] + 1
    u01 = float(theta[nc + 1])
    r = 0.0
    for m in range(n_comp):
        i = nc + n_par * m
        A, u0, w = theta[i], theta[i + 1], theta[i + 2]
        if n_par == 4:
            r += float(A) * float(shape_fn(np.array([u01]), float(u0),
                                           float(w), float(theta[i + 3]))[0])
        else:
            r += float(A) * float(shape_fn(np.array([u01]), float(u0),
                                           float(w))[0])
    return r


def _depth_grad(theta, base, shape_fn, n_comp, n_par=3):
    """∂depth/∂θ。gauss/lorentz 解析（shape 对 A/u0/w 就地可导，F-95①）；
    voigt 的 ∂V/∂(u0,w,g) 无简单闭式 ⇒ F-95① 明文允许有限差分（中心差分，
    步长 C_FD_REL·|θ|），调用方回显 depth_err_delta='fd'。

    死代码声明（P2 评审）：depth 在第一成分线心取值，n_comp=1（gauss1/lorentz
    的唯一可达形态）时 u01≡u0 ⇒ x≡0 ⇒ 交叉项 g[u0]/g[w] 恒为 0——交叉项仅
    为 gauss2-吸收（n_comp=2）的一般性保留。gauss 与 lorentz 的导数式不同
    （原实现把高斯式误用于洛伦兹，x≡0 下数值无差故未暴露，此处一并修正）：
      gauss   ∂s/∂u0 = s·x/w、    ∂s/∂w = s·x²/w
      lorentz ∂s/∂u0 = 2·s²·x/w、 ∂s/∂w = 2·s²·x²/w（s=1/(1+x²) 就地可导）
    """
    g = np.zeros(np.asarray(theta).size)
    nc = base['order'] + 1
    u01 = float(theta[nc + 1])
    if n_par == 4:                                    # voigt：F-95① fd 支
        from .constants import C_FD_REL
        for k in range(nc, theta.size):
            d = C_FD_REL * max(abs(float(theta[k])), 1e-12)
            tp = np.asarray(theta, dtype=float).copy()
            tm = tp.copy()
            tp[k] += d
            tm[k] -= d
            g[k] = (_depth_of(tp, base, shape_fn, n_comp, n_par)
                    - _depth_of(tm, base, shape_fn, n_comp, n_par)) / (2.0 * d)
        return g
    is_lorentz = shape_fn is _shape_lorentz
    for m in range(n_comp):
        i = nc + 3 * m
        A, u0, w = float(theta[i]), float(theta[i + 1]), float(theta[i + 2])
        x = (u01 - u0) / w
        s = float(shape_fn(np.array([u01]), u0, w)[0])
        g[i] = s
        if w > 0 and s > 0:
            if is_lorentz:
                g[i + 1] = 2.0 * A * s * s * x / w
                g[i + 2] = 2.0 * A * s * s * x * x / w
            else:
                g[i + 1] = A * s * x / w
                g[i + 2] = A * s * x * x / w
    return g


def _closed_vs_boot(closed, boot_lo, boot_hi):
    """F-96⑦/T-65：闭式 vs 自助相差 > 2× ⇒ 触发文案（两值由调用方并排回显）。"""
    if closed is None or boot_lo is None or boot_hi is None:
        return None
    boot = 0.5 * (float(boot_hi) - float(boot_lo))
    if closed <= 0 or boot <= 0:
        return None
    ratio = max(closed, boot) / min(closed, boot)
    if ratio > 2.0:
        return ('闭式 σ_W=%.4g 与自助分位半宽 %.4g 相差 %.1f×（>2×，F-96⑦）：'
                '两个都列，误差不可按单一 1σ 解读（CA-44⑥）'
                % (closed, boot, ratio))
    return None


# ─── 6. 自助对照（F-95②：白化残差参数化重抽 + 重拟合） ────────────────────

def _bootstrap(win, lam, flux, sigma, idx_fit, base, theta, shape_fn, n_comp,
               axis, L, n_boot, seed, bounds, n_par=3):
    """参数化自助：flux* = m(θ̂) + σ·(L·ε*)，单起点重拟（初值 θ̂，F-91 同一引擎
    形态与同一盒），逐次重算 |W_signed|/line_flux/depth。返回分位数输入数组。"""
    f, sg = flux[idx_fit], sigma[idx_fit]
    u = _u_of(lam[idx_fit], axis)
    m_hat = _model(theta, base, u, win, shape_fn, n_comp, n_par)
    lb, ub = bounds                                    # 与主拟合同盒（F-91②）
    dlam = _grad(lam)
    rng = np.random.Generator(np.random.PCG64(int(seed)))
    span = np.where(np.isfinite(ub) & np.isfinite(lb), ub - lb, 1.0)
    margin = 1e-9 * span
    side_idx = np.asarray(base['side_idx'], dtype=int)
    side_loc = np.searchsorted(idx_fit, side_idx)     # 全长索引 → 拟合区内局部
    # 复用主基线 σ-裁剪的 active 集（P2 评审，与闭式同轴）：闭式 base['coef']
    # 是在 active 行上解出的，自助复本若用全侧带点重解，两法对照（F-96⑦）会
    # 混入「活动集不同」的口径差。active 缺失（外来 base）⇒ 退回全点。
    act = np.asarray(base.get('active',
                              np.ones(side_idx.size, dtype=bool)), dtype=bool)
    M_side = _cheb_design(_u_of(lam[side_idx], axis), base['order']) \
        / sigma[side_idx][:, None]
    w_out, s_out, d_out = [], [], []
    for _k in range(int(n_boot)):
        eps = rng.standard_normal(idx_fit.size)
        fstar = m_hat + sg * (L @ eps if L is not None else eps)
        fstar_full = np.asarray(flux, dtype=float).copy()
        fstar_full[idx_fit] = fstar                   # 复本铺回全长再取窗
        # 复本内两步口径（F-36）：W*/line_flux* 用复本侧带重拟的基线（与闭式
        # 同一估计量同一 active 集，F-96⑦ 的两法对照才同轴）；depth* 用联合
        # 重拟（比值量）。
        coef_star, *_ = np.linalg.lstsq(
            M_side[act], fstar[side_loc[act]] / sigma[side_idx[act]],
            rcond=None)
        base_star = dict(base, coef=coef_star)
        res = least_squares(
            lambda xv: _whiten((_model(xv, base, u, win, shape_fn, n_comp,
                                       n_par) - fstar) / sg, L),
            np.clip(theta, lb + margin, ub - margin), bounds=(lb, ub),
            method='trf', x_scale='jac', diff_step=C_DIFF_STEP,
            max_nfev=C_NFEV_MAX, ftol=_FTOL, xtol=_FTOL)
        terms = _ew_terms(win, lam, fstar_full, sigma, base_star, dlam)
        w_out.append(abs(terms['W_signed']))
        if win['line_kind'] == 'emission':
            s_out.append(_flux_terms(win, lam, fstar_full, sigma, base_star,
                                     dlam)['line_flux'])
        else:
            d_out.append(_depth_of(res.x, base, shape_fn, n_comp, n_par))
    return {'ew': np.array(w_out), 'line_flux': np.array(s_out),
            'depth': np.array(d_out), 'n_used': len(w_out)}


# ─── 7. snr_res（F-68：每分辨率元信噪，非逐像素、非线峰） ─────────────────

def snr_res(win, lam, sigma, base, r_resolution=None):
    """SNR_res = C / (σ_C,pix/√n_pix_per_res)。分辨率元宽取仪器 FWHM（无 R 时
    按 C_DLAM_OVER_FWHM_MAX 的等效值 ⇒ n_pix_per_res=C_DLAM_OVER_FWHM_MAX）；
    σ_C,pix = 线窗内逐像素连续谱噪声（§3.5 的 σ 列）中位数。回显 snr_def。"""
    widx = np.flatnonzero(_window_mask(lam, win))
    sg_c = float(np.median(np.asarray(sigma, dtype=float)[widx]))
    u_w = _u_of(lam[widx], base['axis'])
    C = float(np.median(_cheb_design(u_w, base['order'])
                        @ np.asarray(base['coef'], dtype=float)))
    if r_resolution:
        fwhm_inst = float(win['center']) / float(r_resolution)
        n_res = max(1.0, fwhm_inst / float(np.median(_grad(lam[widx]))))
        src = 'instrument_fwhm'
    else:
        n_res = float(C_DLAM_OVER_FWHM_MAX)
        src = 'no_r_equivalent'
    snr = C / (sg_c / math.sqrt(n_res)) if sg_c > 0 else math.inf
    return {'snr_res': float(snr), 'n_pix_per_res': n_res, 'snr_def': src,
            'C_continuum': C, 'sigma_c_pix': sg_c}


# ─── 7b. 天光发射线联合线性扣除（F-86，U-31='subtract' 的实现路径；P3c） ────

def sky_subtract(lam, flux, sigma, win):
    """F-86：把线窗内命中的天光发射段（C_MASK_EMIS_TABLE，[O I] 气辉三线）的
    轮廓当作已知形状，以 C_SKY_SIDE 侧带定局部连续谱，连续谱基 + 各天光线幅值
    在**同一次**加权线性最小二乘内一起解（「网格 × 线性闭式 LS」范式同 R-7：
    形状参数（峰位/FWHM）由该谱的天光线残差实测钉死——与 F-69 的
    sky_emission_fwhm「用同一条谱的天光发射线实测宽度」同一口径，只有幅值进
    线性解；线性层 lstsq(SVD)，禁正规方程，F-62②），扣完再进线积分。

    线区残差 RMS 未降到扣除前的 C_SKY_RESID_FLOOR 倍以下 ⇒ 自动退回 mask
    （win 原样返回）+ reverted=True，调用方挂 CA-37（不得给出「扣过了」的假象）。
    大气吸收带段不在本通路（永远 mask，F-72①）；F-72③ 的线心落带拒绝在
    select_window 已先行生效，不受本函数影响；禁止用扣除结果反推源的线流量
    （扣除后的 flux 只服务本条线的测量通路，not_in_budget[] 计入
    sky_subtraction，F-58/F-72②）。

    返回 (flux_new, win_new, info)；输入数组不被修改（RO 纪律）。"""
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    emis_segs = [s for s in win['excluded'] if '天光发射' in s['reason']]
    info = {'attempted': True, 'sky_subtracted': False, 'reverted': False,
            'n_sky_lines': 0, 'rms_before': None, 'rms_after': None,
            'rms_ratio': None, 'sky_lines': [], 'note': None}
    if not emis_segs:
        info['reverted'] = True
        info['note'] = ('线窗内无天光发射段命中：无扣除对象，退回 mask'
                        '（F-86/CA-37）')
        return flux, win, info
    lo_e, hi_e = win['lo'] - C_SKY_SIDE, win['hi'] + C_SKY_SIDE
    mE = np.isfinite(lam) & np.isfinite(flux) & (lam >= lo_e) & (lam <= hi_e)
    # 大气吸收带内的像素不得进设计矩阵（telluric 吸收会污染局部连续谱，F-72①）
    for a, b, _n in _segs_in(lo_e, hi_e, C_MASK_ABS_TABLE):
        mE &= ~((lam >= a) & (lam <= b))
    if int(np.count_nonzero(mE)) < 12:
        info['reverted'] = True
        info['note'] = '扩展区可用像素不足，无法定局部连续谱，退回 mask'
        return flux, win, info
    lamE, fE, sE = lam[mE], flux[mE], sigma[mE]
    in_any = np.zeros(mE.sum(), dtype=bool)
    for seg in emis_segs:
        in_any |= (lamE >= seg['lo']) & (lamE <= seg['hi'])
    out_any = ~in_any
    if int(np.count_nonzero(out_any)) < 6:
        info['reverted'] = True
        info['note'] = '侧带无可用像素，无法定局部连续谱，退回 mask'
        return flux, win, info
    # 粗连续谱：侧带像素的线性最小二乘（初定峰用）；σ 裁剪防天光线翼泄漏
    keep = out_any.copy()
    for _ in range(2):
        M = np.column_stack([np.ones(int(np.count_nonzero(keep))),
                             (lamE[keep] - float(np.mean(lamE[out_any])))
                             / max(C_SKY_SIDE, 1.0)])
        coef0, *_ = np.linalg.lstsq(M / sE[keep][:, None], fE[keep]
                                    / sE[keep], rcond=None)
        r_all = fE - (coef0[0] + coef0[1] * (lamE - float(np.mean(lamE[out_any])))
                      / max(C_SKY_SIDE, 1.0))
        med = float(np.median(r_all[keep]))
        s_n = max(C_MAD_SCALE * float(np.median(np.abs(r_all[keep] - med))),
                  1e-30)
        newly = keep & (np.abs(r_all - med) > C_CLIP_NSIGMA * s_n) & out_any
        if not bool(np.any(newly)):
            break
        keep &= ~newly
    # 候选天光线（实现裁量，docs/TECHNICAL.md 登记）：锚在 C_MASK_EMIS_TABLE 的
    # **名义中心**（表行中点，[O I] 三线位置已知）±3 Å 内从数据细化——不做全域
    # 自由峰检索，否则科学线自身的峰会被误当天光线吸进扣除模型（禁用扣除结果
    # 反推源的线流量，F-86 末句）。与科学线心 <5 Å 的天光线跳过（源/天光同位
    # 不可分，该段保守保留 mask）。每峰实测 FWHM（半高交点，与 F-69 的
    # sky_emission_fwhm 同口径）；无显著峰（>4σ）的段不扣除、保留 mask。
    sig_n = max(C_MAD_SCALE * float(np.median(
        np.abs(r_all[out_any] - float(np.median(r_all[out_any]))))), 1e-30)
    d_lam = float(np.median(np.abs(np.diff(lamE))))
    shapes, segs_hit, segs_kept = [], [], []
    for a, b, name in _segs_in(lo_e, hi_e, C_MASK_EMIS_TABLE):
        cen0 = 0.5 * (a + b)
        seg_d = {'lo': a, 'hi': b, 'reason': '因天光发射排除：%s' % name}
        if abs(cen0 - win['center']) < 5.0:
            segs_kept.append(seg_d)                   # 源/天光同位，保守 mask
            continue
        m_c = (lamE >= cen0 - 3.0) & (lamE <= cen0 + 3.0)
        if int(np.count_nonzero(m_c)) < 5:
            segs_kept.append(seg_d)
            continue
        jj = np.flatnonzero(m_c)
        j = int(jj[np.argmax(r_all[jj])])
        if r_all[j] < 4.0 * sig_n or r_all[j] < r_all[j - 1]                 or r_all[j] < r_all[j + 1]:
            segs_kept.append(seg_d)                   # 无显著峰：保留 mask
            continue
        den = r_all[j - 1] - 2.0 * r_all[j] + r_all[j + 1]
        frac = 0.5 * (r_all[j - 1] - r_all[j + 1]) / den if den < -1e-30 else 0.0
        cen = float(lamE[j] + max(min(frac, 1.0), -1.0)
                    * (lamE[j + 1] - lamE[j - 1]) / 2.0)
        half = r_all[j] / 2.0
        l = j
        while l > 0 and r_all[l] > half:
            l -= 1
        r_ = j
        while r_ < r_all.size - 1 and r_all[r_] > half:
            r_ += 1
        fwhm = float(min(max(lamE[r_] - lamE[l], 2.0 * d_lam), 30.0))
        shapes.append((cen, fwhm))
        segs_hit.append(seg_d)
    if not shapes:
        info['reverted'] = True
        info['note'] = ('天光段内未检出显著发射峰（>4σ）或与科学线心同位：无扣除'
                        '对象，退回 mask（F-86/CA-37）')
        return flux, win, info
    lc = float(np.mean(lamE[out_any]))
    M = np.column_stack(
        [np.ones(lamE.size), (lamE - lc) / max(C_SKY_SIDE, 1.0)]
        + [np.exp(-4.0 * math.log(2.0) * ((lamE - c) / fw) ** 2)
           for c, fw in shapes])
    coef, *_ = np.linalg.lstsq(M / sE[:, None], fE / sE, rcond=None)
    cont = coef[0] + coef[1] * (lamE - lc) / max(C_SKY_SIDE, 1.0)
    sky = np.zeros_like(cont)
    for k, (c, fw) in enumerate(shapes):
        sky += coef[2 + k] * np.exp(-4.0 * math.log(2.0) * ((lamE - c) / fw) ** 2)
    # CA-37 判据：被扣除段核内残差 RMS（扣除连续谱后）须降到 0.7× 以下
    core = np.zeros(lamE.size, dtype=bool)
    for seg in segs_hit:
        core |= (lamE >= seg['lo']) & (lamE <= seg['hi'])
    rms_b = float(np.sqrt(np.mean((fE[core] - cont[core]) ** 2))) \
        if bool(np.any(core)) else None
    rms_a = float(np.sqrt(np.mean((fE[core] - cont[core] - sky[core]) ** 2))) \
        if bool(np.any(core)) else None
    info['n_sky_lines'] = len(shapes)
    info['rms_before'], info['rms_after'] = rms_b, rms_a
    info['rms_ratio'] = (rms_a / rms_b) if (rms_b and rms_a is not None
                                            and rms_b > 0) else None
    info['sky_lines'] = [{'lambda_aa': c, 'fwhm_aa': fw,
                          'amp': float(coef[2 + k])}
                         for k, (c, fw) in enumerate(shapes)]
    if rms_b is None or rms_a is None or not rms_b > 0 \
            or rms_a > C_SKY_RESID_FLOOR * rms_b:
        info['reverted'] = True
        info['note'] = ('残差 RMS 未降到 %.2f× 以下（ratio=%s）：自动退回 mask'
                        '（F-86/CA-37，不得称「已扣除」）'
                        % (C_SKY_RESID_FLOOR,
                           None if info['rms_ratio'] is None
                           else '%.3f' % info['rms_ratio']))
        return flux, win, info
    flux_new = np.array(flux, dtype=float)            # 只读纪律：不改输入数组
    mE_full = np.flatnonzero(mE)
    flux_new[mE_full] = fE - sky
    hit_names = {s['reason'] for s in segs_hit}
    win_new = dict(win, excluded=[s for s in win['excluded']
                                  if s['reason'] not in hit_names])
    win_new['sky_segs_kept'] = segs_kept              # 未扣除段仍在 mask（可辨）
    widx2 = np.flatnonzero(_window_mask(lam, win_new))
    win_new['n_pix_win'] = int(widx2.size)
    info['sky_subtracted'] = True
    info['note'] = ('天光发射线按线形扣除成功（n=%d，RMS %.3f→%.3f）；'
                    'sky_subtraction 已计入 not_in_budget（F-58/F-72②）'
                    % (len(shapes), rms_b, rms_a))
    return flux_new, win_new, info


# ─── 8. 主入口 measure_line（F-34…F-42 + §3.9.3 全量落 S3） ───────────────

def measure_line(lam, flux, sigma, lam_center, line_kind, *, profile='gauss1',
                 baseline_order=1, half_width=C_LINE_WIN, z=0.0,
                 r_resolution=None, n_boot=C_BOOT_N, budget_left=None,
                 err_seed=None, spec_hash=None, mask_abs=None, mask_emis=None,
                 sky_handling='mask'):
    """单条线测量唯一入口（S3 三步向导的计算核；纯函数、只读输入）。

    返回 §3.9.3.1 线族行的 §4 键子集：lambda_obs_vac_aa / fwhm_obs_aa /
    ew_obs_aa / ew_signed_aa / ew_rest_aa / line_flux（发射）/ depth（吸收）/
    误差键与 ew_err_terms 三键 / err_source{} / err_scope{} / engine_status /
    snr_res / upper_limit_3sigma / warnings[]。速度族键恒 null + 留位理由
    （M-6，P3c）。n_boot/budget_left：C_BOOT_N_BUDGET 共享池的请求侧接口
    （装配层传余量；超限回落 + CA-44③ + boot_budget_applied）。
    sky_handling（U-31/F-86）：'mask'（默认，天光发射段剔除）| 'subtract'
    （P3c 的 C_SKY_SIDE 侧带 + 联合线性扣除，失败自动退回 mask + CA-37；
    大气吸收带永远只 mask，F-72①）。
    """
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    if lam.size != flux.size or lam.size != sigma.size:
        raise LineError('E-14', 'lam/flux/sigma 长度不一致')
    if profile not in _PROFILES:
        raise LineError('E-14', 'profile ∈ %s（U-26 词表）' % (_PROFILES,))
    # F-39 闸（P3b）：voigt 只在仪器分辨率可得时开放——全库无 R（r_source='none'
    # 的库内现状，M-4 未完成）⇒ 拒绝维持，宽度只能作观测宽度报告（TXT-8/CA-08）。
    if profile == 'voigt' and not r_resolution:
        raise LineError('E-14', '全库无仪器分辨率 R（r_source=\'none\'，M-4 未'
                        '完成）：voigt 禁用（F-39/U-26），宽度只能作为观测宽度'
                        '报告（TXT-8/CA-08）', {'reason': 'voigt_disabled_no_r'})
    if sky_handling not in ('mask', 'subtract'):
        raise LineError('E-14', "sky_handling ∈ {'mask', 'subtract'}（U-31）")
    if not 0 <= int(baseline_order) <= 7:
        raise LineError('E-14', 'baseline_order ∈ [0, C_POLY_ORDER_MAX]')
    warnings = []
    win = select_window(lam, lam_center, line_kind, half_width=half_width,
                        mask_abs=mask_abs, mask_emis=mask_emis)
    sky_info = None
    if sky_handling == 'subtract':
        # F-86（P3c，真解锁：只依赖 C_MASK_EMIS_TABLE 常量与谱数据本身，不依赖
        # M-6 线表帧/f 值）：大气吸收带段不在本通路（永远 mask，F-72①）。
        flux, win, sky_info = sky_subtract(lam, flux, sigma, win)
        if sky_info['reverted']:
            warnings.append(_warn(
                'CA-37', '天光线扣除未起效（残差 RMS 未降到扣除前的 '
                'C_SKY_RESID_FLOOR=%.2f 倍以下），已自动退回 mask：该带仍按剔除'
                '处理，不得称「已扣除」（F-86/CA-37）' % C_SKY_RESID_FLOOR,
                reason='sky_subtract_reverted'))
    widx = np.flatnonzero(_window_mask(lam, win))
    side_idx = _sideband_idx(lam, win)
    idx_fit = np.unique(np.concatenate([side_idx, widx]))
    base = baseline_fit(lam, flux, sigma, side_idx, idx_fit, int(baseline_order))
    warnings.extend(base.get('ca20', []))             # F-62③ 降阶 CA-20（两路都可达）
    dlam = _grad(lam)

    # ── F-68 门：SNR_res < C_LINE_SNR_MIN ⇒ 不拟合，只报 3σ 上限（CA-44④） ──
    sn = snr_res(win, lam, sigma, base, r_resolution)
    if sn['snr_res'] < C_LINE_SNR_MIN:
        terms0 = _ew_terms(win, lam, flux, sigma, base, dlam)
        # §3.9.3.1「3σ_W，σ_W 取 F-41/F-96 的总项」：nodetect 时侧带基线已
        # 拟出、continuum 项可得 ⇒ 上限 = 3·√(photon² + continuum²)，不再只
        # 用 photon 项（P1 评审修正）。本分支不进 AR(1) 白化通路，F-55 放大
        # 不适用（ρ 的口径与白化一起在检出路径估计）。
        cont0 = terms0['continuum']
        ul = 3.0 * math.sqrt(terms0['photon'] ** 2
                             + (cont0 ** 2 if cont0 is not None else 0.0))
        warnings.append(_warn(
            'CA-44', 'snr_res=%.2f < C_LINE_SNR_MIN=%s：不拟合，只报未检出与 '
            '3σ 上限（F-68）；误差键按 §3.9.3.1 置 null' % (sn['snr_res'],
                                                          C_LINE_SNR_MIN),
            reason='snr_res_below_gate'))
        return _assemble_nodetect(win, sn, ul, base, warnings)

    axis = base['axis']
    x0, bounds, names, shape_fn, mult = _bounds_and_x0(
        win, base, lam, flux, sigma, idx_fit, profile, axis)
    n_par = _NPAR_OF[profile]
    n_comp = 2 if profile == 'gauss2' else 1
    if n_comp > C_MAX_COMPONENTS:                     # F-37：成分数上限
        raise LineError('E-14', '轮廓成分数 %d 超出 C_MAX_COMPONENTS=%s'
                        % (n_comp, C_MAX_COMPONENTS))
    rho, rho_warn = _errs.rho_lag1(flux[idx_fit])
    if rho_warn:
        warnings.append(rho_warn)
    L, cov_method = _cov_path(flux[idx_fit], rho)
    best, fun, nfev, optimality = _fit_joint(
        lam, flux, sigma, idx_fit, base, x0, bounds, win, shape_fn, n_comp,
        axis, L, n_par)
    theta = best.x.copy()
    chi2 = 2.0 * best.cost                            # F-93①（无 loss=，D-15）
    dof = int(idx_fit.size) - theta.size
    if best.status == 0 or optimality > C_OPTIMALITY_MAX:   # F-91⑦ + CA-44①
        warnings.append(_warn('CA-44', '线拟合未收敛（status=%d 或 optimality '
                              '超限），误差不可按 1σ 解读' % best.status))
    # ── 协方差（F-93：全有/全无；触界/强相关 ⇒ 该参数 σ null + CA-44②） ──
    sigma0, cov_reason = _cov_from_jac(best.jac, dof)
    if cov_reason == 'rank_deficient':                # 差分步长塌缩救援（同 S2）
        s2, r2 = _cov_from_jac(_ct._hybrid_jac(fun, theta), dof)
        if r2 is None:
            sigma0, cov_reason = s2, None
    infl = _infl(chi2, dof)
    if cov_method == 'diag_infl':
        infl *= _errs.corr_inflation(rho)             # F-92③ 降级路放大
    if sigma0 is None:
        warnings.append(_warn('CA-44', '线参数协方差整体不可信（%s），逐参数 σ '
                              '全部置 null' % cov_reason))
    sig_raw = np.sqrt(np.diag(sigma0)) if sigma0 is not None \
        else np.full(theta.size, np.nan)
    sig = sig_raw * infl
    for i, nm in enumerate(names):                    # F-93③ 触界 ⇒ σ null
        if best.active_mask[i] != 0:
            sig[i] = np.nan
            warnings.append(_warn('CA-44', '参数 %s 触界，σ 判不可信，报告值'
                                  '降级为上限/下限表述（F-93③）' % nm))
    for i in range(theta.size):                       # F-93③ 强相关 ⇒ σ null
        for j in range(i + 1, theta.size):
            if sigma0 is not None and sig_raw[i] > 0 and sig_raw[j] > 0 \
                    and abs(sigma0[i, j] / (sig_raw[i] * sig_raw[j])) \
                    > C_DEGEN_RHO:
                sig[i] = sig[j] = np.nan
                warnings.append(_warn('CA-44', '参数 %s/%s 强相关(|ρ|>%s)，σ '
                                      '判不可信（F-93③）'
                                      % (names[i], names[j], C_DEGEN_RHO)))

    # ── 派生量（λ/FWHM：u 空间解出乘 λ 换回 Å，F-70；观测宽度 TXT-8） ──
    nc = base['order'] + 1
    b_ax = axis[1]
    u0_1, w_1 = float(theta[nc + 1]), float(theta[nc + 2])
    lam0 = float(_lam_of_u(u0_1, axis))
    if n_par == 4:                                    # voigt：FWHM_u=O-L(w,g)
        fwhm_obs = lam0 * (b_ax / 2.0) * _voigt_fwhm_u(w_1, float(theta[nc + 3]))
    else:
        fwhm_obs = lam0 * (b_ax / 2.0) * mult * w_1   # FWHM_obs = λ·FWHM_u
    lam_err = fwhm_err = None
    if sigma0 is not None and np.isfinite(sig[nc + 1]):
        lam_err = lam0 * (b_ax / 2.0) * float(sig[nc + 1])   # σ_λ = λ·σ_u
        g_fw = np.zeros(theta.size)
        if n_par == 4:                                # ∂FWHM_u/∂w、∂/∂g（O-L 解析）
            dFw, dFg = _voigt_fwhm_u_grad(w_1, float(theta[nc + 3]))
            g_fw[nc + 2] = lam0 * (b_ax / 2.0) * dFw
            g_fw[nc + 3] = lam0 * (b_ax / 2.0) * dFg
        else:
            g_fw[nc + 1] = fwhm_obs * b_ax / 2.0      # ∂/∂u0（λ 的 Jacobian）
            g_fw[nc + 2] = fwhm_obs / w_1
        fwhm_err = math.sqrt(max(float(g_fw @ sigma0 @ g_fw), 0.0)) * infl
    if fwhm_err is not None and not math.isfinite(fwhm_err):
        # F-93③ 传导（P2a）：delta_method 传播结果非有限 ⇒ null + CA-44②，
        # 禁 NaN 出户（_assemble 的 isfinite 过滤不挂签，这里补守卫+告警）。
        fwhm_err = None
        warnings.append(_warn('CA-44', 'fwhm_obs_err_aa 的链式传播结果非有限'
                              '（σ NaN），置 null（F-93③）',
                              reason='derived_err_nonfinite'))

    # ── EW 闭式三项（F-41/F-96）；基线项缺失 ⇒ 自助回落，仍缺 ⇒ E-15 d) 类 ──
    terms = _ew_terms(win, lam, flux, sigma, base, dlam)
    # F-41/F-55：相邻像素相关 ⇒ 闭式 photon 与总项乘 √((1+ρ)/(1−ρ))（F-41
    # 明文「相邻像素相关时按 F-55 放大」；口径与 F-55 一致：ρ 不可估或
    # ρ ≤ C_RHO_MIN 时不放大。自助路已带白化 L 的相关结构，不二次放大。）
    rho_infl = _errs.corr_inflation(rho)
    ew_err_note = None
    if rho_infl > 1.0:
        ew_err_note = ('相邻像素相关（rho_lag1=%.2f > C_RHO_MIN）：闭式 '
                       'EW/线流量误差的 photon 与总项已按 F-55 放大 ×%.3f'
                       '（F-41/F-55）；err_source=bootstrap 分支的白化复本'
                       '自带相关结构，不二次放大' % (rho, rho_infl))
    cap = C_BOOT_N_BUDGET if budget_left is None else int(budget_left)
    want_boot = min(int(n_boot), C_BOOT_N, max(cap, 0))
    boot, n_boot_used, seed = None, 0, None
    if not terms['baseline_ok']:
        if want_boot < 2:
            raise LineError('E-15', 'EW 误差缺连续谱/基线项且自助不可用（协方差 '
                            'null 或预算耗尽）——F-94③/F-96⑥ 的 d) 类',
                            {'reason': 'ew_err_baseline_term_missing'})
        seed = _resolve_seed(spec_hash, err_seed)
        boot = _bootstrap(win, lam, flux, sigma, idx_fit, base, theta,
                          shape_fn, n_comp, axis, L, want_boot, seed, bounds,
                          n_par)
        n_boot_used = boot['n_used']
        warnings.append(_warn('CA-44', '闭式协方差不可用：EW 误差回落自助分位'
                              '（F-95②/F-93③），err_source=bootstrap',
                              reason='closed_fallback_bootstrap'))
    elif want_boot >= 2:
        seed = _resolve_seed(spec_hash, err_seed)
        boot = _bootstrap(win, lam, flux, sigma, idx_fit, base, theta,
                          shape_fn, n_comp, axis, L, want_boot, seed, bounds,
                          n_par)
        n_boot_used = boot['n_used']
        if want_boot < int(n_boot):                   # F-95③：超预算回落 + CA-44③
            warnings.append(_warn('CA-44', '自助请求 %d 次超共享池上限 %d（'
                                  'C_BOOT_N/C_BOOT_N_BUDGET），按 %d 次回落'
                                  % (int(n_boot), want_boot, want_boot),
                                  reason='boot_budget'))

    ew_err = terms['continuum']
    ew_err_src = 'closed_form'
    ew_boot_lo = ew_boot_hi = None
    if boot is not None and boot['ew'].size >= 2:
        ew_boot_lo = float(np.percentile(boot['ew'], 16))
        ew_boot_hi = float(np.percentile(boot['ew'], 84))
    if terms['baseline_ok']:
        # F-55 放大乘在总项上（photon 原值进平方和，rho_infl 乘在外层——
        # 回显的 terms['photon'] 在 _assemble 前同步放大，见下）
        ew_err = rho_infl * math.sqrt(terms['photon'] ** 2
                                      + terms['continuum'] ** 2)
        # F-96③：coherent/continuum > 3× ⇒ CA-44⑤（order/Δλ/W 进说明，TXT-23）
        if terms['continuum'] and terms['continuum_coherent'] is not None \
                and terms['continuum_coherent'] > 3.0 * terms['continuum']:
            warnings.append(_warn(
                'CA-44', 'ew_err_terms.continuum_coherent/continuum = %.1f > 3：'
                '基线阶数 %d 或窗口宽度（Δλ=%.1f Å, W=%.2f Å）主导整体安置项，'
                '保留该行为并写明（F-96③④/TXT-23），禁止为消除它改公式或缩窗口'
                % (terms['continuum_coherent'] / terms['continuum'],
                   base['order'], terms['dl_tot'], terms['W_signed'])))
    elif boot is not None and boot['ew'].size >= 2:
        ew_err = 0.5 * (ew_boot_hi - ew_boot_lo)
        ew_err_src = 'bootstrap'
    if terms['baseline_ok'] and boot is not None and boot['ew'].size >= 2:
        msg = _closed_vs_boot(ew_err, ew_boot_lo, ew_boot_hi)
        if msg:                                       # F-96⑦：两个都列（T-65）
            warnings.append(_warn('CA-44', msg, reason='closed_vs_bootstrap'))

    W_signed = terms['W_signed']
    x_z = 1.0 + float(z or 0.0)
    is_em = line_kind == 'emission'
    # ── line_flux（发射，加性基线）/ depth（吸收，比值型）——F-67 互斥空值 ──
    line_flux = line_flux_err = None
    lf_terms = None
    depth = d_lo = d_hi = None
    depth_src = 'none'
    if is_em:
        lf_terms = _flux_terms(win, lam, flux, sigma, base, dlam)
        line_flux = lf_terms['line_flux']
        if terms['baseline_ok']:
            line_flux_err = rho_infl * math.sqrt(lf_terms['photon'] ** 2
                                                 + lf_terms['continuum'] ** 2)
        elif boot is not None and boot['line_flux'].size >= 2:
            line_flux_err = 0.5 * (float(np.percentile(boot['line_flux'], 84))
                                   - float(np.percentile(boot['line_flux'], 16)))
    else:
        depth = _depth_of(theta, base, shape_fn, n_comp, n_par)
        if sigma0 is not None:
            g_d = _depth_grad(theta, base, shape_fn, n_comp, n_par)
            s_d = math.sqrt(max(float(g_d @ sigma0 @ g_d), 0.0)) * infl
            # F-93③ 传导（P2a）：传播结果非有限（σ NaN）⇒ 视为不可用，落
            # 入下方 depth_src=='none' 分支置 null + CA-44②，禁 NaN 出户。
            if math.isfinite(s_d) and (s_d > C_REL_NONLIN * abs(depth)
                                       or depth == 0):
                # F-95②：比值型超线性判据 ⇒ 自助 16/84 分位（禁对称）
                if boot is not None and boot['depth'].size >= 2:
                    depth_src = 'bootstrap'
                    d_lo = float(depth - np.percentile(boot['depth'], 16))
                    d_hi = float(np.percentile(boot['depth'], 84) - depth)
            elif math.isfinite(s_d):
                depth_src = 'delta_method'
                d_lo = d_hi = s_d
        if depth_src == 'none':
            warnings.append(_warn('CA-44', 'depth 误差不可按 1σ 解读（协方差 '
                                  'null、传播结果非有限或自助不可用，'
                                  'F-95②/F-93③）',
                                  reason='depth_err_unavailable'))
    boot_budget_applied = any(w.get('reason') == 'boot_budget'
                              for w in warnings)
    if rho_infl > 1.0:                                # 回显口径与总项同步放大
        terms['photon'] = terms['photon'] * rho_infl
        if lf_terms is not None:
            lf_terms['photon'] = lf_terms['photon'] * rho_infl
    res = _assemble(win, sn, base, profile, names, theta, sig, sig_raw, infl,
                    chi2, dof, best, nfev, optimality, cov_method, rho,
                    terms, ew_err, ew_err_src, ew_boot_lo, ew_boot_hi,
                    n_boot_used, seed, lam0, lam_err, fwhm_obs, fwhm_err,
                    W_signed, x_z, line_flux, line_flux_err, lf_terms,
                    depth, d_lo, d_hi, depth_src, boot_budget_applied,
                    warnings)
    if ew_err_note is not None:
        # F-55 放大的书面注明（TXT-23：误差求法口径可辨）
        res['ew_err_note'] = ew_err_note
    # F-95①：voigt 的 depth 误差梯度走有限差分（解析不可得），回显 delta 口径；
    # F-86：天光扣除信息块（mask 路径恒 None，簿记容器不影响数值键）。
    res['depth_err_delta'] = 'fd' if n_par == 4 else 'analytic'
    res['sky_subtract'] = sky_info
    return res


# pylint: disable=too-many-arguments,too-many-locals,too-many-branches
def _assemble(win, sn, base, profile, names, theta, sig, sig_raw, infl, chi2,
              dof, best, nfev, optimality, cov_method, rho, terms, ew_err,
              ew_err_src, ew_boot_lo, ew_boot_hi, n_boot_used, seed, lam0,
              lam_err, fwhm_obs, fwhm_err, W_signed, x_z, line_flux,
              line_flux_err, lf_terms, depth, d_lo, d_hi, depth_src,
              boot_budget_applied, warnings):
    """§4 线表键装配（F-94：值键与误差键同批出现；F-67 互斥空值）。"""
    is_em = win['line_kind'] == 'emission'
    ew_obs = abs(W_signed)
    err_source = {
        'lambda_obs_vac_aa': 'covariance' if lam_err is not None else 'none',
        'fwhm_obs_aa': ('covariance' if fwhm_err is not None
                        and math.isfinite(fwhm_err) else 'none'),
        'ew_obs_aa': ew_err_src if ew_err is not None else 'none',
        'ew_signed_aa': ew_err_src if ew_err is not None else 'none',
        'ew_rest_aa': ew_err_src if ew_err is not None else 'none',
        'line_flux': ('bootstrap' if is_em and line_flux_err is not None
                      and ew_err_src == 'bootstrap'
                      else 'closed_form' if is_em and line_flux_err is not None
                      else 'none'),
        'depth': depth_src,
        'upper_limit_3sigma': 'count', 'snr_res': 'count',
        'n_win_pix': 'count', 'snr_def': 'metadata',
    }
    ew_err_terms = {'photon': terms['photon'],
                    'continuum': terms['continuum'],
                    'continuum_coherent': terms['continuum_coherent']}
    return {
        'detected': True,
        'line_kind': win['line_kind'], 'profile': profile,
        'lambda_center_input_aa': win['center'],
        'lambda_obs_vac_aa': lam0, 'lambda_err_aa': lam_err,
        'fwhm_obs_aa': fwhm_obs,
        'fwhm_obs_err_aa': (fwhm_err if fwhm_err is not None
                            and math.isfinite(fwhm_err) else None),
        'fwhm_intr_aa': None, 'vel_fwhm_kms': None, 'vel_shift_kms': None,
        'z_fit': None, 'velocity_family_note': _VEL_SLOT, 'width_note': _TXT8,
        'ew_obs_aa': ew_obs, 'ew_signed_aa': W_signed,
        'ew_rest_aa': W_signed / x_z,
        'ew_rest_err_aa': None if ew_err is None else ew_err / x_z,
        'ew_err_aa': ew_err, 'ew_err_form': 'two_term',
        'ew_err_terms': ew_err_terms,
        'ew_boot_lo_aa': ew_boot_lo, 'ew_boot_hi_aa': ew_boot_hi,
        'line_flux': line_flux if is_em else None,      # F-67：吸收恒 null
        'line_flux_err': line_flux_err if is_em else None,
        'line_flux_err_terms': None if not is_em else {
            'photon': lf_terms['photon'], 'continuum': lf_terms['continuum'],
            'continuum_coherent': lf_terms['continuum_coherent']},
        'depth': None if is_em else depth,              # F-67：发射恒 null
        'depth_err_lo': None if is_em else d_lo,
        'depth_err_hi': None if is_em else d_hi,
        'err_source': err_source,
        'err_scope': {k: 'stat' for k in err_source},
        'err_semantics': 'lower_bound',
        'baseline': {'order': base['order'], 'cond_2': base['cond_2'],
                     'poly_basis': base['poly_basis'], 'n_clip': base['n_clip'],
                     'axis': base['axis']},
        'n_win_pix': terms['n_win'], 'snr_res': sn['snr_res'],
        'n_pix_per_res': sn['n_pix_per_res'], 'snr_def': sn['snr_def'],
        'chi2': chi2, 'dof': dof, 'infl': infl,
        'engine': ENGINE,
        'engine_status': {'scipy_version': scipy.__version__, 'x_scale': 'jac',
                          'diff_step': C_DIFF_STEP, 'nfev': nfev,
                          'njev': int(best.njev), 'optimality': optimality,
                          'status': int(best.status),
                          'active_mask': [int(a) for a in best.active_mask],
                          'n_starts': 1},
        'cov_method': cov_method, 'rho_used': rho,
        'err_seed': seed, 'n_boot': n_boot_used,
        'rng_algo': 'pcg64' if n_boot_used else None,
        'boot_budget_applied': boot_budget_applied,
        'excluded_segments': win['excluded'],
        'params': {n: float(v) for n, v in zip(names, theta)},
        'sigma_theta': {n: (None if not np.isfinite(v) else float(v))
                        for n, v in zip(names, sig)},
        'sigma_theta_raw': {n: (None if not np.isfinite(v) else float(v))
                            for n, v in zip(names, sig_raw)},
        'warnings': warnings,
    }


def _assemble_nodetect(win, sn, upper_limit, base, warnings):
    """F-68 未检出分支：不拟合；误差键 null + CA-44④；上限走 count 免配对行。"""
    return {
        'detected': False, 'line_kind': win['line_kind'],
        'lambda_center_input_aa': win['center'],
        'lambda_obs_vac_aa': None, 'lambda_err_aa': None,
        'fwhm_obs_aa': None, 'fwhm_obs_err_aa': None,
        'fwhm_intr_aa': None, 'vel_fwhm_kms': None, 'vel_shift_kms': None,
        'z_fit': None, 'velocity_family_note': _VEL_SLOT, 'width_note': _TXT8,
        'ew_obs_aa': None, 'ew_signed_aa': None, 'ew_rest_aa': None,
        'ew_rest_err_aa': None, 'ew_err_aa': None, 'ew_err_form': 'two_term',
        'ew_err_terms': {'photon': None, 'continuum': None,
                         'continuum_coherent': None},
        'ew_boot_lo_aa': None, 'ew_boot_hi_aa': None,
        'line_flux': None, 'line_flux_err': None, 'line_flux_err_terms': None,
        'depth': None, 'depth_err_lo': None, 'depth_err_hi': None,
        'upper_limit_3sigma': float(upper_limit),
        'err_source': {'upper_limit_3sigma': 'count', 'snr_res': 'count',
                       'n_win_pix': 'count', 'snr_def': 'metadata',
                       'ew_obs_aa': 'none', 'line_flux': 'none',
                       'depth': 'none', 'lambda_obs_vac_aa': 'none',
                       'fwhm_obs_aa': 'none'},
        'err_scope': {k: 'stat' for k in ('ew_obs_aa', 'line_flux', 'depth')},
        'null_reason': ('snr_res=%.2f < C_LINE_SNR_MIN=%s：未检出，只报 3σ 上限'
                        '（F-68/CA-44④）' % (sn['snr_res'], C_LINE_SNR_MIN)),
        'baseline': {'order': base['order'], 'cond_2': base['cond_2'],
                     'poly_basis': base['poly_basis'], 'n_clip': base['n_clip']},
        'snr_res': sn['snr_res'], 'n_pix_per_res': sn['n_pix_per_res'],
        'snr_def': sn['snr_def'], 'excluded_segments': win['excluded'],
        'engine': None, 'engine_status': None, 'warnings': warnings,
    }


# ─── 9. 生长曲线（F-71：线性域 N；饱和 ⇒ 下限 + CA-27） ───────────────────

def growth_curve(lines, boot_ews=None):
    """F-71 柱密度（线性生长域）：N = 1.1296e17·W[mÅ]/(λ0²·f)（系数就地导出，
    见 N_COEF_CGS 注；禁输出速度结构/金属丰度等派生量）。lines: [{species,
    lambda0_rest_aa, f_osc, ew_obs_aa, ew_err_aa}]（EW 以 Å 计 ⇒ mÅ=Å×1e3；
    f 缺 ⇒ 该线只报 EW，note 书面原因）。

    饱和判据（CA-27）：同离子多条线的 N 互差 (max−min)/min > C_COG_TOL_RATIO
    ⇒ column_density=null、column_density_lower_bound=min N、σ 键 null、挂
    CA-27。线性域 σ_N/N = σ_W/W（delta_method）；boot_ews 给出时（自助 EW 复
    本，F-95③ 同一预算池产物）⇒ N 区间取自助 16/84 分位（弯曲段禁对称），
    err_source='bootstrap'。
    """
    from .constants import C_COG_TOL_RATIO
    per_line, warnings = [], []
    for ln in lines:
        f_osc = ln.get('f_osc')
        row = {'species': ln.get('species'),
               'lambda0_rest_aa': ln.get('lambda0_rest_aa'),
               'f_oscillator': f_osc, 'ew_obs_aa': ln.get('ew_obs_aa'),
               'column_density': None, 'sigma_n': None, 'note': None}
        if f_osc is None:
            row['note'] = '库内无振子强度，只报 EW（F-71）'
            per_line.append(row)
            continue
        lam0 = float(ln['lambda0_rest_aa'])
        ew_v = ln.get('ew_obs_aa')
        # EW=0/负/非有限守卫（P2 评审）：W=0 ⇒ N=0，会把 0 带进多线族——
        # 饱和判据 (max−min)/min 除零、逆方差 1/σ_N² 除零。该线不进柱密度族，
        # 只报 EW 并给书面原因（F-94③ 的 none 语义：不是 0、不冒充）。
        if ew_v is None or not math.isfinite(float(ew_v)) \
                or not float(ew_v) > 0:
            row['note'] = 'EW 非正或非有限（未检出），不进柱密度族（F-71）'
            per_line.append(row)
            continue
        n = N_COEF_CGS * float(ew_v) * 1e3 / (lam0 ** 2 * float(f_osc))
        s_rel = (float(ln['ew_err_aa']) / float(ln['ew_obs_aa'])
                 if ln.get('ew_err_aa') and ln.get('ew_obs_aa') else None)
        row['column_density'] = n
        row['sigma_n'] = None if s_rel is None else n * s_rel
        per_line.append(row)
    ns = [r['column_density'] for r in per_line
          if r['column_density'] is not None]
    out = {'per_line': per_line, 'column_density': None,
           'column_density_err': None, 'column_density_lower_bound': None,
           'saturated': False, 'err_source': 'none', 'err_scope': 'stat',
           'warnings': warnings, 'ca27': None}
    if not ns:
        return out
    if len(ns) >= 2 and (max(ns) - min(ns)) / min(ns) > C_COG_TOL_RATIO:
        out['saturated'] = True
        out['column_density_lower_bound'] = min(ns)
        msg = ('同离子多线柱密度互差 %.2f > C_COG_TOL_RATIO=%s：生长曲线已饱和，'
               'column_density 只能作下限，σ 键置 null（F-71/CA-27）'
               % ((max(ns) - min(ns)) / min(ns), C_COG_TOL_RATIO))
        out['ca27'] = msg
        warnings.append(_warn('CA-27', msg))
        return out
    w = [r for r in per_line if r['column_density'] is not None]
    if not all(r['sigma_n'] and r['sigma_n'] > 0 for r in w):
        out['column_density'] = float(np.mean(ns))    # σ 缺 ⇒ 等权均值、不立 σ
        out['err_source'] = 'none'
        return out
    iv = [1.0 / r['sigma_n'] ** 2 for r in w]
    vals = np.array([r['column_density'] for r in w])
    out['column_density'] = float(np.sum(vals * iv) / sum(iv))   # 逆方差合成
    out['column_density_err'] = math.sqrt(1.0 / sum(iv))
    out['err_source'] = 'delta_method'
    if boot_ews is not None:                          # 弯曲段：自助分位（不对称）
        nb = [N_COEF_CGS * np.asarray(b) * 1e3
              / (float(r['lambda0_rest_aa']) ** 2 * float(r['f_oscillator']))
              for r, b in zip(w, boot_ews)
              if b is not None and r['f_oscillator']]
        if nb:
            lo = float(min(np.percentile(x, 16) for x in nb))
            hi = float(max(np.percentile(x, 84) for x in nb))
            out['column_density_err_lo'] = max(out['column_density'] - lo, 0.0)
            out['column_density_err_hi'] = max(hi - out['column_density'], 0.0)
            out['err_source'] = 'bootstrap'
    return out
