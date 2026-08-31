"""修改版 VegasAfterglow 拟合程序（vendored）：自定义 MCMC 外壳 + 先验配置。

源自作者的 Astro_Script/Fit/VegasAfterglow 工作区，见 custom_mcmc.py 模块头注释。
"""
from .custom_mcmc import (CONSTRAINTS, EXTINCTION_LAWS, JET_TYPES,
                          MEDIUM_TYPES, clean_dataframe, compute_metrics,
                          load_chain_h5, make_flux_functions, plot_corner,
                          required_params, run_mcmc, save_products)  # noqa: F401
