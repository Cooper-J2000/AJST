"""pcigale 宿主星系拟合执行器。

run(job_id, config, log, workdir=None, filters=None)：
  1. config['photometry']（[{band, mag, mag_err, mag_sys, source}]）→ mJy 观测表
  2. 生成 pcigale.ini（固定模块模板 sfhdelayed+bc03+dustatt_modified_CF00+redshifting）
  3. subprocess 调 `pcigale run`（OMP_NUM_THREADS=1，超时 10 分钟）
  4. 解析 out/results.txt → {'best':…, 'bayes':…, 'bayes_err':…}
  5. 用 out/host_best_model.fits + 观测点画 log-log SED → sed.png

config = {'mode': 'fixed'|'photoz',
          'redshift': float|None,            # fixed 模式必填
          'grid': {'tau_main': [...], 'age_main': [...], 'Av_ISM': [...],
                   'z_min': .., 'z_max': .., 'z_step': ..},   # z* 仅 photoz 用
          'photometry': [{band, mag, mag_err, mag_sys, source}]}
"""
import math
import os
import shutil
import subprocess

import numpy as np

from app import get_session
from models import FilterDef

_LN10 = math.log(10)
_C_AA_PER_S = 2.99792458e18      # 光速 [Å/s]
_MJY_PER_CGS_FNU = 1e26          # 1 erg/s/cm²/Hz = 1e26 mJy

_PCIGALE_BIN = os.environ.get(
    'AJST_PCIGALE_BIN',
    '/home/ajst/ajst/bin/pcigale')

_TIMEOUT_S = 600  # pcigale run 超时 10 分钟

# pcigale 要求 ini 必须配套 ini.spec 且 spec 的 section/键必须与 ini 完全一致
# （configobj 校验：spec 声明而 ini 缺失的 section/键会报错）。nebular/dl2014
# 为可选模块，故 spec 不能是静态文件，需按同一模块选择动态生成。
# 下列 spec 文本由本机安装的 pcigale 2025.1 `init`+`genconf` 生成后固化，
# 若升级 pcigale 需同步重新生成。
_SPEC_HEADER = """data_file = string()
parameters_file = string()
sed_modules = cigale_string_list()
analysis_method = string()
cores = integer(min=1)
bands = cigale_string_list()
properties = cigale_string_list()
additionalerror = float(min=0.0)
[sed_modules_params]
"""
_SPEC_MODULES = {
    'sfhdelayed': """  [[sfhdelayed]]
    tau_main = cigale_list()
    age_main = cigale_list(dtype=int, minvalue=0.)
    tau_burst = cigale_list()
    age_burst = cigale_list(dtype=int, minvalue=1.)
    f_burst = cigale_list(minvalue=0., maxvalue=0.9999)
    sfr_A = cigale_list(minvalue=0.)
    normalise = boolean()
""",
    'bc03': """  [[bc03]]
    imf = cigale_list(dtype=int, options=0. & 1.)
    metallicity = cigale_list(options=0.0001 & 0.0004 & 0.004 & 0.008 & 0.02 & 0.05)
    separation_age = cigale_list(dtype=int, minvalue=0)
""",
    'nebular': """  [[nebular]]
    logU = cigale_list(options=-4.0 & -3.9 & -3.8 & -3.7 & -3.6 & -3.5 & -3.4 & -3.3 & -3.2 & -3.1 & -3.0 & -2.9 & -2.8 & -2.7 & -2.6 & -2.5 & -2.4 & -2.3 & -2.2 & -2.1 & -2.0 & -1.9 & -1.8 & -1.7 & -1.6 & -1.5 & -1.4 & -1.3 & -1.2 & -1.1 & -1.0)
    zgas = cigale_list(options=0.0001 & 0.0004 & 0.001 & 0.002 & 0.0025 & 0.003 & 0.004 & 0.005 & 0.006 & 0.007 & 0.008 & 0.009 & 0.011 & 0.012 & 0.014 & 0.016 & 0.019 & 0.020 & 0.022 & 0.025 & 0.03 & 0.033 & 0.037 & 0.041 & 0.046 & 0.051)
    ne = cigale_list(options=10 & 100 & 1000)
    f_esc = cigale_list(minvalue=0., maxvalue=1.)
    f_dust = cigale_list(minvalue=0., maxvalue=1.)
    lines_width = cigale_list(minvalue=0.)
    emission = boolean()
    line_list = string()
""",
    'dustatt_modified_CF00': """  [[dustatt_modified_CF00]]
    Av_ISM = cigale_list(minvalue=0)
    mu = cigale_list(minvalue=.0001, maxvalue=1.)
    slope_ISM = cigale_list()
    slope_BC = cigale_list()
    filters = string()
""",
    'dl2014': """  [[dl2014]]
    qpah = cigale_list(minvalue=0.47, maxvalue=7.32)
    umin = cigale_list(options=0.10 & 0.12 & 0.15 & 0.17 & 0.20 & 0.25 & 0.30 & 0.35 & 0.40 & 0.50 & 0.60 & 0.70 & 0.80 & 1.00 & 1.20 & 1.50 & 1.70 & 2.00 & 2.50 & 3.00 & 3.50 & 4.00 & 5.00 & 6.00 & 7.00 & 8.00 & 10.00 & 12.00 & 15.00 & 17.00 & 20.00 & 25.00 & 30.00 & 35.00 & 40.00 & 50.00)
    alpha = cigale_list(options=1.0 & 1.1 & 1.2 & 1.3 & 1.4 & 1.5 & 1.6 & 1.7 & 1.8 & 1.9 & 2.0 & 2.1 & 2.2 & 2.3 & 2.4 & 2.5 & 2.6 & 2.7 & 2.8 & 2.9 & 3.0)
    gamma = cigale_list(minvalue=0., maxvalue=1.)
""",
    'redshifting': """  [[redshifting]]
    redshift = cigale_list(minvalue=0.)
""",
}
_SPEC_ANALYSIS = """[analysis_params]
  variables = cigale_string_list()
  bands = cigale_string_list()
  save_best_sed = boolean()
  save_chi2 = option('all', 'none', 'properties', 'fluxes')
  lim_flag = option('full', 'noscaling', 'none')
  mock_flag = boolean()
  redshift_decimals = integer()
  blocks = integer(min=1)
"""

