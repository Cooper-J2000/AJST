# AJST 暂现源光变目录系统 — 技术文档

> 版本：2.13
> 更新日期：2026-08-27

> **说明**：本文档由开发过程中的技术文档整理而来，部分内容（数据规模、批次导入历史、
> 已删除的脚本与备份路径等）为历史快照；如有与代码不一致之处，**以代码为准**。

> **时间约定（v2.2 起强制）**：所有时间字段（`t0`、`created_at`、`updated_at`、CSV/JSON 中的 `T0`）
> 一律为 **UTC 原值**，系统在任何通道（ETL 导入、API 录入、数据库存储、`--dump` 导出）
> 都不做时区转换；输入即使不带 `Z` 也视为 UTC，带时区偏移的输入只取时间字面值。

---

## 一、系统架构

```
┌──────────────────┐      REST API       ┌──────────────────────┐
│  前端 SPA (ESM)   │ ◄─── JSON ────────► │  Flask 后端 (Python)  │
│  /frontend/       │                     │  /backend/            │
│  index.html       │                     │  app.py + routes/     │
│  js/pages/*.js    │                     │  port 5000            │
└──────────────────┘                     └───────┬──────────────┘
                                                 │ SQLAlchemy
                                                 ▼
                                        ┌──────────────────────┐
                                        │  PostgreSQL 14        │
                                        │  db=ajst_catalog      │
                                        │  peer auth（默认）     │
                                        └──────────────────────┘
                                              ↕  python3 etl.py
                                        ┌──────────────────────┐
                                        │  catadata/ 文件       │
                                        │  info/*.json (628)    │
                                        │  lc/*.csv   (628)     │
                                        │  filters.json (72)    │
                                        └──────────────────────┘
```

### 数据流

- **PostgreSQL 是数据实体（持久化存储）**——所有增删改直接写库，服务器重启/前端刷新不丢
- `catadata/` 文件是初始数据源/文件级备份
- **正常使用不需要 ETL**：网页端编辑后数据直接持久化到数据库
- ETL（`python3 etl.py`）仅在两种场景需要：
  1. 首次部署：从 CSV/JSON 文件导入数据库
  2. 手动改了文件后：`python3 etl.py --sync` 同步到数据库
  3. 数据库 → 文件同步：`python3 etl.py --dump`

### 当前数据规模

| 指标 | 值 |
|---|---|
| 暂现源 | 1440（含 catalog-only 文献源与目录导入新建源，见 §8.5-§8.12） |
| 光变数据点 | 227,322 |
| 光学滤波器 | 81 |
| 望远镜 | 842 |
| 有红移 | 763 |
| 光谱 | 111（spectra 表 + `catadata/spectra/` 文件） |

---

## 二、数据库设计（10 张表）

### 2.1 `transients` — 暂现源主表

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | VARCHAR(32) PK | 唯一标识，如 `EP240315a` / `GRB000131A` |
| `ra` | FLOAT | J2000 赤经（度） |
| `dec` | FLOAT | J2000 赤纬（度） |
| `t0` | DATETIME | 触发时刻（UTC） |
| `trigger_instrument` | VARCHAR(64) | 触发仪器 |
| `redshift` | FLOAT | 红移值 |
| `redshift_type` | VARCHAR(16) | `value` / `phot_z` / `spec` / `spec-host` / `upperlimit` |
| `redshift_ref` | TEXT | 红移引用（GCN 编号等） |
| `pos_error` | FLOAT | 位置误差 |
| `pos_error_unit` | VARCHAR(16) | 缺省 `arcsec` |
| `pos_ref` | TEXT | 位置引用 |
| `comment` | TEXT | 用户备注 |
| `sub_tag` | JSONB | 子标签数组，如 `["L","S","X"]` |
| `tags` | JSONB | 标签数组，如 `["fxt","grb"]` |
| `aliases` | JSONB | 别名数组 |
| `extra_data` | JSONB | **任意扩展字段（推荐方式）**，含 `catalog_data`（外部目录参数，见 §8.5）与 `derived`（静止系派生量，见 §8.13） |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |

### 2.2 `lightcurves` — 光变数据点

所有源（628 个）的数据点都存储在同一张表中，通过 `transient_id` 外键区分。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK auto | |
| `transient_id` | VARCHAR(32) FK | → transients.id，级联删除 |
| `time` | FLOAT | **统一以秒为单位** |
| `time_err` | FLOAT | 时间误差（秒） |
| `time_unit` | VARCHAR(8) | 固定为 `s`（ETL 自动统一） |
| `band` | VARCHAR(32) | 波段标识 |
| `flux_density` | FLOAT | **原始值**（星等或流量密度，不强制转换） |
| `flux_density_err` | FLOAT | 原始误差 |
| `flux_density_unit` | VARCHAR(32) | 原始单位：`mag` / `mJy` / `uJy` / `Jy` / `cgs` |
| `mag_system` | VARCHAR(8) | 原始星等系统：`AB` / `Vega` |
| `gext_corr` | BOOLEAN | 是否已做银河消光改正 |
| `upperlimit` | BOOLEAN | 是否为上限 |
| `host_subtracted` | BOOLEAN | 测光是否已扣除宿主星系（false=直接对目标测光可能含宿主，NULL=未知；2026-08-26 新增，详情页数据表展示/行内编辑、CSV 上传映射均已支持） |
| `gext_Alambda` | FLOAT | 银消量 A_λ（mag） |
| `mag_gextcor` | FLOAT | 银消改正后 AB 星等 |
| `mag_gextcor_err` | FLOAT | 银消改正后 AB 星等误差 |
| `flux_density_gextcor` | FLOAT | 银消后流量（mJy） |
| `flux_density_gextcor_err` | FLOAT | 银消后流量误差（mJy） |
| `flux_density_gextcor_unit` | VARCHAR(32) | 固定为 `mJy` |
| `weights` | FLOAT | 拟合权重，缺省 1.0 |
| `discard` | BOOLEAN | **标记为后处理排除**，不是删除 |
| `telescope` | VARCHAR(128) | |
| `instrument` | VARCHAR(128) | |
| `reference` | TEXT | 引用 |
| `comment` | TEXT | 备注 |
| `extra_data` | JSONB | 扩展字段 |
| `created_at` / `updated_at` | DATETIME | |

索引：`idx_lc_transient_band` (transient_id, band)、`idx_lc_transient_time` (transient_id, time)

### 2.3 `filters` — 滤波器定义

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | VARCHAR(32) PK | 波段名，如 `r`、`J`、`uvot-u`、`wise-w1` |
| `wavelength` | FLOAT | 有效波长（Å） |
| `filter_type` | VARCHAR(16) | `mean` / `ref` / `eff` / `guess` |
| `vega2ab` | FLOAT | Vega→AB 转换 |
| `description` | TEXT | 说明（望远镜/巡天名称） |
| `extra_data` | JSONB | 扩展字段 |

当前 72 个滤波器，涵盖 UV 到远红外（fuv 1548Å → wise-w4 220883Å）。

### 2.4 `tags` — 标签定义

| 列 | 类型 |
|---|---|
| `id` | BIGINT PK auto |
| `name` | VARCHAR(64) UNIQUE |
| `description` | TEXT |
| `color` | VARCHAR(16) |

### 2.5 `articles` — 相关研究文章（2026-08-26 新增；2026-08-27 扩展 title/bibtex）

每个源可有多条；详情页「基本信息」卡片展示并可维护（登录可添加，修改/删除仅管理员）。
条目以自增 `id` 定位——同一作者同年可能有多篇文章，`name` 简称**不唯一**，不能用作定位键。
BibTeX 可能很长，前端不整段展示，仅提供「复制到剪贴板」按钮。
落盘：随 `etl.py --dump` 写入每源 `info/<tid>.json` 的 `articles` 字段，导入时全量替换重建（见 §3.1）。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK auto | |
| `transient_id` | VARCHAR(32) FK→transients CASCADE | |
| `name` | VARCHAR(256) | 文章简称，建议格式「第一作者+年份」（如 `Dainotti+2024`），不唯一 |
| `url` | TEXT | 文章网页链接 |
| `title` | TEXT | 文章标题（可含引号/冒号/空格），可空 |
| `bibtex` | TEXT | BibTeX 引用信息，可空 |
| `source` | VARCHAR(128) | 录入账户（POST 时自动记录） |
| `created_at` / `updated_at` | DATETIME | naive UTC |

### 2.6 `host_galaxies` — 宿主星系（2026-08-29 新增）

每个源至多一行（transient_id UNIQUE）。详情页「宿主星系」tab 展示与维护；pcigale SED 拟合（见 §8.20）结果经用户确认后写入 `derived`。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK auto | |
| `transient_id` | VARCHAR(32) FK→transients CASCADE, UNIQUE | |
| `ra` / `dec` | FLOAT | 宿主坐标（度） |
| `redshift` / `redshift_err` | FLOAT | 宿主红移；光谱红移 err=0，测光红移有误差 |
| `redshift_type` | VARCHAR(16) | `spec` / `phot` |
| `photometry` | JSONB | `[{band, mag, mag_err, mag_sys(AB/Vega/ST), source}]` |
| `derived` | JSONB | 采纳的拟合参数 `{m_star, sfr, age_main, Av_ISM, chi2, fit_at, job_id}` |
| `comment` / `source` | TEXT / VARCHAR(128) | |

落盘：随 `--dump` 写入 `info/<tid>.json` 的 `host_galaxy` 字段，导入时 upsert（见 §3.1）。

### 2.7 预留表

| 表名 | 用途 |
|---|---|
| `spectra` | 光谱文件元数据（已启用，111 条：filename/仪器/观测日期/波长范围/file_path→`catadata/spectra/<tid>/`、extra_data 含 observer/reducer/flux_type/来源，见 §8.10、§8.15） |
| `images` | 图像文件（空，等待扩展） |
| `fitting_results` | 余辉拟合任务记录（v2.5 起启用，见 §8.14） |
| `extinction_corrections` | 银河消光改正记录（空，等待扩展） |

---

## 三、ETL 数据导入

### 3.1 命令

```bash
# 全量重建（清空数据库重灌所有文件）
python3 etl.py

# 增量同步（只更新文件有变化的源）
python3 etl.py --sync

# 指定源导入
python3 etl.py --transient EP240315a GRB000131A

# 数据库 → 文件回写
python3 etl.py --dump
```

> **`--dump` 覆盖范围**（2026-08-27 起）：`info/*.json`（含每源的 `articles` 研究文章字段）、
> `lc/*.csv`、`filters.json`。光谱文件由后端在上传/删除时同步维护，全量重建会自动
> 从 `catadata/spectra/` 重建 `spectra` 表索引。`--dump` 只写不删：删除整个源或删光
> 某源光变点后需手动删除对应文件。info JSON 中的 `articles` 字段在导入时对该源做
> 全量替换（缺该字段的旧格式文件不动库中条目），全量重建不丢文章数据。

### 3.2 单位处理（导入时）

**时间 → 秒（导入时自动统一）**
```
s, sec, second(s) → ×1
min, m, minute(s) → ×60
h, hour(s)        → ×3600
d, day(s)         → ×86400
```

**流量/星等 → 保留原始值与原始单位（v2.1 起不再强制转 mJy）**

`flux_density` / `flux_density_err` / `flux_density_unit` 按 CSV 原样入库
（`mag` / `mJy` / `uJy` / `Jy` / `cgs`），星等系统记入 `mag_system`。
统一为 mJy 的工作在**银河系消光改正**时完成，结果写入
`flux_density_gextcor` / `flux_density_gextcor_err`（单位固定 mJy），
供余辉拟合等下游功能直接使用。

### 3.3 AB 星等 ↔ mJy 转换公式（消光改正时使用）

```
flux_density (mJy) = 3.631 × 10^(6 - ABmag / 2.5)

ABmag = 16.4 - 2.5 × log10(flux_density_mJy)
```

**误差传播：**
```
σ_flux (mJy) = (ln(10) / 2.5) × flux_mJy × σ_mag
σ_mag       = (2.5 / ln(10)) × σ_flux / flux_mJy
```

### 3.4 JSON 文件格式（`catadata/info/<id>.json`）

```json
{
  "transient_id": "GRB000131A",
  "alias": [],
  "ra": 93.3875,
  "dec": -51.933333,
  "T0": null,
  "Trigger_Instrument": null,
  "redshift": 4.5,
  "tag": ["grb"],
  "sub_tag": ["L"],
  "pos_error": null,
  "pos_ref": null,
  "redshift_type": null,
  "redshift_ref": null,
  "comment": null
}
```

字段说明：
- `tag`：主标签，`["fxt"]` / `["grb"]` / `["sn"]` / `["tde"]`
- `sub_tag`：子标签，`["L"]` / `["S"]` / `["X"]` 等

### 3.5 CSV 文件格式（`catadata/lc/<id>.csv`）

```
time,time_err,time_unit,band,flux_density,flux_density_err,flux_density_unit,mag_system,...
4665.6,300.0,s,atlas-c,15.6,0.2,mag,AB,...
```

注意：CSV 中的单位是**原始单位**，导入时仅时间统一为秒，流量/星等保留原始单位入库。

---

## 四、银河系消光改正（光学波段）

核心代码：`backend/extinction.py`（算法遵循 `catadata/galaxy_extinction.py` 的描述）。

