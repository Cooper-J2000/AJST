"""
统计接口
GET /api/stats/overview    — 全目录概览
GET /api/stats/redshifts   — 红移分布
GET /api/stats/bands       — 波段覆盖统计
GET /api/stats/tags        — 标签 / 子标签目标数
GET /api/stats/hosts       — 宿主星系覆盖与参数分布
"""
from flask import Blueprint, jsonify, request
from sqlalchemy import func, distinct, text, select
from app import get_session
from models import (Transient, Lightcurve, FilterDef, HostGalaxy,
                    distance_modulus, prewarm_distance_modulus)
from extinction import correct_host_phot, filter_meta
import hashlib

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


def _hosts_data_etag(sess):
    """宿主统计的数据版本 token（ETag）。

    由宿主/暂现源的 updated_at 极值与行数、滤波器条数与各影响输出的属性拼成
    （3 条廉价聚合查询），任何影响 /api/stats/hosts 输出的写入都会改变它；
    浏览器重复请求可拿 304。
    """
    a = sess.execute(select(func.count(HostGalaxy.id),
                            func.max(HostGalaxy.updated_at))).one()
    b = sess.execute(select(func.count(Transient.id),
                            func.max(Transient.updated_at))).one()
    # filters 表没有 updated_at，凡参与输出的列都要逐项进 token：
    #   条数（增删）、gext_coeff（A_λ）、vega2ab（Vega→AB 换算）、wavelength
    #   （gext_coeff 为 NULL 时 A_λ 由波长现算）。漏项会让客户端拿到陈旧 304——
    #   实测改 vega2ab 后响应体变了而 ETag 不变。
    c = sess.execute(select(func.count(FilterDef.id),
                            func.sum(FilterDef.gext_coeff),
                            func.sum(FilterDef.vega2ab),
                            func.sum(FilterDef.wavelength))).one()
    return hashlib.sha1('|'.join(str(x) for x in (*a, *b, *c)).encode()).hexdigest()


@stats_bp.route('/hosts', methods=['GET'])
def host_stats():
    """宿主星系统计：覆盖率、红移类型计数、M*/SFR 分布、宿主测光绝对星等点。

    abs_mag_points: [{tid, band, z, mag(AB), abs_mag, mag_err, err_assumed, upperlimit,
                      gext_applied, gext_Alambda, mag_raw, mag_corr, mag_sys, gext_corr}]
      M = m_AB − μ(z_host)，μ 取 `host_galaxies.gext_distmod`（v2.19.3 起持久化），
      缺值时回退 `models.distance_modulus`（astropy Planck18，批量预热）；
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
        etag = _hosts_data_etag(sess)
        if request.if_none_match.contains(etag):
            # 数据未变：直接 304，不做任何计算/序列化
            return '', 304, {'ETag': '"%s"' % etag, 'Cache-Control': 'no-cache'}
        # 显式 ORDER BY：宿主是无序查询时返回的行序会随堆内物理序变化（一次写入就变），
        # 导致同一份数据两次请求的 abs_mag_points / m_star_points 数组顺序不同（前端导出 CSV 会抖）
        hosts = sess.query(HostGalaxy).order_by(HostGalaxy.transient_id).all()
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
        # 滤波器元数据用进程内缓存（extinction.filter_meta，免每次解码 transmission）
        filters = filter_meta(sess)

        def _vega2ab(band):
            f = filters.get(band) or filters.get(str(band).lower())
            return (f['vega2ab'] or 0.0) if f else 0.0

        # 暂现源坐标 + E(B-V) 缓存：一次性预取，避免逐行 sess.get(Transient)（N+1）
        t_rows = {r.id: r for r in sess.execute(
            select(Transient.id, Transient.ra, Transient.dec, Transient.gext_ebv)
            .where(Transient.id.in_([h.transient_id for h in hosts]))).all()}

        def _coords(h):
            if h.ra is not None and h.dec is not None:
                return h.ra, h.dec
            t = t_rows.get(h.transient_id)
            return (t.ra, t.dec) if t is not None else (None, None)

        def _ebv(h):
            """E(B-V) 缓存：必须与 _coords(h) 返回的坐标同源，避免跨坐标系取值。

            宿主 ra/dec 齐全 → 宿主行缓存（NULL 时由 correct_host_phot 按宿主
            坐标现查尘图）；坐标回退暂现源 → 才用暂现源行缓存。
            """
            if h.ra is not None and h.dec is not None:
                return h.gext_ebv
            t = t_rows.get(h.transient_id)
            return None if t is None else t.gext_ebv

        abs_mag_points = []
        # 距离模数批量预热（未命中的 z 一次向量化），随后逐行查询全部命中缓存
        prewarm_distance_modulus([h.redshift for h in hosts])
        for h in hosts:
            z = h.redshift
            if z is None or z <= 0:
                continue
            dm = h.gext_distmod if h.gext_distmod is not None else distance_modulus(z)
            if dm is None:
                continue
            phot = h.photometry or []
            # 有未改正行时整体算一次改正（E(B-V)/滤波器系数已缓存，不再查尘图/重查滤波器表）
            corr = None
            if any(isinstance(p, dict) and not p.get('gext_corr', False) for p in phot):
                ra, dec = _coords(h)
                res = correct_host_phot(sess, ra, dec, phot, ebv=_ebv(h))
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
        resp = jsonify({
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
        resp.set_etag(etag)
        resp.cache_control.no_cache = True   # 每次带 If-None-Match 复验，命中即 304
        return resp
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
