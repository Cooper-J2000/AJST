"""宿主星系拟合接口（pcigale / prospector 双引擎）
GET    /api/hostfit/config                — 按引擎分的默认配置/可用波段（公开）
POST   /api/hostfit/jobs                  — 提交拟合任务（需登录，config.engine 选引擎）
GET    /api/hostfit/jobs?transient_id=    — 任务列表（公开）
GET    /api/hostfit/jobs/<id>             — 任务详情（公开，含 parameters）
GET    /api/hostfit/jobs/<id>/files/<kind>— 产物（results | sed_png | best_model | corner | log）
DELETE /api/hostfit/jobs/<id>             — 删除任务（仅管理员）
"""
import os

from flask import Blueprint, jsonify, request, abort, send_file

from app import get_session, require_auth, require_admin, current_username
from models import Transient, FilterDef, FittingResult
from hostfit import jobs as hostfit_jobs

hostfit_bp = Blueprint('hostfit', __name__)

_FILE_KINDS = {
    'results':    (os.path.join('out', 'results.txt'), 'text/plain', False),
    'sed_png':    ('sed.png', 'image/png', False),
    'best_model': (os.path.join('out', 'host_best_model.fits'),
                   'application/octet-stream', True),
    'corner':     ('corner.png', 'image/png', False),
    'log':        ('run.log', 'text/plain', False),
}
# prospector 的 results 在 job_dir 根下（results.txt），pcigale 的在 out/ 下；
# job_file 统一按 extra_data.files 里登记的相对路径取，_FILE_KINDS 仅留作
# mimetype/下载方式元数据（路径以登记为准，见 job_file）。

_DEFAULTS = {
    'tau_main': [1000, 3000, 5000],
    # age_main 默认须覆盖到宇宙年龄量级：pcigale 的 χ² 归一按绝对误差加权，
    # 网格全是年轻模型时最暗波段（如 NUV）会钉死整体缩放，导致质量量级失真
    # 且 χ² 很差（2026-09-18 hostfit_32 排查）。超过宇宙年龄的模型 pcigale
    # 自动置 NaN，高红移下安全。
    'age_main': [100, 1000, 5000, 13000],
    'Av_ISM': [0.0, 0.3, 1.0],
    'z_min': 0.0, 'z_max': 2.0, 'z_step': 0.05,
}
_MODULES_BASE = 'sfhdelayed+bc03+dustatt_modified_CF00+redshifting'
# 网页端可勾选的可选模块（config 键 → pcigale 模块名）
_OPTIONAL_MODULES = {'use_nebular': 'nebular', 'use_dl2014': 'dl2014'}

# prospector 默认配置（先验: mass[M☉] LogUniform / tau[Gyr] LogUniform /
# tage[Gyr]、dust2[5500Å 光学深度]、logzsol TopHat）
_PROSPECTOR_DEFAULTS = {
    'priors': {
        'mass': [1e8, 1e12],
        'tage': [0.05, 13.0],
        'tau': [0.1, 30.0],
        'dust2': [0.0, 2.0],
        'logzsol': [-2.0, 0.19],
    },
    'sampler': 'dynesty',
    'dynesty': {'nlive': 100},
    'emcee': {'nwalkers': 32, 'niter': 3000, 'nburn': 500},
    'z_min': 0.0, 'z_max': 2.0,
}
_PROSPECTOR_OPTIONAL = {'use_nebular': 'nebular（星云发射线+连续谱）',
                        'use_duste': 'dust_emission（尘埃红外再辐射）',
                        'use_igm': 'IGM 吸收（默认开）'}
_PROSPECTOR_SAMPLERS = ('dynesty', 'emcee')
_DYNESTY_NLIVE_MAX = 500
_DYNESTY_NLIVE_MIN = 10
_EMCEE_NWALKERS_MIN = 8
_EMCEE_NWALKERS_MAX = 128
_EMCEE_NITER_MIN = 100
_EMCEE_NITER_MAX = 20000

_MAG_SYSTEMS = ('ab', 'vega', 'st', 'stmag', '', None)


def _filter_has_curve(f):
    c = ((f.extra_data or {}).get('transmission')) or {}
    return bool(c.get('wl') and c.get('tr'))


