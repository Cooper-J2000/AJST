"""M1: SED 构建器（纯计算，同步调用）。

流程（设计方案 §3-M1）：
  1. 取数：跳过 discard，频率解析沿用 fitting/jobs.py 约定
     （filters 表有效波长 λ→ν=c/λ；表外按波段名解析 '4.8GHz'/'10keV'）。
  2. 流量规范化：use_gext=True 时优先 flux_density_gextcor 列（mJy，已含银消
     改正），否则 _raw_to_mjy（AB 零点 16.4；Vega + vega2ab）。
  3. 同时化：mode='window'（默认）取 t_sel±dt 内直接取点不插值（余辉惯例）；
     mode='interp' 每波段在 t_sel 处手写 RBF 核 GP 回归内插（log t 拟合 log F；
     只在数据覆盖范围内插值，禁止外推；波段 <3 点退化为最近点并标记）。
  4. 上限点：'exclude'（默认剔除）/'include'（保留并 is_ul 标记，拟合单侧罚）。
  5. k 改正（可选，z 可用时）：幂律近似，输出静止系 SED 副本 points_rest。

时间单位：t_sel/dt 输入为天（相对 Transient.t0），内部与拟合侧用秒。
"""
import bisect
import math

import numpy as np

from models import Transient, Lightcurve, FilterDef

_C_AA_PER_S = 2.99792458e18  # 光速 [Å/s]
_DAY_S = 86400.0
_LN10 = math.log(10)


def _fj():
    """惰性 import fitting.jobs（其引擎栈依赖可选的 VegasAfterglow/emcee，
    惰性化保证本模块在无拟合依赖的环境中仍可用于 build/epochs 同步端点）"""
    from fitting import jobs as fj
    return fj


def _lc_time_days(lc):
    """Lightcurve 行 → 相对 t0 的天数；时间/单位非法返回 None"""
    factor = _fj()._TIME_UNIT_TO_S.get((lc.time_unit or 's').lower())
    if factor is None or lc.time is None:
        return None
    return lc.time * factor / _DAY_S


def _flux_mjy(lc, filt, use_gext, vega_missing):
    """单点 → (f_mjy, ferr_mjy, gext_used)；无法换算返回 (None, None, False)"""
    if use_gext and lc.gext_corr and lc.flux_density_gextcor is not None:
        unit = (lc.flux_density_gextcor_unit or 'mjy').strip().lower()
        factor = _fj()._FLUX_UNIT_TO_MJY.get(unit, 1.0)
        ferr = lc.flux_density_gextcor_err
        return (lc.flux_density_gextcor * factor,
                ferr * factor if ferr else None, True)
    conv = _fj()._raw_to_mjy(lc, filt, vega_missing)
    if conv is None:
        return None, None, False
    return conv[0], conv[1], False


