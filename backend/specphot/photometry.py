"""specphot.photometry —— API-2（POST /photometry，S1 合成测光主计算）。

编排：请求校验（§5.2.1 的 Q-*）→ 装载（库内 spectrum_id / 上传件数组，二选一 Q-23）
→ 银河消光逐点改正（F-59：观测系、积分之前）→ 逐波段 band_integrals（F-46）
→ 实测点配对（F-24/F-61）→ AnchorSet 合并（F-78）→ 模式裁决（F-13/F-14）
→ anchored 闭式 GLS（F-49…F-52）→ §4.2 契约装配。
全链只读（RO-1…RO-5）；缓存 ST-3/F-107③；闸门 ST-1/ST-12。
误差次序纪律（F-97①②）：代理 σ̂ 先除 k_proxy(ρ, j_used) 去偏（修估计量本身），
再进 propagate_band_sigma 的 AR(1) 放大（修传播）；误差列（实测值）不去偏。

已定口径（规格未逐项钉死处的实现约定，P1b/后续期复核点）：
  - 源类别推导：库内无 category 列 ⇒ Transient.tags 含 'grb' 取 'grb_afterglow'、
    含 'sn' 取 'sn_platform'、其余 'other'（grb_early 无可用相位判据，登记待澄清）。
  - 星等行：flux_density_unit ∈ ('mag','magnitude')（宿主 extinction.MAG_UNITS 口径）；
    mag_system NULL 按 AB 处理；gext_corr=True 且有 mag_gextcor 时取已改正 AB 值，
    逐点在 anchor_provenance 回显 gext_corr。
  - anchored 参与波段的 mag_err_cal² = max(varκ − 2·h_i·σ_stat_i², 0)
    （流量空间 GLS 的 Cov(f_i, κ̂) = −κ·h_i·σ_syn_i²/f_i 展开，与 F-56 的
    delta_m × (1−h_i) 收缩同源）；未参与波段 = mag_err_cal_nonparticipant（F-56）。
  - 非 anchored（direct/自动降级）下 σ_cal 无法评估 ⇒ mag_err_cal=null、
    err_source.mag_err_cal='none' 并附书面原因（§3.5 σ_cal direct 行 / TXT-23：
    标 none 者不是 0）；此时 f_err_mjy 与 delta_m_err 的 err_scope='stat'。
  - Q-31 语义：custom_curves 先并入后查缺失；原文未禁止覆盖已登记波段 ⇒ 允许，
    覆盖与否由结果回显 curve_source='upload' 可辨（非静默）。
  - ST-5：轻档硬超时 C_ENDPOINT_TIMEOUT_S 在计算相位边界做 elapsed 检查，
    超时返回 E-11（504）；不中断在跑的 numpy 计算，只尽快放闸回话（§8.2）。
  - 登记不修（规格未逐项钉死，后续期复核）：used=False 锚点行仍作比对行
    （F-78② 只退出锚定，比对行保留）；谱无 mjd 时取中位时刻点做幅值锚定（F-77）；
    缓存查询在谱装载之后（ckey 依赖 spec_hash/mask_hash，必须先装载）；
    mw.correct 请求开关本身不回显（施加结果经 mw{} 全键回显）；缺键默认 true
    （§5.2.1 规范默认，T-8 两端对齐后取齐；显式 false 仍不改正）；curve_coverage 的
    with_curve 含本次 custom_curves 而 registered 只含库内注册表，两种作用域混装。
  - mask_hash/preprocess_hash 的 F-80 序列化助手已迁往 preprocess.py（F-113⑤
    的单一载体纪律；本文件只 import 消费，行为逐字节不变）。

P1b 接线（02b §3.14 / §9 P1b 行）：preprocess 非恒等选项按 Q-33 校验后进入
preprocess.py 的掩膜归并（F-107① 四类并集，自动表 F-72①② 默认剔除）与误差列
退化判定（F-110 六值闭集）；verdict≠ok 时按 U-53 的 errcol_choice 二选一（回落
代理 ⇒ CA-47②，坚持原列 ⇒ sigma_method='spec_err'+CA-47①），errcol_accepted
与 verdict、最终 sigma_method、CA-47 档位三处同现；离群点只找候选（F-111②③，
T-79③：未经 U-52 确认前掩膜不变）；factor>1/平滑开启 ⇒ E-13（501 phase='P2'）。
化约序 F-108⑦（second_diff_degraded 覆盖回落值）在本期以规则注释留位：P1b 无
合束，factor 恒 1，该序只可能由 P2 的 factor>1 触发。

P2c 接线（02b §3.12 / §9 P2c 行）：diagnostics 的前三键真实现（diagnostics.py，
F-105⑥ 零新增基础设施）——resp_perturb（F-83：σ_resp 换通带形状扰动散布口径，
只改 mag_err_resp/resp_method 两列 + diagnostics{} + CA-39）、beta_matrix（F-82：
实测 vs 合成色的 β 上三角格集进 diagnostics{}，CA-40 可达）、anchor_reinsert
（F-84：锚点残差逐波段再插入谱 ⇒ m_syn_local 诊断镜像列，causal_use=
'diagnostic_only'，主列 mag 不动）。关闭时不进入任何计算路径 ⇒ T-48① 逐字节
恒等保持；后三键（z_from_lines/frame_probe/sky_subtract）保持 501 phase='P3c'。

P2 切片 2b（model 定标解锁，§3.3 model 行 + Q-9/Q-10/Q-34）：mode='model' 或
auto 落到 model 时**用 S2 连续谱模型曲线代替观测谱**做波段积分（覆盖外可外推
⇒ extrapolated=true + CA-12；「以模型参考谱做流量定标」L-38⚠️）。模型以
use_model 携带（Q-9：必须同时带 mask_hash/frame/flux_transform_applied 且
mask_hash 等于本次掩膜；Q-34：preprocess_hash 缺省 ⇒ 按恒等态处理）；S2 侧
CA-44 的拟合不得被消费（F-91⑦，use_model.comparable=false ⇒ E-14）。无
use_model 的 model 路径保持 501（phase='P2'）。S2 参数误差向波段通量的传播
不在本期 ⇒ model 定标下 mag_err_stat/f_err_mjy/delta_m_err 按 null 出并附
书面原因（TXT-23），err_source='none'。
"""
import math
import time

import numpy as np
from flask import jsonify, request

from app import get_session, require_auth
from models import FilterDef, Lightcurve, Transient
import coords
import extinction
import wavconvert

from . import (specphot_bp, SPEC_PHOT_VERSION, _compute_gate, _err,
               _load_error_response, _result_cache, _cache_lock)
from . import registry, response, fluxcal, errors as errs
from . import diagnostics as _diag
from . import preprocess as pre
from . import continuum as _cont
from .constants import (C_MAX_BANDS, C_MAX_EXCLUDE, C_MAX_ANCHORS,
                        C_DT_TOL_TABLE, C_DT_TOL_D, C_DT_INTERP_D,
                        C_MAG_SANITY_LO, C_MAG_SANITY_HI, C_MAG_SANITY_WINDOW,
                        C_EXT_BANDAVG_EPS, C_CURVE_PX_PER_FWHM_MIN,
                        C_MIN_UPLOAD_POINTS, C_OVERLAP_MIN, C_CACHE_MAX,
                        C_ENDPOINT_TIMEOUT_S, C_RHO_MIN, C_SIGMA_STRIDE,
                        C_CLIP_MAX_FRAC, C_AA_PER_S, C_XLINK_MASK_MAX)
from .meta import load_spectrum_by_id
from .reader import (SpecLoadError, _normalize_rows, _new_stats, _assemble,
                     _to_float, WL_MIN, WL_MAX, _norm_lambda_frame)

MAG_UNITS = ('mag', 'magnitude')          # 宿主 extinction.MAG_UNITS 同值


# ─── 上传件数组 → LoadedSpectrum（A-7：请求自带全部输入；Q-26 同判据） ────

def _parse_upload_coords(m_in):
    """Q-24：ra_deg/dec_deg 若提供，必须能被宿主 coords.parse_ra/parse_dec 解析。

    格式/域非法 ⇒ E-14 bad_coordinates（不得被下游消光路径的裸 except 吞成 CA-23；
    缺坐标仍走 F-77/CA-23 的"未改正出数"缺省矩阵，不在此拒）。
    """
    ra_raw, dec_raw = m_in.get('ra_deg'), m_in.get('dec_deg')
    if ra_raw is None and dec_raw is None:
        return None, None
    try:
        ra = coords.parse_ra(ra_raw) if ra_raw is not None else None
        dec = coords.parse_dec(dec_raw) if dec_raw is not None else None
    except (ValueError, TypeError):
        raise SpecLoadError('E-14', f'坐标无法解析（ra_deg={ra_raw!r}, '
                                    f'dec_deg={dec_raw!r}；十进制度或 sexagesimal '
                                    '文本均可，dec ∈ [-90,90]）', reason='bad_coordinates')
    if (ra_raw is not None and ra is None) or (dec_raw is not None and dec is None):
        raise SpecLoadError('E-14', f'坐标无法解析（ra_deg={ra_raw!r}, '
                                    f'dec_deg={dec_raw!r}）', reason='bad_coordinates')
    if ra is None or dec is None:
        raise SpecLoadError('E-14', 'ra_deg 与 dec_deg 必须成对提供',
                            reason='bad_coordinates')
    return ra, dec


def _load_upload_arrays(spec):
    lam, flux, ferr = spec.get('lam_aa'), spec.get('flux'), spec.get('flux_err')
    if spec.get('spec_hash') and not (isinstance(lam, list) and isinstance(flux, list)):
        # T-43/A-7②：spec_hash 只允许用于结果缓存命中，拿 hash 单独来取谱 ⇒
        # 永远 E-01（404，§5.3 无 reason 闭集 ⇒ 只给 message）。正常缺 spec_hash
        # 的路径（纯数组上传）不受影响。
        raise SpecLoadError('E-01',
                            '只携带 spec_hash 而不携带谱数组：上传件不落盘、服务端'
                            '无谱可取，spec_hash 不能作为谱的身份（A-7②/T-43）',
                            status=404)
    if not isinstance(lam, list) or not isinstance(flux, list) \
            or len(lam) != len(flux) or len(lam) < C_MIN_UPLOAD_POINTS:
        raise SpecLoadError('E-14', 'spectrum.lam_aa 与 spectrum.flux 必须为等长数组'
                                    f'且 ≥ {C_MIN_UPLOAD_POINTS} 点', reason='bad_row')
    has_err = isinstance(ferr, list) and len(ferr) == len(lam)
    # V-3a：JSON 原值（字符串数字/None）逐值过 reader._to_float 规范化 —— 否则
    # 原值直进 _normalize_rows 会在 math.isfinite 处 TypeError ⇒ 500。
    # λ/flux 不可解析行按读侧同款纪律计数丢弃（CA-10）；第三值不可解析按无误差列
    # 处理（同 reader.parse_text_spectrum 的宿主口径）。
    rows, stats, warnings = [], _new_stats(), []
    n_dropped = 0
    for i, x in enumerate(lam):
        lx, fx = _to_float(x), _to_float(flux[i])
        if lx is None or fx is None:
            n_dropped += 1
            continue
        if not (WL_MIN < lx < WL_MAX):        # V-20：λ 合法域（同 reader 判据），越界拒
            raise SpecLoadError('E-14',
                                f'波长 {lx} Å 超出合理范围 ({WL_MIN}-{WL_MAX} Å)',
                                reason='wavelength_out_of_range')
        row = [lx, fx]
        if has_err:
            ex = _to_float(ferr[i])
            if ex is not None:
                row.append(ex)
        rows.append(row)
    if n_dropped:
        stats['n_dropped'] += n_dropped
        warnings.append({'code': 'CA-10', 'message': f'丢弃 {n_dropped} 个非数值数据行'})
    if len(rows) < C_MIN_UPLOAD_POINTS:       # Q-26：规范化/丢弃后仍须达最少点数
        raise SpecLoadError('E-14',
                            f'可解析数据点太少（{len(rows)} < {C_MIN_UPLOAD_POINTS}）',
                            reason='too_few_points')
    lam, flux, ferr = _normalize_rows(rows, stats, warnings)
    m_in = spec.get('meta') or {}
    if not isinstance(m_in, dict):            # P1 输入防御：meta 非 dict ⇒ E-14 不 500
        raise SpecLoadError('E-14', 'spectrum.meta 必须为 JSON 对象', reason='bad_enum')
    mjd_raw, z_raw = m_in.get('mjd'), m_in.get('z')
    mjd = _to_float(mjd_raw) if mjd_raw is not None else None
    if mjd is None and mjd_raw is not None:
        raise SpecLoadError('E-14', f'spectrum.meta.mjd={mjd_raw!r} 无法数值化',
                            reason='bad_enum')
    z = _to_float(z_raw) if z_raw is not None else None
    if z is None and z_raw is not None:
        raise SpecLoadError('E-14', f'spectrum.meta.z={z_raw!r} 无法数值化',
                            reason='bad_enum')
    ra_deg, dec_deg = _parse_upload_coords(m_in)
    # F-79②：lambda_frame 三态（vacuum/air/unknown），与 reader.load_upload_text
    # 同一实现（_norm_lambda_frame 同词表）；air ⇒ wavconvert 换真空恰好一次。
    frame = _norm_lambda_frame(m_in.get('lambda_frame'))
    prov_frame = 'user' if frame is not None else 'default'
    if frame is None:
        frame = 'vacuum'
    lambda_frame_converted, n_below_convert = None, 0
    if frame == 'air':
        # F-79②③：先计数（判定量是入参侧的空气 λ）再转换，< CONVERT_MIN_A 的点不转
        n_below_convert = sum(1 for x in lam if x < wavconvert.CONVERT_MIN_A)
        lam = [wavconvert.air_to_vacuum(x) if x >= wavconvert.CONVERT_MIN_A else x
               for x in lam]
        lambda_frame_converted = 'air_to_vac'
        if n_below_convert:
            warnings.append({'code': 'CA-34',
                             'message': f'{n_below_convert} 个点低于 '
                                        f'{wavconvert.CONVERT_MIN_A:.0f} Å 未做空气→真空'
                                        '转换：本谱为混合帧'})
    elif frame == 'unknown':
        warnings.append({'code': 'CA-34',
                         'message': 'lambda_frame=unknown：不做任何框架假设，'
                                    '速度类输出（vel_* / z_fit）将被禁用'})
    elif prov_frame == 'default':
        warnings.append({'code': 'CA-34',
                         'message': 'lambda_frame 未声明：按 vacuum 处理（假定值，TXT-21）'})
    tp = (0.5 if mjd == int(mjd) else 1e-5) if mjd is not None else None   # F-61
    meta = {'ra_deg': ra_deg, 'dec_deg': dec_deg,
            'z': z if z is not None else 0.0,
            'category': m_in.get('category') or 'other',
            'mjd': mjd, 'time_precision_d': tp,
            # F-79②：air 换真空后轴已是真空 ⇒ 回显 vacuum（同 reader.load_upload_text）
            'lambda_frame': 'vacuum' if frame == 'air' else frame,
            'already_gext_corrected': False,
            'flux_type': None, 'u_fluxes': m_in.get('flux_unit'),
            'flux_unit_assumed': False}
    prov = {'ra_deg': 'user' if ra_deg is not None else 'default',
            'dec_deg': 'user' if dec_deg is not None else 'default',
            'z': 'user' if z is not None else 'default',
            'category': 'user' if m_in.get('category') else 'default',
            'mjd': 'user' if mjd is not None else 'default',
            'lambda_frame': prov_frame}
    ls = _assemble('upload', None, None, lam, flux, ferr, meta, prov,
                   stats, warnings, lambda_frame_converted, n_below_convert)
    if spec.get('spec_hash') and spec['spec_hash'] != ls['spec_hash']:
        ls['warnings'].append({'code': 'CA-10',
                               'message': '请求携带的 spec_hash 与重算值不一致，以重算值为准'})
    return ls


