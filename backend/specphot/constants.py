"""specphot 常量表（02 规格 §7）。

本模块已落地：§7.1（零点与单位换算）、§7.2（通带、曲线与读侧）、§7.3（误差预算）、
§7.4（定标与测光配对）、§7.7（S0 输入装载与拟合引擎）全量，
外加 §7.5 的 C_COND_MAX（F-50 的 pinv 切换在 fluxcal 切片就要用）
与 §7.6 的 C_SPEC_PHOT_VERSION / C_CACHE_MAX / C_MAX_BANDS / C_ENDPOINT_TIMEOUT_S
（API-2 接线补齐；重档超时常量随 S2/S3/API-8 切片消费）。
§7.5 连续谱随 continuum 切片落地；其余小节（§7.8 / §7.9 余量）随各自模块的切片落地。

空气↔真空换算没有数值常量：一律 import wavconvert（02 §7.1 C_AIR2VAC 行，
禁行线 5 —— 不自写公式、不把阈值当期望值；窗口下限用 wavconvert.CONVERT_MIN_A）。
"""
import math as _math

# ─── §7.1 零点与单位换算 ─────────────────────────────────────────────
C_AB_ZERO = 48.60                      # AB 零点（约定值；禁写 m_AB(3631 Jy) == 0）
C_AB_MJY_ZERO = 16.4                   # f_mjy ↔ m_AB 零点（与宿主 bands.js / extinction.py 同值）
C_AB_LAM_ZERO = 2.4079482426801846     # = 48.60 − 2.5·log10(c[Å/s])，AB 在 Fλ 空间的写法（显示 2.408）
C_ST_ZERO = 21.10                      # ST 零点，配 ⟨Fλ⟩_ST 与 −5·log10(λ/5556 Å)
# C_ST_ZERO 行的合并常数（−5·log10 λ_Å 写法的常数项，全精度载体；显示 2.3762）。
# 数值上恰等于 21.10 − 5·log10(5556)（机器精度内 diff = 0.0，已实测核对）；
# §7.1 未给它单独命名，本名称为实现补名（T-52 的断言值）。
C_ST_LAM_CONST = 2.376188814672112
C_AA_PER_S = 2.99792458e18             # c，Å/s（SI 精确定义值）

# ─── §7.2 通带、曲线与读侧 ───────────────────────────────────────────
C_MIN_PIXELS = 8                       # 通带内最少可用像素（不足 ⇒ E-04 too_few_pixels）
C_OVERLAP_REJECT = 0.5                 # 通带-谱重叠比 < 此值 ⇒ E-04 no_overlap
C_OVERLAP_MIN = 0.95                   # 重叠比 ∈ [REJECT, MIN) ⇒ 保留 + CA-05
C_CURVE_PX_PER_FWHM_MIN = 8            # 曲线每半高宽节点数下限（低于 ⇒ CA-01 px_per_fwhm_low）
C_DLAM_OVER_FWHM_MAX = 3.0             # 降采样后 Δλ ≤ FWHM/此值；兼作无 R 时的分辨率元因子
C_MAX_SPECPOINTS = 20000               # 单谱点数闸门（2e4；库内实测最大 7,764）
C_MAX_UPLOAD_BYTES = 2_000_000         # 上传文本内容闸门（≈2 MB）
C_MAX_BODY_MB = 32                     # 请求体上限（与宿主 config.py 同源，禁止为绕过而改）
C_GAP_FACTOR = 5.0                     # Δλ > 此值 × 本文件中位 Δλ ⇒ 记入 gaps[]（V-10）
C_FLUX_MEDIAN_MAX_CGS = 1e-13          # median(|F|) 的 cgs 合理域上限（V-17 / F-14 第一道闸）
C_MAX_EXCLUDE = 32                     # 掩膜段数上限（Q-2 / V-13）
# 大气吸收带（只能剔除，禁止插值；F-72①）。宿主 tell 组（frontend/js/spec_lines.js:53
# 的 [[6867,6884],[7594,7621]]）已被第 1、3 行完全覆盖，不重复登记。
C_MASK_ABS_TABLE = [
    [6867.0, 6884.0, 'O2 B 带'],
    [7550.0, 7700.0, 'O2 A 带'],
    [7594.0, 7621.0, 'O2 A 带 细结构'],
]
# 天光发射线（[O I] 气辉三线，中心 ±40 Å；F-72② / F-69）
C_MASK_EMIS_TABLE = [
    [5537.0, 5617.0, '[O I] 5577'],
    [6260.0, 6340.0, '[O I] 6300'],
    [6323.0, 6403.0, '[O I] 6363'],
]

