"""L1: specphot 上传 FITS/ECSV 分支（P2+，F-76；解析验收 T-81）。

覆盖条款（02b 附表 T-81 逐条 + F-76 原文）：
  T-81①  跨格式不变量：同一谱以「宿主文本网格」「.ecsv」「.fits 表格」三载体
          上传 ⇒ lam_aa/flux/flux_err 逐点一致（<1e-12；无误差列三侧同为 null）、
          n_points 三值相同、spec_hash 三串相同（F-80 规范化编码的跨格式不变量）。
          含 lambda_frame=air 的变体（air→真空换算在 S0，三侧同一批数）。
  T-81②  多扩展 / 多列 / WCS 无法唯一判定 ⇒ E-14 且错误点名待指定的扩展与列名，
          服务端不代选；用户可用 hdu/col_lambda/col_flux/col_err 指定后通过。
  T-81③  两道闸门对三格式同值生效：点数 C_MAX_SPECPOINTS、单件 C_MAX_UPLOAD_BYTES；
          请求体 32 MB（config.py:45，ST-13）不得为绕过校验而修改。
  T-81④  astropy 在运行解释器内可得（M-7）⇒ 本文件模块级 import；不可得 ⇒
          收集期 ImportError 显式失败，**不得 skip**。
  F-76   λ 单位换算一律 astropy.units（TUNIT um/nm/m → Å；禁行线 5、不自写公式）；
          波长列无 TUNIT ⇒ E-14（不按 Å 猜，V-19 同纪律）；头键映射对齐文本路径
          头键集（ra/dec/redshift/time/mjd/lambda_frame/name/instrument，含 FITS
          惯用别名 z→redshift、OBJECT→name）。
  F-79②  lambda_frame 头键在 FITS/ECSV 载体内同样生效（air ⇒ 恰好一次换真空）。

所有 FITS/ECSV 均用 astropy 现造于内存 BytesIO ⇒ 解析回收；零落盘（RO-5）。
文本路径回归在 test_l1_specphot_reader.py，不受本分支影响。
"""
import base64
import io

import astropy.units as u
import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

import wavconvert
from specphot.constants import C_MAX_SPECPOINTS, C_MAX_UPLOAD_BYTES
from specphot.reader import (SpecLoadError, load_upload_ecsv,
                             load_upload_fits, load_upload_text, spec_hash)

REL_TOL = 1e-12   # T-81① 的相对差上限（规格原文）


def _rows(n=12, start=4000.0, step=10.0, err=True):
    rows = [[start + i * step, 1e-15 * (1 + i * 0.01)] for i in range(n)]
    if err:
        rows = [r + [1e-16 * (1 + i * 0.02)] for i, r in enumerate(rows)]
    return rows


def _text_of(rows):
    lines = ['# wavelength_unit: angstrom']
    lines += [' '.join(str(v) for v in r) for r in rows]
    return '\n'.join(lines)


def _fits_of(rows, lam_unit='Angstrom', header=None, name='SPECTRUM'):
    cols = [fits.Column(name='wavelength', format='D', unit=lam_unit,
                        array=np.array([r[0] for r in rows])),
            fits.Column(name='flux', format='D',
                        array=np.array([r[1] for r in rows]))]
    if len(rows[0]) > 2:
        cols.append(fits.Column(name='flux_err', format='D',
                                array=np.array([r[2] for r in rows])))
    hdu = fits.BinTableHDU.from_columns(cols, name=name)
    for k, v in (header or {}).items():
        hdu.header[k] = v
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(buf)
    return buf.getvalue()


def _ecsv_of(rows, lam_unit='Angstrom', meta=None):
    tab = {'wavelength': np.array([r[0] for r in rows]) * u.Unit(lam_unit),
           'flux': np.array([r[1] for r in rows])}
    if len(rows[0]) > 2:
        tab['flux_err'] = np.array([r[2] for r in rows])
    t = Table(tab)
    t.meta.update(meta or {})
    buf = io.StringIO()
    t.write(buf, format='ascii.ecsv')
    return buf.getvalue()


