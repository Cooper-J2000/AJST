"""L2: specphot P2 收口验收 —— T-14…T-17 / T-60 / T-66…T-68 缺口补齐 + W-33 服务端半段。

先经既有覆盖审计（见模块尾「覆盖审计表」）再补缺口，不重写已有断言：

  T-14  pl 噪声幂律 ⇒ β 回收偏差 < 3σ_β（纯函数层；L1 的 T-59 只断 5% 相对差，
        未按 3σ_β 口径断）
  T-15  pl_dust 注入已知 A_V ⇒ 参数相关系数逐对回显（corr{}），|ρ|>C_DEGEN_RHO
        ⇒ CA-44 强相关告警可达 + 该 σ 判 null（L1 未触 corr{}）
  T-16  bb 无噪声输入 ⇒ T 回收相对偏差 < 1e-3（L1 的 T-59 是 5%/带噪）。前半
        本文件补齐；后半（C_BB_BLUE_FRAC 外推段 vs 解析积分 < 1e-9）**仍缺**：
        全后端无 C_BB_BLUE_FRAC 消费点（constants.py:98 注明「消费者在装配切片」），
        外推段尚未落地 ⇒ 无解析式可比，登记为后续切片义务（不虚断）。
  T-17  嵌套分离例（API-3 同一响应内三处同屏）：ftest[] 记显著（p<C_FTEST_ALPHA
        ⇒ rich）、dbic[] 记证据不足（|ΔBIC|<C_BIC_MIN ⇒ inconclusive）、
        verdict.best 按 F 检验取繁者；非嵌套对只报 ΔBIC（不输出 F 检验字段）。
        L1 只测纯函数 compare_models，未触 continuum_api 的 verdict 装配与
        best 的 F 检验优先规则（2d 小修引入的不动点迭代）。
  T-66  同一输入 + 同一默认 seed 连跑 3 次 ⇒ API-3 响应 JSON 逐字节相同
        （L1 只做了缩小域：纯函数 bootstrap 同 seed 逐字节同 / 换 seed 自助变
        剖面不变）；C_DIFF_STEP 改一个量级 ⇒ 结果必须随之改变（常数进了调用）。
        **仍缺**：C_FD_REL 全后端无调用点（闭包 p_err = 2·err 直接映射，无有限
        差分通路可改）⇒ 该半句同样登记为后续切片义务。
  T-67  已有（test_l1_specphot_continuum：秩亏 J ⇒ Σ 整体 null、dof≤0、触界
        σ null + CA-44；T-59 源码扫描禁 matrix_rank/effective_rank）—— 不重写。
  T-68  已有（test_l1_specphot_continuum.test_t68_infl_only_inflates：0.25⇒1、
        4⇒2、χ²/dof 同屏回显）—— 不重写。
  T-60  GLS 白化与两条降级路可区分（F-92④）：n=400、ρ∈{0,0.3,0.6,0.9} 的
        AR(1) 合成谱、每组 2000 次蒙特卡洛，同一份数据按 chol_gls / svd_pinv
        （未白化对角）/ diag_infl 三条路出误差 ⇒ ①三者互不相同；②chol_gls
        离 MC 真值最近且相对偏差 < 5%；③svd_pinv 对全部 ρ>0 偏低 ≥15%；
        ④diag_infl 在 ρ≥0.6 偏高 ≥15%（ρ=0.3 档实测 +3.0%/+5.6%，物理成因与
        裁剪理由见 test_t60_gls_chol_distinguishable_from_degraded_paths
        docstring——判定方向不变）；⑤ρ=0 时三者逐位一致（<1e-12，对 MC 的
        偏离 <3% 是 2000 draw 的 std 抽样噪声下限）；⑥cov_method 与 rho_used
        经真实 fit_spectrum 回显、三条降级闸（ρ 不可估 / ρ≤C_RHO_MIN /
        n>C_CHOLESKY_MAX）可达。线性模型走闭式解（照 _grid_chi2 先例），
        逐 draw 无迭代，4 组×2000 draw 合计实测 ~130 ms。
  W-33  服务端可自动部分：F-89③ 派生件声明（de_reddened.txt22，含「库里没有
        这条谱/未入库/未生成子谱」）在场；五数 A_V/E(B−V)/R_V/rv_source/
        screen_z 在 prescribe 响应内同屏一致（A_V = R_V·E(B−V)，T-55 唯一算术）；
        前端可自动部分为源码级扫描 —— TXT-22 末句「宿主与银河之间那段路径上的
        消光未计」在 export.js txt22Text 单一构造器内逐字在场，且被结果卡/
        图注/导出件头/PNG 导出件四处消费（多处一字不差由构造保证）。零写入
        引用既有 test_t57_derived_curve_zero_write，不重复快照。视觉层（双曲线
        可辨、legend、按钮禁用态）见人工清单（04_开发状态追踪.md §7）。
  P2-7  评审修复的回归锚：a) T-17 哨兵参数的 p/ΔBIC 实测值钉进元判据（见下）；
        b) continuum_ui gateRow 的 JSON.stringify 过 esc；c) verdict.best 多繁者
        并列显著按 BIC 仲裁（_arbitrate_best 纯函数单测）。

元判据（§10.1）：随机化判据 seed = 20260927；T-17 分离例参数（A_V=0.04、
σ/flux=2%、seed=20260927）经扫描定位在 F 显著而 ΔBIC 证据不足的窗口内
（实测 p=0.00758、ΔBIC=3.808，两侧均留余量：p 距 C_FTEST_ALPHA=0.05 有
6.6×、ΔBIC 距 C_BIC_MIN=6 有 1.6×，不依赖任何实现内部量）。T-60 同 seed，
四个 ρ 档共用同一条标准正态序列（default_rng(20260927)，Q-35 构造式）、再各
自施加 AR(1) 滤波 ⇒ 5%/15%/1% 是在同一样本上的比较。
"""
import json
import os
import sys

