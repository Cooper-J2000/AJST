"""F-45/F-45': in-domain answerability of a template against its own table.

Semantics follow the engine-side indomain.py word for word (OUT-11 -- rewritten
here against the public API only): a point is one (band, rest-frame epoch) pair
from the table the surface was built from; the point is answered when
`predict` returns a finite apparent magnitude for it; a ChromaShiftError (or a
NaN) means unanswered.  Per-band attribution (F-45') classifies every failure
into the closed vocabulary {not-on-surface, out-of-t_valid,
below-min-active-bands, error:<code>} so the UI can say which band lost how
many points and why (T-45: Σn == total, Σok == answered).

The table is read by its own manifest's declaration -- shipped CSVs do not
share the wizard's §4.3 layout (file name, time unit, epoch zero and frame all
differ; sn1998bw's table is already rest-frame, CA-15).  Rows carrying
upperlimit/discard never entered the fit (the engine's clean_rows dropped
them), so they are not points; labels absent from band.map never entered
either.

Timing (02 §8.1): the full nine shipped templates are 5959 points ≈ 7.8 s,
worst single template 1.22 s -- computed once at build time and cached in
library.json (IA-4), never on the list path.
"""

from __future__ import annotations

import csv as _csv
import math
from collections import Counter
from pathlib import Path

from . import engine, paths

#: F-45' closed reason vocabulary (T-45 asserts membership of `reason`).
REASONS = ("not-on-surface", "out-of-t_valid", "below-min-active-bands")

_TRUE_TOKENS = {"t", "true", "1", "y"}


def _truthy(v, declared) -> bool:
    toks = {str(x).strip().lower() for x in (declared or ["true"])}
    return str(v).strip().lower() in (toks | _TRUE_TOKENS)


def _read_points(csv_path: Path, spec) -> list[tuple[str, float]]:
    """(registry band, rest-frame days) pairs of the fit-entering rows.

    Everything comes from the manifest: time column/unit/epoch_zero/frame,
    band column + label→registry map, flag columns.  Rows dropped by the
    engine's clean_rows (upper limits, discards, unmapped labels) are skipped
    the same way here.
    """
    ph = spec.photometry
    time_cfg = ph.get("time") or {}
    t_col = time_cfg.get("column", "time")
    t_unit = time_cfg.get("unit", "s")
    frame = time_cfg.get("frame", "observer")
    ez = time_cfg.get("epoch_zero") or {}
    z = float(spec.z)

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))

    # 零点：fixed ⇒ value 换算到时间列单位；first_point ⇒ 全表最早一行
    if ez.get("kind") == "first_point":
        zero = min(float(r[t_col]) for r in rows)
    else:
        zero = float(ez.get("value") or 0.0)
        ez_unit = ez.get("unit") or t_unit
        if ez_unit == t_unit:
            pass
        elif ez_unit == "s" and t_unit == "day":
            zero /= 86400.0
        elif ez_unit == "day" and t_unit == "s":
            zero *= 86400.0
        # mjd 列配 fixed mjd 零点：同单位直减；其余组合按声明直减

    def _days(r):
        try:
            t = float(r[t_col]) - zero
        except (TypeError, ValueError):
            return None
        d = t / 86400.0 if t_unit == "s" else t   # mjd 减零点后天数口径同 day
        return d / (1.0 + z) if frame == "observer" else d

    out = []
    if (ph.get("layout") or "long") == "wide":
        # wide：每波段一列，声明在 value.bands（column/error_column/filter）。
        # 每个非空单元格 = 一个点；引擎 clean_rows 同样跳过空缺。
        vbands = (ph.get("value") or {}).get("bands") or {}
        for r in rows:
            days = _days(r)
            if days is None:
                continue
            for label, cfg in vbands.items():
                band = (cfg or {}).get("filter") or label
                v = r.get((cfg or {}).get("column") or label)
                if v is None or not str(v).strip():
                    continue
                try:
                    float(v)
                except ValueError:
                    continue
                out.append((band, days))
        return out

    band_cfg = ph.get("band") or {}
    b_col = band_cfg.get("column", "band")
    band_map = band_cfg.get("map") or {}
    flags = ph.get("flags") or {}
    ul_col = flags.get("upper_limit_column")
    dc_col = flags.get("discard_column")

    for r in rows:
        band = band_map.get(r.get(b_col))
        if band is None:
            continue
        if ul_col and _truthy(r.get(ul_col), flags.get("upper_limit_true_values")):
            continue
        if dc_col and _truthy(r.get(dc_col), flags.get("discard_true_values")):
            continue
        days = _days(r)
        if days is None:
            continue
        out.append((band, days))
    return out


def _classify(band, t_rest, surf, exc=None):
    """One unanswered point → its F-45' reason."""
    if band not in surf.bands:
        return "not-on-surface"
    lo, hi = float(surf.t_valid[0]), float(surf.t_valid[1])
    if not (lo <= t_rest <= hi):
        return "out-of-t_valid"
    if exc is not None:
        ctx = getattr(exc, "context", None) or {}
        if "valid_days" in ctx or "requested_days" in ctx:
            return "out-of-t_valid"
        if exc.code == "E_FILTER":
            return "not-on-surface"
    try:
        import numpy as np
        n_active = int(surf.min_active_bands_at(np.asarray([t_rest]))[0])
        if n_active < int(getattr(surf, "require_min_bands", 2)):
            return "below-min-active-bands"
    except Exception:
        pass
    return f"error:{exc.code}" if exc is not None else "error:nan"


def compute(template_id: str, root=None) -> dict:
    """{total, answered, by_band:{band:{n, ok, reason, reasons}}} for one
    template.  Engine calls only; no DB, no files beyond the library root."""
    cs = engine.require()
    base = Path(root) if root is not None else paths.library_root()
    spec = cs.TemplateSpec.from_yaml(paths.template_yaml(base, template_id))
    surf = engine.load_surface_cached(template_id, base)
    bank = engine.bank_cached(base)
    dist = spec.distance_obj()
    z = float(spec.z)
    one_plus = 1.0 + z

    csv_name = (spec.photometry or {}).get("file") or f"{template_id}.csv"
    points = _read_points(base / "data" / "raw" / csv_name, spec)
    per_band: dict[str, dict] = {}
    total = answered = 0
    for band, t_rest in points:
        b = per_band.setdefault(band, {"n": 0, "ok": 0, "reasons": Counter()})
        b["n"] += 1
        total += 1
        if band not in bank:                       # E_FILTER without the call
            b["reasons"]["not-on-surface"] += 1
            continue
        try:
            pred = cs.predict(surf, bank, dist, band, z=z,
                              times_obs_days=[t_rest * one_plus], mode="band")
            mag = pred.mag[0]
            if mag is not None and math.isfinite(float(mag)):
                b["ok"] += 1
                answered += 1
                continue
            reason = _classify(band, t_rest, surf)
        except cs.ChromaShiftError as exc:
            reason = _classify(band, t_rest, surf, exc=exc)
        b["reasons"][reason] += 1

    by_band = {}
    for band in sorted(per_band):
        b = per_band[band]
        reasons = dict(b["reasons"])
        by_band[band] = {
            "n": b["n"], "ok": b["ok"],
            "reason": (None if not reasons else
                       max(reasons, key=reasons.get)),
            "reasons": reasons,
        }
    return {"total": total, "answered": answered, "by_band": by_band}