# ─── §7.7 S0 输入装载、诊断增强与拟合引擎 ─────────────────────────────
C_MIN_UPLOAD_POINTS = 10               # 上传件最少数据行（= 宿主 spectra.py 的 MIN_POINTS）
C_MAX_ANCHORS = 24                     # 手加锚点行上限（Q-25）
C_SESSION_INPUT_BUDGET_MB = 3          # sessionStorage 参数预算（IA-11 / ST-12）
C_Z_TOL = 0.02                         # z_from_lines 与所用 z 的容差（F-81 / CA-36）
C_LINE_MATCH_MIN = 3                   # 参与 z_fit 的最少命中线数（F-81）
C_SHAPE_PERT_EPS = 0.05                 # 通带形状扰动相对幅度（ln λ 上 RMS，F-83）
C_SHAPE_PERT_P = 60                     # 通带形状扰动组数（F-83）
C_SKY_SIDE = 60.0                      # 天光扣除局部连续谱侧带半宽（Å，F-86）
C_SKY_RESID_FLOOR = 0.7                # 扣除后线区残差 RMS ≤ 此值 × 扣除前才算起效（CA-37）
C_FRAME_PROBE_MIN_SNR = 8              # frame_probe 所需每分辨率元信噪（F-85）
C_DIFF_STEP = 1.4901161193847656e-08   # 数值 Jacobian 相对步长（= √eps）
C_NFEV_MAX = 20000                     # max_nfev（与宿主 lightcurves.py 同值）
C_MAX_STARTS = 4                       # 多起点上限（宿主先例同数，F-91⑤）
C_OPTIMALITY_MAX = 1e-6                # 一阶最优性事后门限（F-91⑦）
C_REL_NONLIN = 0.2                     # 非线性守门双阈值（F-95②）
C_CHOLESKY_MAX = 20000                 # AR(1) 白化点数顶（超过改 diag_infl，F-92③）
C_FD_REL = 1e-6                        # 派生量有限差分步长（F-95①；不得与 C_DIFF_STEP 互替）

# ─── §7.3 误差预算 ──────────────────────────────────────────────────
C_SCALE_2ND_DIFF = 0.6052698             # 二阶差分代理一致性因子 = C_MAD_SCALE/√6（F-54）
C_MAD_SCALE = 1.4826022                  # 高斯下 MAD→σ 一致性因子 = 1/Φ⁻¹(0.75)（mad_window 支）
C_MAD_WIN = 51                           # mad_window 分支滑窗点数（二阶差分支不用窗）
C_RHO_MIN = 0.2                          # AR(1) 门限：r 超此值积分误差乘 √((1+r)/(1−r))（F-55）
C_SIGMA_STRIDE = 2                       # 二阶差分步长 j（L-46 式(1) 转述的 DER_SNR 原设置）