- **E(B-V)**：CSFD(2023) 尘埃图（`dustmaps` 的 `CSFDQuery`，尘埃图数据需预先下载到运行环境的 dustmaps 包内）
- **消光曲线**：Pei (1992)（`dust_extinction.shapes.P92`），Rv 固定 3.1
- **改正流程**（对每个数据点）：
  1. 取原始星等（`flux_density_unit=mag` 直接用原值；流量密度数据先按 AB 零点 `mag = 16.4 - 2.5·log10(f_mJy)` 换算）
  2. 若为 Vega 星等系统：`AB = mag + vega2ab`（查 `filters` 表）
  3. 按源坐标查 E(B-V)，`A_λ = 3.1·E(B-V)·P92(λ_波段)`
  4. `mag_gextcor = AB - A_λ`，星等误差不变（加减常数）
  5. 转回流量：`flux_density_gextcor = 3.631 × 10^(6 - mag_gextcor/2.5)`，`σ_flux = (ln10/2.5)·flux·σ_mag`
- **执行方式**（数据入库后的可选步骤，需登录）：
  - 前端：事件列表页「全局银消改正」按钮（全库）；详情页「银消改正」按钮（单源）；数据表每行 🌙 按钮（单点）
  - API：`POST /api/extinction/run`，body `{}` / `{"transient_id": "..."}` / `{"lightcurve_id": N}`
- **适用条件**：源有 RA/Dec 坐标 + 波段在 `filters` 表中（keV/GHz 等非光学波段自动跳过）+ 数据为有效星等或正的流量密度
- **自动重算**：数据点被修改（流量/波段等）、源坐标变动、滤波器波长或 Vega2AB 变动时，已改正的数据点自动重算；条件不再满足（如坐标被清空）时自动清除改正结果
- **清除**：`POST /api/extinction/clear`（参数同 run）
- ETL `--dump` 导出的 CSV 含 `mag_Gextcor` / `mag_Gextcor_err` 列，可随文件回导

## 五、REST API

所有接口返回 JSON，`POST`/`PUT`/`DELETE` 需要鉴权（Flask signed cookie session）。
权限分两级：**管理员**（`admin` 账户）可执行全部操作；**普通用户**（管理后台创建）可新增/上传数据、扣点（discard）、提交拟合任务、修改自己录入的光变记录（`source` = 本账户），其余删除/修改已有数据的操作返回 403（下表「权限」列中标注"管理员"的接口普通用户调用返回 403）。

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/transients` | 列表（搜索/筛选/分页/排序） | 否 |
| POST | `/api/transients` | 新建事件 | 登录 |
| GET | `/api/transients/<id>` | 单源详情 | 否 |
| PUT | `/api/transients/<id>` | 更新基本信息 | 管理员 |
| DELETE | `/api/transients/<id>` | 删除事件（级联删除） | 管理员 |
| GET | `/api/lightcurves/<tid>` | 某源的全部光变数据 | 否 |
| POST | `/api/lightcurves/batch` | 批量新增光变点（自动记录 `source`=当前账户） | 登录 |
| POST | `/api/lightcurves/fit_model` | 时变函数拟合（pl/bpl/sbpl + tb 预设范围，不落库，见 §8.19） | 否 |
| PUT | `/api/lightcurves/<id>` | 更新单个光变点（普通用户可改自己录入的记录，即 `source`=本账户；他人记录仅可改 `discard` 扣点） | 登录 |
| DELETE | `/api/lightcurves/<id>` | 删除单个光变点 | 管理员 |
| DELETE | `/api/lightcurves` | 按 transient_id 删除 | 管理员 |
| GET | `/api/filters` | 滤波器列表（支持 `?sort=&order=`） | 否 |
| POST | `/api/filters` | 新建滤波器 | 登录 |
| PUT | `/api/filters/<id>` | 更新滤波器 | 管理员 |
| GET | `/api/tags` | 标签列表 | 否 |
| GET | `/api/stats/overview` | 汇总统计 | 否 |
| GET | `/api/relations` | 统计关系定义列表 + 各关系当前可用来源目录（见 §8.13） | 否 |
| GET | `/api/relations/<name>/data` | 取某关系统计数点（`?source=best` 或目录短名） | 否 |
| GET | `/api/extinction/status` | 银消功能可用性 + 覆盖统计 | 否 |
| POST | `/api/extinction/run` | 执行银消改正（`{}`全部源 / `{"transient_id"}`单源 / `{"lightcurve_id"}`单点） | 管理员 |
| POST | `/api/extinction/clear` | 清除改正结果（参数同 run） | 管理员 |
| GET | `/api/spectra` | 光谱元数据列表（`?transient_id=` 过滤） | 否 |
| GET | `/api/spectra/<id>` | 单条光谱完整数据（数值已统一强转） | 否 |
| POST | `/api/spectra/upload` | 上传光谱（两列/三列文本或 JSON，服务端校验规范化） | 登录 |
| DELETE | `/api/spectra/<id>` | 删除光谱（记录 + 文件） | 管理员 |
| GET | `/api/fitting/engines` | 拟合引擎清单（模型情形/默认先验/采样缺省，见 §8.14） | 否 |
| POST | `/api/fitting/jobs` | 提交余辉拟合任务（异步） | 登录 |
| GET | `/api/fitting/jobs` | 拟合任务列表（`?transient_id=` 过滤） | 否 |
| GET | `/api/fitting/jobs/<id>` | 任务详情（状态/参数估计/产物清单） | 否 |
| GET | `/api/fitting/jobs/<id>/files/<kind>` | 下载产物（`h5` 采样链 / `corner` 角图 / `lc_model` 模型光变；vegas_unified 另有 `metrics` / `lc_plot` / `lc_ratio`，见 §8.21） | 否 |
| DELETE | `/api/fitting/jobs/<id>` | 删除任务记录及产物 | 管理员 |
| GET | `/api/ingest/resolve` | 解析目标源（名称/别名精确 + 坐标锥形，只查不建，见 §8.16） | Bearer token |
| POST | `/api/ingest/photometry` | STDWeb 测光点接入（解析/映射/去重/单事务入库，见 §8.16） | Bearer token |
| GET | `/api/gcn/ids` | GCN 存档全部 circular id（数值升序，带缓存，见 §8.17） | 否 |
| GET | `/api/gcn/<cid>` | 单期 GCN circular JSON 内容 | 否 |
| GET | `/api/gcn/<cid>/related` | 库中与该期 GCN 相关的光变记录（reference 精确 + 暴名模糊，见 §8.17） | 否 |
| GET | `/api/gcn/status` | GCN 存档概况（期数/最新期号/存档目录修改时间 archive_mtime）+ 更新任务状态 | 否 |
| POST | `/api/gcn/update` | 从 NASA GCN 下载最新整包替换本地存档（后台线程） | 登录 |
| GET | `/api/export/transients` | 导出事件 CSV | 登录 |
| GET | `/api/export/lightcurves/<tid>` | 导出光变 CSV | 登录 |
| POST | `/api/auth/login` | 登录（`{username, password}`；省略 username 兼容旧版按 admin 验证） | — |
| POST | `/api/auth/logout` | 退出 | — |
| GET | `/api/auth/status` | 鉴权状态（返回 `authenticated`/`username`/`role`） | — |
| GET | `/api/admin/users` | 用户列表 | 管理员 |
| POST | `/api/admin/users` | 新增普通用户 `{username, password}` | 管理员 |
| PUT | `/api/admin/users/<id>` | 修改普通用户 `{username?, password?}`（不可改 admin） | 管理员 |
| DELETE | `/api/admin/users/<id>` | 删除普通用户（不可删 admin） | 管理员 |
| GET | `/api/articles` | 研究文章列表（`?transient_id=` 过滤） | 否 |
| POST | `/api/articles` | 添加文章条目 `{transient_id, name, url, title?, bibtex?}`（自动记录 `source`=当前账户） | 登录 |
| PUT | `/api/articles/<id>` | 修改文章条目 `{name?, url?, title?, bibtex?}`（title/bibtex 传空串即清空） | 管理员 |
| DELETE | `/api/articles/<id>` | 删除文章条目 | 管理员 |
| GET | `/api/hosts/<tid>` | 宿主星系信息（无记录 404） | 否 |
| PUT | `/api/hosts/<tid>` | upsert 宿主信息 `{ra?, dec?, redshift?, redshift_err?, redshift_type?, photometry?, derived?, comment?}` | 登录 |
| DELETE | `/api/hosts/<tid>` | 删除宿主信息 | 管理员 |
| GET | `/api/hostfit/config` | pcigale 拟合默认网格与可用波段 | 否 |
| POST | `/api/hostfit/jobs` | 提交宿主 SED 拟合 `{transient_id, mode, redshift?, grid, photometry}` | 登录 |
| GET | `/api/hostfit/jobs` | 任务列表（`?transient_id=`） | 否 |
| GET | `/api/hostfit/jobs/<id>` | 任务详情（含 best/bayes 参数） | 否 |
| GET | `/api/hostfit/jobs/<id>/files/<kind>` | 产物：`results`/`sed_png`/`best_model`/`log` | 否 |
| DELETE | `/api/hostfit/jobs/<id>` | 删除任务及产物 | 管理员 |

### 列表查询参数

```
GET /api/transients?search=EP24
                   &z_min=0.5&z_max=5.0
                   &tag=fxt
                   &ra_min=100&ra_max=200
                   &dec_min=-30&dec_max=30
                   &has_z=true
                   &sort=redshift&sort=t0&order=desc
                   &page=1&per_page=50
```

支持排序的列：`id`, `ra`, `dec`, `redshift`, `t0`

### 鉴权

```bash
# 登录（用户名 + 密码；管理员账户为 admin）
curl -X POST http://localhost:5000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的密码"}'

# 检查状态
curl http://localhost:5000/api/auth/status
# → {"authenticated": true, "username": "admin", "role": "admin"}
```

- 账户存于 `users` 表（密码经 werkzeug 哈希存储），首次启动自动创建管理员 `admin`，密码必须通过环境变量 `AJST_CATALOG_PASSWORD` 显式设置；未设置时每次启动随机生成。
- 管理员维护后台：`http://localhost:5000/admin`（仅 admin 可登录），可新增/修改/删除普通用户。
- 权限两级：**管理员**全部操作；**普通用户**可新增/上传数据、扣点（discard）、提交拟合任务、修改自己录入的光变记录（`source`=本账户），其余删除/修改已有数据返回 403。
- 光变表 `source` 列自动记录提交账户（ingest API 记为 `ingest-api` 或 body 指定的 `source`），`created_at`/`updated_at` 自动记录存入/最近修改时间（naive UTC）。

---

## 六、前端功能

### 6.1 页面路由

| 路由 | 页面 | 功能 |
|---|---|---|
| `#/` / `#/list` | 事件列表 | 搜索/筛选（标签/红移/RA/Dec）/排序（ID/红移/T0）/分页/删除/全局银消改正 |
| `#/stats` | 全局统计 | 概览卡片 + Mollweide 全天图 + 红移直方图（按 tag 筛选）+ 波段覆盖 |
| `#/stats/relations` | 统计关系 | GRB 瞬时辐射 6 个 2D 统计关系（Amati 等）散点 + 分组拟合（见 §8.13） |
| `#/transient/<id>` | 单源详情 | 基本信息 + 子标签 + 光变曲线 + 全列数据表 + 行内编辑 |
| `#/compare` | 多源对比 | 叠加对比光变曲线（含误差棒显示开关）；事件列表支持按名称/别名筛选 |
| `#/filters` | 光学滤光片 | 81 个滤波器 CRUD（类型 mean/ref/eff/guess 齐全），可排序/添加/行内编辑 |
| `#/new` | 新建事件 | 创建暂现源 |
| `#/tools/gcn` | GCN 阅读工具 | 工具箱条目：GCN circular 浏览 + 源信息/测光录入直写数据库（见 §8.17） |
| `#/tools/digitizer` | 抠图取数 | 工具箱条目：从图像提取数据点，直写 lightcurves 表（见 §8.18） |

### 6.2 单源详情页标签

| 标签 | 内容 |
|---|---|
| 概览 | 基本信息表、子标签、Aladin Lite 天球图 |
| 光变曲线 | Chart.js 多波段光变曲线，Y 轴切换 mJy/绝对星等，X 轴切换线性/对数，顶部 day/MJD 副轴，静止系选项，误差棒显示开关，波段勾选面板（全选/全不选），原始/银消改正数据切换，经验函数拟合叠加（pl/bpl/sbpl），缩放/重置/导出 |
| 数据表 | 全部 24 列光变数据（含银消后 AB 星等及误差、来源 source、存入/修改时间），支持行内编辑 + 添加记录 + 多选批量删除 + 上传数据表（CSV 列映射导入）+ 单点银消改正（删除/银消仅管理员；行内编辑管理员任意记录、普通用户仅自己录入的记录（source=本账户），其余记录可扣点）；「列显示」面板勾选要展示的列（localStorage 持久化，默认紧凑子集：时间/时间误差/波段/流量/误差/单位/星等系统/银消/上限/银消量/望远镜/仪器/引用/备注；仅影响页面显示，导出 CSV 始终为服务端全列完整版）；首列勾选框标记行 → 整行橙色高亮（仅前端定位用，不写库），表头复选框可全标/全清 |
| 余辉拟合 | VegasAfterglow 正向激波拟合：配置/提交任务/状态轮询/结果展示（见 §8.14） |
| 光谱数据 | 光谱列表（多选对比/逐条偏移/删除）+ 波长-流量图；观测者系横轴 + 有红移时静止系副轴（λ/(1+z)，逐帧同步）；绝对/相对流量模式（相对模式按中值归一+用户偏移）；误差条可开关；**横纵轴各自可切线性/对数**（对数 Y 自动滤除非正流量点）；坐标范围设置（数字输入 xmin/xmax/ymin/ymax + 图上拖拽框选缩放 + 恢复默认）；TNS 风格谱线标记面板（30 组常见谱线 H/He/C/N/O/…+自定义波长+Tellurics+星系线+WR 线，逐组 z 与 v_exp 可调，λ=λ₀(1+z)(1−v/c)，详见 §6.5）；上传光谱（登录后） |
| 余辉SED分析（开发中） | 预留 |

