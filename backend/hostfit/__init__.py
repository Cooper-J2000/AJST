"""宿主星系 pcigale 拟合子系统。

- 任务记录复用 fitting_results 表（model_name='pcigale_host'），
  状态在 extra_data.status（pending|running|done|failed|interrupted）。
- 单 worker 线程池串行执行（pcigale 重负载，排队即可）。
- 产物文件存 backend/fitting_store/<transient_id>/hostfit_<job_id>/。
"""
