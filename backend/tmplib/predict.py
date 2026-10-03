"""API-6 orchestration: one template curve, validated, graded, disclosed.

Pipeline (design doc 02 §3.4, §5):

1. Q-1/Q-3/Q-4/Q-5/Q-6/Q-9/Q-11 validation -- everything finite, closed
   vocabularies, sizes capped *before* the engine is touched (ST-2).
2. Staleness gate: the engine-input axis must be clean or the read refuses
   (E-12; A-3 -- a read never rebuilds).
3. F-24: relocation below the engine's low-z floor needs an explicit mu /
   d_L_Mpc or allow_low_z (E-11); a caller-supplied mu becomes the distance.
4. F-27: explicit epochs are clipped to the surface's rest window instead of
   refused whole; the overlap is evaluated by the engine untouched (T-08).
5. F-05'/IA-14: time_origin is resolved and provenance-labelled here, server
   side (ST-17: suggestions carry their source and are never auto-applied).
6. The response carries the engine's meta verbatim (F-54), NaN -> null at the
   boundary with a domain reason (F-55), and the Δμ disclosure (POS-9/F-46).

ST-1: no DB session is held anywhere in this module -- the catalogue
counterpart row arrives as a plain dict (`catalog`), fetched and closed by the
caller before we run.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np

from . import domain, engine, guard, mudelta, paths

#: host-side caps (§7); engine thresholds are read live via engine.limits()
MAX_POINTS = 512          # C_MAX_POINTS: requested epoch list
POINTS_MAX = 1200         # C_POINTS_MAX: n_times clamp (F-44)
SAME_Z_TOL = 1e-6         # C_SAME_Z_TOL


class TLError(Exception):
    """One API error: stable code + HTTP status + Chinese message + context."""

    def __init__(self, code: str, http: int, message: str, **context):
        super().__init__(message)
        self.code, self.http, self.message, self.context = code, http, message, context


# ── validation helpers (Q-3: one finite-float parser at the boundary) ────────

def _finite(value, name: str) -> float:
    # bool 显式拒收（审核 P3-5）：float(True)==1.0，否则 z=true 会当 z=1 通过
    if isinstance(value, bool):
        raise TLError("TL_PARAM_NOT_FINITE", 400,
                      f"{name} 必须是有限数值，得到布尔值 {value!r}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise TLError("TL_PARAM_NOT_FINITE", 400,
                      f"{name} 必须是有限数值，得到 {value!r}") from None
    if not math.isfinite(v):
        raise TLError("TL_PARAM_NOT_FINITE", 400,
                      f"{name} 必须是有限数值，得到 {value!r}")
    return v


def _opt_finite(value, name: str):
    return None if value is None else _finite(value, name)


def _epoch_list(value, name: str) -> list[float] | None:
    """Q-5: non-empty, finite, <= MAX_POINTS, ascending-deduplicated."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise TLError("TL_PARAM_NOT_FINITE", 400, f"{name} 必须是非空数值列表")
    out = sorted({_finite(v, name) for v in value})
    if len(out) > MAX_POINTS:
        raise TLError("TL_QUOTA", 400,
                      f"{name} 长度 {len(out)} 超过上限 {MAX_POINTS}（不截断）",
                      limit=MAX_POINTS, got=len(out))
    return out


def _reddening(body: dict):
    """Q-9: build the engine Reddening from the request's reddening block."""
    spec = body.get("reddening")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise TLError("TL_INCONSISTENT_ARGS", 400, "reddening 必须是对象")
    cs = engine.require()
    law_name = spec.get("law", "grey")
    if law_name == "grey":
        law = cs.GreyLaw()
    elif law_name == "ccm89":
        law = cs.Ccm89Law()
    elif law_name == "powerlaw":
        beta = spec.get("beta")
        if beta is None:
            raise TLError("TL_DECLARE_MISSING", 400,
                          "reddening.law=powerlaw 必须给 beta（该律无引用台账条目，"
                          "β 由调用方提供并自行论证）")
        law = cs.PowerLawLaw(_finite(beta, "reddening.beta"))
    else:
        raise TLError("TL_INCONSISTENT_ARGS", 400,
                      f"reddening.law 必须是 grey/ccm89/powerlaw，得到 {law_name!r}")
    r_v = _opt_finite(spec.get("r_v"), "reddening.r_v")
    if r_v is not None and r_v <= 0:
        raise TLError("TL_PARAM_NOT_FINITE", 400, "reddening.r_v 必须 > 0")
    mw = _opt_finite(spec.get("mw_ebv"), "reddening.mw_ebv") or 0.0
    host = _opt_finite(spec.get("host_ebv"), "reddening.host_ebv") or 0.0
    if mw < 0 or host < 0:
        raise TLError("TL_PARAM_NOT_FINITE", 400, "reddening 的 E(B-V) 必须 ≥ 0")
    return cs.Reddening(mw_E_B_V=mw, host_E_B_V=host,
                        r_v_mw=r_v or 3.1, r_v_host=r_v or 3.1, law=law)