def _gp_predict(ts, fs, ferrs, t0):
    """手写 RBF 核 GP 回归：log t 空间拟合 log F，在 t0 处内插。

    返回 (f_mjy, ferr_mjy)。误差 = GP 预测方差 + 拟合的测光噪声项。
    只应在数据时间覆盖范围内调用（外推由调用方禁止）。"""
    import scipy.linalg as sla
    from scipy.optimize import minimize

    x = np.log10(np.asarray(ts, dtype=float))
    y = np.log10(np.asarray(fs, dtype=float))
    ye = np.asarray(ferrs, dtype=float) / (np.asarray(fs, dtype=float) * _LN10)
    # 大样本波段只取 t0 附近最多 60 点（GP 为 O(N³)，远端点对局部内插无贡献）
    if len(x) > 60:
        idx = np.sort(np.argsort(np.abs(x - math.log10(t0)))[:60])
        x, y, ye = x[idx], y[idx], ye[idx]
    x0 = math.log10(t0)

    def _kernel(x1, x2, l, sf):
        d2 = ((x1[:, None] - x2[None, :]) / l) ** 2
        return sf * sf * np.exp(-0.5 * d2)

    def nll(theta):
        l, sf, sn = 10.0 ** theta
        K = _kernel(x, x, l, sf) + np.diag(ye * ye + sn * sn)
        try:
            c, low = sla.cho_factor(K)
        except Exception:
            return np.inf
        logdet = 2.0 * np.sum(np.log(np.diag(c)))
        alpha = sla.cho_solve((c, low), y)
        return 0.5 * y @ alpha + 0.5 * logdet + 0.5 * len(x) * np.log(2 * np.pi)

    theta0 = np.array([math.log10(0.3),
                       math.log10(max(float(np.std(y)), 0.1)),
                       math.log10(max(float(np.median(ye)), 0.01))])
    bounds = [(-2.0, 2.0), (-3.0, 2.0), (-4.0, 1.0)]
    try:
        res = minimize(nll, theta0, method='L-BFGS-B', bounds=bounds)
        theta = res.x if np.isfinite(res.fun) else theta0
    except Exception:
        theta = theta0
    l, sf, sn = 10.0 ** theta
    K = _kernel(x, x, l, sf) + np.diag(ye * ye + sn * sn)
    # cho_factor 非正定兜底：对角逐步加抖重试；仍病态则退化为最近点
    c = low = None
    for jitter in (0.0, 1e-8, 1e-6, 1e-4):
        try:
            c, low = sla.cho_factor(
                K + (jitter * sf * sf) * np.eye(len(x)) if jitter else K)
            break
        except Exception:
            continue
    if c is None:
        # 协方差矩阵病态（理论上 diag 含噪声项应为正定，防御性兜底）：
        # 退化为最近点，误差取该点测光误差
        i_near = int(np.argmin(np.abs(x - x0)))
        fs_arr = np.asarray(fs, dtype=float)
        fe_arr = np.asarray(ferrs, dtype=float)
        return float(fs_arr[i_near]), float(fe_arr[i_near])
    alpha = sla.cho_solve((c, low), y)
    kstar = sf * sf * np.exp(-0.5 * ((x - x0) / l) ** 2)
    mu = float(kstar @ alpha)
    var_lat = max(float(sf * sf - kstar @ sla.cho_solve((c, low), kstar)), 1e-10)
    var = var_lat + sn * sn  # GP 方差 + 测光噪声水平
    f = 10.0 ** mu
    return f, f * _LN10 * math.sqrt(var)


