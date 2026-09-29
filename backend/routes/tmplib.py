"""模板库 × K 改正：P0+P1+P2 端点（设计文档 02 §5）

GET    /api/tmplib/config              — API-1  引擎自检（公开，恒 200）
GET    /api/tmplib/guards              — API-13 三轴 stale 独立报告（公开，恒 200）
GET    /api/tmplib/templates           — API-2  模板列表 + 状态（公开；不含 rows_sha256/mu，IA-4）
GET    /api/tmplib/templates/<id>      — API-3  manifest 摘要 + QC 全文 + μ 分解（公开）
POST   /api/tmplib/predict             — API-6  单条模板曲线预测（公开，同步）
GET    /api/tmplib/preview?transient_id= — API-4  造模板预检（登录；行账+波段映射+CA-12+域内预估）
POST   /api/tmplib/templates           — API-5  造模板向导建面（登录；写 tmplibrary/）
POST   /api/tmplib/templates/<id>/rebuild — API-7  重建面+重算指纹（登录）
DELETE /api/tmplib/templates/<id>      — API-11 软删/硬删（管理员；硬删需密码二次校验）
GET    /api/tmplib/in_domain/<id>      — API-12 域内可答比例缓存值（公开，F-45）
GET    /api/tmplib/export.csv|.json    — API-9  同 API-8 参数的文件形式（公开，F-56 单算双渲染）
POST   /api/tmplib/budget              — API-10 单项误差预算（公开；Q-7 n_draws，默认 32 见 budget.py）

约定（A-2/A-3/E-30/T-30/ST-10）：
  - 宿主没有 501/504 handler，"暂不可用"一律 200 + 显式 code
    （TL_ENGINE_UNAVAILABLE，E-21 在本端点的 200 形态）；
  - 错误一律 jsonify 且带稳定 code，前端只按 code 分支；
  - 引擎异常的 message/context 统一过 _scrub()，响应不泄漏绝对路径；
  - 未知异常记 repr 到服务端日志，不外泄 traceback。
  - ST-1：任何 DB 读取都在调引擎之前完成并关闭 session；
  - A-3：只有 API-5/7/11 写盘，且只写 tmplibrary/；读路径永不 rebuild。
"""
import logging
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

from app import get_session, require_auth, require_admin
from models import Transient, User
from tmplib import engine, extract, guard, indomain, mudelta, paths, \
    budget as tl_budget, compare as tl_compare, export as tl_export, \
    predict as tl_predict, surfaces
from tmplib import manifest as tl_manifest

log = logging.getLogger(__name__)

tmplib_bp = Blueprint('tmplib', __name__)

# P4 能力面：对比（P3）+ 导出/预算（P4）全开
_CAPABILITIES = {
    'endpoints': ['/api/tmplib/config', '/api/tmplib/guards',
                  '/api/tmplib/templates', '/api/tmplib/templates/<id>',
                  '/api/tmplib/predict', '/api/tmplib/preview',
                  '/api/tmplib/compare', '/api/tmplib/export.csv',
                  '/api/tmplib/export.json', '/api/tmplib/budget',
                  '/api/tmplib/templates/<id>/rebuild', '/api/tmplib/in_domain/<id>'],
    'predict': True,
    'compare': True,
    'export': True,
    'budget': True,
    'build': True,
    'library_admin': True,
}

# ST-4：建面与预测共用一把非阻塞信号量；拿不到立即 429，不排队。
# 名字保留 _PREDICT_SEM（T-31 直接引用它）。
_PREDICT_SEM = surfaces.BUILD_SEM

# 引擎错误码 → (AJST code, HTTP)（E-16 映射表；T-16/T-32：互不借用）
_ENGINE_CODE_MAP = {
    'E_CFG': ('TL_INCONSISTENT_ARGS', 400),
    'E_DATA': ('TL_DATA', 422),
    'E_FILTER': ('TL_BAND_UNKNOWN', 422),
    'E_DOMAIN': ('TL_OUT_OF_DOMAIN', 409),
    'E_CONVERGE': ('TL_NOT_CONVERGED', 409),
    'E_INTERP': ('TL_BAD_SURFACE', 422),
}


def _scrub(text):
    """剥掉绝对路径（T-30）：已知根替换为占位符，兜底正则吃掉残留 /home/... 形态"""
    if not text:
        return text
    out = str(text)
    for base, token in _scrub_bases():
        out = out.replace(str(base), token)
    return re.sub(r'/home/\S+', '<path>', out)


def _scrub_bases():
    bases = []
    try:
        bases.append((paths.library_root(), '<tmplib>'))
        bases.append((paths.default_root().parents[1], '<repo>'))
        cs = engine._import()
        if cs is not None:
            import pathlib
            bases.append((pathlib.Path(cs.__file__).resolve().parent, '<engine>'))
    except Exception:
        pass
    return bases


