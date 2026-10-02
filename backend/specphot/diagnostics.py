"""specphot.diagnostics —— 诊断增强（02b §3.12，U-44 开关组，默认全关）。

P2c 落地 S1/S2 侧前三项：beta_matrix（F-82）/ resp_perturb（F-83）/
anchor_reinsert（F-84）。后三项（z_from_lines / frame_probe / sky_subtract，
F-81/F-85/F-86）属 P3c，请求到达时由 photometry.py 以 501 phase='P3c' 拒绝
（E-15 + tooltip 指期次，A-3），本文件不含其实现。

共同纪律（02b §3.12 头 + F-105① 同族）：
  ① 每项一个显式开关；关闭时本模块**不进入任何计算路径** ⇒ 响应与不含本实现
    逐字节相同（T-48①；diagnostics{} 恒在场、未开启为空对象，不在比较域内）。
  ② 输出一律进 diagnostics{} 子对象，不进主结果表必填列；resp_method /
    m_syn_local 两列按 §4.3 是列集追加位（恒在场、未开启写 lower_bound / null）。
  ③ 计算成本受 ST-9/ST-10 封顶：resp_perturb 触顶时降规模并回显 perturb_n
    （Q-27），而不是超时。

实现约定（规格未逐项钉死处的裁量，P2c 收口汇报登记）：
  - F-82 的 β_ij 口径：分子「(f_i/f_j)_2 / (…)_1」的两次取值 = 同波段对用**实测**
    锚点色与**合成**色各算一次（「用合成值与实测值各算」），两支各差 |Δt| ≤
    dt_tol_eff（F-61 生效容差，对谱时刻）才成格；β_ij = (ΔC_obs − ΔC_syn) /
    log10(ν_i/ν_j)，格内 dt_d = |mjd_i − mjd_j|（格内两锚点的时刻差，两边各自
    已在容差内）。纯幂律 + 无污染 ⇒ 全格 β≈0；离群锚点 ⇒ 该格 |β| 超 3σ_β。
  - F-82 的 σ_β：delta_method，Var(ΔC) = δm_err_i² + δm_err_j² + resp_i² +
    resp_j² + cross，cross 按两行是否参与 anchored GLS 分档（误差映射表
    「同波段对的 −2Cov」的一阶精确形式，对角 C、Monte-Carlo 实测）：
    两行都参与 ⇒ +2·Var(κ)_mag（GLS 残差反相关，Cov(dm_i,dm_j) = −Var(κ)_mag；
    修正方向为放大 σ_β——参与行的 delta_m_err² = var_in·(1−h_i) 已按 F-56
    收缩、不含 Var(κ)_mag）；恰一行参与 ⇒ 0（非参与行的 κ* 误差与参与行被
    GLS 吸收后的残差分量精确定消，Cov = 0）；两行都不参与 ⇒ −2·Var(κ)_mag
    （两行 delta_m_err 各含 +Var(κ)_mag（F-56 非参与支），Cov = +Var(κ)_mag，
    −2 恰好抵消）。Var(κ)_mag = (2.5/ln10·σ_κ/κ*)²，非 anchored 时为 0。
  - F-82 的 color_err_cal（§3.9.3.1 颜色行）：同一 κ* 的定标偏移在色（两行
    之差）中**精确定消** ⇒ 两行 mag_err_cal 在场（anchored）时格值 = 0.0
    （闭式结果、非缺测冒充；§3.9.3.1 明文禁止把两个单波段 σ 直接 quadrature
    ——完全相关下同向偏移应抵消而非相加）；任一行 mag_err_cal = null
    （σ_cal 无法评估：非 anchored/model 等）⇒ null，TXT-23 书面原因进
    block note。resp 项仍进 color_err_stat（F-82 明文）。
  - F-83 的平滑尺度 σ_lnl = 该波段通带自身的 √Var_p(ln λ)（F-2 定义，
    band_integrals 的 sigma2_lnl 回显值），即扰动在 ln λ 上以通带自身宽度为
    相关长度 ⇒ 低频乘性形状误差；幅度按 C_SHAPE_PERT_EPS 归一（支撑内 RMS）。
  - F-83 的随机流：以 (spec_hash, band, 组号) 派生 default_rng 种子 ⇒ 同一输入
    逐字节可复现（F-93⑤ 同族；种子派生式登记 docs/TECHNICAL.md）。
  - F-84 的局部改正 = L-5 的「用实测流构造 S·10^{0.4m} 后卷积」：该波段支撑内
    乘常数 a_i = g_i/(κ*·f_i)（残差 g_i − κ*f_i 按积分线性性恰好整体插入），
    重算通带积分得 m_syn_local ⇒ 积分线性 ⇒ m_syn_local = AB(g_i)（与实测差
    ≈ 0 是构造性结论，作为通路自检回显 delta_m_local）；诊断价值在 m_syn_local
    （≡ 局部最优）与主列 mag（单一全局 κ*）之差暴露个别波段的系统性跑偏。
"""
import math
import time
import zlib

import numpy as np

from .constants import C_SHAPE_PERT_EPS, C_SHAPE_PERT_P
from .response import ab_mag_from_fnu, band_integrals
from .reader import SpecLoadError

# 响应组预算：扰动组循环在 ST-5 轻档硬超时（C_ENDPOINT_TIMEOUT_S）之前预留
# 本秒数给装配与回程；触顶即停并回显实际组数（Q-27 降规模，不超时）。规格 §7
# 未给此预留量 ⇒ 实现补名登记（缺则登记义务）。
_C_SHAPE_PERT_RESERVE_S = 0.25

_DIAG_ONLY = ('该矩阵/列只用于发现不一致（causal_use=\'diagnostic_only\'）：'
              '用的是合成测光，其系统性偏色项（F-17）与 σ_resp 下限会污染格值，'
              '不得作为物理 β 报告（F-82/F-84 常驻声明）')


def _beta_note():
    return _DIAG_ONLY


# ─── F-83 resp_perturb：通带形状扰动 ⇒ 真实的 σ_resp ─────────────────────

