"""API-10 orchestration (S4): the per-term 1-sigma error budget of one
template curve (design doc 02 §3.5 F-33…F-39, §5 API-10).

* Q-7: ``n_draws ∈ {0} ∪ [3,200]`` -- 0 means systematics only (the
  photometric term is then *absent*, not zero: F-34).
* Default n_draws is 32, not the engine's 64 and not the 200 cap, because of
  UNVERIFIED-2 (POS-6:承诺上限 = 已实测可达上限): measured on this host
  (burst_advocate interpreter, 2026-09-29), one `error_budget` call with
  n_times=300 costs **13.7–18.1 s at n_draws=200** across the nine shipped
  templates (144.0 s total) and **2.4–3.1 s at n_draws=32** (24.9 s total).
  200 stays requestable but is over the 10 s interactive budget, so the
  default is 32 and the applied value is echoed in `provenance` (F-44 显形).
* F-35: the distance term is broken out top-level with its `describe()` so
  the panel can show it alone and flag it dominant (sn2002ap: 0.47 mag).
* F-36/F-37: `meta.components` passes through verbatim -- per-band
  `gain_mag_per_dex` + `gain_source` (the frontend greys any source other
  than "unfold fit") and the sed_residual `all_bands_mag` table.
* F-39: engine warnings pass through verbatim; the Monte-Carlo diagonal-only
  caveat (`cross_talk_mag_per_dex`) must reach the UI.

ST-1: no DB session anywhere in this module -- the budget is template-only
and needs no catalogue row.  A-3: a read never rebuilds; a stale surface is
refused with TL_STALE (E-12), same gate as the predict path.
"""

from __future__ import annotations

import math
from pathlib import Path

from . import engine, paths, predict as tl_predict

#: UNVERIFIED-2 closure (see module docstring): 200 draws实测 13.7–18.1 s ⇒
#: 默认降到 32（实测 2.4–3.1 s），上限仍 200（C_MAX_DRAWS / Q-7）。
DEFAULT_N_DRAWS = 32
MAX_DRAWS = 200
MIN_DRAWS = 3            # 引擎 _MIN_DRAWS；0 = 只算系统项
DEFAULT_SEED = 20260918  # 引擎默认 seed（固定 ⇒ 可复现，F-38）
DEFAULT_N_TIMES = 300


def validate_request(body) -> dict:
    """Q-7/形状校验，在任何引擎调用之前（纯函数，TLError 抛出）。"""
    if not isinstance(body, dict):
        raise tl_predict.TLError("TL_INCONSISTENT_ARGS", 400,
                                 "请求体必须是 JSON 对象")
    tid = body.get("template_id")
    if not isinstance(tid, str) or not paths.valid_id(tid):
        raise tl_predict.TLError("TL_TEMPLATE_UNKNOWN", 404,
                                 f"非法模板 id: {tid!r}")
    band = body.get("band")
    if not isinstance(band, str) or not band:
        raise tl_predict.TLError("TL_PARAM_NOT_FINITE", 400,
                                 "band 必填且非空（大小写敏感）")
    z = tl_predict._opt_finite(body.get("z"), "z")
    if z is not None and z < 0:
        raise tl_predict.TLError("TL_PARAM_NOT_FINITE", 400,
                                 f"z 必须 ≥ 0，得到 {z}")
    mode = body.get("mode", "auto")
    if mode not in ("auto", "band", "mono"):
        raise tl_predict.TLError("TL_INCONSISTENT_ARGS", 400,
                                 f"mode 必须是 auto/band/mono，得到 {mode!r}")

    nd_raw = body.get("n_draws", DEFAULT_N_DRAWS)
    if isinstance(nd_raw, bool) or not isinstance(nd_raw, (int, float)) \
            or not math.isfinite(nd_raw) or int(nd_raw) != nd_raw:
        raise tl_predict.TLError("TL_PARAM_NOT_FINITE", 400,
                                 f"n_draws 必须是整数，得到 {nd_raw!r}")
    n_draws = int(nd_raw)
    if not (n_draws == 0 or MIN_DRAWS <= n_draws <= MAX_DRAWS):
        raise tl_predict.TLError(
            "TL_QUOTA", 400,
            f"n_draws ∈ {{0}} ∪ [{MIN_DRAWS},{MAX_DRAWS}]（Q-7），得到 {n_draws}"
            f"（0 = 只算系统项；{MAX_DRAWS} 次实测 13.7–18.1 s/模板，"
            f"默认 {DEFAULT_N_DRAWS} 实测 2.4–3.1 s）",
            limit=MAX_DRAWS, got=n_draws)

    seed_raw = body.get("seed", DEFAULT_SEED)
    if isinstance(seed_raw, bool) or not isinstance(seed_raw, (int, float)) \
            or not math.isfinite(seed_raw) or int(seed_raw) != seed_raw:
        raise tl_predict.TLError("TL_PARAM_NOT_FINITE", 400,
                                 f"seed 必须是整数，得到 {seed_raw!r}")

    nt_raw = body.get("n_times", DEFAULT_N_TIMES)
    if isinstance(nt_raw, bool) or not isinstance(nt_raw, (int, float)) \
            or not math.isfinite(nt_raw) or int(nt_raw) < 1:
        raise tl_predict.TLError("TL_PARAM_NOT_FINITE", 400,
                                 "n_times 必须是正整数")
    n_times = min(int(nt_raw), tl_predict.POINTS_MAX)   # F-44 钳制显形

    return {"template_id": tid, "band": band, "z": z, "mode": mode,
            "n_draws": n_draws, "seed": int(seed_raw),
            "n_times": n_times, "n_times_requested": int(nt_raw)}


