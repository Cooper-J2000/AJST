"""
STDWeb → AJST 测光数据接入 API（ingest）

GET  /api/ingest/resolve     — 解析目标源（名称/别名精确 + 坐标锥形，只查不建）
POST /api/ingest/photometry  — 上传测光点（鉴权→解析源→映射→去重→单事务入库→消光重算）

鉴权：与现有会话认证并存，本模块只认 `Authorization: Bearer <AJST_INGEST_TOKEN>`。
设计目标：与 STDpipe/STDweb 测光流水线无缝衔接（STDweb 侧配置上传端点即可），
也可由智能体按本文件中的接口约定辅助完成对接。
"""
import hmac
import math
from datetime import datetime
from functools import wraps

from flask import Blueprint, current_app, request, jsonify

from app import get_session
from models import Transient, Lightcurve, FilterDef
from routes.lightcurves import _apply_lc_fields
import extinction

ingest_bp = Blueprint('ingest', __name__)


def ang_dist(ra1, dec1, ra2, dec2):
    """角距离（度，小角近似）"""
    d = math.radians((dec1 + dec2) / 2)
    return math.hypot((ra1 - ra2) * math.cos(d), dec1 - dec2)


# ─── Bearer token 鉴权 ───
def require_ingest_token(f):
    """ingest 接口专用：Bearer token 常量时间比较。
    未配置 AJST_INGEST_TOKEN → 503；token 缺失/不符 → 401。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = current_app.config.get('AJST_INGEST_TOKEN')
        if not token:
            return {'error': 'ingest API disabled'}, 503
        auth = request.headers.get('Authorization', '')
        parts = auth.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != 'bearer' \
                or not hmac.compare_digest(parts[1], token):
            return {'error': 'invalid ingest token'}, 401
        return f(*args, **kwargs)
    return decorated


# ─── 源匹配助手 ───
def _name_match(t, name):
    """名称精确匹配 id 或 aliases（大小写不敏感）"""
    n = name.strip().lower()
    if not n:
        return False
    if (t.id or '').lower() == n:
        return True
    return any(str(a).lower() == n for a in (t.aliases or []))


def _distance_arcsec(ra, dec, t):
    """与源坐标的角距（角秒），任一侧缺坐标 → None"""
    if ra is None or dec is None or t.ra is None or t.dec is None:
        return None
    return ang_dist(ra, dec, t.ra, t.dec) * 3600.0


def _find_by_name(sess, name):
    return [t for t in sess.query(Transient).all() if _name_match(t, name)]


def _cone_search(sess, ra, dec, radius_arcsec):
    """坐标锥形检索（small-angle 近似，复用 ang_dist），返回 [(transient, dist_arcsec)] 按距离升序"""
    hits = []
    for t in sess.query(Transient).all():
        d = _distance_arcsec(ra, dec, t)
        if d is not None and d <= radius_arcsec:
            hits.append((t, d))
    hits.sort(key=lambda x: x[1])
    return hits


def _candidate_dict(t, dist_arcsec=None):
    return {
        'id': t.id,
        'ra': t.ra,
        'dec': t.dec,
        't0': t.t0.isoformat() if t.t0 else None,
        'aliases': t.aliases or [],
        'distance_arcsec': dist_arcsec,
    }


@ingest_bp.route('/resolve', methods=['GET'])
@require_ingest_token
def resolve():
    """解析目标源：name/别名精确匹配 + ra,dec,radius 锥形检索。只查不建。"""
    name = request.args.get('name', '').strip()
    try:
        ra = float(request.args.get('ra')) if request.args.get('ra') not in (None, '') else None
        dec = float(request.args.get('dec')) if request.args.get('dec') not in (None, '') else None
    except ValueError:
        return {'error': 'ra/dec must be numbers (degrees)'}, 400
    try:
        radius = float(request.args.get('radius', 5.0))
        if radius <= 0:
            raise ValueError
    except ValueError:
        return {'error': 'radius must be a positive number (arcsec)'}, 400
    if not name and (ra is None or dec is None):
        return {'error': 'provide name or ra,dec'}, 400

    sess = get_session()
    try:
        found = {}  # id -> (transient, distance_arcsec)
        if name:
            for t in _find_by_name(sess, name):
                found[t.id] = (t, _distance_arcsec(ra, dec, t))
        if ra is not None and dec is not None:
            for t, d in _cone_search(sess, ra, dec, radius):
                if t.id not in found:
                    found[t.id] = (t, d)
        candidates = sorted(
            (_candidate_dict(t, d) for t, d in found.values()),
            key=lambda c: (c['distance_arcsec'] is None, c['distance_arcsec']),
        )
        return jsonify({'candidates': candidates})
    finally:
        sess.close()


# ─── band 宽松归一化 ───
def _normalize_band(band, known_ids):
    """小写归一化 → 查 filters 表 → 常见变体（去 uvot-/gaia:: 前缀、大小写不敏感匹配）。
    返回 (最终 band, warning 或 None)；查不到照收，仅给 warning 不阻断。"""
    b = str(band).strip().lower()
    if not b:
        return b, None
    if b in known_ids:
        return b, None
    variants = [b]
    for prefix in ('gaia::', 'uvot-'):
        if b.startswith(prefix):
            variants.append(b[len(prefix):])
    lowered = {fid.lower(): fid for fid in known_ids}
    for v in variants:
        if v in lowered:
            return lowered[v], None
    return b, f"band '{b}' not in filters table"


# ─── 点级去重（容差沿用 grbsn_photometry_import.py:72-79 标准，按设计文档 §6 决策 4） ───
def _is_duplicate(bucket, t_sec, mag, upperlimit):
    """bucket: [(time, mag, upperlimit)]。上限点只比时间。"""
    tol_t = max(10.0, 1e-4 * abs(t_sec))
    for (t0, m0, ul0) in bucket:
        if abs(t0 - t_sec) >= tol_t:
            continue
        if upperlimit:
            return True                      # 上限点只比时间
        if not ul0 and m0 is not None and abs(m0 - mag) < 0.03:
            return True
    return False


def _parse_t0(value):
    """ISO 字符串 → naive UTC datetime；非法返回 None"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