def _job_brief(row):
    ed = row.extra_data or {}
    return {
        'id': row.id,
        'transient_id': row.transient_id,
        'engine': ed.get('engine') or ('prospector' if row.model_name == 'prospector_host'
                                       else 'pcigale'),
        'status': ed.get('status'),
        'mode': (ed.get('config') or {}).get('mode'),
        'chi_squared': row.chi_squared,
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def _job_detail(row):
    ed = row.extra_data or {}
    d = _job_brief(row)
    d.update({
        'model_name': row.model_name,
        'config': ed.get('config'),
        'parameters': row.parameters or {},
        'error': ed.get('error'),
        'runtime_s': ed.get('runtime_s'),
        'warnings': ed.get('warnings') or [],
        'created_by': ed.get('created_by'),
        'files': {kind: f'/api/hostfit/jobs/{row.id}/files/{kind}'
                  for kind in (ed.get('files') or {})},
    })
    return d


@hostfit_bp.route('/config', methods=['GET'])
def get_config():
    """按引擎分的默认配置 + 可用波段列表。

    pcigale 可用波段 = 有 pcigale_name；prospector 可用波段 = 有透过率曲线。
    """
    sess = get_session()
    try:
        all_filters = sess.query(FilterDef).all()
        bands_pcg = sorted(f.id for f in all_filters
                           if (f.extra_data or {}).get('pcigale_name'))
        bands_prs = sorted(f.id for f in all_filters if _filter_has_curve(f))
    finally:
        sess.close()
    return jsonify({
        'pcigale': {'defaults': _DEFAULTS, 'modules': _MODULES_BASE,
                    'optional_modules': _OPTIONAL_MODULES,
                    'available_bands': bands_pcg},
        'prospector': {'defaults': _PROSPECTOR_DEFAULTS,
                       'optional_modules': _PROSPECTOR_OPTIONAL,
                       'available_bands': bands_prs},
    })


def _validate_photometry(config, sess, engine, errors):
    """两引擎共用的测光点校验。返回 invalid_points（非空即 400）。

    pcigale 查 pcigale_name，prospector 查透过率曲线存在性。
    """
    photometry = config.get('photometry')
    if not isinstance(photometry, list) or not photometry:
        errors.append('photometry 必须是非空数组 [{band, mag, mag_err, mag_sys, source}]')
        photometry = []

    filters = {f.id: f for f in sess.query(FilterDef).all()}
    invalid_points = []
    seen_bands = set()
    for i, p in enumerate(photometry):
        if not isinstance(p, dict):
            invalid_points.append({'index': i, 'reason': '不是对象'})
            continue
        band = p.get('band')
        filt = filters.get(band)
        if engine == 'prospector':
            usable = filt is not None and _filter_has_curve(filt)
            if not usable:
                invalid_points.append({'index': i, 'band': band,
                                       'reason': '波段无透过率曲线'
                                                 '（不支持 prospector 拟合）'})
                continue
        else:
            pcg = (filt.extra_data or {}).get('pcigale_name') if filt else None
            if not pcg:
                invalid_points.append({'index': i, 'band': band,
                                       'reason': '波段无 pcigale_name'
                                                 '（不支持 pcigale 拟合）'})
                continue
        try:
            float(p.get('mag'))
        except (TypeError, ValueError):
            invalid_points.append({'index': i, 'band': band, 'reason': 'mag 缺失/非法'})
            continue
        mag_sys = p.get('mag_sys')
        if isinstance(mag_sys, str):
            mag_sys = mag_sys.strip().lower()
        if mag_sys not in _MAG_SYSTEMS:
            invalid_points.append({'index': i, 'band': band,
                                   'reason': f'mag_sys 非法: {p.get("mag_sys")!r}'})
            continue
        if p.get('mag_err') is not None:
            try:
                float(p['mag_err'])
            except (TypeError, ValueError):
                invalid_points.append({'index': i, 'band': band, 'reason': 'mag_err 非法'})
                continue
        seen_bands.add(band)
    if len(seen_bands) < 4:
        errors.append(f'有效测光波段不足 4 个（当前 {len(seen_bands)} 个）')
    return invalid_points


def _validate_prospector(config, errors):
    """prospector 引擎专属校验（sampler/采样参数上限/priors 范围/photoz z 区间）。"""
    mode = config.get('mode', 'fixed')
    if mode == 'photoz':
        try:
            z_min, z_max = float(config['z_min']), float(config['z_max'])
            if not (z_max > z_min >= 0):
                raise ValueError
            config['z_min'], config['z_max'] = z_min, z_max
        except (KeyError, TypeError, ValueError):
            errors.append("mode='photoz' 时需合法 z_min/z_max（z_max > z_min >= 0）")

    sampler = config.get('sampler') or 'dynesty'
    if sampler not in _PROSPECTOR_SAMPLERS:
        errors.append(f"sampler 必须为 {('/'.join(_PROSPECTOR_SAMPLERS))}")
        sampler = 'dynesty'
    config['sampler'] = sampler

    if sampler == 'dynesty':
        dyn = config.get('dynesty') or {}
        try:
            nlive = int(dyn.get('nlive') or _PROSPECTOR_DEFAULTS['dynesty']['nlive'])
            if not (_DYNESTY_NLIVE_MIN <= nlive <= _DYNESTY_NLIVE_MAX):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f'dynesty.nlive 必须为 {_DYNESTY_NLIVE_MIN}~'
                          f'{_DYNESTY_NLIVE_MAX} 的整数')
            nlive = _PROSPECTOR_DEFAULTS['dynesty']['nlive']
        config['dynesty'] = {'nlive': nlive}
    else:
        em = config.get('emcee') or {}
        dft = _PROSPECTOR_DEFAULTS['emcee']
        try:
            nwalkers = int(em.get('nwalkers') or dft['nwalkers'])
            niter = int(em.get('niter') or dft['niter'])
            nburn = int(em.get('nburn') if em.get('nburn') is not None
                        else dft['nburn'])
            if not (_EMCEE_NWALKERS_MIN <= nwalkers <= _EMCEE_NWALKERS_MAX
                    and _EMCEE_NITER_MIN <= niter <= _EMCEE_NITER_MAX
                    and 0 <= nburn < niter):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f'emcee 参数非法（nwalkers {_EMCEE_NWALKERS_MIN}~'
                          f'{_EMCEE_NWALKERS_MAX}，niter {_EMCEE_NITER_MIN}~'
                          f'{_EMCEE_NITER_MAX}，0 <= nburn < niter）')
            nwalkers, niter, nburn = dft['nwalkers'], dft['niter'], dft['nburn']
        config['emcee'] = {'nwalkers': nwalkers, 'niter': niter, 'nburn': nburn}

    priors = config.get('priors') or {}
    if not isinstance(priors, dict):
        errors.append('priors 必须是对象 {参数名: [lo, hi]}')
        priors = {}
    dft_pr = _PROSPECTOR_DEFAULTS['priors']
    out_priors = {}
    for name, dft in dft_pr.items():
        pr = priors.get(name)
        if pr is None:
            out_priors[name] = list(dft)
            continue
        try:
            lo, hi = float(pr[0]), float(pr[1])
            if not (lo < hi and hi > 0):
                raise ValueError
        except (TypeError, ValueError, IndexError):
            errors.append(f'priors.{name} 必须为 [lo, hi] 且 lo < hi、hi > 0')
            out_priors[name] = list(dft)
            continue
        out_priors[name] = [lo, hi]
    config['priors'] = out_priors

    # 可选组件开关（nebular / duste / igm）；igm 缺省开
    config['use_nebular'] = bool(config.get('use_nebular'))
    config['use_duste'] = bool(config.get('use_duste'))
    config['use_igm'] = bool(config.get('use_igm', True))


