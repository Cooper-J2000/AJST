"""VegasAfterglow 自定义 MCMC 外壳（AJST vendored 版）
====================================================

源自作者的修改版拟合工作区 ``Astro_Script/Fit/VegasAfterglow/vegas_custom_mcmc.py``，
随 AJST 代码仓库分发（backend/fitting/vegas_unified/）。与原版差异：

- 去掉了对 ``vegas_dataloader`` 的依赖（数据由 AJST 后端 prepare_data() 从数据库
  直接供给，引擎层组装成本模块约定的 DataFrame）；
- ``run_mcmc`` 新增 ``n_workers`` 形参（显式指定线程数，优先于 VEGAS_MCMC_WORKERS
  环境变量与 80% 核数自动值），便于网页端按任务限流。

内置 ``Fitter`` 无法处理的模型组合（例如正反激波 + 独立正向激波共用环境密度 n_ism
的组合模型、带联合先验约束的双成分喷流），由本模块直接调用 ``VegasAfterglow.Model``
获取各成分流量，自行构建似然并驱动 emcee。

物理/统计约定
-------------
- 似然: chi2 = sum_i w_i * (F_model(t_i, nu_i) - F_obs_i)^2 / err_i^2，
  logL = -chi2 / 2（与 VegasAfterglow 2.0.6 内置 Fitter 一致；上限点编码为
  flux=0 / err=上限，即模型流量高出上限即受罚）。
- 先验: 各自由参数在 [lower, upper] 内均匀；Scale.log 参数在 log10 空间采样。
- xi_e 固定为 1（Radiation 构造默认值），on-axis (theta_obs=0，可在 JSON 中放开)。
- 环境介质（ISM/星风）、喷流结构（top-hat/gaussian/...）、宿主星系消光
  （Pei92 smc/lmc/mw + A_V）由 make_flux_functions 的 jet/medium/extinction
  参数选择，注册表见 JET_TYPES / MEDIUM_TYPES / EXTINCTION_LAWS。

输入数据 DataFrame 约定（由引擎层组装）
---------------------------------------
列: t_sec, nu_hz, band_label, f_nu_cgs, f_nu_err_cgs, weights, upperlimit；
须按 t_sec 升序（C++ paired 求值要求），t<=0 点必须剔除。

输出（每个 fit 目录）
---------------------
- ``chain_record.h5`` : 完整 MCMC 链（采样空间）、log_prob、数据与全部配置，可复现
- ``corner_plot.png`` : 后验角图
- ``lc_plot.png``     : 最佳拟合 + 68% 可信区间光变曲线
- ``metrics.txt``     : chi2 / dof / reduced-chi2 / BIC / AIC 与参数估计
"""

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import emcee
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import corner
import numpy as np

from VegasAfterglow import (
    GaussianJet, ISM, Magnetar, Model, Observer, ParamDef, PowerLawJet,
    PowerLawWing, Radiation, Scale, StepPowerLawJet, TophatJet,
    TwoComponentJet, Wind,
)
from VegasAfterglow.extinction import BUILTIN_LAWS
from VegasAfterglow.units import mJy

__all__ = ["clean_dataframe", "load_chain_h5", "run_mcmc", "compute_metrics",
           "save_products", "plot_corner", "plot_lightcurve",
           "plot_lightcurve_with_ratio", "make_flux_functions",
           "required_params", "CONSTRAINTS", "JET_TYPES", "MEDIUM_TYPES",
           "EXTINCTION_LAWS"]


# ==================== 模型构建（脚本与 notebook 共用的唯一实现） ====================

RESOLUTIONS = (0.1, 0.25, 10)   # (phi, theta, t) 分辨率，与内置 Fitter 默认一致
COMP_CASES = ("fs_rs", "frs_plus_fs")   # 支持成分虚线拆分的情形

_C_CGS = 2.99792458e10                  # 光速 [cm/s]
_LN10_OVER_2P5 = 0.4 * math.log(10.0)   # A_V * k(λ) -> 光学深度（与内置 Fitter 一致）

# 喷流结构注册表（与核心包 VegasAfterglow.fitting.config.JETS 一致）:
# jet 类型 -> (构造函数, 从参数字典读取的参数名, 固定 kwargs, 是否支持磁星注入)
JET_TYPES = {
    "tophat":        (TophatJet,       ("theta_c", "E_iso", "Gamma0"), {}, True),
    "gaussian":      (GaussianJet,     ("theta_c", "E_iso", "Gamma0"), {}, True),
    "powerlaw":      (PowerLawJet,     ("theta_c", "E_iso", "Gamma0", "k_e", "k_g"),
                      {}, True),
    "two_component": (TwoComponentJet, ("theta_c", "E_iso", "Gamma0", "theta_w",
                                        "E_iso_w", "Gamma0_w"), {}, True),
    "step_powerlaw": (StepPowerLawJet, ("theta_c", "E_iso", "Gamma0", "E_iso_w",
                                        "Gamma0_w", "k_e", "k_g"), {}, True),
    "powerlaw_wing": (PowerLawWing,    ("theta_c", "E_iso_w", "Gamma0_w", "k_e", "k_g"),
                      {}, False),
    "uniform":       (TophatJet,       ("E_iso", "Gamma0"),
                      {"theta_c": math.pi / 2}, True),
}

