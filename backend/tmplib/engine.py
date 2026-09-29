"""Thin in-process wrapper over the `chromashift` engine (S0, design doc 02 §6.1).

In-process is the only backend (`SubprocessCli` was cut by §6.4 and is not
missed at these latencies).  Rules this module enforces:

* ST-1: nothing here touches a DB session or imports anything that could --
  callers must finish their reads before calling in.
* ST-7: manifests are read with `TemplateSpec.from_yaml(<path>)`, never through
  `registry.manifest()`'s full rescan on a hot path.
* ST-14: `available()` / `deps()` are the only availability probes; nobody else
  may `try: import chromashift`.
* High-frequency reads (`load_surface`, `bank`) are memoised with the artefact's
  own sha256 as the invalidation key, because the engine re-hashes every input
  on each load (registry.py:103-132).  The cache key covers the artefact files
  only -- a manifest/code/filter edit leaves the npz untouched, so staleness
  remains the guard axis's job (guard.py), and any predict path must check the
  guard before using a cached surface.
"""

from __future__ import annotations

import hashlib
import threading
from importlib import import_module
from pathlib import Path

from . import paths

#: Engine dependency floors, mirrored from ChromaShift's pyproject.toml so that
#: `deps()` can answer "is this interpreter good enough" without importing the
#: engine (ST-14).  Keep in sync when the engine raises a floor.
DEP_FLOORS = {
    "numpy": "2.4",
    "scipy": "1.17",
    "astropy": "7.2",
    "dust_extinction": "1.7",
    "pandas": "2.3",
    "yaml": "6.0",  # PyYAML
}

#: Limits the host adds on top of the engine (§7 constant table).  Engine-owned
#: values (low-z floor, colour-term cap) are read from the engine at call time
#: by `limits()`; these are host policy and live nowhere else.
HOST_LIMITS = {
    "max_curves": 8,      # C_MAX_CURVES
    "max_sources": 8,     # C_MAX_SOURCES
    "max_points": 512,    # C_MAX_POINTS (requested epoch list)
    "points_max": 1200,   # C_POINTS_MAX (server-side n_times clamp)
    "max_draws": 200,     # C_MAX_DRAWS
}


class EngineUnavailable(Exception):
    """Raised by `require()`; `.reasons` is a list of machine-readable codes."""

    def __init__(self, reasons: list[dict]):
        super().__init__("; ".join(r["code"] for r in reasons))
        self.reasons = reasons


def _import():
    """Import chromashift, or None.  Internal -- the public probe is available()."""
    try:
        return import_module("chromashift")
    except Exception:
        return None


