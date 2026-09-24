"""L1: 闭包关系系数表 + α–β 诊断（sedfit/closure.py + closure_relations.json）。

约定 Fν ∝ t^(−α) ν^(−β)；每条关系 α_pred = a·β + b。

本文件断言分两类，**来源性质不同，不要混**：

  [理论] 可由标准闭包关系直接推出，是独立的物理真值：
      ISM  慢冷 ν_m<ν<ν_c : α = (3β)/2        → a=1.5, b=0
      ISM  慢冷 ν>ν_c     : α = (3β−1)/2      → a=1.5, b=−0.5
      ISM  快冷 ν>ν_m     : α = (3β−1)/2      → a=1.5, b=−0.5
      Wind 慢冷 ν_m<ν<ν_c : α = (3β+1)/2      → a=1.5, b=+0.5
      喷流拐折后 ν>ν_c     : α = 2β            → a=2.0, b=0
  代入 β=p/2 或 (p−1)/2 即得教科书形式（Sari+1998；Granot & Sari 2002 Table 1）。

  [理论·已核原文] 快冷 ν_c<ν<ν_m 段：β 固定 1/2，α = 1/4（ISM 与 wind 同值）。
      推导（Sari, Piran & Narayan 1998, ApJL 497, L17；式/图号均属该文）：
        式 (7)：该段 Fν ∝ (ν/ν_c)^(−1/2)·Fν,max
        式 (11)：绝热 ISM 下 ν_c ∝ t^(−1/2)、Fν,max = 常数
        ⟹ Fν ∝ t^(−1/4) ⟹ α = 1/4，β = 1/2
      图 2a 对该段（图 1a 的 C 段）直接标注 t^(−1/4)［完全辐射为 t^(−4/7)］，
      而 ν<ν_c 段标注 t^(+1/6) —— 两段极易混，不要互相套用。
      wind：谱形不变，改用风介质标度 ν_c ∝ t^(+1/2)、Fν,max ∝ t^(−1/2)
      （Gao, Lei, Zou, Wu & Zhang 2013, NewAR 57, 141 = arXiv:1310.2181 正文；
        另见 Zhang 2019《The Physics of Gamma-Ray Bursts》第 8 章）⟹ 同样 α = 1/4。
      Gao+2013 各表中该段亦记为 β=1/2、α=1/4、α=β/2。
      注：该段 β 恒为 1/2，故 a、b 两条的"斜率"无物理含义，
          唯一有意义的量是 β=1/2 处的取值 α_pred = 1/4。
"""
import json

import pytest

from sedfit import closure

EXPECTED_IDS = {
    'ism_sc_mc', 'ism_sc_above_c', 'ism_fc_above_m', 'ism_fc_cm',
    'wind_sc_mc', 'wind_sc_above_c', 'wind_fc_above_m', 'wind_fc_cm',
    'jetbreak_above_c', 'jetbreak_mc',
    'ism_sc_mc_inject', 'ism_sc_above_c_inject', 'ism_fc_cm_inject',
    'ism_fc_above_m_inject', 'wind_sc_mc_inject', 'wind_sc_above_c_inject',
    'wind_fc_cm_inject', 'wind_fc_above_m_inject',
}

REQUIRED_FIELDS = {'id', 'medium', 'regime', 'injection', 'segment',
                   'label', 'alpha', 'beta', 'p', 'ref'}

# [理论] 已由标准闭包关系独立推出
ALPHA_FROM_THEORY = {
    'ism_sc_mc':        (1.5, 0.0),
    'ism_sc_above_c':   (1.5, -0.5),
    'ism_fc_above_m':   (1.5, -0.5),
    'wind_sc_mc':       (1.5, 0.5),
    'jetbreak_above_c': (2.0, 0.0),
}

# [理论·已核原文] 见模块 docstring；两条在 β=1/2 处均给出 α = 1/4
ALPHA_LOCKED_CURRENT = {
    'ism_fc_cm':   (0.5, 0.0),
    'wind_fc_cm':  (-0.5, 0.5),
}


@pytest.fixture(scope='module')
def table():
    return {r['id']: r for r in closure.load_table()['relations']}


# ── 表结构 ─────────────────────────────────────────────────────────────

def test_relation_set_is_the_expected_one(table):
    assert set(table) == EXPECTED_IDS
    assert len(table) == 18


@pytest.mark.parametrize('rid', sorted(EXPECTED_IDS))
def test_every_relation_has_the_required_fields(table, rid):
    r = table[rid]
    assert REQUIRED_FIELDS <= set(r), f"{rid} 缺字段 {REQUIRED_FIELDS - set(r)}"
    assert isinstance(r['label'], str) and r['label']
    assert isinstance(r['ref'], str) and r['ref']
    assert isinstance(r['medium'], str) and r['medium']


