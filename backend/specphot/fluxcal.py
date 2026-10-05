"""specphot S1-c：定标模式判定（F-13/14）+ anchored 流量空间闭式解（式 6–8）+ AnchorSet。

纪律（02 §3.3）：
  - 双键交叉：uncal_kind 必须同时读 flux_type 与 u_fluxes（文件侧，绝不读 extra_data，
    T-54 读侧半段已在 reader 钉死）；表外值宁判 contradictory 并回显原值，不猜（F-13）。
  - direct 两道量级闸门：第一道 median(|F|)（V-17，先于配对）；第二道用实测点交叉，
    选带规则闭序——可配对点数最多者、并列取 λ 最小者（F-14，T-37）。
  - anchored 在**流量空间**求单一缩放因子（式 6–8，禁止星等空间取均值，F-11）；
    缩放方向固定 scales_spectrum：F_谱 ← κ*·F_谱，mag = m_syn − 2.5·log10 κ*（F-49）。
  - 波段协方差 C 不可假设对角（F-50）：cond > C_COND_MAX 改 pinv 并回显
    s_anchor.cov_method ∈ {chol, pinv}（与顶层 cov_method 词表不同轴，禁止互写）。
"""
import math

import numpy as np
import scipy.linalg

from .constants import (C_AB_ZERO, C_COND_MAX, C_FLUX_MEDIAN_MAX_CGS, C_MAX_ANCHORS)
from .reader import SpecLoadError

_ABS_UNITS = {'erg/s/cm^2/angstrom'}
_MODES = ('direct', 'anchored', 'model', 'auto')


def _warn(code, message):
    return {'code': code, 'message': message}


def _norm(v):
    return ' '.join(str(v).strip().lower().split()) if v is not None else None


def classify_flux_calibration(flux_type, u_fluxes, source='catalog'):
    """F-13 双键判定 → uncal_kind ∈ {absolute, uncalibrated, contradictory, unknown}。

    source='upload' 时 flux_unit 头键充当 u_fluxes、flux_type 视为缺失：
    (缺失, 绝对单位) 才可判 absolute；catalog 路径必须两键一致支持才判 absolute
    （etl.py:358 默认会把未定标谱标成 absolute ⇒ 单键标签不可信）。
    """
    ft, uf = _norm(flux_type), _norm(u_fluxes)
    raw = {'flux_type': flux_type, 'u_fluxes': u_fluxes}
    warnings = []
    if (ft is not None and ft != 'absolute') or \
            (uf is not None and uf not in _ABS_UNITS and uf != 'uncalibrated'):
        kind = 'contradictory'   # 表外值（含同义写法），宁判矛盾不猜
    elif ft == 'absolute' and uf in _ABS_UNITS:
        kind = 'absolute'
    elif ft == 'absolute' and uf == 'uncalibrated':
        kind = 'contradictory'
    elif uf == 'uncalibrated':
        kind = 'uncalibrated'
    elif source == 'upload' and ft is None and uf in _ABS_UNITS:
        kind = 'absolute'
    else:
        kind = 'unknown'         # 单键绝对单位 / 双键皆缺 / 仅 flux_type=absolute
    if kind == 'contradictory':
        warnings.append(_warn(
            'CA-03', f'定标标签矛盾或表外值（flux_type={flux_type!r}, '
                     f'u_fluxes={u_fluxes!r}），已降级 anchored/model'))
    return {'uncal_kind': kind, 'warnings': warnings, 'raw': raw}


def first_gate_suspect(flux_median_cgs):
    """F-14 第一道量级闸门（V-17，先于配对）：median(|F|) 落在高簇 ⇒ 量级不可信。"""
    return flux_median_cgs is not None and float(flux_median_cgs) > C_FLUX_MEDIAN_MAX_CGS


def pick_mag_sanity_band(candidates):
    """F-14 第二道闸门的闭序选带：可配对点数最多者，并列取 λ 最小者。

    candidates = [{'band', 'n_pairable', 'lambda_pivot_aa'}, …]；空 ⇒ None。
    """
    if not candidates:
        return None
    return min(candidates,
               key=lambda c: (-int(c['n_pairable']), float(c['lambda_pivot_aa'])))