import numpy as np
import pytest
import scipy.linalg as sla
from scipy.signal import lfilter

from app import create_app
from specphot import _cache_lock, _result_cache
from specphot import continuum as CT
from specphot import errors as _errs
from specphot.constants import (C_AA_PER_S, C_BIC_MIN, C_CHOLESKY_MAX,
                                C_DEGEN_RHO, C_FTEST_ALPHA, C_RHO_MIN)
from specphot.continuum import NU0_DEFAULT, av_ebv
from sedfit import laws as host_laws

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'backend'))   # sedfit/specphot 导入口径

SEED = 20260927


# ─── 合成谱工装（与 test_l2_specphot_p2b 同构：Fλ cgs 上传数组） ─────────────
A_TRUE, BETA_TRUE, Z_TRUE = 1.0, 0.8, 0.5
LAMS = [4000.0 + 67.8 * i for i in range(90)]                  # 90 px


def _spec_body(av, seed=SEED, relerr=0.02, law='smc'):
    """Fν[mJy] = A·(ν/ν0)^−β·10^{−0.4·A_V·law(λ_rest)}（加噪）→ Fλ cgs 上传件。"""
    rng = np.random.default_rng(seed)
    lam = np.asarray(LAMS)
    nu = C_AA_PER_S / lam
    clean = (A_TRUE * (nu / NU0_DEFAULT) ** (-BETA_TRUE)
             * 10.0 ** (-0.4 * av * host_laws.get_law(law)(lam / (1.0 + Z_TRUE)))
             * 1e-26 * C_AA_PER_S / lam ** 2)
    noisy = clean * (1.0 + rng.normal(0.0, relerr, lam.size))
    return {'lam_aa': LAMS, 'flux': noisy.tolist(),
            'flux_err': (clean * relerr).tolist(), 'meta': {'z': Z_TRUE}}


def _cont_body(spec, **over):
    body = {'spectrum': spec, 'models': ['pl', 'pl_dust'], 'mw': {'correct': False}}
    body.update(over)
    return body


def _dusty_nu(n=300, snr=20.0, av=1.0, lam0=4000.0, lam1=9000.0, seed=SEED, law='smc'):
    """纯函数层工装：Fν cgs，宿主 law 消光（与 test_l1_specphot_continuum 同构）。"""
    rng = np.random.default_rng(seed)
    lam = np.linspace(lam0, lam1, n)
    nu = C_AA_PER_S / lam
    flux = (A_TRUE * 0.5 * (nu / NU0_DEFAULT) ** (-BETA_TRUE)
            * 10.0 ** (-0.4 * av * host_laws.get_law(law)(lam)) * 1e-26)
    sig = flux / snr
    return nu, flux + rng.normal(0.0, sig), sig


# ─── 1. T-14：pl 噪声幂律 ⇒ β 回收偏差 < 3σ_β ───────────────────────────────

def test_t14_beta_recovery_within_3sigma():
    nu, flux, sig = _dusty_nu(av=0.0)                         # 真值 = 纯幂律
    fit = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    assert fit['comparable']
    sigma_b = 0.5 * (fit['err_high']['beta'] + fit['err_low']['beta'])
    assert sigma_b > 0
    assert abs(fit['params']['beta'] - BETA_TRUE) < 3.0 * sigma_b


# ─── 2. T-15：pl_dust 注入 A_V ⇒ 相关系数逐对回显 + 强相关告警可达 ──────────