def build_sed(sess, transient_id, t_sel, dt=None, mode='window', bands=None,
              use_gext=True, upperlimits='exclude', k_correct=False,
              z_override=None, k_beta=1.0):
    """构建单历元 SED。t_sel/dt 单位天（相对 t0）。

    返回 dict：
      {t_sel, dt, z, z_source, k_corrected,
       points: [{band, nu_hz, f_mjy, ferr_mjy, is_ul, interp, t_actual_days,
                 wavelength_a, weights}],
       points_rest: [...]（仅 k_correct 且 z 可用时）,
       meta: {n_bands, n_points, lambda_min_a, lambda_max_a, has_uv, has_ir,
              mode, gext_used, warnings}}
    """
    t_sel = float(t_sel)
    if t_sel <= 0:
        raise ValueError('t_sel 必须 > 0（天，相对 t0）')
    if mode not in ('window', 'interp'):
        raise ValueError(f"mode 必须为 'window' 或 'interp'，得到 {mode!r}")
    if upperlimits not in ('exclude', 'include'):
        raise ValueError(f"upperlimits 必须为 'exclude' 或 'include'，得到 {upperlimits!r}")
    if dt is None:
        dt = 0.1 * t_sel
    dt = float(dt)
    if dt <= 0:
        raise ValueError('dt 必须 > 0（天）')

    t = sess.get(Transient, transient_id)
    if t is None:
        raise ValueError(f'暂现源不存在: {transient_id}')
    if z_override is not None:
        z, z_source = float(z_override), 'override'
    elif t.redshift:
        z, z_source = float(t.redshift), 'catalog'
    else:
        z, z_source = None, None

    filters = {f.id: f for f in sess.query(FilterDef).all()}
    rows = (sess.query(Lightcurve)
            .filter_by(transient_id=transient_id, discard=False)
            .order_by(Lightcurve.time).all())
    # bands 显式判断 None：空列表 = 用户未选任何波段（零波段、空 SED），
    # 与缺省 None（不限制波段）语义不同，不能用真值判断混淆
    band_sel = set(bands) if bands is not None else None

    warnings_out = []
    if bands is not None and not band_sel:
        warnings_out.append('未选择任何波段：SED 为空（0 波段 0 点）')
    skip_no_filter, skip_bad_t, skip_bad_flux, skip_no_err = set(), 0, 0, 0
    vega_missing = set()
    n_gext = 0
    per_band = {}  # band -> {'nu','wl','det':[...], 'ul':[...]}

    for lc in rows:
        if band_sel is not None and lc.band not in band_sel:
            continue
        filt = filters.get(lc.band)
        if filt is not None and filt.wavelength:
            nu = _C_AA_PER_S / filt.wavelength
            wl_a = float(filt.wavelength)
        else:
            nu = _fj()._parse_freq_band(lc.band)
            if nu is None:
                skip_no_filter.add(lc.band)
                continue
            wl_a = _C_AA_PER_S / nu
        t_days = _lc_time_days(lc)
        if t_days is None or t_days <= 0:
            skip_bad_t += 1
            continue
        f_mjy, ferr_mjy, gext = _flux_mjy(lc, filt, use_gext, vega_missing)
        if f_mjy is None or f_mjy <= 0:
            skip_bad_flux += 1
            continue
        if gext:
            n_gext += 1
        b = per_band.setdefault(lc.band, {'nu': nu, 'wl': wl_a, 'det': [], 'ul': []})
        entry = {'t_days': t_days, 'f_mjy': float(f_mjy),
                 'ferr_mjy': float(ferr_mjy) if ferr_mjy else None,
                 'weights': float(lc.weights if lc.weights is not None else 1.0)}
        if lc.upperlimit:
            b['ul'].append(entry)
        else:
            if not ferr_mjy or ferr_mjy <= 0:
                skip_no_err += 1
                continue
            b['det'].append(entry)

    points = []
    lo, hi = t_sel - dt, t_sel + dt
    for band, b in sorted(per_band.items(), key=lambda kv: kv[1]['nu']):
        # 表按原始 time 排序，混合时间单位时 t_days 未必有序，统一重排
        b['det'].sort(key=lambda e: e['t_days'])
        b['ul'].sort(key=lambda e: e['t_days'])
        det, ul = b['det'], b['ul']
        common = {'band': band, 'nu_hz': b['nu'], 'wavelength_a': b['wl']}
        if mode == 'window':
            sel = [e for e in det if lo <= e['t_days'] <= hi]
            for e in sel:
                points.append({**common, 'f_mjy': e['f_mjy'],
                               'ferr_mjy': e['ferr_mjy'], 'is_ul': False,
                               'interp': False, 't_actual_days': e['t_days'],
                               'weights': e['weights']})
        else:  # interp
            if (len(det) >= 3
                    and det[0]['t_days'] <= t_sel <= det[-1]['t_days']):
                f, ferr = _gp_predict(
                    [e['t_days'] for e in det], [e['f_mjy'] for e in det],
                    [e['ferr_mjy'] for e in det], t_sel)
                points.append({**common, 'f_mjy': f, 'ferr_mjy': ferr,
                               'is_ul': False, 'interp': True,
                               't_actual_days': t_sel,
                               'weights': 1.0})
            elif det:
                # 点数 <3 或 t_sel 超出覆盖：退化最近点并标记
                near = min(det, key=lambda e: abs(e['t_days'] - t_sel))
                points.append({**common, 'f_mjy': near['f_mjy'],
                               'ferr_mjy': near['ferr_mjy'], 'is_ul': False,
                               'interp': False, 't_actual_days': near['t_days'],
                               'weights': near['weights']})
                warnings_out.append(
                    f'波段 {band} 仅 {len(det)} 点或 t_sel 超出覆盖范围，'
                    '退化为最近点（不外推）')
        if upperlimits == 'include':
            for e in ul:
                if lo <= e['t_days'] <= hi:
                    # 上限点：f_mjy 为上限值本身（拟合侧按 f=0/err=UL 编码）
                    points.append({**common, 'f_mjy': e['f_mjy'],
                                   'ferr_mjy': e['f_mjy'], 'is_ul': True,
                                   'interp': False, 't_actual_days': e['t_days'],
                                   'weights': e['weights']})

    if skip_no_filter:
        warnings_out.append(f'filters 表无定义且波段名无法解析频率，已跳过: '
                            f'{sorted(skip_no_filter)}')
    if skip_bad_t:
        warnings_out.append(f'{skip_bad_t} 点时间非法（t<=0 或未知时间单位），已跳过')
    if skip_bad_flux:
        warnings_out.append(f'{skip_bad_flux} 点流量非法/单位不支持，已跳过')
    if skip_no_err:
        warnings_out.append(f'{skip_no_err} 个探测点误差缺失或为 0，已跳过')
    if vega_missing:
        warnings_out.append(f'Vega 星等但缺 vega2ab 转换系数，已跳过波段: '
                            f'{sorted(vega_missing)}')
    if z is None:
        warnings_out.append('无红移：宿主消光律不红移（仅为观测系等效值），'
                            'k 改正不可用，黑体 R/L 不可算')

    wls = [p['wavelength_a'] for p in points]
    meta = {
        'n_bands': len({p['band'] for p in points}),
        'n_points': len(points),
        'lambda_min_a': min(wls) if wls else None,
        'lambda_max_a': max(wls) if wls else None,
        'has_uv': any(w < 3000 for w in wls),
        'has_ir': any(w > 1e4 for w in wls),
        'mode': mode,
        'gext_used': bool(use_gext and n_gext > 0),
        'warnings': warnings_out,
    }
    result = {'t_sel': t_sel, 'dt': dt, 'z': z, 'z_source': z_source,
              'k_corrected': False, 'points': points, 'meta': meta}

    if k_correct:
        if z:
            # 幂律近似 K 改正：ν→(1+z)ν，Fν→Fν·(1+z)^(β−1)（β=k_beta，默认 1）
            factor = (1.0 + z) ** (k_beta - 1.0)
            result['points_rest'] = [
                {**p, 'nu_hz': p['nu_hz'] * (1.0 + z),
                 'f_mjy': p['f_mjy'] * factor,
                 'ferr_mjy': p['ferr_mjy'] * factor}
                for p in points]
            result['k_corrected'] = True
            meta['k_beta'] = k_beta
        else:
            warnings_out.append('k_correct=True 但无红移，已跳过 k 改正')
    return result