def resolve_mode(mode_requested, uncal_kind, *, flux_scale_suspect=False,
                 anchors_available=0, model_available=False):
    """模式裁决。显式 direct 不过闸 ⇒ 硬拒 E-03（F-14，不静默降级）；auto 按
    direct → anchored → model 顺序判并回显实际模式（§3.3 表）。

    model 通路（P2 切片 2b）：model_available=True 表示请求携带了通过 Q-9/Q-34
    校验的 use_model（S2 拟合结果）⇒ mode='model' 或 auto 落到 model 时生效；
    否则保持 E-13（501 phase='P2'，无 S2 拟合的路径不开放）。"""
    if mode_requested not in _MODES:
        raise SpecLoadError('E-14', f"mode 只能是 {'/'.join(_MODES)}，"
                                    f"收到 {mode_requested!r}", reason='bad_enum')
    direct_ok = uncal_kind == 'absolute' and not flux_scale_suspect
    if mode_requested == 'direct':
        if uncal_kind != 'absolute':
            raise SpecLoadError('E-03', f'谱定标标签不是绝对流量（uncal_kind='
                                        f'{uncal_kind}），direct 不可用',
                                reason='label_not_absolute')
        if flux_scale_suspect:
            raise SpecLoadError('E-03', '流量量级与绝对定标标签不符'
                                        '（median(|F|) 落在高簇或 |Δm| 超窗）',
                                reason='flux_scale_suspect')
        return {'mode_effective': 'direct', 'warnings': []}
    if mode_requested == 'auto':
        if direct_ok:
            return {'mode_effective': 'direct', 'warnings': []}
        if anchors_available >= 1:
            mode_requested = 'anchored'
        elif model_available:
            mode_requested = 'model'
        else:
            # auto 无路可走：以前落到 model 报 E-13 feature_disabled（501），
            # 文案还是期次闸门措辞，用户无从行动。改为 E-10/409 指明可行下一步。
            raise SpecLoadError('E-10', 'auto 定标无可用路径：谱非绝对流量（或流量'
                                '量级不可信）、无可配对实测锚点、也未携带 S2 '
                                'use_model——可手加锚点或先做 S2 连续谱拟合',
                                status=409, reason='auto_no_calibration_path')
    if mode_requested == 'anchored':
        if anchors_available < 1:
            raise SpecLoadError('E-10', '无可配对实测点，anchored 需要至少 1 个锚点',
                                status=409)
        return {'mode_effective': 'anchored', 'warnings': []}
    # model：§3.3 model 行——用 S2 模型曲线代替观测谱；无 use_model ⇒ 未开放
    if not model_available:
        raise SpecLoadError('E-13', 'model 定标需携带 use_model（S2 拟合结果）',
                            details={'phase': 'P2'})
    return {'mode_effective': 'model', 'warnings': []}


class AnchorSet:
    """F-78：锚点行集（源表行 origin='catalog' 与手加行 origin='manual' 同权但可辨）。

    行结构：{band, mag, mag_system='AB', mjd=None, mag_err=None, anchor_origin,
             note='', used=True}。T-42：同波段两行（任意来源组合）⇒ E-14 且列出冲突两行
    （附加字段 band_conflict/conflict_rows）；取消勾选的行不进 n_bands_used。
    """

    def __init__(self, rows):
        rows = list(rows or [])
        if len(rows) > C_MAX_ANCHORS:
            raise SpecLoadError('E-14', f'锚点行 {len(rows)} 超过上限 {C_MAX_ANCHORS}',
                                reason='anchor_set_invalid')
        self.rows = []
        for i, r in enumerate(rows):
            row = dict(r)
            row.setdefault('mag_system', 'AB')
            row.setdefault('mjd', None)
            row.setdefault('mag_err', None)
            row.setdefault('note', '')
            row.setdefault('used', True)
            if row.get('anchor_origin') not in ('catalog', 'manual'):
                raise SpecLoadError('E-14', f'锚点行 {i} 缺 anchor_origin'
                                            "（catalog/manual）",
                                    reason='anchor_set_invalid')
            if row.get('mag') is None or row.get('band') in (None, ''):
                raise SpecLoadError('E-14', f'锚点行 {i} 缺 band/mag',
                                    reason='anchor_set_invalid')
            self.rows.append(row)
        seen = {}
        for row in self.rows:
            b = row['band']
            if b in seen:
                raise SpecLoadError(
                    'E-14', f"波段 {b} 有两行锚点（同波段冲突）",
                    reason='anchor_set_invalid',
                    details={'band_conflict': True,
                             'conflict_rows': [seen[b], row]})
            seen[b] = row

    def used_rows(self):
        return [r for r in self.rows if r['used']]

    def used_bands(self):
        return [r['band'] for r in self.used_rows()]