# ── time origin (F-05' / IA-14 / ST-17 / Q-11) ───────────────────────────────

def _epoch_zero(spec) -> dict:
    return (spec.photometry.get("time") or {}).get("epoch_zero") or {}


def _effective_ez_unit(spec) -> str | None:
    ez = _epoch_zero(spec)
    return ez.get("unit") or (spec.photometry.get("time") or {}).get("unit")


def _first_point_mjd(spec, root: Path) -> float | None:
    """For epoch_zero.kind=first_point: the table's minimum time as an MJD.

    Only readable when the time column is already MJD (both shipped
    first_point templates are); otherwise the absolute zero is unknowable
    here and reported as null rather than guessed (ST-17).
    """
    if (_effective_ez_unit(spec)) != "mjd":
        return None
    import pandas as pd
    candidates = spec.photometry_paths(root)
    csv = next((c for c in candidates if c.is_file()), None)
    if csv is None:
        return None
    col = (spec.photometry.get("time") or {}).get("column", "time")
    try:
        return float(pd.read_csv(csv, usecols=[col])[col].min())
    except Exception:
        return None


def catalog_t0_mjd(catalog: dict | None) -> float | None:
    """The counterpart's transients.t0 as MJD (DB column is a naive UTC datetime)."""
    if not catalog:
        return None
    t0 = catalog.get("t0")
    if t0 is None:
        return None
    if isinstance(t0, datetime):
        return (t0 - datetime(1858, 11, 17)).total_seconds() / 86400.0
    if isinstance(t0, (int, float)):
        return float(t0)
    return None


def resolve_time_origin(spec, user_mjd, catalog, root: Path) -> dict:
    """The §4.2 `time_origin` block.  Six kinds (IA-14):

    user-supplied (the caller's MJD) / epoch-zero-value (manifest declares an
    absolute MJD zero) / epoch-zero-source (zero is relative; absolute date
    lives only in source prose) / table-first-row (first_point manifest) /
    catalog-t0 (zero taken from the catalogue counterpart's t0) / event-t0
    (catalogue sources' default -- never produced for a template curve).

    Suggestions are labelled with their provenance and never auto-applied
    (ST-17).  `first_point` + blank input answers 200 with the offset, it does
    not refuse (Q-11); refusal needs an explicit event-t0 demand (E-24, in
    predict_curve).
    """
    ez = _epoch_zero(spec)
    ez_kind = ez.get("kind")
    ez_source = ez.get("source", "")
    cat_mjd = catalog_t0_mjd(catalog)
    cat_id = (catalog or {}).get("id")

    out = {"value_mjd": user_mjd, "kind": None, "source": ez_source,
           "suggested_mjd": None, "suggested_source": None, "t_zero_mjd": None}

    if ez_kind == "first_point":
        out["kind"] = "table-first-row"
        zero = _first_point_mjd(spec, root)
        out["t_zero_mjd"] = zero
        if cat_mjd is not None:
            out["suggested_mjd"] = cat_mjd
            out["suggested_source"] = (f"transients.t0 of {cat_id}（从库反查；"
                                       "模板未声明绝对日期）")
            if zero is not None:
                out["offset_vs_catalog_t0_days"] = round(zero - cat_mjd, 4)
    elif ez_kind == "fixed" and _effective_ez_unit(spec) == "mjd":
        out["kind"] = "epoch-zero-value"
        out["t_zero_mjd"] = float(ez.get("value"))
        out["suggested_mjd"] = float(ez.get("value"))
        out["suggested_source"] = "epoch_zero.value 声明"
    else:  # fixed with a relative unit (day/s): the zero is relative
        out["kind"] = "epoch-zero-source"
        if cat_mjd is not None:
            out["t_zero_mjd"] = cat_mjd
            out["suggested_mjd"] = cat_mjd
            out["suggested_source"] = (f"transients.t0 of {cat_id}（从库反查；"
                                       "模板未声明绝对日期）")
    if cat_mjd is not None:
        out["catalog_t0_mjd"] = cat_mjd

    if user_mjd is not None:
        out["kind"] = "user-supplied"
        out["source"] = f"用户输入基准时刻 MJD {user_mjd}"
    return out