def _walk(obj):
    """对嵌套结构里的字符串统一过 _scrub（引擎/QC 文本可能含绝对路径）"""
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, dict):
        return {k: _walk(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_walk(v) for v in obj]
    return obj


def _err(code, message, http=400, **extra):
    body = {'error': _scrub(message), 'code': code}
    body.update(_walk(extra))
    return jsonify(body), http


def _tl_error(e: tl_predict.TLError):
    return _err(e.code, e.message, e.http, **e.context)


def _engine_error(e):
    """ChromaShiftError → 稳定 code 响应（E-30：context 键原样透传）"""
    ctx = dict(e.context or {})
    if e.code == 'E_DATA' and 'stale_reasons' in ctx:  # 面陈旧是 E-12 不是 E-16
        return _err('TL_STALE', e.message, 409,
                    stale_because={'engine_inputs': ctx.get('stale_reasons'),
                                   'catalog_rows': None, 'filter_vendor': None})
    code, http = _ENGINE_CODE_MAP.get(e.code, ('TL_INTERNAL', 500))
    if code == 'TL_INTERNAL':
        log.error('tmplib 未映射的引擎错误: %r', e)  # ST-10：repr 进日志，不外泄
        return _err(code, '引擎内部错误（已记录）', 500)
    return _err(code, e.message, http, context=ctx)


def _library_entry(template_id, root=None):
    """library.json 里该模板的条目（读不到索引/无条目 ⇒ {}）。"""
    try:
        lib = paths.read_library(root) or {}
    except ValueError:
        return {}
    return (lib.get('templates') or {}).get(template_id) or {}


def _fetch_counterpart(template_id, entry=None):
    """库内对应源行（ST-1/A-8：短 session，读完即关；引擎调用全程不持连接）。

    出厂模板查 COUNTERPARTS；向导模板（origin=catalog）的对应源就是它自己的
    transient_id（F-19）。返回 None = 无对应源配置；{'lookup_error': True} =
    库暂不可达。
    """
    cid = mudelta.counterpart_of(template_id, entry)
    if cid is None:
        return None
    try:
        sess = get_session()
        try:
            row = sess.get(Transient, cid)
        finally:
            sess.close()
    except Exception as e:
        log.warning('tmplib: 对应源 %s 查询失败: %r', cid, e)
        return {'id': cid, 'lookup_error': True}
    if row is None:
        return {'id': cid, 'redshift': None, 'gext_distmod': None, 't0': None}
    return {'id': cid, 'redshift': row.redshift,
            'gext_distmod': row.gext_distmod, 't0': row.t0}


def _live_rows_hash(template_id, entry, root=None):
    """第 7 项指纹的活库重算（F-52；ST-1：自带短 session，返回前已关闭）。

    用条目里登记的 extract 配方在现库上重跑取数管线；返回 sha256 或 None
    （无库绑定/库不可达 ⇒ 调用方按"未验证"处理，不当作"一致"）。
    """
    recipe = entry.get('extract') or {}
    src = recipe.get('transient_id') or entry.get('transient_id')
    if not src:
        return None
    try:
        sess = get_session()
        try:
            rows = extract.fetch_rows(sess, src)
        finally:
            sess.close()
    except Exception as e:
        log.warning('tmplib: %s 行集重算取数失败: %r', template_id, e)
        return None
    try:
        bank = engine.bank_cached(root or paths.library_root())
        decls = {d['band']: d['system']
                 for d in (recipe.get('declarations') or [])}
        csv_rows, _, _ = extract.build_rowset(
            rows, rowset=recipe.get('rowset', 'raw'),
            null_system_policy=recipe.get('null_system_policy', 'drop'),
            declarations=decls, bands=recipe.get('bands'),
            registry_ids=frozenset(bank.names()))
        return extract.rows_hash(csv_rows)
    except Exception as e:
        log.warning('tmplib: %s 行集重算失败: %r', template_id, e)
        return None


# ── P0：自检与守卫 ─────────────────────────────────────────────────────────

@tmplib_bp.route('/config', methods=['GET'])
def config():
    """API-1：引擎自检与能力声明。引擎不可用 ⇒ 200 + TL_ENGINE_UNAVAILABLE"""
    avail = engine.available()
    body = {
        'code': 'TL_OK' if avail['ok'] else 'TL_ENGINE_UNAVAILABLE',
        'available': avail['ok'],
        'unavailable_reasons': [
            {'code': r['code'], 'detail': _scrub(r['detail'])}
            for r in avail['reasons']],
        'backend': 'in-process',
        'engine': None,
        'deps': engine.deps(),
        'enums': engine.engine_enums(),
        'limits': engine.limits(),
        'vendor': guard.vendor_axis(),
        'library': None,
        'capabilities': dict(_CAPABILITIES),
    }
    if avail['ok']:
        body['engine'] = {
            'name': 'chromashift',
            'version': engine.version(),
            'code_sha256': engine.code_sha256(),  # C_ENGINE_PIN
            'cosmology': engine.cosmology(),
        }
    try:
        lib = paths.read_library()
        if lib is not None:
            body['library'] = {
                'schema': lib.get('schema'),
                'templates_registered': len(lib.get('templates') or {}),
            }
    except ValueError as e:
        body['library'] = {'error': _scrub(str(e))}
    return jsonify(body)


@tmplib_bp.route('/guards', methods=['GET'])
def guards():
    """API-13：三轴 stale 独立报告（IA-13：单轴故障不拖垮其余两轴）"""
    rep = guard.report()
    return jsonify({
        'code': 'TL_OK',
        'ok': rep['ok'],
        'axes': _walk(rep['axes']),
    })


# ── P1：库列表与详情 ───────────────────────────────────────────────────────

@tmplib_bp.route('/templates', methods=['GET'])
def templates():
    """API-2：列表 + state + 三轴 stale 概要 + bands/modes/t_valid/z/距离 kind。

    不含 rows_sha256 现算、不含 mu（IA-4）；stale 判据来自引擎 status()
    的实测行（R-11），catalog_rows/filter_vendor 轴取 library.json 登记值。
    """
    avail = engine.available()
    if not avail['ok']:
        return _err('TL_ENGINE_UNAVAILABLE',
                    'K 改正引擎当前不可用（' + '; '.join(
                        r['detail'] for r in avail['reasons']) + '）', 200,
                    available=False,
                    reasons=[r['code'] for r in avail['reasons']])
    cs = engine.require()
    root = paths.library_root()
    try:
        lib = paths.read_library() or {}
    except ValueError as e:
        return _err('TL_DATA', str(e), 500)
    entries = lib.get('templates') or {}
    try:
        rows = cs.status(root)
    except Exception as e:
        log.error('tmplib status() 失败: %r', e)
        return _err('TL_INTERNAL', '模板库状态读取失败（已记录）', 500)
    out = []
    for row in rows:
        entry = entries.get(row['id'], {})
        sb = dict(entry.get('stale_because') or {})
        # 引擎轴以实测为准（status() 的 stale_reasons）；其余两轴取登记值
        sb['engine_inputs'] = row.get('stale_because') or []
        sb.setdefault('catalog_rows', None)
        sb.setdefault('filter_vendor', None)
        if row.get('stale'):
            state = 'stale'
        elif not row.get('surface'):
            state = 'never-built'
        else:
            state = entry.get('state') or 'fresh'
        in_domain = entry.get('in_domain')
        out.append({
            'id': row['id'],
            'label': entry.get('label'),
            'origin': entry.get('origin', 'shipped'),
            'z': row.get('z'),
            'distance_kind': row.get('distance'),
            'bands': row.get('bands'),
            'modes': row.get('modes'),
            't_valid_days': row.get('t_valid_days'),
            'n_time_nodes': row.get('n_time_nodes'),
            'state': state,
            'stale_because': sb,
            'error': row.get('error'),
            'in_domain': ({'total': in_domain.get('total'),
                           'answered': in_domain.get('answered')}
                          if isinstance(in_domain, dict) else None),
        })
    return jsonify({'code': 'TL_OK', 'templates': _walk(out),
                    'history': _walk(lib.get('history') or [])})  # 族谱（S4 只读展示）


@tmplib_bp.route('/templates/<template_id>', methods=['GET'])
def template_detail(template_id):
    """API-3：manifest 摘要 + QC 全文 + μ 分解 + per-band 详情 + in_domain。

    IA-1：stale 模板可看 QC —— 本端点不因 stale 拒绝，只如实报状态。
    """
    if not paths.valid_id(template_id):
        return _err('TL_TEMPLATE_UNKNOWN', f'非法模板 id: {template_id!r}', 404)
    avail = engine.available()
    if not avail['ok']:
        return _err('TL_ENGINE_UNAVAILABLE', 'K 改正引擎当前不可用', 200,
                    available=False)
    cs = engine.require()
    root = paths.library_root()
    known = cs.manifests(root)
    if template_id not in known:
        return _err('TL_TEMPLATE_UNKNOWN',
                    f'模板 {template_id!r} 不存在（可用: {", ".join(sorted(known))}）',
                    404, available=sorted(known))
    try:
        spec = cs.TemplateSpec.from_yaml(known[template_id])  # ST-7
    except Exception as e:
        return _err('TL_DATA', f'manifest 不可读: {e}', 422)

    # QC 全文（sidecar 原文；stale 也可看，IA-1）
    import json as _json
    qc_path = root / 'data' / 'surfaces' / f'{template_id}.qc.json'
    qc = None
    if qc_path.is_file():
        try:
            qc = _json.loads(qc_path.read_text(encoding='utf-8'))
        except ValueError:
            qc = {'error': 'qc sidecar 不可解析'}

    # 状态（引擎轴实测；面不可读时降级为 sidecar 信息）
    from chromashift import registry
    npz = root / 'data' / 'surfaces' / f'{template_id}.npz'
    stale = registry.stale_reasons(npz, spec, root)
    surf = None
    if not stale and npz.is_file():
        try:
            surf = engine.load_surface_cached(template_id, root)
        except Exception as e:
            log.warning('tmplib: 面 %s 可读性检查失败: %r', template_id, e)

    per_band = None
    if surf is not None:
        try:
            bank = engine.bank_cached(root)
            per_band = {}
            modes = dict(surf.mode_by_band)
            for b in surf.bands:
                try:
                    trust = bank.get(b).trust
                except Exception:
                    trust = None
                per_band[b] = {'mode': modes.get(b), 'trust': trust}
        except Exception as e:
            log.warning('tmplib: per-band 详情失败 %s: %r', template_id, e)

    ez = (spec.photometry.get('time') or {}).get('epoch_zero') or {}
    from chromashift.template import SCHEMA as MANIFEST_SCHEMA
    entry = _library_entry(template_id, root)
    catalog = _fetch_counterpart(template_id, entry)  # 短 session，先读后关（ST-1）
    mu = mudelta.compute_mu(spec, catalog, entry)

    # 第 7 项指纹活库比对（F-52：API-3 前置再算；IA-1 只报不拒）
    rows_live = None
    catalog_rows_state = None
    if entry.get('rows_sha256') and entry.get('extract'):
        rows_live = _live_rows_hash(template_id, entry, root)
        if rows_live is not None:
            if rows_live == entry['rows_sha256']:
                catalog_rows_state = []      # 一致；探针条目另注 F-19 口径
            else:
                catalog_rows_state = ['rows-drifted']   # CA-11
                stale = list(stale)  # stale 仅引擎轴；状态合成见下

    body = {
        'code': 'TL_OK',
        'id': template_id,
        'label': spec.label,
        'origin': entry.get('origin', 'shipped'),
        'manifest': {
            'object_class': spec.object_class,
            'schema': MANIFEST_SCHEMA,
            'z': spec.z,
            'redshift': spec.redshift,
            'distance': spec.distance,
            'time': {
                'column': (spec.photometry.get('time') or {}).get('column'),
                'unit': (spec.photometry.get('time') or {}).get('unit'),
                'frame': (spec.photometry.get('time') or {}).get('frame'),
                'epoch_zero': dict(ez),
            },
            'validity': spec.validity,
            'reddening': spec.reddening,
            'stretch': spec.stretch,
        },
        'state': ('stale' if (stale or catalog_rows_state) else
                  (entry.get('state') or 'fresh')),
        'stale_because': {'engine_inputs': stale,
                          'catalog_rows': catalog_rows_state
                          if catalog_rows_state is not None else
                          (entry.get('stale_because') or {}).get('catalog_rows'),
                          'filter_vendor': (entry.get('stale_because') or {})
                          .get('filter_vendor')},
        'rows_sha256': entry.get('rows_sha256'),  # 登记值（建面时冻结行集的指纹）
        'rows_sha256_live': rows_live,            # 活库重算；None = 未验证
        'rows_note': ('与本库同源，差异待核（漂移探针，非一致性证明）'
                      if entry.get('origin') == 'catalog-derived'
                      and catalog_rows_state == [] else None),
        'in_domain': entry.get('in_domain'),      # 建面时算（F-45）；无则 null
        'mu': mu,
        'time_origin_defaults': tl_predict.resolve_time_origin(spec, None, catalog, root),
        'surface': None,
        'per_band': per_band,
        'qc': qc,
    }
    if surf is not None:
        body['surface'] = {
            'bands': list(surf.bands),
            'mode_by_band': dict(surf.mode_by_band),
            't_valid_days': [float(v) for v in surf.t_valid],
            'n_time_nodes': int(surf.time_rest.size),
            'nu_valid_Hz': [float(v) for v in surf.nu_valid],
        }
    return jsonify(_walk(tl_predict._sanitize(body)))


# ── P1：预测 ───────────────────────────────────────────────────────────────

@tmplib_bp.route('/predict', methods=['POST'])
def predict():
    """API-6：单条模板曲线。ST-4：拿不到信号量立即 429，不排队"""
    if not _PREDICT_SEM.acquire(blocking=False):
        return _err('TL_BUSY', '引擎正忙于另一条预测/建面，请稍后重试（不自动重试）', 429)
    try:
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, dict):
            return _err('TL_INCONSISTENT_ARGS', '请求体必须是 JSON 对象', 400)
        # ST-1：先取库内对应源（短 session、读完即关），再进引擎
        catalog = None
        rows_live = None
        tid = payload.get('template_id')
        if isinstance(tid, str) and paths.valid_id(tid):
            entry = _library_entry(tid)
            catalog = _fetch_counterpart(tid, entry)
            # F-52：有登记指纹+取数配方的模板，预测前置活库比对（E-12 同门）
            if entry.get('rows_sha256') and entry.get('extract'):
                rows_live = _live_rows_hash(tid, entry)
        result = tl_predict.predict_curve(payload, catalog=catalog,
                                          rows_live_hash=rows_live)
        return jsonify(_walk(result))
    except tl_predict.TLError as e:
        return _tl_error(e)
    except Exception as e:
        import chromashift
        if isinstance(e, chromashift.ChromaShiftError):
            return _engine_error(e)
        log.error('tmplib predict 未知异常: %r', e)  # ST-10
        return _err('TL_INTERNAL', '预测内部错误（已记录）', 500)
    finally:
        _PREDICT_SEM.release()


