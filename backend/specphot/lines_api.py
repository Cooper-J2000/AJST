"""specphot.lines_api —— API-4（POST /line，S3 谱线测量主计算，P3 切片 2）。

编排（§5.2.3 + §3.9 + F-34…F-42/F-66…F-72）：请求校验（Q-18/20/21/22 + Q-23
来源二选一 + Q-15 n_boot/基线阶 + Q-27 诊断布尔 + Q-35 err_seed）→ 装载
（库内/上传，复用 photometry 的装载与 preprocess 管线——merge_masks/F-110 判定/
去偏 σ/双哈希全走同一代码路径，与 API-2/3 同源）→ 银河消光（F-59②，观测系、
测量之前）→ 线心换算（λ_obs = λ_rest·(1+z)，z 取请求或谱记录）→
lines.select_window/measure_line（F-34…F-42 + §3.9.3 全量，纯函数层唯一入口）→
§4.2 LineResult 键集装配（F-94⑤ 补齐键 + F-67 互斥空值 + snr_res）→ 缓存
（ST-3，双哈希进键）。全链只读（RO-1…RO-5）。

Q-20 vs F-72③ 掩膜冲突 reason 的调和决定（P3 切片 2 登记，本文件为唯一落地处）：
  - Q-20 原文：「候选线心（含 z 平移后）落在 C_MASK_ABS_TABLE 或
    C_MASK_EMIS_TABLE 命中区 ⇒ 拒该次测量并写是哪一类（两类处理方式不同，
    措辞不得混用，F-72）｜E-14（理由 mask_conflict）」——wire reason 钉死
    'mask_conflict'，且要求"写是哪一类"。
  - F-72③ 原文：「判定为发射线且线心落在吸收带内、或判定为吸收线且线心落在
    气辉线上 ⇒ 拒绝该测量（E-14）并写明理由」——两类掩膜的**类属与措辞**
    可分辨（T-20：措辞不得混用）。
  - lines.select_window 已按 F-72③ 出双 reason（line_center_in_abs_band /
    line_center_in_skyline），并在该处登记了与 Q-20 字面的张力。
  - **调和决定：wire reason 统一映射回 Q-20 的 'mask_conflict'**（对外契约
    一张脸），类属可分辨性不由 reason 承担、而由（a）message 文案逐字保留
    F-72③ 的两类措辞（"大气吸收带"/"天光发射线"，不得混用）、（b）details
    增设 mask_conflict_kind ∈ {abs_band, skyline} 与 f72_reason（原双 reason
    原样透传）两个机器可读键承担。即：Q-20 的 reason 字面与 F-72③/T-20 的
    类属可辨同时成立，两头都不牺牲；不修订任一文档。
  - 断言载体：test_l2_specphot_lines_api.py 的调和测试（reason/kind/message
    三层各按其源文档断言，且两条 message 互不相同）。

frame 闸门（W-25 两条，不得合并表述；vel_*/z_fit 本期恒 null（M-6），闸门
结构先就位，随 P3c 的 M-6 复核线表启用）：
  ① 线表侧（Q-22/W-25①）：请求 velocity_output=true（vel_* 家族"强提交"开
     关，实现侧裁量登记——规格未给键名，此键是让 Q-22 可触发的最小接口）而
     line_frame 不在 {'air','vacuum'}（未经 M-6 复核）⇒ E-14，reason=
     'line_frame_unverified'，文案说**线表**未复核（82–87 km/s 系统差）。
  ② 谱级（W-25②/F-79②/U-39）：velocity_output=true 而谱级
     lambda_frame=='unknown' ⇒ E-14，reason='lambda_frame_unknown'，文案说
     **谱的波长轴**未声明框架。两支 reason/文案互不相同、均不得与
     mask_conflict 混用；②在①之后判（同一强提交两条都违反时报线表侧——
     F-38 的系统差是逐条线的、更根本）。
"""
import math
import time

import numpy as np
from flask import jsonify, request

from app import require_auth

from . import (specphot_bp, SPEC_PHOT_VERSION, _compute_gate, _err,
               _load_error_response, _result_cache, _cache_lock)
from . import diagnostics as _diag
from . import lines as LN
from . import preprocess as pre
from . import registry as _reg
from .constants import (C_ABS_DETECT_SIGMA, C_AOD_SAMP_MIN, C_AOD_SNR_RES_MIN,
                        C_BOOT_N, C_BOOT_N_BUDGET, C_CACHE_MAX, C_LINE_WIN,
                        C_MASK_ABS_TABLE, C_MASK_EMIS_TABLE, C_MAX_BOOT,
                        C_POLY_ORDER_MAX, C_BASE_ITER, C_BASE_SIDE,
                        C_SAT_DEPTH_FLOOR, C_WING_RED_KMS, C_WING_SPREAD_DEX,
                        C_XLINK_MASK_MAX)
from .meta import load_spectrum_by_id
from .photometry import (_apply_mw, _errcol_stage, _load_upload_arrays,
                         _sigma_stage)
from .reader import SpecLoadError

_PROFILES = ('gauss1', 'gauss2', 'lorentz', 'voigt')   # U-26（voigt 随 Q-21 拒）
_LINE_FRAMES = ('air', 'vacuum')                       # F-38：用户核对后填


class _LineApiError(Exception):
    def __init__(self, code, reason, message, details=None):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.details = details or {}


def _bad(reason, message, details=None):
    return _LineApiError('E-14', reason, message, details)


def _e09(reason, message, details=None):
    """Q-18 族：必要元数据缺失（§5.3 E-09 = metadata_required，400）。"""
    return _LineApiError('E-09', reason, message, details)


def _warn(code, message, **kw):
    w = {'code': code, 'message': message}
    w.update(kw)
    return w


# Q-20/F-72③ 调和的唯一映射表（见模块 docstring 的调和决定）
_MASK_KIND_OF = {'line_center_in_abs_band': 'abs_band',
                 'line_center_in_skyline': 'skyline'}