# ── the curve ────────────────────────────────────────────────────────────────

def _sanitize(obj):
    """F-55: NaN/Inf -> None at the boundary; numpy -> plain python."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_sanitize(v) for v in obj.tolist()]
    if isinstance(obj, (np.floating, np.integer)):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def predict_curve(body: dict, *, catalog: dict | None = None,
                  root: str | Path | None = None,
                  rows_live_hash: str | None = None) -> dict:
    """Validate -> gate -> predict -> grade.  Raises TLError on refusal.

    `rows_live_hash`: the 7th fingerprint recomputed by the route layer from a
    short, already-closed DB session (F-52/ST-1); None = unverifiable, noted
    not refused."""
    base = Path(root) if root is not None else paths.library_root()
    avail = engine.available(base)
    if not avail["ok"]:
        raise TLError("TL_ENGINE_UNAVAILABLE", 409,
                      "K 改正引擎当前不可用（"
                      + "; ".join(r["detail"] for r in avail["reasons"]) + "）",
                      reasons=[r["code"] for r in avail["reasons"]])
    cs = engine.require()

    # Q-1: id shape and existence.
    tid = body.get("template_id")
    if not isinstance(tid, str) or not paths.valid_id(tid):
        raise TLError("TL_TEMPLATE_UNKNOWN", 404, f"非法模板 id: {tid!r}")
    known = cs.manifests(base)
    if tid not in known:
        raise TLError("TL_TEMPLATE_UNKNOWN", 404,
                      f"模板 {tid!r} 不存在（可用: {', '.join(sorted(known))}）",
                      available=sorted(known))
    spec = cs.TemplateSpec.from_yaml(known[tid])  # ST-7: directed read

    band = body.get("band")
    if not isinstance(band, str) or not band:
        raise TLError("TL_PARAM_NOT_FINITE", 400, "band 必填且非空（大小写敏感）")

    z = _opt_finite(body.get("z"), "z")
    if z is None:
        z = float(spec.z)
    if z < 0:
        raise TLError("TL_PARAM_NOT_FINITE", 400, f"z 必须 ≥ 0，得到 {z}")

    mode = body.get("mode", "auto")
    if mode not in ("auto", "band", "mono"):
        raise TLError("TL_INCONSISTENT_ARGS", 400,
                      f"mode 必须是 auto/band/mono，得到 {mode!r}")

    # time_origin: null (= 该模板自己的 T0) or a finite positive MJD (Q-4).
    time_origin_raw = body.get("time_origin")
    user_mjd = None
    if time_origin_raw is not None:
        user_mjd = _finite(time_origin_raw, "time_origin")
        if user_mjd <= 0:
            raise TLError("TL_PARAM_NOT_FINITE", 400,
                          "time_origin 必须是正的 MJD 或 null")

    # Q-11: only an explicit event-t0 demand on a first_point template refuses.
    if (body.get("require_time_origin_kind") == "event-t0"
            and _epoch_zero(spec).get("kind") == "first_point"):
        alts = sorted(t for t, p in known.items()
                      if (_epoch_zero_safe(cs, p).get("kind")) != "first_point")
        raise TLError("TL_TIME_ORIGIN_UNAVAILABLE", 409,
                      f"模板 {tid} 的零点是表内最早一行，不是事件时刻；"
                      "可留空（按形状比较）或改用其它模板",
                      epoch_zero_kind="first_point",
                      suggested_mjd=catalog_t0_mjd(catalog),
                      alternatives=alts)

    # F-24: below the low-z floor the distance must be declared, not defaulted.
    lim = engine.limits()
    allow_low_z = bool(body.get("allow_low_z"))
    mu_in = _opt_finite(body.get("mu"), "mu")
    d_l_in = _opt_finite(body.get("d_l_mpc"), "d_l_mpc")
    relocated = abs(z - spec.z) > SAME_Z_TOL
    declared = spec.distance or {}
    declared_low_ok = (not relocated and declared.get("kind") != "cosmological") \
        or (not relocated and bool(declared.get("allow_low_z")))
    if z < lim["low_z"] and not declared_low_ok:
        if mu_in is None and d_l_in is None and not allow_low_z:
            raise TLError("TL_LOW_Z_DISTANCE", 409,
                          f"z={z:g} < {lim['low_z']:g}：本征速度给出的距离误差比宇宙学"
                          "本身还大，宇宙学 d_L 在此不是一个测量。请给 mu / d_l_mpc，"
                          "或显式 allow_low_z=true（TXT-13）",
                          z=z, low_z=lim["low_z"])

    # Q-6: extrapolation is opt-in and acknowledged.
    extrapolation = body.get("extrapolation", "reject")
    if extrapolation not in ("reject", "linear"):
        raise TLError("TL_INCONSISTENT_ARGS", 400,
                      f"extrapolation 必须是 reject/linear，得到 {extrapolation!r}")
    if extrapolation == "linear" and not body.get("advanced_ack"):
        raise TLError("TL_INCONSISTENT_ARGS", 400,
                      "extrapolation=linear 需要 advanced_ack=true（外推默认不开放，F-28）")

    # Q-5 + F-44: epoch list cap (no silent truncation); n_times clamp is echoed.
    times_rest = _epoch_list(body.get("times_rest_days"), "times_rest_days")
    times_obs = _epoch_list(body.get("times_obs_days"), "times_obs_days")
    if times_rest is not None and times_obs is not None:
        raise TLError("TL_INCONSISTENT_ARGS", 400,
                      "times_rest_days 与 times_obs_days 只能给一个")
    n_times_req = body.get("n_times", 300)
    if not isinstance(n_times_req, (int, float)) or isinstance(n_times_req, bool) \
            or not math.isfinite(n_times_req) or int(n_times_req) < 1:
        raise TLError("TL_PARAM_NOT_FINITE", 400, "n_times 必须是正整数")
    n_times = min(int(n_times_req), POINTS_MAX)
    n_times_clamped = n_times != int(n_times_req)

    reddening = _reddening(body)

    # Staleness gate (E-12): a read refuses a stale surface, never rebuilds (A-3).
    from chromashift import registry
    npz = base / "data" / "surfaces" / f"{tid}.npz"
    stale = registry.stale_reasons(npz, spec, base)
    if stale:
        raise TLError("TL_STALE", 409,
                      f"模板 {tid} 的面已陈旧（{', '.join(stale)}），需先重建",
                      stale_because={"engine_inputs": stale,
                                     "catalog_rows": None, "filter_vendor": None})

    # 第 7 项指纹（F-52）：库行漂移 ⇒ 同一道 E-12 拒绝。路由层已用短 session
    # 算好 live hash 传进来（ST-1）；没传 = 无绑定/库暂不可达，记 note 不拒。
    lib = None
    try:
        lib = paths.read_library(base)
    except ValueError:
        pass
    entry = (lib or {}).get("templates", {}).get(tid) or {}
    recorded_rows = entry.get("rows_sha256")
    notes = []
    if recorded_rows and entry.get("extract"):
        if rows_live_hash is None:
            notes.append("第 7 项指纹（catalog rows）本次未验证：无库绑定或库暂不可达")
        elif rows_live_hash != recorded_rows:
            raise TLError("TL_STALE", 409,
                          f"模板 {tid} 冻结行集与库当前数据不符（rows_sha256 漂移，"
                          "CA-11）：需重新取数并重声明（向导重建）",
                          stale_because={"engine_inputs": [],
                                         "catalog_rows": ["rows-drifted"],
                                         "filter_vendor": None})

    # The surface is needed for the F-27 clip; the cached load is sha-guarded.
    surf = engine.load_surface_cached(tid, base)
    one_plus = 1.0 + z
    clipped: list[float] = []
    if times_rest is not None:
        times_obs_days = np.asarray(times_rest, float) * one_plus  # T-06
    elif times_obs is not None:
        times_obs_days = np.asarray(times_obs, float)
    else:
        times_obs_days = None
    if times_obs_days is not None:
        kept, dropped = domain.clip_to_window(times_obs_days, surf.t_valid, one_plus)
        clipped = [float(v) for v in dropped]
        if kept.size == 0:
            raise TLError("TL_OUT_OF_DOMAIN", 409,
                          f"请求时刻全部落在模板适用窗 t∈[{surf.t_valid[0]:g}, "
                          f"{surf.t_valid[1]:g}] 天（静止系）之外",
                          t_valid_days=[float(surf.t_valid[0]), float(surf.t_valid[1])],
                          clipped_epochs=clipped)
        times_obs_days = kept

    distance = None
    if mu_in is not None:
        distance = cs.Distance.from_distance_modulus(mu_in, z, source="api caller mu")
    elif d_l_in is not None:
        distance = cs.Distance(d_L_Mpc=d_l_in, kind="measured", z=z,
                               source="api caller d_L_Mpc")
    elif allow_low_z and z < lim["low_z"] and not declared_low_ok:
        # F-24 的放行必须随距离对象进引擎：引擎对 relocated / 声明 cosmological
        # 的模板自建距离时不带 allow_low_z（template.py），不在此显式构造，
        # 上面的宿主侧校验就是一道死开关。
        distance = cs.Distance.cosmological_at(z, allow_low_z=True)

    pred = cs.predict_template(tid, band, z=z, root=base,
                               times_obs_days=times_obs_days,
                               n_times=n_times, mode=mode, reddening=reddening,
                               distance=distance,
                               extrapolation=extrapolation)

    # ST-15/E-20：vendor 漂移不阻断（引擎按快照算的数仍自洽），但必须随曲线
    # 显形（审核 P3-3：此前只进 API-1/13，预测/导出响应看不到 CA-01）。
    vendor_note = None
    try:
        vendor = guard.vendor_axis(base)
    except Exception:
        vendor = {"ok": None}
    vendor_alert = []
    if vendor.get("ok") is False:
        vendor_alert.append("CA-01")
        vendor_note = vendor.get("message") or "滤光片 vendor 快照与现文件不一致"

    t_origin = resolve_time_origin(spec, user_mjd, catalog, base)
    dom = domain.judge(pred, requested_mode=mode, clipped_epochs=clipped,
                       time_origin_kind=t_origin["kind"], spec=spec,
                       t_valid=surf.t_valid)
    if dom["absolute_mag_reference"] == "out-of-coverage":
        # F-55: the NaN columns must be explained at the boundary.
        if "out-of-coverage" not in dom["reasons"]:
            dom["reasons"].append("out-of-coverage")

    points = {
        "time_obs_days": pred.time_obs_days,
        "time_obs_s": pred.time_obs_days * 86400.0,
        "mag_AB": pred.mag,
        "M_abs_AB": pred.absolute_mag,
        "K_mag": pred.k_correction,
        "mag_AB_err": None,
        "stretch": None,
    }
    # ST-9: the reference-epoch shift is server arithmetic.  rel seconds need
    # the template zero's absolute MJD; absent that, the key is omitted.
    if user_mjd is not None and t_origin.get("t_zero_mjd") is not None:
        points["time_rel_s"] = ((pred.time_obs_days
                                 - (user_mjd - t_origin["t_zero_mjd"])) * 86400.0)

    mu = None
    try:
        mu = mudelta.compute_mu(spec, catalog, entry)
    except Exception:
        mu = None  # μ 披露失败不拖垮曲线本身（IA-3 精神）

    curve = {
        "kind": "template-prediction",
        "template_id": tid,
        "transient_id": mudelta.counterpart_of(tid, entry),
        "band": band, "mode": pred.mode, "z": z,
        "relocated": bool(pred.meta.get("relocated")),
        "time_origin": t_origin,
        "domain": dom,
        "mu": mu,
        "distance_modulus": float(pred.distance_modulus),
        "points": points,
        "meta": dict(pred.meta),          # F-54: verbatim 21-key passthrough
        "warnings": list(pred.warnings),
        "alerts": dom["alerts"] + vendor_alert,
    }
    if vendor_note:
        notes.append(vendor_note)
    if n_times_clamped:
        notes.append(f"n_times 已钳到 {POINTS_MAX}（请求 {int(n_times_req)}，F-44）")
    return {
        "code": "TL_OK",
        "curve": _sanitize(curve),
        "provenance": {
            "engine_version": engine.version(),
            "engine_code_sha256": engine.code_sha256(),
            "cosmology": engine.cosmology(),
            "backend": "in-process",
            "host_n_times_applied": n_times,
            "host_max_points": MAX_POINTS,
        },
        "notes": notes,
    }


def _epoch_zero_safe(cs, yaml_path) -> dict:
    try:
        return _epoch_zero(cs.TemplateSpec.from_yaml(yaml_path))
    except Exception:
        return {}
