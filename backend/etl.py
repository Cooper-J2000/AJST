#!/usr/bin/env python3
"""
ETL: 将 catadata/ 中的 CSV + JSON 数据导入 PostgreSQL。

用法:
  python3 etl.py                    # 全量重建（清空所有数据重灌）
  python3 etl.py --sync             # 增量同步：只更新新增或变动的源
  python3 etl.py --transient EPXXX  # 只更新指定源（支持多个）
  python3 etl.py --dump             # 库 → 文件（纯导出，不动任何文件）
  python3 etl.py --dump --prune     # 导出后把孤儿文件/陈旧 CSV 移到 backups/dump_prune_<时间>/
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from app import get_engine, get_session
from models import (Base, Transient, Lightcurve, FilterDef, Tag, Spectrum,
                    Article, HostGalaxy, utcnow, t0_to_mjd)
from models import refresh_distmod
from routes.tags import register_tags
import extinction

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('AJST_DATA_DIR', os.path.join(PROJECT_ROOT, 'catadata'))
LC_DIR = os.path.join(DATA_DIR, 'lc')
INFO_DIR = os.path.join(DATA_DIR, 'info')
FILTERS_FILE = os.path.join(DATA_DIR, 'filters.json')
TAGS_FILE = os.path.join(DATA_DIR, 'tags.json')
SPECTRA_DIR = os.path.join(DATA_DIR, 'spectra')
MJD_EPOCH = datetime(1858, 11, 17)

# CSV 列名到模型字段的映射
CSV_FIELD_MAP = {
    'time': 'time', 'time_err': 'time_err', 'time_unit': 'time_unit',
    'mjd': 'mjd',
    'band': 'band', 'flux_density': 'flux_density',
    'flux_density_err': 'flux_density_err', 'flux_density_unit': 'flux_density_unit',
    'mag_system': 'mag_system', 'Gext_corr': 'gext_corr', 'upperlimit': 'upperlimit',
    'Gext_Alambda': 'gext_Alambda',
    'mag_Gextcor': 'mag_gextcor', 'mag_Gextcor_err': 'mag_gextcor_err',
    'flux_density_Gextcor': 'flux_density_gextcor',
    'flux_density_Gextcor_err': 'flux_density_gextcor_err',
    'flux_density_Gextcor_unit': 'flux_density_gextcor_unit',
    'weights': 'weights', 'discard': 'discard', 'telescope': 'telescope',
    'instrument': 'instrument', 'reference': 'reference', 'comment': 'comment',
    'source': 'source',
}
BOOL_FIELDS = {'gext_corr', 'upperlimit', 'discard'}
FLOAT_FIELDS = {'time', 'time_err', 'mjd', 'flux_density', 'flux_density_err',
                'gext_Alambda', 'mag_gextcor', 'mag_gextcor_err',
                'flux_density_gextcor', 'flux_density_gextcor_err', 'weights'}
# ===================== 一致性检查 / 孤儿清理（2026-09-25）=====================
# 为什么需要：--dump 只写不删，而全量重建/--sync 以"文件存在"为触发条件
# （needs_update() 对库里不存在的源返回 True；from_dump 对 0 点源跳过写 CSV）。
# 于是"删掉一个源 / 删光某源的点"会被下一次重建或同步悄悄撤销。下面把识别与
# 安全闸都做成纯函数（好测、不连库），清理动作可逆（移到 backups/ 而不是删）。
PRUNE_MAX_ABS = 50        # 孤儿绝对数上限
PRUNE_MAX_FRAC = 0.20     # 或"现有源数"的这个比例，取两者较大者作为阈值


def list_data_files(info_dir=None, lc_dir=None):
    """扫描数据目录，返回 (info 文件 dict: id->路径, lc 文件 dict: id->路径)。"""
    info_dir = info_dir or INFO_DIR
    lc_dir = lc_dir or LC_DIR

    def scan(d, ext):
        out = {}
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.endswith(ext):
                    out[fn[:-len(ext)]] = os.path.join(d, fn)
        return out

    return scan(info_dir, '.json'), scan(lc_dir, '.csv')


def find_orphan_files(db_ids, info_files, lc_files):
    """孤儿 = 有 info/lc 文件但库里没有该源 → 重建/--sync 会把它当新源重新建出来。"""
    oi = sorted(pth for tid, pth in info_files.items() if tid not in db_ids)
    ol = sorted(pth for tid, pth in lc_files.items() if tid not in db_ids)
    return oi, ol


def find_stale_lc_files(empty_ids, lc_files):
    """陈旧 CSV = 库里有该源但光变点为 0，而文件里仍有数据行（读不了的文件跳过）。"""
    stale = []
    for tid in sorted(empty_ids):
        pth = lc_files.get(tid)
        if not pth:
            continue
        try:
            with open(pth, newline='') as f:
                rows = sum(1 for _ in csv.reader(f)) - 1     # 减去表头
        except OSError:
            continue
        if rows > 0:
            stale.append(pth)
    return stale


def prune_allowed(n_orphans, n_db, force=False):
    """安全闸：孤儿过多时拒绝清理（防库处于半空状态时把文件库整片搬走）。

    两条判据：库里 0 个源（典型的"清库后导入中断"）→ 一律拒绝；否则孤儿数不得超过
    max(50, 现有源数×0.2)。要越过请显式加 --prune-force。
    """
    if force:
        return True, ''
    if n_db == 0:
        return False, (f'库中 0 个源（半空状态）却有 {n_orphans} 个孤儿文件：'
                       f'疑似清库后导入未完成，拒绝清理。确认无误后加 --prune-force')
    frac = int(n_db * PRUNE_MAX_FRAC)
    limit = max(PRUNE_MAX_ABS, frac)
    if n_orphans > limit:
        return False, (f'孤儿 {n_orphans} 个 > 阈值 {limit}'
                       f'（= max({PRUNE_MAX_ABS}, {PRUNE_MAX_FRAC:.0%} × 现有 {n_db} 源)）：'
                       f'拒绝自动清理。确认库不是半空状态后加 --prune-force')
    return True, ''


def prune_backup_dir(data_dir=None, now=None):
    """prune 是搬家不是删除：所有文件移到 <data>/backups/dump_prune_<YYYYmmdd_HHMM>/。"""
    stamp = (now or datetime.now()).strftime('%Y%m%d_%H%M')
    return os.path.join(data_dir or DATA_DIR, 'backups', f'dump_prune_{stamp}')



def parse_bool(val):
    if val is None or val == '':
        return False
    return val.strip().lower() in ('y', 'yes', 'true', '1')


def parse_float(val):
    if val is None or val == '':
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def get_file_mtime(path):
    """获取文件最后修改时间戳"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


