"""specphot.meta —— API-1（首屏 meta）/ API-5（曲线点集）/ API-6（health）/ API-8（按坐标查 E(B−V)）。

API-4 的未到期占位已随 P3 切片 2 删除（真实现迁 lines_api.py 的 POST /line；
API-2=photometry.py、API-3=continuum_api.py 同此先例）。

库内谱装载走 reader.load_catalog_spectrum：本文件只负责把「Spectrum 记录 + 文件」
装配成与宿主 GET /api/spectra/<id> 等价的 payload（字符串行清洗 +
wavconvert.to_vacuum_points 转真空，只用宿主公开函数），DB 一律短会话（ST-1 同纪律）。
"""
import json
import os
import time

from flask import jsonify, request

from app import get_session
from models import Spectrum, FilterDef, Lightcurve, Transient
import coords
import extinction
import wavconvert

from . import (specphot_bp, SPEC_PHOT_VERSION, _compute_gate, _err,
               _load_error_response, probe_deps, gate_status, cache_stats)
from . import registry
from .reader import load_catalog_spectrum, SpecLoadError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPECTRA_DIR_PREFIX = os.path.join(PROJECT_ROOT, 'catadata', 'spectra') + os.sep


# ─── 库内谱 payload 装配（等价宿主 routes/spectra.py 的 GET 路径） ──────

def _read_spectrum_payload(rec):
    """Spectrum 记录 → {'meta': rec.to_dict(), 'data': <清洗+转真空后的文件 JSON>}。

    与宿主 GET 同两步：① float() 清洗数据行（口径同 _coerce_spec_data，
    这里不重数 n_dropped —— reader 的 V-3 计数面向原始行，GET 路径本就已清洗）；
    ② wavconvert.to_vacuum_points 按 wavelength_type 转真空（F-79① 的唯一换算点）。
    """
    path = os.path.normpath(os.path.join(PROJECT_ROOT, rec.file_path))
    if not path.startswith(SPECTRA_DIR_PREFIX) or not os.path.exists(path):
        raise SpecLoadError('E-01', f'光谱文件缺失: {rec.file_path}', status=404)
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    for obj in data.values():
        sp = obj.get('spectra') if isinstance(obj, dict) else None
        if not sp or 'data' not in sp:
            continue
        rows = []
        for d in sp['data']:
            try:
                row = [float(d[0]), float(d[1])]
                if len(d) > 2 and d[2] not in (None, ''):
                    row.append(float(d[2]))
                rows.append(row)
            except (TypeError, ValueError, IndexError):
                continue
        sp['data'], _ = wavconvert.to_vacuum_points(
            rows, rec.wavelength_type, sp.get('u_wavelengths'))
    return {'meta': rec.to_dict(), 'data': data}


def load_spectrum_by_id(spectrum_id):
    """库内谱 → LoadedSpectrum（含源记录）；不存在/不可读 ⇒ SpecLoadError(E-01)。"""
    sess = get_session()
    try:
        rec = sess.query(Spectrum).filter(Spectrum.id == spectrum_id).first()
        if rec is None:
            raise SpecLoadError('E-01', f'光谱 {spectrum_id} 不存在', status=404)
        t = sess.query(Transient).filter(Transient.id == rec.transient_id).first()
        source_record = None
        if t is not None:
            source_record = {'tid': t.id, 'ra': t.ra, 'dec': t.dec,
                             'redshift': t.redshift}
    finally:
        sess.close()
    payload = _read_spectrum_payload(rec)
    return load_catalog_spectrum(payload, source_record=source_record)


# ─── API-1：首屏 meta ─────────────────────────────────────────────────

def _bands_payload():
    """全部波段 + 曲线登记口径（DB 短会话；curve_kind 走代码侧 registry，M-1）。"""
    sess = get_session()
    try:
        rows = sess.query(FilterDef).order_by(FilterDef.wavelength).all()
        items = []
        for r in rows:
            tr = (r.extra_data or {}).get('transmission') or {}
            has_curve = bool(tr.get('wl') and tr.get('tr'))
            items.append({
                'id': r.id, 'wavelength': r.wavelength, 'filter_type': r.filter_type,
                'vega2ab': r.vega2ab, 'gext_coeff': r.gext_coeff,
                'description': r.description, 'has_curve': has_curve,
                'curve_kind': registry.curve_kind(r.id) if has_curve else None,
                'source_note': registry.source_note(r.id) if has_curve else None,
            })
    finally:
        sess.close()
    coverage = {
        'n_bands': len(items),
        'n_with_curve': sum(1 for b in items if b['has_curve']),
        'n_registered': len(registry.CURVE_REGISTRY),
        'unregistered_with_curve': [b['id'] for b in items
                                    if b['has_curve'] and b['curve_kind'] is None],
        'registry_fingerprint': registry.registry_fingerprint(),
    }
    return items, coverage


def _n_catalog_anchors(transient_id):
    """可配对锚点数（anchor_origin='catalog'）：该源有曲线波段内的非上限、
    未丢弃星等点。F-24 的时刻配对由 photometry 切片细化，这里只给首屏计数。"""
    if not transient_id:
        return 0
    sess = get_session()
    try:
        return (sess.query(Lightcurve)
                .filter(Lightcurve.transient_id == transient_id,
                        Lightcurve.flux_density_unit == 'mag',
                        Lightcurve.discard.is_(False),
                        Lightcurve.upperlimit.is_(False),
                        Lightcurve.band.in_(registry.registered_filter_ids()))
                .count())
    finally:
        sess.close()


