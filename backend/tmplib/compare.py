"""API-8 orchestration (S3): up to 8 template curves + up to 8 sources under
the partial-success protocol (A-6 -- the request itself is only 400 when its
own shape/quota is invalid; any single curve/source failure marks that entry
`state: error` and leaves the rest alone).

Curve kinds (§4.2):
  * ``template-prediction`` -- delegated to P1's ``predict.predict_curve``
    verbatim (same staleness gates, same grading).
  * ``kcorrected-measured`` -- the S3 core (F-21'): for a source that has a
    template, every qualified catalogue row becomes
    ``M_meas = m_AB − μ_engine − K`` with K taken from the source's own
    surface at the same observed epoch and μ always the engine value (the
    catalogue gext_distmod only appears as the Δμ cross-check, POS-9).
    Rows are F-06'-cleaned: discards dropped and counted, upper limits
    listed separately, epochs outside t_valid clipped per point (same
    semantics as the template layer).  Vega-system rows are converted to AB
    server-side with the engine's ``SystemConverter`` (ST-9).
  * ``measured`` -- the same F-06' cleaning without any engine call, so the
    frontend can draw raw points under one protocol.

ST-1/A-8: this module never opens a DB session -- catalogue rows and source
metadata are fetched by the route layer inside one short, already-closed
session and passed in as plain data; engine calls happen afterwards.
T-29: nothing here changes the existing compare-page flux/absmag paths --
the cleaning applies only inside this new API.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np

from . import engine, extract, guard, mudelta, paths, predict as tl_predict

log = logging.getLogger(__name__)   # ST-10："已记录"必须有实际记录

MAX_CURVES = 8          # Q-8
MAX_SOURCES = 8         # Q-8

#: transient -> template resolution order: explicit request pair first, then
#: the library's own bindings (wizard entries carry transient_id; the four
#: catalog-derived shipped entries carry extract.transient_id; COUNTERPARTS
#: is the shipped-name fallback, F-19).
_SOURCE_TEMPLATE_CODES = {
    "no-template": "TL_NO_TEMPLATE_FOR_SOURCE",
    "unknown": "TL_TRANSIENT_UNKNOWN",
}


def validate_request(body) -> dict:
    """Q-8/A-6: shape + quota, before any DB or engine touch."""
    if not isinstance(body, dict):
        raise tl_predict.TLError("TL_INCONSISTENT_ARGS", 400,
                                 "请求体必须是 JSON 对象")
    curves = body.get("curves") or []
    sources = body.get("sources") or []
    if not isinstance(curves, list) or not isinstance(sources, list):
        raise tl_predict.TLError("TL_INCONSISTENT_ARGS", 400,
                                 "curves/sources 必须是数组")
    if len(curves) > MAX_CURVES:
        raise tl_predict.TLError(
            "TL_QUOTA", 400,
            f"模板曲线 {len(curves)} 条超过上限 {MAX_CURVES}（不截断，E-04）",
            limit=MAX_CURVES, got=len(curves))
    if len(sources) > MAX_SOURCES:
        raise tl_predict.TLError(
            "TL_QUOTA", 400,
            f"源 {len(sources)} 个超过上限 {MAX_SOURCES}（不截断，E-04）",
            limit=MAX_SOURCES, got=len(sources))
    if not curves and not sources:
        raise tl_predict.TLError("TL_INCONSISTENT_ARGS", 400,
                                 "curves 与 sources 至少给一个")
    for i, c in enumerate(curves):
        if not isinstance(c, dict) or not c.get("template_id") or not c.get("band"):
            raise tl_predict.TLError(
                "TL_INCONSISTENT_ARGS", 400,
                f"curves[{i}] 需要 template_id 与 band")
    for i, s in enumerate(sources):
        if not isinstance(s, dict) or not s.get("transient_id"):
            raise tl_predict.TLError(
                "TL_INCONSISTENT_ARGS", 400,
                f"sources[{i}] 需要 transient_id")
    return {"curves": curves, "sources": sources,
            "include_measured": bool(body.get("include_measured", True))}


def _err_entry(kind, *, code, message, **ids):
    return {"kind": kind, "state": "error",
            "error": {"code": code, "message": message}, **ids}


def _engine_entry(kind, exc, **ids):
    """E-30：引擎异常按 code 分派为部分成功条目（审核 P1-1：此前一律
    TL_INTERNAL，E_DOMAIN/E_FILTER 的 code 与 context 在 API-8 里双双丢失，
    前端无法出域判定卡）。未知引擎码按 ST-10 记日志后仍给 TL_INTERNAL。"""
    mapped = engine.ENGINE_CODE_MAP.get(exc.code)
    if mapped is None:
        log.error("tmplib compare 未映射的引擎错误: %r", exc)
        return _err_entry(kind, code="TL_INTERNAL",
                          message="引擎内部错误（已记录）", **ids)
    entry = _err_entry(kind, code=mapped[0], message=exc.message, **ids)
    if getattr(exc, "context", None):
        entry["error"]["context"] = dict(exc.context)   # E-13：逐键展开
    return entry


def _template_for_source(transient_id: str, lib: dict) -> str | None:
    """The source's own template id, or None (F-23: such sources cannot be
    K-corrected -- there is no surface to take K from)."""
    best = None
    for tid, entry in (lib.get("templates") or {}).items():
        if entry.get("deleted"):
            continue
        bound = entry.get("transient_id") \
            or (entry.get("extract") or {}).get("transient_id")
        if bound == transient_id and entry.get("origin") == "catalog":
            return tid                     # 向导条目是该源自己的面，优先
        if bound == transient_id:
            best = best or tid
    if best:
        return best
    for tid, cid in mudelta.COUNTERPARTS.items():
        if cid == transient_id and tid in (lib.get("templates") or {}):
            return tid
    return None


def _split_rows(csv_rows):
    """F-06': detections / upper limits / discard count out of the CSV rowset
    (upperlimit/discard rows are in the CSV but never enter a curve)."""
    det, ul = [], []
    n_discard = 0
    for r in csv_rows:
        if r[6]:
            n_discard += 1
        elif r[5]:
            ul.append(r)
        else:
            det.append(r)
    return det, ul, n_discard


def _stale_gate(tid, spec, root):
    """Same E-12 engine-inputs gate as the predict path (A-3: refuse, never
    rebuild).  Returns the stale list ([] = fresh)."""
    from chromashift import registry
    npz = root / "data" / "surfaces" / f"{tid}.npz"
    try:
        return list(registry.stale_reasons(npz, spec, root) or [])
    except Exception:
        return ["surface-unreadable"]


def kcorrected_measured(transient_id: str, template_id: str, *,
                        transient: dict, rows: list[dict], root: Path) -> dict:
    """F-21': M_meas = m_AB − μ_engine − K for every qualified row of the
    source, K from its own surface at the same observed epoch."""
    cs = engine.require()
    known = cs.manifests(root)
    if template_id not in known:
        raise tl_predict.TLError("TL_TEMPLATE_UNKNOWN", 404,
                                 f"模板 {template_id!r} 不存在")
    spec = cs.TemplateSpec.from_yaml(known[template_id])   # ST-7
    stale = _stale_gate(template_id, spec, root)
    if stale:
        raise tl_predict.TLError(
            "TL_STALE", 409,
            f"模板 {template_id} 的面已陈旧（{', '.join(stale)}），需先重建",
            stale_because={"engine_inputs": stale,
                           "catalog_rows": None, "filter_vendor": None})
    lib = paths.read_library(root) or {}
    entry = (lib.get("templates") or {}).get(template_id) or {}
    surf = engine.load_surface_cached(template_id, root)
    bank = engine.bank_cached(root)
    z = float(spec.z)
    one_plus = 1.0 + z
    t_lo, t_hi = float(surf.t_valid[0]), float(surf.t_valid[1])

    # 取数配方：条目登记值优先（F-52 同口径）；显式配对无配方 ⇒ raw+drop
    recipe = entry.get("extract") or {}
    try:
        csv_rows, ledger, _ = extract.build_rowset(
            rows,
            rowset=recipe.get("rowset", "raw"),
            null_system_policy=recipe.get("null_system_policy", "drop"),
            declarations={d["band"]: d["system"]
                          for d in recipe.get("declarations") or []},
            bands=recipe.get("bands"),
            registry_ids=frozenset(bank.names()))
    except extract.SourceRejected as e:
        raise tl_predict.TLError(
            "TL_DATA", 422,
            f"null_system_policy=drop-source：{e} ⇒ 整源作废（F-10）")

    det_rows, ul_rows, n_discard = _split_rows(csv_rows)
    conv = cs.SystemConverter(bank)
    dist = spec.distance_obj()
    mu_engine = float(dist.distance_modulus)

    detections, upper_limits, clipped, failed = [], [], [], []
    by_band: dict[str, list] = {}
    for band, t_s, mag, err, system, _ul, _dc in det_rows:
        by_band.setdefault(band, []).append((t_s, mag, err, system))

    for band in sorted(by_band):
        rows_b = by_band[band]
        if band not in surf.bands:
            # 不在面上的波段（库行比出厂表多波段是常态）⇒ 与 indomain 同口径，
            # 不调引擎（否则整批 E_DOMAIN 淹没真实边缘情形）
            for r in rows_b:
                failed.append({"t_obs_s": r[0], "band": band,
                               "reason": "not-on-surface"})
            continue
        t_obs_days = np.asarray([r[0] / 86400.0 for r in rows_b], float)
        t_rest = t_obs_days / one_plus
        in_win = (t_rest >= t_lo) & (t_rest <= t_hi)
        for r, t_r, ok in zip(rows_b, t_rest, in_win):
            if not ok:                        # 逐点标记，不入曲线（模板层同口径）
                clipped.append({"t_obs_s": r[0], "band": band,
                                "reason": "out-of-t_valid"})
        idx = np.nonzero(in_win)[0]
        if idx.size == 0:
            continue
        # Vega/ST → AB（服务端，ST-9；E_Cfg 的波段整带记 failed）
        try:
            mags_ab = [float(conv.to_ab(np.asarray([rows_b[i][1]]),
                                        rows_b[i][3], band=band)[0])
                       for i in idx]
        except cs.ChromaShiftError as e:
            for i in idx:
                failed.append({"t_obs_s": rows_b[i][0], "band": band,
                               "reason": f"error:{e.code}"})
            continue
        try:
            pred = cs.predict(surf, bank, dist, band, z=z,
                              times_obs_days=t_obs_days[idx], mode="auto")
        except cs.ChromaShiftError as e:
            for i in idx:
                failed.append({"t_obs_s": rows_b[i][0], "band": band,
                               "reason": f"error:{e.code}"})
            continue
        for j, i in enumerate(idx):
            k = pred.k_correction[j]
            m_ab = mags_ab[j]
            t_s, _mag, err, system = rows_b[i]
            if k is None or not math.isfinite(float(k)):
                failed.append({"t_obs_s": t_s, "band": band,
                               "reason": "k-unavailable(out-of-coverage)"})
                continue
            detections.append({
                "t_obs_s": t_s, "band": band,
                "m_AB": m_ab,
                "m_AB_err": (None if err is None else float(err)),
                "system_in": system,
                "K": float(k), "mode": pred.mode,
                "M_meas": float(m_ab - mu_engine - float(k)),   # F-21'
                "t_rest_s": float(t_rest[i] * 86400.0),
            })

    for band, t_s, mag, err, system, _ul, _dc in ul_rows:
        try:
            m_ab = float(conv.to_ab(np.asarray([mag]), system, band=band)[0])
        except cs.ChromaShiftError:
            m_ab = None
        upper_limits.append({"t_obs_s": t_s, "band": band, "m_AB": m_ab,
                             "system_in": system})

    notes, alerts = [], []
    if not (spec.reddening or {}).get("mw_removed"):
        # 与 P1 域判级的 absolute-mag-not-intrinsic 同族（CA-05）：raw 行集
        # 的 M_meas 含银消，不是内禀绝对星等
        alerts.append("CA-05")
        notes.append("该模板表未扣银消（mw_removed=false）：M_meas 含银消，"
                     "不是内禀绝对星等（与同面模板曲线的口径一致）")
    mu = None
    try:
        mu = mudelta.compute_mu(spec, transient, entry)   # Δμ 对照（POS-9）
    except Exception:
        mu = None

    return {
        "kind": "kcorrected-measured",
        "transient_id": transient_id, "template_id": template_id,
        "z": z, "rowset": recipe.get("rowset", "raw"),
        "distance_modulus_engine": mu_engine,
        "mu": mu,
        "points": {"detections": detections, "upper_limits": upper_limits,
                   "clipped": clipped, "failed": failed},
        "counts": {
            "rows_total": ledger["total"],
            "detections": len(detections),
            "upper_limits": len(upper_limits),
            "discard": n_discard,
            "clipped": len(clipped),
            "failed": len(failed),
            "rejected": ledger["rejected"],
        },
        "systems_audit": conv.summary(),
        "alerts": alerts,
        "notes": notes,
    }


def measured_series(transient_id: str, *, rows: list[dict], root: Path) -> dict:
    """F-06'-cleaned raw points, no engine call (T-29: this cleaning lives
    only inside API-8).  m_AB conversion is server-side (ST-9)."""
    cs = engine.require()
    bank = engine.bank_cached(root)
    csv_rows, ledger, _ = extract.build_rowset(
        rows, rowset="raw", null_system_policy="drop",
        registry_ids=frozenset(bank.names()))
    det_rows, ul_rows, n_discard = _split_rows(csv_rows)
    conv = cs.SystemConverter(bank)

    def _ab(mag, system, band):
        try:
            return float(conv.to_ab(np.asarray([mag]), system, band=band)[0])
        except cs.ChromaShiftError:
            return None

    return {
        "kind": "measured",
        "transient_id": transient_id,
        "points": {
            "detections": [
                {"t_obs_s": t_s, "band": band, "mag": mag,
                 "mag_err": err, "system_in": system,
                 "m_AB": _ab(mag, system, band)}
                for band, t_s, mag, err, system, _u, _d in det_rows],
            "upper_limits": [
                {"t_obs_s": t_s, "band": band, "mag": mag,
                 "system_in": system, "m_AB": _ab(mag, system, band)}
                for band, t_s, mag, err, system, _u, _d in ul_rows],
        },
        "counts": {
            "rows_total": ledger["total"],
            "detections": len(det_rows),
            "upper_limits": len(ul_rows),
            "discard": n_discard,
            "rejected": ledger["rejected"],
        },
        "systems_audit": conv.summary(),
    }


def run(req: dict, *, source_data: dict, catalogs: dict, rows_hashes: dict,
        root=None) -> dict:
    """The partial-success fan-out.  `source_data` maps transient_id ->
    (transient_dict|None, rows) fetched by the route layer; `catalogs` /
    `rows_hashes` are the predict-path companions keyed by template_id."""
    base = Path(root) if root is not None else paths.library_root()
    cs = engine.require()   # 全部分支都要引擎；不可用由路由层 available() 前置
    try:
        lib = paths.read_library(base) or {}
    except ValueError:
        lib = {}
    # ST-15/E-20：vendor 漂移不阻断，但每条涉及引擎通带的曲线都要显形（CA-01）
    try:
        vendor_ok = guard.vendor_axis(base).get("ok")
    except Exception:
        vendor_ok = None
    curves_out = []

    for item in req["curves"]:
        tid = item.get("template_id")
        try:
            res = tl_predict.predict_curve(
                item, catalog=catalogs.get(tid),
                root=base, rows_live_hash=rows_hashes.get(tid))
            curves_out.append(res["curve"])
        except tl_predict.TLError as e:
            curves_out.append(_err_entry(
                "template-prediction", code=e.code, message=e.message,
                template_id=tid, band=item.get("band")))
        except cs.ChromaShiftError as e:
            curves_out.append(_engine_entry(
                "template-prediction", e, template_id=tid,
                band=item.get("band")))
        except Exception as e:
            log.error("tmplib compare 模板曲线未知异常: %r", e)   # ST-10
            curves_out.append(_err_entry(
                "template-prediction", code="TL_INTERNAL",
                message="内部错误（已记录）",
                template_id=tid, band=item.get("band")))

    for item in req["sources"]:
        sid = item["transient_id"]
        transient, rows = source_data.get(sid, (None, []))
        if transient is None:
            curves_out.append(_err_entry(
                "measured", code=_SOURCE_TEMPLATE_CODES["unknown"],
                message=f"源 {sid!r} 不存在", transient_id=sid))
            continue
        if req["include_measured"]:
            try:
                curves_out.append(measured_series(sid, rows=rows, root=base))
            except tl_predict.TLError as e:
                curves_out.append(_err_entry(
                    "measured", code=e.code, message=e.message,
                    transient_id=sid))
            except cs.ChromaShiftError as e:
                curves_out.append(_engine_entry("measured", e, transient_id=sid))
            except Exception as e:
                log.error("tmplib compare 实测序列未知异常: %r", e)   # ST-10
                curves_out.append(_err_entry(
                    "measured", code="TL_INTERNAL",
                    message="内部错误（已记录）", transient_id=sid))
        tid = item.get("template_id") or _template_for_source(sid, lib)
        if not tid:
            # F-23/W-24：无模板的源没有面可取 K ⇒ 逐条标 ✕，不是请求失败
            curves_out.append(_err_entry(
                "kcorrected-measured",
                code=_SOURCE_TEMPLATE_CODES["no-template"],
                message=f"源 {sid} 在模板库里没有自己的模板：没有面可取 K，"
                        "该源不参与 K 改正（实测点仍给出）",
                transient_id=sid))
            continue
        try:
            curves_out.append(kcorrected_measured(
                sid, tid, transient=transient, rows=rows, root=base))
        except tl_predict.TLError as e:
            curves_out.append(_err_entry(
                "kcorrected-measured", code=e.code, message=e.message,
                transient_id=sid, template_id=tid))
        except cs.ChromaShiftError as e:
            curves_out.append(_engine_entry(
                "kcorrected-measured", e, transient_id=sid, template_id=tid))
        except Exception as e:
            log.error("tmplib compare K 改正序列未知异常: %r", e)   # ST-10
            curves_out.append(_err_entry(
                "kcorrected-measured", code="TL_INTERNAL",
                message="内部错误（已记录）",
                transient_id=sid, template_id=tid))

    notes = ["部分成功协议（A-6）：单条失败只标该条 state=error，"
             "其余照常；μ 一律引擎值，库 gext_distmod 仅作 Δμ 对照（F-21'）"]
    if vendor_ok is False:
        # ST-15/E-20/TXT-10：漂移不阻断，逐曲线显形 + 一条总说明
        for c in curves_out:
            if c.get("state") != "error":
                c.setdefault("alerts", []).append("CA-01")
        notes.append("引擎的通带快照取自本库的 filters.json，记录 sha 与现文件"
                     "不一致（CA-01）：K 改正按快照计算；处置在引擎侧重跑 "
                     "vendor 脚本，本功能无权改引擎数据。")
    return {
        "code": "TL_OK",
        "curves": tl_predict._sanitize(curves_out),
        "provenance": {
            "engine_version": engine.version(),
            "engine_code_sha256": engine.code_sha256(),
            "cosmology": engine.cosmology(),
            "backend": "in-process",
            "host_max_curves": MAX_CURVES,
            "host_max_sources": MAX_SOURCES,
        },
        "notes": ["部分成功协议（A-6）：单条失败只标该条 state=error，"
                  "其余照常；μ 一律引擎值，库 gext_distmod 仅作 Δμ 对照（F-21'）"],
    }