# ─── 时间单位 → 秒（供 import_one_lightcurve 的内联换算使用） ───
TIME_UNIT_MAP = {
    's': 1, 'sec': 1, 'second': 1, 'seconds': 1,
    'min': 60, 'm': 60, 'minute': 60, 'minutes': 60,
    'h': 3600, 'hour': 3600, 'hours': 3600,
    'd': 86400, 'day': 86400, 'days': 86400,
}


def list_available_tids():
    """扫描 info/ 和 lc/ 目录，找出所有可用的 transient ID"""
    tids = set()
    if os.path.isdir(INFO_DIR):
        for f in os.listdir(INFO_DIR):
            if f.endswith('.json'):
                tids.add(f.replace('.json', ''))
    if os.path.isdir(LC_DIR):
        for f in os.listdir(LC_DIR):
            if f.endswith('.csv'):
                tids.add(f.replace('.csv', ''))
    return sorted(tids)


def needs_update(tid):
    """检查某个源是否需要更新（文件比 DB 新，或者 DB 中不存在）"""
    info_file = os.path.join(INFO_DIR, f'{tid}.json')
    lc_file = os.path.join(LC_DIR, f'{tid}.csv')
    info_mtime = get_file_mtime(info_file)
    lc_mtime = get_file_mtime(lc_file)
    latest_file_mtime = max(info_mtime, lc_mtime)

    sess = get_session()
    try:
        t = sess.query(Transient).filter(Transient.id == tid).first()
        if t is None:
            return True  # DB 中不存在，需要新建
        # updated_at 为 naive UTC，按 UTC 求 epoch 与文件 mtime 比较
        db_updated = t.updated_at.replace(tzinfo=timezone.utc).timestamp() if t.updated_at else 0
        return latest_file_mtime > db_updated + 1  # 1秒容差
    finally:
        sess.close()


# ===================== 导入函数 =====================

def import_filters(sess, force=False):
    """导入滤波器定义（幂等 upsert）"""
    if not os.path.exists(FILTERS_FILE):
        print('[SKIP] filters.json not found')
        return
    with open(FILTERS_FILE) as f:
        raw = json.load(f)
    count = 0
    for fid, info in raw.items():
        existing = sess.query(FilterDef).filter(FilterDef.id == fid).first()
        if existing and not force:
            continue
        if existing:
            existing.wavelength = info.get('wavelength', 0)
            existing.filter_type = info.get('type')
            existing.vega2ab = info.get('Vega2AB', 0.0)
            existing.description = info.get('description')
            existing.extra_data = info.get('extra_data') or {}
            existing.gext_coeff = extinction.dust_coeff(existing.wavelength)
        else:
            sess.add(FilterDef(
                id=fid, wavelength=info.get('wavelength', 0),
                filter_type=info.get('type'), vega2ab=info.get('Vega2AB', 0.0),
                gext_coeff=extinction.dust_coeff(info.get('wavelength', 0)),
                description=info.get('description'),
                extra_data=info.get('extra_data') or {},
            ))
        count += 1
    sess.commit()
    print(f'  [OK] Filters: {count} checked/updated')