def suggest_epochs(sess, transient_id, min_bands=3, dt_frac=0.1, max_epochs=20):
    """扫描全部测光点，按 t±dt_frac·t 窗口聚合，返回满足 min_bands 的历元建议。

    返回 [{t_days, n_bands, n_points, bands}]，按 n_points 降序截断 max_epochs。
    相邻窗口互相重叠的候选只保留点数最多的一个。"""
    dt_frac = float(dt_frac)
    if dt_frac <= 0:
        raise ValueError('dt_frac 必须 > 0')
    rows = (sess.query(Lightcurve)
            .filter_by(transient_id=transient_id, discard=False)
            .order_by(Lightcurve.time).all())
    det_times, all_times = {}, {}  # band -> sorted [t_days]
    for lc in rows:
        t_days = _lc_time_days(lc)
        if t_days is None or t_days <= 0:
            continue
        all_times.setdefault(lc.band, []).append(t_days)
        if not lc.upperlimit:
            det_times.setdefault(lc.band, []).append(t_days)
    for d in (det_times, all_times):
        for band in d:
            d[band] = sorted(d[band])

    candidates = []  # (n_bands, n_points, t_days, bands)
    bands_all = sorted(all_times)
    for band, ts in det_times.items():
        for t in ts:
            lo, hi = t * (1 - dt_frac), t * (1 + dt_frac)
            nb, np_, hit = 0, 0, []
            for b in bands_all:
                arr = all_times[b]
                i0 = bisect.bisect_left(arr, lo)
                i1 = bisect.bisect_right(arr, hi)
                if i1 > i0:
                    np_ += i1 - i0
                    d = det_times.get(b)
                    if d and bisect.bisect_right(d, hi) > bisect.bisect_left(d, lo):
                        nb += 1
                        hit.append(b)
            if nb >= min_bands:
                candidates.append((nb, np_, t, hit))

    # 贪心去重叠：按 (波段数, 点数) 降序，与已选历元窗口互叠的跳过
    candidates.sort(key=lambda c: (-c[0], -c[1]))
    picked = []
    for nb, np_, t, hit in candidates:
        lo, hi = t * (1 - dt_frac), t * (1 + dt_frac)
        if any(not (hi < p['t_days'] * (1 - dt_frac)
                    or lo > p['t_days'] * (1 + dt_frac)) for p in picked):
            continue
        picked.append({'t_days': t, 'n_bands': nb, 'n_points': np_,
                       'bands': sorted(hit)})
        if len(picked) >= max_epochs:
            break
    picked.sort(key=lambda e: -e['n_points'])
    return picked