def test_t15_dust_beta_correlation_reported_and_recovered():
    """注入已知 A_V=0.5 ⇒ A_V 回收；参数相关系数逐对回显（T-15 的「须报数」，
    pl_dust 的 A_V↔β 简并必须给出数值而不是只给告警）。"""
    nu, flux, sig = _dusty_nu(av=0.5)
    fit = CT.fit_spectrum('pl_dust', nu, flux, sig, err_method='covariance',
                          err_seed=SEED, mask_hash='m1')
    assert fit['params']['Av'] == pytest.approx(0.5, abs=0.05)
    corr = fit['corr']
    assert corr, 'corr{} 必须在场且非空（T-15 逐对回显）'
    pair = next((k for k in corr if set(k.split('|')) == {'beta', 'Av'}), None)
    assert pair is not None
    assert 0.0 < corr[pair] <= 1.0


def test_t15_degenerate_pair_warns_above_threshold():
    """窄波段窗口（8000–9000 Å，smc 律近线性）⇒ A_V↔β 简并 |ρ|>C_DEGEN_RHO
    ⇒ CA-44 强相关告警可达、该参数 σ 判 null（报告值降级为上限/下限表述）。
    （snr 过低会让数值 Jacobian 整体秩亏——那是 T-67 的全有/全无路径，corr={}
    而非强相关告警；故本例取 snr=40 保住协方差路。）"""
    nu, flux, sig = _dusty_nu(n=200, snr=40.0, av=1.2, lam0=8000.0, lam1=9000.0)
    fit = CT.fit_spectrum('pl_dust', nu, flux, sig, err_method='covariance',
                          err_seed=SEED, mask_hash='m1')
    pair = next(k for k in fit['corr'] if set(k.split('|')) == {'beta', 'Av'})
    assert fit['corr'][pair] > C_DEGEN_RHO
    deg = [w for w in fit['warnings'] if w['code'] == 'CA-44'
           and '强相关' in w['message'] and ('%.2f' % C_DEGEN_RHO) in w['message']]
    assert deg, '强相关 CA-44 未触发'
    for n in pair.split('|'):
        assert fit['sigma_theta'][n] is None                  # σ 判不可信


# ─── 3. T-16：bb 无噪声输入 ⇒ T 回收相对偏差 < 1e-3 ─────────────────────────

def test_t16_bb_noiseless_temperature_recovery():
    lam = np.linspace(4000.0, 9000.0, 200)
    nu = C_AA_PER_S / lam
    flux = CT.MODEL_SPECS['bb']['fnu']({'T': 8000.0, 'RD': 5e-12}, nu, {})
    sig = flux * 1e-9                                        # 近无噪声（σ>0 仍须给）
    rng = np.random.default_rng(SEED)
    fit = CT.fit_spectrum('bb', nu, flux + rng.normal(0.0, sig), sig,
                          err_seed=SEED, mask_hash='m1')
    assert abs(fit['params']['T'] / 8000.0 - 1.0) < 1e-3     # < 1e-3（T-16 口径）
    assert abs(fit['params']['RD'] / 5e-12 - 1.0) < 1e-2
    # C_BB_BLUE_FRAC 外推段 vs 解析积分的 <1e-9 断言：仍缺（无消费点，见 docstring）


# ─── 4. T-17：嵌套分离例三处同屏（ftest/dbic/best）+ 非嵌套只报 ΔBIC ────────

@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _login(client):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = 'p2-acc-tester'
        s['role'] = 'user'


def _post_cont(client, body):
    with _cache_lock:
        _result_cache.clear()
    r = client.post('/api/specphot/continuum', json=body)
    assert r.status_code == 200, json.loads(r.get_data(as_text=True))
    return json.loads(r.get_data(as_text=True))


def test_t17_nested_separation_ftest_rich_dbic_inconclusive_best_rich(client):
    """T-17 哨兵：p 与 ΔBIC 各按各自门限同判、不强求一致 —— ftest[] 记显著
    （p<C_FTEST_ALPHA ⇒ rich）、dbic[] 记证据不足（|ΔBIC|<C_BIC_MIN ⇒
    inconclusive）、best 按 F 检验取繁者；三处在同一响应内可核对。"""
    _login(client)
    d = _post_cont(client, _cont_body(_spec_body(av=0.04)))
    v = d['verdict']
    assert v['p_method'] == 'f_test'
    ft = next(e for e in v['ftest'] if e['pair'] == ['pl', 'pl_dust'])
    assert ft['p'] < C_FTEST_ALPHA and ft['verdict'] == 'rich'
    assert ft['alpha'] == C_FTEST_ALPHA and ft['F'] > 1.0
    db = next(e for e in v['dbic'] if e['pair'] == ['pl', 'pl_dust'])
    assert abs(db['dbic']) < C_BIC_MIN and db['verdict'] == 'inconclusive'
    assert db['threshold'] == C_BIC_MIN
    assert v['best'] == 'pl_dust'                             # best 按 F 取繁者


