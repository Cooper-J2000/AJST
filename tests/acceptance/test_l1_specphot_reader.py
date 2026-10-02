"""L1: specphot reader —— S0 装载（F-73…F-80）与读/上传校验（V-1…V-20）。

覆盖条款：
  V-1…V-17（库内谱读侧）、V-18…V-20（上传侧）、F-73/F-74/F-75/F-77/F-79/F-80
  T-40（元数据优先级，redshift 四情形 + F-77 各键）
  T-41（上传闸门子集：too_few_points / 列数不一致行号 / 字节闸）
  T-43（spec_hash 语义）、T-44（与宿主语法一致性，私函数级比对）
  T-45（上传件框架一次性变换）、T-53（库内谱不重复转换，调用计数）
  M-3（定标键只读文件侧，绝不读 extra_data）
golden：库内真实谱文件（字符串行 89 行文件 / gext 二级谱 / GRB120422A 重复 λ 文件），
数值全部内联；文件缺失时 skip（脱库/无数据机器不误报）。一律不用 db_cur 夹具。
"""
import json
import math
import os

import pytest

import wavconvert
from specphot import reader
from specphot.reader import SpecLoadError, load_catalog_spectrum, load_upload_text, spec_hash

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPECTRA_DIR = os.path.join(_REPO_ROOT, 'catadata', 'spectra')


def codes(ls):
    return [w['code'] for w in ls['warnings']]


def _cat_payload(rows, sp_keys=None, meta_rec=None, objects=None):
    sp = {'u_wavelengths': 'Angstrom', 'data': rows}
    sp.update(sp_keys or {})
    data = objects if objects is not None else {'OBJ': {'spectra': sp}}
    meta = {'id': 7, 'transient_id': 'T1', 'wavelength_type': None}
    if meta_rec is not None:
        meta.update(meta_rec)
    return {'meta': meta, 'data': data}


def _up_text(rows, extra_headers=None):
    lines = ['# wavelength_unit: angstrom']
    lines += (extra_headers or [])
    lines += [' '.join(str(v) for v in r) for r in rows]
    return '\n'.join(lines)


def _up_rows(n=12, start=4000.0, step=10.0, flux=1e-15):
    return [[start + i * step, flux * (1 + i * 0.01)] for i in range(n)]


# ═══ V-1 / E-01 ═══

def test_v1_missing_payload_is_e01_404():
    for bad in (None, {}, {'meta': {}}, {'data': {}}, {'data': {'A': {}}}):
        with pytest.raises(SpecLoadError) as ei:
            load_catalog_spectrum(bad)
        assert ei.value.code == 'E-01' and ei.value.status == 404


# ═══ V-2：多对象只处理第一个 ═══

def test_v2_multi_object_takes_first_with_ca10():
    sp1 = {'u_wavelengths': 'Angstrom', 'data': [[4000.0, 1e-15], [4010.0, 2e-15]]}
    sp2 = {'u_wavelengths': 'Angstrom', 'data': [[5000.0, 9e-15]]}
    payload = _cat_payload(None, objects={'FIRST': {'spectra': sp1}, 'SECOND': {'spectra': sp2}})
    ls = load_catalog_spectrum(payload)
    assert ls['lam_aa'][0] == 4000.0 and len(ls['lam_aa']) == 2
    assert 'CA-10' in codes(ls)


# ═══ V-3 / V-3a：字符串行规范化与非数值行丢弃 ═══

def test_v3a_string_rows_and_scientific_notation():
    rows = [['2.65E+03', ' 1.5e-15 '], ['2660', '2.5E-15'], ['2670.0', 3.5e-15]]
    ls = load_catalog_spectrum(_cat_payload(rows))
    assert ls['lam_aa'] == [2650.0, 2660.0, 2670.0]
    assert ls['flux_flambda_cgs'][0] == 1.5e-15
    assert ls['read_stats']['n_dropped'] == 0


