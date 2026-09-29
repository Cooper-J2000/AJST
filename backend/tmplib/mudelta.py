"""F-46: Δμ = μ_engine − μ_catalog per template, with attributable causes.

Two distance moduli coexist on every plot that overlays a template curve on a
catalogue source (POS-9): the engine's μ from the manifest's declared distance,
and the catalogue's `transients.gext_distmod` (= Planck18.distmod(redshift)
rounded to 0.001, models.py -- a cosmological cache, *not* an indicator
measurement, OUT-12).  Their difference is a first-class disclosure, not a
footnote: the shipped maximum is 0.2920 mag (sn2002ap), twice the engine's own
0.15 colour-term cap, which is exactly the CA-13 threshold (T-46: the threshold
is read off the engine, not invented here).

Causes are attributed in the design's fixed priority (F-46); the first match
wins, the rest are listed as secondary, and nothing unattributable is written
as zero.

ST-1: this module never opens a DB session.  The route layer fetches the
counterpart row on a short session and passes a plain dict in.
"""

from __future__ import annotations

#: Shipped template -> catalogue counterpart (measured, 01 §E.8/T-16).
#: at2018cow / at2025ulz deliberately absent: no catalogue counterpart (E-25).
#: sn1998bw's counterpart is GRB980425A (alias SN1998bw); sn2010bh's catalog
#: redshift (0.06) differs from the manifest's (0.0591) -- a T-21 z conflict.
COUNTERPARTS = {
    "at2017gfo": "GRB170817A",
    "sn2006aj": "GRB060218A",
    "sn2002ap": "SN2002ap",
    "ep250108a": "EP250108a",
    "ep260321a": "EP260321a",
    "sn2010bh": "GRB100316D",
    "sn1998bw": "GRB980425A",
}

#: CA-13 threshold == the engine's own colour-term cap (C_MU_DELTA_ALERT, T-46).
_ALERT_FALLBACK = 0.15


def alert_threshold() -> float:
    try:
        from chromashift.build import _MAX_COLOUR_TERM_MAG
        return float(_MAX_COLOUR_TERM_MAG)
    except Exception:
        return _ALERT_FALLBACK


def counterpart_of(template_id: str, entry: dict | None = None) -> str | None:
    """Shipped mapping first; a wizard-built (origin=catalog) template's
    counterpart is its own source transient (library.json entry.transient_id)."""
    cid = COUNTERPARTS.get(template_id)
    if cid:
        return cid
    return (entry or {}).get("transient_id")


def _planck18_distmod(z: float) -> float | None:
    """The catalogue's own recipe (models.py): Planck18.distmod(z)."""
    try:
        from astropy.cosmology import Planck18
        return float(Planck18.distmod(float(z)).value)
    except Exception:
        return None


def compute_mu(spec, catalog: dict | None, entry: dict | None = None) -> dict:
    """The §4.2 `mu` block for one template.

    `catalog` = {'id', 'redshift', 'gext_distmod'} of the counterpart, or None;
    `entry` = the library.json record (lets a wizard-built template resolve its
    own source transient as counterpart).  E-25 semantics: no (usable)
    counterpart -> `ok: False` with an explicit code; the UI must show
    "无对照", never 0 (E-25).
    """
    dist = spec.distance_obj()
    mu_engine = dist.distance_modulus
    tid = counterpart_of(spec.id, entry)
    base = {
        "engine": round(mu_engine, 4),
        "engine_distance": dist.describe(),
        "counterpart_id": tid,
    }
    if tid is None:
        return {**base, "ok": False, "code": "TL_NO_COUNTERPART",
                "message": "该模板在库内没有对应源，Δμ 无对照（不是 0）"}
    if catalog is None:
        return {**base, "ok": False, "code": "TL_NO_COUNTERPART",
                "message": f"库内对应源 {tid} 不存在，Δμ 无对照（不是 0）"}
    if catalog.get("lookup_error"):
        return {**base, "ok": False, "code": "TL_NO_COUNTERPART",
                "message": f"库查询暂不可用，未能与 {tid} 比对 Δμ（不是 0）"}
    mu_catalog = catalog.get("gext_distmod")
    if mu_catalog is None:
        # CA-12 family: the counterpart record exists but carries no usable mu.
        return {**base, "ok": False, "code": "TL_NO_COUNTERPART",
                "message": f"库内对应源 {tid} 缺 gext_distmod，Δμ 无对照（不是 0）"}
    mu_catalog = float(mu_catalog)
    delta = mu_engine - mu_catalog

    causes: list[dict] = []
    z_cmb = spec.z_cmb()
    has_pos = bool((spec.redshift or {}).get("position")) and z_cmb is not None
    # ① a non-cosmological declared distance vs the catalogue's distmod(z).
    if dist.kind != "cosmological":
        d_pc = 10.0 ** ((mu_catalog + 5.0) / 5.0)
        causes.append({"cause": "距离来源不同",
                       "detail": f"引擎 distance.kind={dist.kind}"
                                 f"（d_L={dist.d_L_Mpc:.2f} Mpc）vs 库 gext_distmod="
                                 f"Planck18.distmod(z)（反解 {d_pc / 1e6:.2f} Mpc）"})
    # ② the template's z and the catalogue's z differ.
    z_cat = catalog.get("redshift")
    if z_cat is not None and abs(float(z_cat) - spec.z) > 1e-6:  # C_SAME_Z_TOL
        d1, d2 = _planck18_distmod(spec.z), _planck18_distmod(float(z_cat))
        diff = None if (d1 is None or d2 is None) else round(d1 - d2, 4)
        causes.append({"cause": "z 值不同",
                       "detail": f"模板 z={spec.z:g} vs 库 z={float(z_cat):g}"
                                 f"（Planck18.distmod 差 {diff} mag）"})
    # ③+④: a cosmological distance folds the CMB frame term into one line
    # ("350 km/s + ③, 合成一行" -- F-46); ③ stands alone only for a
    # non-cosmological distance that still declares a line-of-sight position.
    if dist.kind == "cosmological":
        detail = "宇宙学距离口径（350 km/s 本征速度项"
        if has_pos:
            detail += f" + CMB 参考系，z_cmb={z_cmb:.7f}"
        causes.append({"cause": "口径项", "detail": detail + "）"})
    elif has_pos:
        causes.append({"cause": "参考系项",
                       "detail": f"CMB 参考系改正：z_cmb − z_helio = {z_cmb - spec.z:+.6f}"})

    primary = causes[0] if causes else {"cause": "未归因", "detail": ""}
    out = {
        **base,
        "ok": True,
        "catalog": mu_catalog,
        "catalog_source": "transients.gext_distmod",
        "catalog_redshift": z_cat,
        "delta": round(delta, 4),
        "cause": primary["cause"],
        "detail": primary["detail"],
        "secondary": causes[1:],
    }
    if abs(delta) > alert_threshold():
        out["alert"] = "CA-13"
    return out
