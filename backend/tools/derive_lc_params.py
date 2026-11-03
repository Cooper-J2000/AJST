#!/usr/bin/env python3
"""
SN/TDE 光变派生量计算（params 数据层 P3）：
- 目标源：tags 含 'sn' 或 'tde' 的 transients
- 从本库 lightcurves 计算 4 个量，写入 extra_data.params（src='computed:lc'）：
    m_b_peak   绝对 B 星等峰值 = 峰值 AB 星等 − 距离模数（银消已在星等口径内改正）
    dm15_b     峰后 15 静止系日星等 − 峰值星等（线性插值，前后点跨度 ≤10 静止系日）
    color_bv   峰期（±5 静止系日）B−V 颜色（AB 系统）
    delta_l40  （仅 tde）ΔL40 = log10(L_40d/L_peak) ≈ −0.4·Δm，多波段取各波段平均
- 星等口径（二选一已定为方案 A，见下）：统一用**银消改正后 AB 星等**——
    行有 mag_gextcor（gext_corr=true，extinction.py 管线产物：AB + CSFD/P92 改正）直接用；
    无改正列的行按同一口径现算：obs_mag → Vega→AB（filters.vega2ab）
    → 减 A_λ = filters.gext_coeff × transients.gext_ebv（缺 gext_coeff 时按波长现算）。
  因此 M = m_corr − DM 即可，不再另减 A_B（方案 B 的 R_B≈4.1 不采用：
  选中波段未必恰好是 B，用该波段自身的 P92 系数与全站管线口径一致）。
- 距离模数：优先 gext_distmod 缓存；NULL 时用 ΛCDM（H0=70, Ωm=0.3）自算，
  该源 src 改记 'computed:lc:dm-lcdm' 区分。
- 静止系 B/V 对应观测波段：选 filters.wavelength 最接近 4400/5500×(1+z) 的条目，
  容差 ±15%；无可靠红移（NULL/≤0/phot_z/upperlimit）的源跳过。
- 只写这 4 个 key，幂等可重跑；写库时更新 params_computed 日期。
用法:
  python3 derive_lc_params.py           # dry-run，只打印统计
  python3 derive_lc_params.py --apply   # 写入数据库
"""
import math
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import get_session
from models import Transient, Lightcurve
from extinction import obs_mag, filter_meta, dust_coeff

# 静止系标准波长（Å）与观测波段选择容差
REST_B_A = 4400.0
REST_V_A = 5500.0
BAND_TOL = 0.15            # ±15%
INTERP_MAX_SPAN_REST = 10.0   # dm15/delta_l40 插值前后点最大跨度（静止系日）
COLOR_WINDOW_REST = 5.0       # color_bv 峰期窗口（静止系日）

TARGET_TAGS = ('sn', 'tde')
PARAM_KEYS = ('m_b_peak', 'dm15_b', 'color_bv', 'delta_l40')


def lcdm_distmod(z):
    """标准 ΛCDM（H0=70, Ωm=0.3）距离模数；gext_distmod 缓存缺失时的回退"""
    from astropy.cosmology import FlatLambdaCDM
    return float(FlatLambdaCDM(H0=70, Om0=0.3).distmod(z).value)


def select_band(bands_wl, target_a, tol=BAND_TOL):
    """从 {band: wavelength} 中选波长最接近 target_a 且在 ±tol 内的波段"""
    best = None
    for band, wl in bands_wl.items():
        if wl is None or wl <= 0:
            continue
        if abs(wl - target_a) / target_a <= tol:
            if best is None or abs(wl - target_a) < abs(best[1] - target_a):
                best = (band, wl)
    return best


def corr_mag_rows(rows, fmeta, ebv):
    """光变行 → [(mjd, mag_ab_corr, err)]（银消改正后 AB 星等，按 mjd 排序）。
    优先用行上现成的 mag_gextcor（管线产物，不重复改正）；
    否则按同一口径现算（obs_mag → Vega→AB → 减 A_λ）。"""
    out = []
    for lc in rows:
        if lc.upperlimit or lc.discard or lc.mjd is None:
            continue
        if lc.gext_corr and lc.mag_gextcor is not None:
            out.append((lc.mjd, lc.mag_gextcor, lc.mag_gextcor_err))
            continue
        om = obs_mag(lc)
        if om is None:
            continue
        mag, err = om
        meta = fmeta.get(lc.band) or {}
        if (lc.mag_system or '').strip().lower() == 'vega':
            mag += meta.get('vega2ab') or 0.0
        if ebv:
            coeff = meta.get('gext_coeff')
            if coeff is None:
                coeff = dust_coeff(meta.get('wavelength'))
            if coeff:
                mag -= coeff * ebv
        out.append((lc.mjd, mag, err))
    out.sort(key=lambda p: p[0])
    return out