def _smooth_lnlnoise(cl, t, sigma_lnl, rng, eps=C_SHAPE_PERT_EPS):
    """一组 ln λ 上的低频乘性形状扰动 δ：白噪声 → σ_lnl 高斯核平滑 → 支撑内
    RMS 归一到 C_SHAPE_PERT_EPS。返回与 cl 等长的 δ 数组（T' = T·(1+δ) 用）。"""
    x = np.log(np.asarray(cl, dtype=float))
    z = rng.standard_normal(x.size)
    dx = float(np.median(np.diff(x))) if x.size > 1 else 1e-3
    s = max(float(sigma_lnl), dx, 1e-3)
    # 高斯核半径取 3σ（节点数计，尾截断）；核长超过曲线节点数的一半 ⇒ 无从谈
    # 低频，退化为未平滑白噪声（仍按幅度归一，极短曲线的口径由 T-49 量级断言兜住）
    half = min(max(1, int(math.ceil(3.0 * s / max(dx, 1e-6)))),
               max((x.size - 1) // 2, 0))
    if half < 1:
        d = z
    else:
        k = np.exp(-0.5 * (np.arange(-half, half + 1) * (dx / s)) ** 2)
        k /= k.sum()
        d = np.convolve(z, k, mode='same')
    sup = np.asarray(t, dtype=float) > 0.0
    rms = float(np.sqrt(np.mean(d[sup] ** 2))) if sup.any() \
        else float(np.sqrt(np.mean(d ** 2)))
    if rms <= 0.0:
        return np.zeros_like(d)
    return d * (eps / rms)


def resp_perturb(lam, flux, band_defs, bands, *, weighting, mask_ranges,
                 sigma_lnl, seed_base, deadline, p_req=C_SHAPE_PERT_P):
    """F-83：C_SHAPE_PERT_P 组通带形状扰动 ⇒ 每波段星等样本散布（σ_resp 新口径）。

    band_defs: {band: {'lam','t'}}（参与波段的通带曲线）；sigma_lnl: {band: σ²_lnl}
    （主积分的 sigma2_lnl 回显）。逐组逐波段以扰动曲线重算通带平均（只做一次
    已向量化的加权积分，不重拟合谱，§5.4 预算行）；触 deadline − 预留即停并
    降组数（Q-27）。返回 ({band: 散布 std 或 None}, info)。
    """
    mags = {b: [] for b in bands}
    n_done = 0
    for g in range(int(p_req)):
        for b in bands:
            d = band_defs.get(b)
            if d is None or not d.get('lam'):
                continue
            rng = np.random.default_rng([seed_base & 0xFFFFFFFF,
                                         zlib.crc32(b.encode()) & 0xFFFFFFFF, g])
            delta = _smooth_lnlnoise(d['lam'], d['t'],
                                     math.sqrt(max(sigma_lnl.get(b) or 0.0, 0.0)),
                                     rng)
            t_p = np.clip(np.asarray(d['t'], dtype=float) * (1.0 + delta), 0.0, None)
            try:
                integ = band_integrals(lam, flux, d['lam'], t_p.tolist(),
                                       weighting=weighting,
                                       mask_ranges=mask_ranges)
            except SpecLoadError:
                continue                      # 扰动实现不该 E-04；保守跳过该组
            if integ['fnu_cgs'] > 0:
                mags[b].append(ab_mag_from_fnu(integ['fnu_cgs']))
        n_done = g + 1
        if time.monotonic() > deadline - _C_SHAPE_PERT_RESERVE_S:
            break                             # ST-9/ST-10 触顶降规模并回显
    out = {}
    for b, arr in mags.items():
        out[b] = (float(np.std(np.asarray(arr), ddof=1))
                  if len(arr) >= 2 else None)
    info = {'perturb_n': n_done, 'perturb_n_requested': int(p_req),
            'downscaled': n_done < int(p_req), 'eps': C_SHAPE_PERT_EPS,
            'resp_method': 'perturbation'}
    return out, info


# ─── F-82 beta_matrix：两两颜色时间演化矩阵 ──────────────────────────────

def beta_matrix(rows, spec_mjd, dt_tol_eff, var_kappa_mag=0.0):
    """F-82：上三角格集 + 每格 dt_d / σ_β；CA-40 判定（格间 β 互差显著）。

    rows: 每波段一行 {band, pivot_aa, mag, m_obs, m_obs_mjd, mag_err_stat,
    mag_err_cal, mag_err_resp, delta_m_err, participating}（null 分量按 0 计；
    participating = 该行参与 anchored GLS，由 photometry 按 h_map 同通道传入；
    TXT-23 的书面原因由 block note 承担）。两支色都要求锚点时刻对谱时刻
    |Δt| ≤ dt_tol_eff。σ_β 的 κ* 协方差项按参与分档，推导见文件头
    （都参与 +2Var(κ)_mag / 恰一行参与 0 / 都不参与 −2Var(κ)_mag）。
    返回 (block, ca40_cells)：block = {'cells': […], 'causal_use', 'note'}。
    """
    elig = [r for r in rows
            if r.get('m_obs') is not None and r.get('m_obs_mjd') is not None
            and spec_mjd is not None
            and abs(float(r['m_obs_mjd']) - float(spec_mjd)) <= float(dt_tol_eff)]
    cells = []
    for i in range(len(elig)):
        for j in range(i + 1, len(elig)):
            a, b = elig[i], elig[j]
            if not a.get('pivot_aa') or not b.get('pivot_aa'):
                continue
            lg = math.log10(float(b['pivot_aa']) / float(a['pivot_aa']))
            if lg == 0.0:
                continue
            g = lambda r, k: (float(r[k]) if r.get(k) is not None else 0.0)
            d_c = (float(a['m_obs']) - float(b['m_obs'])) \
                - (float(a['mag']) - float(b['mag']))
            pa, pb = bool(a.get('participating')), bool(b.get('participating'))
            cross = (2.0 if pa and pb else 0.0 if pa or pb else -2.0) \
                * float(var_kappa_mag)
            var = (g(a, 'delta_m_err') ** 2 + g(b, 'delta_m_err') ** 2
                   + g(a, 'mag_err_resp') ** 2 + g(b, 'mag_err_resp') ** 2
                   + cross)
            cal_ok = a.get('mag_err_cal') is not None \
                and b.get('mag_err_cal') is not None
            cells.append({
                'band_i': a['band'], 'band_j': b['band'],
                'beta': d_c / lg,
                'sigma_beta': math.sqrt(max(var, 0.0)) / abs(lg),
                'color': float(a['mag']) - float(b['mag']),
                'color_err_stat': math.sqrt(g(a, 'mag_err_stat') ** 2
                                            + g(b, 'mag_err_stat') ** 2
                                            + g(a, 'mag_err_resp') ** 2
                                            + g(b, 'mag_err_resp') ** 2),
                # §3.9.3.1 颜色行/F-56 杠杆式：同一 κ* 的定标偏移在两行之差中
                # 精确定消 ⇒ 0.0 是闭式结果；无法评估（任一行 null）⇒ null +
                # block note 书面原因（TXT-23），不以 0 冒充
                'color_err_cal': 0.0 if cal_ok else None,
                'dt_d': abs(float(a['m_obs_mjd']) - float(b['m_obs_mjd']))})
    ca40 = []
    for x in range(len(cells)):
        for y in range(x + 1, len(cells)):
            c1, c2 = cells[x], cells[y]
            if c1['sigma_beta'] is None or c2['sigma_beta'] is None:
                continue
            if abs(c1['beta'] - c2['beta']) > 3.0 * math.sqrt(
                    c1['sigma_beta'] ** 2 + c2['sigma_beta'] ** 2):
                ca40.append((c1, c2))
    note = _DIAG_ONLY + (
        '｜color_err_cal 口径（§3.9.3.1 颜色行/F-56 杠杆式）：同一 κ* 的定标'
        '偏移在色（两行之差）中精确定消，两行 mag_err_cal 在场时格值 0.0 是'
        '闭式结果、不是缺测冒充'
        + ('；有格 color_err_cal=null：该对至少一行 mag_err_cal=null'
           '（σ_cal 无法评估，非 anchored/model 或 mag 越出 sane 域）⇒ 按 '
           'null 出并附此书面原因，不以 0 冒充（TXT-23）'
           if any(c['color_err_cal'] is None for c in cells) else ''))
    block = {'cells': cells, 'causal_use': 'diagnostic_only', 'note': note}
    return block, ca40


# ─── F-84 anchor_reinsert：锚点残差再插入 ────────────────────────────────

def anchor_reinsert_mag(lam, flux, curve_lam, curve_tr, factor, *, weighting,
                        mask_ranges, allow_mono=False, mono_lam_ref=None):
    """F-84：把该波段残差 g_i − κ*·f_i 单独再插入谱（L-5：S·10^{0.4m} 后卷积
    —— κ*·F 谱支撑内乘 g_i/(κ*·f_i)，折算到本函数收到的原始谱上即乘
    factor = g_i/f_i，κ* 公共模消去；积分线性 ⇒ 残差恰好整体插入），重算通带
    积分返回局部改正后的 m_syn（AB）。重积分不可用（E-04）⇒ None。"""
    lam_a = np.asarray(lam, dtype=float)
    plo, phi = float(curve_lam[0]), float(curve_lam[-1])
    sup = (lam_a >= plo) & (lam_a <= phi)
    flux_c = np.where(sup, np.asarray(flux, dtype=float) * float(factor),
                      np.asarray(flux, dtype=float))
    try:
        integ = band_integrals(lam_a.tolist(), flux_c.tolist(), curve_lam,
                               curve_tr, weighting=weighting,
                               mask_ranges=mask_ranges, allow_mono=allow_mono,
                               mono_lam_ref=mono_lam_ref)
    except SpecLoadError:
        return None
    return ab_mag_from_fnu(integ['fnu_cgs']) if integ['fnu_cgs'] > 0 else None


def anchor_reinsert_block(cells, note=None):
    """F-84 的 diagnostics.anchor_reinsert 装配：cells = [{band, m_syn_local,
    delta_m_local, shrink_1_minus_h, reason?}]；m_syn_local 不得顶替主列 mag
    （F-11 单一全局缩放因子不被架空），causal_use='diagnostic_only' 常驻。"""
    out = {'cells': cells, 'causal_use': 'diagnostic_only', 'note': _DIAG_ONLY}
    if note:
        out['note'] = f'{_DIAG_ONLY}｜{note}'
    return out


# ═══ P3c · 线侧诊断增强（02b §3.12：F-81 z_from_lines / F-85 frame_probe） ═══
# 交付形态（§9 P3c 行 + §12 M-6）：实现完整就位，但**闸门后**——两件的判据都
# 挂在 M-6 复核线表/参考特征表上（registry.M6_LINE_TABLE / M6_FRAME_FEATURES，
# 现状 None ⇒ 全库未满足），生产请求一律输出「null + 书面原因」的闸门块（键恒
# 在场，F-94③ 的 none 语义：不是 0、不冒充）。数值路径只经测试内合成 fixture
# （monkeypatch registry 两键）模拟「M-6 复核态」驱动——fixture 是测试内合成，
# 不是伪造库内线表。
#
# 共同的前置（T-51）：lambda_frame='unknown' ⇒ E-14（reason='lambda_frame_unknown'，
# 由装配层拒绝）；每分辨率元信噪不足 ⇒ inconclusive / null，不得沉默成默认值。
# 绝不自动改写用户 z（U-37）或 lambda_frame（U-39），CA-36/CA-37 只写告警。

_GATE_NOTE = ('M-6 线表复核未完成（registry.M6_LINE_TABLE/M6_FRAME_FEATURES 为'
              '空）：宿主 29 组固定线表静止系波长全为整数 Å 且未标空气/真空帧，'
              '空气→真空偏移 0.92–2.20 Å ≡ 82–87 km/s（F-38），z_from_lines/'
              'frame_probe 没有判据，输出按 null + 本书面原因出（F-94③/T-51）')


def z_from_lines_gate_block():
    """F-81 的 M-6 闸门块：§4.2 键集 z_from_lines{z_fit, sigma_z, n_lines_used,
    lines[], verdict} 恒在场、值键全 null + 书面原因（T-51 skip 的原因本体）。"""
    return {'z_fit': None, 'sigma_z': None, 'n_lines_used': None, 'lines': [],
            'verdict': 'disabled_line_frame_unverified', 'reason': _GATE_NOTE,
            'ca36': None}


def frame_probe_gate_block():
    """F-85 的 M-6 闸门块：frame_probe{frame_suggestion, n_support,
    log_lik_ratio} 恒在场 + 书面原因（禁止沉默为 'vacuum'，T-51）。"""
    return {'frame_suggestion': 'inconclusive', 'n_support': None,
            'log_lik_ratio': None, 'features': [], 'reason': _GATE_NOTE,
            'note': 'M-6 未复核 ⇒ 判据缺位；inconclusive 是闸门值、不是默认值'}


def _ln_grid(lam, flux, sigma, ok):
    """谱 → 等距 ln λ 网格上的白化残差输入（返回 grid, step, f_g, s_g）。"""
    lg = np.log(np.asarray(lam, dtype=float)[ok])
    fg = np.asarray(flux, dtype=float)[ok]
    sg = np.asarray(sigma, dtype=float)[ok]
    n = lg.size
    step = float((lg[-1] - lg[0]) / max(n - 1, 1))
    grid = np.linspace(lg[0], lg[-1], n)
    order = np.argsort(lg)
    f_g = np.interp(grid, lg[order], fg[order])
    s_g = np.interp(grid, lg[order], sg[order])
    return grid, max(step, 1e-8), f_g, s_g


def _running_median(x, win):
    """一维滑动中位数（奇数窗、反射填充）——去基线残差的局部连续谱估计。"""
    n = x.size
    w = int(min(max(win, 3), n if n % 2 else n - 1) // 2 * 2 + 1)
    pad = np.pad(x, w // 2, mode='reflect')
    idx = np.arange(w) + np.arange(n)[:, None]
    return np.median(pad[idx], axis=1)


def z_from_lines(lam, flux, sigma, *, z_used, line_table, ok=None,
                 z_min=0.0, z_max=1.5, template_w=None):
    """F-81：由线位反推红移（一次互相关 + 峰拟合，ln λ 等距网格 + FFT 互相关，
    无迭代，§5.4 预算 ≤300 ms）。

    line_table：M-6 复核线表（registry.M6_LINE_TABLE 的运行时值；测试传合成
    fixture）——[{lambda0_rest_aa, line_frame, species?}]；air 条目经宿主
    wavconvert 换真空恰好一次（F-38③）。
    ① 只报候选：z_fit 与 z_used 之差 > C_Z_TOL ⇒ verdict='ca36_mismatch' +
      ca36 文案（调用方进 warnings[]），**绝不自动改写 z**（CA-36/U-37）；
      被采信/被否决的线逐条列在 lines[]（status ∈ accepted/rejected）。
    ② 命中线数 < C_LINE_MATCH_MIN ⇒ z_fit=null（verdict='too_few_lines'）。
    ③ σ_z 由互相关峰的二阶曲率与白化残差噪声估计（σ_z=(1+z_fit)·σ_τ；
      实现裁量：σ_τ=√2·σ_CC/√k，k 为峰部抛物线曲率、σ_CC 为互相关噪声底——
      登记于 docs/TECHNICAL.md）。
    返回 §4.2 键集块；纯函数、只读输入。
    """
    import wavconvert
    from .constants import C_LINE_MATCH_MIN, C_Z_TOL
    lam = np.asarray(lam, dtype=float)
    ok = (np.isfinite(lam) & np.isfinite(flux) & np.isfinite(sigma)
          & (np.asarray(sigma, dtype=float) > 0)) if ok is None else ok
    if not bool(np.any(ok)):
        return {'z_fit': None, 'sigma_z': None, 'n_lines_used': 0, 'lines': [],
                'verdict': 'no_data', 'ca36': None}
    grid, step, f_g, s_g = _ln_grid(lam, flux, sigma, ok)
    cont = _running_median(f_g, max(31, int(round(0.02 / step)) | 1))
    resid = (f_g - cont) / s_g                       # 白化残差
    sig_r = max(1.4826 * float(np.median(np.abs(resid
               - np.median(resid)))), 1e-12)
    # 静止系候选线表（M-6 复核态）：air ⇒ 换真空恰好一次
    lam0_vac = np.array([
        (wavconvert.air_to_vacuum(float(e['lambda0_rest_aa']))
         if e.get('line_frame') == 'air'
         else float(e['lambda0_rest_aa'])) for e in line_table])
    lam0_vac = lam0_vac[np.isfinite(lam0_vac) & (lam0_vac > 0)]
    w_t = float(template_w) if template_w else max(2.0 * step, 2e-4)
    span = grid[-1] - grid[0]
    pad = int(2 ** math.ceil(math.log2(grid.size + grid.size)))
    T0 = np.zeros(pad)
    u0s = np.log(lam0_vac)
    inb = (u0s > grid[0] - 0.05) & (u0s < grid[-1] + 0.05 + math.log(1 + z_max))
    if not bool(np.any(inb)) or lam0_vac.size == 0:
        return {'z_fit': None, 'sigma_z': None, 'n_lines_used': 0, 'lines': [],
                'verdict': 'no_lines_in_band', 'ca36': None}
    for u0 in u0s[inb]:
        j0 = int(round((u0 - grid[0]) / step))
        if 0 <= j0 < pad:
            T0[j0] += 1.0
    R0 = np.zeros(pad)
    R0[:grid.size] = resid
    cc = np.fft.irfft(np.fft.rfft(R0) * np.conj(np.fft.rfft(T0)), pad)
    # τ 搜索域：z ∈ [z_min, z_max] ⇒ 正平移 τ = ln(1+z)（只搜正 lag 支；z<0 不
    # 在本条搜索域内，F-81 是红移反推不是周期搜索）
    lo_i = int(round(math.log(1.0 + z_min) / step))
    hi_i = min(int(round(math.log(1.0 + z_max) / step)), pad // 2)
    if hi_i - lo_i < 8:
        return {'z_fit': None, 'sigma_z': None, 'n_lines_used': 0, 'lines': [],
                'verdict': 'no_lines_in_band', 'ca36': None}
    seg = cc[lo_i:hi_i + 1]
    k_peak = lo_i + int(np.argmax(seg))

    def _c_at(kk):
        return float(cc[kk % pad])

    c_m, c_0, c_p = _c_at(k_peak - 1), _c_at(k_peak), _c_at(k_peak + 1)
    denom = c_m - 2.0 * c_0 + c_p
    frac = 0.5 * (c_m - c_p) / denom if abs(denom) > 1e-30 else 0.0
    tau_hat = (k_peak + max(min(frac, 1.0), -1.0)) * step
    z_fit = math.exp(tau_hat) - 1.0
    # σ_z：峰曲率 k（负的二阶差分/step²）与互相关噪声底——噪声底取正 lag 区
    # [0, grid.size) 内、峰位 ±50 格以外的 MAD（零延拓区不算，否则低估）
    guard = min(max(int(round(50.0)), 5), grid.size // 4)
    lags = np.arange(0, min(grid.size, pad))
    mask_n = np.ones(lags.size, dtype=bool)
    kp = k_peak % pad
    mask_n &= (np.abs(lags - kp) > guard) | (lags > pad)  # 峰邻域除外
    cc_n = cc[lags[mask_n]]
    cc_n = cc_n if cc_n.size >= 8 else cc[lags]
    s_cc = max(1.4826 * float(np.median(np.abs(cc_n - np.median(cc_n)))),
               1e-30)
    k_curv = max(-denom / step ** 2, 1e-30)          # −C''(τ*)
    sigma_tau = math.sqrt(2.0) * s_cc / math.sqrt(k_curv)
    sigma_z = (1.0 + max(z_fit, 0.0)) * sigma_tau
    # 逐线采信/否决：预测观测波长处白化残差局部峰（±3×template_w 窗）
    rows, n_hit = [], 0
    for e, l0 in zip(line_table, lam0_vac):
        lam_pred = l0 * (1.0 + z_fit)
        u_p = math.log(lam_pred)
        j = int(round((u_p - grid[0]) / step))
        hit, z_line = False, None
        if 2 <= j < grid.size - 2:
            w_loc = max(int(round(3.0 * w_t / step)), 2)
            j_hit = max(0, j - w_loc) + int(np.argmax(
                resid[max(0, j - w_loc): j + w_loc + 1]))
            if resid[j_hit] > 3.0 * sig_r:
                hit = True
                z_line = math.exp(grid[j_hit]) / l0 - 1.0   # λ_obs/λ0 − 1
        accepted = hit and z_line is not None \
            and abs(z_line - z_fit) <= max(3.0 * sigma_tau, C_Z_TOL)
        n_hit += 1 if accepted else 0
        rows.append({'species': e.get('species'),
                     'lambda0_rest_aa': float(e['lambda0_rest_aa']),
                     'line_frame': e.get('line_frame'),
                     'lambda_obs_pred_aa': float(lam_pred),
                     'z_line': None if z_line is None else float(z_line),
                     'status': 'accepted' if accepted
                     else ('rejected' if hit else 'no_match')})
    ca36 = None
    if n_hit < C_LINE_MATCH_MIN:
        verdict = 'too_few_lines'
        z_fit = None
        sigma_z = None
    else:
        verdict = 'ok'
        if z_used is not None and abs(z_fit - float(z_used)) > C_Z_TOL:
            verdict = 'ca36_mismatch'
            ca36 = ('z_from_lines 的 z_fit=%.5f 与所用 z=%.5f 之差 %.5f > '
                    'C_Z_TOL=%s：线位与所用红移矛盾，静止系结果可疑；'
                    '被采信/被否决的线见 lines[]，不自动改写 z（F-81/CA-36）'
                    % (z_fit, float(z_used), abs(z_fit - float(z_used)),
                       C_Z_TOL))
    return {'z_fit': (float(z_fit) if z_fit is not None else None),
            'sigma_z': (float(sigma_z) if sigma_z is not None else None),
            'n_lines_used': int(n_hit), 'lines': rows, 'verdict': verdict,
            'ca36': ca36}


def frame_probe(lam, flux, sigma, *, features, ok=None):
    """F-85：上传件波长框架反证——检查大气吸收带（C_MASK_ABS_TABLE 的 O₂ B/A
    带）与天光发射线（C_MASK_EMIS_TABLE）的凹陷/上凸中心落在真空波长还是空气
    波长上（两者相差 0.92–2.20 Å = C_AIR2VAC，一律调宿主 wavconvert）。

    features：M-6 复核参考特征表（registry.M6_FRAME_FEATURES 的运行时值；测试
    传合成 fixture）——[{name, kind ∈ {abs, emis}, lambda_vac_aa}]。
    输出 frame_probe{frame_suggestion ∈ {vacuum, air, inconclusive}, n_support,
    log_lik_ratio, features[]}，**只进 diagnostics{}**，不得自动改写 U-39
    （F-79②/F-85）。每分辨率元信噪（无 R ⇒ C_DLAM_OVER_FWHM_MAX 等效口径，与
    lines.snr_res 同一约定）< C_FRAME_PROBE_MIN_SNR 或带内无数据 ⇒ 该特征
    inconclusive；全部不可用 ⇒ frame_suggestion='inconclusive'（禁止沉默为
    'vacuum'，T-51）。
    判据（实现裁量，docs/TECHNICAL.md 登记）：每特征取 ±3 Å 段、局部线性连续谱
    （段两端各 25% 像素），abs 取凹陷/emis 取上凸的抛物线内插中心 λ_obs；
    λ_air = wavconvert.vacuum_to_air(λ_vac)；中心不确定度 σ_λ 由峰曲率与像素
    噪声传播；log_lik_ratio = Σ (Δ_air²−Δ_vac²)/(2σ_λ²)（正 ⇒ 偏真空）；
    n_support = |判定显著（|Δ_vac−Δ_air| > σ_λ）且同向| 的特征数。
    """
    import wavconvert
    from .constants import C_DLAM_OVER_FWHM_MAX, C_FRAME_PROBE_MIN_SNR
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    ok = (np.isfinite(lam) & np.isfinite(flux) & np.isfinite(sigma)
          & (sigma > 0)) if ok is None else ok
    rows, llr, n_support, votes = [], 0.0, 0, []
    for ft in features:
        lv = float(ft['lambda_vac_aa'])
        la = float(wavconvert.vacuum_to_air(lv))
        row = {'name': ft.get('name'), 'kind': ft.get('kind'),
               'lambda_vac_aa': lv, 'lambda_air_aa': la,
               'lambda_obs_aa': None, 'sigma_lambda_aa': None,
               'vote': None, 'significant': False, 'usable': False}
        m = ok & (lam >= lv - 3.0) & (lam <= lv + 3.0)
        n_pix = int(np.count_nonzero(m))
        if n_pix < 9:
            row['vote'] = 'inconclusive'
            row['note'] = '带内无数据或像素不足'
            rows.append(row)
            continue
        lamF, fF, sF = lam[m], flux[m], sigma[m]
        # 每分辨率元信噪（无 R 等效口径，与 lines.snr_res 同约定）
        snr_res = float(np.median(fF / sF)) * math.sqrt(C_DLAM_OVER_FWHM_MAX)
        if snr_res < C_FRAME_PROBE_MIN_SNR:
            row['vote'] = 'inconclusive'
            row['note'] = '每分辨率元信噪 %.1f < C_FRAME_PROBE_MIN_SNR=%s' % (
                snr_res, C_FRAME_PROBE_MIN_SNR)
            rows.append(row)
            continue
        # 局部线性连续谱：段两端各 25% 像素
        o = np.argsort(lamF)
        nq = max(int(0.25 * n_pix), 2)
        edge = np.zeros(n_pix, dtype=bool)
        edge[o[:nq]] = True
        edge[o[-nq:]] = True
        Mc = np.column_stack([np.ones(int(np.count_nonzero(edge))),
                              lamF[edge] - float(np.mean(lamF))])
        coef, *_ = np.linalg.lstsq(Mc, fF[edge], rcond=None)
        cont = coef[0] + coef[1] * (lamF - float(np.mean(lamF)))
        d = (fF - cont) * (-1.0 if ft.get('kind') == 'abs' else 1.0)
        j = int(np.argmax(d))
        if j <= 0 or j >= n_pix - 1:
            row['vote'] = 'inconclusive'
            row['note'] = '特征中心贴段沿，无法内插'
            rows.append(row)
            continue
        d_m, d_0, d_p = d[j - 1], d[j], d[j + 1]
        den = d_m - 2.0 * d_0 + d_p
        if den >= 0:                                  # 不是凸峰（上凸判据 den<0）
            row['vote'] = 'inconclusive'
            row['note'] = '段内无显著 %s 特征' % ('凹陷' if ft.get('kind') == 'abs' else '上凸')
            rows.append(row)
            continue
        frac = 0.5 * (d_m - d_p) / den
        lam_obs = float(lamF[j] + max(min(frac, 1.0), -1.0)
                        * (lamF[j + 1] - lamF[j - 1]) / 2.0)
        s_n = max(1.4826 * float(np.median(np.abs(
            (fF[edge] - cont[edge]) - np.median(fF[edge] - cont[edge])))), 1e-30)
        k_curv = max(-den / max((lamF[j + 1] - lamF[j - 1]) / 2.0, 1e-9) ** 2,
                     1e-30)
        sigma_lam = math.sqrt(2.0) * s_n / math.sqrt(k_curv)
        dv, da = abs(lam_obs - lv), abs(lam_obs - la)
        vote = 'vacuum' if dv <= da else 'air'
        significant = abs(da - dv) > max(sigma_lam, 1e-6)
        llr += (da * da - dv * dv) / (2.0 * sigma_lam ** 2)
        row.update({'lambda_obs_aa': lam_obs, 'sigma_lambda_aa': sigma_lam,
                    'vote': vote, 'significant': significant, 'usable': True})
        if significant:
            n_support += 1
            votes.append(vote)
        rows.append(row)
    if n_support >= 1 and votes and all(v == votes[0] for v in votes):
        suggestion = votes[0]
    else:
        suggestion = 'inconclusive'
    return {'frame_suggestion': suggestion, 'n_support': n_support,
            'log_lik_ratio': float(llr), 'features': rows,
            'note': ('只进 diagnostics{}，由用户决定是否改 U-39；'
                     '禁止自动改写 lambda_frame（F-85/F-79②）')}


# ═══ P3d · 阻尼翼与吸收系统（02b §3.13：F-98…F-105，U-48 三开关，默认全关） ═══
# 交付形态（§9 P3d 行 + §12 M-4/M-6）：实现完整就位，但**闸门后**——M-4（R）与
# M-6（线表帧与 f 值）库内均未满足；F-98① 明文「静止系 Lyα 真空波长由线表给出、
# 本模块不另立 Lyα 常量」⇒ 生产请求的识别/翼拟合一律输出「null + 书面原因」的
# 闸门块（键恒在场，F-94③/T-72②）；数值路径只经测试内合成 fixture 驱动。
# 不依赖前置的 F-102 闭集空输出与 F-103/F-104 簿记半支**真实现**。实现裁量
# （τ 剖面就地导出、双连续谱族构造、检出显著性、intervening_or_host 判据、
# Δχ²=1 剖面算法与预算）逐条登记 docs/TECHNICAL.md 的 P3d 段。

_ABS_GATE_NOTE = (
    'M-6 线表复核未完成（registry.M6_LINE_TABLE 为空）：静止系 Lyα 真空波长与'
    '金属线 rest 波长/f 值无输入源（F-98① 本模块不另立 Lyα 常量），z_abs 的'
    '金属线路由缺位 ⇒ 识别/翼拟合按 null + 本书面原因出（F-94③/F-105①）')

_C_GAMMA_LYA = 6.2649e8   # Γ(Lyα) s⁻¹（A 值和；仅剖面形状用——λ/f 由线表传入）
import scipy
import scipy.constants as _sc
from .constants import (C_ABS_CLASS_DLA, C_ABS_CLASS_LLS, C_ABS_CLASS_SUBDLA,
                        C_ABS_DETECT_SIGMA, C_WING_B_GRID_KMS, C_WING_B_KMS,
                        C_WING_FIT_MIN_SNR, C_WING_RED_KMS,
                        C_WING_Z_WIN_SIGMA)

_SIGMA_INT_CGS = math.pi * (_sc.e * 2.99792458e9) ** 2 / ((_sc.m_e * 1e3)
                                                          * (_sc.c * 1e2))  # πe²/(m_e c)≈0.02654

C_C_KMS = 299792.458


def lya_entry(m6):
    """M-6 线表中的 Lyα 条目（species 含 'ly'；air 条目换真空恰好一次，F-38③）。
    F-98①：本模块不另立 Lyα 常量 ⇒ 找不到条目即闸门（None）。
    （P3d 评审迁自 lines_api：API-2 的 CA-46① 接线与 API-4 共用同一条路由。）"""
    if not m6:
        return None
    for e in m6:
        if 'ly' not in str(e.get('species', '')).lower():
            continue
        lam0 = float(e['lambda0_rest_aa'])
        if e.get('line_frame') == 'air':
            import wavconvert
            lam0 = wavconvert.air_to_vacuum(lam0)
        f_osc = e.get('f_osc')
        return (lam0, float(f_osc) if f_osc is not None else None, e)
    return None


def absorber_mask_segs(lam, z_eff, m6=None):
    """F-103①/④：已安置系统的 Lyα 蓝侧 IGM 掩膜段（第五类，测量/积分**前**并入）。
    前置缺（M-6 无 Lyα 条目）或 z 未显式给出/≤0 ⇒ 空段（T-72① 恒等不破）。
    API-4（lines_api）与 API-2（photometry 的 CA-46① 接线）共用本函数 ⇒
    同一个 z_eff 判定、同一条 mask_hash 第 4 槽通路（F-107①）。
    （P3d 评审迁自 lines_api._absorber_mask_segs，逻辑逐字不变。）"""
    lya = lya_entry(m6)
    if lya is None or not z_eff or float(z_eff) <= 0:
        return []
    lam_a = lya[0] * (1.0 + float(z_eff))
    lo = lam_a * (1.0 - C_WING_RED_KMS / C_C_KMS)
    lo, hi = max(lo, float(np.min(lam))), lam_a
    return [[lo, hi]] if hi > lo and hi >= float(np.min(lam)) else []


def forest_stats_reasons(r_source):
    """F-102①②：forest_stats 恒 null 的**全部适用原因**（闭集四值，不得增减）。"""
    rs = ['single_sightline', 'continuum_unknown', 'lls_stochastic']
    if r_source == 'none':                       # F-69：分辨率不可知 ⇒ 本支成立
        rs.append('resolution_below_gate')
    return rs


def class_thresholds_dex():
    """F-98③：本次判档用的三个对数值（C_ABS_CLASS_* 的 log10，回显给导出件）。"""
    return {'dla': math.log10(C_ABS_CLASS_DLA),
            'sub_dla': math.log10(C_ABS_CLASS_SUBDLA),
            'lls': math.log10(C_ABS_CLASS_LLS)}


def classify_logn(logn_val):
    """F-98③ 的三档半开区间（本划分的唯一载体在规格 F-98③，本函数只消费）。
    未达 LLS 阈 ⇒ None（空值；禁止 'none'/'other' 之类字符串，T-70）。"""
    if logn_val is None:
        return None
    n = 10.0 ** float(logn_val)
    if n >= C_ABS_CLASS_DLA:
        return 'dla'
    if n >= C_ABS_CLASS_SUBDLA:
        return 'sub_dla'
    if n >= C_ABS_CLASS_LLS:
        return 'lls'
    return None


def b_pin(b_metal_cog=None, r_res=None):
    """F-99②：b 钉住的三路优先级（metal_cog > resolution_element >
    engineering_default），b_source 回显；同一 b 值只走一条误差通路（F-103③）。"""
    if b_metal_cog is not None and math.isfinite(float(b_metal_cog)) \
            and float(b_metal_cog) > 0:
        return float(b_metal_cog), 'metal_cog'
    if r_res is not None and math.isfinite(float(r_res)) and float(r_res) > 0:
        return C_C_KMS / float(r_res), 'resolution_element'
    return float(C_WING_B_KMS), 'engineering_default'


def _voigt_tau(lam, lam0_vac, z_abs, b_kms, logn, f_osc):
    """Lyα 阻尼翼的光学深度剖面 τ(v)（z/b 钉住、logN 线性标度；禁行线 19：
    z/b 永不出现在自由参表）。v = c·(λ/λ_a − 1) km/s，λ_a = λ0(1+z)。"""
    lam = np.asarray(lam, dtype=float)
    lam_a = float(lam0_vac) * (1.0 + float(z_abs))
    lam_cm = lam_a * 1e-8
    v = C_C_KMS * (lam / lam_a - 1.0)
    sig_v = float(b_kms) / math.sqrt(2.0)
    gam_v = _C_GAMMA_LYA / (4.0 * math.pi) * lam_cm / 1e5
    phi = scipy.special.voigt_profile(v, sig_v, gam_v)
    k = _SIGMA_INT_CGS * float(f_osc) * lam_cm * 1e-5
    return (10.0 ** float(logn)) * k * phi


def _wing_windows(lam, ok, lam0_vac, z_abs):
    """翼窗（Lyα 红侧 C_WING_RED_KMS）与族 A 的红侧外延带（宽 = 窗宽之半）。
    蓝侧（IGM/森林压制区）不进任何拟合残差（F-99③ 硬约束）。"""
    lam = np.asarray(lam, dtype=float)
    lam_a = float(lam0_vac) * (1.0 + float(z_abs))
    fac = C_WING_RED_KMS / C_C_KMS
    w = ok & (lam >= lam_a) & (lam <= lam_a * (1.0 + fac))
    m = ok & (lam > lam_a * (1.0 + fac)) & (lam <= lam_a * (1.0 + 1.5 * fac))
    return w, m, lam_a


def _wing_snr(lam, flux, sigma, wmask, cont_a, r_res):
    """wing_snr_res（F-68 同一口径：C/(σ_med/√n_res)；无 R ⇒ C_DLAM_OVER_FWHM_MAX
    等效分辨率元，与 lines.snr_res 同约定）。cont_a = 窗上族 A 连续谱数组。"""
    from .constants import C_DLAM_OVER_FWHM_MAX
    if not np.any(wmask):
        return None, 0
    lam_w = np.asarray(lam)[wmask]
    c_med = float(np.median(np.asarray(cont_a))) if cont_a is not None \
        else float(np.median(np.asarray(flux)[wmask]))
    s_med = float(np.median(np.asarray(sigma)[wmask]))
    n_res = (float(C_C_KMS) / float(r_res) / max(float(np.median(
        np.abs(np.diff(lam_w)))), 1e-9)
        if r_res else float(C_DLAM_OVER_FWHM_MAX))
    snr = c_med / (s_med / math.sqrt(n_res)) if s_med > 0 and c_med > 0 \
        else math.inf
    return float(snr), int(np.count_nonzero(wmask))


def _cont_fixed(marg_lam, marg_f, marg_s, win_lam, order):
    """族 A：翼窗红侧外延带上的 Cheb(order)（F-62 基）一次拟合后**固定**——
    L-51② 'for a fixed continuum fit' 的正实现。返回窗上的 C(u) 数组或 None。"""
    if marg_lam.size < max(order + 2, 4):
        return None
    a, b = float(np.log(marg_lam).min()), float(np.log(marg_lam).max()
                                                - np.log(marg_lam).min())
    if b <= 0:
        return None
    u = (2.0 * (np.log(marg_lam) - a) / b - 1.0)
    M = np.polynomial.chebyshev.chebvander(u, order) / marg_s[:, None]
    coef, *_ = np.linalg.lstsq(M, marg_f / marg_s, rcond=None)   # F-62② 禁正规方程
    uw = (2.0 * (np.log(win_lam) - a) / b - 1.0)
    return np.polynomial.chebyshev.chebvander(uw, order) @ coef


def _golden_min(chi2_fn, lo, hi, iters=45):
    """黄金分割细化 χ²(logN) 最小值（0.1 dex 网格量化不足以量 spread 阈）。"""
    gr = (math.sqrt(5.0) - 1.0) / 2.0
    a, b_ = float(lo), float(hi)
    c = b_ - gr * (b_ - a)
    d = a + gr * (b_ - a)
    fc, fd = chi2_fn(c), chi2_fn(d)
    for _ in range(iters):
        if fc < fd:
            b_, fd = d, fc
            d = c
            c = b_ - gr * (b_ - a)
            fc = chi2_fn(c)
        else:
            a, fc = c, fd
            c = d
            d = a + gr * (b_ - a)
            fd = chi2_fn(d)
    xm = 0.5 * (a + b_)
    return xm, chi2_fn(xm)


def _profile_errs(chi2_fn, log0, chi2min, span=1.0):
    """Δχ²=1 剖面（F-99③/F-28 同一判据）：步进加倍找括区 + 二分到 χ²min+1；
    ±span 无交点 ⇒ 该侧 None（区间表述降级，F-104③）。返回 (lo, hi, n_evals)。"""
    target = chi2min + 1.0
    n_evals = 0

    def cross(direction):
        nonlocal n_evals
        step = 0.02
        prev = log0
        while step <= span:
            cur = log0 + direction * step
            c = chi2_fn(cur)
            n_evals += 1
            if c >= target:
                a_, b_ = (prev, cur) if direction > 0 else (cur, prev)
                ca = chi2_fn(a_)
                n_evals += 1
                mid = cur
                for _ in range(60):
                    mid = 0.5 * (a_ + b_)
                    cm = chi2_fn(mid)
                    n_evals += 1
                    if abs(cm - target) < 1e-4:
                        return abs(mid - log0)
                    if (cm - target) * (ca - target) <= 0:
                        b_ = mid
                    else:
                        a_, ca = mid, cm
                return abs(mid - log0)
            prev = cur
            step *= 2.0
        return None
    e_lo, e_hi = cross(-1.0), cross(+1.0)
    return e_lo, e_hi, n_evals


def _adopted_logn(lam_f, f_f, s_f, lam0, z_abs, b_kms, f_osc, order):
    """族 A 自拟合半段：Cheb·lnλ 同阶 + logN 联合初拟后冻结连续谱。
    返回 (logn_refined, coef, D)；wing_logn_two_families 与 b_envelope 共用。"""
    ln = np.log(lam_f)
    u = 2.0 * (ln - ln.min()) / max(ln.max() - ln.min(), 1e-12) - 1.0
    D = np.polynomial.chebyshev.chebvander(u, order)
    grid = np.arange(17.0, 22.5001, 0.1)

    def solve(lg):
        et = np.exp(-_voigt_tau(lam_f, lam0, z_abs, b_kms, lg, f_osc))
        coef, *_ = np.linalg.lstsq(D * et[:, None], f_f / s_f, rcond=None)
        r = f_f / s_f - (D * et[:, None]) @ coef        # 白化残差
        return float(np.sum(r * r)), coef
    chi2 = np.array([solve(float(lg))[0] for lg in grid])
    j = int(np.argmin(chi2))
    lg, _c = _golden_min(lambda x: solve(float(x))[0],
                         grid[max(j - 1, 0)], grid[min(j + 1, grid.size - 1)])
    _chi, coef = solve(float(lg))
    return float(lg), coef, D


def wing_logn_two_families(lam, flux, sigma, *, z_abs, b_kms,
                           lambda_rest_lya_vac_aa, f_osc, cont_order=1,
                           ok=None, budget_left=None, cont_a_override=None):
    """F-99/F-100：同一份翼数据、钉住同一组 z/b，两种连续谱族各拟合一次。
    族 A = 当前选定连续谱模型（cont_a_override 供给，缺省 = 拟合域〔翼窗∪红侧
    外延带〕自拟一次后冻结——L-51② 'fixed continuum fit'），自由参只 logN；
    族 B = lnλ 同阶 Cheb poly，剖面每点联合重拟连续谱。z/b 永不在自由参表
    （禁行线 19）；蓝侧不进残差（F-99③）；spread > C_WING_SPREAD_DEX ⇒ CA-45；
    剖面求值计入 C_BOOT_N_BUDGET 共享池（超池回落 + 回显，F-95③）。
    """
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    ok = (np.isfinite(lam) & np.isfinite(flux) & np.isfinite(sigma)
          & (sigma > 0)) if ok is None else np.asarray(ok, dtype=bool)
    w, m, lam_a = _wing_windows(lam, ok, lambda_rest_lya_vac_aa, z_abs)
    out = {'cont_family_a': 'selected_fixed',
           'cont_family_b': 'poly_lnlambda_refit',
           'wing_logn': None, 'wing_logn_err_lo': None,
           'wing_logn_err_hi': None, 'wing_logn_alt': None,
           'wing_logn_alt_err_lo': None, 'wing_logn_alt_err_hi': None,
           'wing_spread_dex': None, 'free_params_a': ['logN'],
           'free_params_b': ['c%d' % i for i in range(cont_order + 1)]
                           + ['logN'],
           'n_par_a': 1, 'n_par_b': cont_order + 2, 'n_profile_evals': 0,
           'boot_budget_applied': False, 'ca44': None, 'verdict': 'ok'}
    if int(np.count_nonzero(w)) < 12 or int(np.count_nonzero(m)) < max(
            cont_order + 2, 4):
        out['verdict'] = 'wing_window_insufficient'
        out['reason'] = ('翼窗或其红侧外延带可用像素不足（蓝侧不进残差，F-99③），'
                         '两族四个端点全空值 + 本书面原因（F-94③）')
        return out
    fit = w | m                                     # 翼窗 + 红侧外延带
    lam_f, f_f, s_f = lam[fit], flux[fit], sigma[fit]
    ln = np.log(lam_f)
    u_b = 2.0 * (ln - ln.min()) / max(ln.max() - ln.min(), 1e-12) - 1.0
    D_b = np.polynomial.chebyshev.chebvander(u_b, cont_order)
    grid = np.arange(17.0, 22.5001, 0.1)
    need = 400                    # 双族剖面求值实测 ~400（0.1 dex 网格 + 黄金分割细化）
    if budget_left is not None and budget_left < need:
        out['verdict'] = 'profile_budget_exhausted'
        out['reason'] = ('剖面求值预算（C_BOOT_N_BUDGET 共享池）不足以完成双族 '
                         'Δχ²=1 剖面：两族四端点全空值 + 本书面原因（F-95③）')
        out['boot_budget_applied'] = True
        return out
    out['n_profile_evals'] += int(grid.size)

    def _solve(lg, D):
        # 模型 F = C(u)·exp(−τ)：τ 给定后对连续谱系数线性（白化残差 lstsq，F-62②）
        et = np.exp(-_voigt_tau(lam_f, lambda_rest_lya_vac_aa, z_abs, b_kms,
                                lg, f_osc))
        coef, *_ = np.linalg.lstsq(D * et[:, None], f_f / s_f, rcond=None)
        return float(np.sum((f_f / s_f - (D * et[:, None]) @ coef) ** 2))

    # ── 族 A：当前选定模型（供给或自拟合后冻结；剖面只扫 logN）；族 B：lnλ
    # poly 剖面每点联合重拟连续谱。两族各走「网格 argmin → 黄金分割细化 →
    # Δχ²=1 二分剖面」同一三步（F-99③/F-28 同一判据）。 ──
    if cont_a_override is not None:
        cont_a = np.asarray(cont_a_override, dtype=float)
        if cont_a.shape != lam_f.shape:
            out['verdict'] = 'wing_window_insufficient'
            out['reason'] = 'cont_a_override 形状与拟合域不符'
            return out
    else:
        _lg0, coef_a, D_a = _adopted_logn(lam_f, f_f, s_f,
                                          lambda_rest_lya_vac_aa, z_abs,
                                          b_kms, f_osc, cont_order)
        cont_a = (D_a @ coef_a) * s_f               # 冻结（flux 单位，fixed continuum）

    def _family(D, cont_fixed, tag):
        if cont_fixed is None:
            fn = lambda lg: _solve(float(lg), D)
        else:
            fn = lambda lg: float(np.sum(((f_f - cont_fixed * np.exp(
                -_voigt_tau(lam_f, lambda_rest_lya_vac_aa, z_abs, b_kms, lg,
                            f_osc))) / s_f) ** 2))
        chi2 = np.array([fn(float(lg)) for lg in grid])
        j = int(np.argmin(chi2))
        lg, cmin = _golden_min(fn, grid[max(j - 1, 0)],
                               grid[min(j + 1, grid.size - 1)])
        e_lo, e_hi, n_e = _profile_errs(fn, lg, cmin)
        out['n_profile_evals'] += int(grid.size) + n_e + 50
        out['wing_logn' + tag] = lg
        out['wing_logn%s_err_lo' % tag] = e_lo
        out['wing_logn%s_err_hi' % tag] = e_hi
        out['chi2min' + tag] = cmin
        return lg
    lg_a = _family(D_b, cont_a, '')
    lg_b = _family(D_b, None, '_alt')
    out['wing_spread_dex'] = abs(lg_a - lg_b)
    # F-101③：恰为 0 的区间端点 = 过拟合哨兵 ⇒ CA-44，绝不当作"无误差"
    for k in ('wing_logn_err_lo', 'wing_logn_err_hi', 'wing_logn_alt_err_lo',
              'wing_logn_alt_err_hi'):
        if out[k] is not None and out[k] <= 1e-9:
            out['ca44'] = ('%s 恰为 0：过拟合信号（the fit will not converge '
                           'properly and uncertainties of zero are returned，'
                           'L-59①/F-101③），绝不当作无误差' % k)
    return out


def absorber_ident_numeric(lam, flux, sigma, *, z_abs, lambda_rest_lya_vac_aa,
                           f_osc, b_kms, r_res=None, cont_order=1, ok=None,
                           do_fit=True, budget_left=None):
    """F-98①②④ + F-99③ 的门与三档：检出显著性、wing_snr_res、
    intervening_or_host、不拟合分支的书面原因。返回 dict（检出/信噪/红侧判定
    + wing_logn_two_families 的结果或未拟合原因）。do_fit=False（U-48 只开
    absorber_ident）⇒ 识别照跑、翼拟合不执行。"""
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    ok = (np.isfinite(lam) & np.isfinite(flux) & np.isfinite(sigma)
          & (sigma > 0)) if ok is None else np.asarray(ok, dtype=bool)
    w, m, lam_a = _wing_windows(lam, ok, lambda_rest_lya_vac_aa, z_abs)
    out = {'detected': None, 'detect_sigma': None, 'wing_snr_res': None,
           'wing_n_pixels': 0, 'intervening_or_host': 'undetermined',
           'wing_fit': None, 'verdict': 'ok'}
    if int(np.count_nonzero(w)) < 12:
        out['verdict'] = 'lya_out_of_coverage'
        out['reason'] = ('Lyα(1+z)=%.2f Å 的红侧翼窗不在这条谱的覆盖内'
                         '（可用像素 %d < 12）' % (lam_a, int(np.count_nonzero(w))))
        return out
    marg = _cont_fixed(lam[m], flux[m], sigma[m], lam[w], cont_order)
    snr, npix = _wing_snr(lam, flux, sigma, w, marg, r_res)
    out['wing_snr_res'], out['wing_n_pixels'] = snr, npix
    # F-98②：检出显著性（只回答"算不算被检出"）
    if marg is not None:
        d = (marg - flux[w]) / marg
        s_sig = math.sqrt(float(np.sum((sigma[w] / marg) ** 2))
                          / max(np.count_nonzero(w), 1))
        dsig = float(np.mean(d)) / s_sig if s_sig > 0 else 0.0
        out['detect_sigma'] = dsig
        out['detected'] = bool(dsig >= C_ABS_DETECT_SIGMA)
    # F-98④：红侧有没有森林线（翼窗外 [窗端, +2e4 km/s) 的**窄**凹陷计数——
    # 二阶差分判据：阻尼翼在数十像素尺度上光滑（二阶差分 ≲1σ），窄线（~3 px）
    # 凹陷给出 |二阶差分| ≫ √6·σ；阈 −10（≈4.1σ，2400 px 误报期望 ≪1）。
    lam_a2 = lam_a * (1.0 + 1.5 * C_WING_RED_KMS / C_C_KMS)
    lam_a3 = lam_a * (1.0 + 20000.0 / C_C_KMS)
    r = ok & (lam >= lam_a2) & (lam <= lam_a3)
    if int(np.count_nonzero(r)) >= 30:
        fg = flux[r]
        sg = sigma[r]
        d2 = (fg[:-2] - 2.0 * fg[1:-1] + fg[2:]) / sg[1:-1]
        dips = int(np.sum(d2 < -10.0))
        out['intervening_or_host'] = 'intervening' if dips else 'host'
    # F-99③：wing_snr_res < C_WING_FIT_MIN_SNR ⇒ 不拟合（两族四端点全空 +
    # 书面原因，禁止外推，T-70 末支）
    if snr is not None and snr < C_WING_FIT_MIN_SNR:
        out['verdict'] = 'wing_snr_below_gate'
        out['reason'] = ('wing_snr_res=%.1f < C_WING_FIT_MIN_SNR=%s：不拟合，'
                         'wing_logn 两族与四个区间端点全空值 + 本书面原因，'
                         '禁止外推（F-99③/TXT-23）' % (snr, C_WING_FIT_MIN_SNR))
        return out
    out['wing_fit'] = wing_logn_two_families(
        lam, flux, sigma, z_abs=z_abs, b_kms=b_kms,
        lambda_rest_lya_vac_aa=lambda_rest_lya_vac_aa, f_osc=f_osc,
        cont_order=cont_order, ok=ok,
        budget_left=budget_left) if do_fit else None
    return out


def cross_link_band_fracs(lam, mask_bool, band_defs):
    """F-103①：absorber_system_masked 折算到每个波段的通带被掩占比。判定量
    **复用 S1 的算术口径**（band_integrals 的 sel/keep 同式）：frac =
    n_masked/(n_masked+n_used)。band_defs = [(band, curve_lam, curve_tr)]。"""
    lam = np.asarray(lam, dtype=float)
    mask_bool = np.asarray(mask_bool, dtype=bool)
    out = []
    for band, cl, ct in band_defs:
        plo, phi = float(cl[0]), float(cl[-1])
        sel = (lam >= plo) & (lam <= phi)
        n_band = int(np.count_nonzero(sel))
        keep = ~mask_bool[sel]
        n_used = int(np.count_nonzero(keep))
        n_masked = n_band - n_used
        out.append({'band': band, 'bands_masked_frac':
                    (n_masked / n_band if n_band else 0.0),
                    'n_masked_pixels': n_masked, 'n_used_pixels': n_used})
    return out


def b_envelope(lam, flux, sigma, *, z_abs, lambda_rest_lya_vac_aa, f_osc,
               cont_order=1, ok=None, budget_left=None):
    """F-104④：按 C_WING_B_GRID_KMS 网格各跑一次固定-b 重拟合（与族 A 同一
    估计量：_adopted_logn 联合初拟 + 黄金分割细化），log N 包络宽度随 b 单调
    （b 越小翼越矮 ⇒ 同深度需更大 N）。**只作灵敏度展示**：不进 err_source、
    不当误差项（§3.9.3.1 wing_spread_dex 行同纪律）。三次重拟合计入
    C_BOOT_N_BUDGET 同一预算池（F-95③）：余量不足 ⇒ 回落不出 + 书面原因
    （T-74⑤）。返回 (rows, reason)；rows = [{'b_kms', 'wing_logn'}]。"""
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    ok = (np.isfinite(lam) & np.isfinite(flux) & np.isfinite(sigma)
          & (sigma > 0)) if ok is None else np.asarray(ok, dtype=bool)
    w, m, lam_a = _wing_windows(lam, ok, lambda_rest_lya_vac_aa, z_abs)
    if int(np.count_nonzero(w)) < 12 or int(np.count_nonzero(m)) < max(
            cont_order + 2, 4):
        return [], '翼窗/外延带像素不足，b 包络不出（不外推）'
    need = 3 * 110                   # 每 b：0.1 dex 网格 + 黄金分割（~102 次实测）
    if budget_left is not None and budget_left < need:
        return [], ('b 包络剖面求值需约 %d 次，超出 C_BOOT_N_BUDGET 共享池余量 '
                    '%d：按 F-95③ 回落不出并回显（F-104④/T-74⑤）'
                    % (need, budget_left))
    lam_f, f_f, s_f = lam[w | m], flux[w | m], sigma[w | m]
    rows = []
    for b_kms in C_WING_B_GRID_KMS:
        lg, _coef, _D = _adopted_logn(lam_f, f_f, s_f, lambda_rest_lya_vac_aa,
                                      z_abs, float(b_kms), f_osc, cont_order)
        rows.append({'b_kms': float(b_kms), 'wing_logn': lg})
    return rows, None


def z_envelope(lam, flux, sigma, *, z_abs, z_err, lambda_rest_lya_vac_aa,
               f_osc, b_kms, cont_order=1, ok=None, budget_left=None):
    """F-99① 的 z 窗口灵敏度回显（P3d 评审最小实现）：z 在金属线测得的
    z_abs ± C_WING_Z_WIN_SIGMA·σ_z 窗口内取三点（下端/钉住值/上端，下端钳 0）
    各重拟一次 logN——与族 A 同一估计量（_adopted_logn 联合初拟 + 黄金分割
    细化），spread = max−min 回显为 logn_spread_over_z。**只作灵敏度展示**：
    不进 err_source、不是 z 的误差（z_err 另走 covariance 一条，F-103③）；
    三次重拟合计入 C_BOOT_N_BUDGET 同一预算池（F-95③）：余量不足 ⇒ 回落不出
    + 书面原因。返回 (rows, reason)；rows = [{'z_abs', 'wing_logn'}]（z 升序）。"""
    if z_err is None or not (math.isfinite(float(z_err))
                             and float(z_err) > 0):
        return [], ('z_err 不可得（user_z 路由或金属线行缺 λ 误差）：'
                    '±C_WING_Z_WIN_SIGMA·σ_z 窗口无宽度，z 窗口灵敏度不出'
                    '（F-99①）')
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    ok = (np.isfinite(lam) & np.isfinite(flux) & np.isfinite(sigma)
          & (sigma > 0)) if ok is None else np.asarray(ok, dtype=bool)
    w, m, lam_a = _wing_windows(lam, ok, lambda_rest_lya_vac_aa, z_abs)
    if int(np.count_nonzero(w)) < 12 or int(np.count_nonzero(m)) < max(
            cont_order + 2, 4):
        return [], '翼窗/外延带像素不足，z 窗口灵敏度不出（不外推）'
    need = 3 * 110                   # 每 z：0.1 dex 网格 + 黄金分割（~102 次实测）
    if budget_left is not None and budget_left < need:
        return [], ('z 窗口剖面求值需约 %d 次，超出 C_BOOT_N_BUDGET 共享池余量 '
                    '%d：按 F-95③ 回落不出并回显（F-99①）'
                    % (need, budget_left))
    lam_f, f_f, s_f = lam[w | m], flux[w | m], sigma[w | m]
    dz = C_WING_Z_WIN_SIGMA * float(z_err)
    rows = []
    for z in (max(float(z_abs) - dz, 0.0), float(z_abs), float(z_abs) + dz):
        lg, _coef, _D = _adopted_logn(lam_f, f_f, s_f, lambda_rest_lya_vac_aa,
                                      z, float(b_kms), f_osc, cont_order)
        rows.append({'z_abs': float(z), 'wing_logn': lg})
    return rows, None


def absorber_gate_entry(**over):
    """F-98/F-105① 的系统格装配：§4.2 键集恒在场、值键缺省 null/空集
    （F-94③ none 语义；T-72① 同纪律），调用方按状态覆写。"""
    e = dict.fromkeys((
        'system_id', 'z_abs', 'z_err', 'z_source', 'class',
        'class_thresholds_dex', 'intervening_or_host', 'lambda_rest_lya_vac_aa',
        'wing_logn', 'wing_logn_err_lo', 'wing_logn_err_hi', 'wing_logn_alt',
        'wing_logn_alt_err_lo', 'wing_logn_alt_err_hi', 'wing_spread_dex',
        'wing_b_assumption_kms', 'b_source', 'cont_family_a', 'cont_family_b',
        'wing_snr_res', 'wing_n_pixels', 'blue_side_igm_masked',
        'n_lines_in_masked_absorbers', 'sat_flag', 'detect_sigma', 'reason',
        # logn_spread_over_z：F-99① z 窗口灵敏度回显（追加键，只作灵敏度展示；
        # 键恒在场、未算出为 null——F-94④ 键集纪律，登记 docs/TECHNICAL.md）
        'logn_spread_over_z'))
    e.update({'metal_line_ids': [], 'metal_sat_flags': [], 'masked_ranges': [],
              'notes': [], 'err_source': {}, 'err_scope': {}, 'warnings': []})
    e.update(over)
    return e
