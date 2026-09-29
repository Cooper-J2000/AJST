"""Domain verdicts and out-of-window clipping for template curves (F-25/F-27).

The engine answers a request whole or refuses it; the host feature instead
*clips* explicit epoch lists to the template's declared window and answers the
overlap (F-27 -- the one deliberate behavioural wrap around the engine, T-08
pins pointwise equality on the overlap).  Everything else is classification:
a successful `Prediction` is graded `ok` / `extrapolated` (F-25) from its own
`meta`, never by parsing warning prose, and each downgrade carries the CA code
the frontend keys its badges on (E-30).

`impossible` is not produced here: it is the refused request (E_DOMAIN /
E_FILTER), which predict.py turns into an error response before a curve exists.
"""

from __future__ import annotations

#: trust grades that make a curve "extrapolated" rather than "ok" (F-25, CA-07).
WEAK_TRUST = ("nominal", "nominal-guess", "unknown")


def judge(prediction, *, requested_mode: str, clipped_epochs: list,
          time_origin_kind: str, spec, t_valid) -> dict:
    """Grade one engine Prediction.  Pure: no I/O, no engine calls.

    `clipped_epochs` are the observer-frame days dropped by the F-27 clip;
    `t_valid` is the surface's rest-frame window, passed in (not hidden in
    `meta`) so the 21-key passthrough stays verbatim (F-54/T-10).  Returns the
    §4.2 `domain` object: state + machine reasons + CA alerts + boundary
    numbers a reader needs next to the verdict.
    """
    meta = prediction.meta
    reasons: list[str] = []
    alerts: list[str] = []
    state = "ok"

    def degrade(reason: str, alert: str | None = None):
        nonlocal state
        state = "extrapolated"
        reasons.append(reason)
        if alert:
            alerts.append(alert)

    # F-26: auto/band silently became mono -> the colour term is gone (CA-02).
    if requested_mode in ("auto", "band") and prediction.mode == "mono":
        degrade("mode-downgraded-to-mono", "CA-02")
    # CA-04: M/K are NaN by construction at the edge bands (TXT-5) -- the curve's
    # apparent magnitudes stay valid, so this is extrapolated, not impossible.
    if meta.get("absolute_mag_reference") == "out-of-coverage":
        degrade("out-of-coverage", "CA-04")
    elif meta.get("absolute_mag_reference") == "partial":
        degrade("partial-out-of-coverage", "CA-04")
    # CA-07: the band's response is nominal, not a measured curve.
    if meta.get("band_trust") in WEAK_TRUST:
        degrade(f"band-trust-{meta.get('band_trust')}", "CA-07")
    # CA-09: bounded linear continuation beyond the measured coverage was used.
    if (meta.get("sed_extrapolated_fraction") or 0) > 0:
        degrade("sed-extrapolated", "CA-09")
    # CA-05: the table may still carry dust; M and K absorb it in opposite signs.
    if meta.get("absolute_mag_is_intrinsic") in (False, "unknown"):
        degrade("absolute-mag-not-intrinsic", "CA-05")
    # Q-9 / CA-06: caller-added MW reddening on a table already de-reddened.
    red = meta.get("reddening") or {}
    table_red = meta.get("table_reddening") or {}
    if (red.get("mw_E_B_V") or 0) > 0 and table_red.get("mw_removed") is True:
        degrade("double-reddening-risk", "CA-06")

    # F-27: clipped epochs do not downgrade the surviving curve -- they are
    # listed, greyed and explained (TXT-7), and the overlap is untouched.
    if clipped_epochs:
        reasons.append("clipped-epochs")
        alerts.append("CA-03")
    # Q-11 / CA-14: the axis zero is the table's first row, not an event epoch.
    if time_origin_kind == "table-first-row":
        reasons.append("time-origin-not-event")
        alerts.append("CA-14")
    # CA-15: the source table was already rest-frame (only sn1998bw; TXT-17).
    if (spec.photometry.get("time") or {}).get("frame") == "rest":
        reasons.append("table-time-frame-rest")
        alerts.append("CA-15")

    return {
        "state": state,
        "reasons": reasons,
        "alerts": alerts,
        "z_min_for_band": meta.get("z_min_for_band"),
        "z_max_for_band": meta.get("z_max_for_band"),
        "t_valid_days": [float(v) for v in t_valid],
        "absolute_mag_reference": meta.get("absolute_mag_reference"),
        "band_trust": meta.get("band_trust"),
        "clipped_epochs": list(clipped_epochs),
    }


def clip_to_window(times_obs_days, t_valid, one_plus_z, stretch=1.0):
    """Split explicit observer-frame epochs into (kept, clipped) by t_valid.

    The comparison happens in the rest frame, against the same round-off
    allowance the engine's check_time_domain grants (1e-9 of the window), so a
    kept epoch can never be refused downstream and a clipped one never answers.
    """
    import numpy as np
    lo, hi = (float(t_valid[0]), float(t_valid[1]))
    eps = 1e-9 * max(1.0, abs(hi - lo))
    t_obs = np.asarray(times_obs_days, float)
    t_rest = t_obs / (one_plus_z * stretch)
    keep = (t_rest >= lo - eps) & (t_rest <= hi + eps)
    return t_obs[keep], t_obs[~keep]
