"""
光谱数据
GET  /api/spectra?transient_id=X  — 某源的光谱元数据列表（含改正子谱 parent_id/gext_corr）
GET  /api/spectra/<id>            — 单条光谱完整数据（波长-流量数组）
GET  /api/spectra/<id>/download   — 下载为两/三列文本（# 头元数据）
POST /api/spectra/upload          — 上传光谱（需登录），支持两种格式：
  1) 两列/三列文本：波长(Å) 流量 [流量误差]，# 注释行可带 key=value 头
  2) OpenSNSpectra 风格 JSON: {"<名称>": {"spectra": {..., "data": [[wl, flux], ...]}}}
POST /api/spectra/<id>/gext_correct — 生成/覆盖该原始谱的银河系消光改正谱（登录用户）
  服务端校验后统一规范化为 JSON 存储（波长 Å，流量 erg/s/cm^2/Å）
  改正谱为依附原始谱的二级产物（parent_id），原始谱删除时级联删除
"""
import json, os, re
from datetime import datetime, timedelta
from urllib.parse import quote
from flask import Blueprint, jsonify, request, Response
from app import get_session, require_auth, require_admin
from models import Spectrum, Transient
import extinction
import wavconvert

spectra_bp = Blueprint('spectra', __name__)

# 项目根目录（backend 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPECTRA_DIR = os.path.join(PROJECT_ROOT, 'catadata', 'spectra')
# 防路径穿越判据前缀：带分隔符，避免 catadata/spectra-evil 等同前缀目录通过
SPECTRA_DIR_PREFIX = SPECTRA_DIR + os.sep
MJD_EPOCH = datetime(1858, 11, 17)

MIN_POINTS = 10
WL_MIN, WL_MAX = 100.0, 1e7  # Å，合理波长范围


@spectra_bp.route('', methods=['GET'])
def list_spectra():
    sess = get_session()
    try:
        q = sess.query(Spectrum)
        tid = request.args.get('transient_id')
        if tid:
            q = q.filter(Spectrum.transient_id == tid)
        rows = q.order_by(Spectrum.observation_date).all()
        return jsonify([r.to_dict() for r in rows])
    finally:
        sess.close()


@spectra_bp.route('/<int:spec_id>', methods=['GET'])
def get_spectrum(spec_id):
    sess = get_session()
    try:
        r = sess.query(Spectrum).filter(Spectrum.id == spec_id).first()
        if not r:
            return {'error': 'Not found'}, 404
        path = os.path.normpath(os.path.join(PROJECT_ROOT, r.file_path))
        # 防路径穿越：只允许 catadata/spectra 下
        if not path.startswith(SPECTRA_DIR_PREFIX):
            return {'error': 'invalid path'}, 400
        if not os.path.exists(path):
            return {'error': 'spectrum file missing', 'file_path': r.file_path}, 404
        with open(path) as f:
            data = json.load(f)
        _coerce_spec_data(data)
        # 返回前把波长列统一转真空：wavelength_type 为 'air' 或 NULL（留空）
        # 时按空气波长处理做转换（仅 ≥2000 Å、单位 Å）；'vacuum' 不转。
        # 库存文件与下载接口保持原始波长不动，转换只在读/处理路径。
        converted = False
        for obj in data.values():
            sp = obj.get('spectra') if isinstance(obj, dict) else None
            if sp and sp.get('data'):
                sp['data'], c = wavconvert.to_vacuum_points(
                    sp['data'], r.wavelength_type, sp.get('u_wavelengths'))
                converted = converted or c
        meta = r.to_dict()
        meta['wavelength_converted'] = converted
        return jsonify({'meta': meta, 'data': data})
    finally:
        sess.close()


def _coerce_spec_data(data):
    """部分来源 JSON 的数值是字符串（如 OpenSNSpectra），统一转为数值类型"""
    for obj in data.values():
        sp = obj.get('spectra') if isinstance(obj, dict) else None
        if not sp or 'data' not in sp:
            continue
        coerced = []
        for d in sp['data']:
            try:
                row = [float(d[0]), float(d[1])]
                if len(d) > 2 and d[2] not in (None, ''):
                    row.append(float(d[2]))
                coerced.append(row)
            except (TypeError, ValueError, IndexError):
                continue
        sp['data'] = coerced


