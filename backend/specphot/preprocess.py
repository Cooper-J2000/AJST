"""specphot.preprocess —— §3.14 预处理与稳健性（P1b 读侧两段 + P2 数值半段，切片 2c）。

设计权威：`Workplace_Agent/AJST-enrich/spec_x_filter_fuction_design_review/02b_可选功能规格.md`
（F-106…F-113、T-75/76/77/78/79/80、W-36/37/38）与 `02_精简版_核心规格.md` §4.2 preprocess{} 行
（键集与键序的唯一载体）、§5.2 Q-33/Q-34、§4.1 V-21/V-22、F-72、§7.9 常量表、§9 P1b/P2 行。

P1b 范围（§9 P1b 行原文圈定）：
  - 掩膜归并（F-107①）：四类来源并成一个布尔标志位数组后才算 mask_hash。实际
    生效三类：大气吸收表 C_MASK_ABS_TABLE（F-72①，只能剔除）、天光发射表
    C_MASK_EMIS_TABLE（F-72②，默认剔除）、用户数值段（U-13 框选 + U-50 手输，
    同一用户类）。吸收系统类（F-103①）属 P3d，本类恒空数组留位。
  - 误差列退化判定（F-110①/V-22）：六值闭集 errcol_verdict，判定序唯一、命中即停、
    只回显一个值；n_err<30 时相对散布两步跳过、末位 ok 同挂 CA-21。
  - 离群点（F-111②③/T-79③）：C_BASE_ITER 次 σ 裁剪只**找出候选**并回显
    n_outlier_flagged，绝不自动剔除；候选占比 > C_CLIP_MAX_FRAC ⇒ CA-47④ 且
    不提供剔除按钮。
  - F-80①–④ 规范化序列化的**唯一载体**在本文件（F-113⑤ 单一载体纪律；
    自 photometry.py 迁入，行为逐字节不变）。

P2 半段（切片 2c，§9 P2 行 / F-113⑥：F-108①②③⑤ 合束算术与三键回显、
F-109 绘图平滑、F-112 杠杆值端点闸门）：
  - 合束（F-108）：因子闭集 C_PREPROCESS_FACTORS（表外 E-14 rebin_factor）；
    块内平均（Δλ 加权 = 面积守恒 T-76①，均匀网格 Δλ 全等时退化为等权）；
    尾块规则（n%k≠0 丢尾块，n_rebinned_pixels=⌊n/k⌋）；σ_bin 通式
    （w_i²σ_i² + 2Σw_iw_jσ_iσ_jρ^|j−i|)/(Σw)²，闭式仅作 w≡1、σ_i≡σ 特例自检
    （T-76②，1e-12）；rebin_gain=σ/σ_bin 谱级标量，ρ 未回显 ⇒ null + CA-49③。
  - 绘图平滑（F-109）：boxcar 只产出 display_smoothed（与数值数组并列的结构位），
    数值通路只接 fit_array；不进任何哈希、不让 stale（响应逐字节同，T-77②③）。
  - F-112 的杠杆值端点闸门在 S2 poly 侧（continuum.py），本文件只承载
    preprocess{} 的 S2 四键回显位；S1/API-2 无 F-62 基线 ⇒ 该通路不适用（null）。

实现登记（规格未逐项钉死处，切片 2c 汇报同文复述）：
  - F-111② 的候选搜索在 S1 无连续谱基线（F-36 属 S3），P1b 在 F-54 的二阶差分
    残差上做 C_BASE_ITER 次迭代裁剪（阈 C_CLIP_NSIGMA，稳健尺度 = C_MAD_SCALE ×
    median|r−median(r)|；跨 V-10 间隙的三联不参与，F-97④ 同族前提）。S2/S3 落地
    后改挂 F-36 的基线残差，候选语义不变。
  - F-110③ 回落支的 sigma_method 按 F-54 词表取 _proxy_sigma 的实际结果
    （second_diff/mad_window）；原文括注"部分行有列时 mixed"按 V-8 的 ragged 语义
    理解，本实现回落时整列弃用（verdict 非 ok ⇒ 列不可信，不存在"部分坚持"）。
  - Q-33 的 errcol_choice 词表以原文 {auto, proxy, as_provided} 为准；verdict=ok 时
    收到 as_provided ⇒ E-14（reason=errcol_choice，Q-33"仅当判非 ok 才允许"）；
    verdict=ok 时收到 proxy 与 err_policy='force_proxy' 同义（不挂 CA-47②，
    CA-47② 的触发域是 verdict≠ok）。
  - V-21"抽后再判"随 P2 合束算术落地：合束后逐波段 E-04（no_overlap/
    too_few_pixels）在积分层照常行级拒绝；欠分辨闸门在 F-108④/CA-49②。
  - F-108③ ρ 的"未回显"域（实现裁量）：rho_lag1=None（不可估，CA-21）或
    0 ≤ r ≤ C_RHO_MIN（正侧低于 AR(1) 门限 ⇒ F-55 白噪声豁免域）⇒ rebin_gain=null
    + CA-49③；**负 r 恒回显**（F-108③"定义域允许为负"且 CA-49④ 的负 ρ 支
    必须可达，T-76⑤ 的 ρ=−0.9 用例）。ρ 未回显时 σ_bin 的**传播合成**取 ρ=0
    （与 F-55 对该域不放大 corr_infl 的既有白噪声豁免同口径），但收益声明
    （rebin_gain）恒 null —— F-108③"不取 ρ=0 兜底"禁止的是把 0 冒充相关估计
    去声明收益，不是禁止下游传播有数。
  - F-108② 权重：w_i = Δλ_i（被掩像素权重置 0，F-106① 次序：先掩膜后合束）；
    块代表波长 = 未掩像素的 Δλ 加权均值（裁量：规格未钉死代表元取法）。
    dlam_after_aa = 完整块代表 λ 序列的中位 Δλ（"只描述完整块"）。
  - F-108③ rebin_gain 分母：谱级标量取全块 σ_bin 的 median（分子 median(σ_i)
    的同型对应；规格只钉"分母取通式的块内合成值"，块间取中位为裁量）。
  - F-109 响应回显裁量：preprocess.smooth_px 在响应中恒 0 —— 平滑是纯前端绘制
    （F-109⑤），服务端只做 Q-33 形态校验（越界 E-14）与 display_smoothed 结构位；
    回显请求值会让开/关平滑的响应（含 JSON 导出原文）不同，与 T-77②③ 冲突。
  - F-112 的 CA-48②（nonpos_frac 且 cond_2 同时超）由 continuum.fit_poly 装配
    （nonpos_frac 由 API-3 拟合区传入）；S1/API-2 无 F-62 基线 ⇒ S2 四键恒 null/[]。
"""
import hashlib
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .constants import (C_MASK_ABS_TABLE, C_MASK_EMIS_TABLE, C_MAX_EXCLUDE,
                        C_PREPROCESS_FACTORS, C_ERRCOL_BAD_FRAC,
                        C_ERRCOL_REL_SPREAD_TOL, C_CLIP_NSIGMA, C_CLIP_MAX_FRAC,
                        C_BASE_ITER, C_SIGMA_STRIDE, C_MAD_SCALE,
                        C_SMOOTH_BOX_MAX_PX, C_GAP_FACTOR, C_SPEC_PHOT_VERSION,
                        C_REBIN_GAIN_MIN,
                        C_REBIN_GAIN_MAX_RATIO, C_CURVE_PX_PER_FWHM_MIN,
                        C_RHO_MIN)