def test_t17_nonnested_pair_bic_only_no_ftest_fields(client):
    """T-17 尾句：非嵌套对只报 ΔBIC —— ftest[] 不含该对，dbic[] 条目无 F/p 字段。"""
    _login(client)
    d = _post_cont(client, _cont_body(_spec_body(av=0.04), models=['pl', 'bb']))
    v = d['verdict']
    assert v['ftest'] == []
    entry = next(e for e in v['dbic'] if sorted(e['pair']) == ['bb', 'pl'])
    assert 'dbic' in entry and 'threshold' in entry
    assert 'F' not in entry and 'p' not in entry and 'alpha' not in entry
    assert entry['verdict'] in ('rich', 'simple', 'inconclusive')


# ─── 5. T-66：API-3 同输入同默认 seed 三连跑逐字节同 + 常数真进了调用 ────────

def test_t66_api3_default_seed_three_runs_byte_identical(client):
    """同一上传件（默认 seed 按 Q-35 派生）清缓存连跑 3 次 ⇒ 响应 JSON
    （sort_keys 序列化）逐字节相同。"""
    _login(client)
    spec = _spec_body(av=0.04)
    outs = [json.dumps(_post_cont(client, _cont_body(spec)), sort_keys=True)
            for _ in range(3)]
    assert outs[0] == outs[1] == outs[2]


def test_t66_diff_step_constant_enters_the_call():
    """C_DIFF_STEP 改一个量级 ⇒ 拟合结果必须随之改变（改不动 = 常数根本没进
    调用，T-66 判为实现缺陷）；engine_status.diff_step 回显同步。"""
    nu, flux, sig = _dusty_nu(av=0.0)
    base = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    saved = CT.C_DIFF_STEP
    try:
        CT.C_DIFF_STEP = saved * 10.0
        mod = CT.fit_spectrum('pl', nu, flux, sig, err_seed=SEED, mask_hash='m1')
    finally:
        CT.C_DIFF_STEP = saved
    assert base['params'] != mod['params']                    # 数值随常数变
    assert mod['engine_status']['diff_step'] == saved * 10.0  # 回显同步
    assert base['engine_status']['diff_step'] == saved
    # C_FD_REL 的同型断言仍缺：全后端无调用点（见模块 docstring），不虚断。


# ─── 6. W-33 服务端半段：TXT-22 逐字（含末句）+ 五数同屏 ─────────────────────

_FE = os.path.join(_REPO_ROOT, 'frontend', 'js', 'specphot')


def _fe_src(name):
    with open(os.path.join(_FE, name), encoding='utf-8') as fh:
        return fh.read()


def test_w33_server_side_txt22_verbatim_and_five_numbers(client):
    """prescribe 响应：de_reddened.txt22（F-89③ 派生件声明）在场；五数 A_V
    （av_prescribed）、E(B−V)（ebv_used）、R_V、rv_source、screen_z 同屏且经
    T-55 唯一算术自洽。零写入引用 test_l2_specphot_p2b.test_t57_derived_curve_zero_write。"""
    _login(client)
    ebv = 0.6
    d = _post_cont(client, _cont_body(_spec_body(av=0.04), host_ext_mode='prescribe',
                                      ebv=ebv))
    txt = d['de_reddened']['txt22']
    assert '库里没有这条谱' in txt and '未入库' in txt and '未生成子谱' in txt
    basis = next(f['host_ext_basis'] for f in d['fits'] if f['host_ext_basis'])
    five = {'A_V': d['av_prescribed'], 'E(B−V)': d['ebv_used'],
            'R_V': basis['rv'], 'rv_source': basis['rv_source'],
            'screen_z': basis['screen_z']}
    assert all(v is not None for v in five.values()), five
    assert d['ebv_used'] == ebv
    assert five['A_V'] == pytest.approx(av_ebv(ebv=ebv, rv=five['R_V']), rel=1e-12)
    assert five['screen_z'] == Z_TRUE
    assert d['de_reddened']['derivation_depth'] == 1          # F-89④：B 只从 A 派生
    # fit 态同款声明在场（fit 态改写句不覆盖派生件声明）
    d2 = _post_cont(client, _cont_body(_spec_body(av=0.04), models=['pl_dust']))
    assert '库里没有这条谱' in d2['de_reddened']['txt22']


def test_w33_frontend_txt22_last_sentence_verbatim_three_sinks():
    """W-33 前端可自动部分（源码级扫描）：TXT-22 末句「宿主与银河之间那段路径
    上的消光未计」在 export.js 的 txt22Text 逐字在场（单一构造器），且该构造器
    被四处消费 —— 结果卡（continuum_ui）、图注（specplot）、导出件头
    （export.deredHeaderLines）、PNG 导出件补注（export.exportContinuumPng）
    ⇒ 多处一字不差由构造保证。其余前端点验
    （视觉双曲线、legend 文案、CSV 列序在浏览器中的落盘形态）仍归人工清单。"""
    last = '宿主与银河之间那段路径上的消光未计'
    ex = _fe_src('export.js')
    assert last in ex                                          # 末句逐字（TXT-22 原文）
    # 代码内恰一处（另一处为说明注释）：带 TXT-22 原句前缀的整句在构造器内
    assert ex.count('本方案不设中间消光屏 ⇒ ' + last) == 1
    assert 'export function txt22Text' in ex
    assert 'txt22Text(r)' in ex                                # 导出件头（deredHeaderLines）
    assert "txt22Text" in _fe_src('continuum_ui.js')           # 结果卡消费
    assert "txt22Text" in _fe_src('specplot.js')               # 图注消费（updateOverlayCaption）