@spectra_bp.route('/<int:spec_id>/download', methods=['GET'])
def download_spectrum(spec_id):
    """导出为纯文本：# 注释行元数据 + 两/三列（波长Å 流量 [误差]）"""
    sess = get_session()
    try:
        r = sess.query(Spectrum).filter(Spectrum.id == spec_id).first()
        if not r:
            return {'error': 'Not found'}, 404
        path = os.path.normpath(os.path.join(PROJECT_ROOT, r.file_path))
        if not path.startswith(SPECTRA_DIR_PREFIX):
            return {'error': 'invalid path'}, 400
        if not os.path.exists(path):
            return {'error': '光谱文件缺失', 'file_path': r.file_path}, 404
        with open(path) as f:
            data = json.load(f)
        _coerce_spec_data(data)
        obj_name, obj = next(iter(data.items()))
        sp = obj.get('spectra', obj) if isinstance(obj, dict) else {}

        meta = [
            ('source', 'AJST Transient Catalog'),
            ('transient', r.transient_id),
            ('filename', r.filename),
            ('instrument', sp.get('instrument') or r.instrument),
            ('MJD', sp.get('time')),
            ('observer', sp.get('observer')),
            ('reducer', sp.get('reducer')),
            ('u_fluxes', sp.get('u_fluxes')),
            ('u_wavelengths', sp.get('u_wavelengths') or 'Angstrom'),
            ('spec_type', r.spec_type),
            # 下载保持原始存储波长不动，仅在此标注波长类型供下游自行判断
            ('wavelength_type', r.wavelength_type or 'null（按空气波长处理）'),
        ]
        if sp.get('gext_corr'):
            meta.append(('gext_corr',
                         f"true (parent={sp.get('parent_filename')}, "
                         f"E(B-V)={sp.get('gext_ebv')}, Rv={sp.get('gext_rv', '3.1')}, "
                         f"CSFD dust map + Pei1992, at={sp.get('gext_at')})"))
        lines = [f'# {k}: {v}' for k, v in meta if v not in (None, '')]
        lines.append('# columns: wavelength(Angstrom) flux [flux_err]')
        for d in sp.get('data') or []:
            line = f'{d[0]:.6g} {d[1]:.8e}'
            if len(d) > 2:
                line += f' {d[2]:.8e}'
            lines.append(line)

        dl_name = r.filename + '.txt'
        resp = Response('\n'.join(lines) + '\n', mimetype='text/plain; charset=utf-8')
        resp.headers['Content-Disposition'] = (
            f"attachment; filename=\"{dl_name}\"; "
            f"filename*=UTF-8''{quote(dl_name)}")
        return resp
    finally:
        sess.close()


# ─── 上传 ───

class UploadError(ValueError):
    pass


def _sanitize_filename(fn):
    fn = os.path.basename(fn.strip())
    if not re.match(r'^[A-Za-z0-9._\-]+$', fn):
        raise UploadError('文件名只允许字母、数字、点、下划线、短横线')
    return fn


def _to_float(tok):
    try:
        v = float(tok)
        return v if v == v and abs(v) != float('inf') else None
    except (TypeError, ValueError):
        return None


def _mjd_to_dt(mjd):
    return MJD_EPOCH + timedelta(days=float(mjd))


def _parse_text_spectrum(content):
    """解析两列/三列文本 + # 头（key=value）；三列时第三列为流量误差"""
    meta, points = {}, []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            m = re.match(r'#+\s*([A-Za-z_ ]+)\s*[:=]\s*(.+)', line)
            if m:
                meta[m.group(1).strip().lower().replace(' ', '_')] = m.group(2).strip()
            continue
        toks = re.split(r'[,\s]+', line)
        if len(toks) < 2:
            raise UploadError(f'无法解析的行（需两列或三列）: {line[:60]}')
        wl, fl = _to_float(toks[0]), _to_float(toks[1])
        if wl is None or fl is None:
            raise UploadError(f'数值无法解析: {line[:60]}')
        err = _to_float(toks[2]) if len(toks) >= 3 else None
        points.append((wl, fl, err))
    return meta, points


