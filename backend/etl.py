#!/usr/bin/env python3
"""
ETL: 将 catadata/ 中的 CSV + JSON 数据导入 PostgreSQL。

用法:
  python3 etl.py                    # 全量重建（清空所有数据重灌）
  python3 etl.py --sync             # 增量同步：只更新新增或变动的源
  python3 etl.py --transient EPXXX  # 只更新指定源（支持多个）
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import text
from app import get_engine, get_session
from models import Base, Transient, Lightcurve, FilterDef, Tag, utcnow

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('AJST_DATA_DIR', os.path.join(PROJECT_ROOT, 'catadata'))
LC_DIR = os.path.join(DATA_DIR, 'lc')
INFO_DIR = os.path.join(DATA_DIR, 'info')
FILTERS_FILE = os.path.join(DATA_DIR, 'filters.json')

# CSV 列名到模型字段的映射
CSV_FIELD_MAP = {
    'time': 'time', 'time_err': 'time_err', 'time_unit': 'time_unit',
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
}
BOOL_FIELDS = {'gext_corr', 'upperlimit', 'discard'}
FLOAT_FIELDS = {'time', 'time_err', 'flux_density', 'flux_density_err',
                'gext_Alambda', 'mag_gextcor', 'mag_gextcor_err',
                'flux_density_gextcor', 'flux_density_gextcor_err', 'weights'}


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


# ─── 时间单位 → 秒 ───
TIME_UNIT_MAP = {
    's': 1, 'sec': 1, 'second': 1, 'seconds': 1,
    'min': 60, 'm': 60, 'minute': 60, 'minutes': 60,
    'h': 3600, 'hour': 3600, 'hours': 3600,
    'd': 86400, 'day': 86400, 'days': 86400,
}


def to_seconds(value, unit):
    """将时间和时间误差统一转为秒"""
    if value is None:
        return None, 's'
    factor = TIME_UNIT_MAP.get(unit.strip().lower()) if unit else None
    if factor and factor != 1:
        return value * factor, 's'
    return value, 's'


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
        else:
            sess.add(FilterDef(
                id=fid, wavelength=info.get('wavelength', 0),
                filter_type=info.get('type'), vega2ab=info.get('Vega2AB', 0.0),
                description=info.get('description'),
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
    else:
        t = Transient(
            id=tid, ra=parse_float(data.get('ra')), dec=parse_float(data.get('dec')),
            t0=t0, trigger_instrument=data.get('Trigger_Instrument'),
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
        sess.flush()

    # 删除旧光变数据（该源的全量替换）
    sess.query(Lightcurve).filter(Lightcurve.transient_id == tid).delete()
    sess.flush()

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
                    if factor and factor != 1:
                        if lc.time is not None: lc.time *= factor
                        if lc.time_err is not None: lc.time_err *= factor
                    lc.time_unit = 's'
                # 流量/星等保留原始值与原始单位（mag/uJy/Jy/cgs/mJy 原样入库），
                # mJy 统一在银河系消光改正后写入 flux_density_gextcor 列
                sess.add(lc)
                count += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f'  [ERROR] {tid} row: {e}')
    sess.flush()
    return count, errors


def ensure_default_tags(sess):
    """创建默认标签（幂等）"""
    default_tags = [
        ('fxt', 'EP/FXT fast X-ray transient', '#e74c3c'),
        ('grb', 'Gamma-ray burst', '#3498db'),
        ('sn', 'Supernova', '#2ecc71'),
        ('tde', 'Tidal disruption event', '#9b59b6'),
    ]
    for name, desc, color in default_tags:
        existing = sess.query(Tag).filter(Tag.name == name).first()
        if not existing:
            sess.add(Tag(name=name, description=desc, color=color))
    sess.commit()


def from_dump(sess):
    """将数据库当前内容导出回 catadata/ 文件"""
    import csv as csv_mod
    import io

    os.makedirs(INFO_DIR, exist_ok=True)
    os.makedirs(LC_DIR, exist_ok=True)

    transients = sess.query(Transient).order_by(Transient.id).all()
    n_info = 0
    n_lc = 0

    for t in transients:
        # ── 写入 info JSON ──
        info = {
            'transient_id': t.id,
            'alias': t.aliases or [],
            'ra': t.ra,
            'dec': t.dec,
            'T0': (t.t0.isoformat() + 'Z' if t.t0 else None),
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
        # 去掉 None 值和空扩展字段
        info = {k: v for k, v in info.items() if v is not None and v != {}}
        with open(os.path.join(INFO_DIR, f'{t.id}.json'), 'w') as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
        n_info += 1

        # ── 写入 lc CSV ──
        lcs = sess.query(Lightcurve).filter(
            Lightcurve.transient_id == t.id
        ).order_by(Lightcurve.time).all()

        if lcs:
            fields = [
                'time', 'time_err', 'time_unit', 'band',
                'flux_density', 'flux_density_err', 'flux_density_unit',
                'mag_system', 'Gext_corr', 'upperlimit',
                'Gext_Alambda', 'mag_Gextcor', 'mag_Gextcor_err',
                'flux_density_Gextcor', 'flux_density_Gextcor_err',
                'flux_density_Gextcor_unit', 'weights', 'discard',
                'telescope', 'instrument', 'reference', 'comment',
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

    print(f'\n{"=" * 50}')
    print(f'Dump complete: {n_info} info files, {n_lc} LC points')
    print(f'{"=" * 50}')


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
                        help='将数据库当前内容导出回 catadata/ 文件（info JSON + lc CSV）')
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

        # ---- 标签 ----
        if tids is None or args.sync:
            ensure_default_tags(sess)

        # ---- 全量模式：清空重建 ----
        if args.dump:
            from_dump(sess)
            return
        if tids is None:
            print('\n--- Clearing old data ---')
            with engine.connect() as conn:
                conn.execute(text('TRUNCATE TABLE lightcurves, transient_tags, '
                                  'transients, filters, tags, extinction_corrections, '
                                  'spectra, images, fitting_results RESTART IDENTITY CASCADE'))
                conn.commit()
            print('[OK] Cleared')

            # 重新导入 filters（被 truncate 了）
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            import_filters(sess, force=True)

            tids = list_available_tids()
            print(f'\n--- Importing {len(tids)} transients ---')
            for tid in tids:
                import_one_transient(sess, tid)
            sess.commit()
            print(f'  [OK] {len(tids)} transients')

            print(f'\n--- Importing lightcurves ---')
            total_lc = 0
            total_err = 0
            for tid in tids:
                c, e = import_one_lightcurve(sess, tid)
                total_lc += c
                total_err += e
            sess.commit()
            print(f'  [OK] {total_lc} points ({total_err} errors)')

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
