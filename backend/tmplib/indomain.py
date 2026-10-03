"""F-45/F-45': in-domain answerability of a template against its own table.

Semantics follow the engine-side indomain.py word for word: a point is one
(band, rest-frame epoch) pair from the *fit-entering* sample set -- i.e.
`chromashift.build.load_samples(spec, bank, root, distance)`, the rows that
survive the engine's clean_rows (upper limits, discards, unmapped labels,
missing values, sentinel rows are already gone) -- and the point is answered
when `predict` returns a finite apparent magnitude for it; a ChromaShiftError
(or a NaN) means unanswered.  Per-band attribution (F-45') classifies every
failure into the closed vocabulary {not-on-surface, out-of-t_valid,
below-min-active-bands, error:<code>} so the UI can say which band lost how
many points and why (T-45: Σn == total, Σok == answered).

口径更正（审核 P1-2）：本模块最初按 manifest 自行重读 CSV、只剔
upperlimit/discard/未映射行——分母因此比引擎真实进拟合样本多（九模板
6565 vs 底账 5959）。现在直接调用引擎的 load_samples，与 01 §E.6 实测口径
逐字一致。这放宽了 OUT-11 的「只用公开 API」：load_samples 是引擎建面管线
的正式入口（引擎侧旁路脚本 indomain.py 用的就是它），行为由 C_ENGINE_PIN
的 code 指纹守卫——引擎代码一变，本模块的数字随面一起过期重算，不会静默
漂移。

Timing (02 §8.1): the full nine shipped templates are 5959 points ≈ 7.8 s,
worst single template 1.22 s -- computed once at build time and cached in
library.json (IA-4), never on the list path.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import numpy as np

from . import engine, paths

#: F-45' closed reason vocabulary (T-45 asserts membership of `reason`).
REASONS = ("not-on-surface", "out-of-t_valid", "below-min-active-bands")


def _classify(band, t_rest, surf, exc=None):
    """One unanswered point → its F-45' reason."""
    if band not in surf.bands:
        return "not-on-surface"
    lo, hi = float(surf.t_valid[0]), float(surf.t_valid[1])
    if not (lo - 1e-9 <= t_rest <= hi + 1e-9):
        # 与引擎 check_time_domain 的舍入容差同量级；load_samples 的样本本就
        # 落在窗内，这里只防御浮点边界。
        return "out-of-t_valid"
    if exc is not None:
        ctx = getattr(exc, "context", None) or {}
        if "valid_days" in ctx or "requested_days" in ctx:
            return "out-of-t_valid"
        if exc.code == "E_FILTER":
            return "not-on-surface"
    try:
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

    # 与引擎侧 indomain.py 逐字同口径：进拟合样本 = load_samples（clean_rows
    # 之后）。表行 → 样本的一切剔除规则（上限/discard/哨兵/缺值/未映射）都
    # 由引擎负责，本层不再复制一份会漂移的口径。
    samples, _audit = cs.build.load_samples(spec, bank, root=base, distance=dist)

    per_band: dict[str, dict] = {}
    total = answered = 0
    for s in samples:
        band = s.band
        b = per_band.setdefault(band, {"n": 0, "ok": 0, "reasons": Counter()})
        for t_rest in np.asarray(s.t_rest, float):
            b["n"] += 1
            total += 1
            if band not in bank:                   # E_FILTER without the call
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
