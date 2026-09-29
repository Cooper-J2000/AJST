#!/usr/bin/env python
"""Export the filter registry for ChromaShift template libraries, from catadata/filters.json.

ChromaShift (the pure-library successor of the snredshift engine, editable-installed
from ../Astro_Software/ChromaShift) does not ship a filter registry: the caller
supplies one.  AJST is the source of record (`catadata/filters.json`, 81 records, a
subset carrying a peak-normalised response curve), and this script renders it into
the layout ChromaShift's `FilterBank.load()` reads:

    <out>/index.json       registry + provenance (source sha256 recorded for the
                           tmplib vendor guard, CA-01)
    <out>/curves/*.json    one {"name", "wl_A", "T"} file per band with a curve

The output is an input to every built surface: changing it makes the surfaces in
`backend/tmplibrary/data/surfaces/` stale, and they must be rebuilt (from the
tmplib page or `chromashift build --root backend/tmplibrary`).

Rules, both earned in the engine's original `vendor_filters.py`:

* **Merging is the default.**  A run limited with `--bands` must not silently drop
  the other records from the index -- pass `--replace` to mean it.
* **A record that cannot be read is reported, not written.**  A curve with 3 samples
  or a negative transmission yields a pivot wavelength from an integral nobody
  meant to take; refuse here, where the source record is in front of the user.

Usage:
    python scripts/export_filters_for_chromashift.py                    # merge all
    python scripts/export_filters_for_chromashift.py --bands R,r        # merge two
    python scripts/export_filters_for_chromashift.py --replace          # source only
    python scripts/export_filters_for_chromashift.py --out /tmp/x       # other dest
    python scripts/export_filters_for_chromashift.py --dry-run          # no writes
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = REPO_ROOT / "catadata" / "filters.json"
OUT_DIR = REPO_ROOT / "backend" / "tmplibrary" / "data" / "filters"

# Which wavelength the record's single number refers to.  The source file's
# `type` field is trusted only as a hint; this script recomputes pivot and
# effective wavelengths from the curve itself whenever a curve exists.
TYPE_HINTS = {"ref", "eff", "mean", "guess"}

# A curve below this many samples is a straight line between two points, which is a
# guess about the passband rather than a measurement of it.  The sparsest shipped
# curve (Johnson `B`) has 11, so this refuses upstream truncations, not real data.
MIN_CURVE_POINTS = 8
# A "passband" narrower than this in the table cannot carry a band integral: the
# quadrature nodes and the declared spectral coverage would both be set by whatever
# rounding the source used.
MIN_CURVE_SPAN_A = 100.0
# Outside these, the number is a frequency, a micrometre value, or a mistake.  The
# upper bound has to clear Spitzer-MIPS.160mu (1.53e6 A) and the lower one has to
# leave the soft X-ray records alone, so this catches a unit swap rather than
# adjudicating what counts as a filter.
WAVELENGTH_RANGE_A = (90.0, 1.0e7)
TRANSMISSION_TOL = 1e-6

NOTE = ("Response curves are peak-normalised (max T = 1).  Magnitudes here "
        "are always formed as ratios against a zero point computed with the "
        "same curve, so the normalisation cancels.  Records with n_points=0 "
        "carry only a nominal wavelength and can only support the "
        "monochromatic (delta-band) mode.")


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "unnamed"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def monotonise(wl, tr):
    pts = sorted(zip(map(float, wl), map(float, tr)))
    # drop duplicate abscissae, keeping the last value
    out_x: list[float] = []
    out_y: list[float] = []
    for x, y in pts:
        if out_x and abs(x - out_x[-1]) < 1e-9:
            out_y[-1] = y
            continue
        out_x.append(x)
        out_y.append(y)
    return out_x, out_y


def read_curve(name: str, extra: dict) -> tuple[list[float], list[float]] | None | str:
    """The record's transmission table, `None` if the source has none to give, or the
    reason a table it *does* give cannot be exported as a curve.

    The three-way return is the point: 52 of the 81 source records legitimately carry
    only a nominal wavelength, and those are still worth exporting for the delta-band
    mode -- saying so 52 times would only train the reader to skip the line that
    matters.  A *malformed* table is different, and it is reported.
    """
    table = (extra.get("transmission") or {})
    wl, tr = table.get("wl"), table.get("tr")
    if wl is None and tr is None:
        return None
    if not isinstance(wl, list) or not isinstance(tr, list):
        return "transmission table is not a pair of lists"
    if len(wl) != len(tr):
        return f"transmission table has {len(wl)} wavelengths and {len(tr)} values"
    if len(wl) < MIN_CURVE_POINTS:
        return f"only {len(wl)} samples (this script exports curves from {MIN_CURVE_POINTS}+)"
    pairs = [(x, y) for x, y in zip(wl, tr)]
    if any(_float(x) is None or _float(y) is None for x, y in pairs):
        return "transmission table carries a non-numeric or non-finite entry"
    xs, ys = monotonise([float(x) for x, _ in pairs], [float(y) for _, y in pairs])
    if len(xs) < MIN_CURVE_POINTS:
        return f"only {len(xs)} distinct wavelengths after de-duplication"
    if xs[-1] - xs[0] < MIN_CURVE_SPAN_A:
        return (f"curve spans {xs[-1] - xs[0]:.1f} A, below the "
                f"{MIN_CURVE_SPAN_A:.0f} A needed for a band integral")
    if min(ys) < -TRANSMISSION_TOL:
        return f"transmission is negative (min {min(ys):g})"
    if max(ys) > 1.0 + TRANSMISSION_TOL:
        return (f"transmission exceeds unity (max {max(ys):g}); the registry's "
                "peak-normalisation convention would be silently broken")
    if max(ys) <= 0.0:
        return "transmission is zero at every sample, so the band has no passband"
    return xs, ys


def check_record(name: str, entry: dict) -> tuple[dict, list[str]]:
    """One index record plus the list of things worth shouting about."""
    extra = entry.get("extra_data") or {}
    rec: dict = {
        "name": name,
        "registry_type": entry.get("type"),
        "nominal_wavelength_A": entry.get("wavelength"),
        "nominal_wavelength_kind": (entry.get("type")
                                    if entry.get("type") in TYPE_HINTS else None),
        "vega_to_ab": entry.get("Vega2AB"),
        "description": entry.get("description"),
        "svo_id": extra.get("svo_id"),
        "pcigale_name": extra.get("pcigale_name"),
        "curve_file": None,
        "n_points": 0,
    }
    problems: list[str] = []

    lam = _float(entry.get("wavelength"))
    if lam is None:
        problems.append("no nominal wavelength")
    elif not WAVELENGTH_RANGE_A[0] <= lam <= WAVELENGTH_RANGE_A[1]:
        problems.append(f"nominal wavelength {lam:g} A is outside "
                        f"{WAVELENGTH_RANGE_A[0]:g}-{WAVELENGTH_RANGE_A[1]:g} A; "
                        "is the source record in nm or in Hz?")
    if _float(entry.get("Vega2AB")) is None:
        problems.append("no Vega->AB offset; a build that needs this band in AB "
                        "will stop here, which is the answer you want")

    curve = read_curve(name, extra)
    if isinstance(curve, str):
        problems.append(f"refused as a response curve, exported as mono: {curve}")
    elif curve is not None:
        xs, ys = curve
        fname = f"{slug(name)}.json"
        rec["curve_file"] = f"curves/{fname}"
        rec["n_points"] = len(xs)
        rec["_curve"] = {"name": name, "wl_A": xs, "T": ys}
    return rec, problems


def _write(path: Path, text: str, dry_run: bool) -> bool:
    """Write only when the bytes would change; return whether they did."""
    if path.is_file() and path.read_text() == text:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--bands", default="", help="comma-separated subset; default: all")
    ap.add_argument("--replace", action="store_true",
                    help="write an index containing only the source's records, "
                         "dropping every exported band the source no longer names")
    ap.add_argument("--out", default=str(OUT_DIR), help="registry directory to write")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_file():
        raise SystemExit(f"filter registry not found: {src}")
    raw = json.loads(src.read_text())
    if not isinstance(raw, dict) or not raw:
        raise SystemExit(f"{src}: expected a non-empty JSON object of records")
    wanted = [b.strip() for b in args.bands.split(",") if b.strip()] or sorted(raw)

    out_dir = Path(args.out)
    index_path = out_dir / "index.json"
    previous = json.loads(index_path.read_text()) if index_path.is_file() else {}
    prev_filters: dict = dict((previous.get("filters") or {}))
    prev_prov: dict = dict(previous.get("provenance") or {})

    index: dict[str, dict] = {} if args.replace else dict(prev_filters)
    n_dropped = 0
    for name in wanted:
        if name not in raw:
            if args.replace and name in prev_filters:
                fate = "dropped: --replace means the source is the whole registry"
            elif name in prev_filters:
                fate = "kept from the exported index (merge is the default)"
            else:
                fate = "skipped: nothing to export and nothing exported"
            print(f"  ! {name}: absent from the source registry, {fate}")
            if args.replace:
                n_dropped += int(index.pop(name, None) is not None)
            continue
        rec, problems = check_record(name, raw[name])
        curve = rec.pop("_curve", None)
        if curve is not None:
            body = json.dumps(curve, separators=(",", ":"))
            if _write(out_dir / "curves" / f"{slug(name)}.json", body, args.dry_run):
                print(f"  ~ {name}: {'would write' if args.dry_run else 'wrote'} "
                      f"{rec['n_points']}-point curve")
        index[name] = rec
        for problem in problems:
            print(f"  ! {name}: {problem}")

    payload = {
        "vendored_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source_path": str(src),
        "source_sha256": sha256(src),
        "source_read_only": True,
        "note": NOTE,
        "n_records": len(index),
        "n_with_curve": sum(1 for v in index.values() if v["n_points"]),
    }
    # `mode` is deliberately *not* recorded: it describes this run, not the registry,
    # and a key that flips between merge and replace would make every other re-run
    # look like a content change and invalidate the built surfaces for nothing.
    if prev_prov and prev_prov.get("source_sha256") != payload["source_sha256"]:
        print(f"  * the source registry changed since the last export "
              f"({str(prev_prov['source_sha256'])[:8]}... -> "
              f"{payload['source_sha256'][:8]}...)")

    # Compare without `vendored_utc`: a re-run that changes only the timestamp would
    # otherwise invalidate every built surface, whose staleness hash covers this
    # file, for no difference in a single band integral.
    stable = lambda p: {k: v for k, v in p.items() if k != "vendored_utc"}  # noqa: E731
    changed = not prev_filters or stable(prev_prov) != stable(payload)
    if changed:
        _write(index_path, json.dumps({"provenance": payload, "filters": index},
                                      indent=1), args.dry_run)

    vanished = sorted(set(prev_filters) - set(index))
    orphaned = sorted(str(p.relative_to(out_dir)) for p in (out_dir / "curves").glob("*.json")
                      if p.stem not in {slug(k) for k, v in index.items() if v["n_points"]})
    for name in sorted(set(index) - set(prev_filters)):
        print(f"  + {name}: newly exported band")
    for name in vanished:
        print(f"  - {name}: no longer in the index")
    for name in orphaned:
        print(f"  ? {name}: curve file on disk that the index no longer references "
              "(left in place; delete it only once you have read it back)")

    print(f"{'would export' if args.dry_run else 'exported'} {payload['n_records']} "
          f"records, {payload['n_with_curve']} with curves and "
          f"{payload['n_records'] - payload['n_with_curve']} nominal-wavelength-only"
          f" -> {out_dir}"
          + (f" (index unchanged; {n_dropped} band(s) dropped)" if n_dropped else ""))
    if changed and not args.dry_run:
        print("note: data/filters is an input to every surface, so this rebuilds "
              "them: rebuild the tmplibrary surfaces and expect the guard to report "
              "them as stale until you do.")
    elif not changed:
        print("note: nothing changed, so the built surfaces stay fresh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