def _close(a, b):
    return all(abs(x - y) <= REL_TOL * abs(y) for x, y in zip(a, b))


# ═══ T-81①：跨格式不变量（F-80 规范化编码） ═══════════════════════════

def test_t81_1_cross_format_invariant_three_columns():
    rows = _rows()
    lsf = load_upload_fits(_fits_of(rows))
    lse = load_upload_ecsv(_ecsv_of(rows))
    lst = load_upload_text(_text_of(rows))
    assert lsf['lam_aa'] == lst['lam_aa'] == lse['lam_aa']            # 精确相等
    assert lsf['flux_flambda_cgs'] == lst['flux_flambda_cgs'] == lse['flux_flambda_cgs']
    assert lsf['flux_err'] == lst['flux_err'] == lse['flux_err']
    assert lsf['read_stats']['n_points'] == lse['read_stats']['n_points'] \
        == lst['read_stats']['n_points']
    # spec_hash 三串相同 —— F-80 跨格式不变量，不满足即 F-80 实现缺陷（不给放宽容差）
    assert lsf['spec_hash'] == lse['spec_hash'] == lst['spec_hash']


def test_t81_1_cross_format_invariant_two_columns_err_all_null():
    rows = _rows(err=False)
    lsf = load_upload_fits(_fits_of(rows))
    lse = load_upload_ecsv(_ecsv_of(rows))
    lst = load_upload_text(_text_of(rows))
    for ls in (lsf, lse, lst):
        assert ls['flux_err'] is None                                 # 三侧同为 null
        assert ls['read_stats']['has_err'] is False
    assert lsf['spec_hash'] == lse['spec_hash'] == lst['spec_hash']


def test_t81_1_cross_format_invariant_with_air_frame():
    """lambda_frame=air 头键三载体同生效；air→真空换算后仍逐点/hash 一致（F-79②）。"""
    hdr = {'LAMBDA_FRAME': 'air'}
    meta = {'lambda_frame': 'air'}
    lsf = load_upload_fits(_fits_of(_rows(), header=hdr))
    lse = load_upload_ecsv(_ecsv_of(_rows(), meta=meta))
    lst = load_upload_text('# lambda_frame: air\n' + _text_of(_rows()))
    assert lsf['lambda_frame_converted'] == 'air_to_vac'
    expect = [wavconvert.air_to_vacuum(r[0]) for r in _rows()]
    assert _close(lsf['lam_aa'], expect)
    assert _close(lse['lam_aa'], expect) and _close(lst['lam_aa'], expect)
    assert lsf['spec_hash'] == lse['spec_hash'] == lst['spec_hash']


def test_f76_header_key_mapping_fits_aliases():
    """FITS 头键映射对齐文本路径头键集（含 z→redshift、OBJECT→name 惯用别名）。"""
    header = {'RA': 188.7362, 'DEC': -5.1020, 'Z': 0.5, 'MJD': 59000.5,
              'INSTRUMENT': 'XSHOOTER', 'OBJECT': 'NGC-fake-01'}
    ls = load_upload_fits(_fits_of(_rows(), header=header))
    m = ls['meta']
    assert m['ra_deg'] == 188.7362 and m['dec_deg'] == -5.1020
    assert m['z'] == 0.5 and m['mjd'] == 59000.5
    assert m['instrument'] == 'XSHOOTER' and m['name'] == 'NGC-fake-01'
    assert m['m_obs_kind'] == 'timed'
    p = ls['meta_provenance']
    assert p['ra_deg'] == 'file' and p['z'] == 'file' and p['mjd'] == 'file'
    assert p['lambda_frame'] == 'default'                             # 未声明帧


def test_f76_header_key_mapping_ecsv_meta():
    meta = {'redshift': 0.5, 'ra': 12.5, 'dec': -3.25, 'lambda_frame': 'unknown'}
    ls = load_upload_ecsv(_ecsv_of(_rows(), meta=meta))
    assert ls['meta']['z'] == 0.5 and ls['meta']['ra_deg'] == 12.5
    assert ls['meta']['lambda_frame'] == 'unknown'                    # 未知帧不转换
    assert ls['lambda_frame_converted'] is None
    assert ls['meta_provenance']['lambda_frame'] == 'file'