def _line_error_response(e):
    """LineError/_LineApiError → 线上错误响应（§5.3 映射，与 API-3 同表）。

    E-09 ⇒ wire 'metadata_required'（400）；E-15 ⇒ wire 'err_key_missing'
    （500，服务端契约缺陷）。Q-20/F-72③ 调和在此落地：reason 映射回
    'mask_conflict'，类属进 details（docstring 调和决定）。
    注意 lines.LineError 的 reason 位是人类文案、机器 reason 在 details['reason']
    （见 _compute_line 的拦截注）；此处对未拦截的 LineError 统一取机器值。"""
    wire = {'E-01': 'spectrum_not_found', 'E-04': 'band_unusable',
            'E-07': 'input_over_limit', 'E-09': 'metadata_required',
            'E-11': 'compute_timeout', 'E-13': 'feature_disabled',
            'E-14': 'bad_request_state', 'E-15': 'err_key_missing'}.get(
        e.code, 'bad_request_state')
    http = {'E-01': 404, 'E-11': 504, 'E-13': 501, 'E-15': 500}.get(e.code, 400)
    reason = getattr(e, 'reason', None)
    details = dict(getattr(e, 'details', None) or {})
    if isinstance(e, LN.LineError):
        mreason = details.pop('reason', None)
        if mreason:
            reason = mreason
    if reason in _MASK_KIND_OF:
        # Q-20/F-72③ 调和（模块 docstring）：wire reason=mask_conflict；
        # 类属（mask_conflict_kind）与 F-72③ 原 reason（f72_reason）进 details，
        # message 保留 F-72③ 的两类措辞原文（措辞不得混用，T-20）。
        details['mask_conflict_kind'] = _MASK_KIND_OF[reason]
        details['f72_reason'] = reason
        reason = 'mask_conflict'
    return _err(wire, str(e), http, reason=reason, **details)


# ─── U-48 / P3d 吸收系统装配（F-98…F-104；纯数值在 diagnostics，本侧只编排） ──

def _metal_entry(m6, lam_rest):
    """请求线心对 M-6 线表的金属线匹配（±1 Å 最近者；实现裁量，TECHNICAL 登记）。
    Lyα 条目不参与（它是翼的静止线心，不是 z_abs 的金属线路由）。"""
    if not m6 or lam_rest is None:
        return None
    best = None
    for e in m6:
        if 'ly' in str(e.get('species', '')).lower():
            continue
        d = abs(float(e['lambda0_rest_aa']) - float(lam_rest))
        if d <= 1.0 and (best is None or d < best[0]):
            best = (d, e)
    return best[1] if best else None