# 环境介质注册表: medium 类型 -> 从参数字典读取的参数名
#   ism  : 常数密度 ISM(n_ism [cm^-3])
#   wind : 星风 Wind(A_star)（rho = 5e11 * A_star * r^-2 g/cm^3，k_m=2 默认）
MEDIUM_TYPES = {
    "ism":  ("n_ism",),
    "wind": ("A_star",),
}

# 宿主星系消光定律（Pei 1992，核心包 VegasAfterglow.extinction 内置；
# 银河系前景消光需事先从数据中扣除）
EXTINCTION_LAWS = tuple(BUILTIN_LAWS)   # ("smc", "lmc", "mw")


def required_params(case, jet="tophat", medium="ism", extinction=None):
    """
    返回该模型组合要求的物理参数名集合（用于校验 prior JSON 的 params）。
    theta_v 为可选参数（缺省按 0，on-axis），不计入必需集合。
    "two_comp_fs" 情形的喷流结构即 TwoComponentJet，jet 参数被忽略。
    """
    if case == "two_comp_fs":
        jet = "two_component"
    if jet not in JET_TYPES:
        raise ValueError(f"未知喷流结构: {jet}（可选: {sorted(JET_TYPES)}）")
    if medium not in MEDIUM_TYPES:
        raise ValueError(f"未知环境介质: {medium}（可选: {sorted(MEDIUM_TYPES)}）")
    if extinction is not None and extinction not in EXTINCTION_LAWS:
        raise ValueError(f"未知消光定律: {extinction}（可选: {list(EXTINCTION_LAWS)}）")

    names = set(JET_TYPES[jet][1])            # 喷流结构参数
    names.update(MEDIUM_TYPES[medium])        # 介质参数
    names.update(("eps_e", "eps_B", "p"))     # 正向激波微物理
    if case in ("fs_rs", "frs_plus_fs"):
        names.add("tau")                      # 引擎持续时间（RS 壳层厚度）
        names.update(("eps_e_r", "eps_B_r", "p_r"))
    if case == "fs_inject":
        names.update(("L0", "t0", "q"))       # 磁星注入
    if case == "frs_plus_fs":                 # 独立 FS 成分（后缀 "2"），共用介质
        names.update(f"{k}2" for k in JET_TYPES[jet][1])
        names.update(("eps_e2", "eps_B2", "p2"))
    if extinction is not None:
        names.add("A_V")                      # 宿主星系 V 波段消光 [mag]
    return names

# 物理约束（可选，按 case 启用）: case -> (callable(物理参数字典)->bool, 描述)
# 内置 Fitter 的 emcee 路径先验逐参数独立、无法表达联合约束，
# 因此带约束的情形须改走自定义 MCMC 外壳（物理与先验保持不变）。
CONSTRAINTS = {
    # 正反激波对成分必须比独立 FS 成分快，保证 FS+RS 对出现得更早
    "frs_plus_fs": (lambda p: p["Gamma0"] > p["Gamma02"],
                    "Gamma0 > Gamma02"),
    # 窄芯必须比宽翼更窄且更快
    "two_comp_fs": (lambda p: (p["theta_c"] < p["theta_w"])
                    and (p["Gamma0"] > p["Gamma0_w"]),
                    "theta_c < theta_w 且 Gamma0 > Gamma0_w"),
    # 同上，按喷流结构名命中（任何 jet="two_component" 的自定义组合都适用）
    "two_component": (lambda p: (p["theta_c"] < p["theta_w"])
                      and (p["Gamma0"] > p["Gamma0_w"]),
                      "theta_c < theta_w 且 Gamma0 > Gamma0_w"),
}


