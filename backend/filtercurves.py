"""滤光片透过率曲线获取工具（供 routes/filters.py 调用）。

三种来源：
1. pcigale 自带库映射：读 data/filters/name=<名>.pickle（nm→Å，峰值归一）。
2. 用户上传文本曲线：校验（恰好两列数值、波长严格递增、透过率 0~1.5、
   点数下限）→ 峰值归一 → 生成 pcigale .dat 并注册进 pcigale。
3. SVO FPS：机读搜索（index.php?mode=search&search_text=）+ 曲线抓取
   （fps.php?ID= VOTable 优先，getdata.php?format=ascii 兜底）→ 注册 pcigale。

写库约定（FilterDef.extra_data，JSONB 需整体重赋值）：
    {
      'transmission': {'wl': [...Å 升序...], 'tr': [...峰值归一到 1...]},
      'pcigale_name': '...' | None,   # pcigale 库中的滤光片名；注册失败为 None
      'svo_id': '...' | None,         # None 表示 pcigale 自带库 / 用户上传
    }

pcigale 2025.0 的 `pcigale-filters add` CLI 在 numpy>=2 下因 np.trapz 崩溃，
这里进程内调用 pcigale_filters.add_filters() 并打 np.trapz=np.trapezoid shim
（同 scripts/fetch_svo_filters.py）。
"""
import html
import io
import pickle
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

SVO_BASE = 'https://svo2.cab.inta-csic.es/theory/fps'
SVO_FPS_URL = SVO_BASE + '/fps.php?ID={}'
SVO_ASCII_URL = SVO_BASE + '/getdata.php?format=ascii&id={}'
SVO_INFO_URL = SVO_BASE + '/index.php?id={}'
SVO_SEARCH_URL = SVO_BASE + '/index.php?mode=search&boton=Search&search_text={}'

HTTP_RETRIES = 2
HTTP_TIMEOUT = 30
PCIGALE_OUT_DIR = Path('/tmp/pcigale_filters')

MIN_CURVE_POINTS = 10
MAX_TR_VALUE = 1.5          # 上传曲线的原始透过率峰值上限（"略大于 1"）
MAX_SEARCH_RESULTS = 50

PCIGALE_FILTER_DIR_FALLBACK = Path(
    '/home/ajst/miniconda3/envs/burst_advocate/lib/python3.12/site-packages/'
    'pcigale/data/filters')


class CurveError(ValueError):
    """曲线数据/参数校验失败，message 面向用户（含行号或具体原因）。"""


def _pcigale_filter_dir():
    try:
        import pcigale
        return Path(pcigale.__file__).resolve().parent / 'data' / 'filters'
    except Exception:  # noqa: BLE001 - pcigale 未安装时退回落写路径
        return PCIGALE_FILTER_DIR_FALLBACK