# ── P3：对比（模板曲线 + K 改正实测点 + 实测点；部分成功协议） ─────────────

def _fetch_compare_inputs(req):
    """compare/export 共享的取数段（ST-1/A-8：所有 DB 读取在短 session 内完成
    并关闭，返回后不再持连接；引擎编排在调用方之后进行）。

    返回 (source_data, catalogs, rows_hashes)；库不可达时抛 TLError(TL_DATA)。
    """
    source_data = {}
    try:
        sess = get_session()
        try:
            for item in req['sources']:
                sid = item['transient_id']
                if sid in source_data:
                    continue
                tr = extract.fetch_transient(sess, sid)
                rows = extract.fetch_rows(sess, sid) if tr else []
                source_data[sid] = (tr, rows)
        finally:
            sess.close()
    except Exception as e:
        log.warning('tmplib compare/export 取数失败: %r', e)
        raise tl_predict.TLError('TL_DATA', 500, '库查询失败（已记录）')

    # 模板曲线的 μ 对照与第 7 项指纹（各自短 session，同样先取后关）
    catalogs, rows_hashes = {}, {}
    for item in req['curves']:
        tid = item.get('template_id')
        if not isinstance(tid, str) or not paths.valid_id(tid) or tid in catalogs:
            continue
        entry = _library_entry(tid)
        catalogs[tid] = _fetch_counterpart(tid, entry)
        if entry.get('rows_sha256') and entry.get('extract'):
            rows_hashes[tid] = _live_rows_hash(tid, entry)
    return source_data, catalogs, rows_hashes