# ─── 波段与曲线装载（DB 短会话；Q-31 自定义通带并入） ─────────────────────

def _load_band_defs(bands, custom_curves):
    defs = {}
    sess = get_session()
    try:
        for r in sess.query(FilterDef).filter(FilterDef.id.in_(bands)).all():
            tr = (r.extra_data or {}).get('transmission') or {}
            has = bool(tr.get('wl') and tr.get('tr'))
            sn = registry.source_note(r.id)
            defs[r.id] = {'lam': tr.get('wl'), 't': tr.get('tr'),
                          'vega2ab': r.vega2ab, 'wavelength': r.wavelength,
                          'curve_kind': registry.curve_kind(r.id) if has else None,
                          # §4.2 curve_source 词表 pcigale|svo|upload|none：
                          # 按 registry 的 per-band 来源注记分流（P2 便宜项）
                          'curve_source': ('svo' if sn and sn.upper().startswith('svo')
                                           else 'pcigale') if has else 'none'}
        n_total = sess.query(FilterDef).count()
    finally:
        sess.close()
    # Q-31：custom_curves **先并入、后查缺失** —— 若先做"缺曲线即 E-04"，未登记
    # 波段的自定义通带永远不可用（Q-31 的合并语义被空转）。原文只约束"并入后总
    # 通带数仍受 C_MAX_BANDS（Q-1）"且要求回显 curve_source='upload' + 节点数，
    # 未禁止覆盖已登记波段 ⇒ 允许覆盖，但由结果回显可辨（非静默替换）。
    if custom_curves is not None and not isinstance(custom_curves, list):
        raise SpecLoadError('E-14', 'custom_curves 必须为数组', reason='bad_enum')
    for cc in (custom_curves or []):
        if not isinstance(cc, dict):
            raise SpecLoadError('E-14', 'custom_curves 元素必须为对象（Q-31）',
                                reason='curve_invalid')
        b, lam, t = cc.get('band'), cc.get('lam_aa'), cc.get('t')
        kind = cc.get('curve_kind') or 'unknown'
        try:
            ok = (b and isinstance(lam, list) and isinstance(t, list)
                  and len(lam) == len(t) and len(lam) >= 2
                  and kind in ('transmission', 'throughput', 'unknown')
                  and all(math.isfinite(float(v)) and 100.0 <= float(v) <= 1e7
                          for v in lam)
                  and all(float(lam[i + 1]) > float(lam[i])
                          for i in range(len(lam) - 1))
                  and all(math.isfinite(float(v)) and float(v) >= 0.0 for v in t))
        except (TypeError, ValueError):    # P1 输入防御：节点含非数值 ⇒ E-14 不 500
            ok = False
        if not ok:
            raise SpecLoadError('E-14', f'自定义通带 {b!r} 不合规（Q-31：节点≥2、λ 严格递增、'
                                        '[100,1e7] Å、t 有限且 ≥0、curve_kind 三态）',
                                reason='curve_invalid', details={'band': b})
        defs[b] = {'lam': lam, 't': t, 'vega2ab': None, 'wavelength': None,
                   'curve_kind': kind, 'curve_source': 'upload'}
    for b in bands:
        if b not in defs:   # 合并后仍缺曲线的波段才 E-04
            raise SpecLoadError('E-04', f'波段 {b!r} 无响应曲线（未登记）；'
                                        '可用 mono 单色近似需显式授权', reason='curve_missing')
    cov = {'filters_total': n_total,
           'with_curve': sum(1 for d in defs.values() if d['lam']),
           'registered': len(registry.CURVE_REGISTRY),
           'unknown': sum(1 for d in defs.values()
                          if d['lam'] and d['curve_kind'] in (None, 'unknown')),
           'without_curve': n_total - len(registry.CURVE_REGISTRY)}
    return defs, cov


# ─── 实测点配对（F-24/F-61；短会话；AB 星等出） ───────────────────────────