def import_one_transient(sess, tid):
    """导入单个暂现源的 info JSON"""
    info_file = os.path.join(INFO_DIR, f'{tid}.json')
    if not os.path.exists(info_file):
        return False

    with open(info_file) as f:
        data = json.load(f)

    # 解析 T0（一律按 UTC 原样存储：带时区标记也只取时间字面值，不做任何转换）
    t0 = None
    if data.get('T0'):
        try:
            t0 = datetime.fromisoformat(data['T0'].replace('Z', '+00:00'))
            if t0.tzinfo is not None:
                t0 = t0.replace(tzinfo=None)
        except (ValueError, TypeError):
            t0 = None

    # upsert
    t = sess.query(Transient).filter(Transient.id == tid).first()
    if t:
        t.ra = parse_float(data.get('ra'))
        t.dec = parse_float(data.get('dec'))
        t.t0 = t0
        t.t0_ref = data.get('T0_ref')
        t.t0_offset = parse_float(data.get('T0_offset'))
        t.t0_offset_ref = data.get('T0_offset_ref')
        t.trigger_instrument = data.get('Trigger_Instrument')
        t.redshift = parse_float(data.get('redshift'))
        t.redshift_type = data.get('redshift_type')
        t.redshift_ref = data.get('redshift_ref')
        t.pos_error = parse_float(data.get('pos_error'))
        t.pos_error_unit = data.get('pos_error_unit', 'arcsec')
        t.pos_ref = data.get('pos_ref')
        t.tags = data.get('tag', [])
        t.aliases = data.get('alias', [])
        t.comment = data.get('comment')
        t.sub_tag = data.get('sub_tag', [])
        if isinstance(data.get('extra_data'), dict):
            t.extra_data = data['extra_data']
        t.updated_at = utcnow()
        extinction.refresh_ebv(t)      # 坐标建立/变更 → 刷新 E(B-V) 缓存
        refresh_distmod(t)             # 红移建立/变更 → 刷新距离模数缓存
    else:
        t = Transient(
            id=tid, ra=parse_float(data.get('ra')), dec=parse_float(data.get('dec')),
            t0=t0, t0_ref=data.get('T0_ref'),
            t0_offset=parse_float(data.get('T0_offset')),
            t0_offset_ref=data.get('T0_offset_ref'),
            trigger_instrument=data.get('Trigger_Instrument'),
            redshift=parse_float(data.get('redshift')),
            redshift_type=data.get('redshift_type'), redshift_ref=data.get('redshift_ref'),
            pos_error=parse_float(data.get('pos_error')),
            pos_error_unit=data.get('pos_error_unit', 'arcsec'),
            pos_ref=data.get('pos_ref'), tags=data.get('tag', []),
            aliases=data.get('alias', []), comment=data.get('comment'),
            sub_tag=data.get('sub_tag', []),
            extra_data=data.get('extra_data') if isinstance(data.get('extra_data'), dict) else {},
        )
        sess.add(t)
        extinction.refresh_ebv(t)      # 坐标建立 → 计算 E(B-V) 缓存
        refresh_distmod(t)             # 红移 → 距离模数缓存
    sess.flush()

    # 该源的主/副 tag 登记进索引表（幂等；缺失描述留空）
    register_tags(sess, t.tags, 'main')
    register_tags(sess, t.sub_tag, 'sub')

    # 研究文章：info JSON 含 articles 字段时对该源做全量替换（与光变一致；
    # 字段缺失说明是旧格式文件，不动库中现有条目）
    if 'articles' in data:
        sess.query(Article).filter(Article.transient_id == tid).delete()
        for item in data.get('articles') or []:
            name = (item.get('name') or '').strip()
            url = (item.get('url') or '').strip()
            if not name or not url:
                continue
            src = item.get('source')
            # 来源归一：literature-mining / arxiv 一律写为 bot（统一显示口径）
            if str(src or '').strip().lower() in ('literature-mining', 'arxiv'):
                src = 'bot'
            sess.add(Article(
                transient_id=tid, name=name, url=url,
                title=item.get('title'), bibtex=item.get('bibtex'),
                source=src,
            ))
        sess.flush()

    # 宿主星系：info JSON 含 host_galaxy 字段时 upsert（null = 删除该源的宿主记录）
    if 'host_galaxy' in data:
        hg = data['host_galaxy']
        sess.query(HostGalaxy).filter(HostGalaxy.transient_id == tid).delete()
        if hg:
            host = HostGalaxy(
                transient_id=tid,
                ra=parse_float(hg.get('ra')), dec=parse_float(hg.get('dec')),
                redshift=parse_float(hg.get('redshift')),
                redshift_err=parse_float(hg.get('redshift_err')),
                redshift_type=hg.get('redshift_type'),
                photometry=hg.get('photometry') or [],
                derived=hg.get('derived') or {},
                comment=hg.get('comment'), source=hg.get('source'),
            )
            sess.add(host)
            extinction.refresh_ebv(host)   # 坐标建立 → 计算 E(B-V) 缓存
            refresh_distmod(host)          # 红移 → 距离模数缓存
        sess.flush()
    return True


