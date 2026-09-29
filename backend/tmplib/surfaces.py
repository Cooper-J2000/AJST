"""Surface build/rebuild with the two concurrency guards (ST-4/ST-5, POS-3).

* ST-4: one process-wide non-blocking BoundedSemaphore(1) -- a second build
  (or a running batch predict, routes share the semaphore) gets 429 at once,
  never a queue.  The semaphore is acquired by the *route*, so a failed
  acquisition provably produces zero filesystem side effects (T-31).
* ST-5: per-template single-flight (process dict + Lock): a concurrent rebuild
  of the same id gets 409 (IA-5) -- the engine writes npz non-atomically and
  without locking, so this guard is ours, not its.
* POS-3/T-43: the ONLY caller of the engine's build_template in the whole
  backend is `build_surface` below, and the only callers of that are the
  API-5/API-7 route handlers.  No startup hook, no thread, no timer.

ST-1: this module never sees a DB session; rows arrive from the route layer
already fetched.
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import engine, paths

#: ST-4: builds and batch predictions share one slot; non-blocking, no queue.
BUILD_SEM = threading.BoundedSemaphore(1)

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class SingleFlight(Exception):
    """ST-5/IA-5: the same template id is already building."""

    def __init__(self, template_id: str):
        super().__init__(f"模板 {template_id} 正在建面/重建中")
        self.template_id = template_id


def _lock_for(template_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(template_id, threading.Lock())


def build_surface(template_id: str, root=None):
    """Run the engine build for one template.  Caller must hold BUILD_SEM.

    Returns (surface, qc, npz_path).  Raises SingleFlight when the id is
    already in flight; engine errors propagate as ChromaShiftError for the
    route's E-* mapping.
    """
    cs = engine.require()
    base = Path(root) if root is not None else paths.library_root()
    lock = _lock_for(template_id)
    if not lock.acquire(blocking=False):
        raise SingleFlight(template_id)
    try:
        yaml_path = paths.template_yaml(base, template_id)
        surf, qc, out = cs.build_template(yaml_path, base)  # R-01
        engine.reset_caches()  # 新面/新 QC 进缓存前失效旧的（A-3 反面：这是写路径）
        return surf, qc, out
    finally:
        lock.release()