def make_flux_functions(case, lumi_dist, z, jet="tophat", medium="ism",
                        extinction=None):
    """
    按模型情形构造流量函数，返回 (model_flux, component_flux_fn)：

    - ``model_flux(p, t, nu)`` -> 各 (t, nu) 点的总流量密度 [erg/cm^2/s/Hz]
    - ``component_flux_fn(p, t, nu)`` -> {成分名: 流量数组}；单成分情形为 None

    p 为物理参数字典；theta_v 缺省按 0（on-axis）处理。
    ``case`` 为成分拓扑: "fs" / "fs_rs" / "fs_inject" / "frs_plus_fs"
    （"two_comp_fs" 为兼容别名，等价于 "fs" + jet="two_component"）。
    三个物理轴由参数选择（注册表见 JET_TYPES / MEDIUM_TYPES / EXTINCTION_LAWS，
    与核心包内置 Fitter 的 jet/medium/extinction 选项一致）：

    - ``jet``        : 喷流结构，默认 "tophat"；
                       "two_comp_fs" 情形固定为 "two_component"（忽略本参数）
    - ``medium``     : 环境介质 "ism"（n_ism）或 "wind"（A_star），默认 "ism"
    - ``extinction`` : 宿主星系消光定律 None / "smc" / "lmc" / "mw"（Pei 1992），
                       启用时要求参数 A_V [mag]，按静止系波长逐频率衰减
                       （F *= exp(-A_V * 0.4*ln10 * k(λ_rest))，与内置 Fitter 一致）

    xi_e=1（Radiation 默认值）不变；frs_plus_fs 的双成分共用介质参数。
    """
    if case == "two_comp_fs":
        jet = "two_component"
    required_params(case, jet, medium, extinction)   # 校验三个轴取值合法
    ext_law = BUILTIN_LAWS[extinction] if extinction else None

    def make_observer(p):
        return Observer(lumi_dist=lumi_dist, z=z, theta_obs=p.get("theta_v", 0.0))

    def build_jet(p, name="", duration=None, magnetar=None):
        """按 jet 类型构造喷流；name="2" 时读取带后缀2的参数（frs_plus_fs 独立成分）。"""
        ctor, keys, fixed_kw, supports_magnetar = JET_TYPES[jet]
        kwargs = {k: p[f"{k}{name}"] for k in keys}
        kwargs.update(fixed_kw)
        if duration is not None:
            kwargs["duration"] = duration
        if magnetar is not None:
            if not supports_magnetar:
                raise ValueError(f"喷流结构 {jet} 不支持磁星注入")
            kwargs["magnetar"] = magnetar
        return ctor(**kwargs)

    def build_medium(p):
        """frs_plus_fs 的双成分共用同一组介质参数。"""
        if medium == "ism":
            return ISM(p["n_ism"])
        return Wind(p["A_star"])

    def extinction_factor(p, nu):
        """宿主消光衰减因子（逐频率）；未启用或 A_V=0 时为 1。"""
        if ext_law is None:
            return 1.0
        a_v = p.get("A_V", 0.0)
        if a_v == 0.0:
            return 1.0
        lam_rest = (_C_CGS / np.asarray(nu, dtype=float)) / (1.0 + z)
        return np.exp(-a_v * _LN10_OVER_2P5 * ext_law(lam_rest))

    def build_fs(p, name=""):
        """单成分正向激波；name="2" 时为 frs_plus_fs 中的独立成分。"""
        g = lambda k: p[f"{k}{name}"]
        return Model(jet=build_jet(p, name=name), medium=build_medium(p),
                     observer=make_observer(p),
                     fwd_rad=Radiation(g("eps_e"), g("eps_B"), g("p")),
                     resolutions=RESOLUTIONS)

    def build_frs(p):
        """正反激波对（tau = 引擎持续时间 [s]，决定 RS 壳层厚度）。"""
        return Model(jet=build_jet(p, duration=p["tau"]), medium=build_medium(p),
                     observer=make_observer(p),
                     fwd_rad=Radiation(p["eps_e"], p["eps_B"], p["p"]),
                     rvs_rad=Radiation(p["eps_e_r"], p["eps_B_r"], p["p_r"]),
                     resolutions=RESOLUTIONS)

    def build_model(p):
        if case in ("fs", "two_comp_fs"):
            return build_fs(p)
        if case == "fs_rs":
            return build_frs(p)
        if case == "fs_inject":
            return Model(jet=build_jet(p, magnetar=Magnetar(p["L0"], p["t0"], p["q"])),
                         medium=build_medium(p), observer=make_observer(p),
                         fwd_rad=Radiation(p["eps_e"], p["eps_B"], p["p"]),
                         resolutions=RESOLUTIONS)
        if case == "frs_plus_fs":
            return build_frs(p), build_fs(p, name="2")   # 共用介质参数，流量相加
        raise ValueError(f"未知模型情形: {case}")

    def model_flux(p, t, nu):
        m = build_model(p)
        if isinstance(m, tuple):
            total = (np.asarray(m[0].flux_density(t, nu).total)
                     + np.asarray(m[1].flux_density(t, nu).total))
        else:
            total = np.asarray(m.flux_density(t, nu).total)
        return total * extinction_factor(p, nu)

    def comp_flux(p, t, nu):
        ext = extinction_factor(p, nu)
        if case == "fs_rs":
            r = build_model(p).flux_density(t, nu)
            return {"FS": np.asarray(r.fwd.sync) * ext,
                    "RS": np.asarray(r.rvs.sync) * ext}
        if case == "frs_plus_fs":
            m1, m2 = build_model(p)
            r1 = m1.flux_density(t, nu)
            return {"FS (paired)": np.asarray(r1.fwd.sync) * ext,
                    "RS (paired)": np.asarray(r1.rvs.sync) * ext,
                    "FS (standalone)": np.asarray(m2.flux_density(t, nu).total) * ext}
        return None

    return model_flux, (comp_flux if case in COMP_CASES else None)


