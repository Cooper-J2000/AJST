"""L1: ETL 孤儿文件识别与 prune 安全闸（纯函数，不连库）。

背景（2026-09-25）：`etl.py --dump` 只写不删，而全量重建/`--sync` 以"文件存在"
为触发条件（`needs_update()` 对库里不存在的源返回 True）——于是"删掉一个源"这个
动作会被下一次重建/同步悄悄撤销。这里把识别与安全闸抽成纯函数并锁死行为。

判据：
  - 孤儿 = 有 info/lc 文件但库里没有该源 → 重建时会重新建出来。
  - 陈旧 CSV = 库里有该源但其光变点为 0，而 CSV 仍有数据行 → from_dump 的
    `if lcs:` 跳过写文件，旧内容留在盘上，重建时灌回。
  - prune 安全闸：孤儿过多时拒绝自动清理（防库处于半空状态时把文件库整片搬走）。
"""
from datetime import datetime

import pytest

etl = pytest.importorskip('etl', reason='需要 backend 在 sys.path（见 tests/conftest.py）')


# ── 1. 孤儿识别 ────────────────────────────────────────────────────────────

def test_orphan_detection_basic():
    info = {'A': '/d/info/A.json', 'GHOST': '/d/info/GHOST.json'}
    lc = {'A': '/d/lc/A.csv', 'GONE': '/d/lc/GONE.csv'}
    oi, ol = etl.find_orphan_files({'A'}, info, lc)
    assert oi == ['/d/info/GHOST.json']
    assert ol == ['/d/lc/GONE.csv']


def test_orphan_detection_clean():
    info = {'A': '/d/info/A.json'}
    lc = {'A': '/d/lc/A.csv'}
    assert etl.find_orphan_files({'A'}, info, lc) == ([], [])


def test_orphan_detection_empty_db_is_all_orphans():
    """库为空（半空状态）时，所有文件都算孤儿 —— 这正是安全闸要拦的场景。"""
    oi, ol = etl.find_orphan_files(set(), {'A': 'a', 'B': 'b'}, {'A': 'x'})
    assert len(oi) == 2 and len(ol) == 1


def test_list_data_files(tmp_path):
    (tmp_path / 'info').mkdir()
    (tmp_path / 'lc').mkdir()
    (tmp_path / 'info' / 'A.json').write_text('{}')
    (tmp_path / 'info' / 'notes.txt').write_text('x')      # 非 .json 不认
    (tmp_path / 'lc' / 'A.csv').write_text('time\n1\n')
    info, lc = etl.list_data_files(str(tmp_path / 'info'), str(tmp_path / 'lc'))
    assert list(info) == ['A'] and list(lc) == ['A']
    assert etl.list_data_files(str(tmp_path / 'nope'), str(tmp_path / 'nope')) == ({}, {})


# ── 2. 陈旧 CSV（库里 0 点，文件有行）─────────────────────────────────────

def test_stale_lc_detected(tmp_path):
    p = tmp_path / 'A.csv'
    p.write_text('time,flux_density\n1,2\n2,3\n')
    assert etl.find_stale_lc_files({'A'}, {'A': str(p)}) == [str(p)]


def test_header_only_csv_is_not_stale(tmp_path):
    """只有表头 = 确实 0 点，不算陈旧（不该被清）。"""
    p = tmp_path / 'A.csv'
    p.write_text('time,flux_density\n')
    assert etl.find_stale_lc_files({'A'}, {'A': str(p)}) == []


def test_no_lc_file_is_not_stale():
    """库里 0 点且没有 CSV —— 正常（无光变源的常态），不报。"""
    assert etl.find_stale_lc_files({'A'}, {}) == []


def test_stale_skips_unreadable(tmp_path):
    """文件读不了不崩（只读检查脚本会跑在真实数据上）。"""
    assert etl.find_stale_lc_files({'A'}, {'A': str(tmp_path / 'missing.csv')}) == []


# ── 3. prune 安全闸 ────────────────────────────────────────────────────────

def test_prune_allowed_small_batch():
    ok, why = etl.prune_allowed(3, 100)
    assert ok and why == ''


def test_prune_refused_when_over_absolute_limit():
    """阈值 = max(50, 20% × 现有源数)：50 个源时上限 50。"""
    ok, why = etl.prune_allowed(51, 50)
    assert not ok and '50' in why and '--prune-force' in why


def test_prune_allowed_at_absolute_limit():
    assert etl.prune_allowed(50, 50)[0] is True


def test_prune_refused_when_over_fractional_limit():
    """源多时按 20% 卡：1000 源上限 200。"""
    assert etl.prune_allowed(200, 1000)[0] is True
    assert etl.prune_allowed(201, 1000)[0] is False


def test_prune_force_overrides_guard():
    ok, why = etl.prune_allowed(9999, 10, force=True)
    assert ok and why == ''


def test_prune_guard_handles_empty_db():
    """半空库（0 源）时，任何孤儿都超阈值 —— 必须拒绝。"""
    ok, _ = etl.prune_allowed(1, 0)
    assert not ok


# ── 4. 备份目录命名（prune 是可逆的：移到 backups/dump_prune_<时间>/）────

def test_prune_backup_dir_name():
    d = etl.prune_backup_dir('/data', now=datetime(2026, 9, 25, 12, 0))
    assert d == '/data/backups/dump_prune_20260925_1200'
    assert 'backups' in etl.prune_backup_dir()
