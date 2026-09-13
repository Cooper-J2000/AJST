"""M5: 伪玻尔兹曼光变（pseudo-bolometric light curve；设计方案 §3-M5）。

对每个历元调用 sedfit.builder.build_sed 取 SED 点，产出三个量：
  L_obs — 观测系 3000–24000 Å（≈U–K）窗口内 F_λ 梯形积分 × 4πD_L²
          （Nicholl 2018 定义；不做外推、不做 k 改正；窗口外的射电/X 射线
          点不纳入，以免跨频率空隙的梯形面积虚高光度；实际积分覆盖随结果
          记录于 lambda_lobs_range_a）；
  L_bb  — 单黑体快速拟合（χ² 网格 T + 线性最小二乘解 (R/D)²，不跑 MCMC，
          速度优先）外推到 0–∞：L_bb = 4πR²σT⁴。仅 z 可用且光学窗口
          （1500–30000 Å）内 ≥3 个探测波段时给出；
  L_bc  — Lyman, Bersier & James 2014 (MNRAS 437, 3848) 颜色→BC 经验关系
          交叉检验（系数见 _BC_FITS，逐条来自该文 arXiv:1311.1946 源文件
          Eqs. 3–4 与 Tables 2–4，未经二手转录）。BC_x = M_bol − M_x
          （x 为颜色前项波段，Vega 系统；BC 含 UV 贡献的全玻尔兹曼改正）。
          颜色超出拟合范围时仍计算但标记 bc_extrapolated 并告警。

无 z 时：L_obs/L_bb/L_bc 均为 None 并在 warnings 注明（§4.3 约定）。
"""
import math

import numpy as np

from models import Transient, FilterDef, distance_modulus
from sedfit import builder as sed_builder
from sedfit.models import planck_nu

_C_AA_PER_S = 2.99792458e18     # 光速 [Å/s]
_MJY_CGS = 1e-26                # 1 mJy = 1e-26 erg/s/cm²/Hz
_PC_CM = 3.085677581e18
_SIGMA_SB = 5.670374419e-5      # [erg/cm²/s/K⁴]
_L_SUN_BOL = 3.828e33           # 太阳玻尔兹曼光度 [erg/s]（IAU 2015）
_M_SUN_BOL = 4.74               # 太阳玻尔兹曼绝对星等
_LN10 = math.log(10.0)
_AB_ZP_MJY = 16.4               # AB 零点（mJy 制，AJST 全局唯一约定）

# 黑体拟合的观测系光学窗口（Å）：窗口外的射电/X 射线点不参与黑体拟合
_BB_WL_MIN_A, _BB_WL_MAX_A = 1500.0, 30000.0

# L_obs 积分的默认观测系窗口（Å，≈U–K）：pseudo-bolometric 的本义即
# 只积光学/NIR 覆盖段；窗口外的射电/X 点若纳入梯形积分，跨越巨大
# 频率空隙的梯形面积会严重虚高光度
_LOBS_WL_MIN_A, _LOBS_WL_MAX_A = 3000.0, 24000.0

# ─── Lyman, Bersier & James 2014 (MNRAS 437, 3848) BC 系数表 ───
# 来源：该文 arXiv:1311.1946 LaTeX 源文件（Tables 2/3/4 与 Eqs. 3/4 原文数值）。
# BC_x = c0 + c1·(x−y) + c2·(x−y)²（Vega 系统，颜色与星等均需消光改正）。
# 每项：(颜色前项波段 x, c0, c1, c2, rms, 颜色下限, 颜色上限)
# 'all' 为全样本拟合（辐射/复合供能阶段，Eqs. 3–4）；'se'/'ii' 为该文
# Table 2/3 的分类型拟合；'cooling' 为 SBO 冷却阶段拟合（Table 4，两类共用）。
_BC_FITS = {
    'all': {
        'B-I': ('B', -0.057, -0.219, -0.169, 0.053, -0.4, 2.8),
        # g−r 颜色上限在论文正文抽取中被截断；上限取 SE/II 样本范围并集 1.3
        'g-r': ('g', 0.055, -0.219, -0.629, 0.070, -0.3, 1.3),
    },
    'se': {  # SE SNe（Ib/Ic/IIb），Table 2 BC 列
        'B-I': ('B', -0.055, -0.240, -0.154, 0.061, -0.4, 2.3),
        'B-V': ('B', -0.083, -0.139, -0.691, 0.109, 0.0, 1.3),
        'g-r': ('g', 0.054, -0.195, -0.719, 0.076, -0.3, 1.0),
    },
    'ii': {  # SNe II，Table 3 BC 列
        'B-I': ('B', 0.004, -0.297, -0.149, 0.026, 0.0, 2.8),
        'B-V': ('B', -0.138, -0.013, -0.649, 0.094, 0.0, 1.6),
        'g-r': ('g', 0.053, -0.089, -0.736, 0.036, -0.2, 1.3),
    },
    'cooling': {  # SBO 冷却阶段，Table 4（两类共用，仅 BC）
        'B-I': ('B', -0.473, 0.830, -1.064, 0.072, -0.2, 0.8),
        'B-V': ('B', -0.393, 0.786, -2.124, 0.089, -0.2, 0.5),
        'g-r': ('g', -0.146, 0.479, -2.257, 0.078, -0.3, 0.3),
    },
}
_BC_REF = 'Lyman, Bersier & James 2014, MNRAS 437, 3848 (arXiv:1311.1946)'
# 颜色取用优先级（B−I 为该文散度最小的 Johnson 系关系）
_BC_COLOR_PRIORITY = ('B-I', 'B-V', 'g-r')