def _absorber_stage(lam, flux, sig, flags, z_eff, z_explicit, r_res, r_source,
                    row, line_in, budget_left):
    """U-48 开启时的吸收系统格装配（F-98…F-104 + F-102/F-105①）。

    前置缺 ⇒ 降级语义全部来自规格原文：M-6 缺 ⇒ Lyα/金属线无输入源，闸门格
    （值键 null + 书面原因，F-94③）；z 显式 ⇒ user_z 路由（metadata、z_err
    空值 + 原因，§3.9.3.1 z_abs 行）；b 走 engineering_default（C_WING_B_KMS，
    F-99② 第三路）。返回 (systems, cross_link_gate, forest_reasons)。

    cross_link_gate 键集 = §4.2 原文 {bands_masked_frac[], n_bands_over_gate,
    n_lines_in_masked_absorbers, blue_side_igm_masked, b_source, z_source,
    note}（P3d 评审 P1-1 对齐：本端点无波段积分 ⇒ bands_masked_frac 恒空、
    n_bands_over_gate 恒 0；b_source/z_source 回显本格钉住路由，未定即 null）；
    enabled/bands_ca46 是实现追加键（登记 docs/TECHNICAL.md）。F-103① 的波段
    占比判定量在 API-2/S1 侧（diagnostics.cross_link_band_fracs，与
    band_integrals 的 n_masked/used 同口径）。
    """
    forest = _diag.forest_stats_reasons(r_source)
    gate = {'enabled': True, 'bands_masked_frac': [], 'bands_ca46': [],
            'n_bands_over_gate': 0, 'n_lines_in_masked_absorbers': 0,
            'blue_side_igm_masked': None, 'b_source': None, 'z_source': None,
            'note': ('API-4 无波段积分：F-103① 的波段占比判定量在 S1 侧'
                     '（diagnostics.cross_link_band_fracs 与 band_integrals 的 '
                     'n_masked/used 同口径）；本端点做②线心落掩膜区与④蓝端声明')}
    if not flags['absorber_ident']:
        gate = {'enabled': False, 'bands_masked_frac': [], 'bands_ca46': [],
                'n_bands_over_gate': 0, 'n_lines_in_masked_absorbers': 0,
                'blue_side_igm_masked': None, 'b_source': None,
                'z_source': None,
                'note': '未启用（U-48 三开关全关，F-105①：判定量取"未启用"值）'}
        return [], gate, forest
    lya = _diag.lya_entry(_reg.M6_LINE_TABLE)
    if lya is None or lya[1] is None:
        why = _diag._ABS_GATE_NOTE if lya is None else \
            ('M-6 线表的 Lyα 条目缺 f_osc：翼剖面 τ 标定（∫σdν = πe²f/(m_ec)）'
             '无 f 输入（F-98①/F-99）')
        e = _diag.absorber_gate_entry(reason=why)
        e['notes'].append(why)
        return [e], gate, forest
    lam0_lya, f_lya, _lya_row = lya
    # z_abs 三路（§3.9.3.1 z_abs 行）：metal_lines（M-6 + 本请求已测金属线行）
    # → user_z（显式 z）→ 闸门格（无输入源）
    z_abs = z_err = z_source = None
    metal = _metal_entry(_reg.M6_LINE_TABLE, line_in.get('lambda_rest_aa'))
    metal_ids = []
    if metal is not None and row.get('detected') \
            and row.get('lambda_obs_vac_aa'):
        lam0_m = float(metal['lambda0_rest_aa'])
        if metal.get('line_frame') == 'air':
            import wavconvert
            lam0_m = wavconvert.air_to_vacuum(lam0_m)
        lam_obs = float(row['lambda_obs_vac_aa'])
        z_abs = lam_obs / lam0_m - 1.0
        z_err = ((1.0 + z_abs) * float(row['lambda_err_aa']) / lam_obs
                 if row.get('lambda_err_aa') else None)
        z_source = 'metal_lines'
        metal_ids = [metal.get('species')]
    elif z_explicit and float(z_eff) > 0:
        # user_z 路由要求显式 z>0：上传/记录回填的 z=0.0 是 Q-12 的缺省值，不是
        # 用户声明的系统红移 ⇒ 不冒充 user_z（z=0 的 Lyα 翼亦无物理意义）
        z_abs, z_source = float(z_eff), 'user_z'
    if z_abs is None:
        why = ('z_abs 无输入源：金属线路由需 M-6 复核线表 + 本请求金属线行，'
               'user_z 路由需显式 z（请求 z 或谱记录），两者均缺（F-98①）')
        e = _diag.absorber_gate_entry(reason=why)
        e['notes'].append(why)
        return [e], gate, forest
    b_kms, b_source = _diag.b_pin(r_res=r_res)      # F-99② 三路优先级
    gate['b_source'] = b_source                     # F-103③：单一误差通路声明
    gate['z_source'] = z_source
    ok = (np.isfinite(lam) & np.isfinite(flux) & np.isfinite(sig)
          & (sig > 0))
    ident = _diag.absorber_ident_numeric(
        lam, flux, sig, z_abs=z_abs, lambda_rest_lya_vac_aa=lam0_lya,
        f_osc=f_lya, b_kms=b_kms, r_res=r_res, ok=ok,
        do_fit=bool(flags['wing_logn']), budget_left=budget_left)
    wf = ident.get('wing_fit') or {}
    e = _diag.absorber_gate_entry(
        system_id='abs-1', z_abs=z_abs, z_err=z_err, z_source=z_source,
        lambda_rest_lya_vac_aa=lam0_lya,
        wing_b_assumption_kms=b_kms, b_source=b_source,
        cont_family_a=wf.get('cont_family_a'),
        cont_family_b=wf.get('cont_family_b'),
        wing_snr_res=ident.get('wing_snr_res'),
        wing_n_pixels=ident.get('wing_n_pixels'),
        detect_sigma=ident.get('detect_sigma'),
        intervening_or_host=ident.get('intervening_or_host'),
        metal_line_ids=metal_ids)
    e['class_thresholds_dex'] = _diag.class_thresholds_dex()
    es = {'wing_snr_res': 'count', 'wing_n_pixels': 'count',
          'detect_sigma': 'count', 'class': 'metadata',
          'class_thresholds_dex': 'metadata', 'intervening_or_host': 'metadata',
          'b_source': 'metadata', 'wing_b_assumption_kms': 'metadata',
          'cont_family_a': 'metadata', 'cont_family_b': 'metadata'}
    esc = dict.fromkeys(es, 'metadata')
    esc.update({'wing_snr_res': 'stat', 'wing_n_pixels': 'stat',
                'detect_sigma': 'stat'})
    if z_source == 'metal_lines':
        es['z_abs'] = 'covariance'
    else:
        es['z_abs'] = 'none'
        e['notes'].append(
            'z_source=user_z：z_abs 是输入 ⇒ 走 metadata 行、z_err 空值 + 本'
            '书面原因，翼通路不再给它算一次 profile（§3.9.3.1 z_abs 行/F-103③）')
    # err_scope['z_abs'] 恒 'stat'（与 err_source 正交）：err_scope 描述该量
    # 误差的**口径族**（随机项量级），err_source 描述**本通路给不给误差**——
    # user_z 路由 err_source='none'（值 null + 书面原因，TXT-23）不改 scope 词；
    # metal_lines 路由 err_source='covariance'（λ_err 换元，§3.9.3.1 z_abs 行）。
    esc['z_abs'] = 'stat'
    # 蓝侧 IGM（F-103④）：掩膜段已按 z_eff 在测量前并入第五类；本格按 z_abs
    # 重衍以备复核（metal 精化 z 与 z_eff 有差时写明，两者判定量可复核）。
    lam_a = lam0_lya * (1.0 + float(z_abs))
    lo_b = lam_a * (1.0 - C_WING_RED_KMS / _diag.C_C_KMS)
    if float(np.min(lam)) <= lam_a:
        e['masked_ranges'] = [[max(lo_b, float(np.min(lam))), lam_a]]
        e['blue_side_igm_masked'] = True
        gate['blue_side_igm_masked'] = True
        if z_source == 'metal_lines' and abs(float(z_abs) - float(z_eff)) > 1e-4:
            e['notes'].append(
                '掩膜第五类的蓝侧段按测量前 z_eff=%.5f 安置（F-106①：掩膜先于'
                '测量）；本格 masked_ranges 按 z_abs=%.5f 重衍以备复核（F-103①）'
                % (float(z_eff), float(z_abs)))
    else:
        e['blue_side_igm_masked'] = False      # 谱未覆盖 Lyα ⇒ 蓝侧无从参与
        gate['blue_side_igm_masked'] = False
    # F-98②：检出显著性只回答"算不算被检出"
    if ident.get('detected') is False:
        e['notes'].append(
            '检出显著性 %.1f < C_ABS_DETECT_SIGMA=%s：吸收特征未检出；该阈只'
            '回答检出与否，不回答柱密度好坏（F-98②）'
            % (ident.get('detect_sigma') or 0.0, C_ABS_DETECT_SIGMA))
    # F-99/F-100：翼拟合（仅 wing_logn 开）
    if not flags['wing_logn']:
        e['notes'].append('wing_logn 开关未开：不拟合，wing_logn 家族全空值'
                          '（U-48 联动；分类阈值制要 N ⇒ class 亦空值，F-98③）')
    elif ident.get('verdict') != 'ok' or not wf or wf.get('verdict') != 'ok':
        why = ident.get('reason') or wf.get('reason') or '翼拟合未执行'
        e['notes'].append(why)
    else:
        e.update({'wing_logn': wf['wing_logn'],
                  'wing_logn_err_lo': wf['wing_logn_err_lo'],
                  'wing_logn_err_hi': wf['wing_logn_err_hi'],
                  'wing_logn_alt': wf['wing_logn_alt'],
                  'wing_logn_alt_err_lo': wf['wing_logn_alt_err_lo'],
                  'wing_logn_alt_err_hi': wf['wing_logn_alt_err_hi'],
                  'wing_spread_dex': wf['wing_spread_dex']})
        es['wing_logn'] = es['wing_logn_alt'] = 'profile'
        esc['wing_logn'] = esc['wing_logn_alt'] = 'stat'
        e['class'] = _diag.classify_logn(wf['wing_logn'])
        if e['class'] is None:
            e['notes'].append('log N 未达 C_ABS_CLASS_LLS 阈 ⇒ class 为空值'
                              '（不是档名；禁止 none/other 字符串，F-98③）')
        if wf.get('ca44'):
            e['warnings'].append(_warn('CA-44', wf['ca44'],
                                       reason='zero_width_err'))
        # F-100③/CA-45：两族之差超阈 ⇒ 区间表述（禁止 quadrature 合成）
        if (wf.get('wing_spread_dex') or 0.0) > C_WING_SPREAD_DEX:
            e['warnings'].append(_warn(
                'CA-45', '两连续谱族的 log N 之差 %.3f > C_WING_SPREAD_DEX=%s：'
                '主导项是连续谱安置，不得当 1σ 读（F-100③）；两个值、四个区间'
                '端点全都列，禁止把 wing_spread_dex quadrature 进误差棒'
                % (wf['wing_spread_dex'], C_WING_SPREAD_DEX)))
        # F-104④：b 包络（只作灵敏度展示，不进 err_source；入 C_BOOT_N_BUDGET 池）
        env, env_reason = _diag.b_envelope(
            lam, flux, sig, z_abs=z_abs, lambda_rest_lya_vac_aa=lam0_lya,
            f_osc=f_lya, ok=ok,
            budget_left=(None if budget_left is None
                         else budget_left - (wf.get('n_profile_evals') or 0)))
        if env:
            e['notes'].append(
                'b 包络（C_WING_B_GRID_KMS，只作灵敏度展示、不进 err_source，'
                '三次固定-b 重拟合计入 C_BOOT_N_BUDGET 同一预算池，F-104④/'
                'F-95③）：' + '；'.join('b=%.0f km/s ⇒ logN=%.2f'
                                       % (r['b_kms'], r['wing_logn'])
                                       for r in env))
        elif env_reason:
            e['notes'].append('b 包络：' + env_reason)
        # F-99①：z 窗口灵敏度（P3d 评审 P1-4 接线，C_WING_Z_WIN_SIGMA 的唯一
        # 消费点）——z 在金属线 ±C_WING_Z_WIN_SIGMA·σ_z 内三点重拟 logN，回显
        # logn_spread_over_z（追加键、只作灵敏度展示，z 的误差仍走 z_err 的
        # covariance 一条，F-103③）；同入 C_BOOT_N_BUDGET 共享池（b 包络已计
        # 提时按其 need=3×110 扣减，两包络不双计同一份余量）。
        _z_budget = (None if budget_left is None
                     else budget_left - (wf.get('n_profile_evals') or 0)
                     - (3 * 110 if env else 0))
        zenv, zenv_reason = _diag.z_envelope(
            lam, flux, sig, z_abs=z_abs, z_err=z_err,
            lambda_rest_lya_vac_aa=lam0_lya, f_osc=f_lya, b_kms=b_kms, ok=ok,
            budget_left=_z_budget)
        if zenv:
            e['logn_spread_over_z'] = (max(r['wing_logn'] for r in zenv)
                                       - min(r['wing_logn'] for r in zenv))
            e['notes'].append(
                'z 窗口灵敏度（F-99①：z 只在金属线 ±C_WING_Z_WIN_SIGMA·σ_z 窗口'
                '内变、不作自由参；三点重拟只作灵敏度展示、不是 z 的误差——'
                'z_err 另走 covariance 一条）：' + '；'.join(
                    'z=%.5f ⇒ logN=%.2f' % (r['z_abs'], r['wing_logn'])
                    for r in zenv))
        elif zenv_reason:
            e['notes'].append('z 窗口灵敏度：' + zenv_reason)
    # F-104：metal_sat_check（sat_flag 只由被链接线行的 depth/depth_err 导出，
    # 本族不出现 sat_*_err 新列，T-74④）
    if flags['metal_sat_check']:
        e['notes'].append(
            '单云假定（F-104⑤ 常驻）：同一 b 得到的柱密度 strictly correct only '
            'for a single absorbing cloud；多成分/双峰光学深度分布会低估金属柱密度')
        if metal is not None and row.get('detected') \
                and row.get('depth') is not None:
            ratio = 1.0 - float(row['depth'])      # 线心流量比 F/C
            e['sat_flag'] = ('saturated' if ratio < C_SAT_DEPTH_FLOOR
                             else 'unsaturated')
            e['metal_sat_flags'] = [{'species': metal.get('species'),
                                     'sat_flag': e['sat_flag']}]
            br = []
            if row.get('snr_res') is None \
                    or float(row['snr_res']) < C_AOD_SNR_RES_MIN:
                br.append('每分辨率元信噪 %s < C_AOD_SNR_RES_MIN=%s'
                          % (row.get('snr_res'), C_AOD_SNR_RES_MIN))
            if row.get('n_pix_per_res') is None \
                    or float(row['n_pix_per_res']) < C_AOD_SAMP_MIN:
                br.append('每分辨率元采样 %s < C_AOD_SAMP_MIN=%s'
                          % (row.get('n_pix_per_res'), C_AOD_SAMP_MIN))
            if br:
                e['notes'].append(
                    'Na(v) 互比不适用（' + '；'.join(br) + '）：sat_flag 退回 '
                    'C_SAT_DEPTH_FLOOR 单一判据，并写明是哪一支不满足（F-104①）')
            else:
                e['notes'].append(
                    'AOD 适用性条件满足（snr_res ≥ C_AOD_SNR_RES_MIN=%s 且 '
                    'n_pix_per_res ≥ C_AOD_SAMP_MIN=%s 双支达标）；本请求单条被'
                    '链接线 ⇒ 仍退回 C_SAT_DEPTH_FLOOR 单一判据（同离子强弱双线'
                    '互比需同线区第二条线，与 §3.9.3.1 ratio 行同因，F-104①）'
                    % (C_AOD_SNR_RES_MIN, C_AOD_SAMP_MIN))
            e['notes'].append(
                'AOD 方向双向对消（F-104②，只报其一即偏）：噪声使 Na(v) 积分偏高'
                '（over-estimated by less than 10% for S/N per resolution '
                'element ≳7，L-55①）；未分辨的深线使视柱密度偏低（underestimate '
                'the true column density at high line depths，L-55④，方向相反）')
        else:
            e['notes'].append(
                'metal_sat_check 开启但无被链接的已测金属线行（M-6 匹配缺或线未'
                '检出）⇒ sat_flag 空值 + 本书面原因（F-94③）')
    # F-103②：候选线心落在吸收系统掩膜段内 ⇒ 线行挂 CA-46 并计数
    lc = row.get('lambda_center_input_aa')
    for seg in e['masked_ranges']:
        if lc is not None and seg[0] <= float(lc) <= seg[1]:
            gate['n_lines_in_masked_absorbers'] += 1
            row.setdefault('warnings', []).append(_warn(
                'CA-46', '候选线心 %.2f Å 落在吸收系统掩膜段 [%.2f, %.2f] 内：'
                'S1 波段量与 S3 翼量不得并排作物理比对（同一条 mask_hash 已不一致'
                '，F-103②/CA-46）；该线 EW/流量不得与同源 wing_logn 并排比对'
                % (float(lc), seg[0], seg[1])))
            break
    e['err_source'] = es
    e['err_scope'] = esc
    return [e], gate, forest


