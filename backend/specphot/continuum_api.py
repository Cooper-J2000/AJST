"""specphot.continuum_api —— API-3（POST /continuum，S2 连续谱拟合主计算，P2 切片 2b）。

编排（§5.2.2 + §3.8 + F-87…F-90）：请求校验（Q-13/14/15/32/33/34/35）→ 装载
（库内/上传，复用 photometry 的装载与 preprocess 管线——掩膜/F-110 判定/去偏 σ
全走同一代码路径）→ 银河消光（F-59②，观测系、拟合之前）→ 拟合区选段（§3.8
窗口语义：谱实际覆盖内、四类掩膜并集外、非正流量剔除并回显）→
continuum.fit_spectrum/fit_poly（F-91 唯一引擎）→ 宿主消光三态（F-87）→
§4.2 FitResult 契约装配（host_ext_basis 七键 F-88、de_reddened F-89、比较
F-29、闭包 F-30）→ 缓存（ST-3，双哈希进键）。
全链只读（RO-1…RO-5）；de_reddened 是纯派生件（derivation_depth=1，不落盘、
不入库、不生成子谱，T-57）；ST-5 重档硬超时 5 s（§7.6 表值）。

宿主消光三态（F-87，唯一入口 host_ext_mode，Q-32）：
  off       ⇒ 消光因子恒 1（pl_dust 以 A_V≡0 的钉死分支拟合，n_par 少一）；
  fit（默认）⇒ A_V 自由，事后 ebv_display = A_V/R_V 只作显示（F-90 禁回灌）；
  prescribe ⇒ A_V ≡ R_V·E(B−V) 由 continuum.av_ebv（T-55 全模块唯一换算）
              钉死、退出自由参数表，挂 CA-43（给定而非测得，F-87b/CA-43）。
  off/fit 携带 ebv ⇒ 忽略并回显 ebv_ignored=true（Q-32/F-90②），数值与不带
  逐字节同（T-58）；ebv 仍进缓存键 ⇒ 该标志按请求正确回显。
"""
import math
import time

import numpy as np
from flask import jsonify, request

from sedfit import laws as _host_laws          # F-88③：_NOMINAL_RV/_INTRINSIC_RV 实测值
from sedfit.models import _dl_cm as _host_dl_cm   # 宿主纯计算件（Planck18，F-27 复用）

from . import (specphot_bp, SPEC_PHOT_VERSION, _compute_gate, _err,
               _load_error_response, _result_cache, _cache_lock)
from . import continuum as CT
from . import errors as errs
from . import preprocess as pre
from .constants import (C_AA_PER_S, C_BOOT_N, C_BIC_MIN,
                        C_CACHE_MAX, C_HOST_EBV_MAX, C_MAX_BOOT,
                        C_MAX_MODELS, C_POLY_ORDER_MAX)
from .meta import load_spectrum_by_id
from .photometry import (_apply_mw, _errcol_stage, _load_upload_arrays,
                         _sigma_stage)
from .reader import SpecLoadError

LAWS = ('smc', 'lmc', 'mw', 'mw_f99', 'mw_ccm')      # Q-14 五键（宿主 laws.py 全集）
# F-64① 定义域（x=1e4/λ µm⁻¹）：mw=P92 x∈[0.001,1000]；其余四律 x∈[0.3,10]
_LAW_DOMAIN_AA = {'mw': (10.0, 1e7)}
_FIXED_RV_LAWS = ('smc', 'lmc', 'mw')                 # 其余两律 R_V 直接传参（F99/CCM89）


class _ContError(Exception):
    def __init__(self, code, reason, message, details=None):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.details = details or {}


def _bad(reason, message, details=None):
    return _ContError('E-14', reason, message, details)


def _warn(code, message, **kw):
    w = {'code': code, 'message': message}
    w.update(kw)
    return w


def _resolve_rv(law, rv_req, rv_source_req):
    """F-88③/F-64②：R_V 基准裁决 → (rv, rv_source, rv_source_note)。

    标称/内禀两基准取宿主 laws.py 实测值（lmc 3.16 vs 3.41，T-56）；
    重标定只准走 _INTRINSIC_RV。smc/lmc/mw 的曲线无 R_V 参数：请求值必须
    恰为两基准之一（任意的"第三只 R_V"拒绝，F-64②）；mw_f99/mw_ccm 的
    R_V 直接传入律内（域 [1.5,6]，宿主 rv_schema），无内禀重标定概念，
    rv_source 恒记 nominal。"""
    nominal = _host_laws._NOMINAL_RV[law]
    intrinsic = _host_laws._INTRINSIC_RV.get(law)
    if law not in _FIXED_RV_LAWS:
        if rv_source_req == 'intrinsic':
            raise _bad('rv_source', f'{law} 律的 R_V 直接传入 F99/CCM89，'
                                    '无内禀重标定基准（F-88③）')
        rv = 3.1 if rv_req is None else float(rv_req)
        if not (math.isfinite(rv) and 1.5 <= rv <= 6.0):
            raise _bad('host_ext_mode', 'rv 须在 [1.5, 6]（宿主 rv_schema，Q-14）')
        return rv, 'nominal', ('R_V 直接传入律内（F99/CCM89 参数化律，无标称/内禀'
                               '之分，展示记 nominal）；本次取值 '
                               f'R_V={rv:.2g}')
    if rv_req is None:
        if rv_source_req == 'intrinsic':
            if intrinsic is None:
                raise _bad('rv_source', f'{law} 律无内禀基准（laws.py:27）')
            return intrinsic, 'intrinsic', (
                f'R_V 取宿主 sedfit/laws.py:27 _INTRINSIC_RV 的内禀值 '
                f'（重标定只准走此值，F-64②）：R_V={intrinsic:.2g}')
        return nominal, 'nominal', (
            f'R_V 取宿主 sedfit/laws.py:25 _NOMINAL_RV 的标称值：'
            f'R_V={nominal:.2g}')
    rv = float(rv_req)
    for src, val in (('nominal', nominal), ('intrinsic', intrinsic)):
        if val is not None and math.isclose(rv, val, rel_tol=0, abs_tol=1e-9) \
                and (rv_source_req is None or rv_source_req == src):
            if src == 'nominal':
                return rv, 'nominal', (
                    f'R_V 取宿主 sedfit/laws.py:25 _NOMINAL_RV 的标称值：'
                    f'R_V={rv:.2g}')
            return rv, 'intrinsic', (
                f'R_V 取宿主 sedfit/laws.py:27 _INTRINSIC_RV 的内禀值 '
                f'（重标定只准走此值，F-64②）：R_V={rv:.2g}')
    raise _bad('rv_source',
               f'{law} 律的曲线无 R_V 参数：rv 只能取标称值 {nominal} 或内禀值 '
               f'{intrinsic}（重标定只准走 _INTRINSIC_RV，F-64②/F-88③）')