def _pair_catalog(tid, bands, spec_mjd, tp, dt_tol_eff):
    """({band: {mag(AB), mag_err, mjd_obs, kind, dt_obs_d, gext_corr}}, warnings)；
    谱无 mjd ⇒ no_time。"""
    warns = []
    if not tid:
        return {}, warns
    sess = get_session()
    try:
        v2a = {r.id: r.vega2ab for r in
               sess.query(FilterDef.id, FilterDef.vega2ab).filter(FilterDef.id.in_(bands))}
        rows = (sess.query(Lightcurve)
                .filter(Lightcurve.transient_id == tid,
                        Lightcurve.flux_density_unit.in_(MAG_UNITS),
                        Lightcurve.discard.is_(False),
                        Lightcurve.upperlimit.is_(False),
                        Lightcurve.band.in_(bands)).all())
    finally:
        sess.close()
    pts = {}
    n_vega_dropped = 0
    for lc in rows:
        if lc.gext_corr and lc.mag_gextcor is not None:
            mag, merr = lc.mag_gextcor, lc.mag_gextcor_err
        elif lc.flux_density is not None:
            mag, merr = lc.flux_density, lc.flux_density_err
        else:
            continue
        if lc.mag_system == 'Vega':
            if v2a.get(lc.band) is None:
                n_vega_dropped += 1               # F-9：NULL ⇒ 该点不可用，不再静默丢弃
                continue
            mag = mag + float(v2a[lc.band])       # Vega → AB
        if lc.mjd is not None:
            pts.setdefault(lc.band, []).append(
                (float(lc.mjd), float(mag),
                 float(merr) if merr and merr > 0 else None, bool(lc.gext_corr)))
    out = {}
    if n_vega_dropped:
        warns.append({'code': 'CA-01',
                      'message': f'{n_vega_dropped} 个 Vega 星等点因所在波段未登记 '
                                 'vega2ab 无法换算 AB，未参与配对'})
    for b, arr in pts.items():
        arr.sort()
        if spec_mjd is None:                       # F-77：只做幅值比对（取中位时刻点）
            mid = sorted(p[0] for p in arr)[len(arr) // 2]
            p = min(arr, key=lambda q: abs(q[0] - mid))
            out[b] = {'mag': p[1], 'mag_err': p[2], 'mjd_obs': p[0],
                      'kind': 'no_time', 'dt_obs_d': None, 'gext_corr': p[3]}
            continue
        p = min(arr, key=lambda q: abs(q[0] - spec_mjd))
        if abs(p[0] - spec_mjd) <= dt_tol_eff:
            out[b] = {'mag': p[1], 'mag_err': p[2], 'mjd_obs': p[0], 'kind': 'nearest',
                      'dt_obs_d': max(abs(p[0] - spec_mjd), tp), 'gext_corr': p[3]}
            continue
        lo = max((q for q in arr if q[0] < spec_mjd), default=None)
        hi = min((q for q in arr if q[0] > spec_mjd), default=None)
        if lo and hi and (hi[0] - lo[0]) <= C_DT_INTERP_D:   # 禁外推（F-24）
            w = (spec_mjd - lo[0]) / (hi[0] - lo[0])
            out[b] = {'mag': lo[1] + w * (hi[1] - lo[1]),
                      'mag_err': (lo[2] + w * (hi[2] - lo[2])
                                  if lo[2] is not None and hi[2] is not None else None),
                      'mjd_obs': spec_mjd, 'kind': 'interpolated',
                      'dt_obs_d': tp, 'gext_corr': lo[3] or hi[3]}
    return out, warns


def _category_from_tags(tid):
    """源类别推导（docstring 已定口径）：库内无 category 列 ⇒ 查一次 Transient.tags，
    含 'grb' → 'grb_afterglow'、含 'sn' → 'sn_platform'、其余 'other'
    （grb_early 无可用相位判据，登记待澄清）。无 tid ⇒ 'other'。"""
    if not tid:
        return 'other'
    sess = get_session()
    try:
        t = sess.query(Transient).filter(Transient.id == tid).first()
        tags = [str(x).lower() for x in (t.tags or [])] if t is not None else []
    finally:
        sess.close()
    if any('grb' in x for x in tags):
        return 'grb_afterglow'
    if any('sn' in x for x in tags):
        return 'sn_platform'
    return 'other'


# ─── 共享管线（API-2 / API-3 同一代码路径，切片 2b 自视图内迁出；行为逐字节不变） ──

def _apply_mw(ls, mwq, ebv_override, top_warn):
    """银河消光（F-59：观测系逐点、积分之前；Q-11）。返回 (flux_corr, mw)。"""
    meta = ls['meta']
    lam, flux = ls['lam_aa'], ls['flux_flambda_cgs']
    mw = {'applied': False, 'available': False, 'ebv': None, 'ebv_source': None,
          'alambda_ref': None,
          'already_corrected': bool(meta.get('already_gext_corrected')),
          'corrected_ebv': meta.get('mw_corrected_ebv'), 'rv_used': 3.1}
    flux_corr = list(flux)
    if mw['already_corrected']:
        top_warn.append({'code': 'CA-30',
                         'message': '谱文件已带宿主侧银河消光改正，未再次施加（Q-11）'})
    elif mwq.get('correct', True):     # T-8/§5.2.1：缺键默认 true（显式 false 不改正）
        ebv, src = ebv_override, 'user' if ebv_override is not None else None
        if ebv is None:
            if meta.get('ra_deg') is None or meta.get('dec_deg') is None:
                top_warn.append({'code': 'CA-23', 'reason': 'ext_no_coords',
                                 'message': '缺坐标且未覆写 E(B−V)，银河消光未施加'})
            else:
                try:
                    if extinction.status()['available']:
                        ebv, src = extinction.get_ebv(meta['ra_deg'],
                                                      meta['dec_deg']), 'computed'
                except Exception:  # noqa: BLE001 - 图不可用按未改正出数（ST-11）
                    ebv = None
                if ebv is None:
                    top_warn.append({'code': 'CA-23', 'reason': 'ext_map_unavailable',
                                     'message': '尘埃图不可用，银河消光未施加'})
        if ebv is not None:
            flux_corr = [f * 10.0 ** (0.4 * ebv * (extinction.dust_coeff(x) or 0.0))
                         for f, x in zip(flux, lam)]
            kv = extinction.dust_coeff(5500.0)
            mw.update({'applied': True, 'available': True, 'ebv': ebv,
                       'ebv_source': src,
                       'alambda_ref': {'V': round(ebv * kv, 4)} if kv else None})
    return flux_corr, mw


def _errcol_stage(ls, flux_corr, errcol_choice, err_policy, top_warn):
    """F-110 误差列退化判定 + U-53 二选一（API-2/API-3 同一通路）。

    返回 dict(ev, errcol_accepted, ca47_note, use_col)；ca47_note 由调用方在
    缓存查询之后追加（保持告警次序）。verdict=ok 而 choice='as_provided' ⇒
    SpecLoadError(E-14, reason='errcol_choice')（Q-33：不得对可信列伪装"坚持"）。"""
    ev = pre.errcol_verdict(flux_corr, ls['flux_err'])
    if ev['ca21']:
        top_warn.append({'code': 'CA-21',
                         'message': f"误差列有限 σ 行数 {ev['n_err']} < 30，"
                                    '相对散布类判据不可判（F-110① 点数总则）；'
                                    '未判出退化 ≠ 判定可信'})
    errcol_accepted = False
    ca47_note = None
    if ev['verdict'] != 'ok':
        if errcol_choice == 'as_provided':
            # U-53「坚持用该误差列」⇒ sigma_method='spec_err'（列逐行可用时；
            # 部分行不可用仍走 _sigma_stage 的 mixed 回填）+ CA-47① 常驻
            errcol_accepted = True
            ca47_note = ('1', f"errcol_verdict={ev['verdict']}：该误差列已被判定"
                              '不可信，用户选择坚持使用（CA-47①）——'
                              "sigma_method='spec_err'，数值照出但不可信自知")
        else:
            # auto（未确认缺省）与 proxy 同走回落（F-110②）：回落 F-54 代理，
            # CA-47②（措辞与①不同：列未参与本次结果）；化约序 F-108⑦ 注：
            # factor>1 时 second_diff_degraded 将覆盖本回落值（P2 生效）
            ca47_note = ('2', f"errcol_verdict={ev['verdict']}：误差列未参与本次"
                              f"结果，已回落 F-54 代理（CA-47②，"
                              f"errcol_choice={errcol_choice}）")
    else:
        # verdict=ok：既有 U-10 语义不变；errcol_choice='proxy' 与
        # err_policy='force_proxy' 同义（不挂 CA-47②，其触发域是 verdict≠ok）
        if errcol_choice == 'as_provided':
            raise SpecLoadError('E-14',
                                "errcol_choice='as_provided' 仅在 errcol_verdict≠ok "
                                '时允许（Q-33；本次判定为 ok）', reason='errcol_choice')
        use_col = (err_policy == 'auto' and errcol_choice != 'proxy'
                   and errs.usable_err_column(ls['flux_err']))
    if ev['verdict'] != 'ok':
        use_col = errcol_accepted        # ①坚持 ⇒ True；②回落 ⇒ False
    return {'ev': ev, 'errcol_accepted': errcol_accepted,
            'ca47_note': ca47_note, 'use_col': use_col}


def _sigma_stage(ls, flux_corr, use_col, n_px, top_warn):
    """F-23/F-54/F-55/F-97 的逐像素 σ 装配（API-2/API-3 同一通路）。

    返回 dict(rho, sigma_dual(去偏后双 j), sigma_px, sig_method, stride_used,
    sigma_incomplete)。"""
    rho, rho_warn = errs.rho_lag1(flux_corr)
    if rho_warn:
        top_warn.append(rho_warn)
    dual = errs.sigma_second_diff_dual(flux_corr)
    sigma_dual = {'j1': None, 'j2': None}   # 响应回显的去偏双 j；不可用 ⇒ null
    sigma_proxy, sig_method, sigma_incomplete = None, 'spec_err', False
    stride_used = None
    if use_col:
        col = np.asarray(ls['flux_err'], dtype=float)
        ok_px = np.isfinite(col) & (col > 0)
        if not bool(ok_px.all()):
            # F-23：混合误差列的不可解析像素不得按 0 参与传播 ⇒ 回填代理并标
            # sigma_method='mixed'（V-8/F-54 词表）；代理不可估 ⇒ 不得用 0 冒充
            # （TXT-23）：sigma_incomplete 置位，σ_stat 按 null 出（_row_result）
            sigma_proxy, stride_used, _m = _proxy_sigma(rho, dual, flux_corr,
                                                        sigma_dual)
            sigma_incomplete = sigma_proxy is None
            sig_method = 'mixed' if not sigma_incomplete else 'spec_err'
        if sigma_incomplete:
            top_warn.append({'code': 'CA-06',
                             'message': '误差列部分不可用且代理点数不足：坏像素未进'
                                        '误差预算（anchored 时 C_syn 偏小），'
                                        'mag_err_stat 按 null 出（F-23/TXT-23）'})
    else:
        sigma_proxy, stride_used, m = _proxy_sigma(rho, dual, flux_corr, sigma_dual)
        sig_method = m or 'mad_window'
    sigma_px = _sigma_px_array(ls['flux_err'], sigma_proxy, n_px, use_col)
    return {'rho': rho, 'sigma_dual': sigma_dual, 'sigma_px': sigma_px,
            'sig_method': sig_method, 'stride_used': stride_used,
            'sigma_incomplete': sigma_incomplete}


# ─── model 定标（§3.3 model 行：用 S2 模型曲线代替观测谱；Q-9/Q-10/Q-34） ────

def _validate_use_model(um, mask_h, pp_h, masks, ec, z_eff):
    """Q-9/Q-10/Q-34/F-91⑦ 的 use_model 校验，返回归一化 dict（模型曲线 cfg）。

    - 三键齐全（mask_hash/frame/flux_transform_applied）⇒ 缺任一 E-14
      model_provenance_missing；mask_hash 与本次掩膜不一致 ⇒ mask_mismatch。
    - P1 恒观测系（FZ-1 通带平移归后续期）：frame='obs' 且
      flux_transform_applied='none' 之外取值 ⇒ E-14 model_frame_unsupported
      （rest 帧模型消费随 FZ 切片，不在此猜红移算术）。
    - comparable=false（S2 侧 CA-44）⇒ E-14 fit_not_comparable（F-91⑦）。
    - Q-34：preprocess_hash 给定 ⇒ 必须等于本次 pp_h；缺省 ⇒ 按恒等态处理
      （本次 preprocess 非恒等 ⇒ E-14 preprocess_mismatch，不得"缺省即忽略"）。
    """
    for k in ('mask_hash', 'frame', 'flux_transform_applied'):
        if um.get(k) is None:
            raise SpecLoadError('E-14', f'use_model 缺 {k}（Q-9：模型曲线与本次'
                                        '数据同掩膜、同框架才可比）',
                                reason='model_provenance_missing')
    if um['mask_hash'] != mask_h:
        raise SpecLoadError('E-14', 'use_model.mask_hash 与本次掩膜不一致：'
                                    '模型曲线与本次数据不同掩膜 ⇒ 参数与谱不可比'
                                    '（Q-9）', reason='mask_mismatch')
    if um['frame'] != 'obs' or um['flux_transform_applied'] != 'none':
        raise SpecLoadError('E-14', "use_model 仅接受 frame='obs' 且 "
                                    "flux_transform_applied='none'（P1 恒观测系，"
                                    'FZ-1；rest 帧消费随 FZ 切片，禁止双改 Q-10）',
                            reason='model_frame_unsupported')
    if um.get('comparable', True) is not True:
        raise SpecLoadError('E-14', 'use_model.comparable=false：该 S2 拟合未收敛'
                                    '（CA-44），不得被 S1 以 mode=model 消费'
                                    '（F-91⑦）', reason='fit_not_comparable')
    if um.get('preprocess_hash') is not None:
        if um['preprocess_hash'] != pp_h:
            raise SpecLoadError('E-14', 'use_model.preprocess_hash 与本次值不一致'
                                        '（Q-34）', reason='preprocess_mismatch')
    elif masks['user'] or ec['ev']['verdict'] != 'ok' or ec['errcol_accepted']:
        raise SpecLoadError('E-14', 'use_model 缺 preprocess_hash 且本次 preprocess '
                                    '非恒等态：模型来源口径无法建立（Q-34：只带 '
                                    'mask_hash 按恒等态处理）',
                            reason='preprocess_mismatch')
    alias = um.get('model')
    if alias not in _cont.MODEL_SPECS:
        raise SpecLoadError('E-14', f"use_model.model={alias!r} 不在 S2 注册表模型集"
                                    f" {sorted(_cont.MODEL_SPECS)} 内（poly 的 "
                                    'Chebyshev 基依赖拟合窗口定义，暂不可被 S1 消费）',
                            reason='model_unsupported')
    params = um.get('params')
    if not isinstance(params, dict) or not params:
        raise SpecLoadError('E-14', 'use_model.params 必须为非空 JSON 对象',
                            reason='model_params_invalid')
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(float(v)):
            raise SpecLoadError('E-14', f'use_model.params[{k!r}] 必须为有限数字',
                                reason='model_params_invalid')
    nu0 = um.get('nu0')
    if nu0 is None:
        nu0 = _cont.NU0_DEFAULT
    try:
        nu0 = float(nu0)
    except (TypeError, ValueError):
        raise SpecLoadError('E-14', 'use_model.nu0 无法数值化',
                            reason='model_params_invalid')
    if not (math.isfinite(nu0) and nu0 > 0):
        raise SpecLoadError('E-14', 'use_model.nu0 必须为正有限数',
                            reason='model_params_invalid')
    law = um.get('law') or 'smc'
    if law not in ('smc', 'lmc', 'mw', 'mw_f99', 'mw_ccm'):
        raise SpecLoadError('E-14', f'use_model.law={law!r} 非法（Q-14 五键）',
                            reason='bad_enum')
    rv = um.get('rv')
    if rv is None:
        rv = 3.1
    rv = float(rv)
    if not (math.isfinite(rv) and 1.5 <= rv <= 6.0):
        raise SpecLoadError('E-14', 'use_model.rv 须在 [1.5, 6]（宿主 rv_schema）',
                            reason='model_params_invalid')
    z = z_eff
    if um.get('z') is not None:
        z = float(um['z'])
        if not (math.isfinite(z) and z >= 0):
            raise SpecLoadError('E-14', 'use_model.z 必须为非负有限数',
                                reason='model_params_invalid')
    return {'model': alias, 'params': {k: float(v) for k, v in params.items()},
            'nu0': nu0, 'law': law, 'rv': rv, 'z': z}


def _model_curve(ls, defs, bands, um):
    """S2 模型曲线（§3.3 model 行）：在谱的原生 λ 网格上求值模型 Fν，并把各
    波段曲线节点落在谱覆盖**外**的部分并入网格（模型解析可外推，覆盖外积分
    仍走原生网格梯形求积），返回 (lam_grid, flam_cgs, {band: extrapolated})。
    外推判定 = 通带覆盖越出实测覆盖（extrapolated ⇒ 行级 CA-12/TXT-6）。"""
    lam_obs = np.asarray(ls['lam_aa'], dtype=float)
    lo, hi = float(lam_obs[0]), float(lam_obs[-1])
    extra = []
    extrapolated = {}
    for b in bands:
        d = defs[b]
        cl = d.get('lam')
        out = False
        if cl:
            clo, chi = float(cl[0]), float(cl[-1])
            out = clo < lo - 1e-9 or chi > hi + 1e-9
            cl_a = np.asarray(cl, dtype=float)
            extra.extend(cl_a[(cl_a < lo) | (cl_a > hi)].tolist())
        extrapolated[b] = out
    grid = np.unique(np.concatenate([lam_obs, np.asarray(extra, dtype=float)])) \
        if extra else lam_obs
    nu = C_AA_PER_S / grid
    cfg = {'nu0': um['nu0'], 'z': um['z'], 'law': um['law'], 'rv': um['rv']}
    fnu = _cont.MODEL_SPECS[um['model']]['fnu'](dict(um['params']), nu, cfg)  # cgs Fν
    flam = fnu * C_AA_PER_S / grid ** 2        # Fλ = Fν·c/λ²（cgs）
    return grid, flam, extrapolated


# ─── API-2 ───────────────────────────────────────────────────────────────

@specphot_bp.route('/photometry', methods=['POST'])
@require_auth
def photometry():
    if not _compute_gate.acquire(blocking=False):
        return _err('server_busy', '已有计算在跑，请稍后重试',
                    429, reason='busy', retry_after_s=2)
    try:
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return _err('bad_request_state', '请求体应为 JSON 对象', 400, reason='bad_enum')
        sid, spec = body.get('spectrum_id'), body.get('spectrum')       # Q-23
        if (sid is None) == (spec is None):
            return _err('bad_request_state', 'spectrum_id 与 spectrum 必须且只能给一个',
                        400, reason='source_ambiguous')
        if spec is not None and not isinstance(spec, dict):             # P1 输入防御
            return _err('bad_request_state', 'spectrum 必须为 JSON 对象',
                        400, reason='bad_enum')
        if sid is not None:
            # P1 输入防御：spectrum_id 必须是 int 或纯数字字符串（bool 排除），
            # 'abc'/1.5/浮点字符串一律 E-14，不让 int() 的 ValueError 变 500
            if isinstance(sid, bool) or not (
                    isinstance(sid, int)
                    or (isinstance(sid, str) and sid.strip().isdigit())):
                return _err('bad_request_state',
                            f'spectrum_id 类型非法（{sid!r}）：须为整数',
                            400, reason='bad_enum')
            sid = int(sid)
        diag = body.get('diagnostics')
        if diag is not None and not isinstance(diag, dict):             # P1 输入防御
            return _err('bad_request_state', 'diagnostics 必须为 JSON 对象',
                        400, reason='bad_enum')
        diag = diag or {}
        if any(not isinstance(v, bool) for v in diag.values()):         # Q-27：只接受布尔
            return _err('bad_request_state', 'diagnostics 的值只能为布尔（Q-27）',
                        400, reason='bad_enum')
        # P2c（§9 P2c 行 / Q-27）：beta_matrix/resp_perturb/anchor_reinsert 三键
        # 解锁走真实现（输出一律进 diagnostics{} 子对象 + warnings[]，T-48）。
        # P3c（§9 P3c 行）：z_from_lines/frame_probe 也改走真实现——但判据挂 M-6
        # （registry 复核表，库内现状未满足）⇒ 闸门后：unknown 帧 ⇒ E-14（T-51），
        # 未复核 ⇒ null + 书面原因的闸门块（键恒在场），装配点在 diag_out 组装处。
        # sky_subtract（F-86）的消费点在 S3 步 1 的 U-31（sky_handling='subtract'，
        # 随 API-4 提交），本 S1/S2 测光路径不消费 ⇒ 仍 501（指路，不冒充实现）。
        if diag.get('sky_subtract') is True:                              # Q-27
            return _err('feature_disabled',
                        'sky_subtract（F-86）随 P3c 上线，但其消费点在 S3 步 1 的 '
                        "U-31（sky_handling='subtract'，API-4）；本 S1/S2 测光"
                        '路径不接受该开关',
                        501, reason='sky_subtract_use_u31', phase='P3c')
        # P2 2b：model 定标解锁（§3.3 model 行）。use_model 缺席的 model 路径
        # 保持 501（phase='P2'）；use_model 在场 ⇒ Q-9/Q-34 校验后走真实现。
        um = body.get('use_model')
        if um is None and body.get('mode') == 'model':
            return _err('feature_disabled',
                        'model 定标需携带 use_model（S2 拟合结果 + 掩膜/框架声明，'
                        'Q-9）；无 S2 拟合的路径未开放',
                        501, reason='phase_pending', phase='P2')
        if um is not None and not isinstance(um, dict):                 # P1 输入防御
            return _err('bad_request_state', 'use_model 必须为 JSON 对象（Q-9）',
                        400, reason='bad_enum')
        pp = body.get('preprocess') or {}
        if not isinstance(pp, dict):                                    # P1 输入防御
            return _err('bad_request_state', 'preprocess 必须为 JSON 对象',
                        400, reason='bad_enum')
        # Q-33 形态校验：factor 闭集/ranges/smooth/errcol_choice。P2 切片 2c 起
        # factor>1（合束算术 F-108）与 smooth（绘图平滑 F-109，纯前端绘制）放行。
        try:
            factor, pp_ranges, smooth_px, errcol_choice = pre.validate_request(pp)
        except SpecLoadError as e:
            return _load_error_response(e)
        weighting, band_mode = body.get('weighting', 'photon'), body.get('band_mode', 'integrated')
        mag_system, mode = body.get('mag_system', 'AB'), body.get('mode', 'auto')
        err_policy = body.get('err_policy', 'auto')
        if (weighting not in ('photon', 'energy') or band_mode not in ('integrated', 'mono')
                or mag_system not in ('AB', 'ST', 'Vega')
                or mode not in ('auto', 'direct', 'anchored', 'model')
                or err_policy not in ('auto', 'force_proxy')):
            return _err('bad_request_state', '枚举值非法（weighting/band_mode/'
                        'mag_system/mode/err_policy）', 400, reason='bad_enum')
        if mag_system == 'ST':                                          # F-48
            return _err('vega_or_st_unavailable', 'ST 星等制需要库内没有的 Vega 参考谱（P1 禁用）',
                        400, reason='st_unavailable')
        bands_raw = body.get('bands') or []
        if not isinstance(bands_raw, list) \
                or not all(isinstance(b, str) for b in bands_raw):      # P1 输入防御
            return _err('bad_request_state', 'bands 必须为字符串数组',
                        400, reason='bad_enum')
        bands = list(dict.fromkeys(bands_raw))
        if not 1 <= len(bands) <= C_MAX_BANDS:                          # Q-1
            return _err('input_over_limit', f'bands 数量须在 1..{C_MAX_BANDS}',
                        400, reason='too_many_bands')
        if band_mode == 'mono' and body.get('allow_mono') is not True:  # Q-5
            return _err('band_unusable', "band_mode='mono' 需显式 allow_mono=true",
                        400, reason='curve_missing')
        mask_raw = body.get('mask') or []
        if not isinstance(mask_raw, list):                              # P1 输入防御
            return _err('bad_request_state', 'mask 必须为数组', 400, reason='bad_enum')
        if len(mask_raw) > C_MAX_EXCLUDE:                               # Q-2
            return _err('input_over_limit', f'掩膜段数超过上限 {C_MAX_EXCLUDE}',
                        400, reason='too_many_masks')
        mwq = body.get('mw') or {}
        if not isinstance(mwq, dict):                                   # P1 输入防御
            return _err('bad_request_state', 'mw 必须为 JSON 对象', 400, reason='bad_enum')
        try:
            ebv_override = None if mwq.get('ebv') is None else float(mwq['ebv'])
            rz = body.get('redshift', None)
            z_user = None if rz in (None, 'none') else float(rz)
            dt_tol_user = None if body.get('dt_tol_d') is None else float(body['dt_tol_d'])
        except (TypeError, ValueError):
            return _err('bad_request_state', "redshift/mw.ebv/dt_tol_d 类型非法"
                        "（redshift ∈ null/'none'/数字）", 400, reason='bad_enum')
        if ebv_override is not None and not (math.isfinite(ebv_override) and ebv_override >= 0):
            return _err('bad_request_state', 'mw.ebv 必须为有限非负数', 400, reason='not_finite')
        if dt_tol_user is not None and (not math.isfinite(dt_tol_user)
                                        or dt_tol_user < 0):            # P1：负容差拒绝
            return _err('bad_request_state', 'dt_tol_d 必须为非负有限数',
                        400, reason='bad_enum')
        # ST-5：轻档硬超时（§7.6 C_ENDPOINT_TIMEOUT_S=2 s）。在计算相位边界做
        # elapsed 检查，超时 E-11（504）；不中断在跑的 numpy 计算，只尽快放闸回话。
        deadline = time.monotonic() + C_ENDPOINT_TIMEOUT_S

        def _deadline_check():
            if time.monotonic() > deadline:
                raise SpecLoadError('E-11',
                                    '计算超时（ST-5 硬超时），建议减少波段数或缩小谱规模',
                                    status=504)

        try:
            ls = load_spectrum_by_id(sid) if sid is not None else _load_upload_arrays(spec)
            defs, coverage = _load_band_defs(bands, body.get('custom_curves'))
        except SpecLoadError as e:
            return _load_error_response(e)
        if sid is not None:
            # 源类别推导（docstring 已定口径）：库内谱查一次 Transient.tags，
            # 'grb'→grb_afterglow / 'sn'→sn_platform / 其余 other ⇒
            # C_DT_TOL_TABLE 三档容差真正生效（reader/meta 返回契约不动）。
            ls['meta']['category'] = _category_from_tags(ls['tid'])
        lam, flux = ls['lam_aa'], ls['flux_flambda_cgs']
        meta, top_warn = ls['meta'], list(ls['warnings'])
        if mag_system == 'Vega':                                        # Q-6 / F-9 三态
            miss = [b for b in bands if defs[b]['vega2ab'] is None]
            if miss:
                return _err('vega_or_st_unavailable',
                            f'波段 {miss} 未登记 vega2ab，无法换算 Vega 星等',
                            400, reason='vega_unavailable')
            zero_bands = [b for b in bands if float(defs[b]['vega2ab']) == 0.0]
            if zero_bands:                     # F-9：0.0 无法与缺省值区分 ⇒ 仍算但挂 CA-01
                top_warn.append({'code': 'CA-01', 'reason': 'vega2ab_is_zero',
                                 'message': f'波段 {zero_bands} 的 vega2ab 登记值为 0.0'
                                            '（可能是缺省值），Vega 换算仍已执行'})
        _deadline_check()
        # P1b（F-107①）：用户段（U-13 框选 body['mask'] + U-50 手输
        # preprocess.ranges，同一"用户数值段"类）与自动表（C_MASK_ABS_TABLE /
        # C_MASK_EMIS_TABLE 按 F-72①② 默认剔除）并成**一个布尔标志位数组**后才算
        # mask_hash；吸收系统类属 P3d（F-103① 第五类）：P3d 评审 P1-2 起
        # U-48 absorber_ident 在本端点消费的唯一半支 = 掩膜闸门——M-6 线表给出
        # Lyα 且 z_eff>0 时，蓝侧 IGM 段在测量/合束**前**并入（F-106① 次序，
        # mask_hash 第 4 槽，与 API-4 同一 diagnostics.absorber_mask_segs 判定）；
        # 前置缺（M-6 空库现状）⇒ 空段 ⇒ 零开销恒等（各槽与 P3c 基线逐字节同值）。
        # 识别/翼拟合家族结果仍只在 API-4（本端点不产 absorber_systems）；
        # wing_logn/metal_sat_check 仍按 F-105③ 忽略+回显（见 _ignored_u48）。
        # 段级 V-13 过滤/忽略计数与并集后 C_MAX_EXCLUDE 闸都在 pre.merge_masks。
        # 红移三态（Q-12）的 z_eff 判定在 merge_masks 之前：吸收系统第五类掩膜
        # （F-103①，P3d 评审 P1-2 接线）安置 Lyα 蓝侧段要用它——表达式单一出处，
        # 下方比对块直接复用。
        z_eff = meta['z'] if rz is None else z_user
        absorber_segs = _diag.absorber_mask_segs(
            lam, z_eff, registry.M6_LINE_TABLE) \
            if diag.get('absorber_ident') is True else []
        # 次序注记（F-106①）：merge_masks 在银消改正之前调用——布尔标志位掩膜与
        # 逐点乘消光精确可交换（掩膜只决定"谁参与"，不改数值），无数值后果；
        # 数值通路次序纪律（消光→掩膜→合束→误差列判定）在 F-110 判定点生效。
        try:
            masks = pre.merge_masks(lam, list(mask_raw) + pp_ranges,
                                    absorber_ranges=absorber_segs)
        except SpecLoadError as e:
            return _load_error_response(e)
        if masks['n_ignored']:
            top_warn.append({'code': 'CA-10',
                             'message': f"{masks['n_ignored']} 个掩膜段无效或与谱覆盖无重叠，已忽略（V-13）"})
        mask = masks['merged']          # S1 积分消费的生效段（并集、排序）
        # 红移三态（Q-12；S1 P1 恒观测系积分，FZ-1 通带平移归 P1 余项）
        z_warn = z_eff if z_eff is not None else meta['z']
        if z_warn and z_warn > 0.05:
            top_warn.append({'code': 'CA-07', 'reason': 'band_shift_only',
                             'message': f'z={z_warn} 未做静止系改正（FZ-1 通带平移未开启），'
                                        '结果仅作参考（FZ-3）'})
        z_src = ('none' if rz == 'none' else
                 ('user' if rz is not None else ls['meta_provenance'].get('z', 'default')))
        # 银河消光（F-59：观测系逐点、积分之前；Q-11）——API-2/API-3 同一通路
        # （helper 内保持原次序：CA-30 已改判 → CA-23 缺坐标/图不可用）。
        flux_corr, mw = _apply_mw(ls, mwq, ebv_override, top_warn)
        # ── P1b（F-110/V-22）：误差列退化判定（在 V-3a 规范化之后的有限 σ 行上
        # 统计；F-106① 次序中位于消光改正之后）＋ U-53 的 errcol_choice 二选一。──
        # CA-21（F-110① 点数总则）：有列而 n_err<30 ⇒ 相对散布步不可判，末位 ok
        # 只表示"未判出退化"，必须同挂 CA-21（与 F-55 的点数总则同数同口径）。
        ec = _errcol_stage(ls, flux_corr, errcol_choice, err_policy, top_warn)
        ev, errcol_accepted, ca47_note = ec['ev'], ec['errcol_accepted'], ec['ca47_note']
        use_col = ec['use_col']
        # 哈希与缓存键（F-107③：ST-3 键含 mask_hash 与 preprocess_hash 两项）。
        # 判定/选择已定 ⇒ preprocess_hash 在此可算；verdict 需 flux_corr（消光后）
        # ⇒ 缓存查询相对 P1 后移到消光块之后，命中路径多做的是纯函数计算。
        mask_h = pre.mask_hash(masks, lam, ls['spec_hash'])
        pp_h = pre.preprocess_hash(mask_h, factor, ev['verdict'], errcol_accepted,
                                   ls['spec_hash'])
        um_rich = None
        if um is not None:
            # Q-9/Q-10/Q-34/F-91⑦：model 定标的模型来源声明必须在缓存查询之前
            # 校验（校验失败不得命中缓存），归一化结果（nu0/law/rv/z）供 _model_curve。
            um_rich = _validate_use_model(um, mask_h, pp_h, masks, ec, z_eff)
        ckey = pre.f80_hash([sid, ls['spec_hash'], sorted(bands), weighting,
                             band_mode,                     # ST-3
                             mag_system, mode, err_policy, dt_tol_user, z_user,
                             rz == 'none', mwq.get('correct', True), ebv_override,
                             mask_h, pp_h,
                             body.get('anchor_rows'), body.get('custom_curves'),
                             body.get('anchor_bands'), um, diag,
                             SPEC_PHOT_VERSION,
                             # 上传件 meta 是请求自带输入（A-7）：mjd/z/category 影响
                             # dt_tol_eff 与 frame.x，ra/dec/lambda_frame 被逐键回显
                             # ⇒ 必须进缓存键（F-80 的 dict 按 (key,value) 序列化保证
                             # 值敏感）。库内谱的 meta 由 sid 唯一决定，不改变键空间。
                             meta], n=16)
        with _cache_lock:
            if ckey in _result_cache:
                _result_cache.move_to_end(ckey)
                return jsonify(_result_cache[ckey])
        if ca47_note:
            top_warn.append({'code': 'CA-47', 'reason': f'errcol_{ca47_note[0]}',
                             'message': ca47_note[1]})
        # σ 通路（F-23/F-54/F-55/F-97；err_policy=force_proxy 强制代理）。
        # 次序纪律（F-97①②）：ρ 先估（去偏因子与 j 的裁决都要用它）；二阶差分/
        # 滑窗 MAD 代理先按 k_proxy(ρ, j_used) 去偏（修估计量本身），再进 F-55 的
        # 积分放大（修传播），两个因子不可互相顶替。误差列（use_col）是实测误差
        # 不是代理 ⇒ 不去偏（F-97① 的适用域是代理值）。—— API-2/API-3 同一通路。
        sg = _sigma_stage(ls, flux_corr, use_col, len(lam), top_warn)
        rho = sg['rho']
        sigma_dual = sg['sigma_dual']
        sig_method = sg['sig_method']
        sigma_incomplete = sg['sigma_incomplete']
        stride_used = sg['stride_used']
        sigma_px = sg['sigma_px']
        # ── P1b（F-111②③/T-79③）：离群点只找候选、只回显 n_outlier_flagged；
        # 服务端绝不自动剔除 —— 未经 U-52 确认前 n_masked_pixels 与 mask_hash
        # 不因本块改变（候选段随 CA-10/outlier_flagged 告警回显，U-52 逐段确认
        # 后写入用户掩膜类）。候选占比超 C_CLIP_MAX_FRAC ⇒ CA-47④ 且不提供
        # 剔除按钮（一条要剔两成像素的谱，问题在基线不在离群点）。
        n_outlier, outlier_segs, outlier_over = pre.outlier_candidates(lam, flux_corr)
        if outlier_over:
            top_warn.append({'code': 'CA-47', 'reason': 'outlier_frac_over',
                             'message': f'σ 裁剪离群候选 {n_outlier} 个，占比超过 '
                                        f'C_CLIP_MAX_FRAC={C_CLIP_MAX_FRAC}：不提供'
                                        '剔除按钮（CA-47④）—— 问题在基线不在离群点'
                                        '（F-111③）'})
        elif n_outlier:
            top_warn.append({'code': 'CA-10', 'reason': 'outlier_flagged',
                             'message': f'σ 裁剪发现 {n_outlier} 个离群候选'
                                        '（只标记未剔除，F-111②）：逐段确认后才进入'
                                        '用户掩膜（U-52）',
                             'segments': outlier_segs})
        # ── P2 切片 2c（F-108②③⑤⑥⑦ + F-109）：合束主通路（factor>1）。
        # 次序（F-106①）：消光 ⇒ 掩膜 ⇒ 合束 ⇒ 才进 S1；被掩像素不参与块权重与
        # 均值。σ 通路在原始像元上估（F-55 的 rho_lag1 与 F-54 代理均如此），块内
        # 相关噪声按 F-108③ 通式合成 σ_bin；合束后 sigma_method='second_diff_degraded'
        # + CA-06（F-108⑥），verdict≠ok 时按 F-108⑦ 化约序覆盖回落值（CA-47①②
        # 与 CA-06 并现，sigma_method 真值唯一）。r_source='none'（全库现状，F-69）
        # ⇒ 欠分辨闸门无从判定 ⇒ 必挂 CA-47③。── API-2/API-3 同一入口 pre.rebin_stage。
        rebin_info = None
        if factor > 1:
            rebin_info = pre.rebin_stage(lam, flux_corr, sigma_px,
                                         masks['mask_bool'], factor, rho)
            top_warn.append({'code': 'CA-06',
                             'message': f'谱已合束（factor={factor}）：白噪声前提被'
                                        '破坏，sigma_method=second_diff_degraded'
                                        '（F-108⑥/F-97⑦）'})
            top_warn.append({'code': 'CA-47', 'reason': 'rebin_r_source_none',
                             'message': '无仪器 R ⇒ 欠分辨闸门无从判定，无法保证未'
                                        '抽到欠分辨（F-108④/CA-47③）；合束仍可用'})
            top_warn.extend(pre.ca49_warnings(factor, rebin_info['echo']['rebin_gain'],
                                              rebin_info['echo']['px_per_fwhm_after'],
                                              rebin_info['rho_echoed']))
            # 化约序（F-108⑦）：降级态覆盖回落值 —— final sigma_method 唯一真值
            sig_method = 'second_diff_degraded'
            # 抽后传播：块内相关已在 σ_bin 中（F-108③ 通式），波段级不再叠加
            # F-55 放大（避免双计；裁量登记：跨块残余相关由 CA-06 降级声明承担）
            rho_prop = None
            sigma_prop = rebin_info['sigma']
        else:
            rho_prop = rho
            sigma_prop = sigma_px
        # F-106④：PreprocessedSpectrum —— S1/S2/S3 的唯一数据入口（factor>1 时
        # 持抽后网格；display_smoothed 为 F-109 结构位，只写不读，T-77①）。
        ps = pre.build_spectrum(ls, flux_corr, masks, factor=factor,
                                rebin=rebin_info, smooth_px=smooth_px)
        # F-61 生效容差与配对
        tp = meta.get('time_precision_d') or 0.0
        table = dt_tol_user if dt_tol_user is not None else C_DT_TOL_TABLE.get(
            meta.get('category'), C_DT_TOL_D)
        dt_tol_eff = max(table, 2.0 * tp)
        paired, pair_warns = _pair_catalog(ls['tid'], bands, meta.get('mjd'), tp, dt_tol_eff)
        top_warn.extend(pair_warns)
        _deadline_check()
        # AnchorSet 合并（F-78/Q-25）
        a_rows = [{'band': b, 'mag': p['mag'], 'mag_system': 'AB', 'mjd': p['mjd_obs'],
                   'mag_err': p['mag_err'], 'anchor_origin': 'catalog', 'note': '',
                   'gext_corr': p['gext_corr'], 'kind': p['kind'],
                   'dt_obs_d': p['dt_obs_d']} for b, p in paired.items()]
        manual = body.get('anchor_rows') or []
        if not isinstance(manual, list) or not all(isinstance(r, dict) for r in manual):
            return _err('bad_request_state', 'anchor_rows 必须为对象数组',
                        400, reason='anchor_set_invalid')
        if len(manual) > C_MAX_ANCHORS:
            return _err('bad_request_state', f'手加锚点行数超过上限 {C_MAX_ANCHORS}',
                        400, reason='too_many_rows')
        try:
            for r in manual:
                if r.get('band') not in defs:
                    raise SpecLoadError('E-14', f"锚点波段 {r.get('band')!r} 未登记",
                                        reason='band_unknown')
                try:
                    mag_r = float(r.get('mag'))
                    merr_r = None if r.get('mag_err') is None else float(r['mag_err'])
                except (TypeError, ValueError):   # P1 输入防御：非数字 ⇒ E-14 不 500
                    raise SpecLoadError('E-14', '锚点 mag/mag_err 必须为数字',
                                        reason='not_finite')
                if not math.isfinite(mag_r) or (merr_r is not None
                                                and not math.isfinite(merr_r)):
                    raise SpecLoadError('E-14', '锚点 mag/mag_err 必须有限', reason='not_finite')
                mjd_r = r.get('mjd')
                if mjd_r is not None:
                    try:
                        mjd_r = float(mjd_r)      # 规范化后下行，F-61 比对处不再裸 float()
                    except (TypeError, ValueError):
                        raise SpecLoadError('E-14', '锚点 mjd 必须为数字', reason='bad_enum')
                ms = r.get('mag_system', 'AB')
                if ms not in ('AB', 'Vega', 'ST'):     # Q-25：白名单先行（U-29 可用集）
                    raise SpecLoadError('E-14',
                                        f"锚点 mag_system={ms!r} 非法（AB/Vega/ST）",
                                        reason='bad_enum')
                if ms == 'ST' or (ms == 'Vega' and defs[r['band']]['vega2ab'] is None):
                    raise SpecLoadError('E-14', f'锚点 mag_system={ms} 当前不可用',
                                        reason='mag_system_unavailable')
                a_rows.append({'band': r['band'], 'mag': mag_r, 'mag_system': ms,
                               'mjd': mjd_r, 'mag_err': merr_r,
                               'anchor_origin': 'manual', 'note': r.get('note') or '',
                               'gext_corr': False})
            aset = fluxcal.AnchorSet(a_rows)
        except SpecLoadError as e:
            if e.details.get('band_conflict'):
                e.reason = 'band_conflict'
            return _load_error_response(e)
        ab_req = body.get('anchor_bands')
        if ab_req is not None and (not isinstance(ab_req, list)
                                   or not all(isinstance(x, str) for x in ab_req)):
            return _err('bad_request_state', 'anchor_bands 必须为字符串数组',
                        400, reason='bad_enum')          # P1 输入防御
        ab_req = set(ab_req) if ab_req else {r['band'] for r in aset.rows}
        for r in aset.rows:
            # used 判定：U-41 手加行编辑器没有 mag_err 字段（规格 §2 U-41），
            # 且误差表明确支持「m_obs 来自手加锚点而无 mag_err」（§4.2 delta_m 行）
            # ⇒ 手加行不设 σ_obs 门槛（GLS 里该行只经 C_syn 约束，F-51）；
            # 源表行仍要求 mag_err > 0（无误差的实测点进 GLS 无从加权）。
            r['used'] = (r['band'] in ab_req and r['band'] in bands
                         and (r['anchor_origin'] == 'manual'
                              or (r['mag_err'] is not None and float(r['mag_err']) > 0)))
        # 模式裁决（F-13/F-14 双闸）
        cls = fluxcal.classify_flux_calibration(meta.get('flux_type'), meta.get('u_fluxes'),
                                                'catalog' if sid is not None else 'upload')
        top_warn.extend(cls['warnings'])
        suspect = fluxcal.first_gate_suspect(ls['flux_median_cgs'])
        # §3.3 model 行：mode='model'（显式，use_model 已过 Q-9 校验）⇒ 直接用
        # S2 模型曲线积分；其余模式先按观测谱积分（auto 闸门与 direct 判定需要），
        # auto 落到 model 时在 resolve_mode 之后换模型曲线重算。
        model_mode_req = (mode == 'model' and um_rich is not None)
        lam_model = flam_model = None
        model_extrap = {}
        # factor>1：观测谱积分走抽后网格（F-106④ 唯一入口）；未改正通量用同一
        # 块权重施加（delta_a 比对两侧同网格，F-60）。model 定标的模型曲线解析
        # 可外推、不受合束影响，仍在原生网格求值。
        lam_obs_eff = rebin_info['lam'] if factor > 1 else None
        flux_obs_eff = (pre.combine_blocks(flux, rebin_info['blocks'])
                        if factor > 1 else flux)
        try:
            if model_mode_req:
                lam_model, flam_model, model_extrap = _model_curve(ls, defs, bands,
                                                                   um_rich)
                res, dropped = _compute_bands(ls, defs, bands, flam_model, flux, mw,
                                              weighting, band_mode, mask,
                                              lam=lam_model, apply_mw=False)
                lam_int, flux_int = lam_model, flam_model
            else:
                res, dropped = _compute_bands(ls, defs, bands, list(ps.fit_array),
                                              flux_obs_eff, mw, weighting,
                                              band_mode, mask, lam=lam_obs_eff)
                # 诊断增强（F-83/F-84）的重积分必须与主积分同一份谱/网格（同一
                # 口径才可比）：factor>1 为抽后网格，否则原生网格。
                lam_int = lam_obs_eff if lam_obs_eff is not None else lam
                flux_int = list(ps.fit_array)
        except SpecLoadError as e:
            return _load_error_response(e)
        # ── P2c（F-83/U-44）：resp_perturb —— σ_resp 换扰动散布口径。只改
        # mag_err_resp / resp_method 两列与 diagnostics{} 子键 + warnings[]
        # （T-49 明文；mag/f_mjy 等主量不进本通路）。mono 无通带形状可扰 ⇒
        # 不适用（resp_method 仍 lower_bound）。
        diag_out = {}
        # F-105③/T-72④（U-48 的 wing_logn/metal_sat_check 两键只属 API-4）：
        # API-2 收到时按 F-90② 同族纪律处理 ⇒ 忽略但回显 diagnostics_ignored[]，
        # 不得静默采纳、不得 500；未收到时键不出现（既有响应逐字节不变，T-72①
        # 的比较域外）。absorber_ident 不在本列：P3d 评审 P1-2 裁定其在 API-2
        # 消费（仅作第五类掩膜/CA-46① 波段闸门，见 merge_masks 与下方 S1 装配
        # 处；F-105③ 的这一半支修订登记 docs/TECHNICAL.md P3d 评审修复段）。
        _ignored_u48 = [k for k in ('wing_logn', 'metal_sat_check')
                        if diag.get(k) is True]
        resp_method = 'lower_bound'
        if diag.get('resp_perturb') and band_mode == 'integrated':
            spreads, rp_info = _diag.resp_perturb(
                lam_int, flux_int, defs, [b for b in bands if b in res],
                weighting=weighting, mask_ranges=mask,
                sigma_lnl={b: (res[b]['int'].get('sigma2_lnl') or 0.0)
                           for b in bands if b in res},
                seed_base=int(ls['spec_hash'][:8], 16), deadline=deadline)
            for b, spread in spreads.items():
                if spread is not None and b in res \
                        and res[b]['mag_err_resp'] is not None:
                    # T-49：扰动不得让误差变小（CA-39）⇒ max(扰动散布, 下限)
                    res[b]['mag_err_resp'] = max(res[b]['mag_err_resp'], spread)
            resp_method = 'perturbation'
            diag_out['resp_perturb'] = rp_info
            top_warn.append({'code': 'CA-39', 'reason': 'resp_perturb',
                             'message': 'σ_resp 换了口径（通带形状扰动散布，'
                                        f"perturb_n={rp_info['perturb_n']}"
                                        + ('，预算触顶已降规模（Q-27）'
                                           if rp_info['downscaled'] else '')
                                        + '）；幅度 C_SHAPE_PERT_EPS 是工程假定'
                                        '（库内无一条曲线自带形状误差，F-83/§5.4）'})
        elif diag.get('resp_perturb') and band_mode == 'mono':
            top_warn.append({'code': 'CA-15', 'reason': 'resp_perturb_mono',
                             'message': 'resp_perturb 在 band_mode=mono 下不适用：'
                                        '单色近似无通带形状可扰（F-83），'
                                        'resp_method 仍为 lower_bound'
                                        '（reason 扩展已登记 §7 日志）'})
        _deadline_check()                       # ST-5：相位边界（积分完成）
        if mode in ('auto', 'direct') and not suspect:                   # F-14 第二道闸
            cands = [{'band': b, 'n_pairable': 1,
                      'lambda_pivot_aa': res[b]['int']['lambda_pivot_aa'] or 0.0}
                     for b in paired if b in res]
            cb = fluxcal.pick_mag_sanity_band(cands)
            if cb is not None:
                suspect = abs(res[cb['band']]['m_syn_ab']
                              - paired[cb['band']]['mag']) > C_MAG_SANITY_WINDOW
        try:
            mode_eff = fluxcal.resolve_mode(
                mode, cls['uncal_kind'], flux_scale_suspect=suspect,
                anchors_available=len(aset.used_rows()),
                model_available=um_rich is not None)['mode_effective']
        except SpecLoadError as e:
            return _load_error_response(e)
        model_mode = mode_eff == 'model'
        if model_mode and not model_mode_req:
            # auto 落到 model（§3.3 auto 行：direct → anchored → model）：
            # 换 S2 模型曲线重算积分（观测谱积分已服务于上面的闸门判定）
            lam_model, flam_model, model_extrap = _model_curve(ls, defs, bands, um_rich)
            try:
                res, dropped = _compute_bands(ls, defs, bands, flam_model, flux, mw,
                                              weighting, band_mode, mask,
                                              lam=lam_model, apply_mw=False)
                lam_int, flux_int = lam_model, flam_model
            except SpecLoadError as e:
                return _load_error_response(e)
        for b, reason, msg in dropped:
            # V-7/V-13：行级 E-04 ⇒ 该行不出结果；告警挂 CA-05 族（覆盖·网格档，
            # IA-8），message 注明 E-04 reason —— reason 闭集扩展已登记 §7 日志。
            top_warn.append({'code': 'CA-05', 'reason': f'row_rejected_{reason}',
                             'message': f'波段 {b} 未出结果：{msg}（E-04 {reason}）'})
            for r in aset.rows:            # 被弃波段的锚点行退出 GLS 与比对
                if r['used'] and r['band'] == b:
                    r['used'] = False
                    top_warn.append({'code': 'CA-05', 'reason': 'anchor_band_rejected',
                                     'message': f'锚点波段 {b} 未出结果，该锚点行未参与本次锚定'})
        # ── P3d 评审 P1-2：F-103①/CA-46① 的波段占比判定（S1 侧唯一生产装配点，
        # diagnostics.cross_link_band_fracs 在此接通）。判定量与 band_integrals
        # 的 n_masked/(n_masked+n_used) 同式（T-74①）；判定网格 = 掩膜自身的原生
        # 网格（第五类掩膜在合束/模型重积分之前安置，F-106①；合束与 model 定标
        # 请求的通带占比一律按原生掩膜计——裁量登记 docs/TECHNICAL.md）。无吸收
        # 掩膜段 ⇒ 整块跳过（零开销恒等，S1 响应与 P3c 基线逐字节同值）。 ──
        if absorber_segs:
            _xrows = _diag.cross_link_band_fracs(
                lam, masks['mask_bool'],
                [(b, defs[b]['lam'], defs[b]['t']) for b in bands if b in res])
            _over = [r for r in _xrows
                     if r['bands_masked_frac'] > C_XLINK_MASK_MAX]
            for r in _over:
                res[r['band']]['int']['warnings'].append({
                    'code': 'CA-46', 'reason': 'absorber_mask_frac_over',
                    'message': '波段 %s 的通带被吸收系统掩膜占比 %.2f > '
                               'C_XLINK_MASK_MAX=%s：S1 波段量与 S3 翼量不得并排'
                               '作物理比对（同一条 mask_hash 已不一致，F-103①/'
                               'CA-46①；占比与该波段 band_integrals 的 '
                               'n_masked/used 同式，T-74①）'
                               % (r['band'], r['bands_masked_frac'],
                                  C_XLINK_MASK_MAX)})
            diag_out['cross_link_gate'] = {
                'bands_masked_frac': [r['bands_masked_frac'] for r in _xrows],
                'n_bands_over_gate': len(_over),
                'n_lines_in_masked_absorbers': 0,
                'blue_side_igm_masked': True,
                'b_source': None, 'z_source': None,
                'note': ('API-2/S1 侧的 F-103① 判定量：逐请求波段通带被吸收系统'
                         '掩膜占比（与 band_integrals 的 n_masked/used 同式，'
                         'T-74①）；n_lines/b_source/z_source 的钉住簿记在 API-4'
                         '（本端点无吸收系统格与候选线行）')}
        # anchored 闭式 GLS（F-49…F-52；C = C_obs ⊕ C_syn，F-50）
        s_anchor, h_map = None, {}
        if mode_eff == 'anchored':
            used = aset.used_rows()
            f, g, sig_g, wlists = [], [], [], []
            for r in used:
                gi, gw = fluxcal.anchor_mag_to_fnu_cgs(r['mag'], r['mag_system'],
                                                       defs[r['band']]['vega2ab'])
                top_warn.extend(gw)
                f.append(res[r['band']]['int']['fnu_cgs'])
                g.append(gi)
                sig_g.append(gi * (math.log(10.0) / 2.5)
                             * (float(r['mag_err']) if r['mag_err'] is not None else 0.0))
                wlists.append((res[r['band']]['int']['weights_idx'],
                               res[r['band']]['int']['weights_val']))
            # F-55/F-51 口径一致：mag_err_stat 通路经 propagate_band_sigma 在 σ 级乘了
            # corr_infl=√((1+r)/(1−r))；C_syn 是方差级（F-50 的 C_syn,ij = Σ w_i w_j σ_p²）
            # ⇒ 整体乘 infl²（infl 用同一 rho 估出），否则 anchored 的 σ_κ 与
            # mag_err_stat 差 √infl 倍。factor>1 时块内相关已在 σ_bin（F-108③ 通
            # 式）⇒ infl_syn 取 1（rho_prop=None），避免双计（见上方 rebin 块登记）。
            infl_syn = errs.corr_inflation(rho_prop)
            c_syn = errs.cov_syn(wlists, sigma_prop) * (infl_syn ** 2)
            fit = fluxcal.anchored_fit(f, g, np.diag(np.square(sig_g)) + c_syn)
            top_warn.extend(fit.pop('warnings'))
            h_map = {r['band']: h for r, h in zip(used, fit.pop('leverage'))}
            piv = [res[r['band']]['int']['lambda_pivot_aa'] for r in used]
            piv = [p for p in piv if p]
            s_anchor = dict(fit)
            s_anchor['anchor_lambda_range_aa'] = [min(piv), max(piv)] if piv else None
            s_anchor['anchor_provenance'] = [
                {'band': r['band'], 'mjd_obs': r['mjd'], 'mag_obs': r['mag'],
                 'mag_err': r['mag_err'], 'anchor_origin': r['anchor_origin'],
                 'gext_corr': r['gext_corr']} for r in used]
        results = [_row_result(b, res[b], aset, defs[b], mag_system, mode_eff, s_anchor,
                               h_map, sig_method, use_col, rho, mw, dt_tol_eff, tp, ls,
                               sigma_prop,
                               sigma_incomplete=sigma_incomplete,
                               sigma_dual=sigma_dual, stride_used=stride_used,
                               rho_prop=rho_prop,
                               model_mode=model_mode, resp_method=resp_method,
                               extrapolated=model_extrap.get(b, False))
                   for b in bands if b in res]
        # ── P2c（F-82/U-44）：beta_matrix —— 上三角格集 + 每格 dt/σ_β。
        # 开启时只增 diagnostics{}.beta_matrix 子键与（可达时）CA-40 告警。
        if diag.get('beta_matrix'):
            var_kappa_mag = 0.0
            if mode_eff == 'anchored' and s_anchor and s_anchor.get('value'):
                # 共享 κ* 的星等空间方差 = mag_err_cal_nonparticipant 的平方
                # （F-56 非参与波段的完全相关定标项）⇒ β 的 κ* 协方差项
                # （F-94⑤「同波段对的 −2Cov」，符号按参与分档，见 diagnostics）
                var_kappa_mag = (errs.MAG_PER_LN_FLUX * float(s_anchor['sigma'])
                                 / float(s_anchor['value'])) ** 2
            bm_rows = [{'band': r['band'], 'pivot_aa': r['lambda_pivot_aa'],
                        'mag': r['mag'], 'm_obs': r['m_obs'],
                        'm_obs_mjd': r['m_obs_mjd'],
                        'mag_err_stat': r['mag_err_stat'],
                        'mag_err_cal': r['mag_err_cal'],
                        'mag_err_resp': r['mag_err_resp'],
                        'delta_m_err': r['delta_m_err'],
                        # 参与判定与 anchored GLS 同通道（h_map 即 GLS 杠杆表）
                        'participating': (mode_eff == 'anchored'
                                          and r['band'] in h_map)}
                       for r in results]
            bm_block, bm_ca40 = _diag.beta_matrix(bm_rows, meta.get('mjd'),
                                                  dt_tol_eff,
                                                  var_kappa_mag=var_kappa_mag)
            diag_out['beta_matrix'] = bm_block
            if bm_ca40:
                top_warn.append({'code': 'CA-40', 'reason': 'beta_matrix',
                                 'message': 'beta_matrix 非对角格 β 互差显著大于'
                                            '各自 σ_β（>3σ）：单一 β 假设不成立；'
                                            '但合成测光的色项污染 ⇒ 也不得据此报'
                                            '物理 β（F-82/CA-40）'})
        # ── P2c（F-84/U-44）：anchor_reinsert —— 锚点残差再插入（诊断镜像）。
        # 只填 m_syn_local 列（§4.3 列集追加位，恒在场、未开启 null）与
        # diagnostics{} 子键；主列 mag/f_mjy/delta_m 一律不动（F-11：单一全局
        # 缩放因子不被架空；T-50 后半）。残差 g_i − κ*·f_i 插入 κ*·F 谱（支撑内
        # 乘 g_i/(κ*·f_i)）⇒ 折算到原始谱上即乘 g_i/f_i（κ* 公共模消去）。
        if diag.get('anchor_reinsert'):
            ar_cells = []
            if mode_eff == 'anchored' and s_anchor and s_anchor.get('value'):
                kappa = float(s_anchor['value'])
                for r in results:
                    b = r['band']
                    ra = next((x for x in aset.rows if x['band'] == b), None)
                    if b not in h_map or ra is None or r['mag'] is None:
                        ar_cells.append({'band': b, 'm_syn_local': None,
                                         'delta_m_local': None,
                                         'shrink_1_minus_h': None,
                                         'reason': 'not_participating'})
                        continue
                    f_i = res[b]['int']['fnu_cgs']
                    g_i, _gw = fluxcal.anchor_mag_to_fnu_cgs(
                        ra['mag'], ra['mag_system'], defs[b]['vega2ab'])
                    # _gw（Vega 换算告警）已由 anchored GLS 主通路对同一批 used
                    # 行发过 ⇒ 此处不重发（warnings 只增、不重复，T-48 前缀序）
                    if not (f_i > 0) or not (g_i > 0) or kappa <= 0:
                        ar_cells.append({'band': b, 'm_syn_local': None,
                                         'delta_m_local': None,
                                         'shrink_1_minus_h': 1.0 - h_map[b],
                                         'reason': 'nonpositive_flux'})
                        continue
                    m_loc_ab = _diag.anchor_reinsert_mag(
                        lam_int, flux_int, defs[b]['lam'], defs[b]['t'],
                        g_i / f_i, weighting=weighting,
                        mask_ranges=mask, allow_mono=(band_mode == 'mono'),
                        mono_lam_ref=defs[b]['wavelength'])
                    v2a = defs[b]['vega2ab']
                    m_loc = (m_loc_ab if mag_system == 'AB'
                             else (m_loc_ab - float(v2a))
                             if m_loc_ab is not None and v2a is not None
                             else None)
                    r['m_syn_local'] = m_loc
                    ar_cells.append({
                        'band': b, 'm_syn_local': m_loc,
                        'delta_m_local': (m_loc - r['m_obs']
                                          if m_loc is not None
                                          and r['m_obs'] is not None else None),
                        'shrink_1_minus_h': 1.0 - h_map[b]})
            ar_note = (f'本次 mode_effective={mode_eff}：F-84 的前提是 anchored '
                       '的 κ*，局部改正不适用（m_syn_local 恒 null）'
                       if mode_eff != 'anchored' else None)
            diag_out['anchor_reinsert'] = _diag.anchor_reinsert_block(
                ar_cells, note=ar_note)
        # ── P3c（F-81/F-85/U-44 后三的前两键）：z_from_lines / frame_probe。
        # 判据挂 M-6（registry.M6_LINE_TABLE / M6_FRAME_FEATURES，库内现状未
        # 复核 ⇒ None）：谱级 lambda_frame='unknown' ⇒ E-14（T-51，两项都拒）；
        # 复核表缺位 ⇒ null + 书面原因的闸门块（§4.2 键恒在场，F-94③ 的 none
        # 语义）。复核完成（数据准备落地后）才走真实现；谱数组用原生网格
        # （lam/flux_corr/sigma_px——诊断是谱级操作，与波段积分的"同一网格"
        # 要求无关；合束请求也在原生网格上做，登记 docs/TECHNICAL.md）。
        if diag.get('z_from_lines') or diag.get('frame_probe'):
            if meta.get('lambda_frame') == 'unknown':                 # F-81②/T-51
                return _err('bad_request_state',
                            "lambda_frame='unknown'：空气/真空之差 0.92–2.20 Å "
                            '折算 82–87 km/s，足以伪造或抹掉低 z，线侧诊断增强'
                            '禁用（F-81②/F-85/T-51）', 400,
                            reason='lambda_frame_unknown')
            if diag.get('z_from_lines'):
                if registry.m6_line_table_verified():
                    z_block = _diag.z_from_lines(
                        lam, flux_corr, sigma_px, z_used=z_eff,
                        line_table=registry.M6_LINE_TABLE)
                    diag_out['z_from_lines'] = z_block
                    if z_block.get('ca36'):
                        top_warn.append({'code': 'CA-36', 'reason': 'z_from_lines',
                                         'message': z_block['ca36']})
                else:
                    diag_out['z_from_lines'] = _diag.z_from_lines_gate_block()
            if diag.get('frame_probe'):
                if registry.m6_frame_features_verified():
                    diag_out['frame_probe'] = _diag.frame_probe(
                        lam, flux_corr, sigma_px,
                        features=registry.M6_FRAME_FEATURES)
                else:
                    diag_out['frame_probe'] = _diag.frame_probe_gate_block()
        _deadline_check()                       # ST-5：相位边界（装配完成，未写缓存）
        # F-106②/F-113③ 恒等态条件：factor=1、无用户掩膜段、平滑关、误差列判 ok
        # （自动表按 F-72①② 是核心规格的默认行为，不在恒等定义里；掩膜本身是
        # 标志位，数组逐点不变 ⇒ 自动表命中不破恒等）。errcol_accepted=true
        # （U-53 坚持原列）同样脱离恒等态。
        # smooth 不入 identity（F-109⑤：平滑纯前端绘制，响应/导出逐字节同，
        # 不让 stale——identity 语义 = "本响应与无预处理基线逐字节同"，恒为真）
        identity = (factor == 1 and not masks['user']
                    and ev['verdict'] == 'ok' and not errcol_accepted)
        pp_out = pre.response_block(masks, mask_h, pp_h, factor, identity,
                                    ev['verdict'], errcol_accepted, n_outlier,
                                    outlier_over,
                                    rebin=(rebin_info['echo'] if factor > 1
                                           else None))
        body_out = {
            'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': top_warn,
            'spectrum_id': ls['spectrum_id'], 'spectrum_source': ls['source'],
            'spec_hash': ls['spec_hash'], 'tid': ls['tid'],
            'meta': {k: meta.get(k) for k in ('ra_deg', 'dec_deg', 'z', 'category', 'mjd',
                                              'time_precision_d', 'lambda_frame')},
            'meta_provenance': ls['meta_provenance'],
            'lambda_frame_converted': ls['lambda_frame_converted'],
            'n_below_convert': ls['n_below_convert'],
            'anchor_rows': [{k: r.get(k) for k in ('band', 'mag', 'mag_system', 'mjd',
                                                   'mag_err', 'anchor_origin', 'note', 'used')}
                            for r in aset.rows],
            'n_anchor_excluded': len(aset.rows) - len(aset.used_rows()),
            # §4.2：diagnostics{} 恒在场、未开启为空对象（T-48①：不在逐字节
            # 比较域内）；P2c 起前三项（F-82/F-83/F-84）按开关真实现。
            'diagnostics': diag_out, 'mode_requested': mode, 'mode_effective': mode_eff,
            **({'diagnostics_ignored': _ignored_u48} if _ignored_u48 else {}),
            'flux_kind': cls['uncal_kind'], 'flux_median_cgs': ls['flux_median_cgs'],
            'lambda_unit_assumed': 'AA', 'lambda_frame': meta.get('lambda_frame'),
            'frame': {'z_source': z_src, 'x': (1.0 + z_eff) if z_eff is not None else None},
            'flux_transform_applied': 'none', 'kcorr': 'none', 'mw': mw,
            # F-97⑤ 双 j 回显（去偏后的两个 σ_px 与所取步长；代理不可用/走误差列
            # ⇒ null，不冒充）。键名登记于 docs/TECHNICAL.md specphot 小节。
            'sigma_px_j1': sigma_dual['j1'], 'sigma_px_j2': sigma_dual['j2'],
            'sigma_stride_used': stride_used,
            's_anchor': s_anchor, 'read_stats': ls['read_stats'], 'mask_hash': mask_h,
            # §4.2 preprocess{} 18 键（键集与键序的唯一载体在那行）：P1b 起真实值
            # （掩膜并集/双哈希/errcol_verdict/errcol_accepted/n_outlier_flagged），
            # P2 侧键按该行缺省值出（null/[]，不用 0 冒充未计算，TXT-23）。
            'preprocess': pp_out,
            'curve_coverage': coverage, 'results': results}
        with _cache_lock:
            _result_cache[ckey] = body_out
            _result_cache.move_to_end(ckey)
            while len(_result_cache) > C_CACHE_MAX:
                _result_cache.popitem(last=False)
        return jsonify(body_out)
    finally:
        _compute_gate.release()


def _proxy_sigma(rho, dual, flux_corr, sigma_dual):
    """F-97①③⑤ 的代理 σ 装配（去偏在进 F-55 传播之前完成），返回 (σ_px, j_used, method)。

    双 j 裁决（T-62，对去偏前的原始估计比较）：
      |ρ| ≤ C_RHO_MIN ⇒ 白噪声豁免支，允许 j=1（真白噪声下双 j 之差 <3%、
                        与 T-34 配对；有限样本的实测差由 CA-06 双值呈现暴露）；
      ρ > C_RHO_MIN   ⇒ 取双 j 较大者（ρ>0 时恒为 stride 支，与 F-97③ 一致）；
      ρ < −C_RHO_MIN  ⇒ 维持 stride（j=1 的降档只被 |ρ|≤C_RHO_MIN 允许）；
      ρ 不可估（None）⇒ 去偏不可做（k=1），同理由维持默认步长 j=C_SIGMA_STRIDE。
    选定者按 k_proxy(ρ, j_used) 去偏后返回；sigma_dual 就地写入**去偏后**的双 j
    回显值（各自除以各自 j 的 k_proxy；某 j 点数不足 ⇒ 该键 null，不冒充）。
    二阶差分在可裁决步长下不可估 ⇒ 退 mad_window（同样先去偏再进传播）。
    method ∈ {'second_diff', 'mad_window', None}（None = 两支都不可估）。"""
    s1, s2 = dual['j1'], dual['j2']
    d1 = s1 / errs.k_proxy(rho, 1) if s1 is not None else None
    d2 = (s2 / errs.k_proxy(rho, C_SIGMA_STRIDE)) if s2 is not None else None
    sigma_dual['j1'], sigma_dual['j2'] = d1, d2

    def _mw():
        j_mw = 1 if (rho is not None and abs(rho) <= C_RHO_MIN) else C_SIGMA_STRIDE
        mw = errs.sigma_mad_window(flux_corr)
        if mw is None:
            return None, None, None
        return mw / errs.k_proxy(rho, j_mw), j_mw, 'mad_window'

    if rho is not None and abs(rho) <= C_RHO_MIN and s1 is not None:
        return d1, 1, 'second_diff'          # T-62 白噪声豁免支（j=1）
    if s2 is None:
        # 默认步长不可估：白噪声支已在上行接住 j=1；这里 ρ 未知或相关 ⇒ j=1
        # 降档不成立（F-97③），交 mad_window（同样先去偏）
        return _mw()
    if rho is None:
        return d2, C_SIGMA_STRIDE, 'second_diff'
    if rho > C_RHO_MIN:
        # T-62 相关支：取双 j 较大者（原始估计；ρ>0 时恒为 stride 支）
        return (d1, 1, 'second_diff') if (s1 is not None and s1 > s2) \
            else (d2, C_SIGMA_STRIDE, 'second_diff')
    return d2, C_SIGMA_STRIDE, 'second_diff'   # ρ < −C_RHO_MIN：不降 j=1


def _sigma_px_array(flux_err, sigma_proxy, n, use_col):
    """逐像素 σ 数组（F-23）：误差列可用处取之；混合列的坏像素与全谱代理路径
    都填 sigma_proxy（混合语义由调用方标 sigma_method='mixed'），不按 0 参与传播。"""
    e = np.full(n, sigma_proxy if sigma_proxy is not None else 0.0)
    if use_col and flux_err is not None:
        col = np.asarray(flux_err, dtype=float)
        ok = np.isfinite(col) & (col > 0)
        e[ok] = col[ok]
    return e


def _compute_bands(ls, defs, bands, flux_corr, flux_obs, mw, weighting,
                   band_mode, mask, *, lam=None, apply_mw=True):
    """逐波段积分：主 weighting + 另一 weighting（σ_resp 下限 F-57）
    + 未改正通量（F-60 的 delta_a_band_mono）。
    E-04 的 no_overlap/too_few_pixels 是行级（V-7/V-13：该行不出结果、
    其余正常返回）⇒ 逐波段捕获记入 dropped；curve_missing 等仍整单拒。
    返回 (out, dropped)；全部波段都 dropped ⇒ 原样抛 E-04（400）。
    model 定标（§3.3 model 行）：lam/flux_corr 传模型曲线网格与 Fλ、
    apply_mw=False —— 模型曲线在 S2 拟合数据的银消改正帧上（F-59②），
    不得再次施加（Q-10 禁双改），delta_a 比对亦不适用。"""
    lam = ls['lam_aa'] if lam is None else lam
    other = 'energy' if weighting == 'photon' else 'photon'
    out, dropped = {}, []
    for b in bands:
        d = defs[b]
        kw = {'weighting': weighting, 'mask_ranges': mask,
              'allow_mono': band_mode == 'mono', 'mono_lam_ref': d['wavelength']}
        try:
            integ = response.band_integrals(lam, flux_corr, d['lam'], d['t'], **kw)
        except SpecLoadError as e:
            if e.code == 'E-04' and e.reason in ('no_overlap', 'too_few_pixels'):
                dropped.append((b, e.reason, str(e)))
                continue
            raise
        kw2 = dict(kw, weighting=other)
        integ_o = (response.band_integrals(lam, flux_corr, d['lam'], d['t'], **kw2)
                   if integ['band_mode'] == 'integrated' else integ)
        integ_raw = (response.band_integrals(lam, flux_obs, d['lam'], d['t'], **kw)
                     if (mw['applied'] and apply_mw) else integ)
        m_ab = response.ab_mag_from_fnu(integ['fnu_cgs'])
        delta_a = None
        if (apply_mw and mw['applied'] and integ_raw['fnu_cgs'] > 0
                and integ['lambda_pivot_aa']):
            delta_a = abs(2.5 * math.log10(integ['fnu_cgs'] / integ_raw['fnu_cgs'])
                          - mw['ebv'] * (extinction.dust_coeff(integ['lambda_pivot_aa']) or 0.0))
        out[b] = {'int': integ, 'm_syn_ab': m_ab,
                  'mag_err_resp': errs.sigma_resp_lower_bound(
                      m_ab, response.ab_mag_from_fnu(integ_o['fnu_cgs'])),
                  'delta_a': delta_a}
    if not out and dropped:
        raise SpecLoadError('E-04', dropped[0][2], reason=dropped[0][1])
    return out, dropped


def _row_result(b, rb, aset, d, mag_system, mode_eff, s_anchor, h_map,
                sig_method, use_col, rho, mw, dt_tol_eff, tp, ls, sigma_px,
                sigma_incomplete=False, sigma_dual=None, stride_used=None,
                rho_prop=None, model_mode=False, resp_method='lower_bound',
                extrapolated=False):
    """§4.2 results[i] 全键装配（err_source/err_scope 成对纪律 F-94）。

    rho 只作逐行回显（rho_lag1 = F-55 在原始像元上的估计）；σ 传播/放大一律用
    rho_prop —— factor>1 时为 None（块内相关已在 σ_bin，见 photometry 合束块），
    factor=1 时与 rho 同值（行为不变）。

    model 定标（§3.3 model 行）：合成量来自 S2 模型曲线，无逐像素 σ；S2 参数
    误差向波段通量的传播不在本期 ⇒ mag_err_stat/f_err_mjy/delta_m_err 按 null
    出并附书面原因（TXT-23），err_source='none'；覆盖外积分标 extrapolated
    + CA-12（TXT-6）。"""
    integ, warns = rb['int'], list(rb['int']['warnings'])
    m_ab, fnu = rb['m_syn_ab'], integ['fnu_cgs']
    # κ* 守卫：GLS 闭式解在病态数据下可给非正 κ*（合成/锚定流量符号或量级病态），
    # 此时 anchored 无量可出 —— mag 族按 null 出并书面说明，不让 log10(κ*) 变 500。
    kappa_ok = (mode_eff == 'anchored' and s_anchor is not None
                and s_anchor.get('value') is not None
                and float(s_anchor['value']) > 0)
    if mode_eff == 'anchored' and s_anchor is not None and not kappa_ok:
        warns.append({'code': 'CA-03', 'reason': 'kappa_nonpositive',
                      'message': '锚定缩放因子 κ* 非正，定标不可用：mag 族按 null 出'
                                 '（不用负值/零冒充，F-49 方向固定 scales_spectrum）'})
    mag_ab = (fluxcal.anchored_mag(m_ab, s_anchor['value']) if kappa_ok else m_ab)
    # 合理域闸判的是**模式生效后的星等**：direct 下即 m_syn 本身（F-14 原文语境）；
    # anchored 下原始刻度本就不可信（这正是锚定模式存在的前提），闸的对象必须是
    # κ* 改正后的 mag —— 否则归一化/另有刻度的谱在 anchored 下被整行误杀
    #（2026-10-05 审查 SP-A：27/112 条库内谱 anchored 全 null 即此因）。
    sane = C_MAG_SANITY_LO <= mag_ab <= C_MAG_SANITY_HI
    mag_available = sane and (mode_eff != 'anchored' or kappa_ok)
    if not sane:
        what = '定标后 mag' if kappa_ok else 'm_syn'
        warns.append({'code': 'CA-03', 'message': f'{what}={mag_ab:.2f} 越出合理域 '
                      f'[{C_MAG_SANITY_LO},{C_MAG_SANITY_HI}]，mag 族按 null 出'})
    if extrapolated:
        warns.append({'code': 'CA-12',
                      'message': '该波段含覆盖外模型外推（TXT-6），外推区间不可靠'})
    v2a = d['vega2ab']
    to_out = (lambda m: m if mag_system == 'AB' else m - float(v2a))
    if mag_system == 'Vega' and v2a is not None and float(v2a) == 0.0:
        # F-9：登记值恰为 0.0 无法与缺省值区分 ⇒ 仍算但挂 CA-01
        warns.append({'code': 'CA-01', 'reason': 'vega2ab_is_zero',
                      'message': '该波段 vega2ab 登记值为 0.0（可能是缺省值）'})
    # σ_stat（F-54/F-55 闭式传播；代理 ⇒ CA-06）
    m_stat, infl = None, errs.corr_inflation(rho_prop)
    if model_mode:                             # 模型曲线无逐像素 σ（TXT-23）
        pass
    elif sigma_incomplete:                     # TXT-23：坏像素无信息，σ_stat 不出数
        m_stat = None
    elif integ['weights_val'] is not None and (use_col or sigma_px[0] > 0):
        sp = sigma_px[np.asarray(integ['weights_idx'], dtype=int)]
        s_flux, infl = errs.propagate_band_sigma(integ['weights_val'], sp, rho_prop)
        m_stat = errs.mag_err_from_flux(fnu, s_flux) if mag_available and fnu > 0 else None
    if model_mode:
        warns.append({'code': 'CA-15', 'reason': 'model_mode_no_pixel_sigma',
                      'message': 'model 定标：合成量来自 S2 模型曲线，无逐像素 σ；'
                                 'S2 参数误差向波段通量的传播不在本期，'
                                 'mag_err_stat/f_err_mjy/delta_m_err 按 null 出'
                                 '（TXT-23）'})
    elif not use_col or sig_method == 'mixed':      # CA-06：代理值（含混合列的代理回填）
        dual = sigma_dual or {'j1': None, 'j2': None}
        if m_stat is not None and (dual['j1'] is not None or dual['j2'] is not None):
            # T-62：CA-06 的呈现里双 j 两个值都要看得见，并回显所取者
            def _fmt(v):
                return 'null' if v is None else f'{v:.4g}'
            warns.append({'code': 'CA-06',
                          'message': f"误差为代理值、偏乐观（F-54）："
                                     f"j=1:{_fmt(dual['j1'])},j=2:{_fmt(dual['j2'])}，"
                                     f"取 j={stride_used}（F-97⑤ 双 j 对照；"
                                     f"已按 k_proxy 去偏再进 F-55 传播）"})
        else:
            warns.append({'code': 'CA-06',
                          'message': '误差为代理值、偏乐观（F-54）' if m_stat is not None else
                          '误差列不可用且代理点数不足，mag_err_stat 按 null 出（不用 0 冒充）'})
    # 定标（F-49…F-52/F-56）
    participating = mode_eff == 'anchored' and b in h_map
    v_kappa = 0.0
    if kappa_ok:
        v_kappa = errs.mag_err_cal_nonparticipant(s_anchor['sigma'], s_anchor['value']) ** 2
        m_cal = (math.sqrt(max(v_kappa - 2.0 * h_map[b] * (m_stat or 0.0) ** 2, 0.0))
                 if participating else math.sqrt(v_kappa))
        lo_hi = s_anchor['anchor_lambda_range_aa']
        piv = integ['lambda_pivot_aa']
        if lo_hi and piv is not None and not (lo_hi[0] <= piv <= lo_hi[1]):
            warns.append({'code': 'CA-19', 'message': '目标波段 pivot 在锚点色域之外，'
                          'κ* 为外推（F-52）'})
    else:
        # §3.5 σ_cal direct 行 / TXT-23：σ_cal 无法评估 ⇒ null + 书面原因，不是 0
        m_cal = None
    mag = to_out(mag_ab) if mag_available else None
    f_mjy = response.ab_mag_to_f_mjy(mag_ab) if mag_available else None
    # 比对（F-24：每波段至多一行 —— AnchorSet 已拒同波段冲突；catalog 行自带配对 kind）
    row_a = next((r for r in aset.rows if r['band'] == b), None)
    m_obs = m_obs_mjd = dt_obs = d_m = d_m_err = m_kind = m_origin = None
    if row_a:
        m_origin, m_obs_mjd = row_a['anchor_origin'], row_a['mjd']
    if row_a and mag_available:
        if row_a['anchor_origin'] == 'catalog':
            m_kind, dt_obs = row_a['kind'], row_a['dt_obs_d']
        else:                                        # manual 行同走 F-61 容差（F-78④）
            spec_mjd = ls['meta'].get('mjd')
            if row_a['mjd'] is None or spec_mjd is None:
                m_kind = 'no_time'
            else:
                raw_dt = abs(float(row_a['mjd']) - spec_mjd)
                if raw_dt <= dt_tol_eff:
                    m_kind, dt_obs = 'nearest', max(raw_dt, tp)
        if m_kind == 'interpolated':
            warns.append({'code': 'CA-07', 'reason': 'dt_too_large',
                          'message': '实测点超出容差，按线性插值比对（禁外推，F-24）'})
        if m_kind is not None:
            mag_obs_ab = row_a['mag']
            if row_a['mag_system'] == 'Vega':
                mag_obs_ab = mag_obs_ab + float(v2a)
            m_obs = to_out(mag_obs_ab)
            d_m = m_obs - mag
            var_in = (m_stat or 0.0) ** 2 + (float(row_a['mag_err'] or 0.0)) ** 2
            var = (errs.delta_m_var_anchored(var_in, h_map[b]) if participating
                   else var_in + v_kappa if mode_eff == 'anchored' else var_in)
            if var < 0:
                # GLS 杠杆 h_i = f_i(C⁻¹f)_i/(fᵀC⁻¹f) 只保证 Σh_i=1，相关噪声下
                # 单个 h_i 可 >1 ⇒ (1−h_i)·var_in 为负：按 TXT-23 纪律 null + 书面
                # 原因，不让 sqrt(负数) 变 500、也不用 0 冒充（2026-10-05 审查 SP-B）。
                d_m_err = None
                warns.append({'code': 'CA-15', 'reason': 'delta_m_var_negative',
                              'message': '杠杆收缩后 delta_m 方差为负（杠杆 h_i>1，'
                                         '相关噪声下可出现）：delta_m_err 按 null 出，'
                                         '不用 0 冒充（TXT-23）'})
            else:
                d_m_err = math.sqrt(var)
            if row_a['mag_err'] is None:
                # §4.2 delta_m 行：m_obs 来自手加锚点而无 mag_err ⇒ delta_m_err
                # 只含合成侧一项，并写明缺输入 σ（TXT-23）。
                warns.append({'code': 'CA-15', 'reason': 'anchor_mag_err_absent',
                              'message': '锚点行缺 mag_err：delta_m_err 只含合成侧一项，'
                                         '缺输入 σ 已书面声明（TXT-23）'})
    if m_cal is None and not model_mode:
        # §3.5/TXT-23：非 anchored 时 f_err_mjy 只含 stat 项（err_scope 对齐 'stat'）。
        # CA-15 族（派生量因缺前置不可算 ⇒ null + 书面原因）；reason 'cal_not_anchored'
        # 是对 §5.4 CA-15 闭集 {lambda_eff_no_vega, bb_no_vb} 的扩展，已登记 §7 日志。
        warns.append({'code': 'CA-15', 'reason': 'cal_not_anchored',
                      'message': 'σ_cal 无法评估（mode_effective=' + mode_eff
                                + '，非 anchored）：mag_err_cal 按 null 出并附此书面原因，'
                                  '不以 0 冒充（§3.5 / TXT-23）'})
    if model_mode:
        d_m_err = None                          # 模型侧 σ 不可传播（行首 CA-15 已声明）
    total = math.sqrt((m_stat or 0.0) ** 2 + (m_cal or 0.0) ** 2)
    f_err = None if model_mode else \
        (f_mjy * (math.log(10.0) / 2.5) * total if f_mjy is not None else None)
    # read_stats.gaps 元素是 [lo, hi] 对（reader.py 的间隙装载），不是 dict
    gaps = [g for g in (ls['read_stats'].get('gaps') or [])
            if integ['band_lo_aa'] is not None
            and integ['band_lo_aa'] <= g[0] and g[1] <= integ['band_hi_aa']]
    cm = (response.curve_metrics(d['lam'], d['t']) if d['lam'] else
          {'fwhm_aa': None, 'curve_nodes': None, 'curve_px_per_fwhm': None})
    if d['curve_kind'] in (None, 'unknown'):
        warns.append({'code': 'CA-01', 'reason': 'kind_unknown', 'message': '曲线口径未登记（TXT-2）'})
    if cm['curve_px_per_fwhm'] is not None and cm['curve_px_per_fwhm'] < C_CURVE_PX_PER_FWHM_MIN:
        warns.append({'code': 'CA-01', 'reason': 'px_per_fwhm_low', 'message': '曲线采样过疏'})
    if integ['band_mode'] == 'integrated':
        # CA-02 文案随 resp_method 分档（§5.4）：扰动口径是「散布与下限取较大者」
        warns.append({'code': 'CA-02',
                      'message': ('σ_resp 为扰动散布与两加权之差下限的较大者'
                                  '（F-83）' if resp_method == 'perturbation'
                                  else 'σ_resp 为两加权之差的下限（F-57）')})
        warns.append({'code': 'CA-15', 'reason': 'lambda_eff_no_vega',
                      'message': 'lambda_eff 需 Vega 参考谱，P1 按 null 出（F-19）'})
    else:
        warns.append({'code': 'CA-04', 'message': '单色近似，仅量级参考（TXT-10）'})
    if rb['delta_a'] is not None and rb['delta_a'] > C_EXT_BANDAVG_EPS:
        warns.append({'code': 'CA-23', 'reason': 'ext_bandavg_mono',
                      'message': '波段平均消光与单色消光之差超阈（F-60）'})
    return {'band': b, 'filter_id': b,
            'curve_kind': d['curve_kind'] or 'unknown', 'curve_source': d['curve_source'],
            'curve_px_per_fwhm': cm['curve_px_per_fwhm'],
            # §4.2：curve_nodes = 通带曲线节点数；curve_source='upload' 时即请求体
            # 携带的节点数 len(lam)（不是谱像素数）
            'curve_nodes': (len(d['lam']) if d['curve_source'] == 'upload'
                            else cm['curve_nodes']),
            'interp': 'linear_T_on_spec', 'weighting': integ['weighting'],
            'band_mode': integ['band_mode'], 'mag_system': mag_system,
            'mag': mag, 'f_mjy': f_mjy,
            'mag_err_stat': m_stat if mag_available else None,
            'mag_err_cal': m_cal if mag_available else None,
            'mag_err_resp': rb['mag_err_resp'] if mag_available else None, 'f_err_mjy': f_err,
            'sigma_method': sig_method, 'rho_lag1': rho, 'corr_infl': infl,
            'not_in_budget': list(errs.NOT_IN_BUDGET),
            'lambda_pivot_aa': integ['lambda_pivot_aa'],
            'lambda_eff_aa': integ['lambda_eff_aa'],
            'lambda_phot_aa': integ['lambda_phot_aa'],
            'lambda_iso_aa': integ['lambda_iso_aa'],
            'delta_a_band_mono': rb['delta_a'],
            'overlap': integ['overlap'], 'n_used_pixels': integ['n_used_pixels'],
            'n_masked_pixels': integ['n_masked_pixels'], 'n_nonpos': integ['n_nonpos'],
            'nonpos_frac': integ['nonpos_frac'], 'gap_bridged': bool(gaps),
            'n_dup_lam': ls['read_stats'].get('n_dup_lam', 0),
            # §4.2：n_dup_lam>0 时同时回显聚合相对散布（reader stats 现成键）
            'dup_lam_rel_spread': ls['read_stats'].get('dup_lam_rel_spread'),
            'in_coverage': (integ['overlap'] is not None and integ['overlap'] >= C_OVERLAP_MIN),
            'extrapolated': bool(extrapolated),
            'm_obs': m_obs, 'm_obs_mjd': m_obs_mjd, 'dt_obs_d': dt_obs,
            'dt_tol_eff_d': dt_tol_eff, 'm_obs_origin': m_origin, 'm_obs_kind': m_kind,
            'time_precision_d': ls['meta'].get('time_precision_d'),
            'resp_method': resp_method, 'm_syn_local': None,
            'delta_m': d_m, 'delta_m_err': d_m_err,
            'err_source': ({'mag_err_stat': 'none', 'mag_err_cal': 'none',
                            'mag_err_resp': 'closed_form', 'f_err_mjy': 'none',
                            'delta_m_err': 'none'} if model_mode else
                           {'mag_err_stat': 'closed_form' if use_col else 'proxy',
                            # TXT-23：σ_cal 无法评估 ⇒ 'none'（值 null + 书面原因），不是 0
                            'mag_err_cal': 'closed_form' if m_cal is not None else 'none',
                            'mag_err_resp': 'closed_form',
                            'f_err_mjy': 'delta_method', 'delta_m_err': 'delta_method'}),
            # err_scope 对齐 TXT-23：无定标项进 total 时只含随机项 ⇒ 'stat'
            'err_scope': {'mag_err_stat': 'stat', 'mag_err_cal': 'stat+cal',
                          'mag_err_resp': 'stat+cal',
                          'f_err_mjy': 'stat+cal' if m_cal is not None else 'stat',
                          'delta_m_err': 'stat+cal' if m_cal is not None else 'stat'},
            'warnings': warns}