@tmplib_bp.route('/compare', methods=['POST'])
def compare():
    """API-8：≤8 模板曲线 + ≤8 源（Q-8 超出整请求 400）；A-6 恒 200 部分成功
    —— 单条失败只标该条 state=error。ST-1：所有库读取在引擎调用之前完成。"""
    payload = request.get_json(force=True, silent=True)
    try:
        req = tl_compare.validate_request(payload)
    except tl_predict.TLError as e:
        return _tl_error(e)
    avail = engine.available()
    if not avail['ok']:
        return _engine_unavailable_response(avail, http=200)

    try:
        source_data, catalogs, rows_hashes = _fetch_compare_inputs(req)
    except tl_predict.TLError as e:
        return _tl_error(e)

    try:
        result = tl_compare.run(req, source_data=source_data,
                                catalogs=catalogs, rows_hashes=rows_hashes)
        return jsonify(_walk(result))
    except Exception as e:
        log.error('tmplib compare 未知异常: %r', e)   # ST-10
        return _err('TL_INTERNAL', '对比内部错误（已记录）', 500)


# ── P4：导出（API-9）与误差预算（API-10） ──────────────────────────────────

@tmplib_bp.route('/export.csv', methods=['GET'])
@tmplib_bp.route('/export.json', methods=['GET'])
def export_file():
    """API-9：与 API-8 同参数（GET query 编码见 tl_export.parse_query /
    docs/TECHNICAL.md）的文件形式。F-56：compare.run 只调一次，两条通路只做
    序列化；A-3 零写盘（内存渲染）。配额/结构错误才 4xx，曲线级失败按 A-6
    进注释头（state=error 行），与 API-8 同协议。"""
    fmt = 'csv' if request.path.endswith('.csv') else 'json'
    try:
        req = tl_export.parse_query(request.args)
    except tl_predict.TLError as e:
        return _tl_error(e)
    avail = engine.available()
    if not avail['ok']:
        return _engine_unavailable_response(avail, http=200)
    try:
        source_data, catalogs, rows_hashes = _fetch_compare_inputs(req)
    except tl_predict.TLError as e:
        return _tl_error(e)

    # F-57'：源曲线的时间原点 = 该源目录 t0（逐源独立；数据来自已关闭的 session）
    source_origins = {}
    for sid, (tr, _rows) in source_data.items():
        if tr is None:
            continue
        t0_mjd = tl_predict.catalog_t0_mjd(tr)
        source_origins[sid] = {'value_mjd': t0_mjd,
                               'kind': 'event-t0' if t0_mjd is not None else None}

    try:
        table = tl_export.run_export(req, source_data=source_data,
                                     catalogs=catalogs, rows_hashes=rows_hashes,
                                     source_origins=source_origins)
    except Exception as e:
        log.error('tmplib export 未知异常: %r', e)   # ST-10
        return _err('TL_INTERNAL', '导出内部错误（已记录）', 500)
    if fmt == 'json':
        return jsonify(_walk(tl_export.render_json(table)))
    from flask import Response
    return Response(tl_export.render_csv(table), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition':
                             'attachment; filename="tmplib_export.csv"'})