def import_one_lightcurve(sess, tid):
    """导入单个暂现源的光变 CSV"""
    lc_file = os.path.join(LC_DIR, f'{tid}.csv')
    if not os.path.exists(lc_file):
        return 0, 0

    # 确保 transient 存在
    t = sess.query(Transient).filter(Transient.id == tid).first()
    if not t:
        print(f'  [WARN] {tid}: LC file exists but no info JSON, creating stub')
        t = Transient(id=tid)
        sess.add(t)
        extinction.refresh_ebv(t)      # 无坐标 → 置 None
        sess.flush()

    # 删除旧光变数据（该源的全量替换）
    sess.query(Lightcurve).filter(Lightcurve.transient_id == tid).delete()
    sess.flush()

    t0_mjd = t0_to_mjd(t.t0)   # 有 T0 时逐行补齐 MJD（权威时间列）
    count = 0
    errors = 0
    with open(lc_file, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lc = Lightcurve(transient_id=tid)
                for csv_col, model_field in CSV_FIELD_MAP.items():
                    val = row.get(csv_col, '').strip()
                    if val == '':
                        continue
                    if model_field in BOOL_FIELDS:
                        setattr(lc, model_field, parse_bool(val))
                    elif model_field in FLOAT_FIELDS:
                        setattr(lc, model_field, parse_float(val))
                    else:
                        setattr(lc, model_field, val)
                # 时间单位统一转为秒
                if lc.time_unit and lc.time_unit.lower().strip() != 's':
                    factor = TIME_UNIT_MAP.get(lc.time_unit.lower().strip())
                    if factor is None:
                        # 未知单位：不换算、保留原始单位标注（不能谎报为 's'，
                        # 否则时间数量级被静默改错）；下游对未知单位会跳过
                        print(f'  [WARN] {tid}: unknown time_unit '
                              f'{lc.time_unit!r}, time values kept unconverted')
                    else:
                        if factor != 1:
                            if lc.time is not None: lc.time *= factor
                            if lc.time_err is not None: lc.time_err *= factor
                        lc.time_unit = 's'
                # 流量/星等保留原始值与原始单位（mag/uJy/Jy/cgs/mJy 原样入库），
                # mJy 统一在银河系消光改正后写入 flux_density_gextcor 列
                # MJD 权威时间列：CSV 未带 mjd 列时由 time + T0 补齐（无 T0 留空）
                if lc.mjd is None and t0_mjd is not None and lc.time is not None:
                    lc.mjd = t0_mjd + lc.time / 86400.0
                sess.add(lc)
                count += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f'  [ERROR] {tid} row: {e}')
    sess.flush()
    return count, errors


