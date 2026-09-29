"""DB → qualified rows for the template wizard (design doc 02 §3.2, F-10..F-14).

Pipeline, all pure after the single fetch (ST-1: no session is held past
`fetch_rows`; ST-8: one projection query, never N+1):

1. `fetch_rows` pulls the full projection for one transient in a single query
   (the largest source, GRB060218A, is 5373 rows -- §8.3 budgets API-4 at
   900 ms for exactly this shape).
2. `build_rowset` applies, in order, each row counting exactly once:
   nonmag (F-14: Jy/mJy rows -- the 10 keV / GHz class -- are intercepted at
   fetch level, never reach the CSV) → band not in the engine registry
   (F-08 strict same-name, no lower() folding: Cousins R ≠ SDSS r) → band
   deselected by the author → gext rowset value missing (F-11: dropped, never
   falls back to the raw magnitude -- the 16 live counter-example rows make
   this a measured rule, not hygiene) → NULL mag_system under the chosen
   F-10 policy → upperlimit/discard (F-12: written into the CSV with truthy
   tokens so the engine's clean_rows does the accounting; counted under
   rejected so `kept` is the number that can enter the fit).
3. The CSV (§4.3 long layout) and the 7th fingerprint (F-52) share ONE token
   recipe (D-5 finalised here): floats `repr()`, bools `true`/`false`,
   None → empty.  `rows_hash` therefore hashes exactly what `write_csv`
   writes, and a live recompute from the same recipe is comparable.

`row_ledger` follows §4.1 key-for-key (total / kept / rejected{upperlimit,
discard, mag_system_missing, nonmag, gext_missing} plus the two added reasons
unmapped_band / band_deselected).  Invariant:
total == kept + sum(rejected.values()).
"""

from __future__ import annotations

import re

from . import guard  # rows_sha256 lives with the other fingerprint code (P0)

#: §4.3 CSV column order (long layout).  The header is part of the frozen
#: artefact; changing it is a schema change, not a tweak.
CSV_COLUMNS = ("band_label", "t_obs_s", "mag", "mag_err",
               "mag_system", "upperlimit", "discard")

#: lightcurves.mag_system holds exactly these two non-NULL values; mapping them
#: to the engine's closed vocabulary is a fixed table, not a guess (F-10).
SYSTEM_MAP = {"AB": "ab", "Vega": "vega"}

#: F-10: the four dispositions for rows whose photometric system is NULL.
NULL_POLICIES = ("drop", "declare", "drop-band", "drop-source")

#: F-11: the two row sets; `gext` is bound 1:1 to reddening.mw_removed=true.
ROWSETS = ("raw", "gext")

#: flux_density_unit value that marks a magnitude row; anything else (Jy/mJy
#: for the 10 keV / GHz class) is intercepted here (F-14 / OUT-5).
MAG_UNIT = "magnitude"

#: CA-12: same-event candidates share a 6-digit date core (yymmdd) in the id
#: or aliases -- GRB260321A ↔ EP260321a are not alias-linked in the DB, the
#: date token is the only machine handle (01 §E.8).
_DATE_TOKEN = re.compile(r"\d{6}")


class SourceRejected(Exception):
    """null_system_policy='drop-source' fired: the whole source is void (F-10)."""

    def __init__(self, n_missing: int):
        super().__init__(f"{n_missing} 行缺测光系统")
        self.n_missing = n_missing


# ── the one query ────────────────────────────────────────────────────────────

_LC_COLUMNS = ("band", "time", "flux_density", "flux_density_err",
               "flux_density_unit", "mag_system", "gext_corr",
               "mag_gextcor", "mag_gextcor_err", "upperlimit", "discard")


def fetch_rows(sess, transient_id: str) -> list[dict]:
    """All lightcurve rows of one transient, one projection query (ST-8).

    Returns plain dicts so the session can close before any engine work (ST-1).
    """
    from models import Lightcurve
    q = sess.query(*(getattr(Lightcurve, c) for c in _LC_COLUMNS)) \
            .filter(Lightcurve.transient_id == transient_id)
    return [dict(zip(_LC_COLUMNS, row)) for row in q.all()]


def fetch_transient(sess, transient_id: str) -> dict | None:
    """The transient row as a plain dict (preview/manifest/mu all read it)."""
    from models import Transient
    t = sess.get(Transient, transient_id)
    if t is None:
        return None
    return {"id": t.id, "ra": t.ra, "dec": t.dec, "t0": t.t0,
            "redshift": t.redshift, "redshift_type": t.redshift_type,
            "redshift_ref": t.redshift_ref, "gext_distmod": t.gext_distmod,
            "aliases": list(t.aliases or [])}