@tmplib_bp.route('/budget', methods=['POST'])
def budget():
    """API-10：单项误差预算（公开，同步）。ST-4：与预测/建面同一把非阻塞
    信号量，拿不到立即 429 不排队。"""
    if not _PREDICT_SEM.acquire(blocking=False):
        return _err('TL_BUSY', '引擎正忙于另一条预测/建面/预算，请稍后重试（不自动重试）', 429)
    try:
        payload = request.get_json(force=True, silent=True)
        try:
            req = tl_budget.validate_request(payload)
        except tl_predict.TLError as e:
            return _tl_error(e)
        try:
            result = tl_budget.run(req)
            return jsonify(_walk(tl_predict._sanitize(result)))
        except tl_predict.TLError as e:
            return _tl_error(e)
        except Exception as e:
            import chromashift
            if isinstance(e, chromashift.ChromaShiftError):
                return _engine_error(e)
            log.error('tmplib budget 未知异常: %r', e)  # ST-10
            return _err('TL_INTERNAL', '预算内部错误（已记录）', 500)
    finally:
        _PREDICT_SEM.release()


# ── P2：造模板向导（S2） ────────────────────────────────────────────────────

def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _append_history(lib, action, tid, note=''):
    """§4.1 history：build|rebuild|delete|declare 逐条追加。"""
    lib.setdefault('history', []).append({
        'at': _now_utc(), 'by': session.get('username'),
        'action': action, 'id': tid, 'note': _scrub(note)})


def _fresh_library(root):
    """library.json 不存在时的骨架（schema 键与出厂索引同构）。"""
    vendor = {}
    try:
        vendor = guard.vendor_axis(root)
    except Exception:
        pass
    return {
        'schema': paths.SCHEMA_LIB,
        'engine': {'version': engine.version(),
                   'code_sha256': engine.code_sha256(),
                   'cosmology': engine.cosmology()},
        'filters': {'source_sha256': vendor.get('recorded_source_sha256'),
                    'checked_utc': _now_utc()},
        'templates': {}, 'history': [],
    }


def _engine_unavailable_response(avail, http=200):
    return _err('TL_ENGINE_UNAVAILABLE',
                'K 改正引擎当前不可用（' + '; '.join(
                    r['detail'] for r in avail['reasons']) + '）', http,
                available=False,
                reasons=[r['code'] for r in avail['reasons']])


@tmplib_bp.route('/preview', methods=['GET'])
@require_auth
def preview():
    """API-4：造模板预检（只读，A-3）。行账 + 严格同名波段映射（F-08）+
    系统/gext 覆盖率 + z 冲突提示（D-6）+ CA-12 同事件多记录 + U-19 域内预估。"""
    src = request.args.get('transient_id', '')
    if not src:
        return _err('TL_INCONSISTENT_ARGS', 'transient_id 必填', 400)
    # U-13：改勾/改档必须重算账本 —— 口径唯一权威在服务端，preview 带参重算
    # （仍是只读，A-3）。bands 逗号分隔；declare 形如 "B:ab,R:vega"。
    rowset = request.args.get('rowset', 'raw')
    policy = request.args.get('null_system_policy', 'drop')
    if rowset not in extract.ROWSETS:
        return _err('TL_INCONSISTENT_ARGS', 'rowset 二选一 raw|gext（F-11）', 400)
    if policy not in extract.NULL_POLICIES:
        return _err('TL_INCONSISTENT_ARGS',
                    f'null_system_policy 四档 {extract.NULL_POLICIES}（F-10）', 400)
    bands_arg = request.args.get('bands')
    bands = [b for b in bands_arg.split(',') if b] if bands_arg else None
    declarations = {}
    for tok in (request.args.get('declare') or '').split(','):
        if not tok:
            continue
        band, _, sysname = tok.partition(':')
        if sysname in ('ab', 'vega'):
            declarations[band] = sysname
    if policy == 'declare' and not declarations:
        return _err('TL_DECLARE_MISSING',
                    'policy=declare 需要 declare=波段:系统 参数（Q-2/E-02）',
                    400, missing=['declare'])
    avail = engine.available()
    if not avail['ok']:
        return _engine_unavailable_response(avail)
    root = paths.library_root()
    # ST-1/A-8：所有库读取在同一短 session 内完成并关闭，之后不持连接
    try:
        sess = get_session()
        try:
            tr = extract.fetch_transient(sess, src)
            rows = extract.fetch_rows(sess, src) if tr else []
            records = extract.fetch_transient_summaries(sess) if tr else []
        finally:
            sess.close()
    except Exception as e:
        log.warning('tmplib preview 取数失败: %r', e)
        return _err('TL_DATA', '库查询失败（已记录）', 500)
    if tr is None:
        return _err('TL_TRANSIENT_UNKNOWN', f'源 {src!r} 不存在', 404)

    cs = engine.require()
    bank = engine.bank_cached(root)
    registry_ids = frozenset(bank.names())
    # 主账本按请求口径；gext 档的计数一并给出（U-16）
    try:
        _, ledger, band_table = extract.build_rowset(
            rows, rowset=rowset, null_system_policy=policy,
            declarations=declarations, bands=bands, registry_ids=registry_ids)
    except extract.SourceRejected as e:
        # drop-source 且有 NULL 行 ⇒ 整源作废（F-10）；预检如实报，不拦读
        _, ledger, band_table = extract.build_rowset(
            rows, rowset=rowset, null_system_policy='drop',
            bands=bands, registry_ids=registry_ids)
        ledger = dict(ledger, kept=0, csv_rows=0, source_rejected=True,
                      null_system_rows=e.n_missing)
    _, ledger_gext, _ = extract.build_rowset(
        rows, rowset='gext', null_system_policy='drop', registry_ids=registry_ids)

    bands_out = []
    for band in sorted(band_table):
        b = band_table[band]
        trust = None
        if b['in_registry']:
            try:
                trust = bank.get(band).trust
            except Exception:
                pass
        bands_out.append({'band': band, 'trust': trust, **b})

    # D-6：该源已有同事件出厂/向导模板且 z 不一致 ⇒ 报差（不拦）
    z_conflicts = []
    try:
        known = cs.manifests(root)
        for tid_, cid in mudelta.COUNTERPARTS.items():
            if cid != tr['id'] or tid_ not in known or tr['redshift'] is None:
                continue
            spec = cs.TemplateSpec.from_yaml(known[tid_])   # ST-7
            if abs(float(spec.z) - tr['redshift']) > 1e-6:  # C_SAME_Z_TOL
                d1 = mudelta._planck18_distmod(float(spec.z))
                d2 = mudelta._planck18_distmod(tr['redshift'])
                z_conflicts.append({
                    'template_id': tid_, 'template_z': float(spec.z),
                    'catalog_z': tr['redshift'],
                    'delta_mu': (None if d1 is None or d2 is None
                                 else round(d2 - d1, 4)),
                    'note': '库 z 与既有模板 z 不同（CA-10 家族）；造新模板前核对该取哪个'})
    except Exception as e:
        log.warning('tmplib preview z 冲突检查失败: %r', e)

    candidates = extract.same_event_candidates(tr, records)
    ca12 = extract.ca12_alert(tr, candidates)

    suggested = re.sub(r'[^a-z0-9._-]', '', tr['id'].lower())[:48] or 'template'
    if suggested in (cs.manifests(root) or {}):
        suggested = (suggested + '-ajst')[:48]

    tr_out = dict(tr)
    tr_out['t0'] = tr['t0'].isoformat() if tr.get('t0') else None
    return jsonify(_walk({
        'code': 'TL_OK',
        'transient': tr_out,
        'suggested_id': suggested,                       # M_ID 默认
        'query': {'rowset': rowset, 'null_system_policy': policy,
                  'bands': bands, 'declare': declarations},   # 口径回显（U-13）
        'ledger': ledger,                                # 按请求口径的账本
        'rowsets': {
            'raw': {'kept': ledger['kept'],
                    'rejected': ledger['rejected']},
            'gext': {'kept': ledger_gext['kept'],
                     'rejected': ledger_gext['rejected'],
                     'rows_with_gextcor': sum(b['gext_with_value']
                                              for b in band_table.values()),
                     'gext_missing': sum(b['gext_missing']
                                         for b in band_table.values())},
        },
        'bands': bands_out,                              # 大小写两列并存（F-08）
        'same_event': ca12,                              # CA-12 或 null
        'z_conflicts': z_conflicts,
        'indomain_estimate': extract.estimate_indomain(ledger, band_table, bank),
    }))