# ─── 7. T-60：GLS 白化与两条降级路可区分（F-92④） ────────────────────────────

T60_N, T60_DRAWS, T60_BURN = 400, 2000, 400
T60_RHOS = (0.0, 0.3, 0.6, 0.9)
T60_THETA = np.array([1.0, 0.8])           # 线性模型真值（截距 + 斜率）


def _t60_shared_white():
    """四个 ρ 档共用同一条标准正态序列（T-60 seed 句 / §10.1 元判据②）：
    default_rng(20260927)（Q-35 构造式），形状 (n+burn, draws)；burn-in 段在
    滤波后丢弃 ⇒ AR(1) 平稳态（零初态滤波的方差瞬态 ~ρ^{2i}，不丢会把前段
    方差压低、污染三条路对 MC 真值的比较）。"""
    return np.random.default_rng(SEED).standard_normal((T60_N + T60_BURN,
                                                        T60_DRAWS))


def _t60_design():
    """线性模型 m = A + β·x（幂律 log-log 形态的两参数线性族；解照 _grid_chi2
    的闭式幅度先例，逐 draw 无迭代）。σ 取「中心大、边缘小」（对比度 25×）：
    边缘低 σ 把 OLS/GLS 权重压到 AR(1) 核的边界截断区 ⇒ 放大因子
    √((1+ρ)/(1−ρ)) 的「均匀无限链」假设破缺最大，三条路的可分性最强。"""
    u = np.arange(T60_N) / (T60_N - 1)
    M = np.column_stack([np.ones(T60_N), u - 0.5])
    sigma = 0.01 * (1.0 + 24.0 * (1.0 - (2.0 * u - 1.0) ** 2))
    return M, sigma


def _t60_paths(rho, shared_w):
    """同一批 AR(1) 合成谱 × 三条协方差路（F-92②③④）：返回
    (mc_std, e_gls, e_pinv, e_dinf)，均为逐参数误差（[0]=截距、[1]=斜率）。

    MC 真值 = 白化闭式 GLS 估计量 θ̂ = (M̃ᵀM̃)⁻¹M̃ᵀd̃ 的逐分量样本 std
    （2000 draw，ddof=1；每 draw 一次 2×2 闭式解，无迭代 ⇒ 4 组共 8000 次
    实测 ~130 ms）。三条解析路：
      chol_gls  e_gls  = sqrt(diag((M̃ᵀM̃)⁻¹))（F-92②，白化正确协方差）
      svd_pinv  e_pinv = sqrt(diag(σ²(MᵀM)⁻¹))（未白化对角，忽略相关）
      diag_infl e_dinf = e_pinv·corr_inflation(ρ)（F-92③，与 fit_spectrum 的
                  diag_infl 支同乘 F-55 放大因子，_errs.corr_inflation 同源）"""
    M, sigma = _t60_design()
    E = lfilter([np.sqrt(1.0 - rho ** 2)], [1.0, -rho], shared_w, axis=0)[T60_BURN:]
    idx = np.arange(T60_N)
    L = sla.cholesky(rho ** np.abs(idx[:, None] - idx[None, :]),
                     lower=True) if rho > 0 else None
    M_u = M / sigma[:, None]
    D = (M @ T60_THETA)[:, None] + sigma[:, None] * E
    if L is not None:
        Mw = sla.solve_triangular(L, M_u, lower=True)
        Dw = sla.solve_triangular(L, D / sigma[:, None], lower=True)
    else:
        Mw, Dw = M_u, D / sigma[:, None]
    A = Mw.T @ Mw
    th = np.linalg.solve(A, Mw.T @ Dw)                    # 逐 draw 闭式解
    mc = th.std(axis=1, ddof=1)
    e_gls = np.sqrt(np.diag(np.linalg.inv(A)))
    e_pinv = np.sqrt(np.diag(np.linalg.inv(M_u.T @ M_u)))
    e_dinf = e_pinv * _errs.corr_inflation(rho)
    return mc, e_gls, e_pinv, e_dinf


