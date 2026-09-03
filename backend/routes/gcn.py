"""
GCN 通告在线反代

GET  /api/gcn/ids?page=first|last|N — 期号页：first（无参数同义，最新 100 期）/
                                        last（先取总数再查）/ N（第 N 页）；响应含 totalItems
GET  /api/gcn/ids?around=N          — 二分定位包含期号 N 的页（响应含 pos = N 在页内下标）
GET  /api/gcn/<cid>                 — 反代单期 circular JSON（gcn.nasa.gov/circulars/<cid>.json）
GET  /api/gcn/<cid>/related         — 库中与该期 GCN 相关的光变记录（先反代 JSON 提取暴名）
"""
import json
import math
import re
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request
from sqlalchemy import cast, String, or_

from app import get_session
from models import Lightcurve, Transient

gcn_bp = Blueprint('gcn', __name__)

GCN_CIRCULAR_URL = 'https://gcn.nasa.gov/circulars/{cid}.json'

# GCN 官网列表页 loader 端点：返回 items（circularId）+ totalItems；limit 上限 100
GCN_LIST_URL = ('https://gcn.nasa.gov/circulars?_data=routes%2Fcirculars._archive._index'
                '&limit={limit}&page={page}&view=index')
GCN_LIST_PAGE = 100

_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36')
_FETCH_TIMEOUT = 20


def _http_get(url):
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        return resp.read()


# ─── 期号列表 ───

def _fetch_page(page):
    """第 page 页的 (ids, totalItems)；第 1 页 = 最新，页内降序"""
    url = GCN_LIST_URL.format(limit='' if page == 1 else GCN_LIST_PAGE,
                              page='' if page == 1 else page)
    data = json.loads(_http_get(url))
    ids = []
    for it in data.get('items') or []:
        try:
            ids.append(int(it['circularId']))
        except (KeyError, TypeError, ValueError):
            continue
    return ids, int(data.get('totalItems') or 0)


def _last_page_num():
    _, total = _fetch_page(1)
    return max(1, math.ceil(total / GCN_LIST_PAGE))


def _parse_page(raw):
    """?page= 解析：None/空/first → 1；last → 最后一页；否则正整数，非法抛 ValueError"""
    s = str(raw or '').strip().lower()
    if s in ('', 'first'):
        return 1
    if s == 'last':
        return _last_page_num()
    try:
        page = int(s)
    except ValueError:
        raise ValueError('page 必须为 first / last 或 ≥ 1 的整数')
    if page < 1:
        raise ValueError('page 必须为 ≥ 1 的整数')
    return page


def _locate_page(cid):
    """二分定位包含 cid 的页 → (page, pos)；不在列表中 → (None, -1)"""
    lo, hi = 1, _last_page_num()
    while lo <= hi:
        mid = (lo + hi) // 2
        ids, _ = _fetch_page(mid)
        if not ids:
            break
        first, last = ids[0], ids[-1]
        if cid > first:
            hi = mid - 1
        elif cid < last:
            lo = mid + 1
        else:
            try:
                return mid, ids.index(cid)
            except ValueError:
                return mid, -1
    return None, -1


# ─── 单期内容 ───

def _fetch_circular(cid):
    """反代单期 JSON；上游 404 → None"""
    try:
        raw = _http_get(GCN_CIRCULAR_URL.format(cid=cid))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return json.loads(raw)


# ─── 关联光变记录 ───

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


# ─── 路由 ───

@gcn_bp.route('/ids')
def list_ids():
    """期号页：?page=first|last|N，或 ?around=N 二分定位（响应含 pos）"""
    around = request.args.get('around', type=int)
    if around is not None:
        try:
            p, pos = _locate_page(around)
        except Exception as e:
            return {'error': f'获取 GCN 期号列表失败: {e}'}, 502
        if p is None:
            return {'error': f'期号 {around} 不在 GCN 列表中'}, 404
        ids, total = _fetch_page(p)
        return jsonify({'ids': ids, 'total': total, 'page': p, 'pos': pos})
    try:
        page = _parse_page(request.args.get('page'))
    except ValueError as e:
        return {'error': str(e)}, 400
    try:
        ids, total = _fetch_page(page)
    except Exception as e:
        return {'error': f'获取 GCN 期号列表失败: {e}'}, 502
    return jsonify({'ids': ids, 'total': total, 'page': page})


@gcn_bp.route('/<cid>')
def get_circular(cid):
    """反代单期 circular JSON"""
    try:
        cid = int(cid)
    except ValueError:
        return {'error': 'Not found'}, 404
    try:
        data = _fetch_circular(cid)
    except Exception as e:
        return {'error': f'获取 GCN #{cid} 失败: {e}'}, 502
    if data is None:
        return {'error': 'Not found'}, 404
    return jsonify(data)


@gcn_bp.route('/<cid>/related')
def related_lightcurves(cid):
    """库中与该期 GCN 相关的光变记录。两级匹配：
    exact — reference 含 GCN<cid> 或 extra_data.gcn_id == cid
    fuzzy — 正文暴名 token 命中库中源（id 或别名），取全部光变记录
    """
    try:
        cid = int(cid)
    except ValueError:
        return {'error': 'Not found'}, 404
    try:
        data = _fetch_circular(cid)
    except Exception as e:
        return {'error': f'获取 GCN #{cid} 失败: {e}'}, 502
    if data is None:
        return {'error': 'Not found'}, 404

    sess = get_session()
    try:
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
