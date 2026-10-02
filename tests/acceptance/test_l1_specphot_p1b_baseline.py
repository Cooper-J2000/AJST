"""L1: specphot **P1 响应基线固化**（T-75① 的比较基准，02b §3.14 / F-113③）。

本文件在 P1b 动工**之前**把当前 P1 实现对一组固定 golden 请求的 API-2 响应
逐字节存档（tests/acceptance/golden_specphot_p1/*.json，由 tests/acceptance/regen_specphot_golden.py 在期次收口时对**当期代码**实跑重建（当前快照 = P1b 收口态，2026-09-30；P2 数值通路动工前不得再重建）），
P1b 改动落地后现场响应必须与存档在**比较域内**逐字节一致 —— 这就是 T-75①
的「恒等态数值与既有键与不含 §3.14 的实现逐字节相同」的可执行形式。

golden 请求组（API-2，四件）：
  1. upload_direct   上传件直连（λ 4000–4100 Å，避开全部掩膜表覆盖区）
  2. cat83_anchored  库内 spectrum_id=83（5976–9197 Å）+ 手加锚点 anchored
  3. cat112          库内 spectrum_id=112（4723–6743 Å）
  4. cat116          库内 spectrum_id=116（2952–7358 Å）

**比较域裁量（本文件的核心登记项，P1b 汇报同文复述）**：

  - F-113③ 本来就把「§3.14 新增且恒在场」的 `preprocess{}` 子树与 TXT-27 恒在
    场项排除在恒等比较域之外 —— 库内三例的 `preprocess{}` 整体不入比较域。
  - P1b 使 F-107① 的四类掩膜并集**首次真正生效**（P0 起常量表已在
    constants.py，但无消费者；P1b 的 preprocess.py 落地后 C_MASK_ABS_TABLE /
    C_MASK_EMIS_TABLE 按 F-72①② 默认剔除并入掩膜）⇒ 83/112/116 的波长域撞表，
    谱级 `mask_hash`（布尔数组游程编码的哈希，随自动表必然改变）与行级
    `n_masked_pixels`（按通带覆盖计数，golden 波段刻意避开表区故数值不变，但
    键属同一新行为族）列入比较域排除。理由：这两类键的值变化就是 F-107① 四类
    并集这一 **P1b 新行为本身**，把它们留在比较域等于要求"P1b 不实现 F-107①"。
  - 同理排除 `n_outlier_flagged` 的**回显面**：warnings[] 中 reason ∈
    {outlier_flagged, outlier_frac_over} 的条目（F-111② 明令候选"摆在告警区"，
    它是 preprocess.n_outlier_flagged 的同一回显面；83/112 的真实谱有尖峰特征，
    P1b 后被标记候选属预期行为，flagged ≠ removed、掩膜不变，T-79③ 另测）。
    其余 warnings[] 条目（含 CA-14 等既有码）仍逐条比较。
  - 除上述键/条 + `preprocess{}` 子树外，其余全部键（含 results 全表、warnings[]
    逐条、meta/mw/s_anchor/read_stats 等）逐字节一致。
  - upload_direct 的波长域完全避开 C_MASK_ABS/C_MASK_EMIS 覆盖区（5537–5617 /
    6260–6403 / 6867–6884 / 7550–7700 Å 及宿主 tell 组），自动掩膜零命中、无
    离群候选 ⇒ 该例取**全响应严格逐字节**（含 preprocess{} 与 mask_hash），不
    给任何豁免。

库内三例的波段用 custom_curves 刻意选在自动表覆盖区**之外**（83:6000–6200、
112:4800–5400、116:3000–3400 Å）⇒ 通带积分的像素集合不变 ⇒ mag 等全部数值
与 P1 逐字节相同；差异只可能出现在被豁免的键里（测试失败即说明出现了预料外
的键变化，应当修实现而不是扩豁免）。

测试全只读：golden 存档为静态文件随本测试入库，测试进程只读不写
（T-24 快照域 catadata/ + backend/ 与本目录无关）。
"""
import json
import os

import pytest

from app import create_app

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOLDEN_DIR = os.path.join(_REPO_ROOT, 'tests', 'acceptance', 'golden_specphot_p1')

# n_outlier_flagged（F-111②）的告警区回显面：比较域豁免（模块 docstring 裁量③）
WARN_EXCL = {'outlier_flagged', 'outlier_frac_over'}