def test_v3_non_numeric_rows_dropped_and_counted():
    rows = [[4000.0, 1e-15], ['abc', 1e-15], [4010.0, None], [4020.0, 2e-15]]
    ls = load_catalog_spectrum(_cat_payload(rows))
    assert ls['lam_aa'] == [4000.0, 4020.0]
    assert ls['read_stats']['n_dropped'] == 2
    assert 'CA-10' in codes(ls)


# ═══ V-4：真乱序拒绝，不代排序 ═══

def test_v4_descending_rejected_e02():
    rows = [[4000.0, 1e-15], [4010.0, 1e-15], [4005.0, 1e-15]]
    with pytest.raises(SpecLoadError) as ei:
        load_catalog_spectrum(_cat_payload(rows))
    assert ei.value.code == 'E-02'
    with pytest.raises(SpecLoadError) as ei2:
        load_upload_text(_up_text(_up_rows(6) + [[3990.0, 1e-15]] + _up_rows(6, 4100.0)))
    assert ei2.value.code == 'E-02'


# ═══ V-5：点数闸 ═══

def test_v5_too_many_points_e07():
    rows = [[1000.0 + i, 1e-15] for i in range(20001)]
    with pytest.raises(SpecLoadError) as ei:
        load_catalog_spectrum(_cat_payload(rows))
    assert ei.value.code == 'E-07' and ei.value.reason == 'too_many_points'


# ═══ V-6：非正像素计数不整谱拒绝 ═══

def test_v6_nonpos_counted_not_rejected():
    rows = _up_rows(12)
    rows[3][1] = -1e-15
    rows[7][1] = 0.0
    ls = load_upload_text(_up_text(rows))
    st = ls['read_stats']
    assert st['n_nonpos'] == 2
    assert st['nonpos_frac'] == pytest.approx(2 / 12)
    assert 'CA-10' in codes(ls)
    assert len(ls['lam_aa']) == 12          # 不剔除（剔除发生在通带积分时）


# ═══ V-8：has_err 由行宽逐行判定 ═══

def test_v8_two_column_has_no_err():
    ls = load_upload_text(_up_text(_up_rows(12)))
    st = ls['read_stats']
    assert st['has_err'] is False and st['sigma_method'] == 'proxy'
    assert ls['flux_err'] is None


def test_v8_three_column_has_err():
    rows = [r + [1e-16] for r in _up_rows(12)]
    ls = load_upload_text(_up_text(rows))
    st = ls['read_stats']
    assert st['has_err'] is True and st['sigma_method'] == 'column'
    assert ls['flux_err'] == [1e-16] * 12


def test_v8_mixed_row_width_catalog():
    rows = [[4000.0, 1e-15, 1e-16], [4010.0, 2e-15], [4020.0, 3e-15, 3e-16]]
    ls = load_catalog_spectrum(_cat_payload(rows))
    st = ls['read_stats']
    assert st['has_err'] is False and st['sigma_method'] == 'mixed'
    assert st['n_err_missing'] == 1
    assert ls['flux_err'] == [1e-16, None, 3e-16]


# ═══ V-9：重复 λ 聚合 ═══

def test_v9_duplicate_lambda_aggregated_equal_weight():
    rows = _up_rows(12)
    rows.insert(5, list(rows[5]))                 # 复制一行 ⇒ 相邻相等
    rows[5][1] = rows[6][1] * 3                   # 组内流量不同
    ls = load_upload_text(_up_text(rows))
    st = ls['read_stats']
    assert len(ls['lam_aa']) == 12
    assert st['n_dup_lam'] == 1
    assert ls['flux_flambda_cgs'][5] == pytest.approx(rows[6][1] * 2)   # 等权均值
    assert st['dup_lam_rel_spread'] == pytest.approx(2 / 3)             # (3f−f)/max|3f|
    assert 'CA-10' in codes(ls)