def anchor_mag_to_fnu_cgs(mag, mag_system='AB', vega2ab=None):
    """锚点星等 → Fν[cgs]。F-9 三态：Vega 的 vega2ab 为 None ⇒ E-08；恰为 0.0 ⇒
    仍算但挂 CA-01（可能是缺省值）；F-48/F-10：ST 在 P1 一律 E-08。"""
    warnings = []
    if mag_system == 'AB':
        m_ab = float(mag)
    elif mag_system == 'Vega':
        if vega2ab is None:
            raise SpecLoadError('E-08', '该波段未登记 vega2ab，无法换算 Vega 星等',
                                reason='vega_unavailable')
        if float(vega2ab) == 0.0:
            warnings.append(_warn('CA-01', '该波段 vega2ab 登记值为 0.0'
                                           '（可能是缺省值，见展开卡）'))
        m_ab = float(mag) + float(vega2ab)
    elif mag_system == 'ST':
        raise SpecLoadError('E-08', 'ST 星等制需要库内没有的 Vega 参考谱（P1 禁用）',
                            reason='st_unavailable')
    else:
        raise SpecLoadError('E-14', f"mag_system 只能是 AB/ST/Vega，"
                                    f"收到 {mag_system!r}", reason='bad_enum')
    return 10.0 ** (-0.4 * (m_ab + C_AB_ZERO)), warnings


def anchored_mag(m_syn, kappa):
    """F-49：方向固定 scales_spectrum ⇒ mag = m_syn − 2.5·log10 κ*。"""
    return float(m_syn) - 2.5 * math.log10(float(kappa))


def anchored_fit(f, g, C):
    """式 (6)–(8)：流量空间 GLS 闭式解。f = 合成流量，g = 实测流量，C = C_obs ⊕ C_syn。

    返回 s_anchor 键集：value/direction/sigma/sigma_infl/chi2/dof/n_bands_used/
    fit_space/cov_method/infl/leverage。n < 2 ⇒ chi2=None + CA-14（F-12）；
    χ²/dof > 1 ⇒ infl = sqrt(χ²/dof)，σ_κ 与 σ_κ·infl 两个都列（F-51）。
    leverage h_i = f_i·(C⁻¹f)_i/(fᵀC⁻¹f)（F-56 的消去因子来源）。
    """
    f = np.asarray(f, dtype=float)
    g = np.asarray(g, dtype=float)
    C = np.asarray(C, dtype=float)
    n = int(f.size)
    warnings = []
    if n == 0:
        # 空锚点集（如锚点波段全被行级 E-04 丢弃）⇒ 干净 409，不进 LinAlgError 500
        raise SpecLoadError('E-10', '锚点集为空：所有锚点波段均未出结果，anchored 无从进行',
                            status=409)
    if float(np.linalg.cond(C)) > C_COND_MAX:
        c_inv = np.linalg.pinv(C)
        cov_method = 'pinv'
        c_inv_f = c_inv @ f
        c_inv_g = c_inv @ g
    else:
        cho = scipy.linalg.cho_factor(C)
        c_inv = None
        c_inv_f = scipy.linalg.cho_solve(cho, f)
        c_inv_g = scipy.linalg.cho_solve(cho, g)
        cov_method = 'chol'
    a = float(f @ c_inv_f)
    if not math.isfinite(a) or a <= 0.0:
        # C 奇异到 f·C⁻¹f=0（如 σ_obs 与 σ_syn 同时不可用）⇒ 锚定方程退化，
        # 干净拒绝而非 ZeroDivisionError（A-3：显式错误响应）
        raise SpecLoadError('E-10', '锚定方程退化：锚点行的 σ_obs 与谱噪声 σ_syn '
                                    '同时不可用，C 奇异，锚定无法进行',
                            status=409)
    kappa = float(f @ c_inv_g) / a
    sigma = a ** -0.5
    resid = g - kappa * f
    if n < 2:
        chi2, dof = None, 0
        warnings.append(_warn('CA-14', '单波段锚定：无一致性可检验（chi2_anchor=null）'))
    else:
        c_inv_r = (c_inv @ resid) if cov_method == 'pinv' \
            else scipy.linalg.cho_solve(cho, resid)
        chi2, dof = float(resid @ c_inv_r), n - 1
    infl = math.sqrt(chi2 / dof) if (chi2 is not None and chi2 / dof > 1.0) else 1.0
    leverage = (f * c_inv_f / a).tolist()
    return {'value': kappa, 'direction': 'scales_spectrum', 'sigma': sigma,
            'sigma_infl': sigma * infl, 'chi2': chi2, 'dof': dof,
            'n_bands_used': n, 'fit_space': 'flux', 'cov_method': cov_method,
            'infl': infl, 'leverage': leverage, 'warnings': warnings}