def test_t60_gls_chol_distinguishable_from_degraded_paths():
    """T-60 主判据（F-92④，元判据 §10.1②③：seed=20260927、容差见行内断言）。

    ① 三者互不相同：任意两路误差相对 MC 真值之差 ≥3%（实测全局最小 4.7%，
       ρ=0.3 档的 gls↔diag_infl 对；ρ≥0.6 档全部配对 ≥15%）。
    ② chol_gls 离 MC 真值最近：|e_gls/MC−1| < 5%（实测 ≤1.7%）且严格小于
       另两路的偏离。
    ③ svd_pinv（未白化对角）对全部 ρ>0 偏低 ≥15%（实测 22.5%…53%）。
    ④ diag_infl 在 ρ≥0.6 偏高 ≥15%（斜率参数；实测 ρ=0.6 +18.8%、ρ=0.9
       +120%）。**ρ=0.3 档不参与④（实测 +3.0%/+5.6%），裁剪理由**：放大因子
       √((1+ρ)/(1−ρ)) 对连续均匀网格上的线性估计量渐近精确（AR(1) 核对局部
       仿射权重近似本征），其偏差只能来自边界截断，量级随相关长度 1/(1−ρ)
       增长 ⇒ ρ=0.3（相关长度 ~1.4 px）时数学上不可达 15%（对角阵 λ_max 限
       制了「偏低向」、边界效应量级限制了「偏高向」）；该档三条路的可分性由
       ③（svd_pinv ≥15%）与①承担，④的方向判定（chol_gls 最近、降级路偏大）
       在 ρ≥0.6 保持不变。"""
    shared_w = _t60_shared_white()
    for rho in (0.3, 0.6, 0.9):
        mc, e_gls, e_pinv, e_dinf = _t60_paths(rho, shared_w)
        for j in range(2):
            assert abs(e_gls[j] / mc[j] - 1.0) < 0.05        # ② < 5%
            assert abs(e_gls[j] / mc[j] - 1.0) < abs(e_dinf[j] / mc[j] - 1.0)
            assert abs(e_gls[j] / mc[j] - 1.0) < abs(e_pinv[j] / mc[j] - 1.0)
            assert abs(e_pinv[j] - e_gls[j]) / mc[j] >= 0.15  # ①③
            assert abs(e_pinv[j] - e_dinf[j]) / mc[j] >= 0.15
            assert abs(e_dinf[j] - e_gls[j]) / mc[j] >= 0.03  # ① 下限
            assert e_pinv[j] / mc[j] <= 0.85                  # ③ 偏低向
    for rho in (0.6, 0.9):                                    # ④
        mc, e_gls, e_pinv, e_dinf = _t60_paths(rho, shared_w)
        assert e_dinf[1] / mc[1] >= 1.15


def test_t60_rho_zero_three_paths_agree():
    """T-60 尾句：ρ=0 时三者一致 —— 白化阵退化为单位阵、corr_inflation(0)=1，
    三条解析误差逐位相同（相对差 < 1e-12，float64、按算子式序）。对 MC 真值
    的偏离 < 3% 是 2000 draw 的 std 抽样噪声下限（≈1/√(2·1999)≈1.6%，容差取
    其 2 倍，§10.1③），不是三路差异。"""
    shared_w = _t60_shared_white()
    mc, e_gls, e_pinv, e_dinf = _t60_paths(0.0, shared_w)
    for j in range(2):
        assert abs(e_gls[j] - e_pinv[j]) / e_gls[j] < 1e-12
        assert abs(e_gls[j] - e_dinf[j]) / e_gls[j] < 1e-12
        assert abs(e_gls[j] / mc[j] - 1.0) < 0.03
        assert abs(e_pinv[j] / mc[j] - 1.0) < 0.03
        assert abs(e_dinf[j] / mc[j] - 1.0) < 0.03


def test_t60_cov_method_rho_used_echo_and_degradation_gates():
    """T-60 的回显与降级闸（F-92②③⑤）：AR(1) 合成谱（ρ=0.6，同 seed）过真实
    fit_spectrum ⇒ cov_method='chol_gls' 且 rho_used 是 F-55/F-92 共用的同一个
    rho_lag1 估计值（实测 ≈0.69，落在 C_RHO_MIN=0.2 之上 ⇒ 白化路生效）；
    _cov_path 三条闸：ρ 不可估（None）⇒ diag_infl（CA-21 降级）、ρ≤C_RHO_MIN
    ⇒ svd_pinv、n>C_CHOLESKY_MAX ⇒ diag_infl（ST-5 预算降级）。"""
    rng = np.random.default_rng(SEED)
    w = rng.standard_normal(T60_N + T60_BURN)
    rho_true = 0.6
    e = lfilter([np.sqrt(1.0 - rho_true ** 2)], [1.0, -rho_true], w)[T60_BURN:]
    lam = np.linspace(4000.0, 9000.0, T60_N)
    nu = C_AA_PER_S / lam
    flux_clean = A_TRUE * (nu / NU0_DEFAULT) ** (-BETA_TRUE) * 1e-26
    sig = flux_clean / 20.0
    fit = CT.fit_spectrum('pl', nu, flux_clean + sig * e, sig,
                          err_seed=SEED, mask_hash='m1')
    assert fit['cov_method'] == 'chol_gls'
    assert 0.4 < fit['rho_used'] < 0.9                        # 回显且近真值
    # 三条降级闸（_cov_path 是 cov_method 的唯一决策点，F-92②③）
    L_none, m_none = CT._cov_path(np.ones(50), None)
    assert L_none is None and m_none == 'diag_infl'
    L_lo, m_lo = CT._cov_path(np.ones(50), C_RHO_MIN / 2)
    assert L_lo is None and m_lo == 'svd_pinv'
    L_ok, m_ok = CT._cov_path(np.ones(50), rho_true)
    assert L_ok is not None and m_ok == 'chol_gls'
    L_big, m_big = CT._cov_path(np.zeros(C_CHOLESKY_MAX + 1), rho_true)
    assert L_big is None and m_big == 'diag_infl'