def test_v9_inverse_variance_weighting_with_sigma():
    rows = [[4000.0, 1e-15, 1e-16], [4000.0, 3e-15, 1e-16]] \
        + [r + [1e-16] for r in _up_rows(10, 4010.0)]          # 全件统一 3 列（V-18）
    ls = load_upload_text(_up_text(rows))
    assert ls['flux_flambda_cgs'][0] == pytest.approx(2e-15)
    assert ls['flux_err'][0] == pytest.approx(1e-16 / math.sqrt(2), rel=1e-12)


def test_v9_non_monotonic_input_never_reaches_interp():
    """V-4 先于 V-9：乱序+重复的组合也必须先按 E-02 拒"""
    rows = [[4000.0, 1e-15], [3990.0, 1e-15], [3990.0, 2e-15]] + _up_rows(10, 4010.0)
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text(_up_text(rows))
    assert ei.value.code == 'E-02'


# ═══ V-10：间隙 ═══

def test_v10_gap_recorded():
    rows = [[4000.0 + i, 1e-15] for i in range(10)] + [[4100.0, 1e-15], [4101.0, 1e-15]]
    ls = load_upload_text(_up_text(rows))
    st = ls['read_stats']
    assert st['n_gaps'] == 1
    assert st['gaps'] == [[4009.0, 4100.0]]


# ═══ V-11：非有限值 ═══

def test_v11_nonfinite_filtered():
    rows = [[4000.0, 1e-15], [4010.0, float('nan')], [4020.0, float('inf')], [4030.0, 2e-15]]
    ls = load_catalog_spectrum(_cat_payload(rows))
    assert ls['lam_aa'] == [4000.0, 4030.0]
    assert ls['read_stats']['n_nonfinite'] == 2
    assert 'CA-10' in codes(ls)


# ═══ V-14：波长单位键 ═══

@pytest.mark.parametrize('unit', ['nm', 'µm', 'pixel'])
def test_v14_non_angstrom_unit_rejected(unit):
    with pytest.raises(SpecLoadError) as ei:
        load_catalog_spectrum(_cat_payload([[4000.0, 1e-15]], sp_keys={'u_wavelengths': unit}))
    assert ei.value.code == 'E-14' and ei.value.reason == 'bad_wavelength_unit'


def test_v14_missing_unit_key_rejected():
    sp = {'data': [[4000.0, 1e-15]]}
    payload = {'meta': {'id': 7, 'wavelength_type': 'vacuum'}, 'data': {'O': {'spectra': sp}}}
    with pytest.raises(SpecLoadError) as ei:
        load_catalog_spectrum(payload)
    assert ei.value.code == 'E-14'


# ═══ V-15：time / redshift ═══

def test_v15_time_string_mjd_and_precision():
    ls = load_catalog_spectrum(_cat_payload([[4000.0, 1e-15]], sp_keys={'time': '52812.0'}))
    assert ls['meta']['mjd'] == 52812.0
    # F-61：时刻精度两档制 —— 含小数 ⇒ 秒级 1e-5（不再折算 10^-n）
    assert ls['meta']['time_precision_d'] == pytest.approx(1e-5)
    assert ls['meta']['m_obs_kind'] == 'timed'


def test_v15_unparseable_time_downgrades_not_rejects():
    ls = load_catalog_spectrum(_cat_payload([[4000.0, 1e-15]], sp_keys={'time': 'not-a-mjd'}))
    assert ls['meta']['mjd'] is None
    assert ls['meta']['m_obs_kind'] == 'no_time'
    assert ls['meta_provenance']['mjd'] == 'default'


def test_v15_integer_time_precision_is_half_day():
    ls = load_catalog_spectrum(_cat_payload([[4000.0, 1e-15]], sp_keys={'time': '60694'}))
    # F-61：整数 MJD ⇒ 天级 0.5（不是 1.0，更不是 10^-n 折算）
    assert ls['meta']['time_precision_d'] == 0.5


# ═══ V-16：gext_corr 二级谱 ═══