# golden 上传语料：12 点、λ∈[4000,4100]（避开全部掩膜表）、代理 σ 可估的非平谱
LAMS = [4000.0 + 10.0 * i for i in range(12)]
F0 = 1e-14
NOISE = [0.5, 1.3, 0.8, 1.6, 0.9, 1.4, 0.7, 1.2, 1.0, 0.6, 1.1, 0.75]

CC_G = {'band': 'sp-golden-r', 'lam_aa': [3990.0, 4045.0, 4100.0],
        't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'}
# 库内三例的自定义通带（避开自动表覆盖区，见模块 docstring）
CURVES_CAT = {
    83: {'band': 'sp-golden-83', 'lam_aa': [6000.0, 6100.0, 6200.0],
         't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'},
    112: {'band': 'sp-golden-112', 'lam_aa': [4800.0, 5100.0, 5400.0],
          't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'},
    116: {'band': 'sp-golden-116', 'lam_aa': [3000.0, 3200.0, 3400.0],
          't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'},
}


def _golden_requests():
    """{name: (body, exclude_keys, exclude_subtrees)}；exclude 为空 = 全响应严格。"""
    up = {'spectrum': {'lam_aa': list(LAMS), 'flux': [F0 * m for m in NOISE],
                       'flux_err': None, 'meta': {'flux_unit': 'erg/s/cm^2/Angstrom'}},
          'bands': ['sp-golden-r'], 'custom_curves': [dict(CC_G)],
          'weighting': 'photon'}
    cat = {}
    for sid, cc in CURVES_CAT.items():
        cat[sid] = {'spectrum_id': sid, 'bands': [cc['band']],
                    'custom_curves': [dict(cc)], 'weighting': 'photon',
                    'mode': 'anchored',
                    'anchor_rows': [{'band': cc['band'], 'mag': 18.0,
                                     'mag_system': 'AB', 'mag_err': 0.05}]}
    # 库内三例：掩膜新行为族豁免（见模块 docstring 的裁量与理由）
    excl = ({'mask_hash'}, ('preprocess',))   # n_masked_pixels 已收紧回比较域（golden 波段避开表区，两侧恒 0）
    return {
        'upload_direct': (up, set(), ()),
        'cat83_anchored': (cat[83], *excl),
        'cat112_anchored': (cat[112], *excl),
        'cat116_anchored': (cat[116], *excl),
    }


def _prune_safe(obj, keys, subtrees, path=()):
    """深拷贝剔除：dict 键 ∈ keys 的删（任意深度）；路径以 subtrees 之一为前缀的
    整棵子树删；warnings[] 里 reason ∈ WARN_EXCL 的条目删（n_outlier_flagged 的
    回显面，见模块 docstring 的比较域裁量）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            p = path + (k,)
            if k in keys or any(p[:len(st)] == st for st in
                                (s if isinstance(s, tuple) else (s,)
                                 for s in subtrees)):
                continue
            out[k] = _prune_safe(v, keys, subtrees, p)
        return out
    if isinstance(obj, list):
        items = []
        for v in obj:
            if (isinstance(v, dict) and path[-1:] == ('warnings',)
                    and v.get('reason') in WARN_EXCL):
                continue
            items.append(_prune_safe(v, keys, subtrees, path))
        return items
    return obj


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      indent=1)


@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(scope='module', autouse=True)
def _login(client):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = 'p1b-baseline'
        s['role'] = 'user'


@pytest.mark.parametrize('name', ['upload_direct', 'cat83_anchored',
                                  'cat112_anchored', 'cat116_anchored'])
def test_t75_p1_baseline_byte_identical(client, name):
    """T-75①：现场响应与 P1 存档在比较域内逐字节一致。"""
    body, keys, subtrees = _golden_requests()[name]
    golden_path = os.path.join(GOLDEN_DIR, name + '.json')
    assert os.path.exists(golden_path), \
        f'golden 存档缺失：{golden_path}（生成方式见本文件模块 docstring）'
    with open(golden_path, encoding='utf-8') as f:
        archived = json.load(f)
    r = client.post('/api/specphot/photometry', json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    live = json.loads(r.get_data(as_text=True))
    a_canon = _canon(_prune_safe(archived, keys, subtrees))
    l_canon = _canon(_prune_safe(live, keys, subtrees))
    assert l_canon == a_canon, '比较域内响应与 P1 基线不一致（先查实现，再查豁免是否被滥用）'