def _host_ext_basis(law, rv, rv_source, rv_source_note, z_eff):
    """F-88 七键（缺任一键 = 契约失败 T-56）：law / rv / rv_source /
    rv_source_note / screen_z / lam_axis / form。screen_z 一律取生效 z
    （本方案不设中间消光屏，TXT-22）；lam_axis = 静止系真空 λ；
    form = §4.2 所列关系式完整字符串（T-56 按**等值**断言）。"""
    return {'law': law, 'rv': float(rv), 'rv_source': rv_source,
            'rv_source_note': rv_source_note,
            'screen_z': (float(z_eff) if z_eff is not None else None),
            'lam_axis': 'lambda_rest_vacuum_aa（静止系真空波长 λ_rest = λ_obs/(1+z)；'
                        '显示按 FZ-1 折回观测系）',
            'form': 'A_V = R_V·E(B−V)；A_λ = A_V·[A(λ)/A(V)]'}


def _law_clipped(lam_rest, law):
    """F-64①：laws.get_law 对越界 np.clip 平推且静默 ⇒ 本模块回显被截断的
    静止系波长并挂 CA-24（远紫外端平推会低估消光）。"""
    lo, hi = _LAW_DOMAIN_AA.get(law, (1000.0, 1e4 / 0.3))
    out = np.unique(np.round(lam_rest[(lam_rest < lo) | (lam_rest > hi)], 4))
    return [float(x) for x in out]


# CA-38（§5.4）实现约定（P2b 评审落地，登记）：
#   ① 「2000–2300 Å 附近」取闭区间 [2000, 2300]（静止系）；
#   ② 「近紫外点」取 λ_obs < 2400 Å（规格行未给数值；2400 Å 是地面近紫外
#      截止与 IUE 短波端之间的常用 NUV 上界，作实现约定登记）。
CA38_NUV_OBS_AA = 2400.0


def _ca38_break_at_dust_bump(lam, flux, nu_b, z_eff):
    """CA-38 触发判定（纯函数以便直接单测）：pl2 断点 λ_rest = c/ν_b/(1+z)
    落在 [2000, 2300] Å（静止系，实现约定见 CA38_NUV_OBS_AA 注）且谱覆盖内
    无 λ_obs < 2400 Å 的有效点 ⇒ 该"断折"与 2175 Å 尘埃凸起不可区分。
    有效点 = 谱覆盖内 λ 与流量均有限的像素——按**谱覆盖**口径而非拟合区
    （掩膜掉的近紫外像素仍是数据的一部分，F-32 的覆盖口径）；触发返回
    (True, λ_rest)，否则 (False, λ_rest)。"""
    if nu_b is None:
        return False, None
    lam_obs_b = C_AA_PER_S / float(nu_b)
    lam_rest_b = lam_obs_b / (1.0 + float(z_eff)) if z_eff else lam_obs_b
    if not (2000.0 <= lam_rest_b <= 2300.0):
        return False, lam_rest_b
    lam = np.asarray(lam, dtype=float)
    flux = np.asarray(flux, dtype=float)
    has_nuv = bool(np.any(np.isfinite(lam) & np.isfinite(flux)
                          & (lam < CA38_NUV_OBS_AA)))
    return (not has_nuv), float(lam_rest_b)


def _de_reddened(ls, flux_corr, av, basis, host_ext_mode):
    """F-89：曲线 B = F_A·10^{+0.4·A_V·law(λ_rest)}（law 取在静止系真空 λ 上，
    与 F-59⑤ 同一位置、符号相反）。纯派生件：derivation_depth 恒 1、不落盘、
    不入库、不生成子谱（T-57）；host_ext_mode='off' 时无 B（depth=0、无曲线）。"""
    lam = np.asarray(ls['lam_aa'], dtype=float)
    f_a = np.asarray(flux_corr, dtype=float)
    out = {'n_points': 0, 'curve_hash': None, 'derivation_depth': 0,
           'exported': False, 'lam_obs_vac_aa': [], 'lam_rest_vac_aa': [],
           'flux_before': [], 'flux_after': [], 'a_host_mag': [],
           'host_ext_basis': basis}
    if av is None or host_ext_mode == 'off':
        return out
    z = basis['screen_z'] or 0.0
    lam_rest = lam / (1.0 + z) if z else lam
    a_host = float(av) * _host_laws.get_law(basis['law'], basis['rv'])(lam_rest)
    flux_after = f_a * 10.0 ** (0.4 * a_host)
    out.update({
        'n_points': int(lam.size),
        'curve_hash': pre.f80_hash([list(lam), list(f_a), list(flux_after)]),
        'derivation_depth': 1, 'exported': False,
        'lam_obs_vac_aa': [float(x) for x in lam],
        'lam_rest_vac_aa': [float(x) for x in lam_rest],   # §4.3 B 块列 2（定义导出）
        'flux_before': [float(x) for x in f_a],
        'flux_after': [float(x) for x in flux_after],
        'a_host_mag': [float(x) for x in a_host],
        # TXT-22 逐字（F-89③：导出件首行 TXT-11 之外旁注本句；库里没有这条谱）
        'txt22': ('"改正后"是本页按所列基准派生的显示量，库里没有这条谱'
                  '（未入库、未生成子谱）'),
    })
    return out


