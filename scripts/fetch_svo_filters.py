#!/usr/bin/env python
"""为 pcigale 准备 AJST 滤光片透过率曲线。

功能：
1. 内置 AJST filter id → (pcigale 滤光片名, SVO id) 映射表。
2. pcigale 自带库已有的滤光片：直接读 pickle 取透过率写入数据库
   FilterDef.extra_data，不下载。
3. 自带库没有的（如 Swift/UVOT、HST/WFPC2）：从 SVO FPS 拉透过率，写库的同时
   生成 pcigale 格式文件并注册进 pcigale 滤光片库。注册在进程内调用
   pcigale_filters.add_filters() 完成。

写入数据库的 extra_data 结构（JSONB，整体重赋值以保证 SQLAlchemy 跟踪）：
    {
      'transmission': {'wl': [...Å...], 'tr': [... 峰值归一到 1 ...]},
      'pcigale_name': '...',          # pcigale 库中的滤光片名
      'svo_id': '...' | None,         # None 表示用 pcigale 自带库
    }

用法（在仓库根目录或任意目录运行均可）：
    /home/ajst/ajst/bin/python scripts/fetch_svo_filters.py
    /home/ajst/ajst/bin/python scripts/fetch_svo_filters.py --force
    /home/ajst/ajst/bin/python scripts/fetch_svo_filters.py uvot-uvw2 uvot-v

幂等：已有 transmission 且已注册 pcigale 的默认跳过，--force 覆盖重做。
"""
import argparse
import pickle
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / 'backend'
sys.path.insert(0, str(BACKEND_DIR))

PCIGALE_FILTER_DIR = (
    Path('/home/ajst/ajst/lib/python3.12/site-packages/pcigale/data/filters')
)
PCIGALE_OUT_DIR = Path('/tmp/pcigale_filters')

SVO_FPS_URL = 'http://svo2.cab.inta-csic.es/theory/fps/fps.php?ID={}'
SVO_ASCII_URL = 'http://svo2.cab.inta-csic.es/theory/fps/getdata.php?format=ascii&id={}'
SVO_INFO_URL = 'http://svo2.cab.inta-csic.es/theory/fps/index.php?id={}'

# AJST filter id → (pcigale_name, svo_id 或 None 表示用 pcigale 自带库)
MAPPING = {
    # Generic/Johnson U B V R I —— pcigale 自带
    'U': ('generic.johnson.U', None),
    'B': ('generic.johnson.B', None),
    'V': ('generic.johnson.V', None),
    'R': ('generic.johnson.R', None),
    'I': ('generic.johnson.I', None),
    # SDSS u g r i z —— pcigale 自带
    'u': ('sloan.sdss.u', None),
    'g': ('sloan.sdss.g', None),
    'r': ('sloan.sdss.r', None),
    'i': ('sloan.sdss.i', None),
    'z': ('sloan.sdss.z', None),
    # 近红外 J H Ks —— 按任务要求映射到 2MASS（pcigale 内置；UKIRT 与 2MASS
    # J/H 近似，Ks 即 2MASS Ks）
    'J': ('2mass.J', None),
    'H': ('2mass.H', None),
    'Ks': ('2mass.Ks', None),
    # GALEX —— pcigale 自带
    'fuv': ('galex.FUV', None),
    'nuv': ('galex.NUV', None),
    # Swift/UVOT —— pcigale 无，需 SVO（UVOT 是光子计数探测器 → photon）
    'uvot-uvw2': ('swift.uvot.uvw2', 'Swift/UVOT.UVW2'),
    'uvot-uvm2': ('swift.uvot.uvm2', 'Swift/UVOT.UVM2'),
    'uvot-uvw1': ('swift.uvot.uvw1', 'Swift/UVOT.UVW1'),
    'uvot-u':    ('swift.uvot.u',    'Swift/UVOT.U'),
    'uvot-b':    ('swift.uvot.b',    'Swift/UVOT.B'),
    'uvot-v':    ('swift.uvot.v',    'Swift/UVOT.V'),
    # WISE —— pcigale 自带
    'wise-w1': ('wise.W1', None),
    'wise-w2': ('wise.W2', None),
    'wise-w3': ('wise.W3', None),
    'wise-w4': ('wise.W4', None),
    # Spitzer IRAC / MIPS —— pcigale 自带
    'Spitzer-IRAC.I1': ('spitzer.irac.I1', None),
    'Spitzer-IRAC.I2': ('spitzer.irac.I2', None),
    'Spitzer-IRAC.I3': ('spitzer.irac.I3', None),
    'Spitzer-IRAC.I4': ('spitzer.irac.I4', None),
    'Spitzer-MIPS.24mu': ('spitzer.mips.24mu', None),
    # HST ACS/WFC、WFC3/IR —— pcigale 自带
    'F606W': ('hst.acs.wfc.F606W', None),
    'F814W': ('hst.acs.wfc.F814W', None),
    'F105W': ('hst.wfc3.ir.F105W', None),
    'F110W': ('hst.wfc3.ir.F110W', None),
    'F125W': ('hst.wfc3.ir.F125W', None),
    'F160W': ('hst.wfc3.ir.F160W', None),
    # HST WFPC2 —— pcigale 无，需 SVO（取视场面积最大的 WF 通道曲线）
    'F702W': ('hst.wfpc2.wf.F702W', 'HST/WFPC2-WF.F702W'),
    # UKIRT Y —— pcigale 自带 WFCAM（UKIDSS 即用 WFCAM 观测）
    'Y': ('ukirt.wfcam.Y', None),
}

