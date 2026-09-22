"""
导出接口
GET /api/export/transients?format=csv&...   — 导出暂现源列表
GET /api/export/lightcurves/<tid>?format=csv[&t_ref=...] — 导出单源光变（全列 + MJD）
GET /api/export/host_photometry/<tid>?format=csv — 导出单源宿主测光（含改正后星等）

光变导出的 t_ref（基准时刻，决定 time 列 = (MJD − t_ref)×86400 秒）：
  缺省或 't0' → 该源 T0；纯数字 → 视为 MJD；否则按 ISO UTC 时间解析。
  MJD 列始终为绝对值，不随 t_ref 变化；无 MJD 的行（源无 T0）time 保持库中原值。
"""
import csv, io
from datetime import datetime
from flask import Blueprint, request, Response
from app import get_session, require_export_auth
from models import Transient, Lightcurve, HostGalaxy, t0_to_mjd
from extinction import correct_host_phot

export_bp = Blueprint('export', __name__)


def _parse_t_ref(raw, t0_mjd):
    """基准时刻 → MJD。返回 (ref_mjd, label)；缺省/异常回退 T0。"""
    if not raw or raw.strip().lower() == 't0':
        return t0_mjd, 't0'
    s = raw.strip()
    try:
        return float(s), f'mjd:{s}'
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return t0_to_mjd(dt), f'utc:{dt.isoformat()}'
    except (ValueError, TypeError):
        return t0_mjd, 't0'


@export_bp.route('/transients', methods=['GET'])
@require_export_auth
def export_transients():
    # NOTE(增长点): JSON 分支对全表逐行 to_dict（含 comment/extra_data 大字段），
    # 无分页。当前规模（数百源）可接受；源数量上万后应加分页或改用 CSV。
    fmt = request.args.get('format', 'csv')
    sess = get_session()
    try:
        items = sess.query(Transient).order_by(Transient.id).all()
        if fmt == 'json':
            return {'items': [t.to_dict() for t in items]}
        # CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['id', 'ra', 'dec', 't0', 'redshift', 'redshift_type',
                         'tags', 'aliases', 'trigger_instrument'])
        for t in items:
            t0 = t.t0.isoformat() if t.t0 else ''
            writer.writerow([
                t.id, t.ra or '', t.dec or '', t0, t.redshift or '',
                t.redshift_type or '', ';'.join(t.tags or []),
                ';'.join(t.aliases or []), t.trigger_instrument or ''
            ])
        csv_bytes = output.getvalue()
        return Response(
            csv_bytes,
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=transients.csv'}
        )
    finally:
        sess.close()


@export_bp.route('/lightcurves/<tid>', methods=['GET'])
@require_export_auth
def export_lightcurves(tid):
    fmt = request.args.get('format', 'csv')
    sess = get_session()
    try:
        t = sess.query(Transient).filter(Transient.id == tid).first()
        if not t:
            return {'error': 'Not found'}, 404
        t0_mjd = t0_to_mjd(t.t0)
        ref_mjd, ref_label = _parse_t_ref(request.args.get('t_ref'), t0_mjd)
        items = sess.query(Lightcurve).filter(
            Lightcurve.transient_id == tid
        ).order_by(Lightcurve.time).all()

        # 基准时刻即 T0（缺省）时直接用库存 time（缓存列原值，避免经 MJD 往返的浮点噪声）
        rebase = ref_mjd is not None and ref_mjd != t0_mjd

        def _time_at_ref(r):
            """time 列按基准时刻重算（有 MJD 时）；无 MJD 的行保持库中原值。"""
            if rebase and r.mjd is not None:
                return (r.mjd - ref_mjd) * 86400.0
            return r.time

        if fmt == 'json':
            out = []
            for r in items:
                d = r.to_dict()
                d['time'] = _time_at_ref(r)
                out.append(d)
            return {'transient_id': tid, 't_ref': ref_label,
                    't_ref_mjd': ref_mjd, 'items': out}
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['time', 'time_err', 'time_unit', 'mjd', 'band',
                         'flux_density', 'flux_density_err', 'flux_density_unit',
                         'mag_system', 'gext_corr', 'upperlimit', 'host_subtracted',
                         'gext_Alambda', 'mag_gextcor', 'mag_gextcor_err',
                         'flux_density_gextcor', 'flux_density_gextcor_err',
                         'flux_density_gextcor_unit', 'weights', 'discard',
                         'telescope', 'instrument', 'reference', 'comment',
                         'source'])
        for r in items:
            writer.writerow([
                _time_at_ref(r), r.time_err or '', r.time_unit,
                r.mjd if r.mjd is not None else '', r.band, r.flux_density,
                r.flux_density_err if r.flux_density_err is not None else '',
                r.flux_density_unit, r.mag_system or '',
                'y' if r.gext_corr else 'n',
                'y' if r.upperlimit else 'n',
                '' if r.host_subtracted is None else ('y' if r.host_subtracted else 'n'),
                r.gext_Alambda if r.gext_Alambda is not None else '',
                r.mag_gextcor if r.mag_gextcor is not None else '',
                r.mag_gextcor_err if r.mag_gextcor_err is not None else '',
                r.flux_density_gextcor if r.flux_density_gextcor is not None else '',
                r.flux_density_gextcor_err if r.flux_density_gextcor_err is not None else '',
                r.flux_density_gextcor_unit or '',
                r.weights if r.weights is not None else '',
                'y' if r.discard else 'n',
                r.telescope or '', r.instrument or '',
                r.reference or '', r.comment or '', r.source or ''
            ])
        csv_bytes = output.getvalue()
        return Response(
            csv_bytes,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={tid}_lc.csv',
                     'X-AJST-Tref': ref_label}
        )
    finally:
        sess.close()