# results.txt 中提取的关键属性（best./bayes. 前缀下的属性名）
_KEY_PROPS = ['universe.redshift', 'stellar.m_star', 'sfh.sfr', 'sfh.age_main',
              'attenuation.Av_ISM']

# pdf_analysis 的 variables（模块模板固定，以下属性均存在）
_VARIABLES = ('attenuation.Av_BC, attenuation.Av_ISM, attenuation.mu, '
              'attenuation.slope_BC, attenuation.slope_ISM, dust.luminosity, '
              'sfh.age, sfh.age_main, sfh.integrated, sfh.sfr, sfh.sfr10Myrs, '
              'sfh.sfr100Myrs, sfh.tau_main, stellar.m_star, universe.age, '
              'universe.luminosity_distance, universe.redshift')

_SED_MODULES_BASE = ['sfhdelayed', 'bc03']

# 可选模块（前端勾选启用）：(配置键, pcigale 模块名, ini 参数段文本)
# 参数全部固定为常用默认值，不进网格，避免模型数爆炸
_OPTIONAL_MODULES = [
    ('use_nebular', 'nebular', """\
  [[nebular]]
    logU = -2.0
    zgas = 0.02
    ne = 100
    f_esc = 0.0
    f_dust = 0.0
    lines_width = 300.0
    emission = True
    line_list =
"""),
    ('use_dl2014', 'dl2014', """\
  [[dl2014]]
    qpah = 2.50
    umin = 1.0
    alpha = 2.0
    gamma = 0.1
"""),
]


# ─── 测光 → mJy ───