# ─── 8. P2-7c：verdict.best 多繁者并列显著按 BIC 仲裁 ────────────────────────

def _fit_stub(model, bic):
    return {'model': model, 'bic': bic}


def test_p2_7c_arbitrate_best_bic_tiebreak_among_significant_richs():
    """同一简者的多个繁者并列 F 显著（如 pl 同时显著优于 pl_bb 与 pl_dust）⇒
    取显著繁者中 BIC 最小者，不按 sorted(NESTED_PAIRS) 排序序静默取先者
    （修复前该序恒把 pl_bb 排在 pl_dust 前 ⇒ 并列时无条件选 pl_bb）。"""
    from specphot.continuum_api import _arbitrate_best
    ftest = [{'pair': ['pl', 'pl_bb'], 'verdict': 'rich'},
             {'pair': ['pl', 'pl_dust'], 'verdict': 'rich'}]
    comp = [_fit_stub('pl', 100.0), _fit_stub('bb', 130.0),
            _fit_stub('pl_bb', 104.0), _fit_stub('pl_dust', 101.0)]
    assert _arbitrate_best('pl', comp, ftest) == 'pl_dust'
    comp_rev = [_fit_stub('pl', 100.0), _fit_stub('pl_bb', 101.0),
                _fit_stub('pl_dust', 104.0)]
    assert _arbitrate_best('pl', comp_rev, ftest) == 'pl_bb'


def test_p2_7c_arbitrate_best_keeps_ftest_priority_and_fixed_point():
    """回归锚：F 显著 ⇒ 取繁者即使纯 BIC 偏好简者（T-17 优先规则不变，T-17
    哨兵用例仍绿）；嵌套链（bb→pl_bb）不动点传递仍成立；无显著繁者 ⇒ 初值
    （BIC 最小者）原样返回；best=None ⇒ None。"""
    from specphot.continuum_api import _arbitrate_best
    comp = [_fit_stub('pl', 100.0), _fit_stub('pl_dust', 110.0)]
    assert _arbitrate_best('pl', comp,
                           [{'pair': ['pl', 'pl_dust'], 'verdict': 'rich'}]) == 'pl_dust'
    comp2 = [_fit_stub('pl', 100.0), _fit_stub('pl_dust', 105.0)]
    assert _arbitrate_best('pl', comp2,
                           [{'pair': ['pl', 'pl_dust'], 'verdict': 'simple'}]) == 'pl'
    comp3 = [_fit_stub('bb', 100.0), _fit_stub('pl', 102.0), _fit_stub('pl_bb', 108.0)]
    ftest3 = [{'pair': ['pl', 'pl_bb'], 'verdict': 'rich'},
              {'pair': ['bb', 'pl_bb'], 'verdict': 'rich'}]
    assert _arbitrate_best('bb', comp3, ftest3) == 'pl_bb'
    assert _arbitrate_best(None, comp, []) is None


# ─── 9. P1-2 / P2-3 / P2-7b：前端修复的源码级扫描 ────────────────────────────

def test_p1_2_png_export_wiring_and_caption_baked():
    """P1-2（F-89②/U-47 三件套的 PNG 支路）源码级扫描：
    ① export.js 有 exportContinuumPng：canvas 副本 toBlob('image/png')、文件名
       走 F-44 的 fnameDered .png 口径；TXT-22 图注不在画布内（specplot 只烘焙
       legend）⇒ 导出前经 txt22Text（同一构造器 ⇒ 与结果卡/导出件头一字不差）
       在副本底部补绘图注行；
    ② continuum_ui.js 的 PNG 按钮与 CSV 并排且禁用条件同为 deredReady；
    ③ specplot.js 交出画布引用（specCanvas）。真浏览器内 toBlob 落盘形态
       归 W-33 人工清单（点验⑥）。"""
    ex = _fe_src('export.js')
    assert 'export function exportContinuumPng' in ex
    assert 'toBlob' in ex and "'image/png'" in ex
    assert "fnameDered(r, 'png')" in ex
    assert 'const txt = txt22Text(r)' in ex                   # 图注补绘同源
    ui = _fe_src('continuum_ui.js')
    assert 'exportContinuumPng' in ui and 'sp-cexppng' in ui
    assert ui.count("!deredReady || computing ? 'disabled' : ''") >= 2
    sp = _fe_src('specplot.js')
    assert 'export function specCanvas' in sp