HTTP_RETRIES = 3
HTTP_TIMEOUT = 60


def fetch_url(url, binary=True):
    """带重试的 HTTP GET，返回 bytes。"""
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'ajst-catalog/1.0'})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - 网络错误类型繁多，统一重试
            last_err = e
            print(f'    第 {attempt} 次请求失败: {e}')
            time.sleep(5 * attempt)
    raise RuntimeError(f'下载失败({HTTP_RETRIES} 次): {url}: {last_err}')


def fetch_svo_transmission(svo_id):
    """从 SVO FPS 拉透过率曲线，返回 (wl[Å] list, tr list)。优先 VOTable，失败退 ascii。"""
    from astropy.io.votable import parse_single_table
    import io
    try:
        data = fetch_url(SVO_FPS_URL.format(svo_id))
        table = parse_single_table(io.BytesIO(data))
        arr = table.array
        wl = [float(x) for x in arr['Wavelength']]
        tr = [float(x) for x in arr['Transmission']]
        return wl, tr
    except Exception as e:  # noqa: BLE001
        print(f'    VOTable 接口失败({e})，尝试 ascii 接口')
    data = fetch_url(SVO_ASCII_URL.format(svo_id))
    wl, tr = [], []
    for line in data.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            wl.append(float(parts[0]))
            tr.append(float(parts[1]))
    if not wl:
        raise RuntimeError(f'SVO ascii 接口也无数据: {svo_id}')
    return wl, tr


def read_pcigale_builtin(pcigale_name):
    """读 pcigale 自带库 pickle，返回 (wl[Å] list, tr list)。库内波长单位为 nm。"""
    p = PCIGALE_FILTER_DIR / f'name={pcigale_name}.pickle'
    if not p.exists():
        raise RuntimeError(f'pcigale 自带库中找不到 {pcigale_name} ({p})')
    with open(p, 'rb') as f:
        entry = pickle.load(f)
    wl = [float(x) * 10.0 for x in entry.wl]  # nm → Å
    tr = [float(x) for x in entry.tr]
    return wl, tr


def normalize_tr(tr):
    m = max(tr)
    if m <= 0:
        raise RuntimeError('透过率最大值为 0，数据异常')
    return [x / m for x in tr]


