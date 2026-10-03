"""Template-library filesystem conventions (design doc 02 §4.1, F-01/F-50/F-51).

Owns three things and nothing else:

* where the library root is (`AJST_TMPLIB_DIR` overrides the default
  ``catadata/tmplibrary`` in the co-located data repo, so the template
  library ships with AJST-Data);
* how `library.json` is read and written -- the write is tmp+rename atomic and
  keeps the previous full copy as ``library.json.bak`` (F-50), so a crash
  mid-write leaves either the old file or the new one, never a torn one;
* the template-id rule and the soft-delete ``data/.trash/`` convention (F-51;
  the delete endpoint itself is P2, this module only fixes the naming).

This module must stay importable without the engine installed: `paths` is what
`engine.available()` uses to report a missing root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

#: Environment variable that overrides the library root (F-01).
ENV_VAR = "AJST_TMPLIB_DIR"

#: `library.json` schema version (C_SCHEMA_LIB).
SCHEMA_LIB = "ajst-tmplib-2"

#: Template id rule (C_ID_RE): lowercase, <=48 chars, no path separators --
#: which is also what makes `id` safe to interpolate into a path.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")

LIBRARY_JSON = "library.json"

#: ST-12 库配额（C_LIB_QUOTA）：派生数据要有配额意识。默认 200 MB（九出厂
#: 模板 ~2 MB 量级，留足向导模板余量）；AJST_TMPLIB_QUOTA_MB 可覆盖，0 = 关闭。
QUOTA_ENV = "AJST_TMPLIB_QUOTA_MB"
DEFAULT_QUOTA_MB = 200


def default_root() -> Path:
    """``<repo>/catadata/tmplibrary`` -- the data repo next to the code repo."""
    return Path(__file__).resolve().parents[2] / "catadata" / "tmplibrary"


def library_root() -> Path:
    """The effective library root: `AJST_TMPLIB_DIR` wins over the default."""
    env = os.environ.get(ENV_VAR)
    return Path(env) if env else default_root()


def library_json_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else library_root()
    return base / LIBRARY_JSON


def backup_path(root: str | Path | None = None) -> Path:
    return library_json_path(root).parent / (LIBRARY_JSON + ".bak")


def valid_id(template_id: str) -> bool:
    return bool(ID_RE.match(template_id or ""))


def check_id(template_id: str) -> str:
    """Return the id or raise ValueError -- the one gate every path join goes through."""
    if not valid_id(template_id):
        raise ValueError(f"非法模板 id: {template_id!r}（须匹配 {ID_RE.pattern}）")
    return template_id


def template_yaml(root: str | Path, template_id: str) -> Path:
    return Path(root) / "templates" / f"{check_id(template_id)}.yaml"


def surface_npz(root: str | Path, template_id: str) -> Path:
    return Path(root) / "data" / "surfaces" / f"{check_id(template_id)}.npz"


def read_library(root: str | Path | None = None) -> dict | None:
    """The parsed `library.json`, or None when the library has no index yet.

    A corrupt index raises ValueError rather than being papered over: silently
    treating "unreadable" as "empty" would let a later write lose every record.
    """
    path = library_json_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"library.json 不可读: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_LIB:
        raise ValueError(
            f"library.json schema 不符: 期望 {SCHEMA_LIB}，"
            f"得到 {data.get('schema')!r}" if isinstance(data, dict) else "非对象")
    return data


def write_library(data: dict, root: str | Path | None = None) -> Path:
    """Atomically replace `library.json` (F-50).

    Order: snapshot the current file to ``library.json.bak`` -> write a tmp file
    in the same directory -> fsync -> ``os.replace``.  A kill between any two
    steps leaves a consistent state: the old index, or the new one, plus a
    ``.bak`` to recover the previous generation (T-25).  The tmp file carries
    the pid so two writers never share one.
    """
    if data.get("schema") != SCHEMA_LIB:
        raise ValueError(f"拒绝写入 schema != {SCHEMA_LIB} 的索引")
    path = library_json_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, backup_path(root))
    tmp = path.parent / f"{LIBRARY_JSON}.tmp-{os.getpid()}"
    blob = (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # replace did not happen -> clean up the orphan
            tmp.unlink()
    return path


def restore_library_backup(root: str | Path | None = None) -> bool:
    """Recover `library.json` from its `.bak` (T-25).  False when no backup exists."""
    bak = backup_path(root)
    if not bak.is_file():
        return False
    shutil.copy2(bak, library_json_path(root))
    return True


def trash_dir(root: str | Path | None = None) -> Path:
    """Soft-delete landing zone (F-51): ``data/.trash/`` under the library root."""
    return (Path(root) if root is not None else library_root()) / "data" / ".trash"


def trash_target(template_id: str, root: str | Path | None = None,
                 when: datetime | None = None) -> Path:
    """``data/.trash/<id>-<utc>/`` for one soft-deleted template (P2 wires the move)."""
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    return trash_dir(root) / f"{check_id(template_id)}-{stamp}"


# ── ST-12: library quota ─────────────────────────────────────────────────────

def quota_bytes() -> int | None:
    """The library-root size ceiling in bytes, or None when disabled (0)."""
    try:
        mb = int(os.environ.get(QUOTA_ENV, DEFAULT_QUOTA_MB))
    except ValueError:
        mb = DEFAULT_QUOTA_MB
    return None if mb <= 0 else mb * 1024 * 1024


def usage_bytes(root: str | Path | None = None) -> int:
    """Total size of the library root in bytes (templates + surfaces + CSVs)."""
    base = Path(root) if root is not None else library_root()
    total = 0
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            try:
                total += (Path(dirpath) / fn).stat().st_size
            except OSError:
                pass
    return total


def quota_check(root: str | Path | None = None) -> dict:
    """ST-12 判定：``ok=False`` 时 ``over_by_bytes`` 是要拒新建的量。"""
    limit = quota_bytes()
    if limit is None:
        return {"ok": True, "limit_bytes": None, "usage_bytes": usage_bytes(root)}
    usage = usage_bytes(root)
    return {"ok": usage < limit, "limit_bytes": limit, "usage_bytes": usage,
            "over_by_bytes": max(0, usage - limit)}