# ═══ F-76：λ 单位换算（astropy.units，禁自写公式） ═════════════════════

@pytest.mark.parametrize('unit_str,scale', [('um', 1e-4), ('nm', 1e-1),
                                            ('cm', 1e-8), ('m', 1e-10)])
def test_f76_lambda_unit_converted_to_angstrom(unit_str, scale):
    base = _rows()
    rows = [[r[0] * scale] + r[1:] for r in base]     # 载体内列值带该单位
    expect = [u.Unit(unit_str).to(u.angstrom, r[0]) for r in rows]
    assert _close(expect, [r[0] for r in base])                       # 换算式自检
    lsf = load_upload_fits(_fits_of(rows, lam_unit=unit_str))
    assert _close(lsf['lam_aa'], expect)
    lse = load_upload_ecsv(_ecsv_of(rows, lam_unit=unit_str))
    assert _close(lse['lam_aa'], expect)


def test_f76_missing_tunit_is_e14_not_guess():
    """波长列无 TUNIT ⇒ E-14（不按 Å 猜，V-19 同纪律）。"""
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(_fits_of(_rows(), lam_unit=None))
    assert ei.value.code == 'E-14' and ei.value.reason == 'bad_wavelength_unit'


def test_f76_wavelength_range_applies_after_conversion():
    """V-20 λ 合法域在换算成 Å 之后判定（nm 的小数值不是越界）。"""
    rows = _rows()                                    # 4000–4110 Å
    ls = load_upload_ecsv(_ecsv_of(rows, lam_unit='nm'))
    assert ls['read_stats']['n_points'] == 12         # 400–411 nm 换算后合法


# ═══ T-81②：多扩展 / 多列 / WCS 无法唯一判定 ⇒ E-14 点名，不代选 ══════

def test_t81_2_multi_extension_names_candidates_and_rejects():
    hdul = fits.HDUList([fits.PrimaryHDU(),
                         fits.BinTableHDU.from_columns(
                             [fits.Column(name='wavelength', format='D', unit='Angstrom',
                                          array=np.array([r[0] for r in _rows()])),
                              fits.Column(name='flux', format='D',
                                          array=np.array([r[1] for r in _rows()]))],
                             name='OBS'),
                         fits.BinTableHDU.from_columns(
                             [fits.Column(name='wavelength', format='D', unit='Angstrom',
                                          array=np.array([r[0] for r in _rows()])),
                              fits.Column(name='flux', format='D',
                                          array=np.array([r[1] for r in _rows()]))],
                             name='MODEL')])
    buf = io.BytesIO()
    hdul.writeto(buf)
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(buf.getvalue())
    assert ei.value.code == 'E-14' and ei.value.reason == 'ambiguous_hdu'
    # 点名两个候选扩展，并给出 hdu 参数这一用户出口
    assert 'OBS' in str(ei.value) and 'MODEL' in str(ei.value) and 'hdu' in str(ei.value)


def test_t81_2_hdu_parameter_resolves_extension():
    rows = _rows()
    h_obs = fits.BinTableHDU.from_columns(
        [fits.Column(name='wavelength', format='D', unit='Angstrom',
                     array=np.array([r[0] for r in rows]) + 1000.0),
         fits.Column(name='flux', format='D', array=np.array([r[1] for r in rows]))],
        name='OBS')
    h_model = fits.BinTableHDU.from_columns(
        [fits.Column(name='wavelength', format='D', unit='Angstrom',
                     array=np.array([r[0] for r in rows])),
         fits.Column(name='flux', format='D', array=np.array([r[1] for r in rows]))],
        name='MODEL')
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), h_obs, h_model]).writeto(buf)
    ls = load_upload_fits(buf.getvalue(), hdu='MODEL')
    assert ls['lam_aa'][0] == 4000.0                                  # 指定的那个，未代选
    ls2 = load_upload_fits(buf.getvalue(), hdu=1)                     # 按序号亦可
    assert ls2['lam_aa'][0] == 5000.0
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(buf.getvalue(), hdu='NOPE')
    assert ei.value.reason == 'bad_hdu' and 'MODEL' in str(ei.value)