# Lyman 关系所需波段的 id 别名 + 有效波长窗口（Å）双重匹配，
# 防止 id 撞名（如 'v'/'R' 大小写、Rc 等近亲波段混入）
_BAND_MATCH = {
    'B': ({'B', 'b', 'bessell-b', 'bessell_b', 'johnson-b'}, (4100.0, 4700.0)),
    'V': ({'V', 'v', 'bessell-v', 'bessell_v', 'johnson-v'}, (5200.0, 5800.0)),
    'g': ({'g', 'sdss-g', "g'", 'ztf-g', 'ps1-g', 'sloan-g'}, (4300.0, 5100.0)),
    'r': ({'r', 'sdss-r', "r'", 'ztf-r', 'ps1-r', 'sloan-r'}, (5900.0, 6800.0)),
    # 目录中 'I' 有效波长 8657 Å（比 Bessell I 略红，窗口放宽至 9000 Å；
    # 相对 Lyman+2014 的 I 带零点有小的系统差，属可接受近似）
    'I': ({'I', 'bessell-i', 'bessell_i', 'cousins-i', 'johnson-i'},
          (7500.0, 9000.0)),
}


def _dl_cm(z):
    """光度距离 [cm]（Planck18，复用 models.distance_modulus 的分桶缓存）"""
    mu = distance_modulus(z)
    if mu is None:
        return None
    return 10.0 ** (mu / 5.0 + 1.0) * _PC_CM


def _integrate_obs(det, dl):
    """观测波段内 F_λ 梯形积分 → (L_obs, L_obs_err) [erg/s]。

    det：探测点列表（含 wavelength_a/f_mjy/ferr_mjy）。误差按各点半宽权重
    独立传播（trapezoid 权重 w_i = 0.5·(λ_{i+1}−λ_{i−1})，端点取半宽）。"""
    pts = sorted(det, key=lambda p: p['wavelength_a'])
    lam = np.array([p['wavelength_a'] for p in pts], dtype=float)
    flam = np.array([p['f_mjy'] * _MJY_CGS * (_C_AA_PER_S / p['wavelength_a']) ** 2
                     / _C_AA_PER_S for p in pts])  # = Fν·ν²/c [erg/s/cm²/Å]
    flam_err = np.array([p['ferr_mjy'] * _MJY_CGS
                         * (_C_AA_PER_S / p['wavelength_a']) ** 2 / _C_AA_PER_S
                         for p in pts])
    _trapz = getattr(np, 'trapezoid', None) or np.trapz  # numpy 2 / 1.x 兼容
    flux = float(_trapz(flam, lam))  # [erg/s/cm²]
    n = len(pts)
    if n >= 2:
        w = np.empty(n)
        w[1:-1] = 0.5 * (lam[2:] - lam[:-2])
        w[0] = 0.5 * (lam[1] - lam[0])
        w[-1] = 0.5 * (lam[-1] - lam[-2])
        flux_err = float(np.sqrt(np.sum((flam_err * w) ** 2)))
    else:
        flux_err = float('nan')
    area = 4.0 * math.pi * dl * dl
    return flux * area, flux_err * area