def interp_mag(points, mjd_t, max_span_rest, one_plus_z):
    """在 mjd_t 处线性插值星等：取前后最近点，跨度（静止系日）超限返回 None。
    误差按同权重线性插值（近似）；任一侧误差缺失则 err=None。"""
    before = [p for p in points if p[0] <= mjd_t]
    after = [p for p in points if p[0] >= mjd_t]
    if not before or not after:
        return None
    p0, p1 = before[-1], after[0]
    span_rest = (p1[0] - p0[0]) / one_plus_z
    if span_rest > max_span_rest:
        return None
    if p1[0] == p0[0]:
        return p0[1], p0[2]
    w = (mjd_t - p0[0]) / (p1[0] - p0[0])
    mag = p0[1] + w * (p1[1] - p0[1])
    err = None
    if p0[2] is not None and p1[2] is not None:
        err = p0[2] + w * (p1[2] - p0[2])
    return mag, err


def compute_source(t, lc_rows, fmeta):
    """计算单个源的 4 个派生量。
    返回 (params, skips, detail)：params={key:{v,err,src}}；skips=[原因]；detail=抽样说明。"""
    params = {}
    skips = []
    detail = {'bands': {}, 'notes': []}

    z = t.redshift
    if (z is None or z <= 0
            or (t.redshift_type or '') in ('phot_z', 'upperlimit')):
        return params, ['no_redshift'], detail
    opz = 1.0 + z

    # 该源有数据的波段 → 波长
    bands_wl = {}
    for lc in lc_rows:
        if lc.band not in bands_wl:
            meta = fmeta.get(lc.band) or {}
            bands_wl[lc.band] = meta.get('wavelength')

    sel_b = select_band(bands_wl, REST_B_A * opz)
    if sel_b is None:
        return params, ['no_band_b'], detail
    band_b, wl_b = sel_b
    detail['bands']['B'] = f'{band_b} ({wl_b:.0f} Å vs 目标 {REST_B_A * opz:.0f} Å)'

    rows_b = corr_mag_rows([lc for lc in lc_rows if lc.band == band_b],
                           fmeta, t.gext_ebv)
    if not rows_b:
        return params, ['no_detection_b'], detail

    # 峰值 = 最小星等
    pk = min(rows_b, key=lambda p: p[1])
    t_pk, m_pk, e_pk = pk

    # 距离模数
    dm_src = 'computed:lc'
    dm = t.gext_distmod
    if dm is None:
        dm = lcdm_distmod(z)
        dm_src = 'computed:lc:dm-lcdm'
    detail['dm'] = f'{dm:.3f} ({"gext_distmod" if dm_src == "computed:lc" else "ΛCDM 自算"})'
    detail['peak'] = (f'{band_b} 峰 m={m_pk:.3f}'
                      f'±{e_pk:.3f}' if e_pk is not None else
                      f'{band_b} 峰 m={m_pk:.3f}（无误差）') + f' @ MJD {t_pk:.2f}'

    # 1) m_b_peak（星等已是银消改正后 AB，M = m − DM）
    params['m_b_peak'] = {'v': m_pk - dm, 'err': e_pk, 'src': dm_src}

    # 2) dm15_b：峰后 15 静止系日（观测系 15×(1+z) 天）
    t15 = t_pk + 15.0 * opz
    r15 = interp_mag(rows_b, t15, INTERP_MAX_SPAN_REST, opz)
    if r15 is None:
        skips.append('dm15_no_bracket')
    else:
        m15, e15 = r15
        err = None
        if e_pk is not None and e15 is not None:
            err = math.hypot(e_pk, e15)
        params['dm15_b'] = {'v': m15 - m_pk, 'err': err, 'src': 'computed:lc'}
        detail['dm15'] = f'm(15d)={m15:.3f} → Δm15={m15 - m_pk:.3f}'

    # 3) color_bv：峰时刻 B−V（V 在 ±5 静止系日内插值/最近点）
    sel_v = select_band(bands_wl, REST_V_A * opz)
    if sel_v is None:
        skips.append('color_no_band_v')
    else:
        band_v, wl_v = sel_v
        detail['bands']['V'] = f'{band_v} ({wl_v:.0f} Å vs 目标 {REST_V_A * opz:.0f} Å)'
        rows_v = corr_mag_rows([lc for lc in lc_rows if lc.band == band_v],
                               fmeta, t.gext_ebv)
        win = COLOR_WINDOW_REST * opz
        near = [p for p in rows_v if abs(p[0] - t_pk) <= win]
        if not near:
            skips.append('color_no_v_at_peak')
        else:
            # 窗口内能插值则插值，否则取最近点
            rv = interp_mag(near, t_pk, COLOR_WINDOW_REST * 2, opz)
            if rv is None:
                rv = min(near, key=lambda p: abs(p[0] - t_pk))[1:]
            m_v, e_v = rv
            err = None
            if e_pk is not None and e_v is not None:
                err = math.hypot(e_pk, e_v)
            params['color_bv'] = {'v': m_pk - m_v, 'err': err, 'src': 'computed:lc'}
            detail['color'] = f'V={m_v:.3f} → B−V={m_pk - m_v:.3f}'

    # 4) delta_l40（仅 tde）：各波段 ΔlogL ≈ −0.4·Δm(峰→40 静止系日) 的平均
    if 'tde' in (t.tags or []):
        t40 = t_pk + 40.0 * opz
        dl40s, dl40_errs, used = [], [], []
        for band in sorted(bands_wl):
            if bands_wl[band] is None:
                continue
            rows = corr_mag_rows([lc for lc in lc_rows if lc.band == band],
                                 fmeta, t.gext_ebv)
            if len(rows) < 2:
                continue
            r40 = interp_mag(rows, t40, INTERP_MAX_SPAN_REST, opz)
            if r40 is None:
                continue
            m_pk_b = min(rows, key=lambda p: p[1])[1]
            dl40s.append(-0.4 * (r40[0] - m_pk_b))
            if r40[1] is not None:
                dl40_errs.append(0.4 * r40[1])
            used.append(band)
        if not dl40s:
            skips.append('l40_no_post40')
        else:
            v = sum(dl40s) / len(dl40s)
            err = (sum(dl40_errs) / len(dl40_errs)) if dl40_errs else None
            params['delta_l40'] = {'v': v, 'err': err, 'src': 'computed:lc'}
            detail['l40'] = (f'ΔL40={v:.3f}（{len(dl40s)} 波段平均: '
                             f'{",".join(used)}）')

    return params, skips, detail