from .reader import SpecLoadError

# F-110① 六值闭集（判定序即本元组的序；命中即停，只回显一个值）
ERRCOL_VERDICTS = ('ok', 'all_zero', 'constant', 'nonfinite', 'negative',
                   'flat_relative')
# Q-33 词表（原文闭集；U-53 的二选一 = proxy / as_provided，auto 是未确认时的缺省）
ERRCOL_CHOICES = ('auto', 'proxy', 'as_provided')


# ─── F-80①–④ 规范化序列化（全文唯一载体；自 photometry.py 迁入，逐字节同行为） ──

def f80_val(v):
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return '"NaN"'
        if math.isinf(v):
            return '"Infinity"' if v > 0 else '"-Infinity"'
        return repr(float('%.12g' % v))
    if isinstance(v, str):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(v, dict):
        # F-80①：dict 按"排序后的 (key, value) 对"序列化（递归适用）——否则
        # dict 落入 list 分支只得键名，缓存键对 anchor_rows/custom_curves 的
        # 值变化不敏感，会命中错误缓存（ST-3）。
        return '{' + ','.join(f80_val(k) + ':' + f80_val(v[k])
                              for k in sorted(v, key=str)) + '}'
    return '[' + ','.join(f80_val(x) for x in v) + ']'


def f80_hash(seq, n=12):
    return hashlib.sha256(('[' + ','.join(f80_val(v) for v in seq) + ']')
                          .encode('utf-8')).hexdigest()[:n]


# ─── F-107①：四类掩膜归一（并集成布尔标志位数组后才算 mask_hash） ─────────

def _dedupe(segs):
    """保序去重（精确同段；浮点为请求原值，V-13 过滤后比较）。"""
    out = []
    for s in segs:
        if s not in out:
            out.append(s)
    return out


def _v13_filter(segs, lam_min, lam_max):
    """V-13：每段须 lo<hi 且与谱覆盖至少部分重叠；退化/无效段被忽略并回显
    （不进哈希）。返回（有效段 [[lo,hi],…按 (lo,hi) 升序], 忽略数）。"""
    out, ignored = [], 0
    for seg in segs:
        try:
            lo, hi = float(seg[0]), float(seg[1])
        except (TypeError, ValueError, IndexError):
            ignored += 1
            continue
        if lo < hi and hi > lam_min and lo < lam_max:
            out.append([lo, hi])
        else:
            ignored += 1
    out.sort(key=lambda s: (s[0], s[1]))
    return out, ignored


