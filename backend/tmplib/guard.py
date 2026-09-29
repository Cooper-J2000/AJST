"""Three independent staleness axes for the template library (IA-13, API-13).

* **engine_inputs** -- the engine's 6-input fingerprint (manifest / photometry /
  filter_index / filter_curves / extinction / code), per template, via
  `registry.stale_reasons`.  Remedy: rebuild the surface (author's click, P2).
  `code_only` is broken out separately (ST-16): an engine code edit makes every
  shipped surface stale at once and the read path only refuses, so that case is
  an ops event the status bar must show on its own.
* **catalog_rows** -- the 7th fingerprint (F-52): does the CSV still equal the
  catalog rows it was frozen from?  P0 ships only the pure hashing entry point
  (`rows_sha256`); wiring it to the DB and filling `library.json` is P2.  Until
  then every template reports `untracked` -- never silently "ok".
* **filter_vendor** -- ST-15 / CA-01: the library's filter registry was vendored
  from AJST's `catadata/filters.json`; compare the live file's sha256 against
  the `source_sha256` recorded in `data/filters/index.json`.  Remedy is on the
  engine side (re-run the vendor script); this axis only reports.

Each axis computes in its own try/except: a broken axis degrades to an `error`
entry and must not take the other two down with it (IA-13, API-13 always 200).

ST-1: nothing here opens a DB session; `catalog_rows` in P0 reads only files.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import engine, paths

#: Shipped templates whose raw tables were transcribed from the AJST catalogue
#: itself (F-19): for these, rows_sha256 is a drift probe against the live
#: catalogue, not an integrity check.  P0 only records the classification.
CATALOG_DERIVED_IDS = ("at2017gfo", "ep250108a", "sn2002ap", "sn2006aj")


# ── axis 2 placeholder: the 7th fingerprint (F-52) ───────────────────────────

def rows_sha256(rows) -> str:
    """sha256 over the frozen row set, sorted by (time, band).

    `rows` yields `(band, time, mag, mag_err, mag_system, upperlimit, discard)`
    tuples; each becomes one `"band|time|mag|mag_err|mag_system|upperlimit|discard"`
    line.  Token recipe (D-5, finalised in P2 and shared byte-for-byte with the
    frozen CSV, extract.py `token`): None → empty, bool → `true`/`false`,
    float → `repr` (round-trip exact), everything else `str`.
    """
    def _tok(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float):
            return repr(v)
        return str(v)

    lines = sorted(
        "|".join(_tok(v) for v in row) for row in rows
    )
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


# ── axis 1: engine inputs ────────────────────────────────────────────────────

def engine_inputs_axis(root: str | Path | None = None) -> dict:
    base = Path(root) if root is not None else paths.library_root()
    avail = engine.available(base)
    if not avail["ok"]:
        return {"ok": None, "error": "engine-unavailable",
                "reasons": [r["code"] for r in avail["reasons"]]}
    cs = engine.require()
    from chromashift import registry
    from chromashift.errors import ChromaShiftError

    per: dict = {}
    for tid, yaml_path in sorted(cs.manifests(base).items()):
        try:
            spec = cs.TemplateSpec.from_yaml(yaml_path)  # ST-7: directed read
            npz = base / "data" / "surfaces" / f"{spec.id}.npz"
            reasons = registry.stale_reasons(npz, spec, base)
        except ChromaShiftError as exc:
            per[tid] = {"stale": True, "reasons": [f"manifest-error:{exc.code}"]}
            continue
        per[tid] = {"stale": bool(reasons), "reasons": reasons}
    changed = sorted({r for v in per.values() for r in v["reasons"]})
    code_only = sorted(t for t, v in per.items()
                       if v["stale"] and v["reasons"] == ["code"])
    return {
        "ok": all(not v["stale"] for v in per.values()),
        "templates": per,
        "changed_inputs": changed,
        # ST-16: "only the engine code changed" is a distinct ops event.
        "code_only_templates": code_only,
    }


# ── axis 2: catalogue rows (the 7th fingerprint, F-52) ───────────────────────

def catalog_rows_axis(root: str | Path | None = None,
                      rows_provider=None) -> dict:
    """Compare each recorded rows_sha256 against the live catalogue.

    `rows_provider(template_id, entry) -> rows | None` is supplied by the route
    layer (short session, closed before returning -- ST-1/ST-8); with no
    provider the axis reports `deferred`, never silently "ok" (F-52/P0 语义).
    Live verdicts: `ok` (wizard-built: CSV was frozen from these very rows),
    `probe-inconclusive` (catalog-derived drift probe: DB matches the stamp,
    but the shipped CSV predates the recipe, so F-19 says "与本库同源，差异
    待核", never "一致"), `drifted` (CA-11: live rows no longer hash equal),
    `unverifiable` (provider could not produce rows).
    """
    lib = paths.read_library(root)
    if lib is None:
        return {"ok": None, "error": "no-library-json",
                "note": "library.json 尚未初始化"}
    per: dict = {}
    for tid, entry in sorted((lib.get("templates") or {}).items()):
        recorded = entry.get("rows_sha256")
        probe = entry.get("origin") == "catalog-derived"
        status = "untracked"
        if recorded is not None:
            status = "deferred"
            if rows_provider is not None and entry.get("extract"):
                try:
                    live = rows_provider(tid, entry)
                except Exception:
                    live = None
                    status = "unverifiable"
                else:
                    if live is None:
                        status = "unverifiable"
                    elif live == recorded:
                        status = "probe-inconclusive" if probe else "ok"
                    else:
                        status = "drifted"
        per[tid] = {"status": status, "rows_sha256": recorded,
                    "drift_probe": probe}
        if probe and status in ("probe-inconclusive", "drifted"):
            per[tid]["note"] = ("与本库同源，差异待核" if status != "drifted"
                                else "库内行相对建面/登记时已漂移（CA-11）")
    ok = all(v["status"] not in ("drifted",) for v in per.values())
    return {"ok": ok, "templates": per}


# ── axis 3: filter vendor drift (ST-15 / CA-01) ─────────────────────────────

def _catadata_filters(data_dir: str | Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / "filters.json"
    env = os.environ.get("AJST_DATA_DIR")
    if env:
        return Path(env) / "filters.json"
    # default root is <repo>/catadata/tmplibrary -> catadata is its parent
    return paths.default_root().parent / "filters.json"


def vendor_axis(root: str | Path | None = None,
                data_dir: str | Path | None = None) -> dict:
    base = Path(root) if root is not None else paths.library_root()
    index = base / "data" / "filters" / "index.json"
    recorded = None
    if index.is_file():
        try:
            recorded = (json.loads(index.read_text(encoding="utf-8"))
                        .get("provenance", {}) or {}).get("source_sha256")
        except (ValueError, OSError):
            return {"ok": None, "error": "filter-index-unreadable"}
    else:
        return {"ok": None, "error": "filter-index-missing"}

    target = _catadata_filters(data_dir)
    current = None
    if target.is_file():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
    ok = recorded is not None and current is not None and recorded == current
    out = {
        "ok": ok,
        "recorded_source_sha256": recorded,
        "current_sha256": current,  # None = 现文件缺失
    }
    if not ok and recorded is not None:
        out["code"] = "CA-01"
        # TXT-10 的要点：引擎按快照算，处置在引擎侧，本功能无权改引擎数据。
        out["message"] = (
            "引擎的通带快照取自本库的 filters.json（记录 sha 与现文件不一致）。"
            "K 改正按引擎快照计算，可能与当前滤光片页数据不符；需在引擎侧重跑"
            " vendor 脚本，本功能无权改引擎数据。")
    return out


# ── aggregate (API-13) ───────────────────────────────────────────────────────

def report(root: str | Path | None = None,
           data_dir: str | Path | None = None) -> dict:
    """The three axes, each computed behind its own guard (IA-13)."""
    axes = {}
    for name, fn in (("engine_inputs", lambda: engine_inputs_axis(root)),
                     ("catalog_rows", lambda: catalog_rows_axis(root)),
                     ("filter_vendor", lambda: vendor_axis(root, data_dir))):
        try:
            axes[name] = fn()
        except Exception as exc:  # a broken axis must not sink the other two
            axes[name] = {"ok": None, "error": f"{type(exc).__name__}: {exc}"}
    oks = [a.get("ok") for a in axes.values()]
    return {"ok": all(o is True for o in oks), "axes": axes}