def import_spectra(sess):
    """全量重建后：扫描 catadata/spectra/ 重建 spectra 表索引。
    光谱文件是权威存储（上传/删除时文件与库记录同步维护），
    此处只做 文件→库 的索引重建；transient 不存在的目录跳过。
    银河系消光改正谱（文件内 gext_corr=true）是依附原始谱的二级产物：
    两遍扫描，先建父行再按 parent_filename 回挂 parent_id。"""
    if not os.path.isdir(SPECTRA_DIR):
        print('  [SKIP] spectra dir not found')
        return
    count, child_count, errors, skipped, orphan = 0, 0, 0, 0, 0

    def _mk_row(tid, fname, obj_name, sp, parent_id=None):
        data = sp.get('data') or []
        wavs = [float(p[0]) for p in data if p]
        obs_date = None
        mjd = sp.get('time')
        if mjd not in (None, ''):
            try:
                obs_date = MJD_EPOCH + timedelta(days=float(mjd))
            except (TypeError, ValueError):
                obs_date = None
        extra = {
            'observer': sp.get('observer'), 'reducer': sp.get('reducer'),
            'u_fluxes': sp.get('u_fluxes'),
            'u_wavelengths': sp.get('u_wavelengths'),
            'mjd': sp.get('time'), 'sn_name': obj_name,
            'flux_type': sp.get('flux_type', 'absolute'),
            'has_err': any(len(p) > 2 for p in data),
            'source': 'file_scan', 'n_points': len(data),
        }
        if sp.get('gext_corr'):
            extra.update({'gext_corr': True, 'gext_ebv': sp.get('gext_ebv'),
                          'gext_rv': sp.get('gext_rv'),
                          'parent_filename': sp.get('parent_filename')})
        return Spectrum(
            transient_id=tid,
            filename=sp.get('filename') or fname[:-5],
            wavelength_min=min(wavs) if wavs else None,
            wavelength_max=max(wavs) if wavs else None,
            instrument=sp.get('instrument'),
            observation_date=obs_date,
            file_path=f'catadata/spectra/{tid}/{fname}',
            file_type='json',
            spec_type=(sp.get('spec_type') or 'transient')
                      if sp.get('spec_type') in ('transient', 'host', 'mix')
                      else 'transient',
            # 波长类型：vacuum/air，缺失/非法 → NULL（留空按空气波长处理）
            wavelength_type=(sp.get('wavelength_type')
                             if sp.get('wavelength_type') in ('vacuum', 'air')
                             else None),
            parent_id=parent_id,
            extra_data=extra,
        )

    for tid in sorted(os.listdir(SPECTRA_DIR)):
        tdir = os.path.join(SPECTRA_DIR, tid)
        if not os.path.isdir(tdir):
            continue
        if not sess.query(Transient).filter(Transient.id == tid).first():
            print(f'  [WARN] spectra/{tid}: transient not in DB, skipped')
            skipped += 1
            continue
        parsed = []
        for fname in sorted(os.listdir(tdir)):
            if not fname.endswith('.json'):
                continue
            try:
                with open(os.path.join(tdir, fname)) as f:
                    payload = json.load(f)
                obj_name, obj = next(iter(payload.items()))
                sp = obj.get('spectra', obj) if isinstance(obj, dict) else {}
                parsed.append((fname, obj_name, sp))
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f'  [ERROR] spectra {tid}/{fname}: {e}')
        # 第一遍：非改正文件建父行
        id_by_filename = {}
        for fname, obj_name, sp in parsed:
            if sp.get('gext_corr'):
                continue
            try:
                row = _mk_row(tid, fname, obj_name, sp)
                sess.add(row)
                sess.flush()
                id_by_filename[row.filename] = row.id
                count += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f'  [ERROR] spectra {tid}/{fname}: {e}')
        # 第二遍：改正文件回读 parent_filename 挂父行
        for fname, obj_name, sp in parsed:
            if not sp.get('gext_corr'):
                continue
            parent_id = id_by_filename.get(sp.get('parent_filename'))
            if parent_id is None:
                print(f'  [WARN] spectra {tid}/{fname}: parent '
                      f"{sp.get('parent_filename')} not found, skipped")
                orphan += 1
                continue
            try:
                sess.add(_mk_row(tid, fname, obj_name, sp, parent_id=parent_id))
                child_count += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f'  [ERROR] spectra {tid}/{fname}: {e}')
    sess.flush()
    print(f'  [OK] Spectra index rebuilt: {count} files + {child_count} corrected'
          f' ({errors} errors, {orphan} orphans, {skipped} dirs skipped)')


def ensure_default_tags(sess):
    """创建默认标签（幂等）。主 tag 层（kind='main'）。"""
    default_tags = [
        ('fxt', 'EP/FXT fast X-ray transient', '#e74c3c'),
        ('grb', 'Gamma-ray burst', '#3498db'),
        ('sn', 'Supernova', '#2ecc71'),
        ('tde', 'Tidal disruption event', '#9b59b6'),
    ]
    for name, desc, color in default_tags:
        existing = sess.query(Tag).filter(Tag.name == name, Tag.kind == 'main').first()
        if not existing:
            sess.add(Tag(name=name, kind='main', description=desc, color=color))
    sess.commit()


def import_tags(sess, force=False):
    """从 catadata/tags.json 导入 tag 索引（幂等 upsert；force 时描述/颜色以文件为准）"""
    if not os.path.exists(TAGS_FILE):
        print('[SKIP] tags.json not found')
        return
    with open(TAGS_FILE) as f:
        raw = json.load(f)
    items = raw if isinstance(raw, list) else raw.get('tags', [])
    count = 0
    for item in items:
        name = (item.get('name') or '').strip()
        kind = item.get('kind') if item.get('kind') in ('main', 'sub') else 'main'
        if not name:
            continue
        existing = sess.query(Tag).filter(Tag.name == name, Tag.kind == kind).first()
        if existing:
            if force:
                existing.description = item.get('description')
                existing.color = item.get('color')
                count += 1
            continue
        sess.add(Tag(name=name, kind=kind,
                     description=item.get('description'),
                     color=item.get('color')))
        count += 1
    sess.commit()
    print(f'  [OK] Tags: {count} imported/updated')