# ─── §7.4 定标与测光配对 ────────────────────────────────────────────
C_MAG_SANITY_LO = 12.0                   # m_syn 合理域下限（越界 ⇒ CA-03 且该行不出结果）
C_MAG_SANITY_HI = 27.0                   # m_syn 合理域上限
C_MAG_SANITY_WINDOW = 3.0                # direct 第二道闸门：|Δm| 超此值 ⇒ flux_scale_suspect
C_DT_TOL_TABLE = {                       # 按源类别的实测点配对容差（天，F-61）
    'grb_early': 1.0, 'grb_afterglow': 3.0, 'sn_platform': 10.0, 'other': 3.0,
}
C_DT_TOL_D = 0.5                         # 类别字符串查不到时的兜底（不是 other 档）
C_DT_INTERP_D = 15.0                     # 容差外允许线性内插比对的实测点间隔上限（天）
C_EXT_BANDAVG_EPS = 0.005                # 波段平均消光 vs 单色消光之差的 CA-23 阈值（星等）

# ─── §7.5 连续谱拟合（S2） ───────────────────────────────────────────
C_POLY_ORDER_MAX = 7                     # Chebyshev 阶数上限（F-62④ / Q-15）
C_COND_MAX = 1e8                         # 两处共用：poly 设计矩阵超限降阶（F-62③，降到
                                         # 1 阶仍超限拒模型 CA-20）；F-50 波段协方差 C 超限改 pinv
C_GRID_MAX_PER_AXIS = 120                # 网格单轴点数上限（F-65 / ST-9）
C_GRID_EVAL_MAX = 20000                  # 单模型单请求求值次数上限（F-65 / ST-9）
C_DEGEN_RHO = 0.9                        # 参数相关退化门限（F-64③ / F-93③ / T-15）
C_FTEST_ALPHA = 0.05                     # 嵌套 F 检验显著性（F-29②）
C_BIC_MIN = 6                            # 非嵌套 ΔBIC「强证据」门限（F-29④，工程取值）
C_BB_BLUE_FRAC = 0.4                     # 黑体蓝端外推启用比例（F-31；消费者在装配切片）
C_MAX_MODELS = 6                         # S2 单次并存模型数（Q-13）
C_BOOT_N = 200                           # 自助默认次数（F-95②）
C_MAX_BOOT = 1000                        # 自助用户可上调上限（Q-15）
C_BOOT_N_BUDGET = 500                    # 单次请求内全部自助/剖面重拟合次数预算（Q-15 /
                                         # F-28 / F-95③；超出回落 + boot_budget_applied + CA-44③）
C_HOST_EBV_MAX = 2.0                     # prescribe 允许的宿主 E(B−V) 上限（F-87/Q-32/CA-43）

# ─── §7.6（摘）：版本戳与缓存 ────────────────────────────────────────
C_SPEC_PHOT_VERSION = '1.0.0'          # 口径版本戳（F-45；口径变更升版本而非写兼容代码）
C_CACHE_MAX = 200                      # 结果缓存条目上限（LRU，ST-3）

# ─── §7.6：服务侧闸门（API-2 接线补齐） ────────────────────────────────
C_MAX_BANDS = 16                       # 单次请求波段数上限（Q-1 / ST-7）
C_ENDPOINT_TIMEOUT_S = 2.0             # ST-5 轻档硬超时：S1（含 16 波段 + anchored），
                                       # 超时一律 E-11（§7.6：轻 2 s / 重 5 s；重档
                                       # 随 S2/S3/API-8 切片按 §7.6 表值 5 s 启用，
                                       # 不另设规格外常量名）

# ─── §7.9 预处理与稳健性（§3.14，P1b 读侧两段切片落地；其余随 P2 消费者到齐再进） ──
C_PREPROCESS_FACTORS = [1, 2, 3, 4, 6, 8]  # 合束因子闭集（F-108①；P2 切片 2c 起全集放行）
C_ERRCOL_BAD_FRAC = 0.5                # 非有限 σ 点占比阈 ⇒ nonfinite（F-110① 第 2 步）
C_ERRCOL_REL_SPREAD_TOL = 1e-3         # 相对散布容差（F-110① 第 4/5 步共用，单载体）
C_CLIP_NSIGMA = 4.0                    # σ 裁剪找离群候选的阈（取 4 不取 3：V-6
                                       # "非正是常态"；F-111②）