@ingest_bp.route('/photometry', methods=['POST'])
@require_ingest_token
def photometry():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return {'error': 'JSON body required'}, 400
    points = body.get('points')
    if not isinstance(points, list) or not points:
        return {'error': 'points must be a non-empty list'}, 400

    sess = get_session()
    try:
        # ── 1. 解析 transient ──
        transient = None
        resolved_dist = None
        tid = (body.get('transient_id') or '').strip() if body.get('transient_id') else ''
        if tid:
            hits = _find_by_name(sess, tid)
            if hits:
                transient = hits[0]
                resolved_dist = _distance_arcsec(body.get('ra'), body.get('dec'), transient)
        else:
            ra, dec = body.get('ra'), body.get('dec')
            if ra is None or dec is None:
                return {'error': 'transient_id or ra,dec required'}, 400
            radius = float(body.get('resolve_radius', 5.0))
            hits = _cone_search(sess, float(ra), float(dec), radius)
            if hits:  # 命中多个 → 取最近者（_cone_search 已按距离升序）
                transient, resolved_dist = hits[0]

        created_transient = False
        if transient is None:
            if not body.get('create_if_missing'):
                return {'error': 'transient not found'}, 404
            nt = body.get('new_transient') or {}
            nid = str(nt.get('id') or '').strip()
            if not nid:
                return {'error': 'new_transient.id is required and must be non-empty'}, 400
            if nt.get('ra') is None or nt.get('dec') is None or nt.get('t0') is None:
                return {'error': 'new_transient requires id, ra, dec, t0'}, 400
            t0 = _parse_t0(nt.get('t0'))
            if t0 is None:
                return {'error': 'new_transient.t0 is not a valid ISO datetime'}, 400
            if sess.query(Transient).filter(Transient.id == nid).first():
                return {'error': f'transient {nid} already exists'}, 400
            extra = dict(nt.get('extra_data') or {})
            extra['ingest_source'] = 'stdweb'
            transient = Transient(id=nid, ra=float(nt['ra']), dec=float(nt['dec']),
                                  t0=t0, aliases=list(nt.get('aliases') or []),
                                  extra_data=extra)
            sess.add(transient)
            sess.flush()
            created_transient = True

        # ── 2. t0 检查（不做隐式猜测） ──
        if transient.t0 is None:
            sess.rollback()
            return {'error': 'transient has no t0, cannot convert MJD'}, 422
        from astropy.time import Time
        t0_mjd = Time(transient.t0).mjd

        # ── 3. 逐点处理（单事务，任一点校验失败整批回滚） ──
        known_ids = {r[0] for r in sess.query(FilterDef.id).all()}
        # 已有点去重桶：band -> [(time, mag, upperlimit)]
        buckets = {}
        for lc in sess.query(Lightcurve).filter(
                Lightcurve.transient_id == transient.id).all():
            buckets.setdefault(lc.band, []).append((lc.time, lc.flux_density, lc.upperlimit))

        inserted, skipped = [], 0
        warnings = []
        for i, p in enumerate(points):
            if not isinstance(p, dict):
                sess.rollback()
                return {'error': f'points[{i}] must be an object'}, 400
            try:
                mjd = float(p.get('mjd'))
                if not math.isfinite(mjd):
                    raise ValueError
            except (TypeError, ValueError):
                sess.rollback()
                return {'error': f'points[{i}]: mjd missing or invalid'}, 400
            band_raw = p.get('band')
            if band_raw in (None, ''):
                sess.rollback()
                return {'error': f'points[{i}]: band is required'}, 400

            mag, mag_err = p.get('mag'), p.get('mag_err')
            limiting_mag = p.get('limiting_mag')
            if mag is None and limiting_mag is None:
                sess.rollback()
                return {'error': f'points[{i}]: mag and limiting_mag both missing'}, 400
            upperlimit = mag is None

            band, warn = _normalize_band(band_raw, known_ids)
            if warn and warn not in warnings:
                warnings.append(warn)

            t_sec = (mjd - t0_mjd) * 86400.0
            value = limiting_mag if upperlimit else mag
            try:
                value = float(value)
            except (TypeError, ValueError):
                sess.rollback()
                return {'error': f'points[{i}]: mag/limiting_mag invalid'}, 400

            # 点级去重：同 (transient_id, band) 桶内
            bucket = buckets.setdefault(band, [])
            if _is_duplicate(bucket, t_sec, value, upperlimit):
                skipped += 1
                continue

            lc = Lightcurve(transient_id=transient.id)
            fields = {
                'time': t_sec,
                'time_unit': 's',
                'band': band,
                'flux_density': value,
                'flux_density_unit': 'mag',
                'upperlimit': upperlimit,
            }
            if not upperlimit and mag_err is not None:
                fields['flux_density_err'] = mag_err
            if p.get('mag_system'):
                fields['mag_system'] = str(p['mag_system'])
            for f in ('telescope', 'instrument', 'reference'):
                if p.get(f):
                    fields[f] = p[f]
            if isinstance(p.get('extra_data'), dict):
                fields['extra_data'] = p['extra_data']
            _apply_lc_fields(lc, fields)
            lc.source = str(body.get('source') or 'ingest-api')  # 数据来源：ingest 渠道
            sess.add(lc)
            sess.flush()
            bucket.append((t_sec, value, upperlimit))
            inserted.append(lc)

        # ── 4. 消光重算（异常仅记 warning，不回滚入库） ──
        for lc in inserted:
            try:
                extinction.recompute_point(sess, lc)
            except Exception as e:
                warnings.append(f'extinction recompute failed for point: {e}')

        sess.commit()
        return jsonify({
            'transient_id': transient.id,
            'created_transient': created_transient,
            'resolved': {'id': transient.id, 'distance_arcsec': resolved_dist},
            'inserted': len(inserted),
            'skipped_duplicates': skipped,
            'warnings': warnings,
            'points': [{'id': lc.id, 'time': lc.time, 'band': lc.band,
                        'upperlimit': lc.upperlimit} for lc in inserted],
        })
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()