def _union_segs(segs):
    """区间并集（排序后一趟归并重叠/相接段）——absorber_system_masked 的值域：
    与 absorber_systems[].masked_ranges 同源（P3d 评审 P1-3），不另立掩膜通路。"""
    out = []
    for lo, hi in sorted((float(s[0]), float(s[1])) for s in segs):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


# ─── API-4 ───────────────────────────────────────────────────────────────

@specphot_bp.route('/line', methods=['POST'])
@require_auth
def line():
    if not _compute_gate.acquire(blocking=False):
        return _err('server_busy', '已有计算在跑，请稍后重试',
                    429, reason='busy', retry_after_s=2)
    try:
        try:
            body_out = _compute_line()
        except (SpecLoadError, LN.LineError, _LineApiError) as e:
            if isinstance(e, SpecLoadError):
                return _load_error_response(e)
            return _line_error_response(e)
        return jsonify(body_out)
    finally:
        _compute_gate.release()


def _num_or_none(v):
    if v is None or v == '':
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _compute_line():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise _bad('bad_enum', '请求体应为 JSON 对象')
    # Q-23：来源二选一（与 API-2/3 同一装载通路）
    sid, spec = body.get('spectrum_id'), body.get('spectrum')
    if (sid is None) == (spec is None):
        raise _bad('source_ambiguous', 'spectrum_id 与 spectrum 必须且只能给一个')
    if spec is not None and not isinstance(spec, dict):
        raise _bad('bad_enum', 'spectrum 必须为 JSON 对象')
    if sid is not None:
        if isinstance(sid, bool) or not (
                isinstance(sid, int)
                or (isinstance(sid, str) and sid.strip().isdigit())):
            raise _bad('bad_enum', f'spectrum_id 类型非法（{sid!r}）：须为整数')
        sid = int(sid)
    # Q-18：line_kind 必填、无默认（F-66；缺失/非法 ⇒ E-09 line_kind_required，
    # 不进入步 3——本端点是 API-4 的步 3 计算核，缺线型即整个请求拒）
    line_kind = body.get('line_kind')
    if line_kind not in ('emission', 'absorption'):
        raise _e09('line_kind_required',
                   'line_kind 必填且 ∈ {emission, absorption}，无默认（Q-18/'
                   'F-66：两类线在连续谱两侧角色相反，未确认不得进步 3）')
    # line{}（F-34 线区候选行）：species/id_table 只作回显，lambda_rest_aa 必填
    line_in = body.get('line') or {}
    if not isinstance(line_in, dict):
        raise _bad('bad_enum', 'line 必须为 JSON 对象')
    lam_rest = _num_or_none(line_in.get('lambda_rest_aa'))
    if lam_rest is None or lam_rest <= 0:
        raise _bad('line_center_missing',
                   'line.lambda_rest_aa 必填且为正有限数（F-34 线心；本方案不做'
                   '速度反推，线心一律由调用方给定）')
    half_width = body.get('window_halfwidth_aa')
    if half_width is None:
        half_width = C_LINE_WIN
    half_width = _num_or_none(half_width)
    if half_width is None or not 0 < half_width <= 10 * C_LINE_WIN:
        raise _bad('bad_enum', f'window_halfwidth_aa 须为 (0, {10 * C_LINE_WIN:g}]'
                               ' 内有限数（缺省 C_LINE_WIN）')
    # 基线（F-36）：order 消费（lines.baseline_fit）；side_px/iter 的规格值
    # C_BASE_SIDE/C_BASE_ITER 在 lines.py 内钉死——请求侧给别的值 ⇒ 显式拒
    # （不静默采用也不静默忽略；实现限制登记）。
    base_in = body.get('baseline') or {}
    if not isinstance(base_in, dict):
        raise _bad('bad_enum', 'baseline 必须为 JSON 对象')
    b_order = base_in.get('order', 1)
    if isinstance(b_order, bool) or not isinstance(b_order, int) \
            or not 0 <= b_order <= C_POLY_ORDER_MAX:
        raise _bad('bad_enum', f'baseline.order 须为 [0, {C_POLY_ORDER_MAX}] 内'
                               '整数（F-62④/Q-15）')
    b_side = base_in.get('side_px', C_BASE_SIDE)
    if b_side != C_BASE_SIDE:
        raise _bad('baseline_side_px_fixed',
                   f'baseline.side_px 本期钉死 C_BASE_SIDE={C_BASE_SIDE}'
                   '（F-36 的侧带设计；改值属 P3c 的 U-25 手改通路）')
    b_iter = base_in.get('iter', C_BASE_ITER)
    if b_iter != C_BASE_ITER:
        raise _bad('baseline_iter_fixed',
                   f'baseline.iter 本期钉死 C_BASE_ITER={C_BASE_ITER}（F-36）')
    # Q-21/U-26：profile 词表。voigt（P3b）按 F-39 闸门：R 可得（r_source='user'）
    # 才开放；全库无 R（库内现状，M-4 未完成）⇒ 恒拒（E-14，文案对齐 F-39 原文）。
    r_res = _num_or_none(body.get('R'))
    if body.get('R') is not None and (r_res is None or r_res <= 0):
        raise _bad('bad_enum', 'R 须为正有限数或 null（U-27；r_source 随之回显）')
    r_source = 'user' if r_res is not None else 'none'     # F-69 词表
    profile = body.get('profile', 'gauss1')
    if profile not in _PROFILES:
        raise _bad('bad_enum', f'profile ∈ {_PROFILES}（U-26）')
    if profile == 'voigt' and r_res is None:
        raise _bad('voigt_disabled_no_r',
                   "profile='voigt' 而全库无仪器分辨率 R（r_source='none'，M-4 "
                   '未完成）：宽度只能作为观测宽度报告（TXT-8/CA-08），voigt 禁用'
                   '（F-39/U-26/T-22）')
    sky_handling = body.get('sky_handling', 'mask')
    if sky_handling not in ('mask', 'subtract'):
        raise _bad('bad_enum', "sky_handling ∈ {'mask', 'subtract'}（U-31）")
    if sky_handling == 'subtract':
        # F-86（P3c 真解锁）：只依赖 C_MASK_EMIS_TABLE 常量与谱数据本身（不依赖
        # M-6 线表帧/f 值）⇒ 走 lines.sky_subtract 真实现；大气吸收带永远只能
        # mask（F-72①），失败自动退回 mask + CA-37；sky_subtraction 计入
        # not_in_budget[]（F-58/F-72②）。
        pass
    # W-25①：line_frame 逐条、用户核对后填（F-38/M-6）；'unknown' 不是其值域
    line_frame = body.get('line_frame')
    if line_frame is not None and line_frame not in _LINE_FRAMES:
        raise _bad('bad_enum', f"line_frame ∈ {tuple(_LINE_FRAMES)} 或 null"
                               '（F-38：用户核对后填；与谱级 lambda_frame 无关）')
    z_req = _num_or_none(body.get('z'))
    if body.get('z') is not None and (z_req is None or z_req < 0):
        raise _bad('bad_enum', 'z 须为非负有限数或 null（null = 用谱记录/默认）')
    n_boot = body.get('n_boot', C_BOOT_N)
    if isinstance(n_boot, bool) or not isinstance(n_boot, int) \
            or not 1 <= n_boot <= C_MAX_BOOT:
        raise _bad('bad_enum', f'n_boot 须为 [1, {C_MAX_BOOT}] 内整数（Q-15）')
    err_seed = body.get('err_seed')
    if err_seed is not None and (isinstance(err_seed, bool)
                                 or not isinstance(err_seed, int)
                                 or not 0 <= err_seed <= 2 ** 63 - 1):
        raise _bad('err_seed', 'err_seed 须为 null 或 [0, 2^63-1] 内整数（Q-35）')
    # W-25①② 的强提交开关（实现裁量：规格未给键名，见模块 docstring）
    velocity_output = body.get('velocity_output', False)
    if not isinstance(velocity_output, bool):
        raise _bad('bad_enum', 'velocity_output 必须为布尔')
    # U-48 诊断三键（F-105③：只有本端点接受；Q-27 只收布尔）。P3d 真实现：
    #   联动（W-35/U-48）：wing_logn 与 metal_sat_check 在未开 absorber_ident 时
    #   禁用（依赖摆出来，不静默连带开启）⇒ E-14；M-4/M-6 前置缺 ⇒ 可用性子闸
    #   在 _absorber_stage 内降级（null + 书面原因，不自创语义）。
    diag = body.get('diagnostics') or {}
    if not isinstance(diag, dict):
        raise _bad('bad_enum', 'diagnostics 必须为 JSON 对象')
    if any(not isinstance(v, bool) for v in diag.values()):        # Q-27
        raise _bad('bad_enum', 'diagnostics 的值只能为布尔（Q-27）')
    d_flags = {'absorber_ident': bool(diag.get('absorber_ident', False)),
               'wing_logn': bool(diag.get('wing_logn', False)),
               'metal_sat_check': bool(diag.get('metal_sat_check', False))}
    if (d_flags['wing_logn'] or d_flags['metal_sat_check']) \
            and not d_flags['absorber_ident']:
        raise _bad('u48_requires_absorber_ident',
                   'U-48 联动：wing_logn 与 metal_sat_check 在未开 '
                   'absorber_ident 时禁用（W-35：依赖摆出来，不静默连带开启）')
    # Q-33：preprocess{} 与 API-2/3 同形（F-113① 同一块）
    pp = body.get('preprocess') or {}
    if not isinstance(pp, dict):
        raise _bad('bad_enum', 'preprocess 必须为 JSON 对象')
    factor, pp_ranges, smooth_px, errcol_choice = pre.validate_request(pp)
    mask_raw = body.get('mask') or []
    if not isinstance(mask_raw, list):
        raise _bad('bad_enum', 'mask 必须为数组')
    mwq = body.get('mw') or {}
    if not isinstance(mwq, dict):
        raise _bad('bad_enum', 'mw 必须为 JSON 对象')
    try:
        ebv_override = None if mwq.get('ebv') is None else float(mwq['ebv'])
    except (TypeError, ValueError):
        raise _bad('not_finite', 'mw.ebv 类型非法')
    if ebv_override is not None and not (math.isfinite(ebv_override)
                                         and ebv_override >= 0):
        raise _bad('not_finite', 'mw.ebv 必须为有限非负数')

    # ST-5 重档硬超时：§7.6 表值 5 s（S2/S3/API-8 档）
    deadline = time.monotonic() + 5.0

    def _deadline_check():
        if time.monotonic() > deadline:
            raise SpecLoadError('E-11', '计算超时（ST-5 重档硬超时 5 s），'
                                        '建议缩小线窗或降低 n_boot', status=504)

    # 装载（库内/上传同一通路）+ 银河消光 + 误差列判定 + 去偏 σ —— 与 API-2/3
    # 同一批 photometry/preprocess 函数，无第二套实现（F-113① 同源管线）
    ls = load_spectrum_by_id(sid) if sid is not None else _load_upload_arrays(spec)
    meta, top_warn = ls['meta'], list(ls['warnings'])
    lam = ls['lam_aa']
    # F-103①/F-106① 次序：吸收系统掩膜段（第五类）在测量**前**并入——U-48 开且
    # M-6 线表给出 Lyα、z 显式可得时，把 Lyα 蓝侧 IGM 段并进 merge_masks 的
    # absorber 槽（mask_hash 第 4 槽）；前置缺 ⇒ 空段，与 P3c 基线逐字节同值。
    z_explicit = (z_req is not None) or (meta or {}).get('z') is not None
    z_eff = (meta or {}).get('z') if z_req is None else z_req
    z_eff = z_eff or 0.0
    absorber_segs = _diag.absorber_mask_segs(lam, z_eff, _reg.M6_LINE_TABLE) \
        if d_flags['absorber_ident'] and z_explicit else []
    # 蓝侧 IGM 段并入本身不是 CA-46（其闭合触发集三条见 T-23/F-103，不扩）；
    # 掩膜变化经 mask_hash（第 4 槽）与 absorber_systems[].masked_ranges 回显。
    masks = pre.merge_masks(lam, list(mask_raw) + pp_ranges,
                            absorber_ranges=absorber_segs)
    if masks['n_ignored']:
        top_warn.append(_warn('CA-10', f"{masks['n_ignored']} 个掩膜段无效或与谱覆盖"
                                       '无重叠，已忽略（V-13）'))
    flux_corr, mw = _apply_mw(ls, mwq, ebv_override, top_warn)
    ec = _errcol_stage(ls, flux_corr, errcol_choice, 'auto', top_warn)
    mask_h = pre.mask_hash(masks, lam, ls['spec_hash'])
    pp_h = pre.preprocess_hash(mask_h, factor, ec['ev']['verdict'],
                               ec['errcol_accepted'], ls['spec_hash'])
    ckey = pre.f80_hash(['line', sid, ls['spec_hash'],
                         line_in, line_kind, half_width, base_in, profile,
                         (r_res, r_source), sky_handling, line_frame, z_req,
                         n_boot, err_seed, velocity_output, diag,
                         mask_h, pp_h, factor,
                         mwq.get('correct', True), ebv_override,
                         SPEC_PHOT_VERSION,
                         # 上传件 meta 是请求自带输入（A-7，与 API-2/3 同理进键）
                         meta], n=16)
    with _cache_lock:
        if ckey in _result_cache:
            _result_cache.move_to_end(ckey)
            return _result_cache[ckey]
    if ec['ca47_note']:
        top_warn.append(_warn('CA-47', ec['ca47_note'][1],
                              reason=f"errcol_{ec['ca47_note'][0]}"))
    sg = _sigma_stage(ls, flux_corr, ec['use_col'], len(lam), top_warn)
    if not bool(np.any(np.asarray(sg['sigma_px'], dtype=float) > 0)):
        raise _bad('sigma_unavailable', '误差列与代理 σ 均不可用：S3 线测量需要'
                                        '逐像素 σ（不得以 0 冒充，TXT-23）')
    # F-106① 次序：消光 ⇒ 掩膜 ⇒ 合束 ⇒ 才进测量（与 API-3 同一 rebin 通路）
    rebin_info = None
    if factor > 1:
        rebin_info = pre.rebin_stage(lam, flux_corr, sg['sigma_px'],
                                     masks['mask_bool'], factor, sg['rho'])
        top_warn.append(_warn('CA-06', f'谱已合束（factor={factor}）：白噪声前提被'
                                       '破坏，sigma_method=second_diff_degraded'
                                       '（F-108⑥/F-97⑦）'))
        top_warn.extend(pre.ca49_warnings(factor, rebin_info['echo']['rebin_gain'],
                                          rebin_info['echo']['px_per_fwhm_after'],
                                          rebin_info['rho_echoed']))
    lam_a = np.asarray(rebin_info['lam'] if factor > 1 else lam, dtype=float)
    flam_a = np.asarray(rebin_info['flux'] if factor > 1 else flux_corr,
                        dtype=float)
    sig_a = np.asarray(rebin_info['sigma'] if factor > 1 else sg['sigma_px'],
                       dtype=float)
    ok = np.isfinite(lam_a) & np.isfinite(flam_a) & np.isfinite(sig_a) \
        & (sig_a > 0)
    lam_a, flam_a, sig_a = lam_a[ok], flam_a[ok], sig_a[ok]
    if lam_a.size < 8:
        raise SpecLoadError('E-04', f'可用像素 {lam_a.size} < 8（too_few_pixels）',
                            reason='too_few_pixels')

    # W-25①② 框架闸门（强提交才触发；两条 reason/文案不得合并，模块 docstring）
    spec_frame = (meta or {}).get('lambda_frame')
    if velocity_output:
        if line_frame not in _LINE_FRAMES:                    # W-25① 线表侧
            raise _bad('line_frame_unverified',
                       '线表帧未经 M-6 复核（line_frame 未核对填写 air/vacuum）：'
                       '位置线只作标记，任何 vel_* 输出被拒——宿主整数线表的'
                       '空气/真空歧义即 82–87 km/s 系统差（Q-22/W-25①/F-38）')
        if spec_frame == 'unknown':                           # W-25② 谱级
            raise _bad('lambda_frame_unknown',
                       "谱级 lambda_frame='unknown'：这条谱的波长轴未声明框架，"
                       'vel_*/z_fit 禁用（W-25②/F-79②/U-39）；与线表侧 '
                       'line_frame 闸门是两道独立的门，文案不得合并')
        top_warn.append(_warn(
            'CA-44', 'velocity_output=true：M-6 复核线表本期未完成，vel_*/z_fit '
            '仍按 null 出（留位理由见 velocity_family_note）', reason='m6_pending'))

    # 线心（观测系真空 Å）= λ_rest·(1+z)；z_eff/z_explicit 已在装载后判定（Q-12）
    lam_center = lam_rest * (1.0 + z_eff)
    _deadline_check()
    try:
        row = LN.measure_line(
            lam_a, flam_a, sig_a, lam_center, line_kind,
            profile=profile, baseline_order=b_order, half_width=half_width,
            z=z_eff, r_resolution=r_res, n_boot=n_boot,
            budget_left=C_BOOT_N_BUDGET, err_seed=err_seed,
            spec_hash=ls['spec_hash'], sky_handling=sky_handling)
    except LN.LineError as e:
        # Q-20/F-72③ 调和（模块 docstring）：lines.LineError 的**机器 reason 在
        # e.details['reason']**（e.reason 位是 F-72③ 的人类文案——含"大气吸收带/
        # 天光发射线"两类措辞，措辞不得混用，T-20），这里只拦截两类掩膜冲突，
        # 其余 LineError（too_few_pixels/baseline_cond_max 等）原样上抛。
        mreason = (e.details or {}).get('reason')
        if mreason in _MASK_KIND_OF:
            raise _LineApiError(e.code, mreason, e.reason,
                                {k: v for k, v in (e.details or {}).items()
                                 if k != 'reason'})
        raise
    _deadline_check()

    # ── §4.2 LineResult 键集补齐（F-94⑤：值键与误差键同批；F-67 互斥空值已由
    # measure_line 保证）。本请求一线一行（§5.2.3 单 line 对象）；线比 ratio
    # 族要同线区多线（§3.9.3.1），单行请求 ⇒ null + 书面原因。柱密度族要
    # M-6 的振子强度库（f_source），本期未备 ⇒ null + 书面原因（F-71）。 ──
    notes = ['本请求单线一行：线比 ratio/ratio_ref/ratio_err 需同线区第二条'
             '发射线（§3.9.3.1），单行请求恒 null',
             '柱密度族（column_density/f_oscillator/f_source）需 M-6 复核的'
             '振子强度库（F-71/L-26），本期未备 ⇒ null（留位，P3c）']
    row.update({
        'species': line_in.get('species'),
        'lambda_rest_vac_aa': lam_rest,
        'line_frame': line_frame,                 # F-38 逐条回显（null=未核对）
        'model': profile,                         # §4.2 LineResult.model = 轮廓
        'r_source': r_source,
        # F-72②/U-31/F-86：sky_handling='subtract' 的真实现结果（lines.sky_subtract；
        # CA-37 退回 ⇒ False 且不称已扣除）；'mask' 恒 False。
        'sky_subtracted': bool((row.get('sky_subtract') or {})
                               .get('sky_subtracted')),
        # F-58：误差预算外分量下限集（sky_subtraction 恒在——无论本请求是否
        # 扣除，天光背景都不在误差预算内；T-51 的 CA-37 分支同含）。
        'not_in_budget': ['wavecal', 'aperture', 'flux_calibration',
                          'sky_subtraction'],
        'mask_applied': {'abs_table': C_MASK_ABS_TABLE,
                         'emis_table': C_MASK_EMIS_TABLE},
        'baseline_type': row.get('baseline', {}).get('poly_basis'),
        'cond_G': None,   # §4.2 LineResult 键集（F-94⑤）；S3 未立设计矩阵条件数——
                          # 恒 None 占位与 S2 同先例（continuum.py 同键恒 None）
        'column_density': None, 'column_density_lower_bound': None,
        'column_density_err': None, 'f_oscillator': None, 'f_source': None,
        'ratio': None, 'ratio_ref': None, 'ratio_err': None,
        'notes': notes,
        'sky_handling': sky_handling,
        'lambda_frame_spec': spec_frame,          # 谱级（与 line_frame 分列，F-38）
        'velocity_output_requested': velocity_output,
    })
    # F-94⑤ 键集补齐（velocity/去卷积误差家族 + 未检出上限列）：检出行的
    # upper_limit_3sigma 与速度族三个误差键值恒 null、err_source='none'、书面
    # 原因 = velocity_family_note（M-6 现状）/「已检出」（F-94③ 禁 0 冒充；
    # 未检出分支由 lines._assemble_nodetect 自带 count 通道，不在此覆盖）。
    for _k in ('fwhm_intr_err_aa', 'vel_fwhm_err_kms', 'vel_shift_err_kms'):
        row[_k] = None
        row['err_source'][_k] = 'none'
        row['err_scope'][_k] = 'stat'
    row.setdefault('upper_limit_3sigma', None)
    if row['upper_limit_3sigma'] is None:
        row['err_source']['upper_limit_3sigma'] = 'none'
        row['err_scope']['upper_limit_3sigma'] = 'stat'
    # baseline{type, order, cond_2, n_nodes}（§4.2 形态）：type=poly（F-36），
    # n_nodes=每侧基线点数（C_BASE_SIDE，lines._sideband_idx 的固定设计值）
    bl = dict(row.get('baseline') or {})
    bl.setdefault('type', 'poly')
    bl['n_nodes'] = C_BASE_SIDE
    row['baseline'] = bl
    row.setdefault('warnings', [])
    row['warnings'].extend([])

    sig_method_eff = 'second_diff_degraded' if factor > 1 else sg['sig_method']
    # ── U-48/P3d：吸收系统族装配（全关 ⇒ 空数组/恒 null/未启用 gate；三新键 +
    # cross_link_gate 恒在场，F-105①/T-72①②；剖面求值与自助共用预算池，F-95③） ──
    systems, xlink_gate, forest_reasons = _absorber_stage(
        lam_a, flam_a, sig_a, d_flags, z_eff, z_explicit, r_res, r_source,
        row, line_in,
        budget_left=C_BOOT_N_BUDGET - int(row.get('n_boot') or 0))
    # absorber_system_masked[]（§4.2 键位、P3d 评审 P1-3 补齐）：本族掩膜段并集
    # （与 absorber_systems[].masked_ranges 同源，不另立通路——F-103①/F-107①
    # 同一条 mask_hash 第 4 槽）；U-48 关/无段 ⇒ []（键恒在场，F-94④ 键集纪律）
    sys_masked = _union_segs([s for e in systems
                              for s in (e.get('masked_ranges') or [])])
    body_out = {
        'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': top_warn,
        'spectrum_id': ls['spectrum_id'], 'spectrum_source': ls['source'],
        'spec_hash': ls['spec_hash'], 'tid': ls['tid'],
        'meta': {k: meta.get(k) for k in ('ra_deg', 'dec_deg', 'z', 'category',
                                          'mjd', 'time_precision_d',
                                          'lambda_frame')},
        'meta_provenance': ls['meta_provenance'],
        'lambda_frame_converted': ls['lambda_frame_converted'],
        'n_below_convert': ls['n_below_convert'],
        'mw': mw,
        'line_requested': {'species': line_in.get('species'),
                           'lambda_rest_aa': lam_rest,
                           'id_table': line_in.get('id_table')},
        'line_kind': line_kind, 'line_frame': line_frame,
        'r_source': r_source, 'sky_handling': sky_handling,
        'z_eff': z_eff, 'lambda_center_obs_aa': lam_center,
        'half_width_aa': half_width,
        # W-25 闸门结构回显（两条各自在场，vel_*/z_fit 本期恒 null，M-6）
        'frame_gates': {
            'w25_1_line_side': {'line_frame': line_frame,
                                'verified': line_frame in _LINE_FRAMES,
                                'reason': ('line_frame_unverified'
                                           if line_frame not in _LINE_FRAMES
                                           else None)},
            'w25_2_spectrum_side': {'lambda_frame': spec_frame,
                                    'blocked': spec_frame == 'unknown',
                                    'reason': ('lambda_frame_unknown'
                                               if spec_frame == 'unknown'
                                               else None)},
            'vel_outputs_enabled': False,      # M-6 未复核 ⇒ P3c 启用
        },
        'mask_hash': mask_h,
        'preprocess': pre.response_block(
            masks, mask_h, pp_h, factor,
            factor == 1 and not masks['user'] and ec['ev']['verdict'] == 'ok'
            and not ec['errcol_accepted'],
            ec['ev']['verdict'], ec['errcol_accepted'],
            0, False,
            rebin=(rebin_info['echo'] if factor > 1 else None), s2=None),
        'read_stats': ls['read_stats'],
        'sigma_method': sig_method_eff, 'rho_lag1': sg['rho'],
        'errcol_verdict': ec['ev']['verdict'],
        'errcol_accepted': ec['errcol_accepted'],
        'lines': [row],
        # diagnostics（F-105①/T-72②：absorber_systems/forest_stats/
        # forest_stats_reasons + cross_link_gate 四键恒在场；U-48 全关时前三者
        # 空数组/恒 null/闭集原因集，cross_link_gate 取"未启用"值，数值与既有键
        # 与 P3c 基线逐字节相同——T-72① 的比较域不含这四键；P3d 评审 P1-3 追加
        # absorber_system_masked（§4.2 键位，恒在场、空时 []））
        'diagnostics': {'absorber_systems': systems,
                        'forest_stats': None,
                        'forest_stats_reasons': forest_reasons,
                        'cross_link_gate': xlink_gate,
                        'absorber_system_masked': sys_masked},
    }
    with _cache_lock:
        _result_cache[ckey] = body_out
        _result_cache.move_to_end(ckey)
        while len(_result_cache) > C_CACHE_MAX:
            _result_cache.popitem(last=False)
    return body_out