def register_used_tags(sess):
    """把 transients 表现役的主/副 tag 全部登记进索引表（缺失描述留空，幂等）"""
    main_rows = sess.execute(text(
        "SELECT DISTINCT elem FROM transients, LATERAL "
        "jsonb_array_elements_text(tags) elem "
        "WHERE jsonb_typeof(tags) = 'array'")).fetchall()
    sub_rows = sess.execute(text(
        "SELECT DISTINCT elem FROM transients, LATERAL "
        "jsonb_array_elements_text(sub_tag) elem "
        "WHERE jsonb_typeof(sub_tag) = 'array'")).fetchall()
    register_tags(sess, [r[0] for r in main_rows], 'main')
    register_tags(sess, [r[0] for r in sub_rows], 'sub')
    sess.commit()


def dump_tags(sess):
    """将 tag 索引表写回 catadata/tags.json（--dump 的一部分；全量覆盖）"""
    rows = sess.query(Tag).order_by(Tag.kind, Tag.name).all()
    out = [{'name': r.name, 'kind': r.kind, 'description': r.description,
            'color': r.color} for r in rows]
    with open(TAGS_FILE, 'w') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f'  [OK] Tags dumped: {len(out)} entries')


def dump_filters(sess):
    """将数据库中的滤波器定义写回 filters.json（--dump 的一部分）。
    保留文件中原有条目的顺序，新条目按 id 排序追加。"""
    rows = {r.id: r for r in sess.query(FilterDef).all()}
    old = {}
    if os.path.exists(FILTERS_FILE):
        with open(FILTERS_FILE) as f:
            old = json.load(f)
    out = {}
    for fid in old:
        if fid in rows:
            r = rows[fid]
            out[fid] = {'wavelength': r.wavelength, 'type': r.filter_type,
                        'Vega2AB': r.vega2ab, 'description': r.description}
            if r.extra_data:
                out[fid]['extra_data'] = r.extra_data
    for fid in sorted(set(rows) - set(old)):
        r = rows[fid]
        out[fid] = {'wavelength': r.wavelength, 'type': r.filter_type,
                    'Vega2AB': r.vega2ab, 'description': r.description}
        if r.extra_data:
            out[fid]['extra_data'] = r.extra_data
    with open(FILTERS_FILE, 'w') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f'  [OK] Filters dumped: {len(out)} entries')