def _validate_points(points):
    if len(points) < MIN_POINTS:
        raise UploadError(f'数据点太少（{len(points)} < {MIN_POINTS}）')
    points = sorted(points)
    for p in points:
        if not (WL_MIN < p[0] < WL_MAX):
            raise UploadError(f'波长 {p[0]} Å 超出合理范围 ({WL_MIN}-{WL_MAX} Å)')
    dedup = []
    for p in points:
        if dedup and p[0] == dedup[-1][0]:
            dedup[-1] = p
        else:
            dedup.append(p)
    # 统一为 [wl, flux] 或 [wl, flux, err]
    return [[p[0], p[1]] + ([p[2]] if p[2] is not None else []) for p in dedup]


def _parse_upload(content):
    """返回 (sp_dict, obj_name)；sp_dict 为规范化 spectra 结构"""
    text = content.strip()
    if text.startswith('{'):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise UploadError(f'JSON 解析失败: {e}')
        if not isinstance(data, dict) or not data:
            raise UploadError('JSON 顶层应为 {"<名称>": {"spectra": {...}}}')
        obj_name, obj = next(iter(data.items()))
        sp = obj.get('spectra') if isinstance(obj, dict) else None
        if not sp or 'data' not in sp:
            raise UploadError('JSON 缺少 <名称>.spectra.data 字段')
        points = []
        for d in sp['data']:
            if not isinstance(d, (list, tuple)) or len(d) < 2:
                raise UploadError('data 元素应为 [波长, 流量] 或 [波长, 流量, 流量误差] 数组')
            wl, fl = _to_float(d[0]), _to_float(d[1])
            if wl is None or fl is None:
                raise UploadError(f'data 含非数值元素: {d}')
            err = _to_float(d[2]) if len(d) >= 3 else None
            points.append((wl, fl, err))
        points = _validate_points(points)
        sp_out = dict(sp)
        sp_out['data'] = points
        return sp_out, obj_name
    else:
        meta, points = _parse_text_spectrum(text)
        points = _validate_points(points)
        sp = {
            'time': meta.get('mjd') or meta.get('time'),
            'instrument': meta.get('instrument'),
            'observer': meta.get('observer'),
            'reducer': meta.get('reducer'),
            'u_fluxes': meta.get('u_fluxes', 'erg/s/cm^2/Angstrom'),
            'u_wavelengths': meta.get('u_wavelengths', 'Angstrom'),
            'u_time': 'MJD',
            'data': points,
        }
        return sp, None


