"""specphot.upload —— API-7（POST /parse）薄壳。

解析核心全部在 reader（F-75 文本语法 / F-76 的 FITS、ECSV 分支；V-18…V-20、
F-79②、spec_hash 三格式同管线）。本文件只做：请求体形态与格式探测分发
（F-76：magic bytes / ECSV 头判定，不猜）、format_hint 闭集闸、计算闸门
（ST-12：上传件与计算端点共用 ST-1 信号量）、LoadedSpectrum → API-7 响应
形状的装配。不落盘、不入库、不缓存谱数组（RO-5/ST-12：数组只在请求周期内持有）。

传输形态（P2+ 裁量，登记）：FITS 是二进制 ⇒ 请求体带 `content_b64`
（base64，标准字母表，容忍内嵌换行空白）；ECSV 本就是 ASCII 文本 ⇒ 与
txt 同走 `text` 字段。T-81① 的跨格式不变量（spec_hash 三串相同）不受影响：
spec_hash 只吃规范化后的数值数组。
"""
import base64
import binascii
import re

from flask import jsonify, request


from . import (specphot_bp, SPEC_PHOT_VERSION, _compute_gate, _err,
               _load_error_response)
from .preprocess import errcol_verdict
from .reader import (load_upload_text, load_upload_fits, load_upload_ecsv,
                     SpecLoadError)

_FORMATS = ('txt', 'fits', 'ecsv')     # P2+ 放开 fits/ecsv（F-76）
_ECSV_MAGIC_RE = re.compile(r'^#\s*%ECSV\b')
_B64_WS_RE = re.compile(r'\s+')


def _detect_text_format(text):
    """文本负载的格式探测：ECSV 头行 `# %ECSV` ⇒ ecsv，否则 txt（F-75 语法）。"""
    return 'ecsv' if _ECSV_MAGIC_RE.match(text.lstrip()) else 'txt'


def _decode_fits_payload(body):
    """content_b64 → FITS 字节；缺字段/坏 base64/非 FITS magic ⇒ E-14 闭集。"""
    b64 = body.get('content_b64')
    if not isinstance(b64, str) or not b64.strip():
        raise SpecLoadError('E-14', "format_hint='fits' 需要 content_b64（base64）字段",
                            reason='missing_content_b64')
    try:
        data = base64.b64decode(_B64_WS_RE.sub('', b64), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SpecLoadError('E-14', f'content_b64 不是合法 base64: {exc}',
                            reason='bad_base64')
    if not data.startswith(b'SIMPLE'):
        raise SpecLoadError('E-14', '解码后内容缺 FITS magic（SIMPLE=），不是 FITS 文件',
                            reason='unsupported_format')
    return data


@specphot_bp.route('/parse', methods=['POST'])
def parse():
    if not _compute_gate.acquire(blocking=False):
        return _err('server_busy', '已有计算在跑，请稍后重试',
                    429, reason='busy', retry_after_s=2)
    try:
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return _err('bad_request_state', '请求体应为 JSON 对象',
                        400, reason='upload_unparseable')
        raw_hint = body.get('format_hint')
        if raw_hint is None or (isinstance(raw_hint, str) and not raw_hint.strip()):
            fmt = None                          # 缺省 ⇒ 内容探测定格式（F-76）
        else:
            fmt = str(raw_hint).strip().lower()
            if fmt not in _FORMATS:
                return _err('bad_request_state',
                            f"format_hint 只接受 {'/'.join(_FORMATS)}；收到 {fmt!r}",
                            400, reason='upload_format_unsupported')

        try:
            if fmt is None and isinstance(body.get('content_b64'), str) \
                    and body['content_b64'].strip():
                fmt = 'fits'                    # 探测：base64 负载 ⇒ 走 FITS（magic 下验）
            if fmt == 'fits':
                data = _decode_fits_payload(body)
                detected = 'fits'
            else:
                text = body.get('text')
                if not isinstance(text, str) or not text.strip():
                    # Q-28：空串或全注释视为列数不足
                    return _err('bad_request_state', 'text 为空或没有数据行',
                                400, reason='upload_unparseable')
                detected = _detect_text_format(text)
                if fmt is None:
                    fmt = detected              # 探测权威（fits 走不到这里）
            if fmt != detected:
                return _err('bad_request_state',
                            f"format_hint={fmt!r} 与内容探测结果 {detected!r} 不符",
                            400, reason='upload_format_unsupported')

            source = body.get('source')
            source = source if source in ('upload', 'paste') else 'upload'
            lambda_frame = body.get('lambda_frame')
            lambda_frame = lambda_frame if isinstance(lambda_frame, str) else None
            try:
                if fmt == 'fits':
                    ls = load_upload_fits(
                        data, lambda_frame=lambda_frame, source=source,
                        hdu=body.get('hdu'),
                        col_lambda=body.get('col_lambda'),
                        col_flux=body.get('col_flux'),
                        col_err=body.get('col_err'))
                elif fmt == 'ecsv':
                    ls = load_upload_ecsv(text, lambda_frame=lambda_frame,
                                          source=source)
                else:
                    ls = load_upload_text(text, lambda_frame=lambda_frame,
                                          source=source)
            except SpecLoadError as e:
                return _load_error_response(e)
        except SpecLoadError as e:
            return _load_error_response(e)

        st = ls['read_stats']
        # P1b（V-22/W-37）：解析即回显 errcol_verdict ⇒ 前端 U-53 的两选一必须
        # 在计算之前给出（缺该确认 ⇒ 「计算」禁用并列进缺项清单，IA-16）。
        # 判定用原始装载流量（parse 不做消光，口径与 F-110① 的 V-3a 域一致）。
        ev = errcol_verdict(ls['flux_flambda_cgs'], ls['flux_err'])
        return jsonify({
            'ok': True,
            'spec_phot_version': SPEC_PHOT_VERSION,
            'format': fmt,
            'warnings': ls['warnings'],
            'errcol_verdict': ev['verdict'],
            'spec_hash': ls['spec_hash'],
            'n_points': st['n_points'],
            'lam_aa': ls['lam_aa'],
            'flux': ls['flux_flambda_cgs'],
            'flux_err': ls['flux_err'],
            'columns': 3 if st['sigma_method'] == 'column' else 2,
            'flux_unit': ls['meta']['u_fluxes'],
            'flux_unit_assumed': ls['meta']['flux_unit_assumed'],
            'meta': ls['meta'],
            'meta_provenance': ls['meta_provenance'],
            'read_stats': st,
            'flux_median_cgs': ls['flux_median_cgs'],
            'lambda_frame_converted': ls['lambda_frame_converted'],
            'n_below_convert': ls['n_below_convert'],
        })
    finally:
        _compute_gate.release()