def from_dump(sess, prune=False, prune_force=False):
    """将数据库当前内容导出回 catadata/ 文件。

    导出后做一致性检查：报告孤儿文件（库中已无该源）与陈旧 CSV（库中 0 点但文件有行）。
    prune=True 时把它们移到 backups/dump_prune_<时间>/（可逆）；孤儿数超过安全阈值且未给
    prune_force 时拒绝清理并以退出码 2 结束。
    """
    import csv as csv_mod
    import io

    os.makedirs(INFO_DIR, exist_ok=True)
    os.makedirs(LC_DIR, exist_ok=True)

    dump_filters(sess)
    dump_tags(sess)

    transients = sess.query(Transient).order_by(Transient.id).all()
    n_info = 0
    n_lc = 0
    empty_ids = set()

    for t in transients:
        # ── 写入 info JSON ──
        info = {
            'transient_id': t.id,
            'alias': t.aliases or [],
            'ra': t.ra,
            'dec': t.dec,
            'T0': (t.t0.isoformat() + 'Z' if t.t0 else None),
            'T0_ref': t.t0_ref,
            'T0_offset': t.t0_offset,
            'T0_offset_ref': t.t0_offset_ref,
            'Trigger_Instrument': t.trigger_instrument,
            'redshift': t.redshift,
            'tag': t.tags or [],
            'pos_error': t.pos_error,
            'pos_ref': t.pos_ref,
            'redshift_type': t.redshift_type,
            'redshift_ref': t.redshift_ref,
            'comment': t.comment,
            'sub_tag': t.sub_tag or [],
            'pos_error_unit': t.pos_error_unit or 'arcsec',
            'extra_data': t.extra_data or {},
        }
        # 研究文章一并落盘（articles 字段始终写入，空列表表示该源无文章，
        # 导入端按此字段做全量替换，保证全量重建不丢文章数据）
        arts = sess.query(Article).filter(
            Article.transient_id == t.id).order_by(Article.id).all()
        info['articles'] = [
            {k: v for k, v in {
                'name': a.name, 'title': a.title, 'url': a.url,
                'bibtex': a.bibtex, 'source': a.source,
            }.items() if v is not None}
            for a in arts
        ]
        # 宿主星系落盘（无宿主时字段缺失，清理步骤会去掉 None）
        hg = sess.query(HostGalaxy).filter(HostGalaxy.transient_id == t.id).first()
        if hg:
            info['host_galaxy'] = {k: v for k, v in {
                'ra': hg.ra, 'dec': hg.dec,
                'redshift': hg.redshift, 'redshift_err': hg.redshift_err,
                'redshift_type': hg.redshift_type,
                'photometry': hg.photometry or [],
                'derived': hg.derived or {},
                'comment': hg.comment, 'source': hg.source,
            }.items() if v is not None and v != {} and v != []}
        # 去掉 None 值和空扩展字段
        info = {k: v for k, v in info.items() if v is not None and v != {}}
        with open(os.path.join(INFO_DIR, f'{t.id}.json'), 'w') as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
        n_info += 1

        # ── 写入 lc CSV ──
        lcs = sess.query(Lightcurve).filter(
            Lightcurve.transient_id == t.id
        ).order_by(Lightcurve.time, Lightcurve.id).all()

        if lcs:
            fields = [
                'time', 'time_err', 'time_unit', 'mjd', 'band',
                'flux_density', 'flux_density_err', 'flux_density_unit',
                'mag_system', 'Gext_corr', 'upperlimit',
                'Gext_Alambda', 'mag_Gextcor', 'mag_Gextcor_err',
                'flux_density_Gextcor', 'flux_density_Gextcor_err',
                'flux_density_Gextcor_unit', 'weights', 'discard',
                'telescope', 'instrument', 'reference', 'comment',
                'source',
            ]
            with open(os.path.join(LC_DIR, f'{t.id}.csv'), 'w', newline='') as f:
                writer = csv_mod.writer(f)
                writer.writerow(fields)
                for lc in lcs:
                    row = []
                    for col in fields:
                        val = getattr(lc, CSV_FIELD_MAP.get(col, col), '')
                        if isinstance(val, bool):
                            val = 'y' if val else 'n'
                        elif val is None:
                            val = ''
                        row.append(val)
                    writer.writerow(row)
            n_lc += len(lcs)
        else:
            # 该源 0 个光变点：这里不写 CSV（保持原语义），旧文件会留成"陈旧 CSV"
            empty_ids.add(t.id)


    # ---- 一致性检查（总是报告；清理需显式 --prune）----
    db_ids = {t.id for t in transients}
    info_files, lc_files = list_data_files()
    orphan_info, orphan_lc = find_orphan_files(db_ids, info_files, lc_files)
    stale_lc = find_stale_lc_files(empty_ids, lc_files)
    n_orph = len(orphan_info) + len(orphan_lc)
    pending = orphan_info + orphan_lc + stale_lc

    print(f'\n{"=" * 50}')
    print(f'Dump complete: {n_info} info files, {n_lc} LC points')
    print(f'{"=" * 50}')

    if not pending:
        print('[OK] 一致性：文件与库一一对应（无孤儿、无陈旧 CSV）')
        return

    print(f'[WARN] 孤儿文件 {n_orph} 个（info {len(orphan_info)} / lc {len(orphan_lc)}）、'
          f'陈旧 CSV {len(stale_lc)} 个')
    for pth in pending[:10]:
        print('   -', os.path.relpath(pth, DATA_DIR))
    if len(pending) > 10:
        print(f'   …另 {len(pending) - 10} 个')
    print('   影响：下一次全量重建或 --sync 会把它们当"新源/旧点"灌回库。')
    if not prune:
        print('   清理：加 --prune（移到 backups/dump_prune_<时间>/，可逆）')
        return

    ok, why = prune_allowed(n_orph, len(db_ids), force=prune_force)
    if not ok:
        print(f'[REFUSE] {why}')
        sys.exit(2)

    dest = prune_backup_dir()
    os.makedirs(os.path.join(dest, 'info'), exist_ok=True)
    os.makedirs(os.path.join(dest, 'lc'), exist_ok=True)
    moved = 0
    for pth in orphan_info:
        os.replace(pth, os.path.join(dest, 'info', os.path.basename(pth)))
        moved += 1
    for pth in orphan_lc + stale_lc:
        os.replace(pth, os.path.join(dest, 'lc', os.path.basename(pth)))
        moved += 1
    print(f'[OK] 已移走 {moved} 个文件 -> {os.path.relpath(dest, DATA_DIR)}/'
          f'（恢复：把 info/ lc/ 里的文件拷回原位）')


# ===================== 主入口 =====================