def write_pcigale_file(pcigale_name, svo_id, wl, tr):
    """生成 pcigale 格式的滤光片文件，返回文件路径。"""
    PCIGALE_OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = PCIGALE_OUT_DIR / f'{pcigale_name}.dat'
    desc = (f'{pcigale_name} transmission from SVO FPS '
            f'{SVO_INFO_URL.format(svo_id)}')
    with open(path, 'w') as f:
        f.write(f'# {pcigale_name}\n')
        f.write('# photon\n')
        f.write(f'# {desc}\n')
        for w, t in zip(wl, tr):
            f.write(f'{w:.4f} {t:.6e}\n')
    return path


def pcigale_is_registered(pcigale_name):
    return (PCIGALE_FILTER_DIR / f'name={pcigale_name}.pickle').exists()


def pcigale_add(path):
    """注册滤光片到 pcigale 库。

    进程内调用 pcigale_filters.add_filters()（避免子进程 + 环境差异）。为防
    旧版 pcigale_filters 在 numpy>=2 下因 np.trapz 崩溃，先打兼容 shim。
    """
    import numpy as np
    if not hasattr(np, 'trapz'):
        np.trapz = np.trapezoid
    from pcigale_filters import add_filters
    add_filters([str(path)])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('filters', nargs='*',
                        help='只处理指定 AJST filter id（默认全部）')
    parser.add_argument('--force', action='store_true',
                        help='已有 transmission / 已注册 pcigale 的也重做')
    args = parser.parse_args()

    if not PCIGALE_FILTER_DIR.is_dir():
        sys.exit(f'错误: 找不到 pcigale 滤光片目录 {PCIGALE_FILTER_DIR}')

    from app import get_session
    from models import FilterDef

    wanted = args.filters or list(MAPPING)
    unknown = [f for f in wanted if f not in MAPPING]
    if unknown:
        sys.exit(f'错误: 映射表中没有这些 AJST filter id: {unknown}')

    sess = get_session()
    ok, skipped, failed = [], [], []
    try:
        db_ids = {row[0] for row in sess.query(FilterDef.id)}
        for fid in wanted:
            pcigale_name, svo_id = MAPPING[fid]
            print(f'== {fid} → {pcigale_name}' +
                  (f' (SVO: {svo_id})' if svo_id else ' (pcigale 自带)'))
            if fid not in db_ids:
                print('    跳过: 数据库 filters 表中无此 id')
                failed.append((fid, '数据库中无此 id'))
                continue
            row = sess.get(FilterDef, fid)
            ed = dict(row.extra_data or {})
            has_tr = isinstance(ed.get('transmission'), dict) and \
                ed['transmission'].get('wl')
            registered = pcigale_is_registered(pcigale_name)
            if has_tr and registered and not args.force:
                print('    跳过: 已有透过率数据且已注册 pcigale（--force 重做）')
                skipped.append(fid)
                continue
            try:
                if svo_id is None:
                    wl, tr = read_pcigale_builtin(pcigale_name)
                else:
                    wl, tr = fetch_svo_transmission(svo_id)
                    if not registered or args.force:
                        path = write_pcigale_file(pcigale_name, svo_id, wl, tr)
                        pcigale_add(path)
                        print(f'    已注册到 pcigale: {pcigale_name}')
                ed['transmission'] = {'wl': wl, 'tr': normalize_tr(tr)}
                ed['pcigale_name'] = pcigale_name
                ed['svo_id'] = svo_id
                row.extra_data = ed  # 整体重赋值，保证 JSONB 变更被跟踪
                sess.commit()
                print(f'    已写入 extra_data: {len(wl)} 点')
                ok.append(fid)
            except Exception as e:  # noqa: BLE001
                sess.rollback()
                print(f'    失败: {e}')
                failed.append((fid, str(e)))
    finally:
        sess.close()

    print()
    print(f'完成: 成功 {len(ok)}，跳过 {len(skipped)}，失败 {len(failed)}')
    if failed:
        for fid, err in failed:
            print(f'  失败: {fid}: {err}')
        sys.exit(1)


if __name__ == '__main__':
    main()
