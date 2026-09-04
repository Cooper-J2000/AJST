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
from extinction import correct_host_phot

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

    abs_mag_points: [{tid, band, z, mag(AB), abs_mag, mag_err, err_assumed, upperlimit,
                      gext_applied, gext_Alambda, mag_raw, mag_corr, mag_sys, gext_corr}]
      M = m_AB − μ(z_host)，μ 由 models.distance_modulus（astropy Planck18）计算；
      Vega 星等先按 filters 表 vega2ab 转 AB；非上限且缺误差的点按 0.2 mag（err_assumed 标记，不落库）。
      银河系消光：gext_corr 非真的行先按 CSFD+Rv3.1+P92 改正（mag −= A_λ，与光变表逻辑一致，
      extinction.correct_host_phot 只算不写）再算 M；gext_applied 标记该行是否应用了改正，
      gext_Alambda 为应用的银消量。坐标取宿主 ra/dec，缺省回退暂现源坐标，两者都缺
      （或依赖不可用/波段无波长）时按原始值并 gext_applied=false。
      mag_raw 为库中原始星等（原星等系统），mag_corr 为银消改正后、星等系统换算前的星等，
      供前端导出 CSV 时同时给出改正前后两列。
    m_star_points / sfr_points: [{tid, z, m_star|sfr}]，derived 有值的宿主每行一点；
      z 为 null 表示宿主无红移（前端散点图跳过，CSV 导出保留空值）。
    """
    sess = get_session()
    try:
        hosts = sess.query(HostGalaxy).all()
        n_hosts = len(hosts)
        n_transients = sess.query(func.count(Transient.id)).scalar()
        n_spec = sum(1 for h in hosts if h.redshift_type == 'spec')
        n_phot = sum(1 for h in hosts if h.redshift_type == 'phot')
        m_star, sfr = [], []
        # 每宿主一行的 (tid, z, 值)：z 为 None 表示宿主无红移（散点图跳过，CSV 导出保留空值）
        m_star_points, sfr_points = [], []
        for h in hosts:
            d = h.derived or {}
            z = h.redshift if (h.redshift is not None and h.redshift > 0) else None
            if d.get('m_star') is not None:
                m_star.append(d['m_star'])
                m_star_points.append({'tid': h.transient_id, 'z': z, 'm_star': d['m_star']})
            if d.get('sfr') is not None:
                sfr.append(d['sfr'])
                sfr_points.append({'tid': h.transient_id, 'z': z, 'sfr': d['sfr']})
        # 宿主测光 → 绝对星等（需宿主红移）
        filters = {f.id: f for f in sess.query(FilterDef).all()}

        def _vega2ab(band):
            f = filters.get(band) or filters.get(str(band).lower())
            return (f.vega2ab or 0.0) if f else 0.0

        # 暂现源坐标缓存（宿主缺坐标时回退用，惰性查询）
        t_coords = {}

        def _coords(h):
            if h.ra is not None and h.dec is not None:
                return h.ra, h.dec
            tid = h.transient_id
            if tid not in t_coords:
                t = sess.get(Transient, tid)
                t_coords[tid] = (t.ra, t.dec) if t is not None else (None, None)
            return t_coords[tid]

        abs_mag_points = []
        for h in hosts:
            z = h.redshift
            if z is None or z <= 0:
                continue
            dm = distance_modulus(z)
            if dm is None:
                continue
            phot = h.photometry or []
            # 有未改正行时整体算一次改正（尘埃图按坐标查一次）
            corr = None
            if any(isinstance(p, dict) and not p.get('gext_corr', False) for p in phot):
                ra, dec = _coords(h)
                res = correct_host_phot(sess, ra, dec, phot)
                if res.get('ok'):
                    corr = res['rows']
            for idx, p in enumerate(phot):
                if not isinstance(p, dict):
                    continue
                band, mag = p.get('band'), p.get('mag')
                if not band or mag is None:
                    continue
                try:
                    mag = float(mag)
                except (TypeError, ValueError):
                    continue
                mag_raw = mag
                row_mag_sys = str(p.get('mag_sys') or 'AB')
                row_gext_corr = bool(p.get('gext_corr', False))
                # 银消改正（在星等系统换算前减 A_λ；星等加性量，次序可交换）
                gext_applied = False
                gext_alambda = None
                if corr is not None and corr[idx]['applied']:
                    mag = corr[idx]['mag_corr']
                    gext_applied = True
                    gext_alambda = round(corr[idx]['A_lambda'], 4)
                mag_corr = mag
                mag_sys = row_mag_sys.strip().lower()
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
                    'gext_applied': gext_applied, 'gext_Alambda': gext_alambda,
                    'mag_raw': round(mag_raw, 4), 'mag_corr': round(mag_corr, 4),
                    'mag_sys': row_mag_sys, 'gext_corr': row_gext_corr,
                })
        return jsonify({
            'n_hosts': n_hosts,
            'n_transients': n_transients,
            'coverage': (n_hosts / n_transients) if n_transients else 0,
            'n_with_spec_z': n_spec,
            'n_with_phot_z': n_phot,
            'm_star': m_star,
            'sfr': sfr,
            'm_star_points': m_star_points,
            'sfr_points': sfr_points,
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