def _validate(payload, sess):
    """校验提交体，返回 (config, error_response)。

    payload = {transient_id, config: {...}}；config 也可平铺（兼容）。
    config.engine ∈ pcigale | prospector（缺省 pcigale）。
    """
    transient_id = payload.get('transient_id')
    if not transient_id:
        abort(400, description='缺少 transient_id')
    if sess.get(Transient, transient_id) is None:
        abort(404, description=f'暂现源不存在: {transient_id}')

    config = payload.get('config')
    if not isinstance(config, dict):
        config = {k: v for k, v in payload.items() if k != 'transient_id'}

    errors = []
    engine = config.get('engine') or 'pcigale'
    if engine not in hostfit_jobs.ENGINES:
        return None, (jsonify({'error': f'未知拟合引擎: {engine!r}'
                                        f'（可选: {"/".join(hostfit_jobs.ENGINES)}）'}), 400)

    mode = config.get('mode', 'fixed')
    if mode not in ('fixed', 'photoz'):
        errors.append("mode 必须为 'fixed' 或 'photoz'")

    if mode == 'fixed':
        z = config.get('redshift')
        try:
            z = float(z)
            if z <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("mode='fixed' 时 redshift 必填且 > 0")

    if engine == 'pcigale':
        grid = config.get('grid') or {}
        for key in ('tau_main', 'age_main', 'Av_ISM'):
            vals = grid.get(key)
            if (not isinstance(vals, list) or not vals
                    or any(not isinstance(v, (int, float)) for v in vals)):
                errors.append(f'grid.{key} 必须是非空数值数组')
        if mode == 'photoz':
            try:
                z_min, z_max = float(grid['z_min']), float(grid['z_max'])
                z_step = float(grid['z_step'])
                if not (z_max > z_min >= 0) or z_step <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                errors.append("mode='photoz' 时 grid 需含合法 z_min/z_max/z_step"
                              '（z_max > z_min >= 0，z_step > 0）')
    else:
        _validate_prospector(config, errors)

    invalid_points = _validate_photometry(config, sess, engine, errors)

    if invalid_points:
        return None, (jsonify({'error': '存在非法测光点',
                               'invalid_points': invalid_points}), 400)
    if errors:
        return None, (jsonify({'error': 'config 非法', 'details': errors}), 400)
    config['mode'] = mode
    config['engine'] = engine
    if engine == 'pcigale':
        # 可选模块开关（nebular / dl2014），缺省关闭
        for key in _OPTIONAL_MODULES:
            config[key] = bool(config.get(key))
    return config, None


