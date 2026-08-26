"""拟合引擎基类与注册表。

每个引擎提供：
  name / label            — 标识与展示名
  config_schema()         — 返回给前端的模型选项 + 先验模板 + 采样参数默认值（JSON 可序列化）
  validate_config(config) — 校验配置合法性，返回错误信息列表（空列表 = 合法）
  run(config, data, workdir, log) -> dict
      config  : 用户提交的拟合配置
      data    : prepare_data() 的输出（红移 + 各波段 CGS 前 mJy 数据）
      workdir : 本任务的工作目录（产物文件均落在此）
      log     : 日志回调，log(msg) 追加一行到 run.log
      返回    : {params: {名: {v, err}}, chi2, dof, bic, aic, n_steps, runtime_s}
"""

_ENGINES = {}


class BaseEngine:
    name = ''
    label = ''

    def config_schema(self):
        raise NotImplementedError

    def validate_config(self, config):
        """返回错误信息列表；空列表表示配置合法。默认不校验。"""
        return []

    def run(self, config, data, workdir, log):
        raise NotImplementedError


def register(engine_cls):
    """注册引擎类（实例化后放入注册表）"""
    eng = engine_cls()
    _ENGINES[eng.name] = eng
    return engine_cls


def get_engine(name):
    return _ENGINES.get(name)


def list_engines():
    return list(_ENGINES.values())