def _version_tuple(text: str) -> tuple[int, ...]:
    out = []
    for tok in str(text).split("."):
        digits = "".join(c for c in tok if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def deps() -> dict:
    """Installed vs required for each engine dependency (API-1, ST-14)."""
    out = {}
    for mod, floor in DEP_FLOORS.items():
        try:
            m = import_module(mod)
            installed = getattr(m, "__version__", None)
        except Exception:
            installed = None
        ok = installed is not None and _version_tuple(installed) >= _version_tuple(floor)
        out[mod] = {"installed": installed, "minimum": floor, "ok": ok}
    return out


def available(root: str | Path | None = None) -> dict:
    """Can the engine be used at this library root?  Reasons are machine-readable."""
    reasons: list[dict] = []
    if _import() is None:
        reasons.append({"code": "engine-import-failed",
                        "detail": "chromashift 无法导入当前解释器"})
    base = Path(root) if root is not None else paths.library_root()
    if not base.is_dir():
        reasons.append({"code": "root-missing",
                        "detail": "模板库根目录不存在（env AJST_TMPLIB_DIR 可覆盖）"})
    else:
        for rel in ("templates", "data/raw", "data/surfaces", "data/filters/curves",
                    "data/filters/index.json"):
            if not (base / rel).exists():
                reasons.append({"code": f"root-incomplete:{rel}",
                                "detail": f"模板库缺少 {rel}"})
    dep_bad = [k for k, v in deps().items() if not v["ok"]]
    if dep_bad:
        reasons.append({"code": "deps-below-floor",
                        "detail": "依赖低于引擎下限: " + ", ".join(dep_bad)})
    return {"ok": not reasons, "reasons": reasons}


def require():
    """The chromashift module, or EngineUnavailable with machine-readable reasons."""
    cs = _import()
    if cs is None:
        raise EngineUnavailable([{"code": "engine-import-failed",
                                  "detail": "chromashift 无法导入当前解释器"}])
    return cs


def version() -> str | None:
    cs = _import()
    return getattr(cs, "__version__", None) if cs else None


def code_sha256() -> str:
    """The `code` entry of the engine's 6-input fingerprint (C_ENGINE_PIN).

    Replicates registry.py's `_hash_tree` over the engine package's own *.py
    files (registry.py:129-135, 179): files sorted by name, digest fed
    `name` then the file's hex sha256.  Duplicated rather than imported because
    `_hash_tree` is private; the acceptance test pins equality against
    `registry.input_hashes(...)["code"]` so a drift in the engine's recipe
    fails loudly instead of silently pinning the wrong thing.
    """
    cs = require()
    pkg = Path(cs.__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(pkg.glob("*.py"), key=lambda q: q.name):
        h.update(p.name.encode())
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
    return h.hexdigest()


def engine_enums() -> dict:
    """Closed vocabularies the API contract exposes (API-1).

    Read from the engine when importable; the fallbacks are the values frozen in
    design doc 02 §5 (engine 1.0.0) and are marked as such so a drift between
    fallback and engine is visible in the response rather than silent.
    """
    fallback = {
        "known_systems": ["ab", "sdss", "ps1", "ztfg", "ztfr", "ztfi", "panstarrs",
                          "galex", "vega", "johnson", "cousins", "johnson-cousins",
                          "st"],
        "z_error_kinds": ["line-precision", "line-scatter", "unestablished"],
        "distance_kinds": ["cosmological", "measured", "measured-secondary",
                           "redshift-velocity-field"],
        "source": "hardcoded-fallback(engine-1.0.0)",
    }
    try:
        from chromashift.systems import KNOWN_SYSTEMS
        from chromashift.zerror import Z_ERROR_KINDS
        from chromashift.distance import NON_COSMOLOGICAL_KINDS
    except Exception:
        return fallback
    systems = list(dict.fromkeys(KNOWN_SYSTEMS))  # engine list repeats 'ab'
    return {
        "known_systems": systems,
        "z_error_kinds": list(Z_ERROR_KINDS),
        "distance_kinds": ["cosmological", *NON_COSMOLOGICAL_KINDS],
        "source": "engine",
    }


def limits() -> dict:
    """HOST_LIMITS plus the engine-owned thresholds, read live when possible."""
    out = dict(HOST_LIMITS)
    out["low_z"] = 0.02                    # C_LOWZ fallback
    out["max_colour_term_mag"] = 0.15      # C_MAX_COLOUR_TERM fallback
    out["mu_delta_alert"] = 0.15           # C_MU_DELTA_ALERT == colour-term cap (T-46)
    out["source"] = {"low_z": "hardcoded-fallback",
                     "max_colour_term_mag": "hardcoded-fallback"}
    try:
        from chromashift.constants import PECULIAR_VELOCITY_REDSHIFT_LIMIT
        out["low_z"] = float(PECULIAR_VELOCITY_REDSHIFT_LIMIT)
        out["source"]["low_z"] = "engine"
    except Exception:
        pass
    try:
        from chromashift.build import _MAX_COLOUR_TERM_MAG
        out["max_colour_term_mag"] = float(_MAX_COLOUR_TERM_MAG)
        # T-46: the Δμ alert threshold is *the engine's own cap*, not a host number.
        out["mu_delta_alert"] = float(_MAX_COLOUR_TERM_MAG)
        out["source"]["max_colour_term_mag"] = "engine"
    except Exception:
        pass
    return out


def cosmology() -> str | None:
    try:
        from chromashift.constants import DEFAULT_COSMOLOGY_NAME
        return DEFAULT_COSMOLOGY_NAME
    except Exception:
        return None


# ── process-level caches (invalidation key = artefact sha256) ────────────────

_cache_lock = threading.Lock()
_surface_cache: dict = {}   # (root, id) -> ((npz_sha, qc_sha), SpectralSurface)
_bank_sha: dict = {}        # root -> filters/index.json sha256


def _file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def load_surface_cached(template_id: str, root: str | Path | None = None):
    """`chromashift.load_surface` memoised on the npz+qc bytes.

    The engine refuses stale artefacts itself, so a hit is only served when the
    artefact files are byte-identical to the ones the last *validated* load saw.
    A manifest/filter/code edit does not touch those files -- see the module
    docstring: the guard axis owns that check.
    """
    cs = require()
    base = Path(root) if root is not None else paths.library_root()
    npz = paths.surface_npz(base, template_id)
    qc = npz.parent / (npz.stem + ".qc.json")
    sig = (_file_sha(npz), _file_sha(qc))
    key = (str(base), template_id)
    with _cache_lock:
        hit = _surface_cache.get(key)
        if hit and hit[0] == sig and None not in sig:
            return hit[1]
    surf = cs.load_surface(template_id, root=base)  # never rebuild= (A-3)
    with _cache_lock:
        _surface_cache[key] = (sig, surf)
    return surf


def bank_cached(root: str | Path | None = None):
    """`chromashift.bank` with sha-keyed invalidation on top of its per-dir cache."""
    cs = require()
    base = Path(root) if root is not None else paths.library_root()
    sig = _file_sha(base / "data" / "filters" / "index.json")
    with _cache_lock:
        if _bank_sha.get(str(base)) != sig:
            cs.registry.reset_bank()
            _bank_sha[str(base)] = sig
    return cs.bank(base)


def reset_caches() -> None:
    """Drop all memoised state (tests that repoint AJST_TMPLIB_DIR call this)."""
    with _cache_lock:
        _surface_cache.clear()
        _bank_sha.clear()
    cs = _import()
    if cs is not None:
        cs.registry.reset_bank()