def _fit_bb_fast(det, dl):
    """单黑体快速拟合：T 的 χ² 网格 + (R/D)² 线性最小二乘（不跑 MCMC）。

    返回 dict（T/R/L 及近似 1σ、χ²、dof）；波段 <3 时返回 None。
    σ_T 取 Δχ²=1 网格包络（近似），σ_R 由固定 T 下 s=(R/D)² 的最小二乘
    方差传播，σ_L 忽略 T–s 协方差（ln L = ln s + 4 ln T + const）。"""
    opt = [p for p in det
           if _BB_WL_MIN_A <= p['wavelength_a'] <= _BB_WL_MAX_A]
    if len({p['band'] for p in opt}) < 3:
        return None
    nu = np.array([p['nu_hz'] for p in opt], dtype=float)
    f = np.array([p['f_mjy'] * _MJY_CGS for p in opt])
    ferr = np.array([max(p['ferr_mjy'] * _MJY_CGS, 1e-30) for p in opt])
    w = 1.0 / ferr ** 2

    Ts = np.logspace(math.log10(2.0e3), math.log10(3.0e5), 240)
    best = None  # (chi2, T, s, s_err)
    chi2s = []
    for T in Ts:
        m = math.pi * planck_nu(nu, T)  # ×s 即模型流量
        s = float(np.sum(w * f * m) / np.sum(w * m * m))
        if s <= 0:
            chi2s.append(np.inf)
            continue
        chi2 = float(np.sum(w * (f - m * s) ** 2))
        chi2s.append(chi2)
        if best is None or chi2 < best[0]:
            best = (chi2, float(T), s)
    if best is None or not np.isfinite(best[0]):
        return None
    chi2_min, T_best, s_best = best
    # Δχ²=1 包络近似 σ_T（网格分辨率内）
    i0 = int(np.argmin(chi2s))
    lo, hi = i0, i0
    while lo > 0 and chi2s[lo] < chi2_min + 1.0:
        lo -= 1
    while hi < len(Ts) - 1 and chi2s[hi] < chi2_min + 1.0:
        hi += 1
    T_err = max((Ts[hi] - Ts[lo]) / 2.0, Ts[min(i0 + 1, len(Ts) - 1)] - Ts[i0])
    m_best = math.pi * planck_nu(nu, T_best)
    s_err = float(math.sqrt(1.0 / np.sum(w * m_best * m_best)))

    R = math.sqrt(s_best) * dl
    R_err = 0.5 * R * s_err / s_best
    L = 4.0 * math.pi * R ** 2 * _SIGMA_SB * T_best ** 4
    lnL_err = math.sqrt((s_err / s_best) ** 2 + (4.0 * T_err / T_best) ** 2)
    return {'T_bb': T_best, 'T_bb_err': float(T_err),
            'R_bb': R, 'R_bb_err': R_err,
            'L_bb': L, 'L_bb_err': L * lnL_err,
            'chi2_bb': chi2_min, 'dof_bb': len(opt) - 2,
            'n_bb_points': len(opt),
            'bb_at_grid_edge': bool(i0 == 0 or i0 == len(Ts) - 1)}


def _match_band(band, wl_a):
    """波段 id + 有效波长双重匹配 → Lyman 标准波段名（'B'/'V'/'g'/'r'/'I'）"""
    for std, (aliases, (lo, hi)) in _BAND_MATCH.items():
        if band in aliases and lo <= wl_a <= hi:
            return std
    return None


def _compute_bc(det, filters, mu, bc_sample):
    """Lyman+2014 颜色→BC。返回 dict 或 None（颜色不可得/样本未知）。"""
    fits = _BC_FITS.get(bc_sample)
    if fits is None:
        return None
    mags = {}  # 标准波段 -> (m_vega, sigma_mag)
    for p in det:
        std = _match_band(p['band'], p['wavelength_a'])
        if std is None or std in mags:
            continue
        filt = filters.get(p['band'])
        vega2ab = float(filt.vega2ab) if filt and filt.vega2ab else 0.0
        m_ab = _AB_ZP_MJY - 2.5 * math.log10(p['f_mjy'])
        sig = ((2.5 / _LN10) * p['ferr_mjy'] / p['f_mjy']
               if p['ferr_mjy'] and p['f_mjy'] > 0 else 0.1)
        mags[std] = (m_ab - vega2ab, sig)
    for color in _BC_COLOR_PRIORITY:
        if color not in fits:
            continue
        x_band, c0, c1, c2, rms, lo, hi = fits[color]
        b1, b2 = color.split('-')
        if b1 not in mags or b2 not in mags:
            continue
        col = mags[b1][0] - mags[b2][0]
        col_err = math.sqrt(mags[b1][1] ** 2 + mags[b2][1] ** 2)
        bc = c0 + c1 * col + c2 * col * col
        bc_err = math.sqrt(rms ** 2 + ((c1 + 2.0 * c2 * col) * col_err) ** 2)
        m_x = mags[x_band][0] - mu
        m_bol = m_x + bc
        L = _L_SUN_BOL * 10.0 ** (0.4 * (_M_SUN_BOL - m_bol))
        L_err = L * 0.4 * _LN10 * bc_err
        return {'L_bc': L, 'L_bc_err': L_err, 'bc_color': color,
                'bc_color_value': col, 'bc_value': bc, 'bc_err': bc_err,
                'bc_sample': bc_sample, 'bc_ref': _BC_REF,
                'bc_extrapolated': bool(col < lo or col > hi),
                'bc_color_range': [lo, hi]}
    return None