### 6.3 鉴权行为

- 登录需用户名 + 密码；管理员账户为 `admin`（密码取 `AJST_CATALOG_PASSWORD`，未设置时每次启动随机生成），普通用户由管理员在 `/admin` 后台维护
- 导航栏登录后显示当前账户名；管理员额外显示「管理后台」入口（`/admin`）
- 数据表"添加记录"/"上传数据表"按钮与编辑列登录后显示；普通用户编辑列内：自己录入的记录（source=本账户）显示行内编辑按钮，其余记录只有扣点（discard 切换）按钮；单点银消/删除选中与行首删除复选框列仅管理员可见
- 事件编辑、derived 卡片编辑、银消改正按钮仅管理员可用
- 光学滤光片的添加按钮登录后显示，编辑仅管理员
- 光谱的上传按钮登录后显示，删除按钮仅管理员
- 拟合任务提交登录后可用，任务删除按钮仅管理员
- 登录/退出后自动 `location.reload()` 全页刷新
- 删除事件需两步确认：
  1. `confirm("你是否确认要删除 xxx 整个条目？")`
  2. `prompt("请输入 DELETE CONFIRM")`

### 6.4 文件结构

```
frontend/
├── index.html                # SPA 入口
├── admin.html                # 管理员维护后台（/admin，独立页面，用户管理）
├── css/style.css             # 全局样式
├── vendor/                   # 本地化的前端库（v2.12 起不再依赖 CDN：bootstrap/bootstrap-icons/chart.js/aladin.js + 图标字体）
└── js/
    ├── app.js                # 路由 + 鉴权 UI + 全局函数
    ├── api.js                # API 客户端 + 鉴权状态
    ├── layout.js             # 页面容器组件
    ├── spec_lines.js         # TNS 风格谱线标记：谱线组数据 + Chart.js 标记插件 + 面板 HTML（v2.7）
    ├── digitizer_core.js     # 抠图取数核心算法（标定变换/Lab 颜色/掩膜/描线/连通域，v2.11）
    └── pages/
        ├── list.js           # 事件列表（筛选/排序/分页/删除）
        ├── detail.js         # 单源详情（概览/光变/数据表/编辑/添加/批量删除）
        ├── lc_upload.js      # 数据表 CSV 上传（列映射预览导入，v2.9）
        ├── gcn_tool.js       # GCN 阅读工具（工具箱，v2.10，见 §8.17）
        ├── digitizer.js      # 抠图取数（工具箱，v2.11，见 §8.18）
        ├── fitting_tab.js    # 详情页"余辉拟合"标签页（配置/任务轮询/结果展示，见 §8.14）
        ├── stats.js          # 全局统计（全天图/红移柱状图/波段覆盖）
        ├── relations.js      # 统计关系（6 关系平铺卡片/来源切换/分组 OLS 拟合/导出 CSV）
        ├── create.js         # 新建事件
        ├── compare.js        # 多源对比
        └── filters.js        # 光学滤光片 CRUD
```

```
backend/fitting/
├── engines/
│   ├── base.py           # 拟合引擎注册表（可继续注册新引擎）
│   └── vegas_unified.py  # 组合模型引擎（四轴 216 种组合 + 联合约束，见 §8.21）
├── vegas_unified/        # 修改版拟合程序 vendored 副本
│   ├── custom_mcmc.py    # 自定义 MCMC 外壳（联合约束组合）+ 模型构建 + 绘图
│   └── prior_configs/    # 216 种组合的默认先验与描述（JSON，唯一来源）
└── jobs.py               # 单 worker 异步任务队列 + prepare_data 数据准备
backend/fitting_store/    # 拟合产物：<transient_id>/<任务id>/{chain_record.h5, corner.png, lc_model.json, run.log}
                          #   另有 metrics.txt / lc_plot.png / lc_ratio_plot.png
```

### 6.5 光谱图 TNS 风格功能（v2.7 新增）

复刻自 wis-tns.org 对象页 Spectra 区（以 2023ixf/2017iuk 页为准，谱线定义逐字提取自页面
drupal-settings `objectFlot.*.params.markings`），全部在前端实现，无后端改动。

**坐标范围设置**（光谱数据标签页，图上方一行）

- 数字输入：`xmin`/`xmax`/`ymin`/`ymax`（x 为观测者系波长 Å，y 为流量）+「应用」「恢复默认」，
  交互与光变曲线页一致（`specAxisRange` 状态，null=自动）
- 光标框选：图上拖拽框选缩放，选区自动回填输入框；复用 `dragzoom.js`，
  新增 `allowNonPositive` 选项（光谱为线性轴、允许 0/负流量；log 轴调用点行为不变，向后兼容）
- 切换暂现源时范围自动重置

**谱线对比标记面板**（折叠面板「谱线标记」，图上方）

- 30 组谱线，组名/颜色/静止系波长与 TNS 完全一致：
  H（Balmer+Paschen）、He I/II、C II/III/IV、N II/III/IV/V、O I/[O I]/O II（含 SLSN-I blends）/
  [O II]/[O III]/O V/O VI、Na I、Mg I/II、Si II、S II、Ca II（含 H&K、IR 三重线）/[Ca II]、Fe II/III；
  自定义 1–4（自由输入静止系波长）；Tellurics（6867–6884、7594–7621 Å 灰带，不做 z/v 偏移）；
  Galaxy lines（仅 z，含 H/NII/[OII]/[OIII]/NaI/MgII/SII/CaII/ZnII/CrII/FeII/MnII/MgI）；
  WR-WN、WR-WC/O 组合线
- 每组一行：复选框 + 色块 + `z=` 输入（缺省该源红移）+ `v_exp=` 输入（km/s，缺省 0）；
  悬停显示该组全部波长；底部 z-step/v-step 控制数字输入步进（缺省 0.01 / 1000）
- 线位换算（TNS 语义）：**λ = λ₀·(1+z)·(1 − v_exp/c)**，c=299792.458 km/s，v_exp>0 为抛射蓝移
- 绘制：Chart.js 插件 `specLinesPlugin`（afterDraw 逐帧绘制竖虚线 + 组名标签，交替两级高度防重叠；
  落在当前 x 范围内的线才绘制），与框选缩放、顶部静止系副轴实时同步；绝对/相对流量模式均可用

**实现要点**

- `frontend/js/spec_lines.js`：`SPEC_LINE_GROUPS`（组定义）+ `SPEC_MARKING_COLUMNS`（面板分栏）
  + `createSpecLinesPlugin(getState)`（插件工厂）+ `buildMarkingsPanelHTML(zDefault)`（面板 HTML）
- `detail.js`：`_specMarkings` 状态（组 key → `{on, z, v, wl}`，切换源时重置）、
  `specMarkingToggle/specMarkingSet/specMarkingStep` 面板事件（只 `chart.update('none')`，不重建图表）
- 未复刻（未要求）：TNS 的逐条光谱 z 输入、"Download selected ASCII" 按钮

---

## 七、配置与运维

### 7.1 环境变量

| 变量 | 说明 | 缺省值 |
|---|---|---|
| `AJST_CATALOG_PASSWORD` | 管理员 `admin` 的初始密码（仅首次建库播种时读取，之后改密码请用 `/admin` 或直接改 `users` 表） | **无——必须显式设置；未设置时每次启动随机生成** |
| `AJST_INGEST_TOKEN` | 数据接入（ingest）API 的 Bearer token（见 §8.16） | 无；未设置则 ingest API 整体返回 503 |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg2:///ajst_catalog`（本机 Unix socket + peer 认证，以当前 OS 用户连接，无硬编码用户名） |
| `AJST_DATA_DIR` | 数据目录（info/lc/filters/spectra/gcn 等，将数据仓库克隆/放置到该位置即可） | `<项目根>/catadata` |
| `AJST_PYTHON` | `backend/start.sh` 使用的 Python 解释器 | `python3` |
| `PORT` | `backend/start.sh` 监听端口 | `5000` |
| `FLASK_ENV` | 运行环境 | `production` |

### 7.2 启动/重启

**开机自启（v2.7 起，systemd 用户服务）**：

- 服务单元：`~/.config/systemd/user/ajst-catalog.service`，`systemctl --user enable ajst-catalog`；
  如需不开登录会话也随开机启动，执行 `loginctl enable-linger <用户名>`
- 单元文件中用 `%h` 表示家目录（systemd 用户服务占位符），启动命令指向
  `<AJST>/backend/start.sh`（`<AJST>` 为项目根目录）
- 启动脚本：`backend/start.sh` 为通用脚本——`cd` 到脚本所在目录后以
  `${AJST_PYTHON:-python3}` 启动 Flask，监听 `0.0.0.0:${PORT:-5000}`；
  其余配置（`DATABASE_URL` / `AJST_DATA_DIR` / `AJST_CATALOG_PASSWORD` /
  `AJST_INGEST_TOKEN`）全部从进程环境变量读取，需在 systemd 单元（`Environment=`）
  或启动环境中显式提供。**注意：`AJST_CATALOG_PASSWORD` 不显式设置时每次启动都会
  随机生成新密码，导致无法登录**
- 常用命令：
  ```bash
  systemctl --user status ajst-catalog     # 查看状态
  systemctl --user restart ajst-catalog    # 重启（改后端代码后）
  systemctl --user stop ajst-catalog       # 停止
  journalctl --user -u ajst-catalog -f     # 看日志
  ```

**手动启动**（不用 systemd 时）：后端需要一个装好依赖的 Python 环境（Flask / SQLAlchemy /
psycopg2 必需；astropy / dustmaps / dust_extinction 用于银河系消光改正）：

```bash
cd <AJST>/backend
AJST_CATALOG_PASSWORD='你的密码' python3 -c "
import sys; sys.path.insert(0, '.')
from app import create_app
create_app().run(host='0.0.0.0', port=5000, debug=False)
"
```

或直接运行 `bash <AJST>/backend/start.sh`（环境变量同上）。

### 7.3 数据库操作

默认配置走本机 Unix socket + peer 认证（以当前 OS 用户的同名数据库角色连接，无需 `-U`）；
其他部署方式请按 `DATABASE_URL` 的配置调整。

```bash
# 连接查询
psql -d ajst_catalog

# 常用查询
SELECT id, redshift, tags, sub_tag FROM transients WHERE redshift > 3;
SELECT transient_id, band, COUNT(*) FROM lightcurves GROUP BY transient_id, band;

# 备份/恢复
pg_dump ajst_catalog > /tmp/ajst_catalog_$(date +%Y%m%d).sql
psql -d ajst_catalog < backup.sql

# 添加新列
ALTER TABLE transients ADD COLUMN IF NOT EXISTS my_new_col FLOAT;
```

### 7.4 数据文件同步

```bash
# 数据库 → 文件
python3 etl.py --dump

# 文件 → 数据库（增量）
python3 etl.py --sync
```

### 7.5 密码修改

管理员密码已存于 `users` 表（哈希），改 `AJST_CATALOG_PASSWORD` 只影响**首次播种**。修改 admin 密码：

```bash
# 方式一：psql 直接更新（werkzeug 哈希）
python3 -c "
from werkzeug.security import generate_password_hash
print(generate_password_hash('你的新密码'))"  # 复制输出
psql -d ajst_catalog -c \
  "UPDATE users SET password_hash='上一步输出' WHERE username='admin';"
```

普通用户的增删改在 `http://localhost:5000/admin` 后台操作。

---

## 八、扩展指南

### 8.1 添加新属性（推荐：extra_data JSONB）

```bash
curl -X PUT http://localhost:5000/api/transients/EP240315a \
  -H 'Content-Type: application/json' \
  -d '{"extra_data": {"peak_flux": 0.85, "photon_index": 1.95}}'
```

### 8.2 添加新的一级列

```bash
# 1. 加数据库列
psql -d ajst_catalog -c "ALTER TABLE transients ADD COLUMN peak_flux FLOAT;"
# 2. 改 models.py
# 3. 改 etl.py（导入 + 导出）
# 4. 改 routes/transients.py（_apply_transient_fields）
# 5. 改前端 detail.js（显示 + 编辑面板）
# 6. 改 catadata/数据统一列定义.md
```

### 8.3 添加新后端路由

```bash
# 1. 在 backend/routes/ 新建 *.py
# 2. 在 app.py 注册蓝图
# 3. 添加 @require_auth 保护写操作
```

### 8.4 添加新前端页面

```bash
# 1. 在 frontend/js/pages/ 新建 *.js
# 2. 在 frontend/js/app.js 路由表添加映射
# 3. 在 frontend/index.html 导航栏加链接
```

### 8.5 外部 GRB 目录数据（v2.3 新增）

14 个外部 GRB 目录的瞬时辐射参数已规范化并合并进 `transients.extra_data.catalog_data`，
按目录短名组织，每条记录含参数、来源（名称/URL/获取日期）：

```json
"catalog_data": {
  "fermi_gbm": {
    "params": {"t90": {"v": 325.8, "err": 1.2, "band": "50-300 keV"},
               "epeak": {"v": 512.0, "err": [20, 18], "model": "BAND", "frame": "obs"},
               "fluence": {"v": 0.038, "band": "10-1000 keV"}},
    "other": {...}, "name_alt": ["GRB 221009.553"], "trigger_time": "...",
    "source": {"name": "Fermi GBM Burst Catalog", "url": "...", "retrieved": "2026-07-21"}
  }
}
```