# ==================== 数据 ====================

def clean_dataframe(df, t_max=None):
    """
    清洗数据 DataFrame：去掉流量/误差/时间无效的点，
    可选只保留 t_sec < t_max，最后按时间升序排列（C++ paired 求值要求）。
    """
    mask = df["f_nu_cgs"].notna() & df["f_nu_err_cgs"].notna() & (df["t_sec"] > 0)
    df = df[mask]
    if t_max is not None:
        df = df[df["t_sec"] < t_max]
    return df.sort_values("t_sec").reset_index(drop=True)


def load_chain_h5(h5_path):
    """
    读取拟合链记录，自动识别两种格式：
      - 本模块 run_mcmc 保存的格式（chain/log_prob 完整链 + config_json）
      - 内置 Fitter.save 保存的 bilby 格式（posterior/* 后验样本）
    返回 (flat, flat_logp, defs, names, meta)：
    flat 为去掉 burn-in 后的采样空间样本 (nsamples, ndim)，defs 为 ParamDef 列表。
    """
    with h5py.File(h5_path, "r") as f:
        if "chain" in f:                       # 自定义外壳格式
            meta = json.loads(f.attrs["config_json"])
            chain = f["chain"][()]             # (nsteps, nwalkers, ndim)
            logp = f["log_prob"][()]
            nburn = meta["nburn"]
            flat = chain[nburn:].reshape(-1, chain.shape[-1])
            defs = [ParamDef(n, lo, hi, getattr(Scale, s)) for n, s, lo, hi in
                    zip(meta["param_names"], meta["scales"],
                        meta["lower"], meta["upper"])]
            return flat, logp[nburn:].ravel(), defs, list(meta["param_names"]), meta
        # 内置 Fitter（bilby）格式：后验样本，log 参数带 log10_ 前缀
        keys = [k.decode() if isinstance(k, bytes) else str(k)
                for k in f["search_parameter_keys"][()]]
        flat = np.column_stack([f[f"posterior/{k}"][()] for k in keys])
        flat_logp = f["posterior/log_likelihood"][()]
        try:
            meta = json.loads(f["meta_data/vegasafterglow/param_defs_json"][()])
        except Exception:
            meta = {}
    defs = [ParamDef(k[len("log10_"):] if k.startswith("log10_") else k,
                     1e-30, 1e30,    # 哑边界（log 参数要求下界>0），重画时用不到
                     Scale.log if k.startswith("log10_") else Scale.linear)
            for k in keys]
    return flat, flat_logp, defs, [d.name for d in defs], meta


# ==================== MCMC 驱动 ====================

def _sample_space_bounds(defs):
    """把 ParamDef 的物理空间边界换算到采样空间（log 参数取 log10）。"""
    lo = np.array([np.log10(d.lower) if d.scale == Scale.log else d.lower for d in defs])
    hi = np.array([np.log10(d.upper) if d.scale == Scale.log else d.upper for d in defs])
    return lo, hi


def _to_physical(theta, defs):
    """采样空间向量 -> {参数名: 物理值}。"""
    return {
        d.name: (10.0 ** v if d.scale == Scale.log else v)
        for d, v in zip(defs, theta)
    }