def fetch_transient_summaries(sess) -> list[dict]:
    """id/aliases/redshift/gext_distmod for every transient (CA-12 scan).

    One aggregate query over a 2.8k-row table; filtering happens in memory.
    """
    from models import Transient
    return [{"id": t.id, "aliases": list(t.aliases or []),
             "redshift": t.redshift, "gext_distmod": t.gext_distmod}
            for t in sess.query(Transient.id, Transient.aliases,
                                Transient.redshift,
                                Transient.gext_distmod).all()]


# ── tokens (D-5 finalised; shared by CSV bytes and the 7th fingerprint) ──────

def token(v) -> str:
    """One CSV/hash token: floats round-trip via repr, bools lowercase,
    None is empty.  guard.rows_sha256 tokenises identically -- keep in sync."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def row_tokens(row) -> str:
    return "|".join(token(v) for v in row)


def rows_hash(csv_rows) -> str:
    """F-52 第 7 项指纹 over the frozen row set (delegates to guard)."""
    return guard.rows_sha256(csv_rows)


# ── the row set ──────────────────────────────────────────────────────────────

def build_rowset(rows: list[dict], *, rowset: str,
                 null_system_policy: str = "drop",
                 declarations: dict | None = None,
                 bands: list[str] | None = None,
                 registry_ids: set[str] | frozenset = frozenset()):
    """Apply F-08/F-10/F-11/F-14 to fetched rows.

    `declarations`: {band: engine-system} for policy 'declare' (author-declared
    system for that band's NULL-system rows).  `bands`: author-selected band
    subset (None = every registry-mapped band).  Returns
    (csv_rows, ledger, band_table); raises SourceRejected for 'drop-source'.

    Rejection priority (each row counted once): nonmag → unmapped_band →
    band_deselected → gext_missing → mag_system_missing → upperlimit/discard
    (these last two are *written* to the CSV, F-12, but counted as rejected
    because the engine's clean_rows keeps them out of the fit).
    """
    if rowset not in ROWSETS:
        raise ValueError(f"rowset 必须是 {ROWSETS}，得到 {rowset!r}")
    if null_system_policy not in NULL_POLICIES:
        raise ValueError(f"null_system_policy 必须是 {NULL_POLICIES}，"
                         f"得到 {null_system_policy!r}")
    declarations = declarations or {}
    selected = None if bands is None else set(bands)

    # drop-band / drop-source need the tainted-band set up front.
    null_bands = {r["band"] for r in rows
                  if r["flux_density_unit"] == MAG_UNIT
                  and rowset == "raw" and r["mag_system"] is None}
    if null_system_policy == "drop-source" and null_bands:
        raise SourceRejected(sum(1 for r in rows
                                 if r["flux_density_unit"] == MAG_UNIT
                                 and r["mag_system"] is None))
    tainted = null_bands if null_system_policy == "drop-band" else set()

    rejected = {"nonmag": 0, "unmapped_band": 0, "band_deselected": 0,
                "gext_missing": 0, "mag_system_missing": 0, "missing_value": 0,
                "upperlimit": 0, "discard": 0}
    band_table: dict = {}
    csv_rows: list[tuple] = []

    def bt(band):
        return band_table.setdefault(band, {
            "rows": 0, "nonmag": 0,
            "systems": {"ab": 0, "vega": 0, "null": 0},
            "in_registry": band in registry_ids,
            "upperlimit": 0, "discard": 0,
            "gext_with_value": 0, "gext_missing": 0})

    for r in rows:
        band = r["band"]
        b = bt(band)
        b["rows"] += 1
        if r["flux_density_unit"] != MAG_UNIT:
            b["nonmag"] += 1
        sys_tok = SYSTEM_MAP.get(r["mag_system"]) if r["mag_system"] else None
        b["systems"][sys_tok or "null"] += 1
        if r["upperlimit"]:
            b["upperlimit"] += 1
        if r["discard"]:
            b["discard"] += 1
        if r["mag_gextcor"] is not None:
            b["gext_with_value"] += 1
        elif r["gext_corr"]:
            b["gext_missing"] += 1

        if r["flux_density_unit"] != MAG_UNIT:               # F-14
            rejected["nonmag"] += 1
            continue
        if band not in registry_ids:                         # F-08 strict
            rejected["unmapped_band"] += 1
            continue
        if selected is not None and band not in selected:
            rejected["band_deselected"] += 1
            continue

        if rowset == "gext":                                 # F-11
            mag, err, system = r["mag_gextcor"], r["mag_gextcor_err"], "ab"
            if mag is None:
                rejected["gext_missing"] += 1                # 不回落
                continue
        else:
            mag, err = r["flux_density"], r["flux_density_err"]
            system = sys_tok
            if system is None:                               # F-10
                if null_system_policy == "declare" and band in declarations:
                    system = declarations[band]
                else:
                    rejected["mag_system_missing"] += 1
                    continue
            if band in tainted:                              # drop-band
                rejected["mag_system_missing"] += 1
                continue
        if mag is None or r["time"] is None:
            # 量级行却缺值/缺时刻：无法进面，单列账目（实测当前库为 0，兜底用）
            rejected["missing_value"] += 1
            continue

        csv_rows.append((band, float(r["time"]), float(mag),
                         None if err is None else float(err),
                         system, bool(r["upperlimit"]), bool(r["discard"])))
        if r["upperlimit"]:
            rejected["upperlimit"] += 1
        elif r["discard"]:
            rejected["discard"] += 1

    kept = len(csv_rows) - rejected["upperlimit"] - rejected["discard"]
    ledger = {"total": len(rows), "kept": kept,
              "csv_rows": len(csv_rows), "rejected": rejected}
    return csv_rows, ledger, band_table


def write_csv(csv_rows, path) -> None:
    """§4.3 frozen snapshot; tokens are exactly row_tokens() (D-5)."""
    import csv as _csv
    import os
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(CSV_COLUMNS)
            for row in sorted(csv_rows, key=row_tokens):
                w.writerow([token(v) for v in row])
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def csv_sha256(path) -> str:
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ── preview helpers (API-4) ──────────────────────────────────────────────────

def same_event_candidates(transient: dict, all_records: list[dict]) -> list[dict]:
    """CA-12: records sharing a 6-digit date token with this transient."""
    tokens = set(_DATE_TOKEN.findall(transient["id"]))
    for a in transient.get("aliases") or []:
        tokens.update(_DATE_TOKEN.findall(str(a)))
    if not tokens:
        return []
    out = []
    for rec in all_records:
        if rec["id"] == transient["id"]:
            continue
        hay = {rec["id"], *(str(a) for a in rec.get("aliases") or [])}
        if tokens & set(t for h in hay for t in _DATE_TOKEN.findall(h)):
            out.append({"id": rec["id"], "redshift": rec["redshift"],
                        "gext_distmod": rec["gext_distmod"]})
    return out


def ca12_alert(transient: dict, candidates: list[dict]) -> dict | None:
    """Fire when the same event has >1 record and any of them (self included)
    lacks redshift or gext_distmod -- the empty-shell trap (CA-12)."""
    if not candidates:
        return None
    group = [{"id": transient["id"], "redshift": transient["redshift"],
              "gext_distmod": transient["gext_distmod"]}, *candidates]
    weak = [g["id"] for g in group
            if g["redshift"] is None or g["gext_distmod"] is None]
    if not weak:
        return None
    return {"code": "CA-12",
            "message": "同一事件在库里有多条记录，其中 " + "、".join(weak)
                       + " 缺 redshift 或 gext_distmod；别挑到空壳那条。",
            "records": group}


def estimate_indomain(ledger: dict, band_table: dict, bank) -> dict:
    """U-19 pre-build estimate: magnitude rows that survive extraction AND sit
    on a band whose registry record has a measured curve (trust curve/curve+svo).
    预估口径到此为止；真实值在建面后用逐点 predict 算（F-45 两段式）。"""
    on_curve = 0
    for band, b in band_table.items():
        if not b["in_registry"]:
            continue
        try:
            trust = bank.get(band).trust
        except Exception:
            trust = None
        if trust in ("curve+svo", "curve"):
            # 该带可进面行数 = 星等行 − 默认(raw+drop)会被剔的 NULL 系统行
            on_curve += b["rows"] - b["nonmag"] - b["systems"]["null"]
    mag_rows = ledger["total"] - ledger["rejected"]["nonmag"]
    return {"magnitude_rows": mag_rows,
            "on_curve_band_rows": on_curve,
            "estimated_answerable": on_curve,
            "estimated_pct": round(100.0 * on_curve / mag_rows, 1)
            if mag_rows else None,
            "note": "预估 = 落在有曲线波段上的可入行数；真实可答率建面后实测（F-45）"}
