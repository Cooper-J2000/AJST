"""
GCN 通告存档浏览与在线更新
GET  /api/gcn/ids            — 全部 circular id（数值升序，带缓存）
GET  /api/gcn/<cid>          — 单期 circular JSON 内容
GET  /api/gcn/<cid>/related  — 库中与该期 GCN 相关的光变记录（reference 精确 + 暴名模糊）
GET  /api/gcn/status         — 存档概况 + 更新任务状态
POST /api/gcn/update         — 从 NASA GCN 下载最新整包并替换存档（需登录，后台线程）
"""
import json
import os
import re
import shutil
import tarfile
import threading
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify
from sqlalchemy import cast, String, or_

from app import get_session, require_auth
from config import GCN_ARCHIVE_DIR
from models import Lightcurve, Transient

gcn_bp = Blueprint('gcn', __name__)

GCN_ARCHIVE_URL = 'https://gcn.nasa.gov/circulars/archive.json.tar.gz'

# ─── id 列表缓存（目录 mtime + 条目数作失效签名） ───
_ids_cache = {'ids': [], 'signature': None}


def _scan_ids():
    try:
        entries = os.listdir(GCN_ARCHIVE_DIR)
        sig = (os.path.getmtime(GCN_ARCHIVE_DIR), len(entries))
    except OSError:
        _ids_cache.update(ids=[], signature=None)
        return []
    if _ids_cache['signature'] == sig:
        return _ids_cache['ids']
    ids = []
    for fn in entries:
        if fn.endswith('.json'):
            try:
                ids.append(int(fn[:-5]))
            except ValueError:
                continue
    ids.sort()
    _ids_cache.update(ids=ids, signature=sig)
    return ids


def _invalidate_cache():
    _ids_cache['signature'] = None


@gcn_bp.route('/ids')
def list_ids():
    return jsonify({'ids': _scan_ids()})


@gcn_bp.route('/<int:cid>')
def get_circular(cid):
    path = os.path.join(GCN_ARCHIVE_DIR, f'{cid}.json')
    if not os.path.isfile(path):
        return {'error': 'Not found'}, 404
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {'error': f'读取失败: {e}'}, 500
    return jsonify(data)


# ─── 关联光变记录 ───
# 暴名 token 提取（正则与前端 gcn_tool.js / tools/gcn_index.py 保持一致）
_GRB_RE = re.compile(r'\bGRB\s?(\d{6})([A-Z])?\b', re.I)
_EP_RE = re.compile(r'\bEP\s?(2\d{5}[a-z])\b', re.I)

# 高能波段（X 射线/伽马，如 10keV、0.3-10keV）：通常不在 GCN 中报道，关联面板不显示
_HE_BAND_SQL = r'\d+(\.\d+)?\s*(kev|mev|gev)\M|^(kev|mev|gev)$'


def _extract_name_tokens(data):
    text = (data.get('subject') or '') + '\n' + (data.get('body') or '')
    tokens = set()
    for m in _GRB_RE.finditer(text):
        tokens.add(('GRB' + m.group(1) + (m.group(2) or '')).upper())
    for m in _EP_RE.finditer(text):
        tokens.add('EP' + m.group(1).lower())
    return sorted(tokens)


@gcn_bp.route('/<int:cid>/related')
def related_lightcurves(cid):
    """库中与该期 GCN 相关的光变记录，供阅读工具对照核对。
    exact: reference 明确写有 GCN<cid>（或 extra_data.gcn_id 等于 cid）
    fuzzy: 正文中出现的暴名能匹配到库中的源（id 或别名），取其全部光变记录
    """
    path = os.path.join(GCN_ARCHIVE_DIR, f'{cid}.json')
    if not os.path.isfile(path):
        return {'error': 'Not found'}, 404
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {'error': f'读取失败: {e}'}, 500

    sess = get_session()
    try:
        # 1) reference 精确命中：'GCN44171' / 'GCN 44171' / '...(GCN11024)' 等写法，
        #    期号前后不得再跟数字，避免 GCN11024 误中 1102
        pattern = rf'gcn\s*#?\s*{cid}(?![0-9])'
        exact = (sess.query(Lightcurve)
                 .filter(or_(
                     Lightcurve.reference.op('~*')(pattern),
                     Lightcurve.extra_data['gcn_id'].astext == str(cid),
                 ))
                 .filter(~Lightcurve.band.op('~*')(_HE_BAND_SQL))
                 .order_by(Lightcurve.transient_id, Lightcurve.time)
                 .all())
        exact_ids = {lc.id for lc in exact}

        # 2) 暴名模糊匹配：token → 源（id 前缀 / 别名子串，含 "GRB 250610B" 空格变体）
        tokens = _extract_name_tokens(data)
        conds = []
        for tok in tokens[:20]:
            variants = {tok}
            m = re.match(r'([A-Za-z]+)(\d.*)', tok)
            if m:
                variants.add(f'{m.group(1)} {m.group(2)}')
            for v in variants:
                conds.append(Transient.id.ilike(v))
                conds.append(Transient.id.ilike(v + '%'))
                conds.append(cast(Transient.aliases, String).ilike(f'%{v}%'))
        fuzzy = []
        matched_transients = {}
        if conds:
            transients = (sess.query(Transient).filter(or_(*conds))
                          .order_by(Transient.id).limit(10).all())
            tids = [t.id for t in transients]
            for t in transients:
                low_id = t.id.lower()
                hits = [tok for tok in tokens
                        if low_id.startswith(tok.lower().replace(' ', ''))
                        or any(tok.lower().replace(' ', '') in
                               str(a).lower().replace(' ', '') for a in (t.aliases or []))]
                matched_transients[t.id] = hits
            if tids:
                fuzzy = [lc for lc in (sess.query(Lightcurve)
                         .filter(Lightcurve.transient_id.in_(tids))
                         .filter(~Lightcurve.band.op('~*')(_HE_BAND_SQL))
                         .order_by(Lightcurve.transient_id, Lightcurve.time)
                         .limit(400).all())
                         if lc.id not in exact_ids]

        return jsonify({
            'circular_id': cid,
            'tokens': tokens,
            'matched_transients': matched_transients,
            'exact': [lc.to_dict() for lc in exact],
            'fuzzy': [lc.to_dict() for lc in fuzzy],
        })
    finally:
        sess.close()