def _mag_to_mjy(point, filt, warnings):
    """单个测光点 → (f_mjy, ferr_mjy)；无法换算返回 None 并记录警告。

    point: {band, mag, mag_err, mag_sys, source}
    filt:  FilterDef（或含 wavelength/vega2ab/extra_data 属性的对象）
    """
    band = point.get('band')
    mag = point.get('mag')
    mag_err = point.get('mag_err')
    mag_sys = (point.get('mag_sys') or 'ab').strip().lower()

    try:
        mag = float(mag)
    except (TypeError, ValueError):
        warnings.append(f'波段 {band}: 星等非法（{mag!r}），已跳过')
        return None

    if mag_sys in ('ab', ''):
        mag_ab = mag
    elif mag_sys == 'vega':
        v2ab = getattr(filt, 'vega2ab', None) if filt is not None else None
        if not v2ab:  # None 或 0.0：视为缺转换系数
            warnings.append(f'波段 {band}: Vega 星等但缺 vega2ab 转换系数，已跳过')
            return None
        mag_ab = mag + v2ab
    elif mag_sys in ('st', 'stmag'):
        # ST: m_st = -2.5 lg(f_λ) - 21.10（f_λ 单位 erg/s/cm²/Å）
        lam = getattr(filt, 'wavelength', None) if filt is not None else None
        if not lam:
            warnings.append(f'波段 {band}: ST 星等但 filters 表无波长，已跳过')
            return None
        f_lam = 10.0 ** (-0.4 * (mag + 21.10))          # erg/s/cm²/Å
        f_nu = f_lam * lam * lam / _C_AA_PER_S          # erg/s/cm²/Hz
        f_mjy = f_nu * _MJY_PER_CGS_FNU
        if mag_err is not None and float(mag_err) > 0:
            ferr = f_mjy * _LN10 / 2.5 * float(mag_err)
        else:
            warnings.append(f'波段 {band}: 星等误差缺失，按 σ=0.2 mag 处理')
            ferr = f_mjy * _LN10 / 2.5 * 0.2
        return f_mjy, ferr
    else:
        warnings.append(f'波段 {band}: 未知星等系统 {mag_sys!r}，已跳过')
        return None

    f_mjy = 10.0 ** (-0.4 * (mag_ab - 8.90))
    if mag_err is not None and float(mag_err) > 0:
        ferr = f_mjy * _LN10 / 2.5 * float(mag_err)
    else:
        warnings.append(f'波段 {band}: 星等误差缺失，按 σ=0.2 mag 处理')
        ferr = f_mjy * _LN10 / 2.5 * 0.2
    return f_mjy, ferr


def build_observations(config, filters):
    """测光点列表 → pcigale 观测表。

    filters: {band_id: FilterDef}
    返回 (table_text, points, bands_pcg, warnings)
      points:     [{band, pcigale_name, wave_nm, f_mjy, ferr_mjy}]（已成功转换）
      bands_pcg:  pcigale 波段名列表（与表格列顺序一致）
    无可用点时抛 ValueError。
    """
    warnings = []
    points = []
    seen = set()
    for p in config.get('photometry') or []:
        band = p.get('band')
        if p.get('upperlimit'):
            warnings.append(f'波段 {band}: 上限点不参与拟合，已跳过')
            continue
        filt = filters.get(band)
        pcg = ((filt.extra_data or {}).get('pcigale_name')
               if filt is not None else None)
        if not pcg:
            warnings.append(f'波段 {band}: filters 表无 pcigale_name，已跳过')
            continue
        if pcg in seen:
            warnings.append(f'波段 {band}: pcigale 波段 {pcg} 重复，仅取第一个点')
            continue
        conv = _mag_to_mjy(p, filt, warnings)
        if conv is None:
            continue
        f_mjy, ferr_mjy = conv
        lam = getattr(filt, 'wavelength', None)
        points.append({'band': band, 'pcigale_name': pcg,
                       'wave_nm': (lam / 10.0) if lam else None,
                       'f_mjy': f_mjy, 'ferr_mjy': ferr_mjy})
        seen.add(pcg)
    if not points:
        raise ValueError('无可用测光点（全部缺 pcigale_name 或无法换算）')

    mode = config.get('mode', 'fixed')
    if mode == 'photoz':
        z_in = -1.0  # 负 redshift = 测光红移模式
    else:
        z_in = float(config['redshift'])

    header = ['# id', 'redshift']
    for pt in points:
        header += [pt['pcigale_name'], pt['pcigale_name'] + '_err']
    row = ['host', f'{z_in:.6g}']
    for pt in points:
        row += [f"{pt['f_mjy']:.6g}", f"{pt['ferr_mjy']:.6g}"]
    table_text = ' '.join(header) + '\n' + ' '.join(row) + '\n'
    return table_text, points, [pt['pcigale_name'] for pt in points], warnings


# ─── pcigale.ini 生成 ───

def _fmt_list(values):
    return ', '.join(f'{float(v):g}' for v in values)