def run_mcmc(model_flux_fn, defs, data, outdir, nsteps=20000, nburn=6000,
             nwalkers=None, seed=42, config=None, constraint=None,
             n_workers=None):
    """
    用 emcee 跑 MCMC。

    model_flux_fn : callable(params: dict, t: ndarray, nu: ndarray) -> ndarray
                    返回各数据点处的模型流量密度 [erg/cm^2/s/Hz]；异常时抛异常即可。
    defs          : ParamDef 列表（全部自由参数）
    data          : 符合模块头约定的 DataFrame
    constraint    : 可选，callable(params: dict) -> bool，物理参数空间上的硬约束
                    （如 Gamma0 > Gamma02）；不满足时先验为 0（logp = -inf），
                    初始 walker 位置也会重采至满足约束。
    n_workers     : 可选，似然并行线程数；缺省读 VEGAS_MCMC_WORKERS 环境变量，
                    再缺省取可用核数的 80%。
    """
    os.makedirs(outdir, exist_ok=True)
    names = [d.name for d in defs]
    ndim = len(defs)
    lo, hi = _sample_space_bounds(defs)

    t_data = data["t_sec"].values
    nu_data = data["nu_hz"].values
    f_obs = data["f_nu_cgs"].values
    f_err = data["f_nu_err_cgs"].values
    weights = data["weights"].values

    def log_likelihood(theta):
        try:
            f_mod = model_flux_fn(_to_physical(theta, defs), t_data, nu_data)
            f_mod = np.asarray(f_mod, dtype=float)
            if f_mod.shape != f_obs.shape or not np.all(np.isfinite(f_mod)):
                return -np.inf
            chi2 = np.sum(weights * (f_mod - f_obs) ** 2 / f_err**2)
            return -0.5 * chi2 if np.isfinite(chi2) else -np.inf
        except Exception:
            return -np.inf

    # 线程数：显式形参优先，其次 VEGAS_MCMC_WORKERS 环境变量（批量并发跑时人工
    # 限流），否则默认取当前进程可用核数的 80%，自动留出余量。
    # 用 sched_getaffinity 而非 os.cpu_count，能正确反映 taskset/cgroup 限制。
    if n_workers is None:
        n_workers = int(os.environ.get("VEGAS_MCMC_WORKERS", 0))
    if n_workers <= 0:
        n_cores = (len(os.sched_getaffinity(0))
                   if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1))
        n_workers = max(1, int(n_cores * 0.8))
    pool = ThreadPoolExecutor(max_workers=n_workers)

    def log_prob_batch(X):
        """emcee vectorize=True 的批量接口：硬边界先验 + 约束 + 线程池并行似然。"""
        X = np.atleast_2d(X)
        logp = np.full(X.shape[0], -np.inf)
        ok = np.all((X >= lo) & (X <= hi), axis=1)
        if constraint is not None:
            for i in np.where(ok)[0]:
                if not constraint(_to_physical(X[i], defs)):
                    ok[i] = False
        idx = np.where(ok)[0]
        if len(idx):
            logp[idx] = list(pool.map(log_likelihood, [X[i] for i in idx]))
        return logp

    if nwalkers is None:
        nwalkers = max(4 * ndim, 2 * (ndim + 1))
        nwalkers += nwalkers % 2  # 取偶
    rng = np.random.default_rng(seed)
    pos0 = rng.uniform(lo, hi, size=(nwalkers, ndim))
    if constraint is not None:   # 初始位置重采至满足约束，避免 walker 卡在 -inf
        for i in range(nwalkers):
            while not constraint(_to_physical(pos0[i], defs)):
                pos0[i] = rng.uniform(lo, hi)

    sampler = emcee.EnsembleSampler(
        nwalkers, ndim, log_prob_batch, vectorize=True,
        moves=[(emcee.moves.DEMove(), 0.7), (emcee.moves.DESnookerMove(), 0.3)],
    )
    print(f"[{datetime.now():%H:%M:%S}] emcee 开始: ndim={ndim}, nwalkers={nwalkers}, "
          f"nsteps={nsteps}, nburn={nburn}")
    try:
        sampler.run_mcmc(pos0, nsteps, progress=True)
    finally:
        pool.shutdown(wait=True)

    accept = float(np.mean(sampler.acceptance_fraction))
    print(f"[{datetime.now():%H:%M:%S}] 完成, 平均接受率 = {accept:.3f}")

    # ---- 保存完整链（采样空间）+ 数据 + 配置，保证可复现 ----
    h5_path = os.path.join(outdir, "chain_record.h5")
    with h5py.File(h5_path, "w") as f:
        f.create_dataset("chain", data=sampler.get_chain())          # (nsteps, nwalkers, ndim)
        f.create_dataset("log_prob", data=sampler.get_log_prob())    # (nsteps, nwalkers)
        f.create_dataset("data/t_sec", data=t_data)
        f.create_dataset("data/nu_hz", data=nu_data)
        f.create_dataset("data/f_nu_cgs", data=f_obs)
        f.create_dataset("data/f_nu_err_cgs", data=f_err)
        f.create_dataset("data/weights", data=weights)
        f.create_dataset("data/band_label",
                         data=np.array(data["band_label"].astype(str), dtype="S"))
        f.create_dataset("data/upperlimit",
                         data=np.array(data["upperlimit"].astype(str), dtype="S"))
        meta = dict(config or {})
        meta.update(
            param_names=names,
            scales=[str(d.scale).split(".")[-1] for d in defs],
            lower=[d.lower for d in defs], upper=[d.upper for d in defs],
            ndim=ndim, nwalkers=nwalkers, nsteps=nsteps, nburn=nburn,
            seed=seed, sampler="emcee", moves="0.7*DEMove + 0.3*DESnookerMove",
            acceptance_fraction=accept, n_data=len(data),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        f.attrs["config_json"] = json.dumps(meta)
    print(f"链数据已保存: {h5_path}")

    flat = sampler.get_chain(discard=nburn, flat=True)               # (nsamples, ndim)
    flat_logp = sampler.get_log_prob(discard=nburn, flat=True)
    return sampler, flat, flat_logp, names, defs


# ==================== 后处理：指标 / 角图 / 光变 ====================

def compute_metrics(flat_logp, n_data, n_free):
    """由最大对数似然（均匀先验下 logp_max = -chi2_min/2）计算评价指标。"""
    chi2_min = -2.0 * float(np.max(flat_logp))
    dof = n_data - n_free
    return {
        "chi2_min": chi2_min,
        "n_data": n_data,
        "n_free_params": n_free,
        "dof": dof,
        "reduced_chi2": chi2_min / dof if dof > 0 else np.nan,
        "BIC": chi2_min + n_free * np.log(n_data),
        "AIC": chi2_min + 2.0 * n_free,
    }


def plot_corner(flat, defs, names, outpath):
    labels = [rf"$\log_{{10}}$ {n}" if d.scale == Scale.log else n
              for d, n in zip(defs, names)]
    ranges = []
    for i in range(flat.shape[1]):
        lo_i, hi_i = np.percentile(flat[:, i], 0.5), np.percentile(flat[:, i], 99.5)
        if hi_i <= lo_i:  # 后验退化时防止 corner 报错
            lo_i, hi_i = lo_i - 1e-6 * abs(lo_i) - 1e-12, hi_i + 1e-6 * abs(hi_i) + 1e-12
        ranges.append((lo_i, hi_i))
    fig = corner.corner(
        flat, labels=labels, show_titles=True, title_fmt=".3f",
        quantiles=[0.16, 0.5, 0.84], range=ranges,
    )
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _band_offsets(nus, offset_base=None):
    """
    计算各波段的绘图偏移量（nus 已按频率升序）。

    按 1e12 Hz / 1e15 Hz 把频率分为 射电 / 光学 / X射线 三段；段内按频率升序
    依次乘以 10^0, 10^1, 10^2, ...；每段的基准由 offset_base 给出：
        offset_base = {"radio": 1e0, "optical": 1e2, "xray": 1e3} 之类的 dict。
    None 或缺少某段键时该段自动取值：首段为 1，后续段 = 前一段最高波段再高一个量级
    （即默认行为等价于过去的全局 10^j 依次错位）。
    """
    REGIME_EDGES = (1e12, 1e15)   # Hz：射电 < 1e12 <= 光学 < 1e15 <= X射线
    offset_base = offset_base or {}
    regimes = {"radio": nus[nus < REGIME_EDGES[0]],
               "optical": nus[(nus >= REGIME_EDGES[0]) & (nus < REGIME_EDGES[1])],
               "xray": nus[nus >= REGIME_EDGES[1]]}
    offsets = {}
    next_base = 1.0
    for name in ("radio", "optical", "xray"):
        group = regimes[name]
        if len(group) == 0:
            continue
        base = offset_base.get(name, next_base)
        for k, nu in enumerate(group):      # 段内频率升序依次抬高一个量级
            offsets[nu] = base * 10.0 ** k
        next_base = base * 10.0 ** len(group)
    return np.array([offsets[nu] for nu in nus])


def _offset_label(band, offset):
    """图例标签：在波段名后标注偏移量，如 g ×10^3（偏移为 1 时不标注）。"""
    if offset == 1.0:
        return band
    exp = np.log10(offset)
    if abs(exp - round(exp)) < 1e-9:
        return rf"{band} $\times 10^{{{int(round(exp))}}}$"
    return rf"{band} $\times {offset:g}$"


def _draw_lightcurve(ax, model_flux_fn, flat, flat_logp, defs, data,
                     component_flux_fn=None, n_cred=100, seed=0, xlabel=True,
                     offset_base=None):
    """
    在给定 ax 上绘制光变图主体（错位分波段 + 可信带 + 成分虚线）。
    返回 (best_theta, nus, band_colors)，供比值子图等复用。
    """
    rng = np.random.default_rng(seed)
    t_min, t_max = data["t_sec"].min(), data["t_sec"].max()
    t_grid = np.logspace(np.log10(t_min / 3), np.log10(t_max * 3), 200)
    nus = np.sort(data["nu_hz"].unique())

    def grid_flux(theta):
        p = _to_physical(theta, defs)
        return np.array([model_flux_fn(p, t_grid, np.full_like(t_grid, nu))
                         for nu in nus])

    best_theta = flat[np.argmax(flat_logp)]
    best_curve = grid_flux(best_theta)
    idx = rng.choice(len(flat), size=min(n_cred, len(flat)), replace=False)
    sample_curves = np.array([grid_flux(flat[i]) for i in idx])
    lo_band = np.percentile(sample_curves, 16, axis=0)
    hi_band = np.percentile(sample_curves, 84, axis=0)

    # 各成分在最佳拟合参数下的光变（每个频率一组）
    comp_curves = {}
    if component_flux_fn is not None:
        p_best = _to_physical(best_theta, defs)
        labels = list(component_flux_fn(p_best, t_grid[:1], np.full(1, nus[0])).keys())
        comp_curves = {lab: np.empty((len(nus), len(t_grid))) for lab in labels}
        for j, nu in enumerate(nus):
            for lab, arr in component_flux_fn(
                    p_best, t_grid, np.full_like(t_grid, nu)).items():
                comp_curves[lab][j] = arr

    comp_styles = ["--", "-.", ":"]

    # 波段颜色：按频率升序的序号均匀归一化到 0-1，映射到 Spectral 色阶
    # （Spectral 低端=红、高端=蓝紫，即低频偏红、高频偏蓝紫）
    cmap = plt.get_cmap("Spectral")
    band_colors = [cmap(j / max(len(nus) - 1, 1)) for j in range(len(nus))]
    offsets = _band_offsets(nus, offset_base)      # 分段基准 + 段内依次 ×10
    y_floor = np.inf   # 用于定 y 轴下限（只看总流量与数据，忽略极小的成分尾巴）
    for j, nu in enumerate(nus):
        color = band_colors[j]
        sub = data[data["nu_hz"] == nu]
        label = _offset_label(str(sub["band_label"].iloc[0]), offsets[j])
        det = sub[sub["f_nu_cgs"] > 0]
        ul = sub[sub["f_nu_cgs"] == 0]
        ax.errorbar(det["t_sec"], det["f_nu_cgs"] / mJy * offsets[j],
                    yerr=det["f_nu_err_cgs"] / mJy * offsets[j],
                    fmt="o", ms=4, color=color, label=label, zorder=3)
        if len(ul):
            ax.errorbar(ul["t_sec"], ul["f_nu_err_cgs"] / mJy * offsets[j],
                        yerr=0.3 * ul["f_nu_err_cgs"] / mJy * offsets[j],
                        fmt="v", color=color, uplims=True, zorder=3)
        ax.fill_between(t_grid, lo_band[j] / mJy * offsets[j],
                        hi_band[j] / mJy * offsets[j], color=color, alpha=0.25)
        ax.plot(t_grid, best_curve[j] / mJy * offsets[j], color=color, lw=1.2)
        for k, (lab, curves) in enumerate(comp_curves.items()):
            ax.plot(t_grid, curves[j] / mJy * offsets[j], color=color, lw=0.9,
                    ls=comp_styles[k % len(comp_styles)], alpha=0.8)
        band_tot = best_curve[j] / mJy * offsets[j]
        y_floor = min(y_floor, band_tot[band_tot > 0].min(),
                      (det["f_nu_cgs"].min() / mJy * offsets[j]) if len(det) else np.inf)
    ax.set_ylim(bottom=y_floor * 0.3)   # 截掉成分曲线中无关紧要的小值尾巴
    ax.set_xscale("log")
    ax.set_yscale("log")
    if xlabel:
        ax.set_xlabel("t [s]")
    ax.set_ylabel(r"$F_\nu$ [mJy] (offset by $10^j$)")
    band_legend = ax.legend(fontsize=8, ncol=2, loc="lower left")
    ax.add_artist(band_legend)
    # 线型图例：实线=总流量，虚线=各成分
    from matplotlib.lines import Line2D
    style_handles = [Line2D([], [], color="k", lw=1.2, ls="-", label="total")]
    style_handles += [
        Line2D([], [], color="k", lw=0.9, ls=comp_styles[k % len(comp_styles)],
               label=lab)
        for k, lab in enumerate(comp_curves)
    ]
    if comp_curves:
        ax.legend(handles=style_handles, fontsize=8, loc="upper right",
                  title="best-fit components", title_fontsize=8)
    return best_theta, nus, band_colors


def plot_lightcurve(model_flux_fn, flat, flat_logp, defs, data, outpath,
                    component_flux_fn=None, n_cred=100, seed=0, offset_base=None):
    """
    按频率分组画光变（错位分波段风格）：
    实线 = 最佳拟合总流量（最高后验），阴影 = 后验 68% 可信区间，箭头 = 上限；
    component_flux_fn 给定时，用不同虚线型叠加最佳拟合的各成分
    （如正向/反向激波），成分曲线共享波段颜色、以线型区分。
    偏移量：offset_base 可分别指定 {"radio","optical","xray"} 的基准（见 _band_offsets）。

    model_flux_fn(params, t, nu)      -> 总流量数组
    component_flux_fn(params, t, nu)  -> {成分名: 流量数组}，均与拟合时同一定义
    """
    fig, ax = plt.subplots(figsize=(9, 6.5))
    _draw_lightcurve(ax, model_flux_fn, flat, flat_logp, defs, data,
                     component_flux_fn=component_flux_fn, n_cred=n_cred, seed=seed,
                     offset_base=offset_base)
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_lightcurve_with_ratio(model_flux_fn, flat, flat_logp, defs, data, outpath,
                               component_flux_fn=None, n_cred=100, seed=0,
                               offset_base=None):
    """
    双子图版本：上图 = 与 plot_lightcurve 相同的光变图；
    下图 = 共用横轴的窄条，画各波段探测点的 (数据/最佳拟合模型) 比值，
    并以 ratio=1 的横线作为“完美匹配”参考。上限点（flux=0）不参与比值。
    """
    fig, (ax, ax_r) = plt.subplots(
        2, 1, figsize=(9, 8), sharex=True,
        gridspec_kw=dict(height_ratios=[3, 1.2], hspace=0.06))
    best_theta, nus, band_colors = _draw_lightcurve(
        ax, model_flux_fn, flat, flat_logp, defs, data,
        component_flux_fn=component_flux_fn, n_cred=n_cred, seed=seed,
        xlabel=False, offset_base=offset_base)
    plt.setp(ax.get_xticklabels(), visible=False)

    # 最佳拟合模型在各数据点处的流量（paired 求值要求时间升序，数据已按时间排序）
    p_best = _to_physical(best_theta, defs)
    f_mod = np.asarray(model_flux_fn(p_best, data["t_sec"].values,
                                     data["nu_hz"].values), dtype=float)
    nu_index = {nu: j for j, nu in enumerate(nus)}
    det = (data["f_nu_cgs"].values > 0) & np.isfinite(f_mod) & (f_mod > 0)
    ratio = data["f_nu_cgs"].values[det] / f_mod[det]
    ratio_err = data["f_nu_err_cgs"].values[det] / f_mod[det]
    colors = [band_colors[nu_index[nu]] for nu in data["nu_hz"].values[det]]
    ax_r.axhline(1.0, color="k", lw=1.0, zorder=1)
    for x, y, ye, c in zip(data["t_sec"].values[det], ratio, ratio_err, colors):
        ax_r.errorbar(x, y, yerr=ye, fmt="o", ms=3.5, color=c, zorder=2)
    ax_r.set_yscale("log")
    if len(ratio):
        lo, hi = np.percentile(ratio, [1, 99])
        ax_r.set_ylim(min(0.5, lo / 2), max(2.0, hi * 2))  # 始终包含 ratio=1
    ax_r.set_ylabel("data / model")
    ax_r.set_xlabel("t [s]")
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)