@specphot_bp.route('/meta', methods=['GET'])
def meta():
    """API-1：可用波段 + curve_coverage + 注册表版本；带 spectrum_id 时追加谱级口径。"""
    bands, coverage = _bands_payload()
    body = {'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': [],
            'bands': bands, 'curve_coverage': coverage,
            'requires_spectrum': True, 'spectrum': None}
    sid = request.args.get('spectrum_id', type=int)
    if sid is None:
        return jsonify(body)
    try:
        ls = load_spectrum_by_id(sid)
    except SpecLoadError as e:
        return _load_error_response(e)
    body['requires_spectrum'] = False
    body['warnings'] = ls['warnings']
    body['spectrum'] = {
        'spectrum_id': sid, 'tid': ls['tid'], 'spec_hash': ls['spec_hash'],
        'lambda_frame': ls['meta']['lambda_frame'],
        'lambda_frame_converted': ls['lambda_frame_converted'],
        'flux_median_cgs': ls['flux_median_cgs'],
        'flux_type': ls['meta']['flux_type'], 'u_fluxes': ls['meta']['u_fluxes'],
        'n_points': ls['read_stats']['n_points'],
        'read_stats': ls['read_stats'],
        'meta': ls['meta'], 'meta_provenance': ls['meta_provenance'],
        'n_catalog_anchors': _n_catalog_anchors(ls['tid']),
    }
    return jsonify(body)


# ─── API-5：曲线点集 + 登记口径 ────────────────────────────────────────

@specphot_bp.route('/curve/<filter_id>', methods=['GET'])
def curve(filter_id):
    sess = get_session()
    try:
        rec = sess.query(FilterDef).filter(FilterDef.id == filter_id).first()
        tr = ((rec.extra_data or {}).get('transmission') or {}) if rec else {}
        wl, t = tr.get('wl'), tr.get('tr')
        wavelength = rec.wavelength if rec else None
    finally:
        sess.close()
    if rec is None:
        return _err('bad_request_state', f'未知波段: {filter_id}',
                    400, reason='curve_invalid')
    if not wl or not t:
        return _err('band_unusable',
                    f'波段 {filter_id} 无透射曲线（该波段走 E-04/mono 路径）',
                    400, reason='curve_missing')
    return jsonify({'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': [],
                    'filter_id': filter_id, 'wavelength': wavelength,
                    'curve_kind': registry.curve_kind(filter_id),
                    'source_note': registry.source_note(filter_id),
                    'n_points': len(wl), 'lam_aa': wl, 't': t})


# ─── API-6：health ─────────────────────────────────────────────────────

@specphot_bp.route('/health', methods=['GET'])
def health():
    return jsonify({'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': [],
                    'deps': probe_deps(), 'gate': gate_status(),
                    'cache': cache_stats()})


# ─── API-8：按坐标查 E(B−V)（A-8：全站第一个按坐标的消光端点） ─────────

@specphot_bp.route('/ebv', methods=['POST'])
def ebv():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _err('bad_request_state', '请求体应为 JSON 对象',
                    400, reason='bad_coordinates')
    try:
        ra = coords.parse_ra(body.get('ra'))
        dec = coords.parse_dec(body.get('dec'))
    except (ValueError, TypeError):
        ra = dec = None
    if ra is None or dec is None:
        return _err('bad_request_state',
                    '坐标无法解析（示例：ra="12:34:56.7", dec="-05:06:07"，'
                    '或十进制度 ra=188.7362, dec=-5.102）',
                    400, reason='bad_coordinates')

    # ST-15：首次调用触发尘埃图惰性装载（冷启动可达数秒，只回显不中断）；
    # 装载失败/图不可用 ⇒ 200 + available=false + CA-23，不 500、不重试（A-8/ST-11）
    was_cold = extinction._csfd_query is None and extinction._import_error is None
    t0 = time.perf_counter()
    try:
        st = extinction.status()
        if not st['available']:
            raise _DustUnavailable(st.get('error'))
        value = extinction.get_ebv(ra, dec)
    except Exception as e:  # noqa: BLE001 - 装载/查询失败一律降级，不外泄 traceback
        err_msg = e.msg if isinstance(e, _DustUnavailable) else type(e).__name__
        return jsonify({'spec_phot_version': SPEC_PHOT_VERSION,
                        'available': False, 'ebv': None, 'rv': 3.1, 'law': 'P92',
                        'alambda_ref': None, 'cold_start_ms': 0,
                        'detail': err_msg,
                        'warnings': [{'code': 'CA-23',
                                      'message': f'尘埃图不可用（{err_msg}），'
                                                 '银河消光只能按未改正出数'}]})
    cold_ms = int((time.perf_counter() - t0) * 1000) if was_cold else 0

    # alambda_ref：V 带的 A_λ = E(B−V) × k(λ_V)，k 走宿主 dust_coeff（P92）
    alambda_ref = None
    sess = get_session()
    try:
        vband = sess.query(FilterDef).filter(FilterDef.id == 'V').first()
        v_wl = vband.wavelength if vband else None
    finally:
        sess.close()
    if v_wl:
        k_v = extinction.dust_coeff(v_wl)
        if k_v:
            alambda_ref = {'V': round(value * k_v, 3)}
    return jsonify({'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': [],
                    'available': True, 'ebv': round(value, 4), 'rv': 3.1, 'law': 'P92',
                    'alambda_ref': alambda_ref, 'cold_start_ms': cold_ms})


class _DustUnavailable(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg or 'unknown'
