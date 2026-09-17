"""宿主星系 SED 拟合子系统（pcigale / prospector 双引擎）。

- 任务记录复用 fitting_results 表（model_name='pcigale_host' | 'prospector_host'，
  即 f'{engine}_host'），状态在 extra_data.status
  （pending|running|done|failed|interrupted）。
- 单 worker 线程池串行执行（重负载，排队即可）。
- 产物文件存 backend/fitting_store/<transient_id>/hostfit_<job_id>/。
- prospector 依赖（astro-prospector/astro-sedpy/python-fsps/dynesty/emcee）
  在 runner_prospector.run() 内惰性导入，缺包不影响服务启动。
"""