def save_products(outdir, flat, flat_logp, defs, names, data, model_flux_fn,
                  header_lines=(), component_flux_fn=None, with_ratio=True,
                  offset_base=None):
    """
    生成 metrics.txt / corner_plot.png / lc_plot.png；
    with_ratio=True 时额外输出 lc_ratio_plot.png（光变图 + data/model 比值子图）。
    offset_base 可分别指定 radio/optical/xray 波段的偏移基准（见 _band_offsets）。
    """
    phys = np.column_stack([
        10.0 ** flat[:, i] if d.scale == Scale.log else flat[:, i]
        for i, d in enumerate(defs)
    ])
    best = phys[np.argmax(flat_logp)]
    med = np.percentile(phys, 50, axis=0)
    lo68 = np.percentile(phys, 16, axis=0)
    hi68 = np.percentile(phys, 84, axis=0)
    metrics = compute_metrics(flat_logp, len(data), len(defs))

    lines = list(header_lines)
    lines += [
        "",
        "======== 评价指标 ========",
        f"chi2_min     = {metrics['chi2_min']:.2f}",
        f"n_data       = {metrics['n_data']}",
        f"n_free_param = {metrics['n_free_params']}",
        f"dof          = {metrics['dof']}",
        f"reduced chi2 = {metrics['reduced_chi2']:.3f}",
        f"BIC          = {metrics['BIC']:.2f}",
        f"AIC          = {metrics['AIC']:.2f}",
        "",
        "======== 参数估计 (median +1σ/-1σ, 以及最佳拟合) ========",
    ]
    for n, b, m, lo, hi in zip(names, best, med, lo68, hi68):
        lines.append(f"{n:>12s} = {m:.4g} (+{hi-m:.3g} / -{m-lo:.3g})   best = {b:.4g}")
    txt = "\n".join(lines) + "\n"
    with open(os.path.join(outdir, "metrics.txt"), "w") as f:
        f.write(txt)
    print(txt)

    plot_corner(flat, defs, names, os.path.join(outdir, "corner_plot.png"))
    plot_lightcurve(model_flux_fn, flat, flat_logp, defs, data,
                    os.path.join(outdir, "lc_plot.png"),
                    component_flux_fn=component_flux_fn, offset_base=offset_base)
    if with_ratio:
        plot_lightcurve_with_ratio(
            model_flux_fn, flat, flat_logp, defs, data,
            os.path.join(outdir, "lc_ratio_plot.png"),
            component_flux_fn=component_flux_fn, offset_base=offset_base)
    return metrics