@export_bp.route('/host_photometry/<tid>', methods=['GET'])
@require_export_auth
def export_host_photometry(tid):
    """导出宿主星系测光表。

    mag_gextcor 为银河系消光改正后星等（原星等系统不变）：gext_corr=true 的行
    mag_gextcor=mag；未改正行实时按 CSFD+Rv3.1+P92 计算（mag − A_λ，
    extinction.correct_host_phot 只算不写），无法改正（无坐标/波段无波长/
    依赖不可用）时留空。坐标取宿主 ra/dec，缺省回退暂现源坐标。
    """
    fmt = request.args.get('format', 'csv')
    sess = get_session()
    try:
        host = sess.query(HostGalaxy).filter_by(transient_id=tid).first()
        if host is None:
            return {'error': 'no host data'}, 404
        phot = host.photometry or []
        corr = {'ok': False, 'rows': [{} for _ in phot], 'ebv': None}
        if any(isinstance(p, dict) and not p.get('gext_corr', False) for p in phot):
            ra, dec = host.ra, host.dec
            if ra is None or dec is None:
                t = sess.get(Transient, tid)
                if t is not None:
                    if ra is None:
                        ra = t.ra
                    if dec is None:
                        dec = t.dec
            corr = correct_host_phot(sess, ra, dec, phot)
        rows = []
        for p, c in zip(phot, corr['rows']):
            if not isinstance(p, dict):
                continue
            gexted = bool(p.get('gext_corr', False))
            try:
                mag = float(p.get('mag')) if p.get('mag') is not None else None
            except (TypeError, ValueError):
                mag = None
            if gexted or mag is None:
                mag_gc, alam = mag, ''       # 已改正行原样；无 mag 行留空
            elif corr.get('ok') and c.get('applied'):
                mag_gc = round(c['mag_corr'], 4)
                alam = round(c['A_lambda'], 4)
            else:
                mag_gc, alam = '', ''        # 未改正但无法计算改正
            rows.append({
                'band': p.get('band'), 'mag': mag,
                'mag_err': p.get('mag_err'),
                'mag_sys': p.get('mag_sys') or 'AB',
                'upperlimit': bool(p.get('upperlimit')),
                'gext_corr': gexted,
                'gext_Alambda': alam,
                'mag_gextcor': mag_gc,
                'source': p.get('source'),
            })
        if fmt == 'json':
            return {'transient_id': tid, 'ebv': corr.get('ebv'), 'items': rows}
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['band', 'mag', 'mag_err', 'mag_sys', 'upperlimit',
                         'gext_corr', 'gext_Alambda', 'mag_gextcor', 'source'])
        for r in rows:
            writer.writerow([
                r['band'], r['mag'] if r['mag'] is not None else '',
                r['mag_err'] if r['mag_err'] is not None else '',
                r['mag_sys'],
                'y' if r['upperlimit'] else 'n',
                'y' if r['gext_corr'] else 'n',
                r['gext_Alambda'],
                r['mag_gextcor'] if r['mag_gextcor'] != '' else '',
                r['source'] or '',
            ])
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={tid}_host_phot.csv'}
        )
    finally:
        sess.close()
