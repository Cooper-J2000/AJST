"""拟合引擎包：导入即注册全部内置引擎。"""
from .base import BaseEngine, get_engine, list_engines  # noqa: F401
from . import vegas_fs  # noqa: F401  注册 vegas_fs 引擎