def main():
    apply = '--apply' in sys.argv
    sess = get_session()
    fmeta = filter_meta(sess)

    transients = [t for t in sess.query(Transient).all()
                  if set(t.tags or []) & set(TARGET_TAGS)]

    cov = defaultdict(int)
    skip_dist = defaultdict(int)
    n_no_params = 0
    params_map = {}
    samples = []
    for t in transients:
        rows = sess.query(Lightcurve).filter(Lightcurve.transient_id == t.id).all()
        params, skips, detail = compute_source(t, rows, fmeta)
        for s in skips:
            skip_dist[s] += 1
        if not params:
            n_no_params += 1
            continue
        params_map[t.id] = params
        for key in params:
            cov[key] += 1
        if len(params) >= 2:
            samples.append((t.id, t.tags, t.redshift, params, detail))
    # 抽样优先含 delta_l40 的 tde 源，其余按顺序补足 3 个
    samples.sort(key=lambda s: 0 if 'delta_l40' in s[3] else 1)
    samples = samples[:3]

    print(f'目标源数（tags 含 sn/tde）: {len(transients)}')
    print(f'算出至少一个量的源: {len(params_map)}；无可算量: {n_no_params}')
    print('\n逐量覆盖:')
    for key in PARAM_KEYS:
        print(f'  {key:12s} {cov.get(key, 0)}')
    print('\n跳过原因分布:')
    for s, n in sorted(skip_dist.items(), key=lambda kv: -kv[1]):
        print(f'  {s:20s} {n}')

    print('\n抽样计算链（3 个源）:')
    for tid, tags, z, params, detail in samples:
        print(f'  {tid} tags={tags} z={z}')
        for k, v in detail.get('bands', {}).items():
            print(f'    波段 {k}: {v}')
        if 'dm' in detail:
            print(f"    距离模数: {detail['dm']}")
        if 'peak' in detail:
            print(f"    {detail['peak']}")
        for k in ('dm15', 'color', 'l40'):
            if k in detail:
                print(f"    {detail[k]}")
        for key in PARAM_KEYS:
            if key in params:
                p = params[key]
                e = f"{p['err']:.4f}" if p['err'] is not None else 'null'
                print(f"    → {key} = {p['v']:.4f} ± {e} (src={p['src']})")

    if not apply:
        print('\n(dry-run，未写库。加 --apply 执行)')
        sess.close()
        return

    today = date.today().isoformat()
    for t in transients:
        if t.id not in params_map:
            continue
        ed = dict(t.extra_data or {})
        p = dict(ed.get('params') or {})
        p.update(params_map[t.id])   # 只动本脚本负责的 4 个 key
        ed['params'] = p
        ed['params_computed'] = today
        t.extra_data = ed
    sess.commit()
    sess.close()
    print(f'\n已写库: {len(params_map)} 个源的 extra_data.params（params_computed={today}）')


if __name__ == '__main__':
    main()