def test_v16_gext_corrected_flagged_ca30():
    sp_keys = {'gext_corr': True, 'gext_ebv': '0.024704482406377792',
               'gext_rv': '3.1', 'parent_filename': 'parent.dat'}
    ls = load_catalog_spectrum(_cat_payload([[4000.0, 1e-15]], sp_keys=sp_keys))
    assert ls['meta']['already_gext_corrected'] is True
    assert ls['meta']['mw_already_corrected'] is True
    assert ls['meta']['mw_corrected_ebv'] == pytest.approx(0.024704482406377792)
    assert ls['meta']['parent_filename'] == 'parent.dat'
    assert ls['meta']['law_used'] == 'pei1992'
    assert 'CA-30' in codes(ls)


def test_v16_normal_spectrum_not_flagged():
    ls = load_catalog_spectrum(_cat_payload([[4000.0, 1e-15]]))
    assert ls['meta']['already_gext_corrected'] is False
    assert 'CA-30' not in codes(ls)


# ═══ V-17：流量量级带 ═══

def test_v17_high_cluster_flagged_ca03():
    rows = [[4000.0 + i, 0.01] for i in range(5)]     # median|F| = 0.01 > 1e-13
    ls = load_catalog_spectrum(_cat_payload(rows))
    assert ls['read_stats']['flux_scale_high_cluster'] is True
    assert ls['flux_median_cgs'] == pytest.approx(0.01)
    assert 'CA-03' in codes(ls)


def test_v17_cgs_band_not_flagged():
    rows = [[4000.0 + i, 1e-15] for i in range(5)]
    ls = load_catalog_spectrum(_cat_payload(rows))
    assert ls['read_stats']['flux_scale_high_cluster'] is False
    assert 'CA-03' not in codes(ls)


# ═══ F-79① / T-53：库内谱不重复转换 ═══

def test_t53_catalog_never_calls_air_to_vacuum(monkeypatch):
    calls = []
    orig = wavconvert.air_to_vacuum
    monkeypatch.setattr(wavconvert, 'air_to_vacuum',
                        lambda x: (calls.append(x), orig(x))[1])
    rows = [[4000.0 + i * 10, 1e-15] for i in range(20)]
    ls = load_catalog_spectrum(_cat_payload(rows))
    assert calls == []                                       # 0 次调用
    assert ls['lambda_frame_converted'] == 'host'
    assert ls['meta']['lambda_frame'] == 'vacuum'
    assert ls['lam_aa'] == [r[0] for r in rows]              # 逐点恒等（< 1e-12 Å）


def test_t53_ca34_when_wavelength_type_null_and_not_when_declared():
    rows = [[4000.0, 1e-15]]
    ls_null = load_catalog_spectrum(_cat_payload(rows, meta_rec={'wavelength_type': None}))
    assert 'CA-34' in codes(ls_null)
    for declared in ('vacuum', 'air'):
        ls_dec = load_catalog_spectrum(_cat_payload(rows, meta_rec={'wavelength_type': declared}))
        assert 'CA-34' not in codes(ls_dec)


# ═══ T-45 / F-79②：上传件框架一次性变换 ═══

def test_t45_air_upload_converted_exactly_once(monkeypatch):
    calls = []
    orig = wavconvert.air_to_vacuum
    monkeypatch.setattr(wavconvert, 'air_to_vacuum',
                        lambda x: (calls.append(x), orig(x))[1])
    rows = _up_rows(12)
    ls = load_upload_text(_up_text(rows, ['# lambda_frame: air']))
    assert ls['lambda_frame_converted'] == 'air_to_vac'
    assert ls['meta']['lambda_frame'] == 'vacuum'
    assert len(calls) == 12                                  # 每个 ≥2000 Å 的点恰一次
    for got, (lam_air, _) in zip(ls['lam_aa'], rows):
        assert got == pytest.approx(orig(lam_air), abs=1e-6)  # 与 C_AIR2VAC 逐点一致
    assert ls['n_below_convert'] == 0
    assert 'CA-34' not in codes(ls)                          # 声明值，不挂假定告警