def merge_masks(lam, user_ranges, absorber_ranges=None):
    """五类并集（F-107①）。返回 dict：
      abs / emis / user / absorber —— 各类 V-13 过滤后的段数组（哈希第 1–4 槽，
      精确重复段去重：同一段经 U-13 与 U-50 重复申报不改变掩膜与哈希，W-36）；
      merged —— 五类并集（第 5 槽；排序去重、不合并重叠段，重叠去重由布尔数组
                天然完成，n_masked_pixels 计像素不计段）；
      mask_bool —— bool[n] 标志位数组（F-106③：不截断、不插值，全谱等长）；
      n_masked —— 被掩像素总数；n_ignored —— 被忽略的用户段数（CA-10 回显）。

    P1b 实际生效三类：abs/emis 来自常量表按 F-72①② 默认剔除（V-13 过滤后与
    覆盖无重叠的谱上为空数组 ⇒ 与 P1 的空槽逐字节同值，T-75① 不破）；absorber
    属 P3d（F-103①：吸收系统掩膜第五类——U-48 开且前置满足时由识别通路给出
    蓝侧 IGM 段；库内现状前置缺 ⇒ 恒空，各槽与 P3c 基线逐字节同值，T-72① 不破）。
    """
    lam = np.asarray(lam, dtype=float)
    lam_min, lam_max = float(lam[0]), float(lam[-1])
    abs_segs, _ig1 = _v13_filter([[s[0], s[1]] for s in C_MASK_ABS_TABLE],
                                 lam_min, lam_max)
    emis_segs, _ig2 = _v13_filter([[s[0], s[1]] for s in C_MASK_EMIS_TABLE],
                                  lam_min, lam_max)
    user_segs, n_ignored = _v13_filter(user_ranges, lam_min, lam_max)
    user_segs = _dedupe(user_segs)                        # W-36：同段不双计
    absorber_segs, _ig3 = _v13_filter([[s[0], s[1]] for s in
                                       list(absorber_ranges or [])],
                                      lam_min, lam_max)
    absorber_segs = _dedupe(absorber_segs)
    merged = _dedupe(sorted(abs_segs + emis_segs + user_segs + absorber_segs,
                            key=lambda s: (s[0], s[1])))
    if len(merged) > C_MAX_EXCLUDE:                       # F-107⑤：并集后仍受此限
        raise SpecLoadError('E-07', f'五类掩膜并集后段数 {len(merged)} 超过上限 '
                                    f'{C_MAX_EXCLUDE}', reason='too_many_masks')
    mask_bool = np.zeros(lam.size, dtype=bool)
    for lo, hi in merged:
        mask_bool |= (lam >= lo) & (lam <= hi)
    return {'abs': abs_segs, 'emis': emis_segs, 'user': user_segs,
            'absorber': absorber_segs, 'merged': merged, 'mask_bool': mask_bool,
            'n_masked': int(np.count_nonzero(mask_bool)), 'n_ignored': n_ignored}


def mask_hash(masks, lam, spec_hash):
    """§4.2 mask_hash 键序（实现载体；值域=V-13 过滤后的段数组 + 有效像素 RLE）。

    四类段数组（F-107① 序：大气/天光/用户/吸收系统）→ 并集 → 有效像素布尔数组
    游程编码（首段恒 false 计数）→ spec_hash。P1 行为逐字节兼容：仅用户类非空
    且自动表零命中时各槽与 P1 同值。
    """
    lam = np.asarray(lam, dtype=float)
    valid = ~np.asarray(masks['mask_bool'], dtype=bool)
    rle, cur, n = [], False, 0
    for v in valid:
        if bool(v) == cur:
            n += 1
        else:
            rle.append(n)
            cur, n = bool(v), 1
    rle.append(n)
    return f80_hash([masks['abs'], masks['emis'], masks['user'], masks['absorber'],
                     masks['merged'], rle, spec_hash])


def preprocess_hash(mask_hash_, factor, errcol_verdict_, errcol_accepted,
                    spec_hash, version=C_SPEC_PHOT_VERSION):
    """§4.2 preprocess_hash 行（输入集的唯一载体在那一行）：键按序 =
    {factor, mask_hash, errcol_verdict, errcol_accepted, spec_hash, 版本戳本身}；
    编码细则同 F-80①–④。smooth_px 有意不在键集（F-109⑤ 平滑不进哈希）。"""
    return f80_hash([factor, mask_hash_, errcol_verdict_, errcol_accepted,
                     spec_hash, version])


# ─── F-110①：误差列退化判定（六值闭集、判定序唯一、命中即停） ─────────────

def errcol_verdict(flux, flux_err):
    """F-110①/V-22。统计域 = V-3a 规范化后**有限 σ 的行**，记 n_err。

    判定序（唯一、命中即停、只回显一个值）：
      1 negative   任一 σ<0（不计数即判）
      2 nonfinite  非有限 σ 占比 > C_ERRCOL_BAD_FRAC（先于 3–6：整列不可信成因）
      3 all_zero   median(|σ|)=0（有限行上取）
      4 constant   n_err≥30 且 std(σ)/median(|σ|) < C_ERRCOL_REL_SPREAD_TOL
      5 flat_relative 只在 median(|F|)>0 时评估（|F_i|≥median(|F|) 的有限行上
                     r_i=σ_i/|F_i| 的相对散布；median(|F|)≤0 ⇒ 本步不命中）
      6 ok         前五步都不命中
    n_err<30（与 CA-21 同数同口径）⇒ 第 4/5 步跳过；末位 ok 仍可写但必须同挂
    CA-21（ca21=True）。无误差列（flux_err=None）⇒ 'ok'、n_err=0（F-110⑤：
    代理本来就是默认通路，本条判的是"列在但可不可信"）。
    消光改正作用在到达地球的波长上（F-106① 次序），本判定在消光之后以
    flux_corr 调用；常数性消光因子使 r_i 的相对散布不变，判定域不受影响。
    """
    if flux_err is None:
        return {'verdict': 'ok', 'n_err': 0, 'ca21': False}
    s = np.asarray([np.nan if e is None else float(e) for e in flux_err],
                   dtype=float)
    f = np.asarray(flux, dtype=float)
    n_total = s.size
    if n_total == 0:
        return {'verdict': 'ok', 'n_err': 0, 'ca21': False}
    fin = np.isfinite(s)
    n_err = int(np.count_nonzero(fin))

    def _out(v):
        return {'verdict': v, 'n_err': n_err, 'ca21': False}

    if bool(np.any(s[fin] < 0)):                          # 第 1 步
        return _out('negative')
    if (n_total - n_err) / n_total > C_ERRCOL_BAD_FRAC:   # 第 2 步
        return _out('nonfinite')
    if n_err == 0:                                        # 全非有限已被第 2 步接住
        return _out('nonfinite')
    sv = s[fin]
    med_abs = float(np.median(np.abs(sv)))
    if med_abs == 0.0:                                    # 第 3 步
        return _out('all_zero')
    if n_err >= 30:                                       # 点数总则：4/5 步可判域
        if float(np.std(sv)) / med_abs < C_ERRCOL_REL_SPREAD_TOL:
            return _out('constant')                       # 第 4 步（保守先判）
        abs_f = np.abs(f[:n_total][fin])
        med_f = float(np.median(abs_f))
        if med_f > 0:
            sel = abs_f >= med_f
            r = sv[sel] / abs_f[sel]
            med_r = float(np.median(r))
            if med_r > 0 and float(np.std(r)) / med_r < C_ERRCOL_REL_SPREAD_TOL:
                return _out('flat_relative')              # 第 5 步
    ca21 = n_err < 30                                     # 末位 ok + 点数总则
    return {'verdict': 'ok', 'n_err': n_err, 'ca21': ca21}