@spectra_bp.route('/upload', methods=['POST'])
@require_auth
def upload_spectrum():
    body = request.get_json(force=True)
    tid = (body.get('transient_id') or '').strip()
    content = body.get('content') or ''
    if not tid or not content.strip():
        return {'error': 'transient_id 和 content 不能为空'}, 400

    sess = get_session()
    try:
        t = sess.query(Transient).filter(Transient.id == tid).first()
        if not t:
            return {'error': f'暂现源 {tid} 不存在'}, 404
        try:
            filename = _sanitize_filename(body.get('filename') or 'upload.dat')
            sp, obj_name = _parse_upload(content)
        except UploadError as e:
            return {'error': str(e)}, 400

        if not filename.endswith('.dat') and not filename.endswith('.txt'):
            filename += '.dat'
        if sess.query(Spectrum).filter(
                Spectrum.transient_id == tid, Spectrum.filename == filename).first():
            return {'error': f'该源下已存在同名光谱 {filename}'}, 409

        # 表单字段优先于文件内元数据
        instrument = body.get('instrument') or sp.get('instrument')
        observer = body.get('observer') or sp.get('observer')
        reducer = body.get('reducer') or sp.get('reducer')
        mjd = body.get('mjd') or sp.get('time')
        # 流量类型：absolute（绝对流量）/ normalized（归一化流量）
        flux_type = body.get('flux_type') or 'absolute'
        if flux_type not in ('absolute', 'normalized'):
            return {'error': "flux_type 只能为 'absolute' 或 'normalized'"}, 400
        # 光谱类型：transient（暂现源）/ host（宿主）/ mix（混合），不允许为空
        spec_type = (body.get('spec_type') or 'transient').strip().lower()
        if spec_type not in ('transient', 'host', 'mix'):
            return {'error': "spec_type 只能为 'transient'、'host' 或 'mix'"}, 400
        # 波长类型：vacuum（真空）/ air（空气）/ null（留空，下游按空气处理）
        wavelength_type = body.get('wavelength_type') or sp.get('wavelength_type')
        if wavelength_type is not None:
            wavelength_type = str(wavelength_type).strip().lower()
            if wavelength_type not in ('vacuum', 'air'):
                return {'error': "wavelength_type 只能为 'vacuum'、'air' 或 null"}, 400
        u_fluxes = sp.get('u_fluxes') or (
            'erg/s/cm^2/Angstrom' if flux_type == 'absolute' else 'normalized')
        obs_date = None
        if mjd not in (None, ''):
            try:
                obs_date = _mjd_to_dt(float(mjd))
            except (TypeError, ValueError):
                return {'error': f'MJD 无法解析: {mjd}'}, 400
        obj = obj_name or t.id
        wavs = [p[0] for p in sp['data']]
        has_err = any(len(p) > 2 for p in sp['data'])
        store_rel = f'catadata/spectra/{tid}/{filename}.json'
        store_abs = os.path.join(PROJECT_ROOT, store_rel)
        os.makedirs(os.path.dirname(store_abs), exist_ok=True)
        sp_out = {
            'time': str(mjd) if mjd not in (None, '') else None,
            'filename': filename,
            'instrument': instrument,
            'observer': observer,
            'reducer': reducer,
            'flux_type': flux_type,
            'spec_type': spec_type,
            'u_fluxes': u_fluxes,
            'u_wavelengths': sp.get('u_wavelengths') or 'Angstrom (observer frame)',
            'u_time': 'MJD',
            'data': sp['data'],
        }
        if wavelength_type is not None:
            sp_out['wavelength_type'] = wavelength_type   # null 时不写该键
        with open(store_abs, 'w') as f:
            json.dump({obj: {'spectra': sp_out}}, f)

        rec = Spectrum(
            transient_id=tid, filename=filename,
            wavelength_min=min(wavs), wavelength_max=max(wavs),
            instrument=instrument, observation_date=obs_date,
            file_path=store_rel, file_type='json', spec_type=spec_type,
            wavelength_type=wavelength_type,
            extra_data={'observer': observer, 'reducer': reducer,
                        'u_fluxes': sp_out['u_fluxes'], 'u_wavelengths': sp_out['u_wavelengths'],
                        'mjd': sp_out['time'], 'sn_name': obj, 'flux_type': flux_type,
                        'has_err': has_err,
                        'source': 'user_upload', 'n_points': len(sp['data'])},
        )
        sess.add(rec)
        try:
            sess.commit()
        except Exception:
            sess.rollback()
            # DB 未入库，删除已写出的文件，避免遗留孤儿 JSON
            if os.path.exists(store_abs):
                os.unlink(store_abs)
            raise
        return jsonify({'ok': True, 'id': rec.id, 'filename': filename,
                        'n_points': len(sp['data'])}), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 500
    finally:
        sess.close()