def test_t45_unknown_frame_no_transform_and_ca34():
    rows = _up_rows(12)
    ls = load_upload_text(_up_text(rows, ['# lambda_frame: unknown']))
    assert ls['lambda_frame_converted'] is None
    assert ls['meta']['lambda_frame'] == 'unknown'
    assert ls['lam_aa'] == [r[0] for r in rows]
    assert 'CA-34' in codes(ls)


def test_t45_mixed_frame_below_2000_ca34_and_n_below_convert(monkeypatch):
    calls = []
    orig = wavconvert.air_to_vacuum
    monkeypatch.setattr(wavconvert, 'air_to_vacuum',
                        lambda x: (calls.append(x), orig(x))[1])
    rows = [[1500.0 + i * 10, 1e-15] for i in range(6)] + _up_rows(6, 4000.0)
    ls = load_upload_text(_up_text(rows, ['# lambda_frame: air']))
    assert ls['n_below_convert'] == 6
    assert len(calls) == 6                                   # 只对 ≥2000 Å 的点调用
    assert 'CA-34' in codes(ls)
    assert ls['lam_aa'][:6] == [r[0] for r in rows[:6]]      # 低段原样


def test_t45_header_frame_overrides_user_choice():
    rows = _up_rows(12)
    ls = load_upload_text(_up_text(rows, ['# lambda_frame: vacuum']), lambda_frame='air')
    assert ls['lambda_frame_converted'] is None              # 头键 vacuum 胜过用户 air
    assert ls['meta_provenance']['lambda_frame'] == 'file'
    ls2 = load_upload_text(_up_text(rows), lambda_frame='air')
    assert ls2['lambda_frame_converted'] == 'air_to_vac'
    assert ls2['meta_provenance']['lambda_frame'] == 'user'


# ═══ V-19 / V-20 / F-77：上传头键与缺省矩阵 ═══

def test_v19_missing_wavelength_unit_rejected():
    text = '\n'.join(' '.join(map(str, r)) for r in _up_rows(12))
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text(text)
    assert ei.value.code == 'E-14' and ei.value.reason == 'bad_wavelength_unit'


def test_v19_nm_unit_rejected():
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text(_up_text(_up_rows(12), ['# wavelength_unit: nm']))
    assert ei.value.code == 'E-14'


def test_v19_flux_unit_default_and_assumed_flag():
    ls = load_upload_text(_up_text(_up_rows(12)))
    assert ls['meta']['u_fluxes'] == 'erg/s/cm^2/Angstrom'
    assert ls['meta']['flux_unit_assumed'] is True
    ls2 = load_upload_text(_up_text(_up_rows(12), ['# flux_unit: erg/s/cm^2/Angstrom']))
    assert ls2['meta']['flux_unit_assumed'] is False


def test_v19_bad_redshift_header_ignored_with_ca10():
    ls = load_upload_text(_up_text(_up_rows(12), ['# redshift: abc']))
    assert ls['meta']['z'] == 0.0                            # F-77：缺 z 取 0，不拒
    assert ls['meta_provenance']['z'] == 'default'
    assert 'CA-10' in codes(ls)


def test_f77_defaults_matrix_upload():
    ls = load_upload_text(_up_text(_up_rows(12)))
    assert ls['meta']['z'] == 0.0 and ls['meta_provenance']['z'] == 'default'
    assert ls['meta']['category'] == 'other'
    assert ls['meta']['mjd'] is None and ls['meta']['m_obs_kind'] == 'no_time'
    assert ls['meta']['ra_deg'] is None and 'CA-23' in codes(ls)
    assert ls['meta']['lambda_frame'] == 'vacuum'
    assert ls['meta_provenance']['lambda_frame'] == 'default'
    assert 'CA-34' in codes(ls)