# ─── F-111②③：σ 裁剪只找候选（T-79③：未经 U-52 确认前掩膜不变） ───────────

def outlier_candidates(lam, flux):
    """C_BASE_ITER 次 σ 裁剪找离群候选（只标记，不剔除）。

    S1 无基线（F-36 属 S3），候选在 F-54 的二阶差分残差上找（登记见模块
    docstring）：r_i = 2F_i − F_{i−j} − F_{i+j}（j=C_SIGMA_STRIDE），稳健尺度
    s = C_MAD_SCALE × median|r − median(r)|（≈ r 的逐像素 σ），|r−median(r)| >
    C_CLIP_NSIGMA × s 判候选；迭代 C_BASE_ITER 次（已标点退出尺度重估）。跨
    V-10 间隙的三联不参与（F-97④：非等距网格上核方差前提破坏）。覆盖内点数
    < 30 ⇒ 不可判（与 CA-21 同数同口径），返回全零。

    返回 (n_flagged, segments, over_frac)：
      segments —— 连续被标像素并成 [lo,hi] 段（U-52 逐段确认的载体，随
                  CA-10/outlier_flagged 告警回显）；
      over_frac —— 待剔点数 > C_CLIP_MAX_FRAC × 覆盖内点数（F-111③ ⇒ CA-47④，
                  不提供剔除按钮）。
    """
    lam = np.asarray(lam, dtype=float)
    f = np.asarray(flux, dtype=float)
    n = f.size
    j = C_SIGMA_STRIDE
    if n < 2 * j + 1 or n < 30:
        return 0, [], False
    active = np.ones(n, dtype=bool)
    dlam = np.diff(lam)
    med_dlam = float(np.median(dlam)) if dlam.size else 0.0
    span = lam[2 * j:] - lam[:-2 * j]
    span_ok = np.zeros(n, dtype=bool)
    if med_dlam > 0:
        span_ok[j:-j] = span <= C_GAP_FACTOR * med_dlam
    flagged = np.zeros(n, dtype=bool)
    for _ in range(C_BASE_ITER):
        r = np.full(n, np.nan)
        r[j:-j] = 2.0 * f[j:-j] - f[:-2 * j] - f[2 * j:]
        r[~span_ok] = np.nan
        r[~active] = np.nan
        fin = np.isfinite(r)
        if int(np.count_nonzero(fin)) < 30:
            return 0, [], False
        med_r = float(np.median(r[fin]))
        s = C_MAD_SCALE * float(np.median(np.abs(r[fin] - med_r)))
        if s <= 0:
            break
        newly = fin & (np.abs(r - med_r) > C_CLIP_NSIGMA * s)
        if not bool(np.any(newly)):
            break
        flagged |= newly
        active &= ~newly
    n_flagged = int(np.count_nonzero(flagged))
    segments = []
    if n_flagged:
        idx = np.flatnonzero(flagged)
        start = prev = int(idx[0])
        for i in idx[1:]:
            i = int(i)
            if i == prev + 1:
                prev = i
                continue
            segments.append([float(lam[start]), float(lam[prev])])
            start = prev = i
        segments.append([float(lam[start]), float(lam[prev])])
    return n_flagged, segments, bool(n_flagged > C_CLIP_MAX_FRAC * n)


# ─── F-108：合束算术（P2 切片 2c：块内平均/面积守恒/σ 通式/rebin_gain/CA-49） ──

def _dlam_array(lam):
    """逐像素 Δλ（内部取相邻中点式 (λ_{i+1}−λ_{i−1})/2，两端单侧），非均匀网格
    按 V-9 聚合后的原生网格计（F-108②：非均匀按 Δλ 加权；均匀网格 Δλ 全等 ⇒
    自动退化为等权，两例共用同一算术）。"""
    lam = np.asarray(lam, dtype=float)
    d = np.empty(lam.size, dtype=float)
    if lam.size == 1:
        d[0] = 1.0
        return d
    d[1:-1] = (lam[2:] - lam[:-2]) / 2.0
    d[0] = lam[1] - lam[0]
    d[-1] = lam[-1] - lam[-2]
    return d


