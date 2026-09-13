"""M3: 闭包关系 α–β 诊断（纯计算，同步；设计方案 §3-M3）。

约定 Fν ∝ t^(−α) ν^(−β)（喷流前演化）。系数表存于同目录
closure_relations.json（模块级缓存；比照 backend/relations.json 做法，
便于审校与扩充）。无注入标准关系取自 Sari+1998 / Granot & Sari 2002 /
Sari, Piran & Halpern 1999；能量注入关系（L∝t^q 参数化）取自
Racusin+2009 (ApJ 698:43) Table 1 列 c，injection=true 条目的 alpha 以
{a0, qa, b0, qb} 表示 a=a0+qa·q、b=b0+qb·q：调用时提供 q（0≤q<1，
文献常用范围）才代入求值并参与排名，q=None 时注入条目不参与。

诊断逻辑：对每条关系计算 α_pred = a·β + b 与偏差 σ 数
    σ_dev = |α_obs − α_pred| / √(σ_α² + (a·σ_β)²)
按 σ_dev 升序排名；p 无关的固定 β 段（快冷 ν_c<ν<ν_m）另报 β 偏差 beta_dev。
alpha_err/beta_err 可不填（None/0）：分母为 0 时 sigma_dev 输出 null 并附
note「未提供误差，仅给点估计偏差」，此时给 abs_dev = |α_obs − α_pred|。
所有对外返回值（diagnose / plot_data）均经 _finite 清洗，保证不含
NaN/Infinity（Flask jsonify 默认会输出非法 JSON 的 Infinity）。

p_candidates 按 设计方案 §3-M2/M3 约定给出两候选：
    below_nuc      = 2β+1（ν_m<ν<ν_c 标准映射）
    slow_above_nuc = 2β+2（设计方案保留的备选映射，与 sedfit/models.py 的
                     p_alt_2beta_plus_2 对齐；注意标准理论中 ν>ν_c 对应
                     p=2β，各关系自身的 p 字段给出的是标准映射）
"""
import ast
import json
import math
import os

_RELATIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'closure_relations.json')

_cache = None


def load_table():
    """加载闭包关系系数表（模块级缓存）。返回 {'_meta': ..., 'relations': [...]}"""
    global _cache
    if _cache is None:
        with open(_RELATIONS_PATH, encoding='utf-8') as f:
            _cache = json.load(f)
    return _cache


def _p_from_expr(expr, beta):
    """由 β 反解 p。expr 为 '2*beta' / '2*beta+1' 形式的白名单表达式；
    None（p 无关段）返回 None。白名单解析，不用 eval。"""
    if not expr:
        return None
    tree = ast.parse(expr, mode='eval')

    def _node(n):
        if isinstance(n, ast.Expression):
            return _node(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.Name) and n.id == 'beta':
            return float(beta)
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Mult, ast.Add,
                                                          ast.Sub, ast.Div)):
            l, r = _node(n.left), _node(n.right)
            if isinstance(n.op, ast.Mult):
                return l * r
            if isinstance(n.op, ast.Add):
                return l + r
            if isinstance(n.op, ast.Sub):
                return l - r
            return l / r
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -_node(n.operand)
        raise ValueError(f'p 表达式含不允许的语法: {expr!r}')

    return _node(tree)


_NO_ERR_NOTE = '未提供误差，仅给点估计偏差'


def _finite(obj):
    """递归清洗：非有限浮点（NaN/±Infinity）→ None，保证 jsonify 输出合法 JSON"""
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _ab_of(rel, q):
    """取关系的 (a, b) 斜率/截距。注入条目（injection=true）alpha 为
    {a0, qa, b0, qb} 参数化：a=a0+qa·q、b=b0+qb·q，q 为 None 时返回 None
    （调用方据此跳过，不参与排名/绘图）。"""
    al = rel['alpha']
    if rel.get('injection') is True or 'a0' in al:
        if q is None:
            return None
        return (float(al['a0']) + float(al['qa']) * q,
                float(al['b0']) + float(al['qb']) * q)
    return float(al['a']), float(al['b'])


def _check_q(q):
    """能量注入指数合法性：0≤q<1（文献常用范围），越界抛 ValueError（路由层 400）"""
    q = float(q)
    if not math.isfinite(q) or not 0.0 <= q < 1.0:
        raise ValueError(f'q（能量注入指数）必须满足 0 ≤ q < 1，得到 {q!r}')
    return q