C_CLIP_MAX_FRAC = 0.05                 # 候选剔除占比上限，超过 ⇒ CA-47④ 不提供剔除
                                       # 按钮（F-111③）
C_BASE_ITER = 2                        # σ 裁剪迭代次数（§7.10 S3 表：C_LINE_WIN /
                                       # C_BASE_SIDE / C_BASE_ITER 行；F-36/F-111②）
C_SMOOTH_BOX_MAX_PX = 7                # 绘图平滑核长上限（Q-33 形态校验用；核长
                                       # C_SMOOTH_BOX_PX=3 属 P2 消费者，随 F-109 落地）

# ─── §7.9 P2 半段（切片 2c：合束 F-108 / 绘图平滑 F-109 / 杠杆值端点 F-112） ──
C_SMOOTH_BOX_PX = 3                    # 绘图平滑核长（全宽，像元）默认值（F-109①/U-51）；
                                       # ⚠️ 量级先例来自 L-52 Fig 3 的展示口径，不是推荐宽度
C_REBIN_TARGET_PER_FWHM = 2            # 目标采样 = 每分辨率元 2 点（§7.9 唯一 ① 类，
                                       # "two rebinned pixels per resolution element"，F-108④）
C_REBIN_GAIN_MIN = 1.15                # rebin_gain 下闸（CA-49①：合束基本没有收益）
C_REBIN_GAIN_MAX_RATIO = 1.1           # rebin_gain 上闸（CA-49④：gain > 此值×√k ⇒ 负 ρ 可疑）
C_LEVERAGE_K = 3.0                     # 杠杆阈：h_ii > K·p/n 判"低可靠端"（只标记不剔除，
                                       # Σh_ii=p 恒成立，F-112②/T-80②）
C_EDGE_SPREAD_MAX = 0.25               # baseline_edge_spread 告警阈（CA-48①，F-112①④）
C_EDGE_WIN_FRAC = 0.08                 # 端点窗口占 ln λ 覆盖比例（F-112①④）
C_NONPOS_FRAC_MAX = 0.10               # 非正流量占比阈，两处共用（主表 ⚑ 与 CA-48②
                                       # 第二支，同一常量不得各说一半，F-111⑤）

# ─── §7.10 S3 谱线测量（P3 切片 1，lines.py 消费） ──────────────────────
C_LINE_WIN = 40.0                       # 线区默认半宽 Å（未平移前，F-35）
C_BASE_SIDE = 25                        # 基线每侧点数（F-36 / U-25）
C_LINE_SNR_MIN = 3.0                    # 每「分辨率元」信噪门限（非逐像素、非线峰，F-68）
C_MAX_COMPONENTS = 3                    # 单条线最大轮廓成分数（F-37）
C_COG_TOL_RATIO = 0.3                   # 同离子多线柱密度互差比例，超出判生长曲线
                                        # 饱和 ⇒ column_density 降为下限（F-71/CA-27）

# ─── §7.9 P3d 阻尼翼与吸收系统（§3.13，02b；diagnostics.py/lines_api.py 消费） ──
# 分类三阈（① 类，文献给定：F-98③ 挂 L-52/L-53/L-54；互斥无缝半开区间）：
C_ABS_CLASS_DLA = 2e20                  # N ≥ 2e20 ⇒ dla
C_ABS_CLASS_SUBDLA = 1e19               # [1e19, 2e20) ⇒ sub_dla（上界=DLA 阈，无缝）
C_ABS_CLASS_LLS = 1.6e17                # [1.6e17, 1e19) ⇒ lls；未达 ⇒ class 空值
# 检出与 AOD 四阈（① 类成立条件/保守选择，非通用门槛：F-101①/T-71④）：
C_ABS_DETECT_SIGMA = 5                  # 吸收特征检出显著性（σ，L-52② 低分辨光栅谱保守 5σ）
C_AOD_SNR_RES_MIN = 7                   # AOD「高估<10%」成立的每分辨率元信噪（L-55①）；
                                        # ⚠️ 不得挪作阻尼翼门槛（翼用 C_WING_FIT_MIN_SNR）
