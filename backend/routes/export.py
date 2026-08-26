"""
导出接口
GET /api/export/transients?format=csv&...   — 导出暂现源列表
GET /api/export/lightcurves/<tid>?format=csv — 导出单源光变
"""
import csv, io
from flask import Blueprint, request, Response
from app import get_session, require_export_auth
from models import Transient, Lightcurve

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