def sed_to_csv(result):
    """SED 表 CSV 序列化（csv 模块 QUOTE_MINIMAL：波段名等含逗号/引号/换行时
    自动加引号转义；元数据以 # 注释行随附，§4.4 可复现要求）"""
    import csv
    import io

    meta = result.get('meta') or {}
    buf = io.StringIO()
    buf.write('# AJST SED 表\n')
    buf.write(f"# t_sel_days={result.get('t_sel')} dt_days={result.get('dt')} "
              f"z={result.get('z')} z_source={result.get('z_source')} "
              f"mode={meta.get('mode')} k_corrected={result.get('k_corrected')}\n")
    buf.write(f"# lambda_range_a=[{meta.get('lambda_min_a')}, "
              f"{meta.get('lambda_max_a')}] "
              f"has_uv={meta.get('has_uv')} has_ir={meta.get('has_ir')} "
              f"gext_used={meta.get('gext_used')}\n")
    w = csv.writer(buf, lineterminator='\n')
    header = ['band', 'nu_hz', 'wavelength_a', 't_actual_days', 'f_mjy',
              'ferr_mjy', 'is_ul', 'interp']
    w.writerow(header)
    for p in result.get('points') or []:
        w.writerow([p['band'], f"{p['nu_hz']:.6e}", f"{p['wavelength_a']:.6e}",
                    f"{p['t_actual_days']:.6f}", f"{p['f_mjy']:.6e}",
                    f"{p['ferr_mjy']:.6e}", p['is_ul'], p['interp']])
    if result.get('k_corrected'):
        buf.write('# --- 静止系副本（k 改正，幂律近似）---\n')
        w.writerow(header)
        for p in result.get('points_rest') or []:
            w.writerow([p['band'], f"{p['nu_hz']:.6e}", f"{p['wavelength_a']:.6e}",
                        f"{p['t_actual_days']:.6f}", f"{p['f_mjy']:.6e}",
                        f"{p['ferr_mjy']:.6e}", p['is_ul'], p['interp']])
    return buf.getvalue()