def pseudo_bolometric(sess, transient_id, epochs=None, mode='window',
                      dt_frac=0.1, z_override=None, bc_sample='all',
                      max_epochs=30):
    """伪玻尔兹曼光变主函数（同步）。

    epochs：t_days 列表（相对 t0，天）；None 时由 suggest_epochs 自动建议
    （min_bands=3, dt_frac 同窗口），上限 max_epochs 个。
    返回 {transient_id, z, z_source, bc_sample, epochs: [...], warnings}
    （历元按时间升序；每历元 λ 覆盖与是否外推随结果记录，§4.4）。"""
    if bc_sample not in _BC_FITS:
        raise ValueError(f"bc_sample 必须为 {sorted(_BC_FITS)} 之一")
    t = sess.get(Transient, transient_id)
    if t is None:
        raise ValueError(f'暂现源不存在: {transient_id}')
    if z_override is not None:
        z, z_source = float(z_override), 'override'
    elif t.redshift:
        z, z_source = float(t.redshift), 'catalog'
    else:
        z, z_source = None, None
    dl = _dl_cm(z) if z else None

    top_warnings = []
    if epochs is None:
        sugg = sed_builder.suggest_epochs(sess, transient_id, min_bands=3,
                                          dt_frac=dt_frac,
                                          max_epochs=int(max_epochs))
        epochs = sorted(e['t_days'] for e in sugg)
    else:
        epochs = sorted(float(e) for e in epochs)
    if len(epochs) > int(max_epochs):
        top_warnings.append(f'历元数 {len(epochs)} 超过上限 {max_epochs}，已截断')
        epochs = epochs[:int(max_epochs)]
    if not epochs:
        top_warnings.append('无满足条件的历元（需同一窗口内 ≥3 探测波段）')
    if z is None:
        top_warnings.append('无红移：L_obs/L_bb/L_bc 均不可算（仅有 λ 覆盖元数据），'
                            '建议录入宿主红移或用 z_override')

    filters = {f.id: f for f in sess.query(FilterDef).all()}
    mu = distance_modulus(z) if z else None

    out_epochs = []
    for t_days in epochs:
        ew = []
        rec = {'t_days': t_days, 'n_bands': 0,
               'lambda_min_a': None, 'lambda_max_a': None,
               'lambda_lobs_range_a': None,
               'L_obs': None, 'L_obs_err': None,
               'T_bb': None, 'T_bb_err': None, 'R_bb': None, 'R_bb_err': None,
               'L_bb': None, 'L_bb_err': None, 'chi2_bb': None,
               'L_bc': None, 'L_bc_err': None, 'bc_color': None,
               'bc_extrapolated': None,
               'warnings': ew}
        sed = sed_builder.build_sed(sess, transient_id, t_days,
                                    dt=dt_frac * t_days, mode=mode,
                                    z_override=z_override)
        det = [p for p in sed['points'] if not p['is_ul']]
        rec['n_bands'] = len({p['band'] for p in det})
        rec['lambda_min_a'] = sed['meta']['lambda_min_a']
        rec['lambda_max_a'] = sed['meta']['lambda_max_a']
        ew.extend(sed['meta']['warnings'])
        if not det:
            ew.append('该历元无探测点，已跳过')
            out_epochs.append(rec)
            continue
        # λ 覆盖可信度（不做任何外推，只标注覆盖不足的风险）
        lam_min, lam_max = rec['lambda_min_a'], rec['lambda_max_a']
        if lam_min is not None and lam_min > 4000:
            ew.append(f'λ 覆盖缺少蓝端（λmin={lam_min:.0f} Å > 4000 Å）：'
                      'L_obs 明显低估，L_bb 靠瑞利-金斯侧约束')
        if lam_max is not None and lam_max < 8000:
            ew.append(f'λ 覆盖缺少红端（λmax={lam_max:.0f} Å < 8000 Å）：'
                      'L_obs 低估，红外贡献未计入')

        if dl is None:
            ew.append('无红移：本历元 L_obs/L_bb/L_bc 均为 None')
            out_epochs.append(rec)
            continue

        rec['L_obs'], rec['L_obs_err'] = (None, None)
        det_obs = [p for p in det
                   if _LOBS_WL_MIN_A <= p['wavelength_a'] <= _LOBS_WL_MAX_A]
        rec['lambda_lobs_range_a'] = (
            [min(p['wavelength_a'] for p in det_obs),
             max(p['wavelength_a'] for p in det_obs)] if det_obs else None)
        n_excl = len(det) - len(det_obs)
        if n_excl:
            ew.append(f'{n_excl} 个探测点在 L_obs 积分窗口'
                      f'（{_LOBS_WL_MIN_A:.0f}–{_LOBS_WL_MAX_A:.0f} Å）之外'
                      '（射电/X 射线等），未纳入积分')
        if len(det_obs) < 2:
            ew.append('L_obs 积分窗口内探测点 <2，L_obs 为 None')
        else:
            rec['L_obs'], rec['L_obs_err'] = _integrate_obs(det_obs, dl)

        bb = _fit_bb_fast(det, dl)
        if bb is None:
            ew.append(f'光学窗口（{_BB_WL_MIN_A:.0f}–{_BB_WL_MAX_A:.0f} Å）内'
                      '探测波段 <3 个，黑体不可拟合')
        else:
            rec.update({k: bb[k] for k in
                        ('T_bb', 'T_bb_err', 'R_bb', 'R_bb_err',
                         'L_bb', 'L_bb_err', 'chi2_bb')})
            if bb['bb_at_grid_edge']:
                ew.append(f'黑体温度触及网格边界（T={bb["T_bb"]:.0f} K），'
                          '结果可能不可信')
            if not 3e3 <= bb['T_bb'] <= 5e4:
                ew.append(f'T_bb={bb["T_bb"]:.0f} K 超出 SN 文献常见域 '
                          '3e3–5e4 K，请检查是否适合黑体解释')

        bc = _compute_bc(det, filters, mu, bc_sample)
        if bc is None:
            ew.append(f'Lyman+2014 所需颜色（{"/".join(_BC_COLOR_PRIORITY)}）'
                      f'不可得或样本 {bc_sample} 无对应拟合，L_bc 为 None')
        else:
            rec.update({k: bc[k] for k in
                        ('L_bc', 'L_bc_err', 'bc_color', 'bc_extrapolated')})
            if bc['bc_extrapolated']:
                ew.append(f'颜色 {bc["bc_color"]}={bc["bc_color_value"]:.2f} '
                          f'超出 Lyman+2014 拟合范围 {bc["bc_color_range"]}，'
                          'L_bc 为外推值，谨慎使用')
        out_epochs.append(rec)

    if any(e.get('L_bc') is not None for e in out_epochs):
        top_warnings.append(
            'Lyman+2014 BC 关系要求宿主消光已改正的颜色；当前颜色至多含银河系'
            '消光改正，若宿主消光显著，L_bc 存在系统差（建议与 L_bb/L_obs 交叉核对）')
    return {'transient_id': transient_id, 'z': z, 'z_source': z_source,
            'bc_sample': bc_sample, 'bc_ref': _BC_REF,
            'epochs': out_epochs, 'warnings': top_warnings}


def bolometric_to_csv(result):
    """伪玻尔兹曼光变 CSV 序列化（元数据以 # 注释行随附，§4.4）"""
    lines = [
        '# AJST 伪玻尔兹曼光变（设计方案 M5）',
        f"# transient_id={result.get('transient_id')} z={result.get('z')} "
        f"z_source={result.get('z_source')} bc_sample={result.get('bc_sample')}",
        f"# bc_ref={result.get('bc_ref')}",
        't_days,n_bands,lambda_min_a,lambda_max_a,L_obs,L_obs_err,'
        'T_bb,T_bb_err,R_bb,R_bb_err,L_bb,L_bb_err,L_bc,L_bc_err,'
        'bc_color,bc_extrapolated',
    ]
    for e in result.get('epochs') or []:
        lines.append(','.join('' if e.get(k) is None else str(e.get(k))
                              for k in ('t_days', 'n_bands', 'lambda_min_a',
                                        'lambda_max_a', 'L_obs', 'L_obs_err',
                                        'T_bb', 'T_bb_err', 'R_bb', 'R_bb_err',
                                        'L_bb', 'L_bb_err', 'L_bc', 'L_bc_err',
                                        'bc_color', 'bc_extrapolated')))
    return '\n'.join(lines) + '\n'