C_AOD_SAMP_MIN = 2                      # 每分辨率元最少采样点（L-55②）
C_AOD_F_CUT = 0.01                      # Na(v) 积分核流量比下钳位（L-55③）
# 翼拟合与交叉链接八项（③ 类工程门限，01 §五——不得为它们编造文献，F-101①）：
C_WING_B_KMS = 25.0                     # b 的工程默认（F-99② 第三路；文献实测 21.5 属那条视线，不进常量）
C_WING_B_GRID_KMS = [10, 25, 50]        # b 灵敏度网格（只作展示、不进 err_source，F-104④）
C_WING_Z_WIN_SIGMA = 3                  # z 在金属线 ±3σ_z 窗口内变（F-99①）
C_WING_RED_KMS = 3000.0                 # 翼窗只取 Lyα 红侧 3000 km/s（F-99③ 蓝侧不进残差）
C_WING_FIT_MIN_SNR = 15.0               # 翼拟合的每分辨率元信噪顶（F-99③）
C_WING_SPREAD_DEX = 0.15                # 双连续谱族 log N 差告警阈（CA-45，F-100③）
C_XLINK_MASK_MAX = 0.20                 # 单波段被吸收系统掩膜通带占比上限（CA-46①）
C_SAT_DEPTH_FLOOR = 0.05                # 饱和判定的线心最低流量比（F-104①；τ₀=−ln0.05≈3 就地可导）

# T-39 自洽性在模块内先钉一遍（测试再断言），防手滑改错：
assert C_OVERLAP_REJECT < C_OVERLAP_MIN
assert C_MIN_PIXELS <= C_MAX_SPECPOINTS
assert abs(C_DIFF_STEP - _math.sqrt(2.220446049250313e-16)) < 1e-16
assert C_DIFF_STEP < C_FD_REL
assert C_MAG_SANITY_LO < C_MAG_SANITY_HI
assert C_DT_TOL_D <= min(C_DT_TOL_TABLE.values())
assert abs(C_SCALE_2ND_DIFF - C_MAD_SCALE / _math.sqrt(6.0)) < 1e-7
assert C_SIGMA_STRIDE >= 2
assert abs(C_ST_LAM_CONST - (C_ST_ZERO - 5.0 * _math.log10(5556.0))) < 1e-12
# §7.5 S2 自洽（F-29④ / F-93③ / Q-15）
assert 0.0 < C_DEGEN_RHO < 1.0
assert 0.0 < C_FTEST_ALPHA < 1.0
assert C_BOOT_N <= C_BOOT_N_BUDGET <= C_MAX_BOOT * (C_MAX_MODELS - 1)
# §7.10 S3 自洽（F-35/F-36/F-68/F-71）
assert C_LINE_WIN > 0 and C_BASE_SIDE >= 2
assert C_LINE_SNR_MIN > 0 and C_MAX_COMPONENTS >= 1 and C_COG_TOL_RATIO > 0
# §7.9 吸收系统族自洽（F-98③ 半开区间无缝：LLS < SUBDLA < DLA；F-99/F-103）
assert 0 < C_ABS_CLASS_LLS < C_ABS_CLASS_SUBDLA < C_ABS_CLASS_DLA
assert C_ABS_DETECT_SIGMA > 0 and C_WING_FIT_MIN_SNR > 0
assert 0 < C_SAT_DEPTH_FLOOR < 1 and 0 < C_XLINK_MASK_MAX < 1
assert C_WING_B_KMS in C_WING_B_GRID_KMS and len(C_WING_B_GRID_KMS) == 3