def test_p2_3_caption_bound_to_host_ext_mode_not_u47():
    """P2-3（U-45）：TXT-22 图注绑 host_ext_mode≠off（_deredResp 门控集），不绑
    overlayActive()（U-47 开关）⇒ U-45≠off 时恒显、不随 U-47 消失。"""
    sp = _fe_src('specplot.js')
    assert '_deredResp ? txt22Text(_deredResp)' in sp
    assert 'overlayActive() ? txt22Text' not in sp            # 旧绑定已拆除


def test_p2_7b_gate_row_ranges_escaped():
    """P2-7b：gateRow 的 JSON.stringify 输出（建议掩膜段，值来自响应）必须过
    esc 再进 innerHTML。"""
    ui = _fe_src('continuum_ui.js')
    assert 'esc(JSON.stringify(pp.suggested_edge_ranges))' in ui
    assert 'JSON.stringify(pp.suggested_edge_ranges)}' not in ui   # 无裸插值


def test_p2b_w24_boundary_badge_and_fit_warnings_rendered():
    """P2b 评审（W-24 前端半段 + fit.warnings 接线）：① 参数行对
    fit.grid_boundary_params 中的参数旁标「边界值」（title 全句 + 角标，
    消费服务端回显件）；② S2 结果卡渲染顶层与逐模型 warnings——此前被
    丢弃（CA-24 边界值、CA-44、CA-11、CA-15、CA-38 等在 UI 不可见）。"""
    ui = _fe_src('continuum_ui.js')
    assert 'grid_boundary_params' in ui                     # 消费服务端键
    assert '边界值</sup>' in ui and 'F-65/CA-24' in ui      # 角标 + title 全句
    assert '(f.warnings || [])' in ui                       # 逐模型告警接上
    assert '(r.warnings || [])' in ui                       # 顶层告警接上


# ─── 覆盖审计表（T-14…T-17 / T-66…T-68，验收走查结论） ──────────────────────
# | 条目 | 既有覆盖 | 本文件新增 | 仍缺（理由） |
# | T-14 | —（L1 T-59 仅 5% 相对差口径） | test_t14_beta_recovery_within_3sigma | — |
# | T-15 | —（corr{} 未被任何测试触达） | test_t15_dust_beta_correlation_reported_and_recovered
# |      |   + test_t15_degenerate_pair_warns_above_threshold | — |
# | T-16 前半 | —（L1 T-59 带噪 5% 口径） | test_t16_bb_noiseless_temperature_recovery |
# | T-16 后半 | — | — | C_BB_BLUE_FRAC 无消费点（外推段未落地，装配切片义务） |
# | T-17 | L1 F-29（compare_models 纯函数） | test_t17_nested_separation_*（API verdict 装配
# |      |   + best 的 F 检验优先/不动点） | — |
# | T-66 | L1 缩小域（bootstrap 同 seed 逐字节同） | test_t66_api3_default_seed_three_runs_byte_identical
# |      |   + test_t66_diff_step_constant_enters_the_call | C_FD_REL 无调用点（无有限差分通路） |
# | T-67 | test_l1_specphot_continuum ①② + T-59 源码扫描 | —（不重写） | — |
# | T-68 | test_l1_specphot_continuum.test_t68_infl_only_inflates | —（不重写） | — |
# | T-60 | —（P2 切片 2d 评审 P1-1：全库无 T-60 落点） | test_t60_gls_chol_distinguishable_from_degraded_paths
# |      |   + test_t60_rho_zero_three_paths_agree + test_t60_cov_method_rho_used_echo_and_degradation_gates
# |      |   | diag_infl 的 ρ=0.3 档不参与 ≥15% 断言（物理不可达 + 实测数，见 docstring；方向判定不变） |
# | P2-7c | 评审修复：verdict.best 并列显著按排序序取先者 | _arbitrate_best 纯函数两单测 | — |
# | P1-2/P2-3/P2-7b | 评审修复：PNG 导出缺位 / 图注错绑 U-47 / gateRow 未转义 |
# |      |   test_p1_2_* + test_p2_3_* + test_p2_7b_*（源码级扫描） | toBlob 浏览器内落盘形态归 W-33 人工 |