@hostfit_bp.route('/jobs', methods=['POST'])
@require_auth
def submit_job():
    payload = request.get_json(force=True, silent=True) or {}
    sess = get_session()
    try:
        config, err = _validate(payload, sess)
        if err:
            return err
        transient_id = payload['transient_id']
    finally:
        sess.close()
    job_id = hostfit_jobs.create_job(transient_id, config,
                                     created_by=current_username(),
                                     engine=config['engine'])
    return jsonify({'id': job_id, 'transient_id': transient_id,
                    'engine': config['engine'], 'status': 'pending'}), 201


@hostfit_bp.route('/jobs', methods=['GET'])
def list_jobs():
    transient_id = request.args.get('transient_id')
    sess = get_session()
    try:
        q = (sess.query(FittingResult)
             .filter(FittingResult.model_name.in_(hostfit_jobs.MODEL_NAMES)))
        if transient_id:
            q = q.filter_by(transient_id=transient_id)
        rows = q.order_by(FittingResult.id.desc()).all()
        return jsonify([_job_brief(r) for r in rows])
    finally:
        sess.close()


@hostfit_bp.route('/jobs/<int:job_id>', methods=['GET'])
def job_detail(job_id):
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None or row.model_name not in hostfit_jobs.MODEL_NAMES:
            abort(404, description='任务不存在')
        return jsonify(_job_detail(row))
    finally:
        sess.close()


@hostfit_bp.route('/jobs/<int:job_id>/files/<kind>', methods=['GET'])
def job_file(job_id, kind):
    if kind not in _FILE_KINDS:
        abort(404, description=f'未知文件类型: {kind}')
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None or row.model_name not in hostfit_jobs.MODEL_NAMES:
            abort(404, description='任务不存在')
        transient_id = row.transient_id
        files = (row.extra_data or {}).get('files') or {}
    finally:
        sess.close()
    if kind not in files:
        abort(404, description='该产物不存在（任务未完成或生成失败）')
    # 相对路径以任务登记为准（pcigale 的 results/best_model 在 out/ 下，
    # prospector 的 results.txt/corner.png 在 job_dir 根下）
    _default_rel, mimetype, as_attachment = _FILE_KINDS[kind]
    rel = files[kind]
    if os.path.isabs(rel) or '..' in rel.split('/'):
        abort(404, description='产物路径非法')
    path = os.path.join(hostfit_jobs.job_dir(transient_id, job_id), rel)
    if not os.path.exists(path):
        abort(404, description='文件已丢失')
    return send_file(path, mimetype=mimetype,
                     as_attachment=as_attachment,
                     download_name=os.path.basename(rel))


@hostfit_bp.route('/jobs/<int:job_id>', methods=['DELETE'])
@require_admin
def remove_job(job_id):
    ok, msg = hostfit_jobs.delete_job(job_id)
    if not ok:
        code = 404 if msg == '任务不存在' else 400
        return jsonify({'error': msg}), code
    return jsonify({'status': 'ok', 'message': msg})