def _continuum_error_response(e):
    """ContinuumError/_ContError → 线上错误响应（§5.3 映射，与 SpecLoadError 同表）。"""
    wire = {'E-01': 'spectrum_not_found', 'E-04': 'band_unusable',
            'E-07': 'input_over_limit', 'E-11': 'compute_timeout',
            'E-13': 'feature_disabled', 'E-14': 'bad_request_state'}.get(
        e.code, 'bad_request_state')
    http = {'E-01': 404, 'E-11': 504, 'E-13': 501}.get(e.code, 400)
    details = getattr(e, 'details', None) or {}
    return _err(wire, str(e), http, reason=getattr(e, 'reason', None), **details)


def _arbitrate_best(best, comparable, ftest):
    """verdict.best 仲裁（T-17/F-29②④；P2-7c 修复，纯函数以便直接单测）。

    规则：嵌套对的判定依据是 F 检验（p 与 C_FTEST_ALPHA 比）——显著 ⇒ 当前
    best（简者）让位给繁者，即使纯 BIC 偏好简者；纯 BIC 兜底只在非嵌套对或
    F 不显著时生效（调用方已按 BIC 取初值，本函数只在 F 显著时改写）。
    **多繁者并列显著按 BIC 仲裁**：同一简者的多个繁者同时 F 显著（如 pl 同时
    显著优于 pl_bb 与 pl_dust）时，原实现按 `sorted(NESTED_PAIRS)` 的排序序
    静默取先者（pl_bb 恒在 pl_dust 前）⇒ 取显著繁者中 BIC 最小者（F-29④：
    ΔBIC 是模型间唯一可用的并列证据，同屏回显于 dbic[] 可核对）。
    迭代到不动点以覆盖嵌套链（如 pl→pl_bb 同轮显著时的传递）。"""
    if best is None:
        return None
    by_name = {f['model']: f for f in comparable}
    changed = True
    while changed and best in by_name:
        changed = False
        for simple in sorted({s for (s, _r) in CT.NESTED_PAIRS}):
            if best != simple:
                continue
            # 该简者全部 F 显著的繁者（可多于一个）
            richs = sorted({r for (s, r) in CT.NESTED_PAIRS if s == simple
                            and any(e['pair'] == [s, r] and e['verdict'] == 'rich'
                                    for e in ftest)})
            if not richs:
                continue
            cand = min((by_name[m] for m in richs if m in by_name),
                       key=lambda f: f['bic'])
            if cand['model'] != best:
                best = cand['model']
                changed = True
    return best


# ─── API-3 ───────────────────────────────────────────────────────────────

@specphot_bp.route('/continuum', methods=['POST'])
def continuum():
    if not _compute_gate.acquire(blocking=False):
        return _err('server_busy', '已有计算在跑，请稍后重试',
                    429, reason='busy', retry_after_s=2)
    try:
        try:
            body_out = _compute_continuum()
        except (SpecLoadError, CT.ContinuumError, _ContError) as e:
            if isinstance(e, SpecLoadError):
                return _load_error_response(e)
            return _continuum_error_response(e)
        return jsonify(body_out)
    finally:
        _compute_gate.release()