@spectra_bp.route('/<int:spec_id>', methods=['PUT'])
@require_admin
def update_spectrum(spec_id):
    """修改光谱元数据（spec_type / wavelength_type，至少提供其一）：
    DB 记录与库存文件同步写；原始谱的改动会传播到其全部改正子谱。
    wavelength_type 合法值 'vacuum' / 'air' / null（显式 null 或 '' = 清除，
    文件 JSON 中对应键一并移除）"""
    body = request.get_json(force=True) or {}
    if 'spec_type' not in body and 'wavelength_type' not in body:
        return {'error': '至少提供 spec_type 或 wavelength_type 之一'}, 400
    spec_type = None
    if 'spec_type' in body:
        spec_type = (body.get('spec_type') or '').strip().lower()
        if spec_type not in ('transient', 'host', 'mix'):
            return {'error': "spec_type 只能为 'transient'、'host' 或 'mix'"}, 400
    has_wtype = 'wavelength_type' in body
    wavelength_type = None
    if has_wtype:
        raw = body.get('wavelength_type')
        if raw not in (None, ''):
            wavelength_type = str(raw).strip().lower()
            if wavelength_type not in ('vacuum', 'air'):
                return {'error': "wavelength_type 只能为 'vacuum'、'air' 或 null"}, 400
    sess = get_session()
    try:
        r = sess.query(Spectrum).filter(Spectrum.id == spec_id).first()
        if not r:
            return {'error': 'Not found'}, 404
        rows = [r]
        if r.parent_id is None:
            rows += sess.query(Spectrum).filter(Spectrum.parent_id == r.id).all()
        paths = []
        for row in rows:
            path = os.path.normpath(os.path.join(PROJECT_ROOT, row.file_path))
            if not path.startswith(SPECTRA_DIR_PREFIX):
                return {'error': 'invalid path'}, 400
            if spec_type is not None:
                row.spec_type = spec_type
            if has_wtype:
                row.wavelength_type = wavelength_type
            paths.append(path)
        sess.commit()
        # 回写库存文件 JSON，保证全量重建（import_spectra 以文件为准）不丢
        for path in paths:
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                for obj in data.values():
                    sp = obj.get('spectra') if isinstance(obj, dict) else None
                    if sp is not None:
                        if spec_type is not None:
                            sp['spec_type'] = spec_type
                        if has_wtype:
                            if wavelength_type is not None:
                                sp['wavelength_type'] = wavelength_type
                            else:
                                sp.pop('wavelength_type', None)
                with open(path, 'w') as f:
                    json.dump(data, f)
        return jsonify(r.to_dict())
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 500
    finally:
        sess.close()


@spectra_bp.route('/<int:spec_id>/gext_correct', methods=['POST'])
@require_auth
def gext_correct_spectrum(spec_id):
    """生成/重新生成（覆盖）该原始光谱的银河系消光改正谱（CSFD + P92 + Rv=3.1）"""
    sess = get_session()
    try:
        r = sess.query(Spectrum).filter(Spectrum.id == spec_id).first()
        if not r:
            return {'error': 'Not found'}, 404
        result = extinction.correct_spectrum(sess, r)
        sess.commit()
        return jsonify({'spectrum': result['child_row'].to_dict(),
                        'ebv': result['ebv'], 'warnings': result['warnings']})
    except extinction.SpectrumGextError as e:
        sess.rollback()
        return {'error': str(e)}, 400
    except FileNotFoundError as e:
        sess.rollback()
        return {'error': str(e)}, 404
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 500
    finally:
        sess.close()


@spectra_bp.route('/<int:spec_id>', methods=['DELETE'])
@require_admin
def delete_spectrum(spec_id):
    """删除光谱：DB 记录 + 文件；删除原始谱时其改正子谱一并删除
    （DB 行靠 FK ON DELETE CASCADE，文件逐个删除）"""
    sess = get_session()
    try:
        r = sess.query(Spectrum).filter(Spectrum.id == spec_id).first()
        if not r:
            return {'error': 'Not found'}, 404
        paths = [os.path.normpath(os.path.join(PROJECT_ROOT, r.file_path))]
        if r.parent_id is None:
            for c in sess.query(Spectrum).filter(Spectrum.parent_id == r.id).all():
                paths.append(os.path.normpath(os.path.join(PROJECT_ROOT, c.file_path)))
        sess.delete(r)
        sess.commit()
        for path in paths:
            if path.startswith(SPECTRA_DIR_PREFIX) and os.path.exists(path):
                os.remove(path)
        return {'status': 'deleted', 'id': spec_id,
                'deleted_children': len(paths) - 1}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 500
    finally:
        sess.close()