def _recorded_declarations(decl, rows):
    """declarations 落库形态（§4.1）：每条带影响行数、操作者、时刻。"""
    out = []
    if decl['null_system_policy'] == 'declare':
        for d in decl['declarations']:
            n = sum(1 for r in rows
                    if r['flux_density_unit'] == extract.MAG_UNIT
                    and r['mag_system'] is None and r['band'] == d['band'])
            out.append({'field': 'mag_system', 'band': d['band'], 'rows': n,
                        'declared': d['system'],
                        'by': session.get('username'), 'at': _now_utc()})
    return out


def _new_entry(decl, tr, *, ledger, csv_sha, rows_sha, inputs_sha, in_domain,
               mu, state, extra_rejected=None):
    return {
        'origin': 'catalog',
        'transient_id': decl['transient_id'],
        'label': decl.get('label') or f"{decl['transient_id']}（AJST 库内行造模板）",
        'created_at': _now_utc(), 'created_by': session.get('username'),
        'rowset': decl['rowset'],
        'declarations': _recorded_declarations(decl, extra_rejected or []),
        'csv_sha256': csv_sha, 'inputs_sha256': inputs_sha,
        'rows_sha256': rows_sha,
        'row_ledger': ledger,
        'in_domain': in_domain,
        'mu': mu,
        'state': state,
        'stale_because': {'engine_inputs': [], 'catalog_rows': None,
                          'filter_vendor': None},
        'deleted': None,
        'extract': {                       # 活库重算配方（F-52；行账可复现）
            'transient_id': decl['transient_id'],
            'rowset': decl['rowset'],
            'null_system_policy': decl['null_system_policy'],
            'declarations': decl['declarations'],
            'bands': decl['bands'],
        },
    }