def test_v20_wavelength_out_of_domain_rejected():
    rows = _up_rows(12)
    rows[0][0] = 50.0                                        # < WL_MIN = 100
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text(_up_text(rows))
    assert ei.value.code == 'E-14' and ei.value.reason == 'wavelength_out_of_range'


# ═══ T-41（子集）：上传闸门 ═══

def test_t41_too_few_points():
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text(_up_text(_up_rows(9)))
    assert ei.value.code == 'E-14' and ei.value.reason == 'too_few_points'


def test_t41_ragged_columns_first_bad_line():
    lines = ['# wavelength_unit: angstrom'] + ['4000 1e-15'] * 5 + ['4050 1e-15 1e-16'] \
        + ['4060 1e-15'] * 6
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text('\n'.join(lines))
    assert ei.value.code == 'E-14' and ei.value.reason == 'ragged_columns'
    assert ei.value.details['first_bad_line'] == 7           # 第 7 行（含 1 行头键）


def test_t41_byte_gate():
    big = '# wavelength_unit: angstrom\n' + '\n'.join(
        f'{4000.0 + i} {1e-15}' for i in range(200000))      # ≈3 MB > 2 MB 内容闸
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text(big)
    assert ei.value.code == 'E-07' and ei.value.reason == 'body_too_big'


# ═══ T-40 / F-74：元数据优先级 ═══

def _ls_with_z(sp_keys, src):
    return load_catalog_spectrum(
        _cat_payload([[4000.0, 1e-15]], sp_keys=sp_keys), source_record=src)


def test_t40_redshift_priority_four_cases():
    src = {'redshift': 0.5, 'ra': 1.0, 'dec': 2.0}
    assert _ls_with_z({'redshift': '0.1685'}, src)['meta_provenance']['z'] == 'file'
    assert _ls_with_z({'redshift': '0.1685'}, src)['meta']['z'] == 0.1685
    assert _ls_with_z({}, src)['meta_provenance']['z'] == 'source'
    assert _ls_with_z({}, src)['meta']['z'] == 0.5
    both = _ls_with_z({'redshift': '0.1685'}, src)
    assert both['meta']['z'] == 0.1685 and both['meta_provenance']['z'] == 'file'
    none = _ls_with_z({}, {})
    assert none['meta']['z'] == 0.0 and none['meta_provenance']['z'] == 'default'


def test_t40_f77_matrix_each_key():
    ls = _ls_with_z({}, {})                                  # 源记录也全缺
    assert ls['meta']['category'] == 'other' and ls['meta_provenance']['category'] == 'default'
    assert ls['meta']['mjd'] is None and ls['meta']['m_obs_kind'] == 'no_time'
    assert ls['meta']['ra_deg'] is None and 'CA-23' in codes(ls)


def test_m3_flux_type_reads_file_side_never_extra_data():
    """T-54 的读侧半段：文件缺 flux_type 时，extra_data 里的 'absolute' 不得生效"""
    payload = _cat_payload([[4000.0, 1e-15]],
                           meta_rec={'extra_data': {'flux_type': 'absolute'}})
    ls = load_catalog_spectrum(payload)
    assert ls['meta']['flux_type'] is None
    ls2 = load_catalog_spectrum(
        _cat_payload([[4000.0, 1e-15]], sp_keys={'flux_type': 'absolute'}))
    assert ls2['meta']['flux_type'] == 'absolute'


# ═══ T-43：spec_hash ═══

def test_t43_hash_order_invariant_and_value_sensitive():
    lam = [4000.0, 4010.0, 4020.0]
    flx = [1e-15, 2e-15, 3e-15]
    h1 = spec_hash(lam, flx)
    h2 = spec_hash([4010.0, 4000.0, 4020.0], [2e-15, 1e-15, 3e-15])   # 同内容乱序
    assert h1 == h2
    h3 = spec_hash(lam, [1e-15, 2e-15, 3.0000001e-15])
    assert h1 != h3
    assert len(h1) == 12 and all(c in '0123456789abcdef' for c in h1)


