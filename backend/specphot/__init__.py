"""specphot —— 「光谱 × 滤光片」工具（#/tools/specphot）的后端包。

设计权威：`Workplace_Agent/AJST-enrich/spec_x_filter_fuction_design_review/02_精简版_核心规格.md`
（条款 ID：F-*/V-*/C_* 等）。本包全链只读：不写库、上传件不落盘。

本文件按 02 §6.1 落地：蓝图 specphot_bp + 版本戳 + 计算闸门（ST-1/ST-12）
+ 依赖可用性探测（API-6 / ST-14）+ 结果缓存骨架（ST-3）+ 统一错误出口（A-3/A-5）。
app.py 挂载 = 2 行（import + register_blueprint，url_prefix='/api/specphot'，A-2）。
路由分布在各模块（meta.py: API-1/5/6/8；upload.py: API-7；photometry.py: API-2；
continuum_api.py: API-3；lines_api.py: API-4，P3 切片 2），本文件末尾导入它们以触发
装饰器注册。
"""
import threading
from collections import OrderedDict

from flask import Blueprint, jsonify

from .constants import C_SPEC_PHOT_VERSION, C_CACHE_MAX

specphot_bp = Blueprint('specphot', __name__)

SPEC_PHOT_VERSION = C_SPEC_PHOT_VERSION

# ST-1 / ST-12：进程内计算闸门，非阻塞获取；上传件解析（API-7）与计算端点
# （API-2/3/4，随期次落地）共用同一信号量，拿不到立即 E-12（429）
_compute_gate = threading.BoundedSemaphore(1)

# ST-14：依赖可用性在首次需要时探测一次（函数内延迟 import + 双检锁），
# 结果供 API-6 的 deps{} 与 M-7 自检消费；禁止每个请求 try/except 试探
_deps = {}
_deps_lock = threading.Lock()

# ST-3 / A-4：结果缓存（LRU，上限 C_CACHE_MAX）。键含 spec_hash 与请求摘要，
# 只缓存可低成本复算的结果；不含任何服务端会话状态（A-7）。
# 计算端点随期次接入，本期只有 health 回显条目数。
_result_cache = OrderedDict()
_cache_lock = threading.Lock()


def probe_deps():
    """deps{astropy, dust_extinction, dustmaps} 可用性（探测一次后缓存）。"""
    if _deps:
        return dict(_deps)
    with _deps_lock:
        if not _deps:
            for mod in ('astropy', 'dust_extinction', 'dustmaps'):
                try:
                    __import__(mod)
                    _deps[mod] = True
                except Exception:  # noqa: BLE001 - 缺包即不可用，不分类
                    _deps[mod] = False
    return dict(_deps)


def gate_status():
    """闸门余量（非阻塞试拿即放，不改变闸门状态）。"""
    free = _compute_gate.acquire(blocking=False)
    if free:
        _compute_gate.release()
    return {'free': 1 if free else 0, 'max': 1}


def cache_stats():
    with _cache_lock:
        return {'entries': len(_result_cache), 'max': C_CACHE_MAX}


# ─── 统一错误出口（A-3：宿主无 501/504 handler，一律显式 jsonify；
#     A-5：错误响应同样带 spec_phot_version 与 warnings[]） ─────────────

def _err(code, message, http=400, reason=None, **extra):
    body = {'error': message, 'code': code,
            'spec_phot_version': SPEC_PHOT_VERSION, 'warnings': []}
    if reason is not None:
        body['reason'] = reason
    body.update(extra)
    return jsonify(body), http


# reader/fluxcal 的 SpecLoadError → §5.3 的线上 code/HTTP/reason 映射
_WIRE_CODE = {
    'E-01': 'spectrum_not_found',
    'E-02': 'wavelength_not_sorted',
    'E-03': 'not_absolute_flux',
    'E-04': 'band_unusable',
    'E-07': 'input_over_limit',
    'E-08': 'vega_or_st_unavailable',
    'E-10': 'anchor_unavailable',
    'E-11': 'compute_timeout',
    'E-13': 'feature_disabled',
    'E-14': 'bad_request_state',
}
_HTTP_CODE = {'E-01': 404, 'E-10': 409, 'E-11': 504, 'E-13': 501}   # 其余默认 400（E-07 按下表细分）
_E07_413_REASONS = {'too_many_points', 'body_too_big'}   # §5.3 E-07：这两项 413，其余 400
_REASON_MAP = {
    # reader 内部 reason → §5.3/Q-* 的线上 reason（无映射者原样透传）
    'empty': 'upload_unparseable',
    'bad_row': 'upload_unparseable',
    'ragged_columns': 'upload_unparseable',          # 附 first_bad_line（E-22 的保留字段）
    'wavelength_out_of_range': 'upload_unparseable',
    'bad_wavelength_unit': 'wavelength_unit_suspect',
    'unsupported_format': 'upload_format_unsupported',
    # F-76 P2+（FITS/ECSV）：载体损坏类归 upload_unparseable；格式闸类归
    # upload_format_unsupported；ambiguous_hdu/ambiguous_columns/missing_column/
    # bad_hdu/no_lambda_axis/nonlinear_wcs 原样透传 —— 它们是「请指定扩展/列名」
    # 的可操作出口（T-81②），并回显 details 里的候选清单。
    'bad_base64': 'upload_unparseable',
    'missing_content_b64': 'upload_unparseable',
    'bad_fits': 'upload_unparseable',
    'bad_ecsv': 'upload_unparseable',
    'no_data_hdu': 'upload_unparseable',
    'unsupported_fits_shape': 'upload_unparseable',
}


def _load_error_response(e):
    """SpecLoadError → 线上错误响应（§5.3 合并后的 code + reason 闭集）。"""
    wire = _WIRE_CODE.get(e.code, 'bad_request_state')
    reason = _REASON_MAP.get(e.reason, e.reason)
    if e.code == 'E-07':
        http = 413 if reason in _E07_413_REASONS else 400
    else:
        http = _HTTP_CODE.get(e.code, 400)
    return _err(wire, str(e), http, reason=reason, **(e.details or {}))


# 触发各模块的 @specphot_bp.route 注册（放末尾避免循环导入）
from . import meta as _meta_routes        # noqa: E402,F401  API-1/5/6/8
from . import photometry as _photometry_routes  # noqa: E402,F401  API-2
from . import continuum_api as _continuum_routes  # noqa: E402,F401  API-3
from . import lines_api as _lines_routes  # noqa: E402,F401  API-4（P3 切片 2）
from . import upload as _upload_routes    # noqa: E402,F401  API-7

# A-3：宿主无 501/504 handler ⇒ 本模块 SpecLoadError（含 ST-5 超时的 E-11/504）
# 统一在此显式 jsonify 兜底；各视图内已就地转换的不会走到这里。
from .reader import SpecLoadError as _SpecLoadError   # noqa: E402


@specphot_bp.errorhandler(_SpecLoadError)
def _specload_error(e):
    return _load_error_response(e)

__all__ = ['specphot_bp', 'SPEC_PHOT_VERSION', 'probe_deps', 'gate_status',
           'cache_stats']