- **目录清单**（`catadata/external/<短名>/`，含原始文件 + README 逐列文档 + parse.py + normalized.jsonl）：
  `batse`（T90/四通道 fluence/350 个 Epeak）、`fermi_gbm`（T90/Epeak/fluence/峰流量）、
  `fermi_lat`（T90/fluence/静止系 Eiso）、`swift_bat`（T90/Epeak-CPL/fluence/红移）、
  `swift_grb`（T90/Epeak/fluence/Eiso/红移，2004–2012）、`heasarc_grbcat`（多任务 T90/fluence/红移）、
  `uvot_grb`（T90/BAT fluence/光子指数/红移）、`agile_mcal`（T90/43 个 Epeak/fluence）、
  `mpe_greiner`（定位索引，红移）；
  **2026-07-22 新增**：`saxgrbmgrb`（BeppoSAX/GRBM，T90/fluence 40–700 keV）、
  `swiftgrbba`（Swift BA 汇编，T90/fluence/XRT 余辉参数）、
  `rssgrbag`（射电余辉目录，T90/fluence/Eiso/E_γ + 射电峰值光变点）；
  `swift_xrt_live` 仅存档原始表（X 射线余辉目录，无瞬时辐射参数）
  （⚠️ wang2022 数据已于 2026-07-22 应要求从库中删除，其 external 文件保留但已从合并配置移除）
- **统计关系文献样本**（v2.4 新增，`insert_new` 目录）：
  `minaev2020`（Minaev & Pozanenko 2020, MNRAS 492, 1919；320 条，I/II 型分类 + Epi + Eiso）、
  `konus_wind`（Tsvetkova+2017/2021；317 条，Eiso/Liso/Epeak/tlag/e_gamma）、
  `wang2022`（Wang+2022, MNRAS 516, 2575；221 条，Ep,rest + Eiso 校准样本）、
  `liang2023`（Liang+2023, ApJS 266, 31；153 条，Band/CPL 谱型分类）、
  `guidorzi2025`（Guidorzi+2024, A&A 690, A261；216 条，变异性 V + Liso）
- **规范**：`catadata/external/SCHEMA.md`（统一键名；能段/模型/观测系/静止系/置信度逐量标注；误差对称单值、不对称 `[+,−]`）
- **匹配合并**：`backend/tools/catalog_merge.py`（dry-run 默认，`--apply` 写库；该脚本未随仓库发布，见 §8.11 末条）
  - 匹配规则：精确名（含 aliases）→ 字母约定差异时坐标 ≤1.5° → 无字母名按日期+坐标（阈值按目录精度）
  - **insert_new 机制**（v2.4 新增）：5 个统计关系文献目录标记 `insert_new: True`，未匹配记录不再丢弃，
    而是整条入库为新源（`comment` 标记 `catalog-only: <目录标签>`），可参与后续匹配与派生量计算；本次新增 77 个 catalog-only 源
  - 红移：DB 为 NULL 时按 mpe_greiner → swift_bat → swift_grb → grbcat → lat → uvot →（文献目录）优先级填补，`redshift_ref` 记 `catalog:<短名>`
  - 已合并：672/705 个源、3077 条目录记录（v2.4 在 595/628 源、1855 条基础上并入 5 个文献目录；未匹配源主要是无 GRB 对应的 EP/FXT）
- **ETL 往返**：v2.3 起 `--dump` 导出 `extra_data`、导入时读回，全量重建不丢目录数据
- **前端**：详情页概览标签内"外部目录参数"卡片，逐目录展示参数+来源链接
- **波段名统一（2026-08-02）**：GRBSN/Dainotti 中的波段名变体已在**数据层**统一映射到 filters 条目——
  UVOT 系（UVW2/UW2→uvot-uvw2、UVW1/UW1→uvot-uvw1、UVM2/UM2→uvot-uvm2、UVU/B/V→uvot-u/b/v、UWh/UVOT-white/White→uvot-white）、
  Johnson/Cousins 系（R_{c}/R_c/R_C→Rc、I_{c}/I_C/IC→Ic、B_{J}→B、J_{s}→J、K_{s}/K_s→Ks）、
  Sloan 撇号（r'/i'/g'/z'/u'/u′→r/i/g/z/u、i*→i）、Gunn i→i、
  HST 写法（V606/F606W/V/ACS/F606W→F606W、I814/F814W/I→F814W、WFC3/IR/F125W→F125W、
  WFC3/UVIS/F336W→F336W、ACS/F435W→F435W、F160W/H→F160W）；
  按用户判定：C_{r}（已确认为 unfiltered 宽带，主要来自 ROTSE-IIIc/TNT 快速测光）与 clear/Clear/CL/LP/C → **G**（comment 记录原波段名）；
  K'→Ks、50CCD/V→V、BV-MOA→V、RI-MOA→I、R_{special}→Rc（comment 均记原波段名）；
  'NaN'/'289' 无法识别已删除；3.6μm 按用户要求保留原名；
  GHz 射电与 10keV 保留原名（物理频率/能量，非 filters 条目）
- **更新流程**：重新下载原始表 → 重跑各目录 `parse.py` → `catalog_merge.py --apply`

### 8.6 UVOT 光变数据导入（v2.3 新增）

`uvot_grb` 目录的 **noc-db**（归一化最优叠加光变数据库，Roming et al. 2017 推荐的科学产品）
测光点已导入 `lightcurves` 表：

- 脚本：`backend/tools/uvot_noc_import.py`（dry-run 默认，`--apply` 写入；幂等可重跑；该脚本未随仓库发布，见 §8.11 末条）
- 仅 noc-db 一个文件；image-event-3db/5db（原始逐曝光测光，3″/5″ 两种孔径）**不入库**，原始文件保留在 `external/uvot_grb/` 备用
- 逐行去重：与已有行（`transient_id`+`band`+|Δtime|<0.01s+|Δmag|<0.01）重复即跳过
  （目录原始数据中已有约 9000 行来自 Dainotti2024 收集的同一数据源）
- 字段映射：`time=TIME`（暴后秒）、`time_err=(TSTOP-TSTART)/2`、`flux_density=MAG`（**Vega 系统**，
  `flux_density_unit='mag'`）、`upperlimit=False`（noc-db 全部 SIGMA≥2 探测）、
  `telescope='Swift'`、`instrument='UVOT'`、`reference='Roming et al. 2017, ApJS, 228, 13 (MAST HLSP uvotgrb, noc-db)'`、
  `extra_data` 含 `sigma`/`norm_to`
- 首次导入：新增 2145 行（40 个源，含 GRB060218A 的 243 个 uvot-v 点），跳过重复 9089 行；
  63 个 noc-db 中的 GRB 原不在本目录，经坐标/触发日期核验为独立事件后**新建暂现源**
  （`tools/uvot_new_grbs.py`，坐标/T0/触发仪器/红移取自 grb-cat.fits），其 2363 行测光全部导入
- 全部 uvot-* 波段的 `mag_system` 统一为 **Vega**（UVOT 测光本质上即 Vega 系统；
  此前部分数据被误标为 AB，2026-07-22 已全部更正并重算银消改正）
- 导入后已对受影响源执行银消改正并 `--dump` 同步

### 8.7 Fermi LAT 光变数据导入（v2.3 新增）

`fermi_lat` 目录 FITS（`gll_2flgc_dr1.fits`，2FLGC 第二个 LAT GRB 目录）的 97-bin 光变矢量
已导入 `lightcurves` 表：

- 脚本：`backend/tools/fermi_lat_lc_import.py`（dry-run 默认，`--apply` 写入；幂等可重跑；该脚本未随仓库发布）
- 库中不存在的 LAT GRB **新建暂现源**（坐标/T0 取自 FITS 的 RA/DEC/GRBMET；
  非标准 GCNNAME 取 GRBNAME 前 6 位日期命名，如 GRB081006）
- 字段映射：`time=LC_MEDIAN`（暴后秒）、`time_err=(LC_END-LC_START)/2`、`band='lat'`、
  `flux_density=LC_FLUX`（ph/cm²/s，0.1-100 GeV）、`LC_FLUX_ERR<=0` 的 bin 按上限处理、
  `telescope='Fermi'`、`instrument='LAT'`、SIGMA 存 extra_data
- 首次导入：新建 179 个暂现源、4905 个光变点（228 个 LAT GRB 全部覆盖）
- `lat` 波段不在 filters 表（高能），银消改正自动跳过

### 8.8 射电余辉峰值点导入（v2.3 新增）

`rssgrbag`（Chandra & Frail 2012 射电余辉目录）的射电峰值流量密度点已导入 `lightcurves` 表：

- 脚本：`backend/tools/rssgrbag_lc_import.py`（dry-run 默认，`--apply` 写入；幂等；该脚本未随仓库发布）
- 数据：`external/rssgrbag/lightcurve.jsonl`（140 点，每源每频段 1 个峰值点；
  注意原始论文的逐历元测量未电子化，机读版只有峰值表）
- 字段映射：`time`=峰时（秒）、`flux_density`=mJy 峰值流量密度、band 为 `"8.46GHz"` 等
  （1.38–43 GHz，共 15 种）、`method=fit/data` 存 extra_data、reference 记 Chandra & Frail 2012
- 首次导入：新建 13 个暂现源（最近邻 ≥1.3° 确认非重复）、140 个射电点
- 射电 band 不在 filters 表，银消改正自动跳过
- 本批次（grbcata_source_2.md）完整说明见 `catadata/external/导入说明_grbcata_source_2.md`

### 8.9 UKSSDC Burst Analyser XRT 光变导入（v2.3 新增）

