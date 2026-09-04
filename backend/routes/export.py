"""
导出接口
GET /api/export/transients?format=csv&...   — 导出暂现源列表
GET /api/export/lightcurves/<tid>?format=csv — 导出单源光变
GET /api/export/host_photometry/<tid>?format=csv — 导出单源宿主测光（含改正后星等）
"""
import csv, io
from flask import Blueprint, request, Response
from app import get_session, require_export_auth
from models import Transient, Lightcurve, HostGalaxy
from extinction import correct_host_phot

export_bp = Blueprint('export', __name__)


@export_bp.route('/transients', methods=['GET'])
@require_export_auth
def export_transients():
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
        items = sess.query(Lightcurve).filter(
            Lightcurve.transient_id == tid
        ).order_by(Lightcurve.time).all()
        if fmt == 'json':
            return {'transient_id': tid, 'items': [r.to_dict() for r in items]}
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['time', 'time_err', 'time_unit', 'band', 'flux_density',
                         'flux_density_err', 'flux_density_unit', 'mag_system',
                         'upperlimit', 'gext_corr', 'gext_Alambda',
                         'mag_gextcor', 'mag_gextcor_err',
                         'flux_density_gextcor', 'flux_density_gextcor_err',
                         'telescope', 'instrument', 'reference'])
        for r in items:
            writer.writerow([
                r.time, r.time_err or '', r.time_unit, r.band, r.flux_density,
                r.flux_density_err or '', r.flux_density_unit, r.mag_system or '',
                'y' if r.upperlimit else 'n',
                'y' if r.gext_corr else 'n',
                r.gext_Alambda if r.gext_Alambda is not None else '',
                r.mag_gextcor if r.mag_gextcor is not None else '',
                r.mag_gextcor_err if r.mag_gextcor_err is not None else '',
                r.flux_density_gextcor if r.flux_density_gextcor is not None else '',
                r.flux_density_gextcor_err if r.flux_density_gextcor_err is not None else '',
                r.telescope or '', r.instrument or '',
                r.reference or ''
            ])
        csv_bytes = output.getvalue()
        return Response(
            csv_bytes,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={tid}_lc.csv'}
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