def diagnose(alpha, alpha_err, beta, beta_err, q=None):
    """闭包关系诊断。alpha/beta 为观测值，alpha_err/beta_err 为 1σ 误差
    （可 None/0：此时 sigma_dev 为 null，仅给点估计偏差 abs_dev）。
    q：可选能量注入指数（L∝t^q，0≤q<1）；提供时注入条目代入 q 求值后
    参与排名，为 None 时注入条目不参与。

    返回 {input, ranking: [{id, label, medium, regime, segment, injection,
                            alpha_pred, sigma_dev, abs_dev, note, beta_dev,
                            p, ref}],
           best, p_candidates}
    """
    alpha = float(alpha)
    beta = float(beta)
    alpha_err = max(float(alpha_err or 0.0), 0.0)
    beta_err = max(float(beta_err or 0.0), 0.0)
    if q is not None:
        q = _check_q(q)

    ranking = []
    for rel in load_table()['relations']:
        ab = _ab_of(rel, q)
        if ab is None:
            continue  # 注入条目且未提供 q：不参与排名
        a, b = ab
        alpha_pred = a * beta + b
        abs_dev = abs(alpha - alpha_pred)
        sigma = math.sqrt(alpha_err ** 2 + (a * beta_err) ** 2)
        if sigma > 0:
            sigma_dev = abs_dev / sigma
            note = None
        else:
            # 未提供误差：无法给 σ 数，退化为点估计偏差
            sigma_dev = None
            note = _NO_ERR_NOTE
        beta_fixed = (rel.get('beta') or {}).get('fixed')
        beta_dev = (abs(beta - beta_fixed) / beta_err
                    if beta_fixed is not None and beta_err > 0 else None)
        ranking.append({
            'id': rel['id'],
            'label': rel['label'],
            'medium': rel['medium'],
            'regime': rel['regime'],
            'segment': rel['segment'],
            'injection': rel.get('injection', 'none'),
            'alpha_pred': alpha_pred,
            'sigma_dev': sigma_dev,
            'abs_dev': abs_dev,
            'note': note,
            # p 无关段（快冷 ν_c<ν<ν_m）的 β 一致性检查（固定 β=1/2）
            'beta_fixed': beta_fixed,
            'beta_dev': beta_dev,
            'p': _p_from_expr(rel.get('p'), beta),
            'ref': rel.get('ref'),
        })
    # sigma_dev 为 null（未提供误差）的条目排在有 σ 数的之后，组内按 abs_dev 升序
    ranking.sort(key=lambda r: (r['sigma_dev'] is None,
                                r['sigma_dev'] if r['sigma_dev'] is not None
                                else r['abs_dev']))
    return _finite({
        'input': {'alpha': alpha, 'alpha_err': alpha_err,
                  'beta': beta, 'beta_err': beta_err, 'q': q},
        'ranking': ranking,
        'best': ranking[0] if ranking else None,
        # 设计方案 §3-M2/M3 约定的 p 候选（语义见模块 docstring）
        'p_candidates': {'below_nuc': 2.0 * beta + 1.0,
                         'slow_above_nuc': 2.0 * beta + 2.0},
    })


def plot_data(alpha, alpha_err, beta, beta_err, q=None):
    """α–β 诊断图的绘图数据：各理论线端点 + 数据点（Racusin+2009 风格）。

    q：可选能量注入指数（L∝t^q，0≤q<1）；提供时注入关系线一并绘出
    （line dict 带 dash=True，绘图层用虚线区分）。

    返回 {point: {beta, alpha, beta_err, alpha_err},
           lines: [{id, label, beta: [b1, b2], alpha: [a1, a2]}],
           beta_range: [bmin, bmax]}
    β 轴范围取数据点附近并至少覆盖 [0, 1.6]（余辉 β 常见域）。"""
    beta = float(beta)
    alpha = float(alpha)
    if q is not None:
        q = _check_q(q)
    bmin = min(0.0, beta - 0.8)
    bmax = max(1.6, beta + 0.8)
    lines = []
    for rel in load_table()['relations']:
        ab = _ab_of(rel, q)
        if ab is None:
            continue  # 注入条目且未提供 q：不画
        a, b = ab
        dash = rel.get('injection') is True
        # 固定 β 段（a=0 且 beta.fixed）画成竖线标记该 β 值
        beta_fixed = (rel.get('beta') or {}).get('fixed')
        if beta_fixed is not None and a == 0.0:
            lines.append({'id': rel['id'], 'label': rel['label'],
                          'beta': [beta_fixed, beta_fixed],
                          'alpha': [b, b],
                          'marker': 'point', 'dash': dash})
            continue
        lines.append({'id': rel['id'], 'label': rel['label'],
                      'beta': [bmin, bmax],
                      'alpha': [a * bmin + b, a * bmax + b],
                      'marker': 'line', 'dash': dash})
    return _finite({
        'point': {'beta': beta, 'alpha': alpha,
                  'beta_err': float(beta_err or 0.0),
                  'alpha_err': float(alpha_err or 0.0)},
        'lines': lines,
        'beta_range': [bmin, bmax],
    })


def make_plot(alpha, alpha_err, beta, beta_err, q=None):
    """生成 α–β 诊断图 PNG 字节（matplotlib Agg，英文标签）。
    q：可选能量注入指数；提供时注入关系线用虚线绘制。"""
    import io

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    data = plot_data(alpha, alpha_err, beta, beta_err, q=q)
    diag = diagnose(alpha, alpha_err, beta, beta_err, q=q)
    best = diag.get('best') or {}

    fig, ax = plt.subplots(figsize=(7, 5.5))
    for ln in data['lines']:
        style = '--' if ln.get('dash') else '-'
        if ln['marker'] == 'point':
            ax.plot(ln['beta'], ln['alpha'], 's', ms=6, mfc='none',
                    label=ln['label'])
        else:
            ax.plot(ln['beta'], ln['alpha'], style, lw=1.2, alpha=0.8,
                    label=ln['label'])
    pt = data['point']
    ax.errorbar([pt['beta']], [pt['alpha']],
                xerr=[pt['beta_err']] if pt['beta_err'] > 0 else None,
                yerr=[pt['alpha_err']] if pt['alpha_err'] > 0 else None,
                fmt='o', color='k', ms=7, capsize=3, zorder=10,
                label='data')
    bmin, bmax = data['beta_range']
    ax.set_xlim(bmin, bmax)
    ax.set_xlabel(r'$\beta$  ($F_\nu \propto \nu^{-\beta}$)')
    ax.set_ylabel(r'$\alpha$  ($F_\nu \propto t^{-\alpha}$)')
    title = 'Closure relation check'
    if best:
        sd = best.get('sigma_dev')
        # 未提供误差时 sigma_dev 为 None，标题退化为点估计偏差
        title += (f"   best: {best.get('id')} ({sd:.2f}σ)" if sd is not None
                  else f"   best: {best.get('id')} "
                       f"(|Δα|={best.get('abs_dev', 0):.2f}, no errors)")
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=6.5, loc='upper left', framealpha=0.9)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    return buf.getvalue()