def test_f73_loaded_spectrum_contract_keys():
    ls = load_upload_text(_up_text(_up_rows(12)))
    for k in ('source', 'spectrum_id', 'spec_hash', 'tid', 'lam_aa',
              'flux_flambda_cgs', 'flux_err', 'flux_median_cgs', 'meta',
              'meta_provenance', 'read_stats', 'warnings'):
        assert k in ls, k
    assert ls['source'] == 'upload' and ls['spectrum_id'] is None and ls['tid'] is None
    for k in ('ra_deg', 'dec_deg', 'z', 'category', 'mjd', 'time_precision_d',
              'lambda_frame', 'already_gext_corrected', 'law_used'):
        assert k in ls['meta'], k


# ═══ T-44：与宿主上传语法一致性（私函数级，语法层） ═══

def test_t44_syntax_consistency_with_host():
    from routes.spectra import _parse_text_spectrum, _validate_points, UploadError
    cases = [
        # 头键 : 与 = 两种写法 + 注释行 + 空行 + 逗号/空白分隔 + 两列
        '# name: test spec\n# redshift = 0.5\n\n# 普通注释\n'
        + '\n'.join(f'{4000 + i * 10}, {1e-15 + i * 1e-17}' for i in range(12)),
        # 三列 + 混合分隔
        '# wavelength_unit: angstrom\n'
        + '\n'.join(f'{4000 + i * 10}\t{1e-15 + i * 1e-17} 1e-16' for i in range(12)),
        # 重复 λ：两侧都接受，唯一 λ 集合相同（宿主覆盖 / 本模块聚合，不比逐点流量）
        '# wavelength_unit: angstrom\n'
        + '\n'.join(['4000 1e-15', '4000 2e-15'] + [f'{4010 + i * 10} 1e-15' for i in range(10)]),
    ]
    for text in cases:
        h_meta, h_points = _parse_text_spectrum(text)
        headers, rows, _ = reader.parse_text_spectrum(text)
        assert headers == h_meta                                # # 头键收集一致
        assert len(rows) == len(h_points)                       # 行分隔规则一致
        h_valid = _validate_points(h_points)
        ours_lam = sorted({r[0] for _, r, _ in rows})
        assert ours_lam == [p[0] for p in h_valid]              # 唯一 λ 集合相同
    # 拒绝结论一致：点数不足 / λ 越界 / 数值无法解析（首 token 数值、次列垃圾）
    for bad in ('# a: 1\n' + '\n'.join(f'{4000 + i} 1e-15' for i in range(9)),
                '# a: 1\n' + '\n'.join(f'{4000 + i} 1e-15' for i in range(10)) + '\n50 1e-15',
                '# a: 1\n' + '\n'.join(f'{4000 + i} 1e-15' for i in range(10)) + '\n4050 abc'):
        with pytest.raises(UploadError):
            _validate_points(_parse_text_spectrum(bad)[1])
        with pytest.raises(SpecLoadError):
            load_upload_text('# wavelength_unit: angstrom\n' + bad.split('\n', 1)[1])


def test_f75_header_separator_lines_skipped_counted():
    """F-75：表头分隔行（wavelength flux flux_err 之类）跳过并计数。

    这是与宿主有意的语法层差异之一（宿主对首 token 非数值的行直接报错）；
    T-44 的一致性语料回避这类行。"""
    text = '# wavelength_unit: angstrom\nwavelength flux flux_err\n' \
        + '\n'.join(f'{4000 + i * 10} {1e-15 + i * 1e-17}' for i in range(12))
    ls = load_upload_text(text)
    assert ls['read_stats']['n_skipped_header'] == 1
    assert ls['read_stats']['n_points'] == 12


# ═══ golden：库内真实谱文件（数值内联，文件缺失则 skip） ═══