@tmplib_bp.route('/templates', methods=['POST'])
@require_auth
def create_template():
    """API-5：强制声明校验（Q-2/Q-10/Q-12，引擎零调用）→ 取数 → CSV+manifest
    → 建面（ST-4/ST-5）→ in_domain 实测（F-45）→ 写 library.json。"""
    payload = request.get_json(force=True, silent=True)
    try:
        decl = tl_manifest.validate_build_request(payload or {})
    except tl_manifest.DeclError as e:
        return _err('TL_DECLARE_MISSING', str(e), 400,
                    missing=e.missing, **e.context)
    avail = engine.available()
    if not avail['ok']:
        return _engine_unavailable_response(avail, http=409)
    cs = engine.require()
    root = paths.library_root()
    tid = decl['id']
    if tid in cs.manifests(root):
        return _err('TL_TEMPLATE_CONFLICT',
                    f'模板 id {tid!r} 已存在（重复 id 是错误不是覆盖，F-01）', 409)
    try:
        lib = paths.read_library(root) or _fresh_library(root)
    except ValueError as e:
        return _err('TL_DATA', str(e), 500)
    existing = (lib.get('templates') or {}).get(tid)
    if existing and not existing.get('deleted'):
        return _err('TL_TEMPLATE_CONFLICT', f'模板 id {tid!r} 已在索引中', 409)

    # ST-1：取数在短 session 内完成并关闭
    try:
        sess = get_session()
        try:
            tr = extract.fetch_transient(sess, decl['transient_id'])
            rows = extract.fetch_rows(sess, decl['transient_id']) if tr else []
        finally:
            sess.close()
    except Exception as e:
        log.warning('tmplib build 取数失败: %r', e)
        return _err('TL_DATA', '库查询失败（已记录）', 500)
    if tr is None:
        return _err('TL_TRANSIENT_UNKNOWN',
                    f"源 {decl['transient_id']!r} 不存在", 404)

    bank = engine.bank_cached(root)
    decls = {d['band']: d['system'] for d in decl['declarations']}
    try:
        csv_rows, ledger, _ = extract.build_rowset(
            rows, rowset=decl['rowset'],
            null_system_policy=decl['null_system_policy'],
            declarations=decls, bands=decl['bands'],
            registry_ids=frozenset(bank.names()))
    except extract.SourceRejected as e:
        return _err('TL_DATA',
                    f'null_system_policy=drop-source：{e} ⇒ 整源作废（F-10）',
                    422, null_system_rows=e.n_missing)
    if not csv_rows:
        return _err('TL_DATA', '按当前声明没有任何可进面的行（行账见预览）', 422)

    band_map = {b: b for b in sorted({r[0] for r in csv_rows})}   # F-08 严格同名
    manifest_dict = tl_manifest.build_manifest(decl, transient=tr,
                                               band_map=band_map)

    # ST-4：拿不到信号量立即 429 —— 此刻还零写盘（T-31：429 无半路副作用）
    if not surfaces.BUILD_SEM.acquire(blocking=False):
        return _err('TL_BUSY', '引擎正忙于另一条预测/建面，请稍后重试（不自动重试）', 429)
    csv_path = root / 'data' / 'raw' / f'{tid}.csv'
    try:
        extract.write_csv(csv_rows, csv_path)
        try:
            tl_manifest.write_manifest(manifest_dict, root)  # 写后引擎自校验
        except Exception as e:
            # 自校验失败 = 我们的 manifest 生成有 bug；清掉半成品让 id 可重试
            csv_path.unlink(missing_ok=True)
            paths.template_yaml(root, tid).unlink(missing_ok=True)
            import chromashift
            if isinstance(e, chromashift.ChromaShiftError):
                return _engine_error(e)
            raise
        rows_sha = extract.rows_hash(csv_rows)
        try:
            surf, qc, _ = surfaces.build_surface(tid, root)
        except surfaces.SingleFlight as e:
            return _err('TL_BUILD_IN_PROGRESS', str(e), 409)
        except Exception as e:
            import chromashift
            if isinstance(e, chromashift.ChromaShiftError):
                # 建面被拒：保留 manifest+CSV 与 refused 条目（可 API-7 重试）
                entry = _new_entry(decl, tr, ledger=ledger,
                                   csv_sha=extract.csv_sha256(csv_path),
                                   rows_sha=rows_sha, inputs_sha=None,
                                   in_domain=None, mu=None, state='refused',
                                   extra_rejected=rows)
                lib['templates'][tid] = entry
                _append_history(lib, 'build', tid,
                                note=f"refused: {getattr(e, 'code', '?')}")
                paths.write_library(lib, root)
                return _engine_error(e)
            raise

        in_domain = indomain.compute(tid, root)              # F-45 实测
        spec = cs.TemplateSpec.from_yaml(paths.template_yaml(root, tid))
        mu = mudelta.compute_mu(spec, tr, {'transient_id': decl['transient_id']})
        entry = _new_entry(decl, tr, ledger=ledger,
                           csv_sha=extract.csv_sha256(csv_path),
                           rows_sha=rows_sha,
                           inputs_sha=qc.get('inputs_sha256'),
                           in_domain=in_domain, mu=mu, state='fresh',
                           extra_rejected=rows)
        lib['templates'][tid] = entry
        _append_history(lib, 'build', tid,
                        note=f"rows={ledger['kept']}/{ledger['total']}")
        paths.write_library(lib, root)
        return jsonify(_walk(tl_predict._sanitize({
            'code': 'TL_OK', 'id': tid, 'state': 'fresh',
            'row_ledger': ledger,
            'in_domain': in_domain,
            'mu': mu,
            'qc': qc,          # U-18：QC 全文（含 band_admission 整块）
            'surface': {'bands': list(surf.bands),
                        't_valid_days': [float(v) for v in surf.t_valid],
                        'n_time_nodes': int(surf.time_rest.size)},
        })))
    finally:
        surfaces.BUILD_SEM.release()


