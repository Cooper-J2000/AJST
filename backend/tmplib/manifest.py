"""The §3.3 DB→manifest mapping as executable code (M_ID…M_PROV) + Q-2/Q-10/Q-12.

`validate_build_request` runs entirely before the engine is touched (T-33 pins
zero engine calls on these failures); `build_manifest` turns the validated
declaration into the manifest dict; `write_manifest` serialises it and then
re-parses it with `TemplateSpec.from_yaml` -- the write is only accepted if the
engine's own strict loader agrees with it (self-check, M_SCHEMA).

Field rules (all §3.3): time.frame is always `observer` (the catalogue's `time`
is seconds since t0; declaring `rest` would divide by (1+z) twice);
epoch_zero = {kind: fixed, value: 0, unit: s} tied to the source's t0;
reddening.mw_removed == (rowset == "gext"), host_removed stays `unknown` with a
source note (M_RED); stretch.supported = false (M_STR); require_min_bands is
explicit (F-47: default 2, >2 needs a notes justification); the colour-term
cap is never overridden (F-47/M_MCT -- the field is refused outright).
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import yaml

from . import engine, paths

#: Q-2: the declaration fields with no default -- missing any is E-02 with a
#: per-field echo.  (id/object_class/redshift{value,source}/distance{kind,…}/
#: rowset/null_system_policy.)
REQUIRED_FIELDS = ("id", "transient_id", "object_class",
                   "redshift.value", "redshift.source", "distance.kind",
                   "rowset", "null_system_policy")

DISTANCE_KINDS = ("cosmological", "measured", "measured-secondary",
                  "redshift-velocity-field")


class DeclError(Exception):
    """One declaration failure: E-02 shape, per-field echo (Q-2/Q-12)."""

    def __init__(self, message: str, missing: list[str] | None = None,
                 **context):
        super().__init__(message)
        self.missing = missing or []
        self.context = context


def _finite_opt(v, name):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise DeclError(f"{name} 必须是有限数值，得到 {v!r}")
    if not math.isfinite(f):
        raise DeclError(f"{name} 必须是有限数值，得到 {v!r}")
    return f


def validate_build_request(body: dict) -> dict:
    """Q-2/Q-10/Q-12/Q-3/Q-4 for API-5.  Returns a normalised declaration.

    Nothing here touches the DB or the engine (T-33); `engine.engine_enums()`
    is the only engine-adjacent read and it has a hardcoded fallback.
    """
    if not isinstance(body, dict):
        raise DeclError("请求体必须是 JSON 对象")
    missing = []
    for f in REQUIRED_FIELDS:
        cur = body
        for part in f.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            missing.append(f)
    if missing:
        raise DeclError("强制声明缺字段（Q-2，逐项列出；这些字段库里没有可"
                        "信的默认，猜了就是谎报）", missing=missing)

    tid = body["id"]
    if not isinstance(tid, str) or not paths.valid_id(tid):
        raise DeclError(f"id 必须匹配 {paths.ID_RE.pattern}，得到 {tid!r}",
                        missing=["id"])
    red = body["redshift"] or {}
    z = _finite_opt(red.get("value"), "redshift.value")
    if z is None or z < 0:
        raise DeclError("redshift.value 必须是 ≥0 的有限数值", missing=["redshift.value"])

    # Q-12: error/error_kind pairing, refused here with a better message than
    # the engine's load-time rejection -- and before any engine call.
    z_err = _finite_opt(red.get("error"), "redshift.error")
    z_kind = red.get("error_kind")
    kinds = engine.engine_enums()["z_error_kinds"]
    if z_err is not None and z_err <= 0:
        raise DeclError("redshift.error 必须 > 0", missing=["redshift.error"])
    if z_err is not None and not z_kind:
        raise DeclError("填了 redshift.error 就必须选 error_kind（TXT-19：三者"
                        "互不等价，引擎加载时也会拒，本层先拦）",
                        missing=["redshift.error_kind"], z_error_kinds=kinds)
    if z_kind and z_err is None:
        raise DeclError("选了 error_kind 但 redshift.error 为空：没有误差数值"
                        "的 kind 是无出处声明", missing=["redshift.error"])
    if z_kind and z_kind not in kinds:
        raise DeclError(f"error_kind 必须是 {kinds} 之一，得到 {z_kind!r}",
                        missing=["redshift.error_kind"], z_error_kinds=kinds)

    dist = body["distance"] or {}
    dkind = dist.get("kind")
    if dkind not in DISTANCE_KINDS:
        raise DeclError(f"distance.kind 四选一 {DISTANCE_KINDS}，得到 {dkind!r}"
                        "；库 gext_distmod 是 Planck18.distmod(z) 缓存，不是"
                        "指示器测量，标成 measured 是造假（F-18/OUT-12）",
                        missing=["distance.kind"])
    mu = _finite_opt(dist.get("mu"), "distance.mu")
    d_l = _finite_opt(dist.get("d_L_Mpc"), "distance.d_L_Mpc")
    if dkind != "cosmological" and mu is None and d_l is None:
        raise DeclError("非 cosmological 距离必须填 mu 或 d_L_Mpc（U-22）",
                        missing=["distance.mu|d_L_Mpc"])
    allow_low_z = bool(dist.get("allow_low_z"))
    if dkind == "cosmological" and z < engine.limits()["low_z"] and not allow_low_z:
        raise DeclError(f"z={z:g} < {engine.limits()['low_z']:g} 时选 cosmological "
                        "必须显式 allow_low_z=true（F-18/TXT-13：本征速度项比"
                        "宇宙学本身还大）", missing=["distance.allow_low_z"])

    rowset = body["rowset"]
    if rowset not in ("raw", "gext"):
        raise DeclError("rowset 二选一 raw|gext（F-11：一次建面只允许一种）",
                        missing=["rowset"])
    from .extract import NULL_POLICIES
    policy = body["null_system_policy"]
    if policy not in NULL_POLICIES:
        raise DeclError(f"null_system_policy 四档 {NULL_POLICIES}（F-10：测光"
                        "系统零猜测）", missing=["null_system_policy"])
    declarations = body.get("declarations") or []
    if policy == "declare":
        if not isinstance(declarations, list) or not declarations:
            raise DeclError("policy=declare 需要 declarations: [{band, system}]",
                            missing=["declarations"])
        systems = set(engine.engine_enums()["known_systems"])
        for d in declarations:
            if not isinstance(d, dict) or not d.get("band") \
                    or d.get("system") not in systems:
                raise DeclError("declarations 每项需 {band, system}，system 必须"
                                f"在引擎词表 {sorted(systems)} 内",
                                missing=["declarations"])

    # Q-10/F-47: build-tuning knobs are explicit; the colour-term cap is not
    # overridable at all (it changes which bands enter the surface).
    if body.get("max_colour_term_mag") is not None:
        raise DeclError("max_colour_term_mag 不开放覆盖（F-47：引擎默认 0.15；"
                        "它改变哪些波段进面，即改变曲线本身）",
                        refused_field="max_colour_term_mag")
    rmb = body.get("require_min_bands")
    if not isinstance(rmb, int) or isinstance(rmb, bool) or rmb < 2:
        raise DeclError("require_min_bands 必须显式给出且为 ≥2 的整数（F-47：2"
                        " 是插值本身定下的地板）", missing=["require_min_bands"])
    notes = (body.get("validity_notes") or "").strip()
    if rmb > 2 and not notes:
        raise DeclError("require_min_bands>2 必须在 validity_notes 里写理由"
                        "（F-47：3/4 是偏好不是要求）",
                        missing=["validity_notes"])

    bands = body.get("bands")
    if bands is not None and (not isinstance(bands, list)
                              or not all(isinstance(b, str) for b in bands)):
        raise DeclError("bands 必须是波段名字符串数组", missing=["bands"])

    return {
        "id": tid, "transient_id": body["transient_id"],
        "label": (body.get("label") or "").strip() or None,
        "object_class": body["object_class"].strip(),
        "redshift": {"value": z, "source": red["source"],
                     "error": z_err, "error_kind": z_kind,
                     "use_position": bool(red.get("use_position", True))},
        "distance": {"kind": dkind, "mu": mu, "d_L_Mpc": d_l,
                     "allow_low_z": allow_low_z,
                     "source": (dist.get("source") or "").strip()},
        "rowset": rowset, "null_system_policy": policy,
        "declarations": declarations, "bands": bands,
        "require_min_bands": rmb, "validity_notes": notes or None,
        "citations": (body.get("citations") or "").strip() or None,
    }


def build_manifest(decl: dict, *, transient: dict, band_map: dict) -> dict:
    """The §3.3 mapping as one dict.  `transient` = extract.fetch_transient
    output; `band_map` = {table label: registry id} actually used in the CSV."""
    tid = decl["transient_id"]
    red = decl["redshift"]
    z_source = red["source"]
    if transient.get("redshift_ref"):
        z_source = f"{z_source} ｜库侧出处（transients.redshift_ref）：" \
                   f"{transient['redshift_ref']}"          # F-15
    rs = {"value": red["value"], "frame": "heliocentric", "source": z_source}
    if red["error"] is not None:                            # F-16: never invented
        rs["error"], rs["error_kind"] = red["error"], red["error_kind"]
    if red["use_position"] and transient.get("ra") is not None \
            and transient.get("dec") is not None:           # F-17/M_POS
        rs["position"] = {
            "ra_deg": float(transient["ra"]),
            "dec_deg": float(transient["dec"]),
            "frame": "ICRS", "epoch": "J2000",
            "source": f"AJST 库 transients.ra/dec of {tid}（作者勾选使用；实测"
                      "后果 Δz≈1.0–1.4e-3、Δμ≤0.0026 mag，01 §E.4）"}

    dist = {"kind": decl["distance"]["kind"]}
    if decl["distance"]["mu"] is not None:
        dist["mu"] = decl["distance"]["mu"]
    if decl["distance"]["d_L_Mpc"] is not None:
        dist["d_L_Mpc"] = decl["distance"]["d_L_Mpc"]
    if decl["distance"]["allow_low_z"]:
        dist["allow_low_z"] = True
    dist["source"] = decl["distance"]["source"] or (
        "作者经造模板向导声明（U-22）；库 gext_distmod 只作对照不入此字段"
        if dist["kind"] != "cosmological" else
        "作者经造模板向导声明 cosmological；宇宙学为引擎默认 Planck18")

    validity = {"require_min_bands": decl["require_min_bands"]}
    note = decl.get("validity_notes") or (
        "require_min_bands=2（F-47：两条同时测到的带即定出幂律，幂律是非观测"
        "带的标准插值，2 是插值本身定下的地板）")
    validity["notes"] = note

    return {
        "schema": "chromashift-template-1",                 # M_SCHEMA
        "id": decl["id"],                                   # M_ID
        "label": decl["label"] or f"{tid}（AJST 库内行造模板）",
        "object_class": decl["object_class"],               # M_CLASS
        "redshift": rs,
        "distance": dist,                                   # M_DKIND/M_DVAL
        "photometry": {
            "file": f"{decl['id']}.csv",                    # M_FILE
            "layout": "long",                               # M_LAYOUT
            "time": {"column": "t_obs_s", "unit": "s",      # M_TIME
                     "frame": "observer",
                     "epoch_zero": {"kind": "fixed", "value": 0, "unit": "s",
                                    "source": f"库内 {tid} 的 t0 基准（=0；"
                                              "AJST time 列即相对 t0 的观测秒）"}},
            "value": {"column": "mag", "error_column": "mag_err",
                      "unit": "mag", "system_column": "mag_system"},  # M_VAL/M_SYS
            "band": {"column": "band_label", "map": band_map},        # M_BAND
            "flags": {"upper_limit_column": "upperlimit",
                      "upper_limit_true_values": ["true"],
                      "discard_column": "discard",
                      "discard_true_values": ["true"],
                      "require_error": False},              # M_FLAGS
        },
        "validity": validity,                               # M_RMB（M_MCT 不覆盖）
        "reddening": {                                      # M_RED / F-11 绑死
            "mw_removed": decl["rowset"] == "gext",
            "host_removed": "unknown",
            "source": ("rowset=gext ⇒ 表内值为 mag_gextcor（CSFD 银消已扣），"
                       "mw_removed=true；host 端：库内无宿主消光扣除记录 ⇒ "
                       "unknown" if decl["rowset"] == "gext" else
                       "rowset=raw ⇒ 未扣银消，mw_removed=false；host 端：库内"
                       "无宿主消光扣除记录 ⇒ unknown")},
        "stretch": {"supported": False,                     # M_STR
                    "note": "无该类 width-luminosity 律的已验证测量（引擎默认）"},
        "provenance": {"grade": "catalog-derived",          # M_PROV
                       "citations": ([f"AJST catalogue 光变表（transient "
                                     f"{tid}，rowset={decl['rowset']}）"]
                                    + ([decl["citations"]]
                                       if decl.get("citations") else []))},
    }


def write_manifest(manifest: dict, root) -> Path:
    """Serialise + engine strict self-check (M_SCHEMA): the file only stays on
    disk if `TemplateSpec.from_yaml` accepts the bytes just written."""
    cs = engine.require()
    root = Path(root)
    path = paths.template_yaml(root, manifest["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        tmp.write_text(yaml.safe_dump(manifest, allow_unicode=True,
                                      sort_keys=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    cs.TemplateSpec.from_yaml(path)  # strict self-check; raises E_Cfg on drift
    return path