def build_ini(config, bands_pcg):
    """生成 pcigale.ini 文本。基础模块 sfhdelayed+bc03+dustatt_modified_CF00
    +redshifting；nebular / dl2014 由 config 的 use_nebular / use_dl2014 开关启用。"""
    grid = config.get('grid') or {}
    mode = config.get('mode', 'fixed')
    if mode == 'photoz':
        z_line = 'range {} {} {}'.format(
            float(grid['z_min']), float(grid['z_max']), float(grid['z_step']))
    else:
        z_line = ''  # 留空 = 用输入表 redshift 列

    bands_fit = ', '.join(
        b for pcg in bands_pcg for b in (pcg, pcg + '_err'))
    bands_out = ', '.join(bands_pcg)

    # 模块顺序：nebular 紧跟 SSP(bc03)，dl2014 在尘埃吸收之后、redshifting 之前
    modules = list(_SED_MODULES_BASE)
    extra_sections = ''
    for key, mod_name, section in _OPTIONAL_MODULES:
        if config.get(key):
            if mod_name == 'nebular':
                modules.append('nebular')
            extra_sections += section
    modules.append('dustatt_modified_CF00')
    if config.get('use_dl2014'):
        modules.append('dl2014')
    modules.append('redshifting')
    sed_modules = ', '.join(modules)

    return f"""data_file = observations.txt
parameters_file =
sed_modules = {sed_modules}
analysis_method = pdf_analysis
cores = 1

bands = {bands_fit}
properties =
additionalerror = 0.1

[sed_modules_params]
  [[sfhdelayed]]
    tau_main = {_fmt_list(grid['tau_main'])}
    age_main = {_fmt_list(grid['age_main'])}
    tau_burst = 50.0
    age_burst = 20
    f_burst = 0.0
    sfr_A = 1.0
    normalise = True
  [[bc03]]
    imf = 0
    metallicity = 0.02
    separation_age = 10
{extra_sections}  [[dustatt_modified_CF00]]
    Av_ISM = {_fmt_list(grid['Av_ISM'])}
    mu = 0.44
    slope_ISM = -0.7
    slope_BC = -1.3
    filters = {bands_pcg[0]}
  [[redshifting]]
    redshift = {z_line}

[analysis_params]
  variables = {_VARIABLES}
  bands = {bands_out}
  save_best_sed = True
  save_chi2 = none
  lim_flag = noscaling
  mock_flag = False
  redshift_decimals = 2
  blocks = 1
"""


def build_spec(config):
    """生成与 build_ini 模块选择一致的 pcigale.ini.spec 文本。"""
    modules = list(_SED_MODULES_BASE)
    for key, mod_name, _section in _OPTIONAL_MODULES:
        if config.get(key) and mod_name == 'nebular':
            modules.append(mod_name)
    modules.append('dustatt_modified_CF00')
    if config.get('use_dl2014'):
        modules.append('dl2014')
    modules.append('redshifting')
    return (_SPEC_HEADER
            + ''.join(_SPEC_MODULES[m] for m in modules)
            + _SPEC_ANALYSIS)


# ─── 结果解析 ───

def parse_results(path):
    """解析 out/results.txt（单源 id='host'，取第一行数据）。

    返回 ({'best':…, 'bayes':…, 'bayes_err':…}, reduced_chi2)
    """
    with open(path, encoding='utf-8') as f:
        names = f.readline().split()
    if not names or names[0] != 'id':
        raise ValueError(f'results.txt 表头异常: {names[:5]}')
    data = np.loadtxt(path, skiprows=1, ndmin=2, dtype=str)
    if data.shape[0] < 1:
        raise ValueError('results.txt 无数据行')
    values = dict(zip(names[1:], data[0, 1:].astype(float)))

    params = {'best': {}, 'bayes': {}, 'bayes_err': {}}
    for prop in _KEY_PROPS:
        for prefix, key in (('best.', 'best'), ('bayes.', 'bayes')):
            col = prefix + prop
            if col in values:
                params[key][prop] = float(values[col])
        col_err = 'bayes.' + prop + '_err'
        if col_err in values:
            params['bayes_err'][prop] = float(values[col_err])
    chi2 = values.get('best.reduced_chi_square')
    return params, (float(chi2) if chi2 is not None else None)


# ─── SED 图 ───