def run(req: dict, *, root=None) -> dict:
    """一次 `chromashift.error_budget` + 契约整形。TLError / 引擎异常上抛。"""
    base = Path(root) if root is not None else paths.library_root()
    avail = engine.available(base)
    if not avail["ok"]:
        raise tl_predict.TLError(
            "TL_ENGINE_UNAVAILABLE", 409,
            "K 改正引擎当前不可用（"
            + "; ".join(r["detail"] for r in avail["reasons"]) + "）",
            reasons=[r["code"] for r in avail["reasons"]])
    cs = engine.require()
    tid = req["template_id"]
    known = cs.manifests(base)
    if tid not in known:
        raise tl_predict.TLError(
            "TL_TEMPLATE_UNKNOWN", 404,
            f"模板 {tid!r} 不存在（可用: {', '.join(sorted(known))}）",
            available=sorted(known))
    spec = cs.TemplateSpec.from_yaml(known[tid])          # ST-7

    # E-12/A-3：与 predict 同一道 stale 门；读路径永不 rebuild
    from chromashift import registry
    npz = base / "data" / "surfaces" / f"{tid}.npz"
    stale = registry.stale_reasons(npz, spec, base)
    if stale:
        raise tl_predict.TLError(
            "TL_STALE", 409,
            f"模板 {tid} 的面已陈旧（{', '.join(stale)}），需先重建",
            stale_because={"engine_inputs": stale,
                           "catalog_rows": None, "filter_vendor": None})

    budget = cs.error_budget(tid, req["band"], req["z"],
                             mode=req["mode"], n_times=req["n_times"],
                             n_draws=req["n_draws"], seed=req["seed"],
                             root=base)
    summary = budget.summary()   # terms_max_mag/dominant/at_peak/warnings/components
    dist_meta = dict((budget.meta.get("components") or {}).get("distance") or {})
    notes = []
    if req["n_times"] != req["n_times_requested"]:
        notes.append(f"n_times 已钳到 {tl_predict.POINTS_MAX}"
                     f"（请求 {req['n_times_requested']}，F-44）")
    if req["n_draws"] == 0:
        notes.append("n_draws=0：photometric 项缺席（系统项下限，F-34），"
                     "不是 0 误差")
    return {
        "code": "TL_OK",
        "template_id": tid,
        "band": req["band"],
        "mode": summary["mode"],
        "z": summary["z"],
        "budget": {
            "n_draws": summary["n_draws"],
            "seed": summary["seed"],
            "terms_max_mag": summary["terms_max_mag"],   # F-34：缺席而非 0
            "total_max_mag": summary["total_max_mag"],
            "dominant": summary["dominant"],
            "at_peak": summary["at_peak"],
            "warnings": summary["warnings"],             # F-39 原样透传
            "components": summary["components"],         # F-36/F-37 逐带表
        },
        "distance": {                                     # F-35：可单列
            "mag": dist_meta.get("mag"),
            "describe": dist_meta.get("describe"),
            "is_dominant": summary["dominant"] == "distance",
        },
        "provenance": {
            "engine_version": engine.version(),
            "engine_code_sha256": engine.code_sha256(),
            "cosmology": engine.cosmology(),
            "backend": "in-process",
            "host_n_draws_default": DEFAULT_N_DRAWS,
            "host_max_draws": MAX_DRAWS,
            "host_n_times_applied": req["n_times"],
        },
        "notes": notes,
    }
