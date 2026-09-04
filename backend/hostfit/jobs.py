"""宿主星系 pcigale 拟合任务系统（模仿 fitting/jobs.py）。

- 任务记录落在 fitting_results 表：
    model_name  = 'pcigale_host'
    parameters  = {'best': {...}, 'bayes': {...}, 'bayes_err': {...}}
    chi_squared = best.reduced_chi_square
    extra_data  = {engine: 'pcigale', config, status, error, runtime_s,
                   warnings, files: {results, sed_png, best_model, log}, created_by}
    status ∈ pending | running | done | failed | interrupted
- 产物文件存 backend/fitting_store/<transient_id>/hostfit_<job_id>/。
"""
import os
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from app import get_session
from models import FittingResult
from hostfit import runner

_STORE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'fitting_store')

MODEL_NAME = 'pcigale_host'

# 单 worker 串行队列
_pool = ThreadPoolExecutor(max_workers=1)


def job_dir(transient_id, job_id):
    return os.path.join(_STORE_ROOT, str(transient_id), f'hostfit_{job_id}')


def _set_status(sess, row, status, **extra):
    """更新 extra_data（JSONB 需整体重赋值才会被跟踪）"""
    ed = dict(row.extra_data or {})
    ed['status'] = status
    ed.update(extra)
    row.extra_data = ed
    sess.commit()


def create_job(transient_id, config, created_by=None):
    """建任务（pending）并入队，返回任务 id。调用方需已完成校验。"""
    sess = get_session()
    try:
        row = FittingResult(
            transient_id=transient_id,
            model_name=MODEL_NAME,
            parameters={},
            chi_squared=None,
            extra_data={
                'engine': 'pcigale',
                'config': config,
                'status': 'pending',
                'error': None,
                'runtime_s': None,
                'warnings': [],
                'files': {},
                'created_by': created_by,
            })
        sess.add(row)
        sess.commit()
        job_id = row.id
    finally:
        sess.close()
    _pool.submit(_run_job, job_id)
    return job_id


def _run_job(job_id):
    """worker：pending → running → done/failed"""
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None or row.model_name != MODEL_NAME:
            return
        ed = row.extra_data or {}
        _set_status(sess, row, 'running')
        transient_id = row.transient_id
        workdir = job_dir(transient_id, job_id)
        os.makedirs(workdir, exist_ok=True)
        t0 = time.time()
        with open(os.path.join(workdir, 'run.log'), 'a', encoding='utf-8') as lf:
            def log(msg):
                lf.write(str(msg) + '\n')
                lf.flush()
            log(f'===== hostfit job {job_id} (transient {transient_id}) 开始 =====')
            try:
                result = runner.run(job_id, ed.get('config') or {}, log, workdir=workdir)
                files = {}
                for kind, rel in (('results', os.path.join('out', 'results.txt')),
                                  ('sed_png', 'sed.png'),
                                  ('best_model', os.path.join('out', 'host_best_model.fits')),
                                  ('log', 'run.log')):
                    if os.path.exists(os.path.join(workdir, rel)):
                        files[kind] = rel
                row.parameters = result['params']
                row.chi_squared = result['chi2']
                _set_status(sess, row, 'done',
                            runtime_s=round(time.time() - t0, 2),
                            warnings=result.get('warnings') or [],
                            files=files, error=None)
                log('===== 任务完成 =====')
            except Exception as e:
                log('\n===== 任务失败 =====\n' + traceback.format_exc())
                _set_status(sess, row, 'failed', error=str(e))
    finally:
        sess.close()


def mark_interrupted():
    """服务启动时调用：残留的 running/pending 一律标记 interrupted"""
    sess = get_session()
    try:
        n = 0
        for row in sess.query(FittingResult).filter_by(model_name=MODEL_NAME).all():
            ed = row.extra_data or {}
            if ed.get('status') in ('running', 'pending'):
                ed = dict(ed)
                ed['status'] = 'interrupted'
                ed['error'] = ed.get('error') or '服务重启，任务中断'
                row.extra_data = ed
                n += 1
        if n:
            sess.commit()
        return n
    finally:
        sess.close()


def delete_job(job_id):
    """删除任务（仅 done/failed/interrupted）。返回 (ok, message)。"""
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None or row.model_name != MODEL_NAME:
            return False, '任务不存在'
        status = (row.extra_data or {}).get('status')
        if status in ('pending', 'running'):
            return False, '任务正在排队/运行，不能删除'
        transient_id = row.transient_id
        sess.delete(row)
        sess.commit()
    finally:
        sess.close()
    d = job_dir(transient_id, job_id)
    shutil.rmtree(d, ignore_errors=True)
    parent = os.path.dirname(d)
    try:
        os.rmdir(parent)  # 源目录已空则一并清理；非空则跳过
    except OSError:
        pass
    return True, '已删除'