def plot_sed(fits_path, points, out_png):
    """最佳模型 SED（fits，wavelength[nm] / Fnu[mJy]）+ 观测点 → log-log PNG。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from astropy.io import fits

    with fits.open(fits_path) as hdul:
        d = hdul[1].data
        wave, fnu = np.asarray(d['wavelength']), np.asarray(d['Fnu'])

    fig, ax = plt.subplots(figsize=(7, 5))
    m = fnu > 0
    ax.plot(wave[m], fnu[m], '-', color='0.3', lw=1.2, label='pcigale best model')
    xs = [pt['wave_nm'] for pt in points if pt['wave_nm']]
    if xs:
        ax.set_xlim(min(min(xs), wave[m].min()) * 0.5,
                    max(max(xs), wave[m].max()) * 2.0)
    for pt in points:
        if pt['wave_nm']:
            ax.errorbar(pt['wave_nm'], pt['f_mjy'], yerr=pt['ferr_mjy'],
                        fmt='o', ms=6, capsize=3, label=pt['band'])
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel('wavelength [nm]')
    ax.set_ylabel(r'$F_\nu$ [mJy]')
    ax.set_title('Host galaxy SED (pcigale)')
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)


# ─── 主入口 ───

def run(job_id, config, log, workdir=None, filters=None):
    """执行一次 pcigale 拟合。返回 {'params', 'chi2', 'warnings'}。

    workdir/filters 默认自行获取（jobs 层会显式传 workdir；filters 为 None 时查库）。
    """
    if workdir is None:
        from hostfit.jobs import job_dir
        from models import FittingResult
        sess = get_session()
        try:
            row = sess.get(FittingResult, job_id)
            transient_id = row.transient_id
        finally:
            sess.close()
        workdir = job_dir(transient_id, job_id)
    os.makedirs(workdir, exist_ok=True)

    if filters is None:
        sess = get_session()
        try:
            filters = {f.id: f for f in sess.query(FilterDef).all()}
        finally:
            sess.close()

    # 1. 观测表 + ini
    table_text, points, bands_pcg, warnings = build_observations(config, filters)
    for w in warnings:
        log('警告: ' + w)
    log(f'观测表: {len(points)} 个波段: {[pt["band"] for pt in points]}')
    with open(os.path.join(workdir, 'observations.txt'), 'w', encoding='utf-8') as f:
        f.write(table_text)
    ini_text = build_ini(config, bands_pcg)
    with open(os.path.join(workdir, 'pcigale.ini'), 'w', encoding='utf-8') as f:
        f.write(ini_text)
    # pcigale 要求 ini 必须配套 ini.spec，且 spec 的 section/键须与 ini 一致
    spec_text = build_spec(config)
    with open(os.path.join(workdir, 'pcigale.ini.spec'), 'w',
              encoding='utf-8') as f:
        f.write(spec_text)

    # 2. 跑 pcigale（OMP_NUM_THREADS=1，10 分钟超时）
    pcigale = shutil.which('pcigale') or _PCIGALE_BIN
    env = dict(os.environ, OMP_NUM_THREADS='1')
    env['PATH'] = os.path.dirname(pcigale) + os.pathsep + env.get('PATH', '')
    log(f'执行: {pcigale} run  (cwd={workdir})')
    try:
        proc = subprocess.run([pcigale, 'run'], cwd=workdir, env=env,
                              capture_output=True, text=True, timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'pcigale run 超时（{_TIMEOUT_S}s）')
    log('--- pcigale stdout ---\n' + (proc.stdout or '')[-4000:])
    if proc.returncode != 0:
        log('--- pcigale stderr ---\n' + (proc.stderr or '')[-4000:])
        raise RuntimeError(f'pcigale run 失败（exit {proc.returncode}），详见 run.log')

    # 3. 解析结果
    outdir = os.path.join(workdir, 'out')
    params, chi2 = parse_results(os.path.join(outdir, 'results.txt'))
    log(f'reduced_chi2 = {chi2}; best = {params["best"]}')

    # 4. SED 图
    best_fits = os.path.join(outdir, 'host_best_model.fits')
    if os.path.exists(best_fits):
        try:
            plot_sed(best_fits, points, os.path.join(workdir, 'sed.png'))
            log('SED 图已生成: sed.png')
        except Exception as e:
            warnings.append(f'SED 图生成失败: {e}')
            log(f'警告: SED 图生成失败: {e}')
    else:
        warnings.append('未找到 host_best_model.fits，跳过 SED 图')
        log('警告: 未找到 out/host_best_model.fits')

    return {'params': params, 'chi2': chi2, 'warnings': warnings}