def _fetch_url(url, retries=HTTP_RETRIES, timeout=HTTP_TIMEOUT):
    """带重试的 HTTP GET，返回 bytes。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'ajst-catalog/1.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - 网络错误类型繁多，统一重试
            last_err = e
            time.sleep(2 * attempt)
    raise RuntimeError(f'请求失败({retries} 次): {url}: {last_err}')


# ─── 峰值归一 ───
def normalize_tr(tr):
    m = max(tr)
    if m <= 0:
        raise CurveError('透过率最大值为 0，数据异常')
    return [x / m for x in tr]


# ─── 方式 1：pcigale 自带库 ───
def list_pcigale_builtin():
    """列出 pcigale 自带滤光片名（排除本系统自注册的 ajst_* 条目）。"""
    d = _pcigale_filter_dir()
    if not d.is_dir():
        raise RuntimeError(f'找不到 pcigale 滤光片目录: {d}')
    names = [p.name[len('name='):-len('.pickle')] for p in d.glob('name=*.pickle')]
    return sorted(n for n in names if not n.startswith('ajst_'))


def read_pcigale_builtin(name):
    """读 pcigale 自带库 pickle → (wl[Å], tr 已峰值归一)。库内波长单位为 nm。"""
    if not name or not re.fullmatch(r'[A-Za-z0-9_.\-]+', name):
        raise CurveError(f'非法的 pcigale 滤光片名: {name!r}')
    p = _pcigale_filter_dir() / f'name={name}.pickle'
    if not p.exists():
        raise CurveError(f'pcigale 自带库中找不到 {name}')
    import pcigale.data  # noqa: F401 - pickle 反序列化需要该类可导入
    with open(p, 'rb') as f:
        entry = pickle.load(f)
    wl = [float(x) * 10.0 for x in entry.wl]  # nm → Å
    tr = [float(x) for x in entry.tr]
    return wl, normalize_tr(tr)


# ─── 方式 2：上传文本曲线 ───
def parse_curve_text(text):
    """解析两列曲线文本 → (wl[Å], tr 原始值)。

    约定：第一列波长（Å）、第二列透过率；无表头；空白或 # 开头行忽略；
    列分隔符自动识别逗号/空白。校验失败抛 CurveError（含行号/原因）。
    """
    wl, tr, linenos = [], [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p for p in (line.split(',') if ',' in line else line.split()) if p != '']
        if len(parts) != 2:
            raise CurveError(
                f'第 {lineno} 行: 需要恰好两列（波长Å, 透过率），实际 {len(parts)} 列')
        try:
            w, t = float(parts[0]), float(parts[1])
        except ValueError:
            raise CurveError(f'第 {lineno} 行: 无法解析为数值: {line!r}')
        wl.append(w)
        tr.append(t)
        linenos.append(lineno)
    if len(wl) < MIN_CURVE_POINTS:
        raise CurveError(
            f'有效数据点不足: 需要至少 {MIN_CURVE_POINTS} 个，实际 {len(wl)} 个')
    for i, (w, t, ln) in enumerate(zip(wl, tr, linenos)):
        if w <= 0:
            raise CurveError(f'第 {ln} 行: 波长必须为正（实际 {w}）')
        if i > 0 and w <= wl[i - 1]:
            raise CurveError(
                f'第 {ln} 行: 波长必须严格递增（{wl[i - 1]} → {w}）')
        if t < 0:
            raise CurveError(f'第 {ln} 行: 透过率不能为负（实际 {t}）')
    if max(tr) > MAX_TR_VALUE:
        raise CurveError(
            f'透过率峰值 {max(tr):.4g} 过大：请先归一化到峰值 ≈ 1（允许 ≤ {MAX_TR_VALUE}）')
    return wl, tr


# ─── 方式 3：SVO FPS ───
def search_svo(keyword):
    """SVO FPS 机读模糊搜索 → [{id, facility, instrument, description}]。

    SVO 的搜索接口只输出 HTML（无 VOTable 列表接口），这里解析结果表格。
    """
    kw = (keyword or '').strip()
    if len(kw) < 2:
        raise CurveError('搜索关键词太短（至少 2 个字符）')
    page = _fetch_url(SVO_SEARCH_URL.format(urllib.parse.quote(kw))).decode('utf-8', 'replace')
    results = []
    for m in re.finditer(
            r'href="index\.php\?id=([^&"]+)[^"]*"[^>]*>([^<]+)</a>(.*?)</tr>',
            page, re.S):
        fid = html.unescape(m.group(1)).strip()
        # 滤光片 ID 形如 Facility/Instrument.Band
        if '/' not in fid or '.' not in fid:
            continue
        cells = [re.sub(r'<[^>]+>', '', c).strip()
                 for c in re.findall(r'<td[^>]*>(.*?)</td>', m.group(3), re.S)]
        facility = html.unescape(cells[-3]) if len(cells) >= 3 else ''
        instrument = html.unescape(cells[-2]) if len(cells) >= 2 else ''
        description = html.unescape(cells[-1]) if cells else ''
        results.append({'id': fid, 'facility': facility,
                        'instrument': instrument, 'description': description})
        if len(results) >= MAX_SEARCH_RESULTS:
            break
    return results


def fetch_svo_transmission(svo_id):
    """从 SVO FPS 拉透过率曲线 → (wl[Å], tr 原始值)。VOTable 优先，ascii 兜底。"""
    svo_id = (svo_id or '').strip()
    if not svo_id:
        raise CurveError('svo_id 不能为空')
    try:
        from astropy.io.votable import parse_single_table
        data = _fetch_url(SVO_FPS_URL.format(urllib.parse.quote(svo_id, safe='')))
        table = parse_single_table(io.BytesIO(data))
        arr = table.array
        wl = [float(x) for x in arr['Wavelength']]
        tr = [float(x) for x in arr['Transmission']]
        if wl:
            return wl, tr
    except Exception:  # noqa: BLE001 - VOTable 失败退 ascii
        pass
    data = _fetch_url(SVO_ASCII_URL.format(urllib.parse.quote(svo_id, safe='')))
    wl, tr = [], []
    for line in data.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                wl.append(float(parts[0]))
                tr.append(float(parts[1]))
            except ValueError:
                continue
    if not wl:
        raise RuntimeError(f'SVO 无此滤光片数据: {svo_id}')
    return wl, tr


# ─── pcigale 注册 ───
def derive_pcigale_name(fid):
    """从 AJST filter id 派生安全的 pcigale 滤光片名。"""
    return 'ajst_' + re.sub(r'[^A-Za-z0-9_.\-]', '_', str(fid)).lower()


def write_pcigale_file(pcigale_name, wl, tr, desc=''):
    """生成 pcigale 格式的滤光片文件（Å 两列 + 3 行 # 头），返回文件路径。"""
    PCIGALE_OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = PCIGALE_OUT_DIR / f'{pcigale_name}.dat'
    with open(path, 'w') as f:
        f.write(f'# {pcigale_name}\n')
        f.write('# photon\n')
        f.write(f'# {desc}\n')
        for w, t in zip(wl, tr):
            f.write(f'{w:.4f} {t:.6e}\n')
    return path