def main():
    parser = argparse.ArgumentParser(description='AJST Catalog ETL')
    parser.add_argument('--sync', action='store_true',
                        help='增量同步：只更新文件有变动的源')
    parser.add_argument('--transient', nargs='+',
                        help='只更新指定 transient ID（可多个）')
    parser.add_argument('--transients', nargs='+', dest='transient',
                        help='(同 --transient)')
    parser.add_argument('--filters', action='store_true',
                        help='强制刷新滤波器定义')
    parser.add_argument('--dump', action='store_true',
                        help='将数据库当前内容导出回 catadata/ 文件（info JSON + lc CSV + filters.json）')
    parser.add_argument('--prune', action='store_true',
                        help='配合 --dump：把孤儿文件与陈旧 CSV 移到 backups/dump_prune_<时间>/'
                             '（默认只报告不清理；移到备份目录，可逆）')
    parser.add_argument('--prune-force', action='store_true',
                        help='孤儿数超过安全阈值（max(50, 现有源数×0.2)）时仍执行 --prune')
    args = parser.parse_args()

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    print('[OK] PostgreSQL connection')
    Base.metadata.create_all(engine)

    # ---- 判断要处理的 transient IDs ----
    if args.transient:
        tids = args.transient
        mode = '指定源'
    elif args.sync:
        all_tids = list_available_tids()
        tids = [tid for tid in all_tids if needs_update(tid)]
        mode = '增量同步'
        print(f'[INFO] 扫描 {len(all_tids)} 个可用源，需要更新: {len(tids)} 个')
    else:
        tids = None  # 全量
        mode = '全量重建'

    print(f'=== ETL: {mode} ===')

    sess = get_session()
    try:
        # ---- dump 是纯导出：必须先处理并返回，绝不能先跑 import（否则会用
        # 文件里的旧内容覆盖库中较新的数据，例如滤光片 extra_data） ----
        if args.dump:
            from_dump(sess, prune=args.prune, prune_force=args.prune_force)
            return

        # ---- 滤波器（增量模式只检查 DB 中是否有，没有才从文件导入） ----
        if args.filters or not args.sync:
            print('\n--- Filters ---')
            import_filters(sess, force=args.filters or not args.sync)
        else:
            # 增量模式下检查 DB 中是否已有滤波器
            existing_count = sess.query(FilterDef).count()
            if existing_count == 0 and os.path.exists(FILTERS_FILE):
                print('\n--- Filters (needed) ---')
                import_filters(sess, force=True)

        # ---- 标签（增量模式；全量模式必须在 TRUNCATE 之后重建，见下） ----
        if args.sync:
            ensure_default_tags(sess)
            import_tags(sess)   # tags.json 中缺失的条目补入（不覆盖已有描述）

        # ---- 全量模式：清空重建 ----
        if tids is None:
            print('\n--- Clearing old data ---')
            with engine.connect() as conn:
                conn.execute(text('TRUNCATE TABLE lightcurves, transient_tags, '
                                  'transients, filters, tags, extinction_corrections, '
                                  'spectra, images, fitting_results, host_galaxies '
                                  'RESTART IDENTITY CASCADE'))
                conn.commit()
            print('[OK] Cleared')

            # 重新导入 filters（被 truncate 了）
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            import_filters(sess, force=True)

            # 默认标签同样被 TRUNCATE 清掉，必须在清空之后重建；
            # tags.json（tag 索引表的落盘）随后以文件为准灌入
            ensure_default_tags(sess)
            import_tags(sess, force=True)

            tids = list_available_tids()
            print(f'\n--- Importing {len(tids)} transients ---')
            for tid in tids:
                import_one_transient(sess, tid)
            sess.commit()
            print(f'  [OK] {len(tids)} transients')

            # 数据中出现的主/副 tag 全部登记进索引表（tags.json 未覆盖的补空描述行）
            register_used_tags(sess)

            print(f'\n--- Importing lightcurves ---')
            total_lc = 0
            total_err = 0
            for tid in tids:
                c, e = import_one_lightcurve(sess, tid)
                total_lc += c
                total_err += e
            sess.commit()
            print(f'  [OK] {total_lc} points ({total_err} errors)')

            # spectra 表也被 TRUNCATE 清空，从文件重新建索引
            print(f'\n--- Rebuilding spectra index ---')
            import_spectra(sess)
            sess.commit()

            print(f'\n{"=" * 50}')
            print(f'Full rebuild: {len(tids)} transients, {total_lc} LC points')
            print(f'{"=" * 50}')

        # ---- 增量 / 指定源模式 ----
        else:
            if not tids:
                print('\n[OK] 所有源已是最新，无需更新')
                return

            total_lc = 0
            total_err = 0
            for tid in tids:
                print(f'\n--- {tid} ---')
                ok = import_one_transient(sess, tid)
                if ok:
                    print(f'  [OK] Info updated')
                c, e = import_one_lightcurve(sess, tid)
                total_lc += c
                total_err += e
                print(f'  [OK] LC: {c} points ({e} errors)')
            sess.commit()

            print(f'\n{"=" * 50}')
            print(f'{mode}: {len(tids)} transients, {total_lc} LC points')
            print(f'{"=" * 50}')

    finally:
        sess.close()


if __name__ == '__main__':
    main()