def _load_real(relpath):
    fp = os.path.join(SPECTRA_DIR, relpath)
    if not os.path.exists(fp):
        pytest.skip(f'库内谱文件不在本机: {relpath}')
    with open(fp, encoding='utf-8') as f:
        data = json.load(f)
    # 模拟宿主 GET（_coerce_spec_data + to_vacuum_points，wavelength_type NULL）
    for obj in data.values():
        sp = obj.get('spectra')
        if not sp or 'data' not in sp:
            continue
        coerced = []
        for d in sp['data']:
            try:
                row = [float(d[0]), float(d[1])]
                if len(d) > 2 and d[2] not in (None, ''):
                    row.append(float(d[2]))
                coerced.append(row)
            except (TypeError, ValueError, IndexError):
                continue
        sp['data'], _ = wavconvert.to_vacuum_points(coerced, None, sp.get('u_wavelengths'))
    return data


def test_golden_real_string_file_89_rows():
    """GRB030329A/SN2003dh_Jun22.dat.json：89 行全字符串、无 σ 列、高簇流量"""
    data = _load_real('GRB030329A/SN2003dh_Jun22.dat.json')
    payload = {'meta': {'id': 1, 'transient_id': 'GRB030329A', 'wavelength_type': None},
               'data': data}
    ls = load_catalog_spectrum(payload, source_record={'redshift': 0.1685, 'ra': 1.0, 'dec': 2.0})
    st = ls['read_stats']
    assert st['n_points'] == 89
    assert st['n_dropped'] == 0                               # V-3a：字符串行全部救回
    assert st['has_err'] is False
    assert ls['lam_aa'][0] == pytest.approx(4051.144090867836, abs=1e-9)
    assert ls['lam_aa'][-1] == pytest.approx(7946.185260641547, abs=1e-9)
    assert ls['lam_aa'] == [r[0] for r in data['SN2003dh']['spectra']['data']]
    assert ls['flux_median_cgs'] == pytest.approx(0.013)      # 高簇
    assert st['flux_scale_high_cluster'] is True
    assert ls['meta']['z'] == 0.1685 and ls['meta_provenance']['z'] == 'file'
    assert ls['meta']['mjd'] == 52812.0
    assert ls['meta']['time_precision_d'] == pytest.approx(1e-5)   # F-61 秒级
    assert ls['meta']['flux_type'] is None                    # 文件侧无此键
    assert ls['meta']['u_fluxes'] == 'Uncalibrated'
    assert 'CA-34' in codes(ls) and 'CA-03' in codes(ls)


def test_golden_real_gext_spectrum():
    """SN2003dh_Apr04.dat_gextcor.json：全库唯一 gext_corr 二级谱"""
    data = _load_real('GRB030329A/SN2003dh_Apr04.dat_gextcor.json')
    payload = {'meta': {'id': 2, 'transient_id': 'GRB030329A', 'wavelength_type': None,
                        'gext_corr': True},
               'data': data}
    ls = load_catalog_spectrum(payload)
    assert ls['meta']['already_gext_corrected'] is True
    assert ls['meta']['mw_corrected_ebv'] == pytest.approx(0.024704482406377792)
    assert ls['meta']['parent_filename'] == 'SN2003dh_Apr04.dat'
    assert 'CA-30' in codes(ls)


def test_golden_real_duplicate_lambda_file():
    """GRB120422A/SN2012bz_S1_3May_Modified.txt.json：2044 行 → 696 唯一 λ（n_dup=1348）"""
    data = _load_real('GRB120422A/SN2012bz_S1_3May_Modified.txt.json')
    payload = {'meta': {'id': 3, 'transient_id': 'GRB120422A', 'wavelength_type': None},
               'data': data}
    ls = load_catalog_spectrum(payload)
    st = ls['read_stats']
    assert st['n_points'] == 696
    assert st['n_dup_lam'] == 1348
    assert 'CA-10' in codes(ls)
    lam = ls['lam_aa']
    assert all(lam[i + 1] > lam[i] for i in range(len(lam) - 1))   # 聚合后严格升序