def pcigale_add(path):
    """注册滤光片到 pcigale 库（进程内调用 + np.trapz shim，见模块 docstring）。"""
    import numpy as np
    if not hasattr(np, 'trapz'):
        np.trapz = np.trapezoid
    from pcigale_filters import add_filters
    add_filters([str(path)])


def build_curve_extra(fid, curve):
    """按创建请求中的 curve 描述生成 extra_data 片段。

    curve = {'kind': 'pcigale_builtin', 'name': ...}
          | {'kind': 'upload', 'text': ...}
          | {'kind': 'svo', 'svo_id': ...}
    返回 (extra_data 片段, warning or None)。pcigale 注册失败不抛错：
    transmission 照常返回，pcigale_name 置 None，warning 说明原因。
    """
    kind = (curve or {}).get('kind')
    if kind == 'pcigale_builtin':
        name = (curve.get('name') or '').strip()
        wl, tr = read_pcigale_builtin(name)
        return {'transmission': {'wl': wl, 'tr': tr},
                'pcigale_name': name, 'svo_id': None}, None

    if kind == 'upload':
        wl, tr_raw = parse_curve_text(curve.get('text') or '')
        tr = normalize_tr(tr_raw)
        ed = {'transmission': {'wl': wl, 'tr': tr}, 'svo_id': None}
        return _with_pcigale_registration(fid, ed, wl, tr,
                                          desc=f'AJST filter {fid} (user upload)')

    if kind == 'svo':
        svo_id = (curve.get('svo_id') or '').strip()
        if not svo_id:
            raise CurveError('svo_id 不能为空')
        wl, tr_raw = fetch_svo_transmission(svo_id)
        tr = normalize_tr(tr_raw)
        ed = {'transmission': {'wl': wl, 'tr': tr}, 'svo_id': svo_id}
        return _with_pcigale_registration(
            fid, ed, wl, tr,
            desc=f'AJST filter {fid} from SVO FPS {SVO_INFO_URL.format(svo_id)}')

    raise CurveError(f'未知曲线获取方式: {kind!r}')


def _with_pcigale_registration(fid, ed, wl, tr, desc):
    name = derive_pcigale_name(fid)
    try:
        path = write_pcigale_file(name, wl, tr, desc)
        pcigale_add(path)
        ed['pcigale_name'] = name
        return ed, None
    except Exception as e:  # noqa: BLE001 - 注册失败不阻断创建
        ed['pcigale_name'] = None
        return ed, f'曲线已保存但 pcigale 注册失败，hostfit 暂不可用: {e}'
