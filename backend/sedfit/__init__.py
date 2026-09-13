"""暂现源 SED 分析子系统（与 hostfit 平级）。

- builder.py    M1: SED 构建器（逐历元取数 / GP 同时化，纯计算同步）
- laws.py       宿主消光律模板 + 通带综合
- models.py     M2/M4: 幂律+消光、双幂律、黑体系列模型注册表
- closure.py    M3: 闭包关系系数表（closure_relations.json）+ α–β 诊断
- bolometric.py M5: 伪玻尔兹曼光变（L_obs / L_bb / Lyman+2014 BC 交叉）
- jobs.py       异步拟合任务（克隆 fitting/jobs.py 模式，model_name='sed_*'）
"""