[UKSSDC Swift Burst Analyser](https://www.swift.ac.uk/burst_analyser/alldensity.php) 的
XRT 10 keV 未吸收流量密度光变（Evans+2007,2009 数据产品）已导入 `lightcurves` 表：

- 脚本：`backend/tools/burst_analyser_import.py`（dry-run 默认，`--apply` 全量；8 线程抓取，幂等可重跑；该脚本未随仓库发布）
- 数据端点：`/burst_analyser/<obsid>/xrt/xrt_flux_{wt,pc}_DENSITY*.qdp`（ASCII：
  暴后秒、时间误差、流量密度、误差）
- 单位说明：DENSITY 产品的流量密度单位为 **Jy**（docs.php 记载：xspec 幂律归一化
  photons keV⁻¹ cm⁻² s⁻¹ ×0.000662 → Jy；早期曾误标为 erg/cm2/s/keV，2026-07-27 已更正）
- 字段映射：`band='10keV'`、`flux_density_unit='Jy'`、`telescope='Swift'`、
  `instrument='XRT'`、观测模式（wt/pc）与 obsid 存 extra_data、
  reference=`UKSSDC Swift Burst Analyser (Evans+2007,2009)`
- 首次导入（2026-07-27）：索引 1125 源中有数据 1031 个；匹配已有源 537 个、
  **新建 494 个暂现源**（坐标/T0 取自 swiftgrbba）；新增 **142,653** 个光变点，零重复
- `10keV` 波段不在 filters 表（非光学），银消改正自动跳过

### 8.10 GRBSNWebtool 数据导入（v2.3 新增）

[GRBSNWebtool](https://github.com/GabrielF98/GRBSNWebtool)（`Webtool/static/SourceData`，
原始文件存于 `catadata/external/grbsn_webtool/SourceData/`）：

- **测光**（`backend/tools/grbsn_photometry_import.py`，幂等；该脚本未随仓库发布）：
  56 个文件夹全部匹配（GRB020410、GRB030725 为新建，坐标取自 mpe_greiner）；
  新增 **15,106** 个测光点（Optical/IR 为 Vega 星等、Radio 为 mJy 流量密度），
  **每行 reference 为原始 ADS 文献链接**；重复 21,129 行跳过（与库中已有数据重叠）；
  跳过绝对星等/rest-frame/时间占位异常行 1,517 条。
  注意：部分来源（如 Ferrero 2006）数据**已被原作者做过消光改正**，
  这些行 `extra_data.ext_corrected_by_source=true`，银消改正流程会自动跳过它们（extinction.py 内置 guard）。
  ⚠️ 单位修正记录（2026-08-02）：GRBSNWebtool 各文件的单位列不统一，曾因按单一单位假设
  导致错误，已全部修正并重导——`time_unit` 列（days/hours/minutes/seconds 逐行换算）、
  射电 `freq_unit` 列（MHz→GHz ÷1000）、`mag_unit` 列（AB/Vega 逐行取值，缺省 Vega）；
  `dtime` 列语义各文件不一致（部分文件是"时间的秒数副本"而非误差），**一律不导入为 time_err**
- **超新星对应**：27 个源主 tag 加入 `sn`，28 个 SN 名加入 aliases（含 SN2020bvc→GRB200826A 手工关联、
  GRB230812B-SN2023pel 仅关联无数据）

### 8.11 GCN LLM 抽取测光导入（2026-08-25 首导后回滚；2026-08-26 用补充了 trigger-time 的 meta 重导）

> 2026-08-25 的首轮导入（34,121 条）曾应要求整体删除（备份在
> `catadata/backups/gcn_llm_rollback_20260825/`）；2026-08-26 用户在 circulars_meta.csv
> 补充 `trigger-time`/`trigger-instrument` 两列后重新导入，现库内为**重导后的数据**。

源：GCN 通报 LLM 抽取的结构化测量表 `measurements_part*.csv`（27 个文件、52,762 行、
3,064 个事件；来自独立的私有抽取项目，未随本仓库发布）。工具：

- **`backend/tools/gcn_llm_import.py`**（dry-run 默认，`--apply` 写库，幂等去重沿用 grbsn 容差；
  该脚本未随仓库发布，见本节末条）：
  全部入库行 `source='bot'`，`created_at/updated_at` 自动记录。
  - 波段：光学按仪器上下文归一（仪器含 UVOT → `uvot-*`、WISE → `wise-*`、Mephisto → `Mephisto-*`），
    再精确匹配 filters 表——**R/r、I/i、g/G 是不同波段，绝不大小写混叠**；
    filters 表没有的波段（clear/unfiltered 等）不强行入库，转审查。
  - 射电：抽取器吞小数点的整数值按经典频率白名单还原（846GHz→8.46GHz）；
    直读/还原两可的（如 250GHz）转审查不猜。
  - 时间：utc/mjd → 相对 t0 秒；t<0 转审查（t=0 允许，对应新目标的 first_detection t0）。
  - 事件匹配：catalog_merge.Matcher（GRB/EP 规范名）+ 通用别名兜底
    （去空格大写全串键命中 id/aliases，冲突键禁用）。
- **`backend/tools/gcn_llm_new_events.py`**（dry-run/--apply；未随仓库发布）：为 event_unmatched 事件建新目标。
  规范名（GRB 050802.422→GRB050802、EP 大小写变体→小写后缀、LIGO/Virgo 变体→S/G/GW 编号），
  原始写法全记 aliases；**t0 优先级：meta trigger-time（真实触发时刻）> 名字日期
  （GRB 带小数日用分数日）> 最早测量时刻**，`extra_data.t0_basis` 可溯源；
  circulars_meta.csv 有 ra/dec/redshift/trigger-instrument 时填入；
  同时给已有但缺 t0 的目标用 meta trigger-time 补 t0（只填 NULL 不覆盖）。
  安全闸：同日异字母库中已有源的不建（防同一暴字母约定差异误并），转人工审查。
- 结果（2026-08-26 重导）：新建目标 1,502 个（t0 来源 meta_trigger 1,434 / name_date 59 /
  first_detection 9；带坐标 1,132、红移 255），另补已有目标 t0 29 个；
  入库测光 **33,404** 条（`source='bot'`）；审查清单
  `backend/tools/gcn_llm_import_review.csv`（15,314 行：缺时间 7,610、event_unmatched 2,639
  （含 149 个同日异字母事件）、波段无法识别 2,546、早于 t0 2,050（trigger-time 精确后
  暴露的触发前测量）、射电频率歧义 421 等，待人工逐条处理）
- **2026-08-26 清理**：应用户要求删除全部 LIGO/Virgo/KAGRA 触发源——104 个 GW 命名源
  （G/S/GW 编号，含 3,259 条光变点）+ 2 个 trigger_instrument 被标为 LVK 的源
  （FRB20250206A、ZTF19acyldun，疑为 meta 的 LVK 追击通报污染所致）；备份在
  `catadata/backups/lvk_purge_20260826/`。同日还清理了与自身 ID 规范化相同的冗余别名
  （1,396 个目标、1,399 条，备份 `catadata/backups/alias_cleanup_20260826/`）
- **2026-08-26 脚本与备份清理**：应用户要求，全部数据导入脚本已从 `backend/tools/` 删除
  （catalog_merge、grbsn_photometry/spectra_import、gcn_llm_import、gcn_llm_new_events、
  gcn_extract/gcn_index/gcn_apply、tns_sync、ads_fill、fermi_lat_lc_import、
  rssgrbag_lc_import、burst_analyser_import、uvot_noc_import、uvot_new_grbs 及其报告文件），
  `catadata/backups/` 整个目录一并删除；**本文档其余章节对上述脚本/备份路径的引用仅作
  历史记录，这些脚本未随仓库发布**。连带修复：`routes/ingest.py` 原
  `from tools.catalog_merge import ang_dist` 已改为文件内联函数（4 行，小角近似），
  否则重启即 ImportError。tools/ 现仅剩
  check_fitting / check_relations_api / check_relations_fit / derive_prompt_params

### 8.12 transients 表系统清洗（2026-08-26）

依据一次针对 transients 表的系统审查报告（四轮审查：重复/坏行删除、字段修正、坐标与
t0 补全、GRB 编号关联；终态 `transients_cleaned.csv` 2794 行；审查报告来自独立的私有
工作目录，未随本仓库发布）对库执行：

- **字段更新 378 行**：t0 修正/补全 35、坐标补全/修正 300（含 IPN 误差盒中心）、
  触发仪器修正 19、红移清除/修正/类型 11、别名补充 58、tags 1。
  应用方式：仅当 DB 现值与原 CSV 一致时才改写（防覆盖期间新改动），0 冲突
- **合并 23 对**（XRF/GRB 双命名、重复登录、畸形 ID 等）：子表数据
  （lightcurves 等 6 表）改挂目标行，源名+源别名并入目标 aliases，
  目标 `extra_data.merged_from` 记录来源
- **删除 19 行**（官方撤回/非 GRB/幽灵编号：GRB050805A/B、GRB080060/070、
  GRB050309、GRB050402、GRB050727、GRB061109、GRB080420A、GRB091215、GRB070507、
  GRB091231、GRB171003A、GRB200801C、GRB070706B、GRB071110、GRB080410、GRB080628、
  GRB080705；连带其子表数据）
- **暂缓后已执行的 4 对合并**（用户确认，2026-08-26）：
  XRF030723(26)→GRB030723(60)、XRF020903(18)→GRB020903A(71)、
  XRF080109(21)→GRB080109A(313)、XRF050522(11)→GRB050522(14)
- 结果：2840 → **2794** 个目标，与 transients_cleaned.csv 完全一致。备份
  `catadata/backups/transient_clean_20260826/`（transients 全表 + 受影响光变 171 行；
  该目录已随后续清理删除，见 §8.11 末条）
- 报告中"待人工复核"条目（第四节、六-5/六-6）未改动
- **光谱**（`backend/tools/grbsn_spectra_import.py`，源自 Open Supernova Catalog；该脚本未随仓库发布）：
  97 条光谱（10 个源，最多 GRB980425A 30 条）——**不进光变大表**，
  文件存 `catadata/spectra/<tid>/`，元数据登记在预留的 `spectra` 表
  （instrument、观测日期(MJD)、波长范围、observer/reducer/单位/来源）
- **API**：`GET /api/spectra?transient_id=X`（元数据列表）、`GET /api/spectra/<id>`（完整波长-流量数据）
- **前端**：详情页"光谱数据"标签页——左侧光谱列表（MJD/仪器/观测者，可多选对比、逐条纵向偏移、删除按钮），
  右侧波长-流量折线图；横轴为**观测者系波长**，有红移时上方副横轴显示**静止系波长**（λ/(1+z)）；
  支持**绝对流量/相对流量**模式切换：绝对模式仅显示绝对流量光谱（含误差条），相对模式全部显示（按各自中位数归一+用户偏移）；
  `flux_type` 存于 extra_data（absolute/normalized），用户上传时可选择
- **上传**：`POST /api/spectra/upload`（需登录）：两列（波长 流量）或三列（+流量误差）文本（# 头可声明元数据）、
  或 OpenSNSpectra 风格 JSON；服务端校验（≥10 点、波长 100–10⁷ Å、同名 409、文件名防穿越）后统一规范化 JSON 存储
- **删除**：`DELETE /api/spectra/<id>`（需登录），DB 记录与文件一并删除

### 8.13 静止系派生量与统计关系（v2.4 新增）

**派生量命名空间 `extra_data.derived`**：由 `backend/tools/derive_prompt_params.py`
从 `catalog_data`（各目录 params）+ `redshift` 统一计算静止系派生量，**不动 `catalog_data`**：

```json
"derived": {
  "sources": {"konus_wind": {"ep_rest": {"v": ..., "err": [...]}, "eiso": {...}}, ...},
  "best":    {"ep_rest": {"v": ..., "err": [...], "src": "konus_wind"}, ...},
  "grb_type": {"v": "I", "src": "minaev2020"},
  "computed": "2026-07-21"
}
```

- `sources`：按目录保留各目录的派生量；`best`：按内置优先级（如 `ep_rest` 取
  konus_wind → liang2023 → wang2022 → minaev2020 → …）选出的代表值，`src` 记录取值目录
- `grb_type`：I 型（短暴/并合）/ II 型（长暴/坍缩星）；文献有分类（minaev2020 等）用文献值，否则按 T90 判别
- 用法：`python3 derive_prompt_params.py`（dry-run 默认，只打印统计）/ `--apply` 写库；幂等可重跑
- 手动修改保护：网页端详情页编辑 derived 时，改动条目标 `"manual": true`、删除路径记入 `_manual_deleted`（墓碑）；重跑脚本时这些条目保留、被删路径不复活，其余条目照常重算（`preserve_manual()`，报告会打印"保留手动条目/删除墓碑"计数）
- 派生量键名规范见 `catadata/external/SCHEMA.md`（v2.4 新增 `ep_rest`/`lp_iso`/`tlag`/`variability`/`e_gamma`/`grb_type`/`spec_class`）

**统计关系 API**（`backend/routes/relations.py` + `backend/relations.json`）：

- `relations.json` 定义 6 个 2D 关系：`amati`（Ep,rest–Eiso）、`yonetoku`（Ep,rest–Lp,iso）、
  `ghirlanda`（Ep,rest–Eγ）、`lag_lum`（τlag–Lp,iso）、`var_lum`（V–Lp,iso）、`ep_alpha`（Ep,obs–α），
  各关系含坐标键/单位/log 轴标记/文献参考线（`lit`）
- `GET /api/relations`：关系定义列表 + 各关系当前可用的来源目录（扫描库内 derived 统计）
- `GET /api/relations/<name>/data?source=`：取数点；`source=best`（默认）用 `derived.best`，
  否则用 `derived.sources[<目录短名>]`；只返回 x、y 都有值的点，log 轴剔除非正值

**前端统计关系页**（`#/stats/relations`，`frontend/js/pages/relations.js`，全局统计页子页面）：

- 6 个关系平铺卡片；来源目录下拉切换（best / 各文献目录）；tag 筛选
- 点击数据点排除个体样本（按 关系:来源 记忆，可恢复）
- 特殊标记源（输入框填 GRB 名）以星形高亮显示
- log 空间 OLS 分组拟合（I 型 / II 型分组）+ 1σ 置信带 + 文献参考虚线
- 误差棒；hover 显示源名/数值/红移/数据来源；导出当前数据点 CSV

### 8.14 余辉拟合（VegasAfterglow）（v2.5 新增）

基于 VegasAfterglow 的正向激波余辉拟合子系统，从单源详情页"余辉拟合"标签页使用。

**架构**（`backend/fitting/`）：

- `engines/base.py`：拟合引擎注册表——引擎声明元信息（模型情形选项、默认先验、采样缺省）、
  校验配置、执行拟合；设计目标是**可继续添加更多拟合引擎**（新引擎实现同一接口并注册即可，
  前端配置区与 API 自动跟随）
- `engines/vegas_unified.py`：组合模型引擎，**唯一的余辉拟合引擎**（四个物理轴
  共 216 种可用组合 + 联合物理约束，自定义 MCMC 外壳，见 §8.21；v2.5 时期的旧引擎
  `vegas_fs` 已于 2026-08-31 退役下线，历史任务结果仍可查看，只是不能再提交）
- `jobs.py`：单 worker 异步任务队列（后台线程串行执行）；任务记录写 `fitting_results` 表，
  产物存 `backend/fitting_store/<transient_id>/<任务id>/`
  （`chain_record.h5` 采样链、`corner.png` 角图、`lc_model.json` 模型光变 + 1σ 置信带、`run.log`）

**依赖**：运行环境需 `pip install "VegasAfterglow[mcmc]"`（2.0.6，
附带 bilby / emcee / dynesty / corner）。

**模型情形**：四个并列物理轴自由组合——成分拓扑 `model`（fs / fs_rs / fs_inject /
frs_plus_fs）× 喷流结构 `jet`（tophat / gaussian / powerlaw / two_component /
step_powerlaw / powerlaw_wing / uniform）× 环境介质 `medium`（ism / wind）×
宿主消光 `extinction`（none / smc / lmc / mw），共 224 种；去掉核心包不支持磁星注入的
fs_inject + powerlaw_wing 8 种，可用 216 种，每种组合的先验模板对应
`fitting/vegas_unified/prior_configs/<case>.json`（唯一来源，case 名按
`<model>[_<jet>][_wind][_<extinction>]` 拼出，详见 §8.21）。

每条先验形如 `{min, max, scale: log|linear|fixed}`，前端先验编辑器可逐条修改或固定；
采样设置含 `nsteps` / `nburn` / `top_k` / `npool` / `seed`（默认 20000 / 6000 / 10 / 4 /
42，npool 上限 8；网页上请按数据量调小步数）。

**数据准备**（`jobs.prepare_data()`）：

- 优先取银消改正列（`gext_corr=true` 时用 `flux_density_gextcor`，单位 mJy）；
  否则按原始值换算——AB 零点 `mag = 16.4 - 2.5·log10(f_mJy)`，Vega 系统先加 `vega2ab` 转 AB
- 频率按 `filters.wavelength` 由 Å 转 Hz（ν = c/λ）；不在 filters 表的波段尝试从波段名
  解析——射电频率标注（`'4.8GHz'` / `'250MHz'` 等，Hz–THz）直接换算，X 射线光子能量标注
  （`'10keV'` 等，eV–GeV）按 ν = E/h 换算；都无法解析的波段才跳过
- 上限点按 `f=0、err=上限值` 传入；`discard=true` 的点排除

**API**：见 §五 `/api/fitting/...`（engines / jobs 增删查 / 产物下载）。

**前端**（`frontend/js/pages/fitting_tab.js`，详情页"余辉拟合"标签页）：

- 配置区：引擎选择、模型情形下拉、先验编辑器、采样设置
- 任务列表：状态轮询（pending / running / done / failed / interrupted）
- 结果区：参数估计表、模型光变叠加图（含 1σ 置信带）、角图、`chain_record.h5` 下载

**已知事项**：

- 任务在服务端进程内执行，**服务器重启会把 running / pending 任务标记为 `interrupted`**
  （记录保留，已写出的产物不丢），服务启动时由 `mark_interrupted()` 统一处理
- `npool` 默认 4、上限 8
- 验证脚本：`backend/tools/check_fitting.py`

### 8.15 TNS 交叉证认同步（v2.7 新增）

基于人工核对的 AJST ↔ TNS 映射表（57 条，未随仓库发布），仅访问表中给出的
TNS 对象网页执行同步：

- 脚本：`backend/tools/tns_sync.py`（dry-run 默认，`--apply` 写库，幂等可重跑；
  `--skip=<id,...>` / `--only=<id,...>` 过滤；TNS 页面与光谱缓存在 `backend/tools/tns_cache/`，
  请求基础间隔 3s，遇 429 限流指数退避重试；该脚本未随仓库发布，见 §8.11 末条）
- **坐标**：用 TNS 对象页 RA/DEC (J2000) 更新 ra/dec（六分仪解析 + 页面十进制坐标交叉验证，容差 1″），
  `pos_error` 统一 0.5 arcsec，`pos_ref` 指向 TNS 对象网页；坐标变动自动重算该源银消改正
- **别名**：缺失时补 TNS 名（无空格形式；裸年份显示名补 SN 前缀，如 `2001ke`→`SN2001ke`）
- **光谱**：拉取对象页公开 ASCII/ECSV 光谱，转换为项目统一 JSON 存 `catadata/spectra/<tid>/`
  并登记 spectra 表；ECSV 按头部声明换算波长至 Å，非 erg 系流量（如 electron 计数）按 OSC 惯例
  标记 `u_fluxes='Uncalibrated'`；`extra_data.source='TNS'`，并记录对象页/ASCII URL 与 TNS 备注
- 首批（2026-08-04）：56 源坐标更新（GRB230812B、GRB130702A 原坐标错误，偏移 38°/10.5° 属修正）、
  26 源别名新增、14 条光谱入库（11 个源，spectra 表 97→111）；
  GRB200826A 原 SN2020bvc 成协有误（相隔 161°），按用户指定改对应 SN 2020scz（偏移仅 95″）

### 8.16 STDWeb 测光数据接入（ingest）API（v2.8 新增）

供 STDWeb 上传测光数据的独立接入端点（`backend/routes/ingest.py`，设计细则见 STDWeb
项目的上传设计文档 §3；STDWeb 为独立项目，未随本仓库发布）。
与会话认证并存，**只认 `Authorization: Bearer <AJST_INGEST_TOKEN>`**（`hmac.compare_digest`
常量时间比较）；`AJST_INGEST_TOKEN` 未配置时整个 ingest API 返回 503。

**`GET /api/ingest/resolve`** — 解析目标源，只查不建：

- 参数：`name`（精确匹配 `transients.id` 或 `aliases`，大小写不敏感）与
  `ra,dec`（度，锥形检索）至少给一组；`radius` 角秒，默认 5.0
- 返回 `{"candidates": [{id, ra, dec, t0, aliases, distance_arcsec}]}`，按距离升序；无命中为空列表（非 404）

**`POST /api/ingest/photometry`** — 上传测光点（MJD/星等原始量，格式转换全部在服务端）：

```json
{"transient_id": "EP251202a",            // 或改用 ra,dec,resolve_radius 锥形解析
 "create_if_missing": false,             // true 时需附 new_transient {id,ra,dec,t0}
 "points": [{"mjd": 61011.614, "mag": 19.32, "mag_err": 0.08,   // 或只给 limiting_mag（上限点）
             "mag_system": "AB", "band": "r",
             "telescope": "...", "instrument": "...", "reference": "...",
             "extra_data": {"stdweb_task_id": 1234, ...}}]}
```

服务端流程：鉴权 → 解析源（名称/别名精确或锥形取最近，`resolved` 回显）→ t0 检查
→ 逐点处理（MJD→触发后秒数；星等存 `flux_density` + `flux_density_unit='mag'`；
`band` 小写归一化 + 常见变体匹配 filters 表，查不到照收仅 warning；
同 `(transient_id, band)` 桶内 `|Δt| < max(10s, 1e-4·|t|)` 且 `|Δmag| < 0.03`
判重跳过——上限点只比时间；单事务，任一点校验失败整批回滚）→
对每个新插入点调 `extinction.recompute_point`（异常仅记 warning 不回滚）。
`create_if_missing` 新建的源自动写 `extra_data.ingest_source='stdweb'`。

- 响应：`{transient_id, created_transient, resolved, inserted, skipped_duplicates, warnings, points}`
- 错误码（JSON `{"error": ...}`）：400 参数/校验错误；401 token 错误；404 源未找到；
  422 源缺 t0（不做隐式猜测）；503 ingest 未启用
- token 配置：`export AJST_INGEST_TOKEN='xxx'` 加入服务运行环境（如 systemd 单元的
  `Environment=` 或 shell 配置文件），重启服务生效（与 `AJST_CATALOG_PASSWORD` 同机制，见 §7.1/§7.2）

### 8.17 GCN 阅读工具（工具箱，v2.10 新增）

原 tkinter 桌面工具 `gcn_catalogue_tool_1.2.py` 的 Web 化集成，导航「工具箱 → GCN 阅读工具」进入
`#/tools/gcn`（`frontend/js/pages/gcn_tool.js` + `backend/routes/gcn.py`）。

- **存档**：GCN 整包存于项目内 `catadata/gcn/archive/<circularId>.json`（约 4.5 万期，路径配置
  `config.py: GCN_ARCHIVE_DIR`），与开发时所用的外部 ObsNotes 目录完全解耦
  （曾用于索引/抽取的 `backend/tools/gcn_index.py` / `gcn_extract.py` 未随仓库发布，见 §8.11 末条）
- **左栏浏览器**：期号跳转 / Prev / Next（id 列表客户端缓存）；JSON 展示复刻原工具——字符串内
  `\n` 展开为真实换行+缩进、数字红色加粗、key/true/false/null 着色；当前期正文自动提取
  GRB/EP 暴名 token 显示为候选按钮，点击即查库：命中加载源信息卡，
  未命中预填 ID 待新建；底部时间计算器（UTC/MJD 两格式，Δt 同时输出 s/min/h/day）
- **在线更新**：`POST /api/gcn/update`（登录）启动后台线程下载
  `https://gcn.nasa.gov/circulars/archive.json.tar.gz` → 临时目录解压校验 → 备份旧目录后替换
  （失败自动回滚）→ 清 id 缓存；`GET /api/gcn/status` 轮询进度，前端每 2s 轮询；
  status 同时返回 `archive_mtime`（存档目录修改时间，UTC），状态行常显「存档更新于 <时间>」
- **源信息卡**（右上，读写 transients 表）：id/aliases/ra/dec/t0/trigger_instrument/redshift/tags；
  RA/Dec 支持十进制度与 sexagesimal（`hh:mm:ss` / `12h34m56s` / 空格分隔；RA 按小时角 ×15，
  算法与 astropy `Angle` 一致并经 astropy 参考值逐例校验），输入框下方实时显示换算结果；
  加载（id 精确，404 走 search 别名容错）/ 新建（`POST /api/transients`）/ 保存修改（PUT）；
  「打开详情页」直链 `#/transient/<id>`
- **测光录入卡**（右下，写 lightcurves 表）：表单按「来源 / 时间 / 波段与流量 / 备注」分组排布；
  gcn_id 与 reference（`GCN <cid>`）随当前期自动填充；telescope/instrument 直写入库对应列。
  time 取数优先级（各时间量按各自单位 s/m/h/d 换算为秒后再算）：mid → (start+end)/2 →
  start+exposure/2，三种组合至少满足一种；time_err = (end−start)/2 或 exposure/2。
  星等（magnitude+system）与流量密度二选一（都填取星等）；原始 start/end/exposure 与 gcn_id
  存入该点 `extra_data`；保存走 `POST /api/lightcurves/batch`
- **关联光变记录面板**（底部整行，v2.13 新增）：打开某期 circular 时自动查询
  `GET /api/gcn/<cid>/related` 并列表展示，便于对照 GCN 结果实时核对。匹配规则按优先级：
  ①reference 精确——`reference` 含 `GCN<cid>`（正则 `gcn\s*#?\s*<cid>(?![0-9])`，兼容
  `GCN44171`/`GCN 44171`/`...(GCN11024)` 写法，期号后跟数字则不算）或 `extra_data.gcn_id == cid`；
  ②暴名模糊——正文提取的 GRB/EP token 命中库中源的 id（前缀）或别名（子串，含空格变体），
  取这些源的全部光变记录（去除已精确命中者，上限 400 条）。
  两级结果都只保留光学/红外/射电波段——X 射线/伽马等高能波段（`keV/MeV/GeV` 结尾的 band，
  如 `10keV`）通常不在 GCN 中报道，服务端查询时直接排除。
  面板表头支持点击排序（再点反向，第三次取消）；工具条提供筛选：源与 band 为下拉选择
  （选项取自当前结果集 distinct 值），reference 与 comment 为子串搜索（类网页 find，不区分大小写），
  另有「重置筛选」按钮。
  命中记录只涉及一个源时自动加载源信息卡；点击记录行（或「载入」按钮）把该条回填到
  测光录入卡进入编辑模式（时间优先用 extra_data 原始 start/end/expo 还原，缺失时
  mid=time、expo=2·time_err），此时「保存测光记录」变为「更新记录 #id」（PUT 覆盖；
  管理员可更新任意记录，普通用户仅可更新自己录入的记录，即 `source`=本账户），
  另提供「作为新记录保存」（POST 新增）与「取消编辑」；保存/更新后自动刷新关联面板

### 8.18 抠图取数（工具箱，v2.11 新增）

从图像（论文光变图截图等）提取数据点的纯原生 JS 工具，导航「工具箱 → 抠图取数」进入
`#/tools/digitizer`（`frontend/js/pages/digitizer.js` + 纯算法模块 `frontend/js/digitizer_core.js`；
算法参考 MIT 许可的 graph-digitizer）。

- **坐标轴标定**：图上依次打 X① X② Y① Y② 四个参考点并填入数据值；每轴独立选线性/对数
  （对数轴先取 log10 再插值）；星等反转轴由两点值序天然处理（上方点填小值）。
  点一律以**像素坐标**存储，数据坐标经标定变换实时换算——重新标定即整体修正
- **取点**：手动（单击加/拖拽移/删除模式单击删）与自动按颜色提取（3×3 均值取色，
  CIE Lab ΔE 容差滑块）：符号模式 = 掩膜 + 4-连通域 + 面积过滤取质心（离散数据点）；
  描线模式 = 逐列扫描掩膜、按与前一 y 的连续性选段取质心（连续曲线），列步长可调；
  **生成取点**：直线（2 端点）/样条（≥3 控制点）按图像像素步长采样成点
  （log 轴下屏幕等步长 = log 空间等间隔）；样条类型可选**自然三次**（Thomas 算法，
  光滑但可能过冲）或 **Catmull-Rom**（Hermite 形式 + 有限差分切线，**张力 0–1 用户可调**，
  越大越贴折线、越不易过冲）
- **框选范围（ROI）**：框选模式拖拽绘制（角柄调整/框内拖动/可取消）；自动提取仅作用于
  框内（掩膜裁剪 + 描线范围收窄）；「删除框内点」批量删除当前数据集框内点（可撤销）
- **数据集**：一张图可建多个数据集（各自命名/颜色/显隐/点数），自动提取结果进入当前集；
  操作级撤销（快照栈深 20）/清空当前集
- **输出**：导出 CSV（dataset,x,y）；写入 AJST（登录）——搜索选源（显示 t0），
  **数据类型二选一**：
  - *光变点*（lightcurves 表）：X 轴格式选相对时间 s/m/h/d（换算秒）或 MJD（用源 t0 换算
    `(mjd−t0_mjd)×86400`，源无 t0 禁止）；每个数据集单独指定 band（必填）、Y 类型
    （星等 AB/Vega 或 mJy/uJy/Jy/cgs）、上限开关；公共字段 reference/telescope/instrument/comment
    （**comment 缺省自动填 "Digitizer"**）；落库点 `extra_data.digitizer=true` 标记来源
  - *光谱*（spectra 表，走 `POST /api/spectra/upload`）：每个数据集 = 一条光谱（可命名文件名，
    源内唯一）；X 轴单位 Å/nm/μm 统一转 Å（观测者系）；流量类型绝对/归一化/**AB 星等**/
    **ST 星等**（星等逐点换算为绝对流量 erg/s/cm²/Å 入库：AB 按 `F_ν=10^(-(m+48.60)/2.5)`、
    `F_λ=F_ν·c/λ²`，ST 按零点 `F_λ=3.6307805e-9 erg/s/cm²/Å` 无需波长；两者均经 astropy
    逐点比对验证）；元数据
    instrument/MJD/observer/reducer；客户端先校验 ≥10 点、波长 100–1e7 Å
  预览（换算后前 5 行 + 总数）→ 确认 → 写入（光变按 500 条/批 `POST /api/lightcurves/batch`，
  光谱逐条 upload）
- **画布交互**：滚轮以光标为中心缩放、右键/中键拖拽平移、状态栏实时显示光标处数据坐标；
  工作状态存于模块级变量，会话内切换页面不丢失（刷新页面即清空）
- `digitizer_core.js` 为纯函数模块（`makeCalibTransform` / `rgbToLab` / `buildMask` /
  `traceLine` / `detectSymbols` / `avgColorLab` / `sampleLine` / `makeNaturalSpline` /
  `makeCatmullRom` / `sampleSpline`），Node 单测覆盖（log 轴几何中点、Lab 已知值、
  合成掩膜描线/连通域、样条过节点与光滑性、CR 张力解析值、直线/样条采样端点与点数等断言）

### 8.19 经验函数光变拟合与绘图增强（v2.12 新增）

**经验函数拟合**（`POST /api/lightcurves/fit_model`，无需登录，同步最小二乘，结果不落库）：

- 请求体：`{"model": "pl"|"bpl"|"sbpl", "points": [{"t","f","ferr"}...], "bounds": {"tb": [lo, hi]}}`
  （`bounds` 可选，bpl/sbpl 拐点 tb 的预设范围（秒），与数据范围取交集）
- 函数形式（前端 `detail.js fitModelFlux` 为严格镜像）：
  - `pl`：`F = A·t^(−alpha)`，最少 3 点
  - `bpl`：分段幂律，tb 处连续，`A = Fb·tb^alpha1`，最少 5 点
  - `sbpl`（v2.12）：平滑断裂幂律 `F = Fb·[(t/tb)^(n·alpha1)+(t/tb)^(n·alpha2)]^(−1/n)`，
    平滑因子 n>0（越大越尖锐，n→∞ 退化为 bpl），最少 6 点；后端用 logaddexp 数值稳定实现
- 内部以拐点/参考时刻锚定再参数化（Fb/tb），`scipy.optimize.least_squares` 带界求解，
  参数误差由 Jacobian SVD 近似协方差 + 链式法则变换给出
- 前端入口：详情页光变曲线标签下方「添加拟合」行（选 bpl/sbpl 时出现 tb∈[min,max] 预设范围输入）

**距离模数**：`Transient.to_dict()` 新增 `distmod` 字段（astropy Planck18 宇宙学，
`Planck18.distmod(z)`，无红移为 null），列表与详情 API 均返回。

**Vega→AB 绘图转换**（v2.12 修复）：此前 Vega 系统星等在详情页「原始」模式与多源对比页
被当作 AB 直接绘制；现在两处绘图（及拟合取数）统一先做 `mag += filters.vega2ab[band]`
再换算 mJy。注意数据库原始列仍保留原系统，转换只发生在显示/拟合层与银消改正派生列。

**单源光变图新控件**（详情页光变曲线标签）：

- 波段显示：图下方勾选框面板（波段名前复选框 + 色点 + 全选/全不选按钮），替代 Chart.js
  内置图例的划线开关（内置图例已关闭）
- 顶部副轴：`顶部轴: 天/MJD/无`；MJD 由 T0 + t/86400 计算（无 T0 时禁用）；手绘插件
  按显示单位取 1-2-5 规整刻度
- `静止系 t/(1+z)` 复选框（无红移禁用，仅时间轴除以 1+z）
- `Y: 绝对星等`（无红移禁用）：M = m_AB − distmod，线性反向星等轴（右轴 AB 视星等隐藏），
  星等空间误差 σ_m = (2.5/ln10)·σ_F/F
- 多源对比页同样新增 `Y: 绝对星等`（按各源红移分别计算，无红移源不显示）

**学术绘图风格**：刻度/轴标题/图例统一衬线字体（`theme.js ACADEMIC_FONT`，
Times/STIX/Noto Serif SC 回退链），轴线描边、网格弱化，覆盖详情页光变图、
多源对比图与拟合标签页图。

### 8.20 宿主星系与 pcigale SED 拟合（2026-08-29 新增）

- 数据模型：`host_galaxies` 表（§2.6），每源一行；随 `--dump` 往返 `info/<tid>.json` 的 `host_galaxy` 字段。
- 详情页「宿主星系」tab（`frontend/js/pages/hostfit_tab.js`）：宿主信息编辑（坐标/红移/红移类型）、
  多波段测光表（mag + err + mag_sys AB/Vega/ST + 来源，逐行勾选参与拟合）、拟合配置（固定红移 /
  测光红移网格）、任务队列（5s 轮询）、结果展示（best/bayes 参数表 + SED 图 + best_model.fits 下载）。
- 拟合子系统 `backend/hostfit/`：作业记录复用 `fitting_results` 表（model_name=`pcigale_host`，
  状态在 extra_data.status）；runner 生成 pcigale 输入（AB/Vega/ST → mJy 换算，缺 Vega2AB 或无
  pcigale 滤光片的波段跳过并警告）→ 子进程 `pcigale run`（基础链 sfhdelayed+bc03
  +dustatt_modified_CF00+redshifting，pdf_analysis；nebular / dl2014 为前端可勾选的可选模块，
  参数固定默认值；当前模型组合与网格模型数提示实时显示在前端）→ 解析 results.txt →
  matplotlib 生成 sed.png。产物在
  `fitting_store/<tid>/hostfit_<jobid>/`，不进数据仓库、全量重建清空（与余辉拟合一致）。
- 「写入宿主信息」：用户确认后把 bayes 参数写入 `derived`；测光红移模式同时回写
  redshift/redshift_err/redshift_type='phot'。
- 滤光片：`FilterDef.extra_data` 存 `pcigale_name`（如 `sloan.sdss.g`）与 `transmission`
  透过率曲线（SVO FPS 拉取，脚本 `scripts/fetch_svo_filters.py`；pcigale 2025.0 的
  `pcigale-filters add` 在 numpy≥2 下需 np.trapz→trapezoid shim，脚本已内置）。
- 权限：查看公开；宿主信息编辑/提交拟合/写回=登录；删除宿主/删除任务=管理员。
- 列表页「宿主」徽章 + `has_host` 筛选；新建事件页可折叠宿主字段；首页与统计子页
  `/stats/hosts`（覆盖率、M*/SFR 分布、宿主 z vs 暂现源 z 散点）。

### 8.21 组合模型余辉拟合引擎 vegas_unified（2026-08-30 新增；2026-08-31 起为唯一余辉拟合引擎）

接入作者修改版拟合工作区（`Vegas_run_unified.ipynb` / `run_batch_fit.py` 的网页化），
2026-08-31 起取代旧 `vegas_fs` 引擎成为唯一的余辉拟合引擎（历史 `vegas_fs:*` 任务
结果仍可查看，不能再提交；API 默认引擎已改为 `vegas_unified`），共用任务队列、
`fitting_results` 表与"余辉拟合"标签页。

- **模型组合由四个并列物理轴自由选择**（前端为四个并列下拉框，选中即按组合重建先验表）：
  成分拓扑 `model`（`fs` 单成分正向激波 / `fs_rs` 正向+反向激波 / `fs_inject` 正向激波+
  磁星注入 / `frs_plus_fs` 正反激波对+独立正向激波共用介质）× 喷流结构 `jet`
  （tophat / gaussian / powerlaw / two_component / step_powerlaw / powerlaw_wing / uniform，
  注册表 `custom_mcmc.JET_TYPES`）× 环境介质 `medium`（ism `n_ism` / wind `A_star`）×
  宿主消光 `extinction`（none / smc / lmc / mw，Pei 1992 + 线性参数 `A_V`）。
  共 224 种组合，去掉核心包不支持磁星注入的 **fs_inject + powerlaw_wing** 8 种，
  **可用 216 种**；不支持组合在前端有明确红字提示并禁止提交（后端 `validate_config`
  同样拦截）。通用约定：xi_e=1、on-axis（theta_v 固定 0，可在 JSON 放开）。
- **先验的唯一来源**：`fitting/vegas_unified/prior_configs/<case>.json`（随代码分发，
  216 个文件），case 名按 `<model>[_<jet>][_wind][_<extinction>]` 拼出（缺省轴
  tophat/ism/无消光不入名，如 `fs_rs_gaussian_wind_lmc`）；JSON schema：
  `{description, model, fit_engine, jet, medium, extinction, params, fitter_kwargs?}`，
  `fitter_kwargs` 仅放内置 Fitter 的成分开关（fs_rs 的 `rvs_shock`、fs_inject 的
  `magnetar`）。旧式 `{case: ...}` 配置仍兼容（`two_comp_fs` 为 `fs` + `two_component`
  的别名）。
- **联合物理约束**：`frs_plus_fs` 约束 `Gamma0 > Gamma02`；任何 `two_component` 喷流
  组合约束 `theta_c < theta_w 且 Gamma0 > Gamma0_w`（`custom_mcmc.CONSTRAINTS`，按
  成分拓扑名或喷流名均可命中）。内置 Fitter 的先验逐参数独立、无法表达联合约束，带约束
  的组合自动改走**自定义 MCMC 外壳**（`fitting/vegas_unified/custom_mcmc.py`，直接调
  `VegasAfterglow.Model` 构建似然、emcee 驱动，0.7·DEMove+0.3·DESnookerMove，
  线性流量 chi2 与 2.0.6 内置 Fitter 一致）；其余组合走内置 Fitter（按先验 JSON 的
  `fit_engine` 分派）。注意自定义外壳情形只用单色流量密度数据。
- **采样设置**含 `seed`（默认 42）；默认 nsteps/nburn = 20000/6000
  （与修改版工作区一致，网页上请按数据量调小）。
- **产物**在 `fitting_store/<tid>/<job_id>/`：`chain_record.h5`（fitter 路径为 bilby
  格式，custom 路径为完整链+数据+配置，均可用 `custom_mcmc.load_chain_h5` 读回重画）、
  `corner.png`、`lc_model.json`（同 §8.14 前端契约）、以及 custom_mcmc 风格的
  `metrics.txt` / `lc_plot.png`（错位分波段+成分虚线拆分）/ `lc_ratio_plot.png`
  （+data/model 比值子图）；后三种的下载/查看通过
  `/api/fitting/jobs/<id>/files/{metrics,lc_plot,lc_ratio}`。
- 任务记录命名 `vegas_unified:<case>`（`BaseEngine.model_label()`，jobs.py 优先采用）。
- 前端：配置卡为四个轴下拉（含中文标签）、当前组合的描述/实际拟合引擎/联合约束提示、
  seed 输入；先验表按组合从 `priors_by_case` 整套重建（schema 约 230KB，页面加载一次）。
- 源码出处注释在 `custom_mcmc.py` 模块头；与原版差异仅：去掉 dataloader 依赖
  （数据由 `jobs.prepare_data()` 供给）、`run_mcmc` 增加 `n_workers` 形参。
  修改版工作区与 vendored 副本各自独立演化，同步靠人工拷贝（本次同步至工作区
  2026-08-31 状态：216 个组合先验 JSON、四轴 schema）。

---

## 九、关键技术依赖

| 组件 | 版本 | 用途 |
|---|---|---|
| PostgreSQL | 14 | 数据库引擎 |
| Python | 3.12 | 后端 |
| Flask | 3.x | Web 框架 |
| SQLAlchemy | 2.x | ORM |
| psycopg2 | 2.x | PostgreSQL 驱动 |
| astropy / dustmaps / dust_extinction | — | 银河系消光改正（CSFD + P92） |
| VegasAfterglow[mcmc] | 2.0.6 | 余辉正向激波拟合（含 bilby/emcee/dynesty/corner，见 §8.14） |
| JavaScript ESM | — | 前端模块系统 |
| Bootstrap | 5.x | UI 框架 |
| Chart.js | 4.x | 图表 |
| Aladin Lite | — | 天球图 |

---

## 十、常见问题

**Q: 网页端改了数据，为什么文件没同步？**
A: 网页端只写数据库。需要同步到文件跑 `python3 etl.py --dump`

**Q: 改了 CSV 文件，为什么不生效？**
A: 跑 `python3 etl.py --sync` 同步到数据库。或全量 `python3 etl.py`

**Q: 数据库重启后数据还在吗？**
A: 在。PostgreSQL 是持久化存储，重启不丢

**Q: 密码不对怎么办？**
A: 登录需用户名 + 密码。管理员用户名为 `admin`，其密码在首次建库时取环境变量
`AJST_CATALOG_PASSWORD`（**必须显式设置；未设置时每次启动随机生成**，随机密码见启动环境/日志）；
之后密码以 `users` 表为准，修改方式见 §7.5。普通用户密码由管理员在 `/admin` 后台重置。

**Q: `discard = Y` 会删除数据吗？**
A: 不会。此列标记为后处理排除依据，当前所有 API 都原样返回全部数据点

**Q: 修改了后端代码后排序/功能不生效？**
A: 必须重启 Flask 进程：`systemctl --user restart ajst-catalog`（或手动运行时 `fuser -k 5000/tcp` 后再启动）

---

## 十一、版本历史

### v2.13（2026-08-25）— GCN 阅读工具 × 光变数据库深度融合

- 新增 `GET /api/gcn/<cid>/related`：reference 精确（`GCN<cid>` 写法正则 + `extra_data.gcn_id`）
  与暴名模糊（GRB/EP token → 源 id/别名）两级匹配，返回关联光变记录
- GCN 阅读工具新增底部整行「库中关联光变记录」面板：打开 circular 自动展示匹配记录；
  单源命中时自动加载源信息卡；点击记录回填测光录入卡进入编辑模式，
  可选择「更新记录 #id」（PUT 覆盖）或「作为新记录保存」（POST 新增），保存后自动刷新面板
- 关联面板只显示光学/红外/射电波段（服务端排除 `keV/MeV/GeV` 高能波段）；
  表头点击排序（升/降/取消三态）；工具条筛选：源、band 下拉选择，reference、comment 子串搜索
- 光变 PUT 权限放宽：普通用户可更新自己录入的记录（`source`=本账户），他人记录仍仅可扣点；
  详情页数据表同步开放——录入者的记录显示行内编辑按钮（`detail.js canEditLc`），其余记录仅扣点
- 详情页数据表易用性：新增「列显示」勾选面板（22 个数据列可单独显隐，localStorage 持久化；
  默认为紧凑子集——时间/时间误差/波段/流量/误差/单位/星等系统/银消/上限/银消量/望远镜/仪器/引用/备注，
  其余按需勾出；仅影响页面显示，`/api/export/lightcurves/<tid>` 导出始终为全列完整版）；
  表格新增首列行标记复选框，勾选整行橙色高亮便于对比定位（表头复选框全标/全清，纯前端状态不写库，切换源时清空）
- 全部光变图统一新增「误差棒」显示开关（默认开）：单源详情光变曲线（图头复选框）、多源对比图、
  余辉拟合标签页的数据选取预览图与拟合结果图（两处共用开关状态）；开关只影响绘制层
  （`errorBar` 插件 `beforeDatasetsDraw` 早退 + `chart.update('none')` 无动画重绘），不改动数据；
  上限点与模型/拟合线本就不画误差棒，保持不变
- 功能细则见 §8.17 末条

### v2.12（2026-08-18）— 光变图增强：Vega→AB 绘图修正 / SBPL 拟合 / 副轴与绝对星等 / 学术风格

- **Vega→AB 转换启用**：详情页原始模式与多源对比页绘图（及经验函数拟合取数）统一先做
  Vega→AB 转换（`mag += vega2ab[band]`），修复 Vega 测光点被当 AB 绘制的问题
  （GRB251025B 的 I/R/V 波段即此情况）；数据库原始列保持原系统不变
- **波段勾选面板**：单源光变图波段开关从内置图例划线改为图下方勾选框 + 全选/全不选
- **SBPL 拟合**：`fit_model` 新增 smoothly-broken-powerlaw（平滑因子 n），bpl/sbpl 支持
  tb 拐点预设范围（请求体 `bounds.tb`，前端拟合行新增对应输入）
- **顶部副轴**：单源光变图新增 day 轴（默认）与 MJD 轴（T0 计算，无 T0 禁用）
- **静止系与绝对星等**：单源光变图新增静止系 t/(1+z) 选项（与对比页一致）；
  单源图与对比图均新增绝对星等 Y 模式（`Transient.to_dict` 新增 `distmod` 字段，
  astropy Planck18；无红移时选项禁用/源不显示）
- **学术风格**：衬线字体 + 轴线描边 + 弱网格（`theme.js ACADEMIC_FONT/academicFonts`），
  覆盖详情页光变图、多源对比图、拟合标签页图
- 功能细则见 §8.19

### v2.11（2026-08-14）— 抠图取数（工具箱第二条目）

- 新增 `#/tools/digitizer`：从光变图截图提取数据点的原生 JS 工具（功能细则见 §8.18）——
  线性/对数轴标定（星等反转轴天然支持）、手动取点、按颜色自动提取（CIE Lab 掩膜 +
  连通域符号模式 / 连续性描线模式）、多数据集、撤销、CSV 导出、**直写数据库**
- 写库支持两种数据类型：光变点（X 轴相对时间 s/m/h/d 或 MJD→源 t0 换算；每数据集独立
  band/Y 类型/上限标记；comment 缺省自动填 "Digitizer"，落库点 `extra_data.digitizer=true`）
  与光谱（每数据集一条光谱，X 轴 Å/nm/μm 统一转 Å，绝对/归一化流量，走 spectra upload API）；
  光谱 Y 轴支持 AB 星等，入库前逐点换算为绝对流量 erg/s/cm²/Å（经 astropy 比对验证）；
  框选范围（ROI：自动提取限框内/批量删框内点/可调整取消）、直线与自然三次样条插值生成取点
- 新文件：`frontend/js/digitizer_core.js`（纯算法，17 项 Node 断言）、`frontend/js/pages/digitizer.js`；
  纯前端改动，无后端修改
- 选型说明：WebPlotDigitizer 为 AGPL v3（网络服务触发 copyleft），本工具为自研替代，
  规避许可传染；算法思路参考 MIT 许可的 graph-digitizer

### v2.10（2026-08-11）— 工具箱 + GCN 阅读工具

- 顶部导航新增「工具箱」下拉菜单（`frontend/index.html`），首个条目「GCN 阅读工具」→ `#/tools/gcn`
- 原 tkinter 工具 `gcn_catalogue_tool_1.2.py` Web 化（功能细则见 §8.17）：
  GCN circular 浏览器（跳转/翻页/JSON 数字高亮/`\n` 展开）、暴名 token 候选直查数据库、
  源信息卡直读写 transients 表、测光录入卡写 lightcurves 表（time 三级回退取值，原始量存 extra_data）、
  时间计算器、NASA 整包在线更新（后台线程 + 状态轮询）
- GCN 存档复制进项目 `catadata/gcn/archive/`（45212 个 JSON，与开发时的外部目录解耦）
- 新增 `backend/routes/gcn.py`（`/api/gcn/ids|/<cid>|/status|/update`），`config.py` 新增 `GCN_ARCHIVE_DIR`
- 后续细化：源信息卡 RA/Dec 支持 sexagesimal 格式（经 astropy 参考值校验）；
  `/api/gcn/status` 新增 `archive_mtime`（状态行常显存档更新时间）；
  测光录入卡新增 telescope 字段并按「来源/时间/波段与流量/备注」分组重排布局

### v2.9（2026-08-07）— 数据表批量删除 + CSV 上传（列映射导入）

- **多选批量删除**：详情页数据表行首新增复选框列 + 表头全选 + 「删除选中」按钮（登录后可见），
  确认后逐条调 `DELETE /api/lightcurves/<id>`，成功/失败计数 toast 汇总；排序重绘会清空勾选
- **上传数据表**：新模块 `frontend/js/pages/lc_upload.js`，三步弹窗（modal-xl）：
  ① 选文件解析（CSV/TSV/空白分隔，分隔符自动检测可手选，引号感知切分，跳过空行与 `#` 注释行，
  表头自动判断可强制，前 10 行原始预览）→ ② 列映射（21 个数据库列各自选「不导入 / 上传表某列 /
  固定值」，表头名归一化 + 同义词表自动猜测初始映射，一次性映射不保存）→ ③ 预览校验
  （必填列 time/band/flux_density/flux_density_unit 检查、星等数据要求 mag_system、
  无效行统计并跳过、映射结果前 10 行预览）→ 按 500 条/批调 `POST /api/lightcurves/batch` 导入
- 时间约定：映射界面可选 time_unit（s/m/h/d 及常见别名），导入时前端统一换算为秒入库
  （`time`/`time_err` ×60/3600/86400，`time_unit='s'`），与全库时间约定一致
- 布尔列（gext_corr/upperlimit/discard/host_subtracted）接受 y/yes/true/1（大小写不敏感）为真，其余为假；
  其中 host_subtracted 空值导入为 NULL（未知），行内编辑/新增行下拉框含「未知」选项，PUT 传 null 即可置回未知
- 纯前端改动，后端无修改（复用既有 batch 创建与单条删除 API）
- **多源对比筛选**（2026-08-09）：对比页「选择事件对比」新增按名称/别名筛选输入框
  （大小写不敏感子串匹配，纯前端过滤，列表行显示别名；勾选状态不受筛选影响）

### v2.8（2026-08-08）— STDWeb 测光数据接入（ingest）API

- 新增 `backend/routes/ingest.py`（见 §8.16）：`GET /api/ingest/resolve`（名称/别名精确 + 坐标锥形，只查不建）与
  `POST /api/ingest/photometry`（Bearer token 鉴权 → 解析源/显式新建 → t0 检查 → MJD 转触发后秒 →
  星等/上限映射 → band 宽松归一化 → 点级去重 → 单事务入库 → 银消重算）
- 新增 `AJST_INGEST_TOKEN` 环境变量（`backend/config.py`），未配置时 ingest API 返回 503；
  与现有会话认证并存互不影响

### v2.7（2026-08-04）— TNS 交叉证认同步

- 新增 `backend/tools/tns_sync.py`：按人工核对的 TNS 映射表同步坐标（`pos_error=0.5″`、`pos_ref`=TNS 网页）、别名与光谱（见 §8.15；该脚本未随仓库发布）
- 首批 56 源坐标更新 + 26 源别名新增 + 14 条 TNS 光谱入库（11 个源，spectra 表 97→111）
- GRB200826A 成协更正：SN2020bvc（误，相隔 161°）→ SN 2020scz
- 光谱数据标签页增强：坐标范围设置（数字输入 + 拖拽框选缩放，复用 dragzoom.js 并新增 allowNonPositive 选项）；
  TNS 风格谱线对比标记面板（`frontend/js/spec_lines.js`，30 组谱线逐字取自 TNS 对象页，逐组 z/v_exp 可调，见 §6.5）

### v2.6（2026-08-02）— 光谱数据子系统 + 批次导入与数据治理

- **光谱子系统**：`spectra` 表正式启用（97 条，Open Supernova Catalog 经 GRBSNWebtool）；
  光谱文件存 `catadata/spectra/<tid>/`；API 新增 `GET /api/spectra`、`GET /api/spectra/<id>`、
  `POST /api/spectra/upload`、`DELETE /api/spectra/<id>`（见 §8.10）
- **前端"光谱数据"标签页**：多选对比、绝对/相对流量模式（相对模式按中值归一+逐条用户偏移）、
  误差条可开关、观测者系/静止系双横轴（λ/(1+z)，逐帧同步手画副轴）、上传（两列/三列文本或 JSON，
  服务端校验规范化）、删除；事件列表页新增"光谱"条数列；"余辉SED分析"占位标签页
- **本批次导入**（grbcata_source_2.md，见 §8.8-§8.10）：saxgrbmgrb/swiftgrbba/rssgrbag 目录参数、
  rssgrbag 140 个射电峰值点、Burst Analyser 142,653 个 XRT 10keV 点（新建 494 源）、
  GRBSNWebtool 15,106 测光点 + 28 个 SN 别名 + 27 个 `sn` 主标签
- **数据治理**：GRBSN 单位修正（time_unit/freq_unit/mag_unit 逐行换算）并重导；
  波段名变体统一映射到 filters 条目（UVOT/Johnson/Sloan 撇号/HST 写法，clear 类与 C_{r}→G 并记原波段名）；
  uvot 星等系统全部更正为 Vega；NaN 污染清理；52 行 uvot 真重复清理；
  Spitzer 9 个滤光片（SVO FPS，λeff）入库（filters 81 条）；
  波段覆盖图改为按数量取前 40 + 对数横轴；`per_page` 上限 100→10000（修复统计页截断）；
  清空字段 API 语义统一（null/空串=清空，必填字段保护）

### v2.5（2026-07-22）— 余辉拟合（VegasAfterglow）

- 新增余辉拟合子系统 `backend/fitting/`：`engines/base.py` 引擎注册表（可扩展多引擎）+ `engines/vegas_fs.py` VegasAfterglow 正向激波引擎 + `jobs.py` 单 worker 异步任务队列（见 §8.14）
- 模型情形：jet(tophat/gaussian/powerlaw) × medium(ism/wind) × 开关(rvs_shock/magnetar) × extinction(none/smc/lmc/mw)；先验模板内置、前端可改
- 运行环境安装 `VegasAfterglow[mcmc]` 2.0.6（bilby/emcee/dynesty/corner）
- 新增拟合 API：`/api/fitting/engines`、`/api/fitting/jobs`（提交/列表/详情/产物下载/删除）；任务记录启用 `fitting_results` 表，产物存 `backend/fitting_store/<源>/<任务id>/`
- 前端详情页"余辉拟合"标签页上线（`frontend/js/pages/fitting_tab.js`）：配置区（引擎/情形/先验编辑器/采样设置）、任务列表状态轮询、结果区（参数表/模型光变叠加+1σ 置信带/角图/h5 下载）
- 已知事项：服务器重启将 running/pending 任务标记 `interrupted`；`npool` 默认 4（上限 8）；验证脚本 `backend/tools/check_fitting.py`

### v2.4（2026-07-21/22）— GRB 瞬时辐射统计关系

- `catalog_data` 新增 5 个统计关系文献目录：`minaev2020` / `konus_wind` / `wang2022` / `liang2023` / `guidorzi2025`（见 §8.5）
- `catalog_merge.py` 新增 `insert_new` 机制：文献目录未匹配记录整条入库为新源（77 个 catalog-only 源，`comment` 标记）
- `extra_data` 新增 `derived` 命名空间（`sources`/`best`/`grb_type`/`computed`），由 `backend/tools/derive_prompt_params.py` 幂等生成（dry-run 默认，`--apply` 写库）
- 新增统计关系 API：`/api/relations`、`/api/relations/<name>/data?source=`（6 个 2D 关系，定义在 `backend/relations.json`）
- 前端新增统计关系子页面 `#/stats/relations`：关系平铺卡片、来源切换、tag 筛选、个体排除、星形高亮、log 空间 OLS 分组拟合 + 1σ 置信带、文献参考线、导出 CSV
- `catadata/external/SCHEMA.md` params 键列表新增 `ep_rest`/`lp_iso`/`tlag`/`variability`/`e_gamma`/`grb_type`/`spec_class`

### v2.3（2026-07-21）— 外部 GRB 目录数据

- 9 个外部 GRB 目录规范化合并进 `extra_data.catalog_data`（`catadata/external/<短名>/` + `SCHEMA.md` 统一规范 + `catalog_merge.py` 匹配合并）
- 红移按目录优先级填补（`redshift_ref` 记 `catalog:<短名>`）
- ETL 往返：`--dump` 导出 `extra_data`、导入时读回，全量重建不丢目录数据
- 详情页概览新增"外部目录参数"卡片

### v2.2 — UTC 时间约定

- 所有时间字段一律为 UTC 原值，任何通道不做时区转换

### v2.1 — 保留原始流量单位

- `flux_density` 保留 CSV 原始值与原始单位入库，不再强制转 mJy；统一换算在银消改正时完成