@tmplib_bp.route('/templates/<template_id>/rebuild', methods=['POST'])
@require_auth
def rebuild_template(template_id):
    """API-7：重建面（重算 in_domain 与三轴 stale；rows_sha256 登记值不动 —
    它是冻结 CSV 的指纹，活库漂移进 stale_because.catalog_rows，CA-11）。"""
    if not paths.valid_id(template_id):
        return _err('TL_TEMPLATE_UNKNOWN', f'非法模板 id: {template_id!r}', 404)
    avail = engine.available()
    if not avail['ok']:
        return _engine_unavailable_response(avail, http=409)
    cs = engine.require()
    root = paths.library_root()
    if template_id not in cs.manifests(root):
        return _err('TL_TEMPLATE_UNKNOWN',
                    f'模板 {template_id!r} 不存在（或已删除）', 404)
    try:
        lib = paths.read_library(root) or _fresh_library(root)
    except ValueError as e:
        return _err('TL_DATA', str(e), 500)
    entry = (lib.get('templates') or {}).get(template_id)
    if entry is None:
        return _err('TL_TEMPLATE_UNKNOWN', f'模板 {template_id!r} 不在索引中', 404)
    if entry.get('deleted'):
        return _err('TL_TEMPLATE_DELETED',
                    f'模板 {template_id} 已删除（软删），不能重建', 409)

    if not surfaces.BUILD_SEM.acquire(blocking=False):
        return _err('TL_BUSY', '引擎正忙于另一条预测/建面，请稍后重试（不自动重试）', 429)
    try:
        try:
            surf, qc, _ = surfaces.build_surface(template_id, root)
        except surfaces.SingleFlight as e:
            return _err('TL_BUILD_IN_PROGRESS', str(e), 409)
        except Exception as e:
            import chromashift
            if isinstance(e, chromashift.ChromaShiftError):
                return _engine_error(e)
            raise
        in_domain = indomain.compute(template_id, root)
        spec = cs.TemplateSpec.from_yaml(paths.template_yaml(root, template_id))
        catalog = _fetch_counterpart(template_id, entry)
        mu = mudelta.compute_mu(spec, catalog, entry)

        catalog_rows = None
        if entry.get('rows_sha256') and entry.get('extract'):
            live = _live_rows_hash(template_id, entry, root)
            if live is not None:
                catalog_rows = [] if live == entry['rows_sha256'] else ['rows-drifted']
        from chromashift import registry
        npz = root / 'data' / 'surfaces' / f'{template_id}.npz'
        eng_stale = registry.stale_reasons(npz, spec, root)
        entry['inputs_sha256'] = qc.get('inputs_sha256')
        entry['in_domain'] = in_domain
        entry['mu'] = mu
        entry['stale_because'] = {'engine_inputs': eng_stale,
                                  'catalog_rows': catalog_rows,
                                  'filter_vendor': None}
        entry['state'] = 'stale' if (eng_stale or catalog_rows) else 'fresh'
        lib['templates'][template_id] = entry
        _append_history(lib, 'rebuild', template_id,
                        note=f"state={entry['state']}")
        paths.write_library(lib, root)
        return jsonify(_walk(tl_predict._sanitize({
            'code': 'TL_OK', 'id': template_id, 'state': entry['state'],
            'stale_because': entry['stale_because'],
            'in_domain': in_domain, 'mu': mu, 'qc': qc,
        })))
    finally:
        surfaces.BUILD_SEM.release()


@tmplib_bp.route('/templates/<template_id>', methods=['DELETE'])
@require_admin
def delete_template(template_id):
    """API-11（F-51）：默认软删（deleted 标记 + 文件移 data/.trash/<id>-<utc>/）；
    hard=true 需管理员密码二次校验（与 filters 删除同一先例，错密码 403）。
    出厂模板（shipped/catalog-derived）是影子条目，不可删（F-19）。"""
    if not paths.valid_id(template_id):
        return _err('TL_TEMPLATE_UNKNOWN', f'非法模板 id: {template_id!r}', 404)
    root = paths.library_root()
    try:
        lib = paths.read_library(root)
    except ValueError as e:
        return _err('TL_DATA', str(e), 500)
    entry = ((lib or {}).get('templates') or {}).get(template_id)
    if entry is None:
        return _err('TL_TEMPLATE_UNKNOWN', f'模板 {template_id!r} 不存在', 404)
    body = request.get_json(silent=True) or {}
    hard = request.args.get('hard') == 'true' or bool(body.get('hard'))
    if entry.get('deleted') and not hard:
        return _err('TL_ALREADY_DELETED', f'模板 {template_id} 已软删', 409)
    if entry.get('origin') != 'catalog':
        return _err('TL_ORIGIN_PROTECTED',
                    f'{template_id} 是出厂模板（origin={entry.get("origin")}），'
                    '影子条目不可删（F-19）；只允许重建', 409)

    if hard:
        # 与 routes/filters.py 删除同一双保险：当前管理员密码二次校验
        password = body.get('password')
        if not password:
            return _err('TL_FORBIDDEN', '硬删需要提供管理员密码', 403)
        sess = get_session()
        try:
            user = sess.query(User).filter(
                User.username == session.get('username')).first()
        finally:
            sess.close()
        if not user or user.role != 'admin' or not user.check_password(password):
            return _err('TL_FORBIDDEN', '管理员密码错误，未执行删除', 403)

    files = [paths.template_yaml(root, template_id),
             root / 'data' / 'raw' / f'{template_id}.csv',
             paths.surface_npz(root, template_id),
             root / 'data' / 'surfaces' / f'{template_id}.qc.json']
    if hard:
        for f in files:
            f.unlink(missing_ok=True)
        for d in paths.trash_dir(root).glob(f'{template_id}-*'):
            import shutil as _sh
            _sh.rmtree(d, ignore_errors=True)
        del lib['templates'][template_id]
        _append_history(lib, 'delete', template_id, note='hard')
        paths.write_library(lib, root)
        engine.reset_caches()
        return jsonify({'code': 'TL_OK', 'id': template_id, 'mode': 'hard'})

    trash = paths.trash_target(template_id, root)
    trash.mkdir(parents=True, exist_ok=False)
    import shutil as _sh
    moved = []
    for f in files:
        if f.is_file():
            _sh.move(str(f), str(trash / f.name))
            moved.append(f.name)
    entry['deleted'] = {'at': _now_utc(), 'by': session.get('username'),
                        'mode': 'soft'}
    entry['state'] = 'deleted'
    lib['templates'][template_id] = entry
    _append_history(lib, 'delete', template_id,
                    note=f"soft; files→.trash/{trash.name}")
    paths.write_library(lib, root)
    engine.reset_caches()
    return jsonify({'code': 'TL_OK', 'id': template_id, 'mode': 'soft',
                    'moved': moved, 'trash': trash.name})


@tmplib_bp.route('/in_domain/<template_id>', methods=['GET'])
def in_domain_detail(template_id):
    """API-12：域内可答比例的缓存值（建面时实测写入，F-45；公开只读）。"""
    if not paths.valid_id(template_id):
        return _err('TL_TEMPLATE_UNKNOWN', f'非法模板 id: {template_id!r}', 404)
    entry = _library_entry(template_id)
    if not entry or entry.get('deleted'):
        return _err('TL_TEMPLATE_UNKNOWN', f'模板 {template_id!r} 不存在', 404)
    idom = entry.get('in_domain')
    return jsonify(_walk({
        'code': 'TL_OK', 'id': template_id,
        'in_domain': idom,
        'note': None if idom else '该模板尚无实测域内率（下次建面/重建时计算）',
        'definition': '一个点 = 建面所用表里的一对 (波段, 静止系 epoch)；'
                      'predict 返回有限星等即答上（F-45）',
    }))