def sigma_bin_general(sigma_i, w_i, rho):
    """F-108③ **通式**（全文唯一定义处）：
      σ_bin² = [Σ w_i²σ_i² + 2·Σ_{i<j} w_i w_j σ_i σ_j ρ^{j−i}] / (Σ w_i)²
    w_i 为块内权重（Δλ 加权或等权），σ_i 逐像元代理 σ（F-54/F-97 通路），
    ρ = F-55 在原始像元上估的 rho_lag1（AR(1)，定义域 (−1,1)，允许为负）。"""
    s = np.asarray(sigma_i, dtype=float)
    w = np.asarray(w_i, dtype=float)
    sw = float(np.sum(w))
    if s.size == 0 or sw <= 0:
        return math.nan
    ws = w * s
    var = float(np.sum(ws ** 2))
    if rho and s.size > 1:
        ii, jj = np.triu_indices(s.size, k=1)
        var += 2.0 * float(np.sum(ws[ii] * ws[jj] * (float(rho) ** (jj - ii))))
    return math.sqrt(max(var, 0.0)) / sw


def sigma_bin_closed(k, sigma, rho):
    """F-108③ 闭式（w≡1、σ_i≡σ 的**特例**，只作自检，T-76② 断言到 1e-12）：
      σ_bin² = σ²·[k + 2·Σ_{d=1}^{k−1}(k−d)·ρ^d] / k²
    ρ=0 ⇒ σ/√k；ρ→1 ⇒ σ_bin→σ（无收益）。禁则：不得以 σ/√k 实现 rebin_gain。"""
    k = int(k)
    if k < 1:
        return math.nan
    s2 = float(sigma) ** 2 * (k + 2.0 * sum((k - d) * float(rho) ** d
                                            for d in range(1, k))) / (k * k)
    return math.sqrt(max(s2, 0.0))


def rebin_blocks(lam, mask_bool, factor):
    """F-108② 的分块：完整块列表 [(idx, w), …] + 元信息。

    尾块规则（全文唯一）：n % k ≠ 0 时**丢弃**不足 k 的最后一个尾块 ⇒
    n_rebinned_pixels = ⌊n/k⌋；被丢像元数不另立键（n 与 k 均回显 ⇒ 可复算），
    由 TXT-27 摘要行写明。被掩像素权重置 0（F-106① 次序：先掩膜后合束 ⇒
    被掩像素不参与块均值，否则已剔尖峰仍以 1/k 权重漏进结果）。
    w_i = Δλ_i（均匀网格自动退化为等权，T-76① 面积守恒两例同源）。"""
    lam = np.asarray(lam, dtype=float)
    valid = ~np.asarray(mask_bool, dtype=bool)
    dlam = _dlam_array(lam)
    k, n = int(factor), lam.size
    n_blocks = n // k
    blocks = []
    for b in range(n_blocks):
        idx = np.arange(b * k, (b + 1) * k)
        w = dlam[idx] * valid[idx]
        blocks.append((idx, w))
    meta = {'k': k, 'n': int(n), 'n_blocks': n_blocks,
            'n_dropped': int(n - n_blocks * k), 'dlam': dlam}
    return blocks, meta


def combine_blocks(values, blocks):
    """按 rebin_blocks 的块做加权平均（F：块内均值；λ：块代表波长）。
    无有效贡献（全掩/全非有限）⇒ NaN；非有限值与被掩像素一起退出权重并
    归一化（面积守恒断言域 = 未掩且有限的完整块，T-76①）。"""
    v = np.asarray(values, dtype=float)
    out = np.full(len(blocks), np.nan)
    for b, (idx, w) in enumerate(blocks):
        ok = (w > 0) & np.isfinite(v[idx])
        if not bool(np.any(ok)):
            continue
        out[b] = float(np.sum(v[idx][ok] * w[ok]) / float(np.sum(w[ok])))
    return out


def sigma_bin_blocks(sigma_px, blocks, rho):
    """逐块 σ_bin（F-108③ 通式逐块代入；被掩/非有限 σ 像素不参与权重）。"""
    s = np.asarray(sigma_px, dtype=float)
    out = np.full(len(blocks), np.nan)
    for b, (idx, w) in enumerate(blocks):
        ok = (w > 0) & np.isfinite(s[idx])
        if not bool(np.any(ok)):
            continue
        if not bool(np.all(ok)):
            # F-108③：保留原块内下标传 w=0（非有限 σ 像素置 w=0），ρ^{j−i} 指数
            # 按原下标计，不压缩序（压缩会改变相关项的 j−i）
            w = np.where(ok, w, 0.0)
            sv = np.where(np.isfinite(s[idx]), s[idx], 0.0)
            out[b] = sigma_bin_general(sv, w, rho)
        else:
            out[b] = sigma_bin_general(s[idx], w, rho)
    return out


def rho_effective(rho):
    """F-108③ ρ 的有效域裁决（实现登记见模块 docstring）：rho_lag1 未回显
    （None，或 0 ≤ r ≤ C_RHO_MIN 的正侧白噪声豁免域）⇒ 传播合成取 0.0 且
    收益不可声明；负 r 恒回显（CA-49④ 必须可达）。返回 (rho_eff, rho_echoed)。"""
    if rho is None or (0.0 <= float(rho) <= C_RHO_MIN):
        return 0.0, False
    return float(rho), True