def test_t81_2_multi_lambda_column_ambiguous_names_columns():
    rows = _rows()
    hdu = fits.BinTableHDU.from_columns(
        [fits.Column(name='wave_obs', format='D', unit='Angstrom',
                     array=np.array([r[0] for r in rows])),
         fits.Column(name='wave_model', format='D', unit='Angstrom',
                     array=np.array([r[0] for r in rows])),
         fits.Column(name='flux', format='D', array=np.array([r[1] for r in rows]))],
        name='T')
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(buf)
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(buf.getvalue())
    assert ei.value.reason == 'ambiguous_columns'
    assert 'wave_obs' in str(ei.value) and 'wave_model' in str(ei.value)
    assert 'col_lambda' in str(ei.value)                              # 用户出口
    # 用户显式指定后通过（不做"猜最好的那个"）
    ls = load_upload_fits(buf.getvalue(), col_lambda='wave_model')
    assert ls['lam_aa'] == [r[0] for r in rows]


def test_t81_2_multi_err_column_ambiguous_and_col_err_resolves():
    rows = _rows()
    hdu = fits.BinTableHDU.from_columns(
        [fits.Column(name='wavelength', format='D', unit='Angstrom',
                     array=np.array([r[0] for r in rows])),
         fits.Column(name='flux', format='D', array=np.array([r[1] for r in rows])),
         fits.Column(name='flux_err', format='D', array=np.array([r[2] for r in rows])),
         fits.Column(name='flux_sigma', format='D',
                     array=np.array([2 * r[2] for r in rows]))],
        name='T')
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(buf)
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(buf.getvalue())
    assert ei.value.reason == 'ambiguous_columns' and 'col_err' in str(ei.value)
    ls = load_upload_fits(buf.getvalue(), col_err='flux_sigma')
    assert ls['flux_err'] == [2 * r[2] for r in rows]


def test_t81_2_missing_flux_column_is_e14():
    rows = _rows()
    hdu = fits.BinTableHDU.from_columns(
        [fits.Column(name='wavelength', format='D', unit='Angstrom',
                     array=np.array([r[0] for r in rows])),
         fits.Column(name='snr_proxy', format='D', array=np.array([r[1] for r in rows]))],
        name='T')
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(buf)
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(buf.getvalue())
    assert ei.value.reason == 'missing_column' and 'wavelength' in str(ei.value)


def test_ecsv_column_role_detection_matches_fits():
    """ECSV 与 FITS 表格形态用同一套列识别（_resolve_columns）。"""
    rows = _rows()
    t = Table({'wave': np.array([r[0] for r in rows]) * u.AA,
               'flux': np.array([r[1] for r in rows]),
               'flux_err': np.array([r[2] for r in rows])})
    buf = io.StringIO()
    t.write(buf, format='ascii.ecsv')
    ls = load_upload_ecsv(buf.getvalue())
    assert ls['read_stats']['sigma_method'] == 'column'
    assert ls['flux_err'] == [r[2] for r in rows]


# ═══ T-81②：一维谱形态（线性 WCS） ═══════════════════════════════════

def _fits_1d(values, ctype='WAVE', cunit='um', crval=0.4, cdelt=1e-3,
             crpix=1.0, bunit='erg / (cm2 s AA)', drop=()):
    hdu = fits.ImageHDU(np.array(values))
    hdr = hdu.header
    sets = {'CTYPE1': ctype, 'CUNIT1': cunit, 'CRVAL1': crval,
            'CDELT1': cdelt, 'CRPIX1': crpix, 'BUNIT': bunit}
    for k, v in sets.items():
        if k not in drop:
            hdr[k] = v
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(buf)
    return buf.getvalue()


