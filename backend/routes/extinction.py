"""
银河系消光改正
GET  /api/extinction/status            — 功能可用性 + 改正覆盖统计
POST /api/extinction/run               — 执行改正（body: {} 全部源 / {"transient_id": "..."} 单源 / {"lightcurve_id": N} 单点）
POST /api/extinction/clear             — 清除改正结果（参数同 run）
"""
from flask import Blueprint, request, jsonify
from sqlalchemy import func
from app import get_session, require_admin
from models import Transient, Lightcurve, FilterDef
import extinction

extinction_bp = Blueprint('extinction', __name__)


@extinction_bp.route('/status', methods=['GET'])
def get_status():
    sess = get_session()
    try:
        st = extinction.status()
        st['n_corrected'] = sess.query(func.count(Lightcurve.id)).filter(
            Lightcurve.gext_corr.is_(True)).scalar()
        # 可改正的光学数据点：源有坐标 + 波段在滤波器表中
        n_eligible = sess.query(func.count(Lightcurve.id)).join(
            Transient, Lightcurve.transient_id == Transient.id
        ).join(FilterDef, Lightcurve.band == FilterDef.id).filter(
            Transient.ra.isnot(None), Transient.dec.isnot(None),
            Lightcurve.flux_density.isnot(None),
        ).scalar()
        st['n_eligible'] = n_eligible
        return jsonify(st)
    finally:
        sess.close()


@extinction_bp.route('/run', methods=['POST'])
@require_admin
def run_extinction():
    body = request.get_json(silent=True) or {}
    tid = body.get('transient_id')
    lc_id = body.get('lightcurve_id')
    sess = get_session()
    try:
        stats = extinction.run(sess, transient_id=tid, lightcurve_id=lc_id)
        if not stats.get('ok'):
            return {'error': stats.get('error', 'unknown error')}, 500
        sess.commit()
        return jsonify(stats)
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 500
    finally:
        sess.close()


@extinction_bp.route('/clear', methods=['POST'])
@require_admin
def clear_extinction():
    body = request.get_json(silent=True) or {}
    tid = body.get('transient_id')
    lc_id = body.get('lightcurve_id')
    sess = get_session()
    try:
        q = sess.query(Lightcurve).filter(Lightcurve.gext_corr.is_(True))
        if lc_id is not None:
            q = q.filter(Lightcurve.id == lc_id)
        elif tid is not None:
            q = q.filter(Lightcurve.transient_id == tid)
        rows = q.all()
        for lc in rows:
            extinction.clear_point(lc)
        sess.commit()
        return jsonify({'ok': True, 'cleared': len(rows)})
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 500
    finally:
        sess.close()