def px_per_fwhm_after(fwhm_aa, dlam_after_aa):
    """抽后每分辨率元的等效采样点数（F-108④ 下限闸门的判定量）。
    仪器 FWHM 不可得（r_source='none'，F-69 全库现状）⇒ None ⇒ 闸门无从判定
    ⇒ 合束仍可用但必挂 CA-47③；fwhm 已知时 = FWHM/Δλ_after（Δλ ≤ FWHM/
    C_DLAM_OVER_FWHM_MAX 的对偶表述）。"""
    if fwhm_aa is None or not dlam_after_aa or dlam_after_aa <= 0:
        return None
    return float(fwhm_aa) / float(dlam_after_aa)


def rebin_stage(lam, flux, sigma_px, mask_bool, factor, rho):
    """F-108②③⑤ 合束主通路（API-2/API-3 同一入口），返回 dict：
      lam / flux / sigma / mask_bool —— 抽后网格（PreprocessedSpectrum 新形态）
      n_rebinned_pixels / n_dropped / dlam_after_aa / rebin_gain —— §4.2 回显键
      rho_eff / rho_echoed —— ρ 有效域裁决（docstring 登记的裁量）
      blocks —— 块表（同一权重可再施加于其它数组，如未改正通量的 delta_a 比对）
      echo —— response_block 的 rebin 四键
    rebin_gain = median(σ_i)/median(σ_bin)：分子取覆盖内有限未掩行 median(σ_i)
    （保守，不取最大 σ_i），分母取全块 σ_bin 的中位（谱级标量）；ρ 未回显 ⇒
    null（键在场，F-108③）。factor=1 不进本函数（恒等态短路在调用方）。"""
    blocks, meta = rebin_blocks(lam, mask_bool, factor)
    lam_rb = combine_blocks(np.asarray(lam, dtype=float), blocks)
    flux_rb = combine_blocks(flux, blocks)
    rho_eff, rho_echoed = rho_effective(rho)
    sig_rb = sigma_bin_blocks(sigma_px, blocks, rho_eff)
    mb = np.asarray(mask_bool, dtype=bool)
    mask_rb = np.array([bool(np.all(mb[idx])) for idx, _w in blocks], dtype=bool)
    # dlam_after_aa：完整块代表 λ 序列的中位 Δλ（"只描述完整块"，F-108②）
    dlam_after = (float(np.median(np.diff(lam_rb[np.isfinite(lam_rb)])))
                  if np.count_nonzero(np.isfinite(lam_rb)) > 1
                  else float(np.median(meta['dlam']) * factor))
    # rebin_gain：谱级标量（F-108③ 唯一定义处；ρ 未回显 ⇒ null）
    s_all = np.asarray(sigma_px, dtype=float)
    dom = np.zeros(s_all.size, dtype=bool)
    for idx, w in blocks:
        dom[idx[w > 0]] = True
    dom &= np.isfinite(s_all)
    num = float(np.median(s_all[dom])) if bool(np.any(dom)) else None
    fin = sig_rb[np.isfinite(sig_rb) & (sig_rb > 0)]
    den = float(np.median(fin)) if fin.size else None
    gain = None
    if rho_echoed and num is not None and num > 0 and den is not None and den > 0:
        gain = num / den
    echo = {'n_rebinned_pixels': int(meta['n_blocks']),
            'n_dropped': int(meta['n_dropped']),
            'dlam_after_aa': dlam_after,
            'rebin_gain': gain, 'px_per_fwhm_after': None}
    return {'lam': lam_rb, 'flux': flux_rb, 'sigma': sig_rb, 'mask_bool': mask_rb,
            'blocks': blocks, 'rho_eff': rho_eff, 'rho_echoed': rho_echoed,
            'echo': echo, 'k': int(factor)}


def ca49_warnings(factor, rebin_gain, px_per_fwhm_after, rho_echoed):
    """CA-49 闭合触发集（§5.4 表：四支，factor>1 时任一成立），四支文案各自
    可辨（不得把 ③④ 也写成"基本没收益"）。③（ρ 未回显 ⇒ rebin_gain=null）
    与 ① 不并现：null 时 ① 无从判定（T-76⑤）。同屏提示并排比较需同一
    preprocess_hash（F-107③/F-29①）。"""
    k = int(factor)
    out = []
    tail = ('与 curve_nodes、px_per_fwhm_after 同屏回显；要并排比较两个因子，'
            '需要同一 preprocess_hash（F-107③/F-29①）')
    if not rho_echoed:
        out.append({'code': 'CA-49', 'reason': 'rebin_gain_no_rho',
                    'message': '收益不可声明（CA-49③）：无相关估计（rho_lag1 未回'
                               '显）⇒ rebin_gain=null，合束仍可用但不给收益声明'
                               f'（F-108③）；{tail}'})
        return out
    if rebin_gain is not None and rebin_gain < C_REBIN_GAIN_MIN:
        out.append({'code': 'CA-49', 'reason': 'rebin_gain_low',
                    'message': f'这次合束基本没有收益（CA-49①）：rebin_gain='
                               f'{rebin_gain:.3g} < C_REBIN_GAIN_MIN='
                               f'{C_REBIN_GAIN_MIN}，随机误差的下降抵不上通带'
                               f'采样变粗（F-108⑤）；{tail}'})
    if px_per_fwhm_after is not None and px_per_fwhm_after < C_CURVE_PX_PER_FWHM_MIN:
        out.append({'code': 'CA-49', 'reason': 'px_per_fwhm_after_low',
                    'message': f'已粗到欠分辨（CA-49②）：抽后 px_per_fwhm_after='
                               f'{px_per_fwhm_after:.3g} < C_CURVE_PX_PER_FWHM_MIN='
                               f'{C_CURVE_PX_PER_FWHM_MIN}，与 factor=1 的差可能主'
                               f'要由采样口径造成（F-108④）；{tail}'})
    if rebin_gain is not None \
            and rebin_gain > C_REBIN_GAIN_MAX_RATIO * math.sqrt(k):
        out.append({'code': 'CA-49', 'reason': 'rebin_gain_over_independent',
                    'message': f'收益超过独立情形 ⇒ 相关估计可疑（CA-49④）：'
                               f'rebin_gain={rebin_gain:.3g} > '
                               f'C_REBIN_GAIN_MAX_RATIO·√k='
                               f'{C_REBIN_GAIN_MAX_RATIO * math.sqrt(k):.3g}，'
                               f'负 ρ 使 σ_bin < σ/√k（F-108③）；{tail}'})
    return out