def test_t81_2_fits_1d_linear_wcs_um_to_angstrom():
    n = 12
    flux = [1e-15 * (1 + i * 0.01) for i in range(n)]
    ls = load_upload_fits(_fits_1d(flux))
    expect = [u.Unit('um').to(u.angstrom, 0.4 + i * 1e-3) for i in range(n)]
    assert _close(ls['lam_aa'], expect)                               # CRPIX1 像素基
    assert ls['meta']['u_fluxes'] == 'erg / (cm2 s AA)'               # BUNIT → flux_unit
    assert ls['meta']['flux_unit_assumed'] is False
    assert ls['flux_err'] is None                                     # 一维形态无误差列


def test_t81_2_fits_1d_nonlinear_wcs_is_e14():
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(_fits_1d([1e-15] * 12, ctype='WAVE-LOG'))
    assert ei.value.code == 'E-14' and ei.value.reason == 'nonlinear_wcs'


def test_t81_2_fits_1d_missing_wcs_is_e14():
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(_fits_1d([1e-15] * 12, drop=('CTYPE1', 'CUNIT1')))
    assert ei.value.code == 'E-14' and ei.value.reason == 'no_lambda_axis'
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(_fits_1d([1e-15] * 12, drop=('CUNIT1',)))
    assert ei.value.reason == 'bad_wavelength_unit'                   # 单位不可确认不猜


# ═══ T-81③：两道闸门对三格式同值生效 + ST-13 请求体闸 ════════════════

def test_t81_3_points_gate_same_value_fits():
    n = C_MAX_SPECPOINTS + 1
    lam = np.array([4000.0 + i for i in range(n)])
    hdu = fits.BinTableHDU.from_columns(
        [fits.Column(name='wavelength', format='D', unit='Angstrom', array=lam),
         fits.Column(name='flux', format='D', unit='erg / (cm2 s AA)',
                     array=np.full(n, 1e-15))], name='T')
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(buf)
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(buf.getvalue())
    assert ei.value.code == 'E-07' and ei.value.reason == 'too_many_points'


def test_t81_3_bytes_gate_same_value_fits_and_ecsv():
    big_fits = b'SIMPLE' + b'\x00' * C_MAX_UPLOAD_BYTES               # 超 1 B
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(big_fits)
    assert ei.value.code == 'E-07' and ei.value.reason == 'body_too_big'
    big_ecsv = '# %ECSV 1.0\n# ' + 'x' * C_MAX_UPLOAD_BYTES           # 闸在解析前
    with pytest.raises(SpecLoadError) as ei:
        load_upload_ecsv(big_ecsv)
    assert ei.value.code == 'E-07' and ei.value.reason == 'body_too_big'


def test_t81_3_body_limit_32mb_unchanged():
    """ST-13：请求体 32 MB 上限（config.py:45）不得为绕过校验而修改。"""
    from config import MAX_CONTENT_LENGTH
    assert MAX_CONTENT_LENGTH == 32 * 1024 * 1024


def test_f76_too_few_points_e14_fits_ecsv():
    rows = _rows(9)                                                   # < C_MIN_UPLOAD_POINTS
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(_fits_of(rows))
    assert ei.value.code == 'E-14' and ei.value.reason == 'too_few_points'
    with pytest.raises(SpecLoadError) as ei:
        load_upload_ecsv(_ecsv_of(rows))
    assert ei.value.reason == 'too_few_points'


def test_f76_bad_fits_bytes_is_e14():
    with pytest.raises(SpecLoadError) as ei:
        load_upload_fits(b'SIMPLE' + b'\xff' * 2880)
    assert ei.value.code == 'E-14' and ei.value.reason == 'bad_fits'
    with pytest.raises(SpecLoadError) as ei:
        load_upload_ecsv('# %ECSV 1.0\n@@@ not a table @@@')
    assert ei.value.reason == 'bad_ecsv'


def test_f76_spec_hash_uses_canonical_arrays():
    """spec_hash 输入是规范化数组而非原始载体（T-81① 的机理侧断言）。"""
    rows = _rows()
    assert load_upload_fits(_fits_of(rows))['spec_hash'] \
        == spec_hash([r[0] for r in rows], [r[1] for r in rows])
