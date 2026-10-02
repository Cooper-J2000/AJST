"""specphot 代码侧注册表（02 §12 M-1）。

给库内已有透射曲线的 29 个波段登记曲线口径 `curve_kind`：
  - 'transmission'：滤光片透过率（不含探测器/大气等系统响应）
  - 'throughput'  ：系统总响应曲线

判定依据（M-1）：宿主 filtercurves 的三条来源分支（pcigale 自带库 / 用户上传 /
SVO FPS）解析的都只是透过率，且无条件峰值归一 ⇒ 现网 29 条一律 'transmission'；
只有作者手工换成实测系统响应曲线的条目才标 'throughput'。

为什么不写进 filters.extra_data：`etl.py --filters` 会整块覆盖该字段
（M-1 已登记此坑），口径键会被静默清空 ⇒ 口径只活在代码侧。
曲线本体在 filters.extra_data['transmission']（峰值归一、Å 升序，
约定见 backend/filtercurves.py:10-15），运行时由 meta.py 读取，本表不复制曲线。

数据快照：2026-09-30 只读查询生产库（filters 共 81 行，29 行有 transmission）。
"""

# {filter_id: {'curve_kind': ..., 'source_note': ...}}
# source_note 记曲线来源（pcigale 自带库名 / SVO FPS id），供 CA-01 与排查用。
CURVE_REGISTRY = {
    'fuv':            {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 galex.FUV'},
    'uvot-uvw2':      {'curve_kind': 'transmission', 'source_note': 'SVO FPS Swift/UVOT.UVW2'},
    'uvot-uvm2':      {'curve_kind': 'transmission', 'source_note': 'SVO FPS Swift/UVOT.UVM2'},
    'nuv':            {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 galex.NUV'},
    'uvot-uvw1':      {'curve_kind': 'transmission', 'source_note': 'SVO FPS Swift/UVOT.UVW1'},
    'uvot-u':         {'curve_kind': 'transmission', 'source_note': 'SVO FPS Swift/UVOT.U'},
    'U':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 generic.johnson.U'},
    'u':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 sloan.sdss.u'},
    'uvot-b':         {'curve_kind': 'transmission', 'source_note': 'SVO FPS Swift/UVOT.B'},
    'B':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 generic.johnson.B'},
    'g':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 sloan.sdss.g'},
    'uvot-v':         {'curve_kind': 'transmission', 'source_note': 'SVO FPS Swift/UVOT.V'},
    'V':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 generic.johnson.V'},
    'r':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 sloan.sdss.r'},
    'R':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 generic.johnson.R'},
    'i':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 sloan.sdss.i'},
    'I':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 generic.johnson.I'},
    'z':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 sloan.sdss.z'},
    'J':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 2mass.J'},
    'H':              {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 2mass.H'},
    'Ks':             {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 2mass.Ks'},
    'wise-w1':        {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 wise.W1'},
    'Spitzer-IRAC.I1': {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 spitzer.irac.I1'},
    'Spitzer-IRAC.I2': {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 spitzer.irac.I2'},
    'wise-w2':        {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 wise.W2'},
    'Spitzer-IRAC.I3': {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 spitzer.irac.I3'},
    'Spitzer-IRAC.I4': {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 spitzer.irac.I4'},
    'wise-w3':        {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 wise.W3'},
    'wise-w4':        {'curve_kind': 'transmission', 'source_note': 'pcigale 自带库 wise.W4'},
}

CURVE_KINDS = ('transmission', 'throughput')

for _fid, _entry in CURVE_REGISTRY.items():
    assert _entry['curve_kind'] in CURVE_KINDS, _fid


def has_curve(filter_id):
    """该波段是否登记了透射/响应曲线（未登记 ⇒ 走 E-04 / mono 路径）。"""
    return filter_id in CURVE_REGISTRY


def curve_kind(filter_id):
    """登记口径（'transmission' / 'throughput'）；未登记返回 None。"""
    entry = CURVE_REGISTRY.get(filter_id)
    return entry['curve_kind'] if entry else None


def source_note(filter_id):
    """曲线来源说明；未登记返回 None。"""
    entry = CURVE_REGISTRY.get(filter_id)
    return entry['source_note'] if entry else None


def registered_filter_ids():
    """全部已登记波段（按 id 排序的 tuple，便于稳定遍历）。"""
    return tuple(sorted(CURVE_REGISTRY))


def registry_fingerprint():
    """注册表内容指纹（sha256(排序 id 列表)[:12]），API-1 回显用。"""
    import hashlib
    return hashlib.sha256(','.join(registered_filter_ids()).encode('utf-8')).hexdigest()[:12]


# ─── M-6 线表复核（P3/P3c 前置，02 §12 M-6 行）──────────────────────────────
# M-6 尚未开始：宿主线表静止系波长全是整数 Å 且未标帧，需对照 NIST ASD 逐条登记
# line_frame 与 f_source 后才有判据（F-38②：整数 Å + 帧未标 ⇒ 无法从数值反推）。
# 两张表是 P3b/P3c 各能力的**解锁开关**，现状全 None ⇒ 相关能力闸门后
# （null + 书面原因 / E-14，见 lines.py / diagnostics.py / photometry.py）：
#   - M6_LINE_TABLE：z_from_lines（F-81）的静止系候选线表（宿主 spec_lines.js 的
#     复核版）。条目形态 {'species', 'lambda0_rest_aa', 'line_frame' ∈ {'air',
#     'vacuum'}, 'f_source'}；air 条目进互相关前经 wavconvert 换真空恰好一次。
#   - M6_FRAME_FEATURES：frame_probe（F-85）的大气/天光参考特征真空位置表（判据
#     是「整档偏移 0.92–2.20 Å 落在哪一帧」，参考位置本身也要 M-6 复核后才可信
#     ——C_MASK_EMIS_TABLE 的整数 Å 中心既不是 air 也不是 vacuum 精确值）。
#     条目形态 {'name', 'kind' ∈ {'abs', 'emis'}, 'lambda_vac_aa'}。
# 登记动作属数据准备（不改主项目代码，与 D-6 同落点）；本期只立占位与闸门，
# 不伪造任何条目。验收测试的数值路径用测试内合成 fixture（monkeypatch 本模块
# 两键）模拟 M-6 复核态——库内默认态恒为闸门后。
M6_LINE_TABLE = None
M6_FRAME_FEATURES = None


def m6_line_table_verified():
    """M-6 线表复核是否完成（非空且逐条带帧）。闸门唯一判定点。"""
    return bool(M6_LINE_TABLE) and all(
        isinstance(e, dict) and e.get('line_frame') in ('air', 'vacuum')
        and float(e.get('lambda0_rest_aa') or 0) > 0 for e in M6_LINE_TABLE)


def m6_frame_features_verified():
    """M-6 的 frame_probe 参考特征复核是否完成。"""
    return bool(M6_FRAME_FEATURES) and all(
        isinstance(e, dict) and e.get('kind') in ('abs', 'emis')
        and float(e.get('lambda_vac_aa') or 0) > 0 for e in M6_FRAME_FEATURES)