# ─── F-109：绘图平滑（结构隔离：只产出 display_smoothed，数值通路只接 fit_array） ──

def _boxcar_display(flux, px):
    """F-109① 的箱式平滑算术（全宽 px 像元，逐点窗口内有限值均值）。
    结果只作 display_smoothed 结构位（T-77① 的 AST 扫描域内它只以写入位置
    出现在本文件），不进任何哈希、不进导出件、不进数值通路。"""
    f = np.asarray(flux, dtype=float)
    h = max(int(px) // 2, 0)
    out = np.full(f.size, np.nan)
    for i in range(f.size):
        seg = f[max(0, i - h):min(f.size, i + h + 1)]
        ok = np.isfinite(seg)
        if bool(np.any(ok)):
            out[i] = float(np.mean(seg[ok]))
    return out


# ─── Q-33：preprocess{} 请求形态校验 + P1b 期次闸（A-3/E-13） ──────────────

def validate_request(pp):
    """Q-33 形态校验，返回 (factor, ranges, smooth_px, errcol_choice)。

    factor ∈ C_PREPROCESS_FACTORS（表外 ⇒ E-14 rebin_factor；P2 切片 2c 起全集
    放行，封闭集保证 sigma_bin 的相关核与 preprocess_hash 与 k 一一对应，
    F-108①）。ranges 每段 lo<hi 且有限（E-14 preprocess_range）、逐类 ≤
    C_MAX_EXCLUDE（E-07 too_many_masks，Q-2）。smooth ∈ {false} ∪ [1, 上限]
    （Q-33）：JSON true 不在集合内 ⇒ E-14；开启值只做形态校验与 display_smoothed
    结构位（F-109），数值通路与哈希不消费（响应逐字节同，T-77②③）。
    errcol_choice ∈ ERRCOL_CHOICES。
    """
    factor_raw = pp.get('factor', 1)
    try:
        factor = float(factor_raw)
    except (TypeError, ValueError):
        factor = None
    factor_k = None
    if factor is not None and math.isfinite(factor):
        for k in C_PREPROCESS_FACTORS:
            if factor == k:
                factor_k = int(k)
                break
    if factor_k is None:
        raise SpecLoadError('E-14', f'合束因子 {factor_raw!r} 不在闭集 '
                                    f'{list(C_PREPROCESS_FACTORS)} 内（F-108①）',
                            reason='rebin_factor')
    ranges = pp.get('ranges') or []
    if not isinstance(ranges, list):
        raise SpecLoadError('E-14', 'preprocess.ranges 必须为数组（Q-33）',
                            reason='preprocess_range')
    if len(ranges) > C_MAX_EXCLUDE:
        raise SpecLoadError('E-07', f'preprocess.ranges 段数超过上限 '
                                    f'{C_MAX_EXCLUDE}（Q-2 逐类约束）',
                            reason='too_many_masks')
    norm_ranges = []
    for seg in ranges:
        try:
            lo, hi = float(seg[0]), float(seg[1])
        except (TypeError, ValueError, IndexError):
            raise SpecLoadError('E-14', f'preprocess.ranges 段 {seg!r} 必须为 '
                                        '[lo, hi] 且 lo<hi（Q-33）',
                                reason='preprocess_range')
        if not (math.isfinite(lo) and math.isfinite(hi) and lo < hi):
            raise SpecLoadError('E-14', f'preprocess.ranges 段 [{lo}, {hi}] 非法：'
                                        '须有限且 lo<hi（Q-33）',
                                reason='preprocess_range')
        norm_ranges.append([lo, hi])
    smooth = pp.get('smooth', False)
    smooth_px = 0
    if smooth is not False and smooth is not None:
        if isinstance(smooth, bool):
            raise SpecLoadError('E-14', f'preprocess.smooth={smooth!r} 非法：'
                                        '须为 false 或 [1, C_SMOOTH_BOX_MAX_PX] '
                                        '内整数（Q-33 闭集不含布尔 true）',
                                reason='bad_enum')
        try:
            smooth_num = float(smooth)
        except (TypeError, ValueError):
            smooth_num = None
        if smooth_num is None or smooth_num != int(smooth_num) \
                or not (1 <= smooth_num <= C_SMOOTH_BOX_MAX_PX):
            raise SpecLoadError('E-14', f'preprocess.smooth={smooth!r} 非法：'
                                        f'须为 false 或 [1, {C_SMOOTH_BOX_MAX_PX}] '
                                        '内整数（Q-33）', reason='bad_enum')
        smooth_px = int(smooth_num)
    choice = pp.get('errcol_choice', 'auto')
    if choice not in ERRCOL_CHOICES:
        raise SpecLoadError('E-14', f'preprocess.errcol_choice={choice!r} 非法：'
                                    f'取值闭集 {list(ERRCOL_CHOICES)}（Q-33）',
                            reason='bad_enum')
    return factor_k, norm_ranges, smooth_px, choice


# ─── F-106④：PreprocessedSpectrum（S1/S2/S3 的唯一数据入口） ──────────────

@dataclass
class PreprocessedSpectrum:
    """F-106④ 的字段集。掩膜是布尔标志位数组（③：不截断、不插值，等长）；
    factor>1 时本结构持**抽后网格**（lam_aa/flux/flux_err/mask_bool/fit_array
    全部为完整块的合束产物，flux_err=逐块 σ_bin），factor=1 时与
    LoadedSpectrum 逐点相同（恒等态，T-75①）。fit_array 是数值通路唯一入口
    （S1/S2/S3 的函数签名只接它）；display_smoothed 是 F-109 的结构位：
    箱式平滑只写不读（T-77① 的 AST 扫描域内它只以写入位置出现在本文件），
    不进任何哈希、不进导出件。口径声明（curve_kind/mag_system/weighting/
    lambda_frame/消光口径）不进本结构 —— 照 LoadedSpectrum 原样透传
    （F-106⑤：预处理不是第二条口径通路）。"""
    lam_aa: np.ndarray
    flux: np.ndarray
    flux_err: Optional[list]
    mask_bool: np.ndarray
    factor: int
    fit_array: np.ndarray
    display_smoothed: Optional[np.ndarray] = None
    hashes: dict = field(default_factory=dict)


def build_spectrum(ls, flux_corr, masks, factor=1, rebin=None, smooth_px=0):
    """LoadedSpectrum + 消光改正流量 + 掩膜并集 → PreprocessedSpectrum。

    factor>1 时 rebin=rebin_stage(...) 的产物替换本结构网格（S1/S2/S3 只看到
    抽后数据，F-106④ 唯一入口）；smooth_px>0 时另产出 display_smoothed
    （F-109①，与数值数组并列，绝不回流 fit_array）。"""
    lam = np.asarray(ls['lam_aa'], dtype=float)
    fc = np.asarray(flux_corr, dtype=float)
    if rebin is not None and factor > 1:
        lam_a = rebin['lam']
        flux_a = rebin['flux']
        err = [None if not math.isfinite(v) else float(v) for v in rebin['sigma']]
        mask_a = rebin['mask_bool']
        fit_a = rebin['flux']
    else:
        lam_a, flux_a, fit_a = lam, fc, fc
        err = ls['flux_err']
        mask_a = masks['mask_bool']
    disp = _boxcar_display(flux_a, int(smooth_px)) if smooth_px else None
    return PreprocessedSpectrum(
        lam_aa=lam_a,
        flux=flux_a,
        flux_err=err,
        mask_bool=mask_a,
        factor=int(factor),
        fit_array=fit_a,
        display_smoothed=disp,
        hashes={},
    )


def response_block(masks, mask_h, pp_h, factor, identity, verdict, accepted,
                   n_outlier, outlier_over, rebin=None, s2=None):
    """§4.2 preprocess{} 18 键（键集与键序的唯一载体在那行，恒等态下各键取
    默认值、禁止省略键）。rebin=rebin_stage 的 echo（factor>1 时给）覆盖合束
    四键；s2=dict(n_high_leverage, baseline_edge_spread,
    edge_spread_after_downgrade, suggested_edge_ranges) 仅在有 poly 拟合的
    API-3 请求上给（F-112 属 S2 poly 通路），S1/API-2 与无 poly 的 API-3 取
    null/[]（该通路不适用：S1 无 F-62 基线，§4.2 行原文）。smooth_px 恒 0
    （F-109⑤ 纯前端绘制；响应在开/关平滑下逐字节同，T-77②③）。"""
    reb = rebin or {}
    factor = int(factor)
    s2 = s2 or {}
    return {
        'factor': factor,
        'n_rebinned_pixels': int(reb.get('n_rebinned_pixels', 0)),
        'rebin_gain': (1 if factor == 1 else
                       (None if not reb else reb.get('rebin_gain'))),
        'dlam_after_aa': reb.get('dlam_after_aa'),
        'px_per_fwhm_after': reb.get('px_per_fwhm_after'),
        'mask_ranges': masks['merged'],
        'n_masked_pixels': masks['n_masked'],
        'mask_hash': mask_h,
        'preprocess_hash': pp_h,
        'identity': bool(identity),
        'errcol_verdict': verdict,
        'errcol_accepted': bool(accepted),
        'smooth_px': 0,
        'n_outlier_flagged': int(n_outlier),
        'n_high_leverage': (None if not s2 else s2.get('n_high_leverage')),
        'baseline_edge_spread': (None if not s2 else s2.get('baseline_edge_spread')),
        'edge_spread_after_downgrade': (None if not s2 else
                                        s2.get('edge_spread_after_downgrade')),
        'suggested_edge_ranges': (list(s2['suggested_edge_ranges'])
                                  if s2.get('suggested_edge_ranges') else []),
    }