# ─── 在线更新（后台线程 + 状态轮询） ───
_update_status = {
    'state': 'idle',      # idle / downloading / extracting / done / error
    'message': '',
    'started_at': None,
    'finished_at': None,
}
_update_lock = threading.Lock()


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _set_update(state, message=''):
    _update_status.update(state=state, message=message)
    if state in ('done', 'error'):
        _update_status['finished_at'] = _utcnow_iso()


def _run_update():
    base = os.path.dirname(GCN_ARCHIVE_DIR)          # catadata/gcn
    tar_path = os.path.join(base, 'archive.json.tar.gz')
    extract_tmp = os.path.join(base, '_gcn_extract_tmp')
    backup_dir = os.path.join(base, 'archive.backup')
    try:
        # 1. 下载
        _set_update('downloading', f'正在下载 {GCN_ARCHIVE_URL} ...')
        urllib.request.urlretrieve(GCN_ARCHIVE_URL, tar_path)

        # 2. 解压到临时目录（整包内含 archive.json/ 顶层目录）
        _set_update('extracting', '下载完成，正在解压...')
        if os.path.exists(extract_tmp):
            shutil.rmtree(extract_tmp)
        os.makedirs(extract_tmp)
        with tarfile.open(tar_path, 'r:gz') as tar:
            tar.extractall(path=extract_tmp)
        new_archive = os.path.join(extract_tmp, 'archive.json')
        if not os.path.isdir(new_archive) or not any(
                fn.endswith('.json') for fn in os.listdir(new_archive)):
            raise RuntimeError('解压结果无效：未找到 archive.json/ 或其中无 JSON 文件')

        # 3. 替换（旧目录先改名备份，失败可回滚）
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        if os.path.isdir(GCN_ARCHIVE_DIR):
            os.rename(GCN_ARCHIVE_DIR, backup_dir)
        try:
            os.rename(new_archive, GCN_ARCHIVE_DIR)
        except Exception:
            if os.path.isdir(backup_dir):
                os.rename(backup_dir, GCN_ARCHIVE_DIR)
            raise

        # 4. 清理
        shutil.rmtree(extract_tmp, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
        if os.path.exists(tar_path):
            os.unlink(tar_path)
        _invalidate_cache()
        _set_update('done', f'存档已更新，共 {len(_scan_ids())} 期')
    except Exception as e:
        # 回滚：新目录不存在且备份还在时恢复备份
        if not os.path.isdir(GCN_ARCHIVE_DIR) and os.path.isdir(backup_dir):
            os.rename(backup_dir, GCN_ARCHIVE_DIR)
        shutil.rmtree(extract_tmp, ignore_errors=True)
        if os.path.exists(tar_path):
            os.unlink(tar_path)
        _invalidate_cache()
        _set_update('error', f'更新失败: {e}')


@gcn_bp.route('/status')
def archive_status():
    ids = _scan_ids()
    try:
        mtime = datetime.fromtimestamp(
            os.path.getmtime(GCN_ARCHIVE_DIR), timezone.utc).isoformat()
    except OSError:
        mtime = None
    return jsonify({
        'count': len(ids),
        'latest_id': ids[-1] if ids else None,
        'archive_mtime': mtime,
        'update': dict(_update_status),
    })


@gcn_bp.route('/update', methods=['POST'])
@require_auth
def update_archive():
    with _update_lock:
        if _update_status['state'] in ('downloading', 'extracting'):
            return {'error': '已有更新任务正在进行', 'state': _update_status['state']}, 409
        _update_status.update(state='downloading', message='启动下载...',
                              started_at=_utcnow_iso(), finished_at=None)
        threading.Thread(target=_run_update, daemon=True).start()
    return {'status': 'started'}
