"""
统计接口
GET /api/stats/overview    — 全目录概览
GET /api/stats/redshifts   — 红移分布
GET /api/stats/bands       — 波段覆盖统计
GET /api/stats/tags        — 标签 / 子标签目标数
GET /api/stats/hosts       — 宿主星系覆盖与参数分布
"""
from flask import Blueprint, jsonify
from sqlalchemy import func, distinct, text
from app import get_session
from models import Transient, Lightcurve, FilterDef, HostGalaxy, distance_modulus

stats_bp = Blueprint('stats', __name__)


@stats_bp.route('/overview', methods=['GET'])
def overview():
    sess = get_session()
    try:
        n_transients = sess.query(func.count(Transient.id)).scalar()
        n_lc = sess.query(func.count(Lightcurve.id)).scalar()
        n_with_z = sess.query(func.count(Transient.id)).filter(Transient.redshift.isnot(None)).scalar()
        # 波段数
        n_bands = sess.query(func.count(distinct(Lightcurve.band))).scalar()
        # 望远镜数
        n_tels = sess.query(func.count(distinct(Lightcurve.telescope))).scalar()
        # 标签分布
        return jsonify({
            'n_transients': n_transients,
            'n_lightcurves': n_lc,
            'n_with_redshift': n_with_z,
            'n_bands': n_bands,
            'n_telescopes': n_tels,
            'n_hosts': sess.query(func.count(HostGalaxy.id)).scalar(),
        })
    finally:
        sess.close()


@stats_bp.route('/redshifts', methods=['GET'])
def redshift_distribution():
    sess = get_session()
    try:
        rows = sess.query(Transient.redshift).filter(
            Transient.redshift.isnot(None)
        ).order_by(Transient.redshift).all()
        values = [r[0] for r in rows]
        return jsonify({'values': values, 'n': len(values)})
    finally:
        sess.close()


@stats_bp.route('/bands', methods=['GET'])
def band_coverage():
    sess = get_session()
    try:
        rows = sess.query(
            Lightcurve.band,
            func.count(Lightcurve.id).label('cnt')
        ).group_by(Lightcurve.band).order_by(func.count(Lightcurve.id).desc()).all()
        return jsonify([{'band': r.band, 'count': r.cnt} for r in rows])
    finally:
        sess.close()


@stats_bp.route('/hosts', methods=['GET'])
def host_stats():
    """宿主星系统计：覆盖率、红移类型计数、M*/SFR 分布、宿主测光绝对星等点。

    abs_mag_points: [{tid, band, z, mag(AB), abs_mag, mag_err, err_assumed, upperlimit}]
      M = m_AB − μ(z_host)，μ 由 models.distance_modulus（astropy Planck18）计算；
      Vega 星等先按 filters 表 vega2ab 转 AB；非上限且缺误差的点按 0.2 mag（err_assumed 标记，不落库）。
    """
    sess = get_session()
    try:
        hosts = sess.query(HostGalaxy).all()
        n_hosts = len(hosts)
        n_transients = sess.query(func.count(Transient.id)).scalar()
        n_spec = sum(1 for h in hosts if h.redshift_type == 'spec')
        n_phot = sum(1 for h in hosts if h.redshift_type == 'phot')
        m_star, sfr = [], []
        for h in hosts:
            d = h.derived or {}
            if d.get('m_star') is not None:
                m_star.append(d['m_star'])
            if d.get('sfr') is not None:
                sfr.append(d['sfr'])
        # 宿主测光 → 绝对星等（需宿主红移）
        filters = {f.id: f for f in sess.query(FilterDef).all()}

        def _vega2ab(band):
            f = filters.get(band) or filters.get(str(band).lower())
            return (f.vega2ab or 0.0) if f else 0.0

        abs_mag_points = []
        for h in hosts:
            z = h.redshift
            if z is None or z <= 0:
                continue
            dm = distance_modulus(z)
            if dm is None:
                continue
            for p in (h.photometry or []):
                if not isinstance(p, dict):
                    continue
                band, mag = p.get('band'), p.get('mag')
                if not band or mag is None:
                    continue
                try:
                    mag = float(mag)
                except (TypeError, ValueError):
                    continue
                mag_sys = str(p.get('mag_sys') or 'AB').strip().lower()
                if mag_sys == 'vega':
                    mag += _vega2ab(band)
                elif mag_sys not in ('ab', ''):
                    continue  # ST 等其他星等系统暂不换算
                ul = bool(p.get('upperlimit'))
                err = p.get('mag_err')
                try:
                    err = float(err) if err is not None else None
                except (TypeError, ValueError):
                    err = None
                err_assumed = False
                if not ul and (err is None or err <= 0):
                    err, err_assumed = 0.2, True  # 缺省误差 0.2 mag（仅返回，不写库）
                abs_mag_points.append({
                    'tid': h.transient_id, 'band': band, 'z': z,
                    'mag': round(mag, 4), 'abs_mag': round(mag - dm, 4),
                    'mag_err': err, 'err_assumed': err_assumed, 'upperlimit': ul,
                })
        return jsonify({
            'n_hosts': n_hosts,
            'n_transients': n_transients,
            'coverage': (n_hosts / n_transients) if n_transients else 0,
            'n_with_spec_z': n_spec,
            'n_with_phot_z': n_phot,
            'm_star': m_star,
            'sfr': sfr,
            'abs_mag_points': abs_mag_points,
        })
    finally:
        sess.close()


@stats_bp.route('/tags', methods=['GET'])
def tag_counts():
    """标签目标数 + 每个标签下的子标签目标数（tags/sub_tag 为 JSONB 数组）"""
    sess = get_session()
    try:
        tag_rows = sess.execute(text(
            "SELECT t.tag, count(*) FROM transients,"
            " jsonb_array_elements_text(tags) AS t(tag) GROUP BY t.tag ORDER BY count(*) DESC"
        )).all()
        sub_rows = sess.execute(text(
            "SELECT t.tag, s.sub, count(*) FROM transients,"
            " jsonb_array_elements_text(tags) AS t(tag),"
            " jsonb_array_elements_text(sub_tag) AS s(sub)"
            " GROUP BY t.tag, s.sub ORDER BY t.tag, count(*) DESC"
        )).all()
        # 无子标签的目标数（按标签），便于前端补“未标注”
        nosub_rows = sess.execute(text(
            "SELECT t.tag, count(*) FROM transients,"
            " jsonb_array_elements_text(tags) AS t(tag)"
            " WHERE (sub_tag IS NULL OR sub_tag = '[]'::jsonb) GROUP BY t.tag"
        )).all()
        sub_by_tag = {}
        for tag, sub, cnt in sub_rows:
            sub_by_tag.setdefault(tag, []).append({'sub_tag': sub, 'count': cnt})
        return jsonify({
            'tags': [{'tag': r[0], 'count': r[1]} for r in tag_rows],
            'sub_by_tag': sub_by_tag,
            'no_sub': {r[0]: r[1] for r in nosub_rows},
        })
    finally:
        sess.close()