@pytest.mark.parametrize('rid', sorted(EXPECTED_IDS))
def test_alpha_is_either_ab_or_injection_form(table, rid):
    a = table[rid]['alpha']
    if table[rid].get('injection') is True:
        assert {'a0', 'qa', 'b0', 'qb'} <= set(a), f"{rid} 注入条目结构不对: {a}"
    else:
        assert {'a', 'b'} <= set(a), f"{rid} 非注入条目结构不对: {a}"


# ── 系数值 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('rid,ab', sorted(ALPHA_FROM_THEORY.items()))
def test_alpha_coefficients_match_theory(table, rid, ab):
    assert (table[rid]['alpha']['a'], table[rid]['alpha']['b']) == ab


@pytest.mark.parametrize('rid,ab', sorted(ALPHA_LOCKED_CURRENT.items()))
def test_fast_cooling_cm_coefficients(table, rid, ab):
    """系数值已核原文（推导与出处见模块 docstring）"""
    assert (table[rid]['alpha']['a'], table[rid]['alpha']['b']) == ab
    assert table[rid]['beta']['fixed'] == 0.5


@pytest.mark.parametrize('rid', sorted(ALPHA_LOCKED_CURRENT))
def test_fast_cooling_cm_predicts_alpha_one_quarter(table, rid):
    """该段唯一有物理含义的量：α_pred = 1/4（SPN98 式 7 + 式 11、图 2a）"""
    r = table[rid]
    a, b = r['alpha']['a'], r['alpha']['b']
    assert a * r['beta']['fixed'] + b == pytest.approx(0.25)


def test_injection_entries_are_the_expected_eight(table):
    inj = {rid for rid, r in table.items() if r.get('injection') is True}
    assert len(inj) == 8
    assert all(rid.endswith('_inject') for rid in inj)


# ── p(β) 映射 ──────────────────────────────────────────────────────────

def test_p_from_expr():
    assert closure._p_from_expr('2*beta+1', 0.5) == pytest.approx(2.0)
    assert closure._p_from_expr('2*beta', 0.6) == pytest.approx(1.2)


# ── diagnose 行为 ──────────────────────────────────────────────────────

def test_diagnose_ranks_the_true_relation_first():
    """β=0.5 时 ism_sc_mc 预测 α=0.75，给正好落在它上面的观测值"""
    out = closure.diagnose(0.75, 0.05, 0.5, 0.01)
    assert out['ranking'][0]['id'] == 'ism_sc_mc'
    assert out['ranking'][0]['sigma_dev'] == pytest.approx(0.0, abs=1e-12)
    assert out['best']['id'] == 'ism_sc_mc'


def test_diagnose_predicts_the_relation_value():
    out = closure.diagnose(0.75, 0.05, 0.5, 0.01)
    assert out['ranking'][0]['alpha_pred'] == pytest.approx(0.75)


def test_diagnose_without_errors_gives_point_estimate_only():
    """分母为 0 → sigma_dev = null + note，另给 abs_dev"""
    top = closure.diagnose(0.75, 0, 0.5, 0)['ranking'][0]
    assert top['sigma_dev'] is None
    assert top['note'] == '未提供误差，仅给点估计偏差'
    assert top['abs_dev'] == pytest.approx(0.0)


def test_injection_entries_excluded_without_q():
    out = closure.diagnose(0.75, 0.05, 0.5, 0.01)
    assert len(out['ranking']) == 10
    assert all(r.get('injection') is not True for r in out['ranking'])


def test_injection_entries_included_with_q():
    """0 ≤ q < 1 时注入关系参与排名"""
    out = closure.diagnose(0.75, 0.05, 0.5, 0.01, q=0.5)
    assert len(out['ranking']) == 18
    assert any(r.get('injection') is True for r in out['ranking'])


@pytest.mark.parametrize('bad_q', [-0.1, 1.0, 1.5])
def test_q_out_of_range_is_rejected(bad_q):
    with pytest.raises(ValueError):
        closure.diagnose(0.75, 0.05, 0.5, 0.01, q=bad_q)


def test_diagnose_output_is_json_safe():
    """返回值必须不含 NaN/Infinity（Flask jsonify 会输出非法 JSON）"""
    for q in (None, 0.0, 0.5):
        for args in [(0.75, 0.05, 0.5, 0.01), (0.0, 0, 0.0, 0), (5.0, None, 2.0, None)]:
            s = json.dumps(closure.diagnose(*args, q=q), allow_nan=False)
            assert 'NaN' not in s and 'Infinity' not in s