def _compute_continuum():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise _bad('bad_enum', '请求体应为 JSON 对象')
    # F-105③/T-72④：U-48 吸收系统三键只属 API-4，API-3 收到 ⇒ 忽略但回显
    # diagnostics_ignored[]（F-90② 同族纪律；未收到时键不出现，既有响应不变）。
    _diag_req = body.get('diagnostics')
    _ignored_u48 = [k for k in ('absorber_ident', 'wing_logn',
                                'metal_sat_check')
                    if isinstance(_diag_req, dict) and _diag_req.get(k) is True]
    # Q-23：来源二选一（与 API-2 同一装载通路）
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
    # Q-13：模型集 ⊆ §3.8.1 别名表 ∪ {poly}；powerlaw_3seg 不纳入
    models_raw = body.get('models')
    if not isinstance(models_raw, list) or not all(isinstance(m, str) for m in models_raw):
        raise _bad('bad_enum', 'models 必须为字符串数组（Q-13）')
    models = list(dict.fromkeys(models_raw))
    if not 1 <= len(models) <= C_MAX_MODELS:
        raise _bad('too_many_models', f'models 数量须在 1..{C_MAX_MODELS}（Q-13）')
    known = set(CT.alias_table()) | {'poly'}
    for m in models:
        # P2-6 笔误修正：原条件把同一字面量比较写了两遍（`m == 'powerlaw_3seg'
        # or m == 'powerlaw_3seg'`），行为等价于单次比较，删去重复支。§3.8 模型
        # 词表核对：powerlaw_3seg（宿主 MODELS 注册表键）在全期（含 P2b/P2c）都
        # 不纳入 S2 模型集 ⇒ 本分支保持拒绝语义不变；宿主其余段模型
        # （powerlaw_2seg 等）不在 known 别名表 ∪ {poly} 内，走下面的通用
        # 未知别名拒绝（Q-13）。
        if m == 'powerlaw_3seg':
            raise _bad('bad_enum', 'powerlaw_3seg 不纳入 S2 模型集（§3.8.1：两断点'
                                   '三斜率在库内谱覆盖下不可约束）')
        if m not in known:
            raise _bad('bad_enum', f'未知模型别名 {m!r}（Q-13：可用 '
                                   f'{sorted(known)}，键为别名不是类名）')
    # Q-33：preprocess{} 与 API-2 同形（同一 validate_request；P2 切片 2c 起
    # factor>1 合束与 smooth 平滑放行，见 F-108/F-109）
    pp = body.get('preprocess') or {}
    if not isinstance(pp, dict):
        raise _bad('bad_enum', 'preprocess 必须为 JSON 对象')
    factor, pp_ranges, smooth_px, errcol_choice = pre.validate_request(pp)
    sigma_policy = body.get('sigma_policy', 'auto')
    if sigma_policy not in ('auto', 'force_proxy'):
        raise _bad('bad_enum', "sigma_policy 只能是 'auto'/'force_proxy'（实现裁量："
                               "与 API-2 err_policy 同义）")
    # Q-14：law 五键；rv_free 的 R_V 自由度在本期参数化中不开放（登记遗留）
    law = body.get('law', 'smc')
    if law not in LAWS:
        raise _bad('bad_enum', f'law={law!r} 非法（Q-14：五键 {list(LAWS)}）')
    rv_free = body.get('rv_free', False)
    if not isinstance(rv_free, bool):
        raise _bad('bad_enum', 'rv_free 必须为布尔（Q-14）')
    if rv_free:
        raise _bad('rv_free_unsupported',
                   'rv_free=true（R_V 与 A_V 同时拟合，F-64③/CA-41）未随本切片开放；'
                   'R_V 请按标称/内禀基准给定（host_ext_basis.rv_source）')
    rv_req = body.get('rv')
    if rv_req is not None:
        try:
            rv_req = float(rv_req)
        except (TypeError, ValueError):
            raise _bad('bad_enum', 'rv 必须为数字（Q-14）')
    rv_source_req = body.get('rv_source')
    if rv_source_req is not None and rv_source_req not in ('nominal', 'intrinsic'):
        raise _bad('rv_source', "rv_source 只能是 'nominal'/'intrinsic'（F-88③）")
    # Q-32：host_ext_mode 三态（缺省 fit = 现状）
    host_ext_mode = body.get('host_ext_mode', 'fit')
    if host_ext_mode not in ('off', 'fit', 'prescribe'):
        raise _bad('host_ext_mode', 'host_ext_mode ∈ {off, fit, prescribe}（Q-32）')
    ebv_req = body.get('ebv')
    if ebv_req is not None:
        try:
            ebv_req = float(ebv_req)
        except (TypeError, ValueError):
            raise _bad('host_ext_mode', 'ebv 必须为数字（Q-32）')
        if not math.isfinite(ebv_req) or ebv_req < 0:
            raise _bad('host_ext_mode', 'ebv 必须为非负有限数（Q-32）')
    ebv_ignored = host_ext_mode in ('off', 'fit') and ebv_req is not None
    if host_ext_mode == 'prescribe':
        if ebv_req is None:
            raise _bad('host_ext_mode', "host_ext_mode='prescribe' ⇒ ebv 必填"
                                        '（Q-32；无默认，U-46）')
        if ebv_req > C_HOST_EBV_MAX:
            raise _bad('host_ext_mode', f'ebv={ebv_req} 超过 C_HOST_EBV_MAX='
                                        f'{C_HOST_EBV_MAX}（Q-32；R_V=3.1 时 '
                                        'A_V 已越过宿主 [0,6] 域，§7.5）')
    poly_order = body.get('poly_order', 3)
    if isinstance(poly_order, bool) or not isinstance(poly_order, int) \
            or not 1 <= poly_order <= C_POLY_ORDER_MAX:
        raise _bad('bad_enum', f'poly_order 须为 [1, {C_POLY_ORDER_MAX}] 内整数'
                               '（Q-15，仅 model=poly 消费）')
    nu0 = body.get('nu0')
    if nu0 is None:
        nu0 = CT.NU0_DEFAULT
    try:
        nu0 = float(nu0)
    except (TypeError, ValueError):
        raise _bad('bad_enum', 'nu0 无法数值化')
    if not (math.isfinite(nu0) and nu0 > 0):
        raise _bad('bad_enum', 'nu0 必须为正有限数（宿主约定 5e14 Hz，原样回显）')
    z_req = body.get('z')
    if z_req is not None:
        try:
            z_req = float(z_req)
        except (TypeError, ValueError):
            raise _bad('bad_enum', 'z 无法数值化')
        if not (math.isfinite(z_req) and z_req >= 0):
            raise _bad('bad_enum', 'z 必须为非负有限数')
    n_boot = body.get('n_boot', C_BOOT_N)
    if isinstance(n_boot, bool) or not isinstance(n_boot, int) \
            or not 1 <= n_boot <= C_MAX_BOOT:
        raise _bad('bad_enum', f'n_boot 须为 [1, {C_MAX_BOOT}] 内整数（Q-15）')
    err_seed = body.get('err_seed')
    if err_seed is not None and (isinstance(err_seed, bool)
                                 or not isinstance(err_seed, int)
                                 or not 0 <= err_seed <= 2 ** 63 - 1):
        raise _bad('err_seed', 'err_seed 须为 null 或 [0, 2^63-1] 内整数（Q-35）')
    grid_max = body.get('grid_max_per_axis')
    if grid_max is not None and (isinstance(grid_max, bool)
                                 or not isinstance(grid_max, int) or grid_max < 3):
        raise _bad('bad_enum', 'grid_max_per_axis 须为 null 或 ≥3 的整数'
                               '（恒不超过 C_GRID_MAX_PER_AXIS）')
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

    # ST-5 重档硬超时：§7.6 表值 5 s（S2/S3/API-8 档；不另设规格外常量名）
    deadline = time.monotonic() + 5.0

    def _deadline_check():
        if time.monotonic() > deadline:
            raise SpecLoadError('E-11', '计算超时（ST-5 重档硬超时 5 s），'
                                        '建议减少模型数或缩小谱规模', status=504)

    # 装载（库内/上传同一通路）+ 银河消光 + 误差列判定 + 代理 σ（全部复用
    # photometry 的管线函数——预处理管线同源，无第二套实现）
    ls = load_spectrum_by_id(sid) if sid is not None else _load_upload_arrays(spec)
    meta, top_warn = ls['meta'], list(ls['warnings'])
    lam = ls['lam_aa']
    masks = pre.merge_masks(lam, list(mask_raw) + pp_ranges)
    if masks['n_ignored']:
        top_warn.append(_warn('CA-10', f"{masks['n_ignored']} 个掩膜段无效或与谱覆盖"
                                       '无重叠，已忽略（V-13）'))
    flux_corr, mw = _apply_mw(ls, mwq, ebv_override, top_warn)
    ec = _errcol_stage(ls, flux_corr, errcol_choice, sigma_policy, top_warn)
    ev, errcol_accepted = ec['ev'], ec['errcol_accepted']
    mask_h = pre.mask_hash(masks, lam, ls['spec_hash'])
    pp_h = pre.preprocess_hash(mask_h, factor, ev['verdict'], errcol_accepted,
                               ls['spec_hash'])
    ckey = pre.f80_hash(['continuum', sid, ls['spec_hash'], models, mask_h, pp_h,
                         law, rv_req, rv_source_req, host_ext_mode,
                         ebv_req,      # Q-32：off/fit 不消费 ebv，但入键 ⇒ ebv_ignored
                                       # 标志随请求正确回显（T-58 的数值仍逐字节同）
                         nu0, z_req, n_boot, err_seed, poly_order, grid_max,
                         sigma_policy, mwq.get('correct', True), ebv_override,
                         SPEC_PHOT_VERSION,
                         # 上传件 meta 是请求自带输入（A-7，与 API-2 同理进键）
                         meta], n=16)
    with _cache_lock:
        if ckey in _result_cache:
            _result_cache.move_to_end(ckey)
            return _result_cache[ckey]
    if ec['ca47_note']:
        top_warn.append(_warn('CA-47', ec['ca47_note'][1],
                              reason=f"errcol_{ec['ca47_note'][0]}"))
    sg = _sigma_stage(ls, flux_corr, ec['use_col'], len(lam), top_warn)
    rho, sigma_px = sg['rho'], sg['sigma_px']
    if not bool(np.any(np.asarray(sigma_px, dtype=float) > 0)):
        raise _bad('sigma_unavailable', '误差列与代理 σ 均不可用：S2 拟合需要逐像素 '
                                        'σ（不得以 0 冒充，TXT-23）')
    # 离群点候选（F-111②③/T-79③，与 API-2 同一通路）：只标记不剔除——S2 拟合区
    # 只按掩膜/非正剔除，候选占比超限 ⇒ CA-47④。
    n_outlier, _outlier_segs, outlier_over = pre.outlier_candidates(lam, flux_corr)
    if outlier_over:
        top_warn.append(_warn('CA-47', f'σ 裁剪离群候选 {n_outlier} 个，占比超过 '
                                       f'C_CLIP_MAX_FRAC：问题在基线不在离群点'
                                       '（F-111③）', reason='outlier_frac_over'))
    elif n_outlier:
        top_warn.append(_warn('CA-10', f'σ 裁剪发现 {n_outlier} 个离群候选'
                                       '（只标记未剔除，F-111②）',
                              reason='outlier_flagged'))
    # ── P2 切片 2c（F-108）：合束主通路（与 API-2 同一入口 pre.rebin_stage；
    # 次序 F-106①：消光 ⇒ 掩膜 ⇒ 合束 ⇒ 才进拟合）。σ 通路在原始像元上估，
    # 块内相关噪声按 F-108③ 通式合成 σ_bin；合束后 sigma_method=
    # 'second_diff_degraded' + CA-06（F-108⑥），verdict≠ok 时按 F-108⑦ 化约序
    # 覆盖回落值（CA-47①② 与 CA-06 并现）。r_source='none' ⇒ CA-47③。
    rebin_info = None
    if factor > 1:
        rebin_info = pre.rebin_stage(lam, flux_corr, sg['sigma_px'],
                                     masks['mask_bool'], factor, rho)
        top_warn.append(_warn('CA-06', f'谱已合束（factor={factor}）：白噪声前提被'
                                       '破坏，sigma_method=second_diff_degraded'
                                       '（F-108⑥/F-97⑦）'))
        top_warn.append(_warn('CA-47', '无仪器 R ⇒ 欠分辨闸门无从判定，无法保证未'
                                       '抽到欠分辨（F-108④/CA-47③）；合束仍可用',
                              reason='rebin_r_source_none'))
        top_warn.extend(pre.ca49_warnings(factor, rebin_info['echo']['rebin_gain'],
                                          rebin_info['echo']['px_per_fwhm_after'],
                                          rebin_info['rho_echoed']))
    sig_method_eff = 'second_diff_degraded' if factor > 1 else sg['sig_method']
    # ── 拟合区选段（§3.8 窗口语义）：谱实际覆盖内、四类掩膜并集外（F-32 去线
    # 复用 preprocess 掩膜；F-35/F-36 基线掩膜随 S3 切片并类）；像素不做低信噪
    # 硬剔除（F-32：按 σ 降权），非正流量像素剔除并回显计数（V-6 同口径）。
    # factor>1 时拟合区 = 抽后完整块（被掩像素已在块均值外，F-106①），掩膜段
    # 不再二次施加（先掩膜后合束的次序使然）。──
    lam_a = np.asarray(lam, dtype=float)
    flam_a = np.asarray(flux_corr, dtype=float)
    sig_a = np.asarray(sg['sigma_px'], dtype=float)
    unmasked_ok = (~masks['mask_bool']) & np.isfinite(flam_a)
    n_nonpos = int(np.count_nonzero(unmasked_ok & (flam_a <= 0)))
    n_unmasked = int(np.count_nonzero(unmasked_ok))
    nonpos_frac = (n_nonpos / n_unmasked) if n_unmasked else 0.0
    if factor > 1:
        lam_a = rebin_info['lam']
        flam_a = rebin_info['flux']
        sig_a = rebin_info['sigma']
        sel = np.isfinite(flam_a) & (flam_a > 0) & np.isfinite(sig_a) & (sig_a > 0)
    else:
        sel = unmasked_ok & (flam_a > 0) & np.isfinite(sig_a) & (sig_a > 0)
    n_fit = int(np.count_nonzero(sel))
    if n_fit < 8:
        raise SpecLoadError('E-04', f'拟合区可用像素 {n_fit} < 8（掩膜/非正剔除'
                                    f'{"/合束" if factor > 1 else ""}后，'
                                    f'too_few_pixels）', reason='too_few_pixels')
    lam_f = lam_a[sel]
    nu_f = C_AA_PER_S / lam_f
    fnu_f = flam_a[sel] * lam_f ** 2 / C_AA_PER_S     # Fν = Fλ·λ²/c（cgs）
    sig_nu = sig_a[sel] * lam_f ** 2 / C_AA_PER_S
    z_eff = meta['z'] if z_req is None else z_req
    has_uv = bool(lam_f.min() < 3000.0)

    # 宿主消光三态装配（F-87…F-90）
    rv, rv_source, rv_note = _resolve_rv(law, rv_req, rv_source_req)
    basis = None
    av_pin = None
    if host_ext_mode != 'off':
        basis = _host_ext_basis(law, rv, rv_source, rv_note, z_eff)
        if host_ext_mode == 'prescribe':
            # F-87③/F-90②：唯一允许进计算的 E(B−V)→A_V 方向，走 T-55 唯一算术
            av_pin = CT.av_ebv(ebv=ebv_req, rv=rv)
            top_warn.append(_warn(
                'CA-43', f"host_ext_mode='prescribe'：A_V={av_pin:.4g} 由用户给定的 "
                f'E(B−V)={ebv_req:.4g} 经 A_V = R_V·E(B−V) 钉死，是假定不是测值；'
                '幅值 A 与斜率 β 会吸收残余红化，其不确定度不在误差预算内（F-87b）'))
    # off 分支：逐模型 cfg 内给 av_fixed=0.0（消光因子恒 1，F-87①）。
    # 凡含尘埃的模型 = pl_dust/pl2（F-87 前言/Q-32「本次请求中凡含尘埃的模型
    # 一律按固定 A_V 计」），三态钉死同步作用于两者。
    _DUST_ALIASES = ('pl_dust', 'pl2')
    if host_ext_mode != 'off' and not z_eff \
            and any(m in _DUST_ALIASES for m in models):
        # Q-16：含尘埃模型而谱无 z ⇒ 允许，宿主消光只是观测系等效值（CA-23）
        top_warn.append(_warn('CA-23', '谱无红移：宿主消光取在观测系波长上，只是观测系'
                                       '等效值（Q-16/F-87d；laws.extinguish z=None 分支）',
                              reason='host_no_z'))
    if host_ext_mode == 'off' and any(m in _DUST_ALIASES for m in models):
        top_warn.append(_warn('CA-43', "host_ext_mode='off'：pl_dust/pl2 的消光因子恒 1"
                                       '（A_V≡0 钉死分支），模型退化为无尘埃形式'
                                       '（F-87①）', reason='host_off'))

    # 逐模型拟合（F-91 唯一入口；poly 走 F-62 线性层）。cfg 按别名存档：
    # Davies 参数化自助（F-29③）须以与主拟合**同一份** cfg 重拟两侧。
    fits = []
    cfg_by_alias = {}
    for alias in models:
        _deadline_check()
        if alias == 'poly':
            fit = CT.fit_poly(lam_f, flam_a[sel], sig_a[sel], order=poly_order,
                              mask_hash=mask_h, nonpos_frac=nonpos_frac)
            fit['comparable'] = True                 # 线性层无引擎收敛闸（F-91 域外）
            fit['warnings'].append(_warn(
                'CA-11', 'poly 是经验基线（Chebyshev·lnλ），无物理含义：'
                         '参数不可作物理诠释（§3.8.1）'))
        else:
            cfg = {'nu0': nu0, 'z': z_eff, 'dl_cm': (_host_dl_cm(z_eff)
                                                    if z_eff else None),
                   'law': law, 'rv': rv, 't_sel_d': None, 'has_uv': has_uv}
            if alias in _DUST_ALIASES and host_ext_mode != 'fit':
                cfg['av_fixed'] = 0.0 if host_ext_mode == 'off' else av_pin
            cfg_by_alias[alias] = cfg
            fit = CT.fit_spectrum(alias, nu_f, fnu_f, sig_nu, config=cfg,
                                  spec_hash=ls['spec_hash'], mask_hash=mask_h,
                                  err_method='profile', n_boot=n_boot,
                                  err_seed=err_seed, grid_max_per_axis=grid_max)
        if not fit.get('grid_converged', True):
            # F-65/CA-24：最优解落在网格端点 ⇒ 报告值只是边界值、不是似然峰
            fit.setdefault('warnings', []).append(_warn(
                'CA-24', f"模型 {alias} 的网格最优解落在搜索网格端点，"
                         f"报告值是边界值而非似然峰（F-65）"))
        fits.append(fit)
    _deadline_check()

    # 消光律越界回显（§5.4 合并表：原 CA-24① 已并入 CA-23，reason=law_clipped）
    lam_rest_full = lam_f / (1.0 + z_eff) if z_eff else lam_f
    clipped = _law_clipped(lam_rest_full, law) if host_ext_mode != 'off' else []
    if clipped:
        top_warn.append(_warn('CA-23', f'{len(clipped)} 个像素的静止系波长越出 {law} '
                                       f'律定义域（np.clip 平推会低估消光，F-64①）',
                              reason='law_clipped'))

    # 模型比较（F-29/T-17）：嵌套对 F 检验判定 + ΔBIC 并列信息各按各自门限回显、
    # 不强求一致；非嵌套对只报 ΔBIC（不输出 F 检验字段）。同 mask_hash 由构造保证。
    # pl→pl2（DAVIES_PAIRS，F-29③）例外：compare_models 只发
    # davies_bootstrap_required 引导标记（常规 F/Δχ²/ΔBIC 都会高估显著性），
    # 判定在此就地走 davies_bootstrap 的参数化自助经验零分布——以**主拟合同一份
    # cfg**（av_fixed 钉死态一致）与同一白化通路重抽，结果进 verdict.davies[]
    # （p_method='parametric_bootstrap'，边界 χ² 混合 50:50 只作旁证）。
    by_alias = {f['model']: f for f in fits}
    # CA-38（§5.4，P2b 评审落地）：pl2 断点落在 2000–2300 Å（静止系）附近且
    # 覆盖内无近紫外点 ⇒ 该"断折"与 2175 Å 凸起不可区分 ⇒ 断点结论必须保守，
    # 优先看含尘埃模型。文案即保守 verdict 文案：断点/verdict.best 不得据此
    # 单独作物理结论；告警同挂 pl2 模型卡（fit.warnings），随模型一起回显。
    if 'pl2' in by_alias:
        ca38_hit, lam_rest_b = _ca38_break_at_dust_bump(
            lam, flux_corr, by_alias['pl2']['params'].get('nu_b'), z_eff)
        if ca38_hit:
            w38 = _warn(
                'CA-38', f'pl2 断点 λ_rest={lam_rest_b:.0f} Å 落在 2000–2300 Å'
                '（静止系）附近，且谱覆盖内无 λ_obs<2400 Å 的近紫外点：该'
                '"断折"与 2175 Å 尘埃凸起不可区分 ⇒ 断点结论必须保守——'
                'best/断折判定不得据此单独下物理结论，优先看含尘埃模型'
                '（pl_dust/pl2 的 A_V 与残差形态，F-29/L-23）',
                reason='nuv_gap')
            top_warn.append(w38)
            by_alias['pl2'].setdefault('warnings', []).append(w38)
    ftest, dbic, davies = [], [], []
    for (simple, rich) in sorted(CT.NESTED_PAIRS):
        if simple in by_alias and rich in by_alias:
            r = CT.compare_models(by_alias[simple], by_alias[rich])
            if r['verdict'] == 'davies_bootstrap_required':
                davies.append(CT.davies_bootstrap(
                    by_alias[simple], by_alias[rich], nu_f, fnu_f, sig_nu,
                    cfg_by_alias.get(simple, {}), cfg_by_alias.get(rich),
                    n_boot=n_boot, seed=err_seed, spec_hash=ls['spec_hash'],
                    tick=_deadline_check))
            elif r['test'] == 'f_test':
                ftest.append({'pair': r['pair'], 'F': r['F'], 'p': r['p'],
                              'verdict': r['verdict'], 'alpha': r['alpha']})
                # T-17：嵌套对的 ΔBIC 只是并列信息（F-29④ 分档门限 C_BIC_MIN），
                # 与 ftest[] 同屏可核对（p 显著而 ΔBIC 证据不足的分离例即哨兵）
                d = float(by_alias[simple]['bic'] - by_alias[rich]['bic'])
                dbic.append({'pair': r['pair'], 'dbic': d,
                             'verdict': ('rich' if d > C_BIC_MIN else
                                         'simple' if d < -C_BIC_MIN else
                                         'inconclusive'),
                             'threshold': C_BIC_MIN})
    seen_pairs = set()
    for i in range(len(fits)):
        for j in range(i + 1, len(fits)):
            pair = (fits[i]['model'], fits[j]['model'])
            if pair in seen_pairs or tuple(reversed(pair)) in seen_pairs:
                continue
            seen_pairs |= {pair}          # 集合并集写法（RO 静态扫描不收变更法）
            if pair in CT.NESTED_PAIRS or tuple(reversed(pair)) in CT.NESTED_PAIRS:
                continue                  # 嵌套对的 ΔBIC 已在上面随 ftest 并列出
            r = CT.compare_models(fits[i], fits[j])
            if r['test'] == 'bic':
                dbic.append({'pair': r['pair'], 'dbic': r['dbic'],
                             'verdict': r['verdict'], 'threshold': C_BIC_MIN})
    comparable = [f for f in fits if f.get('comparable')]
    best = min(comparable, key=lambda f: f['bic'])['model'] if comparable else None
    best = _arbitrate_best(best, comparable, ftest)
    # F-29③：Davies 对的判定通路是参数化自助 ⇒ 只要本次请求实际评了该对，
    # p_method 就记 'parametric_bootstrap'（F-29③ 原文「回显 p_method=
    # 'parametric_bootstrap'」）；无该对时维持 'f_test'。
    verdict = {'best': best, 'ftest': ftest, 'dbic': dbic, 'davies': davies,
               'p_method': ('parametric_bootstrap' if davies else 'f_test')}

    # ebv_display（F-88：fit 下由 A_V 反算只作显示，走 T-55 唯一算术；F-90 禁回灌）
    av_display, av_display_src = None, None
    if host_ext_mode == 'fit':
        dust_fits = [f for f in comparable
                     if f['model'] in ('pl_dust', 'pl2') and 'Av' in f['params']]
        pick = next((f for f in dust_fits if f['model'] == best), None) or \
            (dust_fits[0] if dust_fits else None)
        if pick is not None:
            av_display = pick['params']['Av']
            av_display_src = pick
    elif host_ext_mode == 'prescribe':
        av_display = av_pin
        av_display_src = None                      # 给定值无拟合 σ（CA-43）

    # 曲线 B（F-89）：A_V 来源 = prescribe 钉死值 / fit 的最优可比尘埃拟合
    de_red = _de_reddened(ls, flux_corr, av_display, basis, host_ext_mode)

    for fit in fits:
        fit['law'] = law
        fit['rv'] = rv
        fit['rv_free'] = rv_free
        fit['law_clipped_lam_a'] = clipped if fit['model'] in ('pl_dust', 'pl2') else []
        fit['host_ext_mode'] = host_ext_mode
        fit['host_ext_basis'] = basis
        fit['kcorr'] = 'none'
        fit['ebv_ignored'] = bool(ebv_ignored)
        if host_ext_mode == 'fit' and fit is av_display_src:
            fit['ebv_display'] = CT.av_ebv(av=av_display, rv=rv)   # 显示值，禁回灌
            sig_av = fit['sigma_theta'].get('Av')
            if sig_av is not None:
                fit['ebv_display_err'] = CT.av_ebv(av=sig_av, rv=rv)
                fit['err_source']['ebv_display_err'] = fit['err_source'].get('Av', 'none')
            else:
                fit['ebv_display_err'] = None
                fit['warnings'].append(_warn(
                    'CA-44', "A_V 的 σ 不可信（CA-44①②），ebv_display_err 按 null 出"
                             '并附此书面原因（TXT-23）'))
        elif host_ext_mode == 'prescribe':
            fit['ebv_display'] = None
            fit['ebv_display_err'] = None          # §3.9.3.1：prescribe 下 null + CA-43
        else:
            fit['ebv_display'] = None
            fit['ebv_display_err'] = None
        if fit['model'] in ('pl', 'pl_dust', 'pl_bb'):
            try:
                fit['closure'] = CT.closure_from_fit(fit)
            except CT.ContinuumError:
                fit['closure'] = None
        else:
            fit['closure'] = None
            fit['warnings'].append(_warn(
                'CA-15', '闭包诊断需要幂律 β（模型 pl/pl_dust/pl_bb）：'
                         '本模型 closure 按 null 出并附此书面原因（TXT-23）',
                reason='closure_needs_beta'))
        fit['verdict'] = verdict
        fit['de_reddened'] = de_red

    # F-112 的 S2 四键（preprocess{} 的 S2 侧）：仅当本请求含 poly 拟合时填真值
    # （杠杆值端点闸门依赖 F-62 的连续谱基线与设计矩阵）；无 poly（含只有 S1 物理模型
    # 的请求）⇒ null/[]（该通路不适用，§4.2 行原文：不得用 0 冒充未计算）。
    s2_block = None
    poly_fits = [f for f in fits if f.get('model') == 'poly'
                 and f.get('baseline_gate')]
    if poly_fits:
        gate = poly_fits[0]['baseline_gate']
        s2_block = {'n_high_leverage': gate['n_high_leverage'],
                    'baseline_edge_spread': gate['baseline_edge_spread'],
                    'edge_spread_after_downgrade': gate['edge_spread_after_downgrade'],
                    'suggested_edge_ranges': gate['suggested_edge_ranges']}

    body_out = {
        'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': top_warn,
        'spectrum_id': ls['spectrum_id'], 'spectrum_source': ls['source'],
        'spec_hash': ls['spec_hash'], 'tid': ls['tid'],
        'meta': {k: meta.get(k) for k in ('ra_deg', 'dec_deg', 'z', 'category',
                                          'mjd', 'time_precision_d', 'lambda_frame')},
        'meta_provenance': ls['meta_provenance'],
        'lambda_frame_converted': ls['lambda_frame_converted'],
        'n_below_convert': ls['n_below_convert'],
        'mw': mw,
        'frame': {'z_source': ('user' if z_req is not None
                               else ls['meta_provenance'].get('z', 'default')),
                  'x': (1.0 + z_eff) if z_eff is not None else None},
        'law': law, 'rv': rv, 'rv_source': rv_source, 'rv_free': rv_free,
        'nu0': nu0, 'z_eff': z_eff, 'host_ext_mode': host_ext_mode,
        'ebv_ignored': bool(ebv_ignored),
        'ebv_used': (None if host_ext_mode != 'prescribe' else ebv_req),
        'av_prescribed': (None if host_ext_mode != 'prescribe' else av_pin),
        'models_requested': models,
        'fit_region': {'n_pixels': n_fit, 'n_nonpos': n_nonpos,
                       'masked_ranges': masks['merged'],
                       'n_masked_pixels': masks['n_masked'],
                       'lam_min_aa': float(lam_f.min()),
                       'lam_max_aa': float(lam_f.max())},
        'sigma_method': sig_method_eff, 'rho_lag1': rho,
        'sigma_px_j1': sg['sigma_dual']['j1'], 'sigma_px_j2': sg['sigma_dual']['j2'],
        'sigma_stride_used': sg['stride_used'],
        'errcol_verdict': ev['verdict'], 'errcol_accepted': errcol_accepted,
        'mask_hash': mask_h,
        'preprocess': pre.response_block(masks, mask_h, pp_h, factor,
                                         factor == 1 and not masks['user']
                                         and ev['verdict'] == 'ok'
                                         and not errcol_accepted,
                                         ev['verdict'], errcol_accepted,
                                         n_outlier, outlier_over,
                                         rebin=(rebin_info['echo'] if factor > 1
                                                else None),
                                         s2=s2_block),
        'read_stats': ls['read_stats'],
        'fits': fits, 'verdict': verdict, 'de_reddened': de_red,
        **({'diagnostics_ignored': _ignored_u48} if _ignored_u48 else {}),
    }
    with _cache_lock:
        _result_cache[ckey] = body_out
        _result_cache.move_to_end(ckey)
        while len(_result_cache) > C_CACHE_MAX:
            _result_cache.popitem(last=False)
    return body_out
