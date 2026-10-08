# AJST 暂现源光变目录系统 — 技术文档

> 版本：2.25
> 更新日期：2026-09-23

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
│  js/pages/*.js    │                     │  port 27101           │
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
| `t0_ref` | TEXT | T0 引用（v2.23 新增，**纯元数据**，不参与任何 MJD 换算与绘图基准） |
| `t0_offset` | FLOAT | T0 偏移量（秒，正=向后/负=提前；v2.23 新增，纯元数据） |
| `t0_offset_ref` | TEXT | T0 偏移量引用（v2.23 新增，纯元数据） |
| `trigger_instrument` | VARCHAR(64) | 触发仪器 |
| `redshift` | FLOAT | 红移值 |
| `redshift_type` | VARCHAR(16) | `value` / `phot_z` / `spec` / `spec-host` / `upperlimit` |
| `redshift_ref` | TEXT | 红移引用（GCN 编号等） |
| `pos_error` | FLOAT | 位置误差 |
| `pos_error_unit` | VARCHAR(16) | 缺省 `arcsec` |
| `pos_ref` | TEXT | 位置引用 |
| `gext_ebv` | FLOAT | 该源坐标处 CSFD 尘图 E(B-V) 的**派生缓存**（2026-09-21 起，见 §四）；坐标写入时刷新，不随文件落盘，为 NULL 时改为现查尘图 |
| `gext_distmod` | FLOAT | 红移对应距离模数 μ（Planck18）的**派生缓存**（2026-09-21 起，见 §四）；红移写入时刷新，为 NULL 时现算 |
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
| `time` | FLOAT | **统一以秒为单位**；相对 T0 的秒数**画图加速缓存**（v2.23 起权威时间为 `mjd`，可随时由 mjd 重算覆盖） |
| `time_err` | FLOAT | 时间误差（秒） |
| `time_unit` | VARCHAR(8) | 固定为 `s`（ETL 自动统一） |
| `mjd` | FLOAT | **观测时间的唯一权威依据**（v2.23 新增，可空；源无 T0 时为 NULL、time 维持原值） |
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
| `gext_coeff` | FLOAT | 银河系消光系数 k_λ = A_λ/E(B-V) = Rv·P92(λ) 的**派生缓存**（2026-09-21 起，见 §四）；P92 定义域（10 Å–1e7 Å）之外为 `0`（不做改正），波长缺失/非法为 NULL |
| `description` | TEXT | 说明（望远镜/巡天名称） |
| `extra_data` | JSONB | 扩展字段 |

当前 72 个滤波器，涵盖 UV 到远红外（fuv 1548Å → wise-w4 220883Å）。

### 2.4 `tags` — 标签定义

| 列 | 类型 |
|---|---|
| `id` | BIGINT PK auto |
| `name` | VARCHAR(64) |
| `kind` | VARCHAR(8)，`main`=主标签 / `sub`=副标签，缺省 `main`（v2.23 新增；唯一约束改为 `(name, kind)`，唯一索引 `uq_tags_name_kind`） |
| `description` | TEXT，文字说明（新建 tag 时**必填**） |
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

每个源至多一行（transient_id UNIQUE）。详情页「宿主星系」tab 展示与维护；pcigale / prospector SED 拟合（见 §8.20）结果经用户确认后写入 `derived`。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK auto | |
| `transient_id` | VARCHAR(32) FK→transients CASCADE, UNIQUE | |
| `ra` / `dec` | FLOAT | 宿主坐标（度；v2.14 起 API 写入支持十进制度或时分秒字符串，入库统一转度） |
| `gext_ebv` | FLOAT | 宿主**自身坐标**处 CSFD 尘图 E(B-V) 的派生缓存（2026-09-21 起，见 §四）；坐标写入时刷新，为 NULL 时改为现查尘图 |
| `gext_distmod` | FLOAT | 宿主红移对应距离模数 μ 的派生缓存（2026-09-21 起，见 §四）；红移写入时刷新，为 NULL 时现算 |
| `redshift` / `redshift_err` | FLOAT | 宿主红移；光谱红移 err=0，测光红移有误差 |
| `redshift_type` | VARCHAR(16) | `spec` / `phot` |
| `photometry` | JSONB | `[{band, mag, mag_err, mag_sys(AB/Vega/ST), source, upperlimit, gext_corr}]`（v2.14 起支持 `upperlimit`；`mag_err` 可空——非上限且误差为空时后续处理按 σ=0.2 mag 计，0.2 不落库；v2.15 起 `gext_corr` **必填**——该行是否已做银河系消光改正，缺失时 PUT 返回 400，缺键的存量行下游按 false 对待，见 §8.24） |
| `derived` | JSONB | 采纳的拟合参数 `{m_star, sfr, age_main, Av_ISM, chi2, fit_at, job_id}`（v2.18 起 prospector 引擎结果另带 `engine` 键，见 §8.20） |
| `comment` / `source` | TEXT / VARCHAR(128) | |

落盘：随 `--dump` 写入 `info/<tid>.json` 的 `host_galaxy` 字段，导入时 upsert（见 §3.1）。

### 2.7 预留表

| 表名 | 用途 |
|---|---|
| `spectra` | 光谱文件元数据（已启用：filename/仪器/观测日期/波长范围/file_path→`catadata/spectra/<tid>/`、`spec_type` 列（transient/host/mix，非空默认 transient，随库存文件 JSON 持久化、重建可回读）、`wavelength_type` 列（v2.24，'vacuum'/'air'/NULL，处理路径默认转真空、文件存原始值，见 §8.32）、`parent_id` 自引用外键（v2.19，银消改正二级谱层级，ON DELETE CASCADE，见 §8.28）、extra_data 含 observer/reducer/flux_type/来源，见 §8.10、§8.15） |
| `images` | 图像文件（空，等待扩展） |
| `fitting_results` | 余辉拟合任务记录（v2.5 起启用，见 §8.14） |
| `extinction_corrections` | 银河消光改正记录（空，等待扩展） |

### 2.8 模板库存储（tmplibrary，随 AJST-Data 分发）

除 PostgreSQL 与 `catadata/` 的源数据文件外，模板库 × K 改正功能（见 §8.34）的数据放在
`catadata/tmplibrary/`：模板 manifest（`templates/<id>.yaml`）、冻结行集
CSV（`data/raw/<id>.csv`）、引擎曲面与 QC（`data/surfaces/<id>.{npz,qc.json}`）、滤光片
vendor 快照（`data/filters/`）。曲面/QC 虽可由引擎重建，但体量小且无本机绝对路径，
**整个目录随 `catadata/`（AJST-Data）进 git 分发**——克隆数据仓库即可用，不必逐台重建；
只有软删回收站 `data/.trash/` 与 `library.json.bak` 写入备份留在本地（`catadata/.gitignore`）。
`library.json` 是库的目录：登记每个模板的来源（出厂/向导/出厂·同源）、行集与输入指纹
（含第 7 项 `rows_sha256`）、域内率、μ 分解、三轴 stale 状态。写盘只经四个端点
（API-5 建面 / API-7 重建 / API-11 删除 / API-14 元数据编辑），读路径永不写；覆盖写是 tmp+rename 原子写并留
`.bak`。库根可用环境变量 `AJST_TMPLIB_DIR` 覆盖。API-14 改 manifest 走
`tmplib/edit.py` 直接改写：校验通过后 tmp+rename 整体重写 yaml（重写前经引擎
`TemplateSpec.from_yaml` 自校验）；历史版本由 AJST-Data 的 git 历史承担，不再单独备份。

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

# 数据库 → 文件回写（纯导出，不动任何文件；导出后报告孤儿文件/陈旧 CSV）
python3 etl.py --dump

# 导出 + 清理孤儿文件与陈旧 CSV（移到 backups/dump_prune_<时间>/，可逆）
python3 etl.py --dump --prune
```

> **`--dump` 覆盖范围**（2026-08-27 起）：`info/*.json`（含每源的 `articles` 研究文章字段）、
> `lc/*.csv`、`filters.json`。光谱文件由后端在上传/删除时同步维护，全量重建会自动
> 从 `catadata/spectra/` 重建 `spectra` 表索引。info JSON 中的 `articles` 字段在导入时对该源
> 做全量替换（缺该字段的旧格式文件不动库中条目），全量重建不丢文章数据。
>
> **一致性（2026-09-25 起）**：`--dump` 依旧**只写不删**（默认不动任何文件），但会**报告**
> 孤儿文件（有 info/lc 文件而库里没有该源）与陈旧 CSV（库里该源 0 点而文件仍有行）——
> 这两类会被下一次全量重建或 `--sync` 当成“新源 / 旧点”重新灌回库（`needs_update()` 对
> 库里不存在的源返回 True；`from_dump` 对 0 点源跳过写 CSV），所以删除源或删光点之后要清理。
> 清理：`python3 etl.py --dump --prune`（移到 `backups/dump_prune_<时间>/`，可逆；库里 0 源时
> 一律拒绝，孤儿数超过 `max(50, 现有源数×0.2)` 时拒绝，需 `--prune-force`）。只读自检：
> `scripts/check_db_file_sync.py`（退出码 1 = 有差异；`scripts/preflight.sh` 第 10 组会跑）。

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
  "T0_ref": null,
  "T0_offset": null,
  "T0_offset_ref": null,
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
- `T0_ref` / `T0_offset` / `T0_offset_ref`（v2.23 起）：T0 引用 / T0 偏移量（秒，正=向后/负=提前）/ 偏移量引用，纯元数据，不参与任何换算
- **v2.30 起规范键恒在**：dump 时所有规范字段（含 `pos_error_unit`、`extra_data`、`articles`、`host_galaxy`）空值显式写 `null`（列表写 `[]`），不再省略键；导入端把「键缺失」与「键 = null」等价处理，旧格式文件可正常导入。对外数据契约的权威定义在数据仓库 `SCHEMA.md`（含校验器 `tools/validate.py` 与贡献流程 `CONTRIBUTING.md`）

### 3.5 CSV 文件格式（`catadata/lc/<id>.csv`）

```
time,time_err,time_unit,mjd,band,flux_density,flux_density_err,flux_density_unit,mag_system,...
4665.6,300.0,s,60479.554,atlas-c,15.6,0.2,mag,AB,...
```

注意：CSV 中的单位是**原始单位**，导入时仅时间统一为秒，流量/星等保留原始单位入库。
`mjd` 列（v2.23 新增，`time_unit` 之后）是观测时间的**唯一权威依据**；旧 CSV 无此列可正常导入（导入时按 t0+time/86400 补齐），给 mjd 不给 time 时也会自动重算 time。

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
- **适用条件**：源有 RA/Dec 坐标 + 波段在 `filters` 表中且有效波长落在光学/紫外/红外窗口（1000 Å–1 mm）内（keV/GHz 等非光学波段自动跳过，计入 `skipped_not_optical`）+ 数据为有效星等或正的流量密度；run/clear 响应附带 `note` 说明该窗口
- **自动重算**：数据点被修改（流量/波段等）、源坐标变动、滤波器波长或 Vega2AB 变动时，已改正的数据点自动重算；条件不再满足（如坐标被清空）时自动清除改正结果
- **清除**：`POST /api/extinction/clear`（参数同 run）
- ETL `--dump` 导出的 CSV 含 `mag_Gextcor` / `mag_Gextcor_err` 列，可随文件回导

### 派生量缓存（2026-09-21，v2.21）

消光改正里有两类与"源"无关、却被反复重算的量，现已落库缓存，避免每次请求重算：

| 缓存 | 位置 | 含义 |
|---|---|---|
| `filters.gext_coeff` | `filters` 表 | k_λ = A_λ/E(B-V) = Rv·P92(λ)（P92 定义域 10 Å–1e7 Å 之外写 `0`＝不做改正；波长缺失/非法写 NULL） |
| `transients.gext_ebv` | `transients` 表 | 该源坐标处的 CSFD E(B-V) |
| `host_galaxies.gext_ebv` | `host_galaxies` 表 | 宿主**自身坐标**处的 CSFD E(B-V) |
| `transients.gext_distmod` / `host_galaxies.gext_distmod` | 两张表 | 红移对应距离模数 μ（Planck18），2026-09-21 起 |

- 三者都是**派生缓存**，不是权威数据：`gext_coeff` 随滤波器新建/改波长自动维护；`gext_ebv` 在坐标写入时刷新
  （transients PUT、hosts PUT、ingest 新建源、`POST /api/transients`、ETL 导入）。
- **读路径对 NULL 一律回退现算**：`gext_coeff` 为 NULL → 按波长现查 P92；`gext_ebv` 为 NULL → 按坐标现查 CSFD 尘图。
  因此**不跑回填脚本也永远正确，只是慢**（宿主统计会逐宿主重查尘图与滤波器表）。
- **不变式（务必遵守）**：`gext_ebv` 的取值坐标必须与测光改正所用坐标**同源**——宿主有自身 ra/dec 时用
  `host_galaxies.gext_ebv`（为 NULL 则按宿主坐标现查），只有坐标确实回退到暂现源时才用 `transients.gext_ebv`。
  宿主位置与源位置可相差角分量级、E(B-V) 不同，混用会取到错位置的尘柱（实测 72″ 偏移处 g 波段 ΔA_g 可达 0.07 mag）。
  `hostfit` 的银消改正同此口径。
- `extinction.filter_meta()` 是滤波器元数据的**进程内缓存**（只取 id/wavelength/vega2ab/gext_coeff 四列，
  60 s TTL + 滤波器写入口显式失效），避免宿主统计里每个宿主都重查整张 `filters` 表（含 transmission JSONB 解码）。
  经 `/api/filters` 写接口修改会立即失效；**直接 SQL 改库**最多 60 s 后生效，也可重启服务或跑回填脚本。
- **不随文件落盘**：`--dump` 不导出这些列（`catadata/` 文件里没有它们），全量重建后全部为 NULL——功能正常
  （读路径回退现算），要恢复缓存与查询速度跑一次回填脚本（见 §7.3）。
- **距离模数 μ**：`gext_distmod` 只依赖 astropy（Planck18），与 dustmaps 无关；`models.distance_modulus()` 是
  进程内按 z 分桶缓存的字典，`prewarm_distance_modulus([z…])` 对未命中的 z 一次向量化求值（列表/统计接口先批量预热，
  避免逐行标量调用 astropy）。实测 763 个真实红移下，向量化实现与逐行标量实现结果完全一致。

## 五、REST API

所有接口返回 JSON，`POST`/`PUT`/`DELETE` 需要鉴权（Flask signed cookie session）。
权限分两级：**管理员**（`admin` 账户）可执行全部操作；**普通用户**（管理后台创建）可新增/上传数据、扣点（discard）、提交拟合任务、修改自己录入的光变记录（`source` = 本账户），其余删除/修改已有数据的操作返回 403（下表「权限」列中标注"管理员"的接口普通用户调用返回 403）。

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/transients` | 列表（搜索/筛选/分页/排序） | 否 |
| POST | `/api/transients` | 新建事件（`ra`/`dec` 支持十进制度或时分秒字符串，入库统一转度，见 §8.22） | 登录 |
| GET | `/api/transients/<id>` | 单源详情 | 否 |
| GET | `/api/transients/sub_tags` | 现役副标签列表（`{sub_tags:[...]}`，列表页副标签筛选用） | 否 |
| PUT | `/api/transients/<id>` | 更新基本信息（`ra`/`dec` 同上支持时分秒） | 管理员 |
| DELETE | `/api/transients/<id>` | 删除事件（级联删除） | 管理员 |
| GET | `/api/lightcurves?transient_id=<tid>` | 光变数据列表（可选 `band` / `telescope` 过滤，`sort` / `order` 排序，`page` / `per_page` 分页） | 否 |
| POST | `/api/lightcurves/batch` | 批量新增光变点（自动记录 `source`=当前账户） | 登录 |
| POST | `/api/lightcurves/fit_model` | 时变函数拟合（pl/bpl/sbpl/fred + tb 预设范围 + 多起点最小二乘与 emcee 后验，返回 param_cov/samples，不落库，见 §8.19） | 否 |
| PUT | `/api/lightcurves/<id>` | 更新单个光变点（普通用户可改自己录入的记录，即 `source`=本账户；他人记录仅可改 `discard` 扣点） | 登录 |
| DELETE | `/api/lightcurves/<id>` | 删除单个光变点 | 管理员 |
| DELETE | `/api/lightcurves` | 按 transient_id 删除 | 管理员 |
| GET | `/api/filters` | 滤波器列表（支持 `?sort=&order=`） | 否 |
| POST | `/api/filters` | 新建滤波器（body 可带 `curve` 一并获取透过率曲线，见 §8.23） | 登录 |
| PUT | `/api/filters/<id>` | 更新滤波器 | 管理员 |
| DELETE | `/api/filters/<id>` | 删除滤波器（body 必须带 `password`=当前管理员密码二次校验，错误/缺失返回 403，见 §8.26） | 管理员 |
| POST | `/api/filters/<id>/curve` | 为已有滤光片补录/替换透过率曲线（body 带 `curve`，同 §8.23 三种方式，会注册 pcigale） | 管理员 |
| GET | `/api/filters/pcigale_builtin` | pcigale 自带滤光片名列表（新增滤光片时映射用） | 登录 |
| POST | `/api/filters/parse_curve` | 校验两列透过率曲线文本（不落库，返回归一化曲线或含行号的错误） | 登录 |
| GET | `/api/filters/svo_search?q=` | SVO FPS 模糊搜索候选滤光片 | 登录 |
| POST | `/api/filters/svo_fetch` | 预览抓取指定 SVO ID 的透过率曲线（不落库不注册） | 登录 |
| GET | `/api/tags` | 标签列表（支持 `?kind=main|sub` 过滤） | 否 |
| POST | `/api/tags` | 新建标签 `{name, kind, description, color?}`（**description 必填**，缺 400） | 登录 |
| PUT | `/api/tags/<id>` | 修改标签描述/颜色；v2.24 起可改 `name`（(name,kind) 冲突 409） | 登录 |
| DELETE | `/api/tags/<id>` | 删除标签 | 登录 |
| POST | `/api/tags/register` | 批量幂等登记标签（内部用） | 登录 |
| GET | `/api/stats/overview` | 汇总统计 | 否 |
| GET | `/api/stats/hosts` | 宿主星系统计（覆盖率 / M*/SFR 分布 / 宿主绝对星等点；带 `ETag` + `Cache-Control: no-cache`，数据未变时返回 304，见 §8.30） | 否 |
| GET | `/api/relations` | 统计关系定义列表 + 各关系当前可用来源目录（见 §8.13） | 否 |
| GET | `/api/relations/<name>/data` | 取某关系统计数点（`?source=best` 或目录短名） | 否 |
| GET | `/api/extinction/status` | 银消功能可用性 + 覆盖统计 | 否 |
| POST | `/api/extinction/run` | 执行银消改正（`{}`全部源 / `{"transient_id"}`单源 / `{"lightcurve_id"}`单点） | 管理员 |
| POST | `/api/extinction/clear` | 清除改正结果（参数同 run） | 管理员 |
| GET | `/api/spectra` | 光谱元数据列表（`?transient_id=` 过滤） | 否 |
| GET | `/api/spectra/<id>` | 单条光谱完整数据（数值已统一强转） | 否 |
| GET | `/api/spectra/<id>/download` | 下载光谱数据文件（`#` 注释头元数据 + 两/三列空白分隔文本，Content-Disposition attachment，见 §8.28） | 否 |
| POST | `/api/spectra/upload` | 上传光谱（两列/三列文本或 JSON，服务端校验规范化） | 登录 |
| PUT | `/api/spectra/<id>` | 修改光谱类型 `spec_type`（transient/host/mix，DB 与库存文件同步写；v2.19 起传播到银消改正子行）；v2.24 起接受 `wavelength_type`（vacuum/air/留空，同样传播改正子谱，见 §8.32） | 管理员 |
| POST | `/api/spectra/<id>/gext_correct` | 光谱银河系消光改正（CSFD+P92+Rv3.1，幂等生成二级改正谱，见 §8.28） | 登录 |
| DELETE | `/api/spectra/<id>` | 删除光谱（记录 + 文件；删除原始谱级联删除其改正谱，见 §8.28） | 管理员 |
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
| GET | `/api/export/lightcurves/<tid>` | 导出光变 CSV（**全部列**，含 mjd；支持 `t_ref` 查询参数指定基准时刻：缺省/`t0`=源 T0，纯数字=MJD，或 ISO UTC 字符串，time 列按基准重算，响应头 `X-AJST-Tref` 回显） | 登录 |
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
| PUT | `/api/hosts/<tid>` | upsert 宿主信息 `{ra?, dec?, redshift?, redshift_err?, redshift_type?, photometry?, derived?, comment?}`（`ra`/`dec` 支持十进制度或时分秒；`photometry` 项可含 `upperlimit`，`mag_err` 可空，见 §8.22；v2.15 起每行必须显式携带 `gext_corr` true/false，缺失返回 400，见 §8.24） | 登录 |
| DELETE | `/api/hosts/<tid>` | 删除宿主信息 | 管理员 |
| GET | `/api/export/host_photometry/<tid>` | 导出宿主测光 CSV/JSON（含实时计算的改正后星等 `mag_gextcor` 列，见 §8.24） | 登录 |
| GET | `/api/hostfit/config` | 宿主 SED 拟合默认配置与可用波段（v2.18 起按引擎分节 `{pcigale:{defaults,modules,optional_modules,available_bands}, prospector:{defaults,optional_modules,available_bands}}`，前端兼容旧扁平结构，见 §8.20；v2.30 起新增 `versions:{pcigale?,prospector?}` 安装版本字段，引擎未安装时省略对应键） | 否 |
| POST | `/api/hostfit/jobs` | 提交宿主 SED 拟合 `{transient_id, mode, redshift?, grid, photometry, config}`（v2.18 起 config 新增 `engine`：`pcigale`（默认）/ `prospector`） | 登录 |
| GET | `/api/hostfit/jobs` | 任务列表（`?transient_id=`；v2.18 起简报带 `engine` 字段） | 否 |
| GET | `/api/hostfit/jobs/<id>` | 任务详情（含 best/bayes 参数与 `engine` 字段） | 否 |
| GET | `/api/hostfit/jobs/<id>/files/<kind>` | 产物：`results`/`sed_png`/`best_model`/`log`；v2.18 起新增 `corner` 类（prospector 角图） | 否 |
| DELETE | `/api/hostfit/jobs/<id>` | 删除任务及产物 | 管理员 |
| GET | `/api/sed/models` | SED 模型清单与参数 schema（动态渲染拟合表单用） | 否 |
| GET | `/api/sed/epochs` | 可用历元建议（`?transient_id=&min_bands=&dt_frac=`） | 否 |
| POST | `/api/sed/build` | 构建单历元 SED `{transient_id, t_sel, dt?, mode?, bands?, ...}`（同步） | 否 |
| POST | `/api/sed/jobs` | 提交 SED 拟合 `{transient_id, config:{model, t_sel|epochs, law?, ...}}` | 登录 |
| GET | `/api/sed/jobs` | 任务列表（`?transient_id=`） | 否 |
| GET | `/api/sed/jobs/<id>` | 任务详情（参数/χ²/BIC/derived/可信度元数据） | 否 |
| GET | `/api/sed/jobs/<id>/files/<kind>` | 产物：`sed_png`/`corner`/`result`/`sed_csv`/`series_csv`/`trl_png`/`series_png`/`h5`/`log` | 否 |
| DELETE | `/api/sed/jobs/<id>` | 删除任务及产物 | 管理员 |
| POST | `/api/sed/closure` | 闭包关系 α–β 诊断 `{alpha, alpha_err, beta, beta_err, q?}`（同步；q 为可选能量注入指数 0≤q<1） | 否 |
| POST | `/api/sed/closure_plot` | α–β 诊断图（PNG） | 否 |
| GET | `/api/sed/closure_relations` | 闭包关系系数表（Gao+2013 框架） | 否 |
| POST | `/api/sed/bolometric` | 伪玻尔兹曼光变 `{transient_id, epochs?, ...}`（同步，max_epochs=30） | 否 |
| GET | `/api/tmplib/config` | 模板库引擎自检（chromashift 版本/code 指纹/依赖下限/限额/能力面/vendor 对照；引擎不可用也恒 200 + `TL_ENGINE_UNAVAILABLE`）；含 `bank_bands`（滤光片库全量清单：band/mode/trust/lambda_pivot_A，前端波段下拉用——预测目标波段不限于模板实测节点波段，凡静频落在面覆盖内的库波段都可算） | 否 |
| GET | `/api/tmplib/guards` | 三轴 staleness 独立报告（引擎输入/库行指纹/滤光片 vendor；单轴故障不拖垮其余轴） | 否 |
| GET | `/api/tmplib/templates` | 模板列表 + 状态（含三轴 stale 概要、域内率；不含 rows_sha256 现算与 μ） | 否 |
| GET | `/api/tmplib/templates/<id>` | 模板详情：manifest 摘要 + QC 全文（含波段准入）+ μ 分解 + 第 7 项指纹活库比对 | 否 |
| POST | `/api/tmplib/templates` | 造模板向导建面（强制声明校验 → 取数 → CSV+manifest → 建面 → 实测域内率；id 冲突 409） | 登录 |
| POST | `/api/tmplib/templates/<id>/rebuild` | 重建面（重算域内率与三轴 stale） | 登录 |
| PATCH | `/api/tmplib/templates/<id>` | 元数据编辑（API-14，白名单：label/object_class/redshift/distance/validity/epoch_zero/citation_append；校验后整体重写 manifest yaml，历史版本由 AJST-Data git 承担，默认保存后立即重建） | 管理员 |
| DELETE | `/api/tmplib/templates/<id>` | 软删（默认）/硬删（`hard=true` 需管理员密码二次校验）；出厂模板影子条目不可删 | 管理员 |
| GET | `/api/tmplib/preview?transient_id=` | 造模板预检（行账/波段映射/系统与 gext 覆盖率/z 冲突/同事件多记录提示/域内预估） | 登录 |
| POST | `/api/tmplib/predict` | 单条模板曲线预测（K 改正/绝对星等/域判定/时间原点建议；429 不排队） | 否 |
| POST | `/api/tmplib/compare` | 批量对比（≤8 模板曲线 + ≤8 源，含 K 改正实测点；部分成功协议恒 200） | 否 |
| GET | `/api/tmplib/export.csv` / `.json` | 同 compare 参数的文件形式（同一批数据两种渲染；query 编码见下） | 否 |
| GET | `/api/tmplib/in_domain/<id>` | 单模板域内可答比例缓存值与逐带归因 | 否 |
| POST | `/api/tmplib/budget` | 单项误差预算（四项 + 逐带色标增益 + sed_residual 逐带表 + distance 单列；`n_draws∈{0}∪[3,200]` 默认 32，429 不排队） | 否 |
| GET | `/api/specphot/meta?spectrum_id=` | 光谱×滤光片首屏 meta：全部波段 + 曲线覆盖/注册表指纹；带 `spectrum_id` 追加该谱口径两键/流量中位数/波长框架/读侧统计/可配对锚点数 | 否 |
| GET | `/api/specphot/curve/<filter_id>` | 透射曲线点集（Å 升序、峰值归一）+ 登记口径 `curve_kind` | 否 |
| GET | `/api/specphot/health` | specphot 版本戳、计算闸门余量、结果缓存条目数、`deps{astropy, dust_extinction, dustmaps}` 可用性 | 否 |
| POST | `/api/specphot/parse` | 上传/粘贴光谱只解析（规范化数组 + `spec_hash` + 读侧统计；不落盘不入库）；文本与 `/api/spectra/upload` 同一语法；P2+ 起另收 `.fits`（`content_b64`）与 `.ecsv`（`text`），格式探测分发（F-76/T-81） | 登录 |
| POST | `/api/specphot/ebv` | 按坐标（HMS 或十进制度字符串）查银河 E(B−V)（CSFD+P92；图不可用 ⇒ 200 + `available=false`，不 500） | 登录 |
| POST | `/api/specphot/photometry` | S1 合成测光（**P1 已上线**）：库内谱或上传件 × 波段曲线的 S1 合成星等（photon/energy 加权、direct/anchored/model 定标、锚点集、银河消光、AR(1) 误差放大）；诊断六开关随 P2c/P3c 开放（501 `feature_disabled`） | 登录 |
| POST | `/api/specphot/continuum` | S2 连续谱拟合（**P2 切片 2b 已上线；P2b 起 pl2 + Davies 参数化自助上线**）：六模型（pl/pl_dust/pl2/bb/pl_bb/dbb）+ poly 基线、host_ext 三态（off/fit/prescribe）、模型比较（F 检验/ΔBIC；pl→pl2 走 Davies 参数化自助）、闭包三候选、de_reddened 纯派生曲线；写法见下 specphot 小节「P2 切片 2b（API-3 + 宿主消光三态）」与「P2b 切片」 | 登录 |
| POST | `/api/specphot/line` | S3 谱线测量（**P3 切片 2 已上线**）：三步向导的计算核（线区+`line_kind` 必选 U-30/E-09 → 基线 F-36 → 轮廓拟合 gauss1/gauss2/lorentz/voigt——voigt 仅当 R 可得，F-39 闸）；单线 `LineResult`（EW 三项分解/线流量与 depth 互斥空值/snr_res 门/自助对照）、`frame_gates`（W-25①② 两道独立的门）、`diagnostics.absorber_systems` 恒在场占位；`vel_*/z_fit` 恒 null（M-6 未复核，强提交 `velocity_output=true` ⇒ E-14）；写法见下 specphot 小节「P3 切片 2（API-4 + 三步向导 + 线表导出）」 | 登录 |

### 列表查询参数

```
GET /api/transients?search=EP24
                   &z_min=0.5&z_max=5.0
                   &tag=fxt
                   &sub_tag=L
                   &has_spectra=true
                   &t0_from=2024-01-01&t0_to=2024-12-31
                   &ra_min=100&ra_max=200
                   &dec_min=-30&dec_max=30
                   &has_z=true
                   &sort=redshift&sort=t0&order=desc
                   &page=1&per_page=50
```

支持排序的列：`id`, `ra`, `dec`, `redshift`, `t0`（光变列表 `/api/lightcurves` 另支持 `sort=mjd`）。
`sub_tag`（v2.23）按副标签筛选；`has_spectra=true` 仅显示有光谱数据的源；`t0_from`/`t0_to` 为 T0 日期范围（`t0_to` 含当天全天）。

### 鉴权

```bash
# 登录（用户名 + 密码；管理员账户为 admin）
curl -X POST http://localhost:27101/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的密码"}'

# 检查状态
curl http://localhost:27101/api/auth/status
# → {"authenticated": true, "username": "admin", "role": "admin"}
```

- 账户存于 `users` 表（密码经 werkzeug 哈希存储），首次启动自动创建管理员 `admin`，密码必须通过环境变量 `AJST_CATALOG_PASSWORD` 显式设置；未设置时每次启动随机生成。
- 管理员维护后台：`http://localhost:27101/admin`（仅 admin 可登录），可新增/修改/删除普通用户。
- 权限两级：**管理员**全部操作；**普通用户**可新增/上传数据、扣点（discard）、提交拟合任务、修改自己录入的光变记录（`source`=本账户），其余删除/修改已有数据返回 403。
- 光变表 `source` 列自动记录提交账户（ingest API 记为 `ingest-api` 或 body 指定的 `source`），`created_at`/`updated_at` 自动记录存入/最近修改时间（naive UTC）。

### 模板库 × K 改正（/api/tmplib）

13 个端点已并入上面的 API 表；实现见 `backend/routes/tmplib.py` 模块头注释与设计文档
`02_功能设计方案.md` §5（该设计文档为外部协作产物，不入库）。P4 新增的两个端点细则：

- `GET /api/tmplib/export.csv` / `export.json`（API-9，公开）：与 API-8 `compare` 同参数的
  文件形式。**GET query 编码**：`curves` / `sources` 各为一个 URL 编码的 JSON 数组串，
  元素形状与 API-8 请求体逐项相同（如 `curves=[{"template_id":"sn2006aj","band":"B","z":0.05}]`），
  `include_measured` 取 `0`/`1`（缺省 `1`）。两种格式是同一批数据的两种渲染（内部只算一次），
  CSV 头为 `#` 注释行（CLI 头模板 + 主机侧六项补充）+ §4.3 的 19 列。内存渲染、零写盘。
- `POST /api/tmplib/budget`（API-10，公开）：`{template_id, band, z?, mode?, n_draws?, seed?, n_times?}`
  → 引擎 ErrorBudget 四项（photometric/sed_residual/colour_term/distance）+ 逐带
  `gain_mag_per_dex`/`gain_source` + sed_residual 逐带表 + distance 项单列 + 引擎 warnings 透传。
  `n_draws ∈ {0}∪[3,200]`（0 = 只算系统项，photometric 项缺席而非 0）。
  **默认 n_draws=32**（UNVERIFIED-2 实测：n_draws=200 每模板 13.7–18.1 s、n_draws=32
  每模板 2.4–3.1 s，九模板 n_times=300，2026-09-29 实测）；与预测/建面共用一把
  非阻塞信号量，占用时立即 429。

### 光谱 × 滤光片（/api/specphot，分期交付中）

「一条谱 → 一组数」：库内谱或上传/粘贴文本谱 × 滤光片透射曲线的合成测光、
连续谱拟合与谱线测量。代码全在 `backend/specphot/`（逻辑下沉、路由薄），全链只读：
不写库、上传件不落盘、不改 `filters` 表（曲线口径 `curve_kind` 登记在代码侧
`specphot/registry.py`，因为 `etl.py --filters` 会整块覆盖 `extra_data`）。

- 响应契约：所有响应（含错误）带 `spec_phot_version` 与 `warnings[]`；错误为
  `{error, code, reason?}`（宿主无 501/504 兜底 handler，一律显式 jsonify）。
- 库内谱装载与 `GET /api/spectra/<id>` 同口径：字符串行 float() 清洗、波长列已是
  真空（不再二次空气→真空）；上传文本与 `/api/spectra/upload` 同一语法（`#` 头键、
  2/3 列、逗号或空白分隔），差异仅在重复 λ 聚合求均值（而非覆盖）与乱序拒绝（而非代排序）。
- 分期状态（未到期子功能 501 `feature_disabled` 占位，响应含 `phase`）：S1 合成
  测光 **P1 已上线**（API-2 `POST /photometry`，前端 `#/tools/specphot` 工作台
  已挂载，S0 装载 + S1 计算 + 诊断占位 `diagnostics{}` 恒在场）；S2 连续谱 **P2
  切片 2b 已上线**（API-3，见下）；**P2c 诊断增强 S1/S2 侧已上线**（见下方
  专条）；**S3 谱线 P3 切片 2 已上线**（API-4，见下）；当前已上线 meta/curve/
  health/parse/ebv/photometry/continuum/line 八个端点。
  P1 验收测试见 `tests/acceptance/test_l1_specphot_*`、`test_l2_specphot_*`
  （含 `test_l2_specphot_p1_acceptance.py` 收口补遗）；P2 2b 验收 =
  `test_l2_specphot_p2b.py`；P3 切片 2 验收 = `test_l2_specphot_lines_api.py`。
- **P1b 预处理读侧半段已上线**（02b §3.14，`backend/specphot/preprocess.py` 是
  F-80 序列化与 PreprocessedSpectrum 的唯一载体）：
  - 响应谱级 `preprocess{}` 18 键恒在（§4.2 键集唯一载体），P2 侧键取 null/[]；
    `mask_hash`/`preprocess_hash` 双哈希进 TXT-27 摘要行、导出 `#` 头与 ST-3
    缓存键；`preprocess_hash` 输入键序 = {factor, mask_hash, errcol_verdict,
    errcol_accepted, spec_hash, 版本戳}（可复算的规则，T-75②）。
  - 四类掩膜并集（F-107①）：大气吸收表/天光发射表（F-72①② 默认剔除）+ 用户
    数值段（U-13 框选 `mask` + U-50 手输 `preprocess.ranges`，同一用户类）；
    V-13 过滤、精确同段去重（W-36：斜纹只画一次、n_masked_pixels 不翻倍）、
    逐类与并集后都受 C_MAX_EXCLUDE。掩膜是布尔标志位数组：不截断、不插值。
  - 误差列退化判定（F-110/V-22）：`errcol_verdict` 六值闭集 {ok, all_zero,
    constant, nonfinite, negative, flat_relative}，判定序唯一命中即停；
    n_err<30 时相对散布步跳过并挂 CA-21。非 ok ⇒ `errcol_choice` 二选一
    （Q-33 词表 {auto, proxy, as_provided}）：回落 F-54 代理（CA-47②，
    sigma_method='second_diff'）或坚持原列（CA-47①，sigma_method='spec_err'、
    errcol_accepted=true；部分行不可用仍按 F-23 走 'mixed' 回填）。`as_provided`
    仅在 verdict≠ok 时允许；API-7 解析响应即回显 `errcol_verdict`（U-53 必须在
    算之前给出，W-37）。读侧 V-9 保留 σ≤0 列值进 `flux_err`（不参与逆方差权重），
    使 T-78②③ 的判定域与 V-8 行宽语义成立。
  - 离群点（F-111②③/T-79③）：C_BASE_ITER 次 σ 裁剪在二阶差分残差上只**找候选**
    （S1 无基线，F-36 属 S3；登记的实现约定），回显 `n_outlier_flagged` 并把候选
    段随 CA-10(reason=outlier_flagged) 摆在告警区；绝不自动剔除，U-52 逐段确认
    后写入用户掩膜。占比 > C_CLIP_MAX_FRAC ⇒ CA-47④、不提供剔除按钮。
  - 因子/平滑的**算术**属 P2：factor>1 与 smooth 开启 ⇒ E-13（501 phase='P2'）；
    factor 表外值 ⇒ E-14(rebin_factor)；前端 U-49/U-51 渲染但禁用（A-3/E-15）。
  - P1 基线固化：`tests/acceptance/test_l1_specphot_p1b_baseline.py` +
    `golden_specphot_p1/*.json`（T-75①，比较域按 F-113③ 排除 preprocess{} 与
    掩膜/离群新行为族键，理由见该文件 docstring）；P1b 验收 =
    `test_l2_specphot_p1b.py`（T-75②③、T-78、T-79①③、Q-33、W-36 服务端半段）。
- **P2+ FITS/ECSV 上传分支已上线**（F-76，验收 T-81；前置 M-7 `astropy` 实测在栈内，
  零新增依赖）：`backend/specphot/reader.py` 增 `load_upload_fits`（两种形态：
  BINTABLE/TABLE 表格按列名/TUNIT 识别 λ/流量/误差列；一维 ImageHDU 走线性 WCS
  `CTYPE1 波长型 + CRVAL1/CRPIX1/CDELT1|CD1_1 + CUNIT1`，LOG/缺 WCS ⇒ E-14）与
  `load_upload_ecsv`（`astropy.table.Table.read`，λ 单位取列 unit、头键取 YAML meta）。
  - λ 单位换算一律 `astropy.units`（TUNIT/CUNIT1/列单位 → Å，不自写公式）；
    波长列无单位 ⇒ E-14（不按 Å 猜，V-19 同纪律）；头键映射对齐文本路径头键集
    （ra/dec/redshift/time/mjd/lambda_frame/name/instrument，含 z→redshift、
    OBJECT→name 别名），共用 `_finalize_upload` 规范化管线（V-18…V-20、F-77 缺省
    矩阵、F-79② 帧处理、spec_hash），⇒ T-81① 跨格式不变量（三载体 spec_hash
    三串相同）成立。
  - F-76 不猜纪律：多扩展（E-14 `ambiguous_hdu`）/ 多候选列（`ambiguous_columns`）/
    WCS 无法唯一判定（`no_lambda_axis`/`nonlinear_wcs`）⇒ E-14 且错误点名候选
    扩展与列名；请求可用 `hdu`/`col_lambda`/`col_flux`/`col_err` 指定。
  - 传输形态（登记的裁量）：FITS 二进制走 `content_b64`（标准 base64），ECSV 本是
    ASCII 走 `text`；`format_hint ∈ {txt, fits, ecsv}`（表外值 ⇒ E-14
    `upload_format_unsupported`），缺省按内容探测（`SIMPLE=` magic bytes /
    `# %ECSV` 头行），显式 hint 与探测不符 ⇒ E-14。点数 `C_MAX_SPECPOINTS` 与
    单件 `C_MAX_UPLOAD_BYTES` 对三格式同值生效；请求体 32 MB（ST-13）未动。
  - 前端 `source_panel.js`：file input `accept` 增 `.fits/.fit/.ecsv`，按扩展分发
    （.fits 读为 dataURL 取 base64，其余 readAsText），全程内存不落盘（RO-5）。
  - 验收：`test_l1_specphot_upload_fits_ecsv.py`（T-81①②③④ 逐条，FITS/ECSV 均
    astropy 现造于内存 BytesIO）+ `test_l2_specphot_api.py` 的 API-7 分支用例。
- **P2 切片 2b（API-3 + 宿主消光三态 + model 定标）已上线**：纯函数层在
  `backend/specphot/continuum.py`（fit_spectrum/fit_poly/compare_models/
  closure_from_fit/profile/bootstrap，引擎 = `least_squares.trf`，T-79① 全目录
  扫描禁 `loss=`/`f_scale`/`'lm'`/`curve_fit`），路由编排在其旁
  `continuum_api.py`（upload.py 同款薄壳）：
  - 装载与预处理管线与 API-2 **同源**（merge_masks/F-110 判定/去偏 σ/合束同一
    批 preprocess 函数，无第二套实现）；但 API-3 **不构造
    PreprocessedSpectrum**（未走 `build_spectrum`——S2 只消费掩膜布尔与逐点 σ，
    不需要合束后的谱对象；factor=1 时两侧逐点数值等价，factor>1 时各自经
    `rebin_stage` 同一算术）。拟合区选段按 §3.8 窗口语义（四类掩膜并集外、
    非正流量剔除并回显 `fit_region.n_nonpos`）。
  - **宿主消光三态**（F-87…F-90，唯一入口 `host_ext_mode`，Q-32）：`off` ⇒
    A_V≡0 钉死分支（pl_dust 退化、n_par 少一）；`fit`（缺省）⇒ A_V 自由，
    事后 `ebv_display` 只显示不回灌（F-90②）；`prescribe` ⇒ `ebv` 必填
    （≤ `C_HOST_EBV_MAX`=2.0，§7.5），A_V ≡ R_V·E(B−V) 钉死、退出自由参数表
    （n_par 少一）并挂 CA-43。off/fit 带 `ebv` ⇒ 忽略并回显 `ebv_ignored=true`，
    数值与不带逐字节相同（T-58；ebv 仍进缓存键 ⇒ 标志按请求正确回显）。
  - **T-55 单一算术**：全模块唯一一条 A_V↔E(B−V) 换算在
    `continuum.av_ebv`（单一函数、单向给定），fit 反算与 prescribe 钉死都经它；
    `host_ext_basis{}` 七键全必填（T-56），R_V 基准取宿主 `sedfit/laws.py` 的
    `_NOMINAL_RV`/`_INTRINSIC_RV`（lmc 3.16 vs 3.41，重标定只准走内禀）。
  - **de_reddened 曲线 B 是纯派生件**（F-89）：derivation_depth 恒 1、不落盘、
    不入库、不生成子谱（T-57 快照测试承载），响应附 TXT-22 逐字节文案。
  - **模型比较与 best（F-29/T-17）**：嵌套对（NESTED_PAIRS）F 检验判定 +
    ΔBIC 并列信息**各按各自门限回显、不强求一致**——`verdict.ftest[]` 记
    {F, p, verdict, alpha=C_FTEST_ALPHA}，`verdict.dbic[]` 对嵌套对与非嵌套对
    都记 {dbic, verdict, threshold=C_BIC_MIN}（非嵌套对不输出 F 检验字段）。
    `verdict.best` 的判定依据是 F 检验：**嵌套对 p < C_FTEST_ALPHA ⇒ 取繁者**
    （即使纯 BIC 偏好简者；迭代到不动点以覆盖嵌套链）；纯 BIC 兜底（BIC 最小者）
    只在非嵌套对或 F 不显著时生效。
  - **P2b 切片（pl2 断折幂律 + Davies 参数化自助）已上线**：`pl2` = 宿主
    `powerlaw_2seg`（§3.8 表：两段平滑幂律 + 宿主消光，s 固定 3.0，硬约束
    β1≤β2）——内部参数化 (β1, Δβ=β2−β1≥0, ν_b, A[, A_V])，报告口径补 β2=β1+Δβ
    （β2 的 σ/区间走 delta_method 精确线性映射）；ν_b 先验域 = 宿主
    params_schema [1e8,1e20] Hz（log 网格，F-65），搜索界按 F-91② 收窄到谱
    频率覆盖内，触网格端点走既有 CA-24 机器（grid_boundary_params 回显）。
    `pl⊂pl2` 是嵌套对但断点 ν_b 在零假设下不可识别（Davies 问题，F-29③）⇒
    该对**不出 F 检验字段/ΔBIC**，`verdict.davies[]` 记参数化自助判定：
    以 pl 拟合曲线为均值按同一白化通路重抽残差、pl/pl2 双双重拟的 T=Δχ²
    经验零分布，p = (1+#{T*≥T_obs})/(1+n)（加 1 式），p_method=
    'parametric_bootstrap'；边界 χ² 混合 50:50（p_chi2mix）仅自检旁证
    （T-18）。ST-10 口径登记（P2b 评审）：davies_bootstrap 的 n_boot **不计入**
    C_BOOT_N_BUDGET 预算池——原文口径分歧（ST-10 行写「单次请求全部自助次数
    之和 ≤ C_BOOT_N_BUDGET」，Q-15/F-28/F-95③ 却把同一池绑定**误差推断**，
    F-29③ 又把参数化自助定为 pl→pl2 的唯一判定通路；若混算，剖面重拟合会把
    判定通路的必需次数挤到不可用）⇒ 裁量：误差推断预算在 fit_spectrum 内照旧
    硬封顶，检验 n_boot 只受 C_MAX_BOOT 与 ST-5 重档 5 s 硬超时兜底（tick 逐次
    检查点，超时 E-11）。三态同步作用于 pl2（Q-32「凡含尘埃的模型」）：off/prescribe 下
    A_V 钉死退出自由参数表（n_par 少一）。拟合侧两处实现裁量（均不触规格
    常量）：① trf 在 pl2 退化谷/准触界分量上 xtol 早停后从返回解**续跑**同一
    引擎形态至多 2 轮（nfev 累计回显）；② 事后最优性门限（F-91⑦）对超限
    分量做可行下降探针——数值零列（F-93① 同形阈值）或任何 ±{1e-3,1e-2,
    1e-1}·域宽探针都降不了 1e-3 χ² 的「数值平台」不构成未收敛证据（Davies
    脊上剩余改善 ~1e-4 χ²，240 点谱的统计分辨单位是 Δχ²=1），真被困局部
    极小（下降收益 O(1) χ²）仍触发 CA-44①。消光求值 memo：A(λ_rest)/A(V)
    数组按 (law, rv, z, ν 网格) 缓存（miss 仍走 `sedfit.laws.get_law` 现算，
    乘式与 `laws.extinguish` 逐项同式同序 ⇒ 数值逐字节同）——宿主
    dust_extinction 的 G03/P92 曲线每次求值重建样条，不缓存会烧穿 ST-5 的
    5 s 重档预算。测试：`test_l1_specphot_p2b_pl2.py`（T-18 回收/自检/检出、
    钉死态、T-31 cond_2 数值纪律）+ `test_l2_specphot_p2b_pl2.py`（Q-13 词表、
    davies 装配、W-24、三态）。
  - **P2 切片 2d（specplot/export 增量，无新端点）已上线** —— de-reddened 导出
    闭环 + 双曲线叠加（原「登记遗留」块至此闭环）：
    - **CSV 导出**（`export.js` `exportContinuumCsv`，纯前端 Blob 下载，不落盘
      服务端、不入库、不生成子谱）：列序 = §4.3「宿主 de-reddened 曲线导出」块
      逐字（发布即冻结）；逐点列取自响应 `de_reddened`（`lam_rest_vac_aa` 由
      2d 起随响应给出），参数映射按 §3.9.3.1——`av`=A_V、`ebv`=`ebv_display`，
      误差列**直接搬运 FitResult**（`av_err_lo/hi`=`err_low/high{Av}`、
      `ebv_err`=`ebv_display_err`，不得另算）；prescribe 下 av/ebv 是输入 ⇒
      误差列空字段 + CA-43 书面原因；`rv` 为所选律常数 ⇒ `rv_err_*` 恒空字段。
      `#` 头：TXT-11 首行 + TXT-22 全文逐字（含末句「宿主与银河之间那段路径上
      的消光未计」）+ `host_ext_basis` 七键（未平铺进表体的
      `rv_source_note`/`lam_axis`/`form` 三键写注释行）+ `derivation_depth` +
      `curve_hash` + preprocess 双哈希（mask_hash/preprocess_hash）；文件名
      F-44 口径 `<src><id>_<host_ext_mode>_dered_<UTC>.csv`。触发点 = S2 动作区
      「导出 CSV（曲线 B）」按钮（无 de_reddened / host_ext_mode=off 时禁用 +
      说明）；JSON 导出（响应原文）整体携带 `host_ext_basis` 与 `de_reddened`。
    - **双曲线叠加**（`specplot.js` + `continuum_ui.attachCurveOverlay` 实装）：
      U-47 开（S2 面板开关，默认开、纯显示不转 stale）且 `host_ext_mode ≠ off`
      ⇒ 谱图上曲线 A（`flux_before`）与曲线 B（`flux_after`）同轴同单位绘制、
      legend 写明「改正后 = 派生件、未入库」（F-89①）；TXT-22 全文常驻图注
      （W-33 的图注位）；U-47 关/off ⇒ 只画 A 并清除叠加（F-89⑤ 不留孤立旧 B）。
    - **TXT-22 三处逐字**（W-33）：结果卡、图注、导出件头共用
      `export.js txt22Text` 单一构造器，保证一字不差；TXT-22 五个数
      （A_V/E(B−V)/R_V/rv_source/screen_z）同行同屏（F-88）。
    - **U-46 fit 态**：E(B−V) 框转只读，显示由拟合 A_V 反算的 `ebv_display`
      （只作显示、禁回喂，F-88/F-90②）；prescribe 态维持常驻换算行。
  - **model 定标解锁**（§3.3 model 行，F-33 单向耦合）：photometry 的
    `mode='model'`/auto 落 model 携 `use_model`（Q-9：mask_hash/frame/
    flux_transform_applied/comparable 必带 + Q-34 preprocess_hash）⇒ 用 S2
    模型曲线代替观测谱积分（覆盖外可外推 + CA-12；comparable=false ⇒ E-14，
    F-91⑦）；无 `use_model` 的 model 路径保持 501 phase='P2'；model 定标下
    mag_err_stat/f_err_mjy/delta_m_err 按 null 出（S2 参数误差传播不在本期）。
  - 前端：`frontend/js/specphot/continuum_ui.js`（S2 参数面板 + 结果卡 +
    TXT-22），S2 页签解禁（S3 当时保持禁用，随 P3 切片 2 解禁），S1 定标模式
    radio 的 model 支解禁；
    新发 CA-11/12/43 归「结果解释」、CA-24 归「近似」（§5.4 末权威分档行，
    results.js `groupOf` 已登记）。
- `/api/specphot/ebv` 是全站第一个按坐标（而非 DB id）查消光的端点；首次调用触发
  尘埃图惰性装载（可达数秒，回显 `cold_start_ms`），重复坐标走宿主 `_ebv_cache`。
- 代理 σ 的去偏与双 j（F-97①⑤，T-61/T-62）：二阶差分/滑窗 MAD 代理先除
  `k_proxy(ρ, j_used)`（`errors.k_proxy`）去偏，再进 F-55 的 AR(1) 积分放大；
  误差列（实测误差）不去偏。`|ρ|>C_RHO_MIN` 时双 `j` 对照取较大者（`ρ>0` 即
  stride 支），`|ρ|≤C_RHO_MIN` 允许 `j=1`（白噪声豁免，双 j 差 <3%）；响应顶层
  回显 `sigma_px_j1`/`sigma_px_j2`（去偏后的两个 σ_px）/`sigma_stride_used`
  （代理不可用或走误差列时为 null，不冒充），`CA-06` 呈现含双 j 两值与所取者。
- `mw.correct` 缺键默认 `true`（§5.2.1 规范默认；显式 `false` 仍不改正；缺坐标
  ⇒ `CA-23 ext_no_coords`）。请求只携带 `spec_hash` 而不带谱数组 ⇒ `E-01`
  （404 `spectrum_not_found`，A-7②：hash 只用于结果缓存命中，不作为谱身份）。
- **P3 切片 2（API-4 + 三步向导 + 线表导出）已上线**：纯函数层
  `backend/specphot/lines.py`（P3 切片 1 交付）由新路由编排层
  `backend/specphot/lines_api.py` 装配（`POST /line`，meta.py 的 501 占位删除）：
  - 请求校验（§5.2.3）：`line_kind` 必填无默认（Q-18 ⇒ E-09
    `metadata_required/line_kind_required`）、Q-23 来源二选一、Q-21 voigt 无 R
    拒（P3b 起按 F-39 闸：R 可得 ⇒ 放行，见下 P3b 段）、U-31
    `sky_handling='subtract'` 随 P3c 的 F-86 真解锁（见下 P3c 段）、Q-15
    n_boot/基线阶、Q-35 err_seed（PCG64，缺省 spec_hash 派生）、Q-27 诊断布尔
    （U-48 三键 ⇒ 501 phase='P3d'）；`baseline.side_px/iter` 钉死
    C_BASE_SIDE/C_BASE_ITER（改值显式拒，U-25 手改属 P3c 通路）。装载与
    API-2/3 同源（merge_masks/F-110/去偏 σ/双哈希/ST-3 缓存/ST-5 超时）。
  - **Q-20 vs F-72③ 掩膜冲突调和决定**（登记在 lines_api.py 模块 docstring 与
    `select_window` 注）：Q-20 字面「线心落 C_MASK_ABS 或 C_MASK_EMIS 命中区
    ⇒ 拒」（wire reason 钉死 `mask_conflict`）与 F-72③ 分档（只拒发射×吸收带、
    吸收×气辉两支；发射×气辉、吸收×吸收带不拒）并存 ⇒ **以 F-72③ 的分档语义
    实现 Q-20**：wire `reason='mask_conflict'` 一张脸（Q-20 字面成立），类属
    可辨性由 details 的 `mask_conflict_kind ∈ {abs_band, skyline}` 与
    `f72_reason`（原双 reason 透传）承担，message 逐字保留 F-72③ 的两类措辞
    （"大气吸收带"/"天光发射线"，T-20/W-25 末句：不得混用）；发射×气辉整窗
    被剔光的空窗支路 ⇒ E-04 too_few_pixels（不冒充 mask_conflict）。
  - frame 闸门（W-25 **两条独立**，不得合并表述）：①线表侧——请求
    `velocity_output=true`（强提交开关，实现裁量键名）而 `line_frame` 未核对
    ⇒ E-14 `line_frame_unverified`（文案说**线表** 82–87 km/s 系统差）；
    ②谱级——`lambda_frame='unknown'` ⇒ E-14 `lambda_frame_unknown`（文案说
    **谱的波长轴**）。①②同违先判①。M-6 未复核 ⇒ 响应 `vel_*/z_fit` 恒 null
    + `velocity_family_note` 留位、`frame_gates.vel_outputs_enabled=false`
    （闸门结构先就位，P3c 已实现、M-6 未复核 ⇒ 维持 null，见下 P3c 段）。
  - 响应装配（§4.2 LineResult 键集，F-94⑤ 值键与误差键同批）：单线一行，
    线比 ratio 族恒 null + 书面原因（§3.9.3.1 单线请求退化支）、柱密度族恒
    null（M-6 振子强度库未备，F-71）、`diagnostics.absorber_systems` 恒在场
    空数组（F-105①）。
  - 前端 `frontend/js/specphot/lines_ui.js`：U-23 三步向导（步 1 选线区——宿主
    `spec_lines.js` 候选**只作位置标记**（M-6/F-38）+ line_kind 必选（U-30，
    未确认「下一步」禁用 E-09，W-23）；步 2 基线形态说明随 line_kind 同步切换
    加性/乘性 + EW 符号说明（TXT-15，W-23）；步 3 轮廓/R/n_boot/line_frame +
    `vel_*` 控件禁用 + tooltip 指 M-6），S3 页签解禁（U-03），结果卡复用
    results 机器形态（err_source/err_scope 逐键 + TXT-8/TXT-15 常驻）。
  - 导出：`export.js` 增 `exportLinesCsv`（§4.3 谱线块列序逐字冻结，F-45），
    `#` 头 = TXT-11 + TXT-23 + 双哈希（mask_hash/preprocess_hash）+ W-25①②
    各一行的框架闸门声明；文件名 F-44（`<src><id>_line_<UTC>.csv`）。
  - 验收：`tests/acceptance/test_l2_specphot_lines_api.py`（校验拒绝族、
    Q-20/F-72③ 三层断言、frame 闸门双支 + 先判序、端到端真值回收、§4.2 键集、
    ST-3 缓存确定性、§4.3 列序源码级冻结断言）。
- **P2c 诊断增强 S1/S2 侧已上线**（02b §3.12 / §9 P2c 行；实现全在
  `backend/specphot/diagnostics.py`，U-44 前三键，API-2 `photometry` 接线）：
  - 公共纪律：三开关全关时 diagnostics.py **不进入任何计算路径** ⇒ 响应与不含
    本实现逐字节相同（T-48①，`diagnostics{}` 恒在场为空对象、不在比较域内，
    与 preprocess{} 的 F-113③ 同形）；开启只增 `diagnostics{}` 子键与
    `warnings[]`（diag 已入 ST-3 缓存键）。后三键 z_from_lines/frame_probe/
    sky_subtract：消费点在 S3 步 1 的 U-31（F-86），本 S1/S2 路径 501
    `phase='P3c'` reason=`sky_subtract_use_u31`（指路不冒充，见下 P3c 段）；
    z_from_lines/frame_probe P3c 起走真实现 + M-6 闸（null 块）。
  - `resp_perturb`（F-83）：`results[i].mag_err_resp` 换通带形状扰动散布口径
    （实现取 max(扰动散布, 两加权之差下限)，T-49「扰动不得让误差变小」），
    `results[i].resp_method ∈ {lower_bound, perturbation}`；`diagnostics.resp_perturb
    = {perturb_n, perturb_n_requested, downscaled, eps, resp_method}`，开启挂
    CA-39。扰动 = 在 ln λ 上以该波段自身 σ_lnl（band_integrals 的 `sigma2_lnl`
    回显值，F-2 定义）为相关长度的高斯核平滑白噪声、支撑内 RMS 归一到
    C_SHAPE_PERT_EPS=0.05；随机流种子 = `default_rng([spec_hash 前 8 位,
    crc32(band), 组号])` ⇒ 同一输入逐字节可复算（F-93⑤ 同族，实现登记）。
    CA-02 行级文案随 resp_method 分档（§5.4）：lower_bound 恒
    「σ_resp 为两加权之差的下限（F-57）」，perturbation 换
    「σ_resp 为扰动散布与两加权之差下限的较大者（F-83）」。
    组数 C_SHAPE_PERT_P=60，预算触顶（deadline − 0.25 s 预留，实现补名
    `_C_SHAPE_PERT_RESERVE_S`）降规模并回显（Q-27/ST-9）；band_mode=mono 不
    适用（CA-15 reason=resp_perturb_mono，reason 扩展登记 §7 日志）。
  - `beta_matrix`（F-82）：`diagnostics.beta_matrix = {cells[{band_i, band_j,
    beta, sigma_beta, color, color_err_stat, color_err_cal, dt_d}],
    causal_use:'diagnostic_only', note}`（note 常驻「只用于发现不一致、不得作为
    物理 β 报告」声明）。**输出块的 `causal_use` 键是 §4.2 `beta_matrix` 键集的
    良性超集**：§4.2 行只列 cells 字段，而 F-82 明文「矩阵旁常驻此声明」、F-84
    硬约束同要求 `causal_use='diagnostic_only'` 写入响应 ⇒ 该键是声明载体，非
    键集漂移（T-63⑦ 按超集读）。β_ij = (ΔC_obs − ΔC_syn)/log10(λ_j/λ_i)（「用
    合成值与实测值各算」：两支色各取合成/实测，纯幂律 ⇒ 全格 ≈0）；成格条件 =
    两支锚点对谱时刻 |Δt| ≤ dt_tol_eff（F-61 生效容差），格内 dt_d =
    |mjd_i − mjd_j|；σ_β 走 delta_method 且 κ* 协方差项按两行参与分档
    （F-94⑤「同波段对的 −2Cov」，对角 C 下一阶精确、MC 实测）：两行都参与
    anchored ⇒ +2·Var(κ)_mag（GLS 残差反相关，Cov(dm_i,dm_j) = −Var(κ)_mag ⇒
    修正方向为放大 σ_β；三波段等权全参与例 σ_β 由 0.00135 归位 0.00405 量级）；
    恰一行参与 ⇒ 0（非参与行的 κ* 误差与参与行被 GLS 吸收后的残差分量精确定
    消）；两行都不参与 ⇒ −2·Var(κ)_mag（两行 delta_m_err 各含 +Var(κ)_mag，
    −2 恰好抵消）。Var(κ)_mag = (2.5/ln10·σ_κ/κ*)²，非 anchored 时为 0；
    参与判定与 anchored GLS 同通道（h_map）。color_err_cal 按 §3.9.3.1 颜色行
    杠杆式：同一 κ* 的定标偏移在色（两行之差）中**精确定消** ⇒ 两行
    mag_err_cal 在场（anchored）时格值 = 0.0（闭式结果、非缺测冒充；明文禁止
    把两个单波段 σ 直接 quadrature），resp 项仍进 color_err_stat（F-82 明文）；
    任一行 mag_err_cal=null（σ_cal 无法评估）⇒ 格值 null + note 书面原因
    （TXT-23）。非对角格 β 互差 > 3√(σ₁²+σ₂²) ⇒ CA-40（可达但不得据此报
    物理 β）。无格可成时 cells=[] 照出（键集恒定）。
  - `anchor_reinsert`（F-84）：`results[i].m_syn_local`（§4.3 列集追加位，恒在
    场、未开启 null；诊断镜像，不得顶替主列 mag，F-11）+ `diagnostics.anchor_reinsert
    = {cells[{band, m_syn_local, delta_m_local, shrink_1_minus_h, reason?}],
    causal_use:'diagnostic_only', note}`（与 F-56 的 (1−h_i) 收缩量并排）。局部
    改正 = L-5 的「实测流构造 S·10^{0.4m} 后卷积」：κ*·F 谱在该波段支撑内乘
    g_i/(κ*·f_i)（折算到原始谱即乘 g_i/f_i，κ* 公共模消去）后真实重积分 ⇒
    m_syn_local = AB(g_i)、delta_m_local ≈ 0 是构造性自检；逐波段残差对全谱
    m_syn_local − mag 暴露单一全局 κ* 的个别波段跑偏。非 anchored ⇒ 全 null +
    note 书面原因（TXT-23 同族）。
  - 前端：workbench U-44 前三项可勾选（每项旁写明新增列与新增计算规模，W-32
    前半；改动 ⇒ stale，IA-4），后三项渲染但禁用（P3c 起改 M-6/U-31 徽章与
    tooltip，见下 P3c 段）；
    results.js 请求体只携带勾选键（Q-27 只收布尔，全关 = 空对象与基线同形），
    诊断卡渲染 `diagnostics{}` 开启项，`groupOf` 新增 CA-39∈「近似」、
    CA-40∈「结果解释」（§5.4 末分档行）。验收 =
    `test_l2_specphot_diagnostics_p2c.py`（T-49/T-50、T-48 后半、Q-27 分键闸、
    W-32 前半服务端可自动部分 + 前端源码级扫描）。
- **P3b 已交付（voigt，闸门后）+ P3c 已交付（线侧诊断三件）**（02 §9 P3b/P3c
  行；2026-10-02）。**前置裁定**：M-4（仪器 R 入库，FITS 导入携带）与 M-6
  （线表帧与 f 值逐条对 NIST ASD 复核）在库内数据上均未满足 ⇒ P3b/P3c 以
  「实现完整就位 + 闸门按条款拒绝语义」形态交付，不伪造库内数据；解锁条件
  逐条写明如下。验收 =
  `test_l1_specphot_p3b_voigt.py` + `test_l2_specphot_p3bc.py`（T-22 两半全测、
  T-51 四支按合成口径全测 + 真实 M-6 数据重跑登记 skip，照 T-10/T-21 先例）。
  - **P3b · voigt（F-39/F-40/T-22，闸门后）**：`lines.py` 新增
    `_voigt_profile(x, σ, γ)`（scipy.special.voigt_profile 峰归一——F-40 只禁
    VoigtFit 本体，scipy 是既有依赖零新增）、`_shape_voigt`（u=lnλ 空间，
    (A,u0,w,g) 每成分 4 参，θ 布局泛化为 `_NPAR_OF`）、FWHM_u 用
    Olivero–Longbothum 近式（0.5346·fL+√(0.2166·fL²+fG²)；梯度解析、数值性质
    由单测对数值半高全宽逐点钉 <0.1%）；EW/bootstrap 机器复用既有通路。
    **闸**：`profile='voigt'` 而 R 不可得（全库 r_source='none'，M-4 未完成）⇒
    E-14 reason=`voigt_disabled_no_r`（文案对齐 F-39 原文：宽度只能作观测宽度
    报告 + TXT-8/CA-08）；R 可得（用户侧 U-27 或 M-4 落地）⇒ 数值路径可达
    （测试用合成 R fixture 驱动）。前端 U-26 的 voigt 单选保持禁用 + tooltip
    写明前置 M-4（T-22 前端半支）。depth 误差梯度对 voigt 无简单闭式 ⇒ 走
    F-95① 明文允许的有限差分（C_FD_REL 中心差分），行键 `depth_err_delta`
    ∈ {analytic, fd} 恒在场回显口径。
  - **P3c · z_from_lines（F-81，M-6 闸门后）**：`diagnostics.py` 实现互相关
    反推（ln λ 等距网格 + FFT 互相关无迭代，§5.4 预算 ≤300 ms；滑动中位数
    去基线、模板宽 = 2×像素步长——实现裁量登记）；σ_z 由互相关峰二阶曲率与
    残差噪声估计（σ_τ=√2·σ_CC/√k，登记）；命中线 < C_LINE_MATCH_MIN ⇒
    z_fit=null；|z_fit−z_used| > C_Z_TOL ⇒ verdict=`ca36_mismatch` + CA-36
    告警 + lines[] 逐条 accepted/rejected，**绝不自动改写 z**（U-37/CA-36）。
    **闸**：M-6 复核线表缺位（`registry.M6_LINE_TABLE=None`，库内现状）⇒
    API-2/3 返回 200 + `diagnostics.z_from_lines` null 闸门块（z_fit/sigma_z/
    n_lines_used/lines/verdict/reason 恒在场，F-94③ none 语义；
    verdict=`disabled_line_frame_unverified`）；`lambda_frame='unknown'` ⇒
    E-14 `lambda_frame_unknown`（T-51）。数值路径只经测试内合成 fixture
    （monkeypatch registry 两键）模拟 M-6 复核态——fixture 是测试内合成，
    不是伪造库内线表。
  - **P3c · frame_probe（F-85，M-6 闸门后）**：`diagnostics.py` 实现特征位置
    判帧——对 M-6 复核参考特征表（`registry.M6_FRAME_FEATURES`）逐特征在 ±3 Å
    段内抛物线内插观测中心，与 vacuum/air（一律 wavconvert 换算）位置比对，
    输出 `frame_probe{frame_suggestion ∈ {vacuum,air,inconclusive}, n_support,
    log_lik_ratio, features[]}`（只进 diagnostics{}，禁自动改写 U-39，F-79②）；
    每分辨率元信噪（无 R ⇒ ×√C_DLAM_OVER_FWHM_MAX 等效口径，与 snr_res 同约）
    < C_FRAME_PROBE_MIN_SNR ⇒ 该特征 inconclusive。**闸**同 z_from_lines
    （M-6 缺位 ⇒ null 闸门块、suggestion=inconclusive——是闸门值不是默认值，
    T-51「禁止沉默为 vacuum」）。合成空气波长谱 ⇒ 'air'、低信噪 ⇒
    'inconclusive'（T-51 第三/四支，全测）。
  - **P3c · sky_subtract（F-86/U-31，真解锁）**：判定依据原文——只依赖
    C_MASK_EMIS_TABLE 常量（[O I] 三线，位置已知）与谱数据本身，不依赖 M-6
    线表帧/f 值 ⇒ 按原文真解锁。`lines.py` 新增 `sky_subtract(lam, flux, σ,
    win)`：线窗内命中的天光段锚在表行名义中心 ±3 Å 内细化峰位（不做全域自由
    峰检索，防科学线被吸进扣除模型；与科学线心 <5 Å 的天光段保守保留 mask——
    F-86 末句「禁止用扣除结果反推源的线流量」）、每峰实测 FWHM（与 F-69 的
    sky_emission_fwhm 同口径）钉形状、连续谱基 + 各天光线幅值在**同一次**加权
    lstsq 内联合解（F-62②：lstsq/SVD 禁正规方程）；大气吸收带像素不进设计
    矩阵且其段永远 mask（F-72①）。被扣段核内残差 RMS 未降到
    C_SKY_RESID_FLOOR=0.7× 以下 ⇒ 自动退回 mask + CA-37（「不得称已扣除」）。
    sky_subtracted=true 计入 `not_in_budget[]`（F-58 的 sky_subtraction；该键
    按下限集恒在场于线行）。API-4 `sky_handling='subtract'` 放行走真实现
    （缓存键已含 sky_handling）；S1/S2 photometry 路径的 U-44 `sky_subtract`
    开关不消费 ⇒ 501 `sky_subtract_use_u31`（指路）。前端 lines_ui U-31
    subtract 单选解锁 + F-86/CA-37 说明；workbench U-44 后三项徽章改 M-6（两
    键，tooltip 写明解锁条件）/U-31（指路 tooltip）；results.js `groupOf` 新增
    CA-36∈红移与线位自洽、CA-37∈结果解释（§5.4 末分档行）。
  - 恒等回归：全关/不开新开关 ⇒ 既有键数值逐字节不变（golden 与 T-79① 仍绿；
    新增键均为恒在场簿记位：线行 `sky_subtract`（mask 路径 null）、
    `not_in_budget`、`depth_err_delta`）。
- **P3d 已交付（阻尼翼与吸收系统，U-48 三开关，可选族闸门后）**（02 §9 P3d
  行 / 02b §3.13；2026-10-02）。**前置裁定**：M-4（R）与 M-6（线表帧与 f 值）
  库内均未满足 ⇒ 整族为**可选族**：代码就位 + U-48 三开关后闸；三开关全关 ⇒
  API-4 数值与既有键与 P3c 基线逐字节相同（T-72①；恒在场的
  `absorber_systems`/`forest_stats`/`forest_stats_reasons` + `cross_link_gate`
  四键属判据、不属比较域）。验收 = `test_l2_specphot_p3d.py`
  （T-69…T-74 数值路径按合成口径全测 + W-35 前端源码扫描；真实 M-4/M-6 数据
  重跑登记 skip，照 T-10/T-21/T-51 先例）。
  - **F-98 识别与三档分类**：`diagnostics.py` `absorber_ident_numeric`——
    Lyα 翼窗（红侧 C_WING_RED_KMS）上检出显著性 = 窗内平均深度及其传播 σ
    之比 ≥ C_ABS_DETECT_SIGMA（只回答检出与否，F-98②）；分类阈值制
    `classify_logn`（`lls`/`sub_dla`/`dla` 半开区间无缝，未达 ⇒ None 空值），
    `class_thresholds_dex` 回显 log10 三值；intervening_or_host 用翼窗外红侧
    窄凹陷二阶差分计数（阈 −10≈4.1σ，误报期望 ≪1；覆盖不足 ⇒ undetermined
    合法终值）。**闸**：λ_Lyα 静止系真空波长由 M-6 线表给出（species 含 'ly'
    的条目），本模块不另立 Lyα 常量（F-98①）⇒ M-6 缺 ⇒ 闸门格（值键 null +
    书面原因）。
  - **F-99 翼拟合**：`wing_logn_two_families`——τ(v)=N·K·voigt_profile(v;
    b/√2, Γ/4π)，K=∫σdν·λ_cm/1e5 由 scipy.constants 就地导出（禁行线 5 同族；
    Γ(Lyα)=6.2649e8 s⁻¹ 仅剖面形状用）；**z/b 永不在自由参表**（禁行线 19）；
    b 三路优先级 `b_pin`（metal_cog > resolution_element > engineering_default），
    b_source 逐字回显；Δχ²=1 剖面 = logN 粗扫 + 黄金分割细化 + 两侧二分到
    χ²min+1（族 B 每点联合重拟连续谱 = 完整 fix-and-refit），剖面求值计入
    C_BOOT_N_BUDGET 共享池（F-95③）；wing_snr_res < C_WING_FIT_MIN_SNR ⇒
    不拟合（两族四端点全空 + 书面原因，禁外推，T-70 末支）。
  - **F-100 双连续谱族**：族 A = 当前选定连续谱模型（`cont_a_override` 供给，
    缺省 = 拟合域〔翼窗∪红侧外延带〕上自拟一次后**冻结**——L-51② "for a
    fixed continuum fit" 的正实现）；族 B = lnλ 上同阶 Cheb poly 联合重拟；
    wing_spread_dex=|A−B| > C_WING_SPREAD_DEX ⇒ CA-45（TXT-24 换系统项句、
    禁 quadrature 合成）。**默认两族同源 ⇒ spread≈0，CA-45 在 API-4 经
    override 通路才可达（S2 模型供给后自然可达）**——登记为分期裁量。
  - **F-101/F-102**：恰为 0 的区间端点 ⇒ CA-44 过拟合哨兵；forest_stats 恒
    null + `forest_stats_reasons` 闭集四值（single_sightline/continuum_unknown/
    lls_stochastic 恒在，resolution_below_gate 随 r_source='none'）——不依赖
    前置 ⇒ **真实现**；响应与导出件无 x_HI/lyc_transmission/flux_pdf 等键
    （T-71③ 源码+响应双扫描；T-71④ 的样本描述≠门槛文档哨兵入测试）。
  - **F-103 交叉链接**：`preprocess.merge_masks` 增 absorber_ranges 参数——
    第五类（蓝侧 IGM 段）在测量**前**并入（F-106① 次序），走既有 mask_hash
    第 4 槽；`cross_link_band_fracs` 复用 S1 的 n_masked/(masked+used) 口径；
    线心落 masked_ranges ⇒ 线行 CA-46 + n_lines_in_masked_absorbers（②支）；
    蓝侧声明 blue_side_igm_masked（④支；本实现翼拟合永不使用蓝侧）。
    CA-46 闭合触发集未扩（并入掩膜本身不是 CA-46）。
  - **F-104 饱和与 b 包络**：sat_flag 只由被链接金属线行 depth 导出
    （1−depth < C_SAT_DEPTH_FLOOR ⇒ saturated），无 sat_*_err 列（T-74④）；
    AOD 适用性（snr_res ≥ C_AOD_SNR_RES_MIN 且 n_pix_per_res ≥ C_AOD_SAMP_MIN）
    不满足 ⇒ notes 点名哪一支并退回单一判据；F-104② 双方向 notes + ⑤ 单云
    声明常驻；b 包络 = 三次固定-b 重拟合（与族 A 同估计量），只作灵敏度展示
    不进 err_source，计入同一预算池、超池回落并回显（T-74⑤）。
  - **F-105 契约**：U-48 三开关只随 API-4（API-2/3 收到 ⇒ 忽略但回显
    `diagnostics_ignored[]`，T-72④，photometry/continuum_api 两处）；导出件
    §4.3 第四块列序冻结（export.js `ABS_COLS` 与规格逐字一致，源码级哨兵），
    U-48 关 ⇒ 表头照写无数据行；四列（class_thresholds_dex/metal_line_ids/
    masked_ranges/forest_stats_reasons）按 F-80⑥ 序列化；前端 lines_ui U-48
    三开关联动（未勾 ident 另两项禁用、无 R ⇒ wing_logn 禁用）+ 吸收系统卡
    （两族 logN + 四端点 + spread 徽章 + TXT-24/25 常驻）；results.js groupOf
    新增 CA-45∈近似、CA-46∈覆盖（§5.4 末分档行）。
  - **z_source 降级语义（按规格原文）**：metal_lines 路由（M-6 匹配 + 本请求
    已测金属线行，z_err 由 λ_err 经 lnλ 换元）→ user_z 路由（显式 z>0；
    metadata、z_err null + 书面原因、翼通路不再算 profile——§3.9.3.1 z_abs 行）
    → 闸门格；请求未给 z 且谱记录无 z ⇒ 不冒充（上传件回填的 z=0.0 是 Q-12
    缺省不是用户声明）。裁量为请求侧 metal_cog b 路由、Na(v) 强弱双线互比
    （单线请求退回 C_SAT_DEPTH_FLOOR 单一判据，与 §3.9.3.1 ratio 行同因）、
    AOD 积分核钳位（柱密度族未解锁 ⇒ 无消费点，随该通路实现）。
  - **P3d 评审修复（4 P1 + 5 P2，2026-10-02）**：
    - **P1-1 `cross_link_gate` 键集对齐 §4.2**（lines_api `_absorber_stage`）：
      补齐 `n_bands_over_gate`（API-4 无波段积分 ⇒ 恒 0）、`b_source`/`z_source`
      （回显本格钉住路由，闸门态 null）；实现追加键 **`enabled` 与 `bands_ca46`**
      在此登记（前者声明 U-48 启用态，后者为 API-4 侧恒空的 ①支波段行留位；
      §4.2 原文七键 + 此两键 = 实际键集，`test_p1_1_cross_link_gate_keyset`
      双态断言）。`bands_masked_frac`/`n_bands_over_gate` 的**真判定量在
      API-2/S1 侧**（见 P1-2）。
    - **P1-2 CA-46① 接线到 API-2**（photometry.py，`cross_link_band_fracs` 的
      唯一生产装配点）：`diag.absorber_ident=true` 且 `diagnostics.
      absorber_mask_segs` 给出掩膜段（M-6 有 Lyα + z_eff>0）时，第五类掩膜在
      测量/合束**前**并入（F-106①、mask_hash 第 4 槽），逐请求波段算通带被掩
      占比（与 band_integrals 的 n_masked/(n_masked+n_used) 同式，T-74① 同
      一算术载体），`> C_XLINK_MASK_MAX` ⇒ 该波段行 `warnings[]` 挂 CA-46
      （①支，reason=`absorber_mask_frac_over`）+ `diagnostics.cross_link_gate`
      回显（bands_masked_frac/n_bands_over_gate/n_lines_in_masked_absorbers=0/
      blue_side_igm_masked/b_source,z_source=null/note——后三者簿记在 API-4）。
      **F-105③ 半开裁定（评审修订，登记）**：`absorber_ident` 在 API-2 消费的
      唯一半支 = 上述掩膜/CA-46① 闸门，不再进 API-2 的 `diagnostics_ignored[]`；
      `wing_logn`/`metal_sat_check` 仍忽略+回显；**API-3 纪律不变**（三键全忽
      略）；识别/翼拟合家族结果仍只在 API-4（API-2 不产 absorber_systems）。
      判定量网格 = 掩膜自身的原生网格（掩膜先于合束/模型重积分安置，F-106①；
      合束与 model 定标请求的通带占比一律按原生掩膜计——裁量）。无掩膜段
      （库内 M-6 空的现状）⇒ 整块跳过，零开销恒等（S1 响应与 P3c 基线逐字节
      同值，golden 不受影响）。
    - **P1-3 顶层 `absorber_system_masked[]`**（lines_api）：响应 diagnostics
      恒在场第五键（§4.2 键位），值 = `absorber_systems[].masked_ranges` 经
      `_union_segs` 归并的并集（同源，不另立掩膜通路——F-103①/F-107① 同一条
      mask_hash 第 4 槽）；U-48 关/无段 ⇒ `[]`。键集断言同步：
      `test_line_result_key_set_and_top_level`/`test_t72_off_switch_byte_identity`。
    - **P1-4 F-99① z 窗口（择低成本支：三点重拟，不删常量）**：
      `diagnostics.z_envelope`——z 在金属线 z_abs ± C_WING_Z_WIN_SIGMA·σ_z 窗口
      内取三点（下端钳 0）各重拟一次 logN（与族 A 同估计量：_adopted_logn +
      黄金分割），spread = max−min 回显为 absorber 格追加键
      **`logn_spread_over_z`**（恒在场、未算出 null；只作灵敏度展示，不进
      err_source、不是 z 的误差——z_err 另走 covariance 一条，F-103③）；
      C_WING_Z_WIN_SIGMA 的唯一消费点在此。z_err 不可得（user_z 路由）⇒ null
      + 书面原因；三次重拟合计入 C_BOOT_N_BUDGET 共享池（b 包络已计提时按
      need 扣减，两包络不双计，F-95③）。装配点 lines_api `_absorber_stage`。
    - **P2 五项**：① absorber 格 `detect_sigma`/`reason` 为追加键（§4.2 格键集
      之外的实现位：检出显著性回显 + 闸门/降级书面原因；恒在场、未算出 null）
      ——登记；② Δχ²=1 剖面与 `continuum._profile_intervals` 的关系 = **同判据
      （步进加倍括区 + 二分到 χ²min+1）、独立 1-D 实现**（后者面向 S2 多参
      profile，前者单参数 logN 就地实现，不共用代码——两处算术演进互不牵连，
      判据一致性由各自测试钉住）——登记；③ E-14 reason **`u48_requires_absorber_ident`**
      （W-35/U-48 联动：wing_logn/metal_sat_check 未开 absorber_ident 时禁用，
      依赖摆出来不静默连带开启）——reason 词表扩展登记；④
      `test_t69_pinned_zb_and_profile_interval` 的 `or True` 恒真尾缀删除
      （b=10 重拟偏离真值更多现在是真断言）；⑤ **diagnostics.py 行数对冲**：
      评审登记 +50（vs §6.3 本族预算 280）；本次修复后实测 P3d 段
      （横幅至文件尾）508 行，超 +228，构成与对冲——数值核（Voigt τ/翼窗/
      黄金分割/Δχ²=1 剖面/两族/包络/占比）约 300 行属预算域；超出部分主要是
      （a）闸门降级分支与书面原因/notes 文案（F-94③ 逐支 null+原因、T-70 末支
      禁外推的可核对性要求），（b）docstring 逐条出处注记（§6.3 预算按纯代码
      计，文案计入所致），（c）P1-4 择三点重拟支的直接代价（z_envelope 38 行
      + b 包络共用件）；§6.3「十四项各自独立、任一不开即整段不进入」的形态
      未破，测试内合成 fixture 的验收代价不在本文件。

---

## 六、前端功能

### 6.1 页面路由

| 路由 | 页面 | 功能 |
|---|---|---|
| `#/` / `#/list` | 事件列表 | 搜索/筛选（标签/红移/RA/Dec）/排序（ID/红移/T0，默认 T0 倒序、T0 为空的排最后）/分页/删除/全局银消改正 |
| `#/stats` | 全局统计 | 概览卡片 + Mollweide 全天图 + 红移直方图（按 tag 筛选）+ 波段覆盖 |
| `#/stats/relations` | 统计关系 | GRB 瞬时辐射 6 个 2D 统计关系（Amati 等）散点 + 分组拟合（见 §8.13） |
| `#/transient/<id>` | 单源详情 | 基本信息 + 子标签 + 光变曲线 + 全列数据表 + 行内编辑 |
| `#/compare` | 多源对比 | 叠加对比光变曲线（含误差棒显示开关）；事件列表支持按名称/别名筛选；模板层可叠加 K 改正引擎预测曲线（虚线），Y 轴三态流量/绝对星等/K 改正绝对星等，支持服务端 CSV/JSON 导出与复制图（见 §8.34） |
| `#/filters` | 光学滤光片 | 81 个滤波器 CRUD（类型 mean/ref/eff/guess 齐全），可排序/添加/行内编辑 |
| `#/new` | 新建事件 | 创建暂现源 |
| `#/tools/gcn` | GCN 阅读工具 | 工具箱条目：GCN circular 浏览 + 源信息/测光录入直写数据库（见 §8.17） |
| `#/tools/digitizer` | 抠图取数 | 工具箱条目：从图像提取数据点，直写 lightcurves 表（见 §8.18） |
| `#/tools/tmplib` | 模板库与造模板 | 工具箱条目：模板清单（状态/域内率/批量重建）+ 详情 QC 与误差预算面板 + 造模板向导（4 步，见 §8.34） |
| `#/tools/specphot` | 光谱 × 滤光片 | 工具箱条目（P1 已挂载）：谱来源（库内/上传粘贴）+ 元数据回填（`meta_provenance` 标签）+ 锚点表 + 参数面板 + 结果表（`err_source`/`err_scope` 成对展示）+ 告警区 + 对照面板；`#/tools/specphot/<spectrum_id>` 直达库内谱（S1 合成测光，API-2） |

### 6.2 单源详情页标签

| 标签 | 内容 |
|---|---|
| 概览 | 基本信息表、子标签、Aladin Lite 天球图 |
| 光变曲线 | Chart.js 多波段光变曲线，Y 轴切换 mJy/绝对星等，X 轴切换线性/对数，顶部 day/MJD 副轴，静止系选项，误差棒显示开关（勾选时有 time_err 的探测点同绘水平时间误差棒），数据点 tooltip 含波段与望远镜（v2.20），波段勾选面板（全选/全不选），原始/银消改正数据切换，经验函数拟合叠加（pl/bpl/sbpl/fred，结果显示每参数 ±1σ 与 1σ 置信带，v2.20 起置信带由 emcee 后验样本分位给出），「复制光变图」按钮（PNG 复制到剪贴板，剪贴板不可用时回退下载），「显示当前时刻」红色竖虚线开关（默认关，无 T0 禁用），时间轴刻度科学计数法，缩放/重置/导出 |
| 数据表 | 全部 24 列光变数据（含银消后 AB 星等及误差、来源 source、存入/修改时间），支持行内编辑 + 添加记录（v2.20 起添加后自动滚动到底部新行） + 多选批量删除 + 上传数据表（CSV 列映射导入）+ 单点银消改正（删除/银消仅管理员；行内编辑管理员任意记录、普通用户仅自己录入的记录（source=本账户），其余记录可扣点）；「列显示」面板勾选要展示的列（localStorage 持久化，默认紧凑子集：时间/时间误差/波段/流量/误差/单位/星等系统/银消/上限/银消量/望远镜/仪器/引用/备注；仅影响页面显示，导出 CSV 始终为服务端全列完整版）；首列勾选框标记行 → 整行橙色高亮（仅前端定位用，不写库），表头复选框可全标/全清；「添加记录」表单：波段可输入+下拉建议（滤光片 id 按波长排序+本源已有波段）、单位下拉（mJy/uJy/Jy/cgs/keV 系/mag）、时间与时间误差各配乘积因子下拉（×86400/×3600/×60/×1，前端换算为秒入库），改 time 的因子即按「time×新因子」重算联动 MJD（v2.35）；time↔MJD 联动（源有 T0 时）：填一侧、另一侧在失焦/改因子时补算，用户手填的一侧永不被覆盖；用户手填 time 时请求体**不带**自动补算出的 MJD（交服务端按 time×因子反算）⇒「MJD 列权威」仅当用户手填 MJD 才生效（v2.35 修正「半截输入定格 MJD」「改因子不换算」两缺陷）；添加记录后停留在数据表页（批量删除等整页刷新同样不再跳回概览） |
| 余辉拟合 | VegasAfterglow 正向激波拟合：配置/提交任务/状态轮询/结果展示（见 §8.14） |
| 光谱数据 | 光谱列表（多选对比/逐条偏移/删除）+ 波长-流量图；观测者系横轴 + 有红移时静止系副轴（λ/(1+z)，逐帧同步）；绝对/相对流量模式（相对模式按中值归一+用户偏移）；误差条可开关；**横纵轴各自可切线性/对数**（对数 Y 自动滤除非正流量点）；坐标范围设置（数字输入 xmin/xmax/ymin/ymax + 图上拖拽框选缩放 + 恢复默认）；TNS 风格谱线标记面板（30 组常见谱线 H/He/C/N/O/…+自定义波长+Tellurics+星系线+WR 线，逐组 z 与 v_exp 可调，λ=λ₀(1+z)(1−v/c)，详见 §6.5）；上传光谱（登录后）；每行下载按钮（`/api/spectra/<id>/download`，公开）；v2.19 起列表按父子分组展示银消改正二级谱（子行缩进+「银消改正」徽标，父子独立勾选绘图；v2.20 起登录用户即可对父行执行/覆盖银消改正，见 §8.28） |
| 余辉SED分析（开发中） | 预留 |

### 6.3 鉴权行为

- 登录需用户名 + 密码；管理员账户为 `admin`（密码取 `AJST_CATALOG_PASSWORD`，未设置时每次启动随机生成），普通用户由管理员在 `/admin` 后台维护
- 导航栏登录后显示当前账户名；管理员额外显示「管理后台」入口（`/admin`）
- 数据表"添加记录"/"上传数据表"按钮与编辑列登录后显示；普通用户编辑列内：自己录入的记录（source=本账户）显示行内编辑按钮，其余记录只有扣点（discard 切换）按钮；单点银消/删除选中与行首删除复选框列仅管理员可见
- 事件编辑、derived 卡片编辑、银消改正按钮仅管理员可用
- T0 等事件字段：新建源时普通登录用户可设定；**库中已存在源的修改仅管理员**（PUT 已 `@require_admin`；POST 新建对重复 id 一律 409，无经新建覆盖已有源的旁路；契约由 tests/acceptance/test_l2_t0_permissions.py 锁定，v2.30）
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
    ├── api.js                # API 客户端 + 鉴权状态（唯一实现，filters/admin 均复用；401 时派发 ajst:auth-expired 事件刷新导航）
    ├── utils.js              # 共享工具：esc/escAttr/fmtNum/safeUrl/minOf/maxOf/sci3 等（2026-09-12 起各页面统一引用）
    ├── chart_plugins.js      # Chart.js 误差棒插件统一实现（各图表页共用，2026-09-12 起）
    ├── layout.js             # 页面容器组件
    ├── spec_lines.js         # TNS 风格谱线标记：谱线组数据 + Chart.js 标记插件 + 面板 HTML（v2.7）
    ├── coords.js             # 坐标解析（度 ⇄ 时分秒，含单值 sexagesimal）+ 输入即时提示（v2.14，见 §8.22；GCN 工具共用）
    ├── bands.js              # 波段工具：频率排序/光谱色阶/AB↔mJy/滤波器缓存（v2.14 起各图表共用）；toMJy/pointToMJy 为流量单位换算唯一实现（2026-09-12 收敛；未知单位返回 null 弃点 + console.warn，y≤0 在 log 轴截断至 1e-13 并在 tooltip 标注，绝对星等模式不绘制截断点）
    ├── digitizer_core.js     # 抠图取数核心算法（标定变换/Lab 颜色/掩膜/描线/连通域，v2.11）
    └── pages/
        ├── home.js           # 主页（瑞士排版：标题/文案/基础统计/事实行/标签分布/入口；事件日历挂载点）
        ├── home_calendar.js  # 主页事件日历（GitHub 贡献日历风格，按 T0 逐日计数，v2.30）
        ├── list.js           # 事件列表（筛选/排序/分页/删除）
        ├── detail.js         # 单源详情编排层（fetch → 注入子模块 → tab 切换；2026-09-12 拆分后约 500 行）
        ├── detail_overview.js    # 详情页概览卡/文章管理/基本信息编辑/全源银消改正
        ├── detail_lctable.js     # 详情页数据表（行渲染/排序/行内编辑/批量删/CSV 上传；单点编辑局部行替换）
        ├── detail_lcchart.js     # 详情页光变图（状态/构建/缩放/拟合叠加/轴范围）
        ├── detail_spectra.js     # 详情页光谱标签页
        ├── detail_derived.js     # 详情页 derived 参数卡
        ├── detail_catalog.js     # 详情页外部目录数据卡
        ├── detail_aladin.js      # 详情页 Aladin 实例管理
        ├── lc_upload.js      # 数据表 CSV 上传（列映射预览导入，v2.9）
        ├── gcn_tool.js       # GCN 阅读工具（工具箱，v2.10，见 §8.17）
        ├── digitizer.js      # 抠图取数（工具箱，v2.11，见 §8.18）
        ├── fitting_tab.js    # 详情页"余辉拟合"标签页（配置/任务轮询/结果展示，见 §8.14）
        ├── stats.js          # 全局统计（全天图/红移柱状图/波段覆盖）
        ├── relations.js      # 统计关系（6 关系平铺卡片/来源切换/分组 OLS 拟合/导出 CSV）
        ├── hostfit_tab.js    # 详情页「宿主星系」标签页（宿主信息/测光表/pcigale·prospector 拟合，见 §8.20、§8.22）
        ├── sed_tab.js        # 详情页「SED 分析」标签页（SED 构建/拟合/结果/时间序列诊断，见 §8.27）
        ├── stats_hosts.js    # 宿主星系统计子页（覆盖率/M*/SFR/绝对星等—红移图，见 §8.22）
        ├── create.js         # 新建事件
        ├── compare.js        # 多源对比（Y 三态流量/绝对星等/K 改正；复制图）
        ├── compare_template.js # 对比页模板层（K 改正预测曲线叠加/Δμ 卡/域判定条/导出入口，见 §8.34）
        ├── tmplib.js         # 模板库与造模板向导（工具箱，见 §8.34）
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
| `AJST_HOST` | `backend/start.sh` 监听地址；**非 loopback 值会被拒绝并报错退出**（本机约定只监听 127.0.0.1） | `127.0.0.1` |
| `PORT` | `backend/start.sh` 监听端口 | `27101` |
| `FLASK_ENV` | 运行环境 | `production` |
| `AJST_SECRET_KEY` | Flask 会话签名密钥 | 未设置时每次启动随机生成（重启即全体登出），生产建议显式设置 |
| `AJST_CORS_ORIGINS` | CORS 允许来源白名单（逗号分隔；会话基于 cookie，不支持通配） | `localhost/127.0.0.1` 的 `$PORT`（默认 27101）/8080 端口；显式设为 `'*'` 恢复旧通配行为（不推荐） |
| `AJST_PCIGALE_BIN` | hostfit 使用的 pcigale 可执行文件路径 | 未设置时按 `shutil.which('pcigale')` → 内置回退路径查找；实际选用路径记录在任务 run.log |
| `AJST_PCIGALE_FILTER_DIR` | pcigale 滤光片库目录覆盖项（网页端滤光片曲线注册与 `scripts/fetch_svo_filters.py` 共用） | 未设置时按已安装 pcigale 包路径 → which('pcigale') 推导 |
| `SPS_HOME` | hostfit prospector 引擎所需的 FSPS 数据目录 | 未设置时回退读 `AJST_SPS_HOME`；prospector 为可选依赖（runner 惰性导入），未安装/未配置不影响服务启动，仅运行 prospector 任务时报错 |
| `AJST_SPS_HOME` | `SPS_HOME` 未设置时的替代 FSPS 数据目录变量 | 无 |
| `AJST_TMPLIB_DIR` | 模板库根目录覆盖项（模板库 × K 改正的数据，见 §2.8/§8.34） | `<项目根>/catadata/tmplibrary` |

安全相关默认行为（2026-09-12 起）：登录接口对同一 IP+账户 15 分钟内失败 5 次锁定（429）；
`SESSION_COOKIE_SAMESITE='Lax'`；请求体上限 `MAX_CONTENT_LENGTH=32MB`（超限 413）；
`POST /api/lightcurves/fit_model` 与统计关系页的服务端拟合端点需登录（匿名访问返回 401，
关系统计页会自动回退浏览器本地 OLS 拟合）。

### 7.2 启动/重启

**开机自启（v2.7 起，systemd 用户服务）**：

- 服务单元：`~/.config/systemd/user/ajst-catalog.service`，`systemctl --user enable ajst-catalog`；
  如需不开登录会话也随开机启动，执行 `loginctl enable-linger <用户名>`
- 单元文件中用 `%h` 表示家目录（systemd 用户服务占位符），启动命令指向
  `<AJST>/backend/start.sh`（`<AJST>` 为项目根目录）
- 启动脚本：`backend/start.sh` 为通用脚本——`cd` 到脚本所在目录后以
  `${AJST_PYTHON:-python3}` 启动 Flask，监听 `${AJST_HOST:-127.0.0.1}:${PORT:-27101}`；
  非 loopback 的 `AJST_HOST` 会被脚本拒绝并报错退出（本机约定：服务只监听 127.0.0.1，
  需要外部可达请走 ssh 隧道，不要改成 0.0.0.0）；
  其余配置（`DATABASE_URL` / `AJST_DATA_DIR` / `AJST_CATALOG_PASSWORD` /
  `AJST_INGEST_TOKEN` / `AJST_SECRET_KEY`）全部从进程环境变量读取，需显式提供——
  **本机做法**：凭据放 `~/.config/ajst.env`（`chmod 600`），单元文件用
  `EnvironmentFile=-%h/.config/ajst.env` 注入（systemd 不读 `~/.bashrc`，只写在 shell 配置里的
  凭据对服务等于没配，2026-09-18 踩过）。**注意：`AJST_CATALOG_PASSWORD` 不显式设置时每次启动都会
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
create_app().run(host='127.0.0.1', port=27101, debug=False)
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

**消光派生缓存回填（2026-09-21 起）**：全量重建或直接 SQL 改库之后，把派生缓存补齐
（幂等、可随时重跑；缺失只影响速度不影响正确性，见 §四）：

```bash
cd <AJST> && python3 scripts/backfill_gext_cache.py
# 输出示例：
#   transients.gext_distmod: 963/2794 updated (1.03s)
#   hosts.gext_distmod     : 31/32 updated (0.00s)
#   filters.gext_coeff   : 0/81 updated (1.64s)
#   coordless NULLs      : 126 rows
#   transients.gext_ebv  : 0/2738 updated (2.89s)
#   hosts.gext_ebv       : 0/25 updated (0.02s)
```

距离模数 μ 只依赖 astropy，故这一步放在 dustmaps 可用性检查**之前**：dustmaps 缺失时仍会回填
`gext_distmod`，只跳过 `gext_coeff` / `gext_ebv`（避免把已有系数写坏）。

> **新版本部署顺序**：改完后端代码 → `systemctl --user restart ajst-catalog`（启动时跑幂等列迁移、
> 补出新列）→ **再**跑 ETL / 回填脚本。顺序反过来会因列不存在而报错。

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

普通用户的增删改在 `http://localhost:27101/admin` 后台操作。

---

## 八、扩展指南

### 8.1 添加新属性（推荐：extra_data JSONB）

```bash
curl -X PUT http://localhost:27101/api/transients/EP240315a \
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
- token 配置：写入 `~/.config/ajst.env`（`chmod 600`），由单元文件的
  `EnvironmentFile=-%h/.config/ajst.env` 注入，重启服务生效（与 `AJST_CATALOG_PASSWORD`
  同机制，见 §7.1/§7.2）。**只写 `~/.bashrc` 对服务无效**（systemd 不读 shell 配置）

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

**经验函数拟合**（`POST /api/lightcurves/fit_model`，无需登录，同步求解（v2.20 起多起点最小二乘 + emcee 后验采样），结果不落库）：

- 请求体：`{"model": "pl"|"bpl"|"sbpl"|"fred", "points": [{"t","f","ferr"}...], "bounds": {"tb": [lo, hi]}}`
  （`bounds` 可选，bpl/sbpl 拐点 tb 的预设范围（秒），与数据范围取交集）
- 函数形式（前端 `detail.js fitModelFlux` 为严格镜像；v2.20 起最少点数统一放宽为参数数
  ——pl=2 / bpl=4 / sbpl=5 / fred=4，N==参数数时走退化拟合，见下）：
  - `pl`：`F = A·t^(−alpha)`
  - `bpl`：分段幂律，tb 处连续，`A = Fb·tb^alpha1`
  - `sbpl`（v2.12）：平滑断裂幂律 `F = Fb·[(t/tb)^(n·alpha1)+(t/tb)^(n·alpha2)]^(−1/n)`，
    平滑因子 n>0（越大越尖锐，n→∞ 退化为 bpl）；后端用 logaddexp 数值稳定实现
  - `fred`（v2.18）：Norris+2005 脉冲模型 `F = A·exp(2μ)·exp(−τ1/u−u/τ2)`，
    u = x+μ−x1 > 0，μ = (τ1/τ2)^{1/2}（此归一化使 A 即峰值强度；峰值位于
    x = x1+(τ1τ2)^{1/2}−μ），参数 A/τ1/τ2/x1
- 内部以拐点/参考时刻锚定再参数化（Fb/tb）。v2.20 起引擎升级：核心抽为纯函数
  `fit_lightcurve_model(model, t, f, sig, req_bounds)`（不依赖 Flask，route 只做参数解析）；
  最少点数放宽为参数数（`LC_FIT_NPAR`：pl=2 / bpl=4 / sbpl=5 / fred=4），N==参数数为
  **退化拟合**（仅 least_squares，`param_errors`/`param_cov`/`samples` 为 null，返回
  `degenerate: true` + 中文 note）；fred/bpl/sbpl 用**多起点** least_squares 取 cost 最小
  （tb 种子=峰值时刻 + 对数时间 25/50/75% 分位；fred 的 τ1/τ2 四组启发组合）；非退化
  拟合再加 **emcee 第二阶段**（16 walkers，burn 400 + 采样 600，log-prior=bounds 内均匀，
  logL=−χ²/2，种子固定 20260918——注意 emcee 3.1.6 用
  `sampler.random_state=np.random.RandomState(...)` 而非构造器 seed）；emcee 失败回退
  SVD 协方差且 `sampler` 字段注明 fallback。单次拟合实测 <0.8s
- 返回（v2.20）：`param_errors`=后验 16/84 分位半宽、`param_cov`=样本协方差
  （`{keys, matrix}`，形状不变向后兼容，求解失败为 null）、新增 `samples`
  （~200 个输出参数基 dict 列表）与 `sampler` 字段；前端据此在拟合结果列表显示
  每参数 ±1σ，置信带优先走 samples 路径（120 点网格逐样本求值取 16/84 分位带，
  修复 bpl 置信带断点不连续 / sbpl 连接处奇异值 / fred 多模），samples 缺失回退
  param_cov 经数值 Jacobian 传播 σ(t) 的旧路径；前端点数预检同步放宽、
  退化拟合弹 toast 提示
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
- 「复制光变图」按钮（v2.18）：当前光变图（带标题与图例）以 PNG 复制到剪贴板；
  非安全上下文 / 剪贴板不可用时回退为下载 PNG
- 「显示当前时刻」开关（v2.18，默认关，源无 T0 时禁用）：图上绘制贯通红色竖虚线
  标记当前时刻（按 T0 起算的时间差定位，随静止系开关做 (1+z) 换算），不改变坐标轴范围
- 时间误差棒（v2.18）：勾选误差棒时，有时间误差（time_err）的探测点同时绘制水平时间误差棒
- 时间横轴刻度标签改科学计数法（v2.18，如 7e3，整数尾数无小数位）
- `Y: 绝对星等`（无红移禁用）：M = m_AB − distmod，线性反向星等轴（右轴 AB 视星等隐藏），
  星等空间误差 σ_m = (2.5/ln10)·σ_F/F
- 多源对比页同样新增 `Y: 绝对星等`（按各源红移分别计算，无红移源不显示）

**学术绘图风格**：刻度/轴标题/图例统一衬线字体（`theme.js ACADEMIC_FONT`，
Times/STIX/Noto Serif SC 回退链），轴线描边、网格弱化，覆盖详情页光变图、
多源对比图与拟合标签页图。

### 8.20 宿主星系与 pcigale / prospector SED 拟合（2026-08-29 新增；2026-09-18 起双引擎）

- 数据模型：`host_galaxies` 表（§2.6），每源一行；随 `--dump` 往返 `info/<tid>.json` 的 `host_galaxy` 字段。
- 详情页「宿主星系」tab（`frontend/js/pages/hostfit_tab.js`）：宿主信息编辑（坐标/红移/红移类型）、
  多波段测光表（mag + err + mag_sys AB/Vega/ST + 来源，逐行勾选参与拟合）、拟合配置卡
  （v2.18 起顶部**引擎单选 pcigale / prospector**，按引擎显示对应参数区；固定红移 /
  测光红移模式）、任务队列（5s 轮询，任务列表与结果区显示引擎徽标）、结果展示
  （best/bayes 参数表 + SED 图 + 产物下载；prospector 结果另含 corner 角图）。
- 拟合子系统 `backend/hostfit/`：作业记录复用 `fitting_results` 表（model_name 按引擎为
  `pcigale_host` / `prospector_host`，状态在 extra_data.status，extra_data.engine 记录引擎；
  v2.18 前缺 engine 字段的存量任务按 model_name 推断），`jobs.py` 按引擎分发 runner，
  产物目录沿用 `fitting_store/<tid>/hostfit_<jobid>/`，不进数据仓库、全量重建清空（与余辉拟合一致）。
- **pcigale 引擎**（`runner.py`）：生成 pcigale 输入（AB/Vega/ST → mJy 换算，缺 Vega2AB 或无
  pcigale 滤光片的波段跳过并警告）→ 子进程 `pcigale run`（基础链 sfhdelayed+bc03
  +dustatt_modified_CF00+redshifting，pdf_analysis；nebular / dl2014 为前端可勾选的可选模块，
  参数固定默认值；当前模型组合与网格模型数提示实时显示在前端）→ 解析 results.txt →
  matplotlib 生成 sed.png。配套 pcigale.ini.spec 由 runner 从随代码分发的超集模板
  （`backend/hostfit/pcigale.ini.spec`）按启用模块逐任务裁剪生成——pcigale 2025.0 要求 ini
  覆盖 spec 声明的所有段，缺整段报 "parameter None: False" 且 exit 0、不产出 out/（2026-09-04
  修复，runner 对该情形有防御性识别，校验失败与拟合失败分开报错）。
  2026-09-18 排查补充：pcigale 的模型归一（scaling）按**绝对误差**加权做解析
  最小二乘，网格若全是年轻模型（age_main 上限 ≪ 宇宙年龄），最暗波段会钉死缩放，
  best m_star 可差几个数量级而任务状态仍是 done——默认 age_main 网格已扩展至
  13000 Myr（超宇宙年龄的模型 pcigale 自动置 NaN），且 runner 在 reduced χ²>10 时
  向 warnings 与 run.log 写"参数不可信、请扩大网格"的显式告警；评估历史
  pcigale_host 结果时以各自 χ²_red 与 warnings 为准。
- **prospector 引擎**（`runner_prospector.py`，v2.18 新增）：
  - 惰性 import prospect / sedpy / fsps——可选依赖（requirements.txt 有对应注释段：
    astro-prospector / astro-sedpy / python-fsps / dynesty / emcee / h5py / corner /
    matplotlib）未安装不影响服务启动与 pcigale 引擎，仅提交 prospector 任务时报错；
    需 FSPS 数据目录（`SPS_HOME`，未设置时回退 `AJST_SPS_HOME`，见 §7.1）。
  - 模型：parametric_sfh（delayed-tau：mass / tage / tau / logzsol / dust2）+ 可选组件
    use_nebular（电离气体发射线）/ use_duste（尘埃红外再辐射）/ use_igm（默认开）；
    先验范围可配（mass/tau 对数均匀，tage/dust2/logzsol TopHat）；红移模式 fixed 或
    photoz（zred 自由，TopHat [z_min, z_max]）。
  - 采样器：dynesty（默认 nlive=100，上限 500）或 emcee（默认 32 walkers × 3000 迭代、
    burn 500；上限 nwalkers 128 / niter 20000）。
  - 输出参数 {best, bayes, bayes_err}：自由参数 + 派生量 m_star（存活恒星质量）、
    mass_formed、sfr（delayed-tau 解析式，100% 形成质量归一）、Av（= dust2 × 1.086）、
    tage；photoz 模式另有 redshift / redshift_err；chi_squared 为最佳拟合约化 χ²。
    派生量由 ≤200 个后验子样本逐个计算后取中位数与 1σ。
  - 产物：result.json / results.txt / sed.png / corner.png / run.log。
  - 端到端验证：以 GRB170817A 宿主（NGC 4993）的文献测光跑通，
    m_star ≈ 1.2×10¹¹ M☉，与文献一致。
- **测光输入口径两引擎一致**：跳过 upperlimit 行、mag_err 缺省按 σ=0.2 mag、AB/Vega/ST
  星等系统换算、银消改正同一实现（`extinction.correct_host_phot`，见 §8.24）；
  **波段可用性判据不同**——pcigale 需要 `extra_data.pcigale_name`（pcigale 滤光片名），
  prospector 需要滤光片透过率曲线（`extra_data.transmission`），且有效波段须 ≥4。
- API（v2.18）：`GET /api/hostfit/config` 返回结构改为按引擎分节
  `{pcigale:{defaults,modules,optional_modules,available_bands}, prospector:{defaults,optional_modules,available_bands}}`
  （前端兼容旧扁平结构）；`POST /api/hostfit/jobs` 的 config 新增 `engine`
  （`'pcigale'` 默认 / `'prospector'`）；任务简报/详情新增 `engine` 字段；
  files 新增 `corner` 类（prospector 角图）。
- 「写入宿主信息」：pcigale 结果照旧把 bayes 参数写入 `derived`；prospector 结果映射
  derived={m_star, sfr, age_main（tage Gyr→Myr）, Av_ISM, chi2, fit_at, job_id, engine}；
  测光红移模式两引擎均同时回写 redshift/redshift_err/redshift_type='phot'。
- 滤光片：`FilterDef.extra_data` 存 `pcigale_name`（如 `sloan.sdss.g`）与 `transmission`
  透过率曲线（SVO FPS 拉取，脚本 `scripts/fetch_svo_filters.py`；pcigale 2025.0 的
  `pcigale-filters add` 在 numpy≥2 下需 np.trapz→trapezoid shim，脚本已内置）。
- 权限：查看公开；宿主信息编辑/提交拟合/写回=登录；删除宿主/删除任务=管理员。
- 列表页「宿主」徽章 + `has_host` 筛选；新建事件页可折叠宿主字段；首页与统计子页
  `/stats/hosts`（覆盖率、M*/SFR 分布、宿主绝对星等—红移图；v2.14 起替换原 z-z 散点，见 §8.22；
  v2.16 起新增 M*—z / SFR—z 散点、等星等参考虚线与每图 CSV 下载，见 §8.25）。

### 8.22 坐标时分秒输入与宿主测光增强（2026-09-03，v2.14）

- **坐标输入**：事件（新建页 / 详情页概览编辑）与宿主星系（宿主标签页 / 新建页折叠区）的
  RA/Dec 均支持十进制度与时分秒格式（`08h08m27.4s`、`8:08:27.41`、`8 08 27.41`、
  `+40d36m44.8s`、`40°36'44.8"` 等）。前端 `js/coords.js` 即时显示换算结果（非法红字提示），
  入库转换以后端 `backend/coords.py`（astropy `Angle`）为准：RA 按小时角归一化到 [0,360)，
  Dec 限 [-90,90]，非法输入 400。
- **宿主测光表**（详情页「宿主星系」tab）：band 列改为 datalist 下拉 + 打字模糊过滤
  （候选取自 filters 表，按波长排序），输入波段不在库中时行内红字提示（仍可保存）；
  新增「上限」列（`upperlimit`，上限行不参与拟合，runner 侧亦跳过）；
  `mag_err` 可留空，非上限且误差为空的点后续处理（拟合 / 统计图误差棒）按 σ=0.2 mag 计
  （占位符与表下注释提醒，0.2 不写入记录）。
- **宿主统计图**：`/api/stats/hosts` 移除 `z_pairs`，新增 `abs_mag_points`
  （[{tid, band, z, mag(AB), abs_mag, mag_err, err_assumed, upperlimit}]）：
  M = m_AB − μ(z_host)，μ 用 `models.distance_modulus`（astropy Planck18）计算，
  Vega 星等先按 filters 表 vega2ab 转 AB，ST 暂不换算；`err_assumed` 标记缺省 0.2 的点。
  前端 `#/stats/hosts` 新增「宿主星系绝对星等 — 红移」图：横轴 z（线性），纵轴可切
  星等（线性反向）/ 流量密度 mJy（对数，M→10pc 处流量密度）；波段勾选面板（全选/全不选）、
  波段颜色与排序（频率升序光谱色阶）、符号（探测圆点 / 上限倒三角）、误差棒开关均与
  详情页光变图同一套规则（波段工具抽到 `js/bands.js` 共用）。
- **Aladin 遮挡修复**：Aladin Lite v3 全屏为 CSS `position:fixed` 实现且组件内部 z-index
  最高仅 1000，低于 sticky navbar（1020），导致全屏/短视口滚动时顶排按钮（全屏/关闭等）
  被页眉遮挡。修复：`#aladinContainer` 抬升为 `z-index: 1030` 的层叠上下文。

### 8.23 新增滤光片时获取透过率曲线（2026-09-04）

- 「添加滤光片」弹窗（`frontend/js/pages/filters.js` 的 filterShowAdd/filterAddSave）在基本
  字段下新增可选的「获取透过率曲线」步骤，四种选择：跳过（仅建定义，弹窗内有"缺失透过率
  曲线的滤光片无法用于宿主星系拟合等相关过程"提示）、pcigale 内置映射、上传曲线文件、
  SVO 搜索。
- 后端逻辑集中在 `backend/filtercurves.py`（路由在 `backend/routes/filters.py`）：
  - **pcigale 内置**：`GET /api/filters/pcigale_builtin` 列名（排除自注册的 `ajst_*`）；
    创建时 `curve={kind:'pcigale_builtin', name}`，后端读 pickle（nm→Å、峰值归一）
    写入 extra_data 并置 `pcigale_name`（已在库中，无需注册）。
  - **上传曲线**：`POST /api/filters/parse_curve` 校验两列文本（恰好两列数值、波长严格
    递增、0 ≤ 透过率峰值 ≤ 1.5、≥10 点，错误含行号）；创建时
    `curve={kind:'upload', text}`，服务端重新校验、峰值归一后写入
    `extra_data.transmission`，并生成 pcigale .dat 注册进 pcigale
    （`pcigale_name='ajst_<id>'`，非字母数字字符转 `_`）。
  - **SVO 搜索**：`GET /api/filters/svo_search?q=` 解析 SVO FPS 搜索页 HTML 返回候选
    （id/facility/instrument/description，上限 50 条；SVO 无机读搜索 API，
    `fps.php?FORMAT=votable` 只返回空 schema）；`POST /api/filters/svo_fetch` 预览抓取；
    创建时 `curve={kind:'svo', svo_id}`，抓取（fps.php VOTable 优先、getdata.php ascii
    兜底）、归一、注册 pcigale，extra_data 记录 `svo_id`。
- pcigale 注册失败**不阻断**创建：transmission 照常写入，`pcigale_name` 置 null，
  响应带 `warning` 字段，前端提示"曲线已保存但 pcigale 注册失败，hostfit 暂不可用"。
  进程内注册沿用 np.trapz→trapezoid shim（同 `scripts/fetch_svo_filters.py`）。
- `POST /api/filters` 对曲线校验错误返回 400（`{error, message}`）且不建条目；
  SVO 网络/解析错误返回 502。解析/搜索端点均不落库，可独立于创建流程复用。

### 8.24 宿主测光银河系消光改正标记与应用（2026-09-04，v2.15）

- **字段**：宿主测光表每行新增 `gext_corr`（bool，「该行是否已做银河系消光改正」）。
  PUT `/api/hosts/<tid>` 时**每行必须显式携带** true/false，缺失返回 400 并指明行号与波段；
  库中缺键的存量行不迁移，下游代码一律按 `false`（未改正）对待（`row.get('gext_corr', False)`）。
- **前端录入**（详情页「宿主星系」tab）：测光表新增「银消已改正」列（是/否下拉）；
  「添加行」的新行默认不预选，保存时若有行未选择则弹显式提醒并红框标出，不允许带空值提交；
  存量行渲染为「否（未改正）」；表下注明"未改正的数据在统计与拟合时会按 CSFD 尘埃图
  + Rv=3.1 + P92 自动改正后使用"。
- **下游统一使用改正后数据**：`backend/extinction.py` 新增 `correct_host_phot(sess, ra, dec, phot_rows)`
  （复用 CSFD 尘埃图查询与 P92 曲线，只算不写库；A_λ 是星等加性量，直接在原星等系统上减，
  与 Vega→AB / ST→mJy 换算可交换）。坐标取宿主 ra/dec，缺省回退暂现源坐标，
  两者都缺或依赖不可用时回退原始值并注明。波段波长超出光学窗口（1000 Å–1 mm）
  或 P92 计算失败的行不改正（reason=not_optical，与 band_no_wavelength 等并列，
  调用方按原始值处理）。
  - `/api/stats/hosts` 的 `abs_mag_points`：gext_corr 非真的行先减 A_λ 再算 M，
    返回点新增 `gext_applied` / `gext_Alambda` 字段（前端 tooltip 标注「已银消改正」）。
  - hostfit 拟合（`hostfit/runner.py` 与 `runner_prospector.py`）：job config 中 gext_corr 非真的行先改正再转 mJy，
    run.log 记录"对 N 个未改正测光点应用了银消改正"（含 E(B-V) 与坐标来源）；
    历史未重跑的 job 不受影响。
  - 导出 `GET /api/export/host_photometry/<tid>?format=csv|json`（需登录）：列含
    band/mag/mag_err/mag_sys/upperlimit/gext_corr/gext_Alambda/mag_gextcor/source，
    `mag_gextcor` 为改正后星等（已改正行=mag，未改正行实时算，无法改正留空）；
    前端宿主信息卡测光表旁有「下载数据表」按钮。

### 8.25 宿主星系统计子页增强（2026-09-04，v2.16）

- **新增两图与重排**：`#/stats/hosts` 新增「恒星质量 — 红移」「恒星形成率 — 红移」散点图
  （纵轴对数；`/api/stats/hosts` 新增 `m_star_points` / `sfr_points`，每项
  `{tid, z, m_star|sfr}`，z 为宿主红移、无则 null 由前端跳过）。页面顺序改为：
  统计卡片 → 绝对星等—红移图（通栏）→ M*/SFR 分布直方图（并排）→ M*—z / SFR—z（并排）。
- **等视星等参考虚线**：绝对星等—红移图头部新增「等星等线」勾选 + 视星等 m 输入框，
  勾选后绘制红色 dashed 曲线 M(z) = m − μ(z)，z 从 0 到当前勾选波段数据点
  （含上限点）的最大红移，随波段勾选/输入/Y 轴模式实时更新；mJy 模式下曲线同步换算。
  前端 `planck18Distmod(z)`（`stats_hosts.js`）为 Planck18 平直 ΛCDM 的 Simpson 数值积分，
  大质量中微子（0.06 eV）按物质项计入，与后端 `models.distance_modulus`
  （astropy Planck18）实测偏差 <0.001 mag（z≤8）。
- **每图 CSV 下载**：5 个统计图卡片头部右侧均有「数据」小按钮（前端 Blob 拼 CSV，
  文件名带日期）。分布直方图导出原始数据（每宿主一行 id/redshift/值，非分箱计数）；
  M*—z / SFR—z 导出绘图点（仅含 z 非空的行）；绝对星等—红移图只导出当前勾选波段，
  每行含银消改正前后星等——为此 `abs_mag_points` 每项新增
  `mag_raw`（库中原始星等）/ `mag_corr`（银消改正后、星等系统换算前）/ `mag_sys` /
  `gext_corr` 四个字段，CSV 列为 id/band/redshift/mag_raw/mag_sys/gext_corr/
  gext_Alambda/mag_corr/mag_ab/abs_mag/mag_err/err_assumed/upperlimit。

### 8.26 滤光片页增强与删除保护（2026-09-04）

- **透过率总览图**（`frontend/js/pages/filters.js`）：
  - 配色改为按**序号**归一化：有曲线的滤光片按波长从小到大排序，第 i / 共 N 条取
    i/(N−1)（N=1 取 0.5）映射 9 锚点 Spectral 色阶（1−t 取样，短波蓝/紫→长波红），
    不再按波长数值归一化；无曲线条目在表格 ID 上取色阶中点色。
  - 图例点击加粗不再改变图例顺序：dataset 顺序固定为列表顺序，置顶绘制改用
    Chart.js dataset `order`（加粗 0、普通 1）；图例色块为实心方框（dataset 显式带
    `backgroundColor`，line 型 legend 的 fillRect 用它填充）。
  - 横轴范围自定义：图下方「最小/最大波长 (Å)」输入框 + 应用/恢复自动按钮（回车即
    应用），留空 = 自动全范围；写入 chart 的 `scales.x.min/max`（留空置 undefined）。
- **添加弹窗**：Vega→AB 输入框下注明「AB = Vega + offset」；上传曲线的格式提示改为
  分点列表（两列/波长单位 Å/无表头/# 与空行忽略/分隔符自动识别/支持 csv·txt·dat/
  透过率预归一）。单条曲线弹窗 footer 新增「下载曲线数据表 (CSV)」按钮（前端 Blob 拼
  `wavelength_A,transmission` 两列，文件名 `filter_<id>_transmission.csv`）。
- **编辑态补录曲线**：行内编辑行新增「获取/补录透过率曲线」按钮，打开独立弹窗复用
  §8.23 三种获取方式（pcigale 内置/上传/SVO），提交到新增端点
  `POST /api/filters/<id>/curve`（仅管理员）——复用 `filtercurves.build_curve_extra`，
  结果按合并语义写入 extra_data（覆盖 transmission/pcigale_name/svo_id），
  upload/SVO 同样注册进 pcigale（`ajst_<id>`），注册失败不阻断（响应带 `warning`）。
- **编辑取消修复**：取消按钮从引用函数内部名的 `onclick="loadFilters()"`（个别时机下
  未生效）改为绑定模块级 `window.filterEditCancel`（重新拉取列表恢复该行显示态）。
- **删除保护**：「删除所选」流程改为 两次 confirm（列 ID + 影响警告 → 再次确认）→
  密码弹窗（`type=password` 输入管理员密码）。`DELETE /api/filters/<id>` 要求 JSON body
  带 `password`，后端用与登录相同的 `User.check_password` 校验**当前登录管理员**的
  密码；缺失/错误返回 403（不用 401，避免前端把密码错误误判为登出）。

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

### 8.27 暂现源 SED 分析（2026-09-13 新增，v2.17）

详情页「SED 分析」标签页（`backend/sedfit/` + `backend/routes/sedfit.py` +
`frontend/js/pages/sed_tab.js`），面向暂现源本体的频谱能量分布构建与拟合，
与宿主星系 SED 拟合（§8.20）互补。四个区块：SED 构建器、模型拟合、任务与结果、
时间序列诊断。

- **SED 构建器（M1）**：`sedfit/builder.py`，同步纯计算。历元选择两种模式：
  时段取值（t_sel±Δt 内各波段直接取点，不插值）与 GP 内插（scipy 手写 RBF 核
  高斯过程回归，log t 空间拟合 log F；只在数据覆盖范围内插值，禁止外推，
  波段点数不足时退化为最近点并标记）。流量优先取银消改正列
  `flux_density_gextcor`（mJy），否则按 AB 零点 16.4 + vega2ab 现场换算；
  频率解析复用 fitting 的 filters 表有效波长 + 波段名（GHz/keV）解析。
  输出含可信度元数据（λ 覆盖、UV/IR 覆盖、z 来源、插值标记）；可选幂律近似
  k 改正。`GET /api/sed/epochs` 按 (最少波段数, Δt 容差) 建议可用历元。
- **模型拟合（M2/M4）**：模型库——幂律+宿主消光 `powerlaw_dust`（smc/lmc/mw/
  mw_f99/mw_ccm 消光律，dust_extinction 实现；smc/lmc 为 G03 平均曲线，mw 为
  P92；R_V 可解锁为自由参数）、两段平滑幂律+宿主消光 `powerlaw_2seg`（单断折，
  硬约束 β1≤β2，平滑度 s 为高级选项默认 3.0）、三段平滑幂律 `powerlaw_3seg`
  （ν_m/ν_c 双断折，硬约束 nu_b1<nu_b2 且 β1≤β2≤β3）、稀释黑体 `blackbody` /
  `2blackbody`（热成分先验 T>2e4 K 防互换）/ `bb_powerlaw`。（v2.18 起移除
  「光学+X 双幂律」模型及其前端关联配置项。）MCMC 复用
  vegas_unified 的 custom_mcmc.py（emcee，worker 内惰性 import，同 fitting 的
  可选依赖约定）；上限点单侧罚。无 z 时黑体退化为 T+R/D 拟合（R/L 不可算并注明）。
  拟合后自动物理检查（β 域/T 域/超光速膨胀等警告）。
- **多历元批量（series）**：`blackbody_series` 按对数均匀历元（或手动勾选）
  逐历元构建+单黑体拟合，产出 T_BB/R_BB/L_BB(t) 序列、三联图与全历元 SED 叠图。
- **闭包关系诊断（M3）**：`sedfit/closure.py` + `closure_relations.json`
  （Gao+2013 框架：ISM/Wind × 慢冷/快冷各谱段 + 拐折后两段；2026-09-13 对照
  Racusin+2009 (ApJ 698:43) Table 1 原文复核修正了无注入系数，并补录能量注入
  关系 8 条（Table 1 列 c，L∝t^q 参数化，alpha 以 {a0,qa,b0,qb} 表示））。
  同步端点输出各关系 α_pred 与偏差 σ 数排名、p 候选值与 α–β 诊断图（PNG）；
  `/api/sed/closure` 与 `/closure_plot` 接受可选 q（能量注入指数，0≤q<1）——
  提供时注入条目代入求值参与排名（图中虚线区分），缺省不参与。
- **伪玻尔兹曼光变（M5）**：`sedfit/bolometric.py`。L_obs 为观测系
  3000–24000 Å（≈U–K）窗口内 F_λ 梯形积分（避免跨射电—光学空隙虚高）；
  L_bb 由逐历元黑体拟合解析积分；L_bc 用 Lyman+2014 颜色→BC 经验关系
  （系数取自 arXiv:1311.1946 源文件 Tables 2–4，含全样本/SE SNe/SNe II/
  SBO 冷却四组）。无 z 源 L_* 全为 null 并注明。
- **任务框架**：克隆 fitting/hostfit 的单 worker 队列模式，复用
  `fitting_results` 表（model_name='sed_*'，无冒号，不串 fitting/hostfit
  列表），产物在 `backend/fitting_store/<tid>/sed_<jobid>/`（sed.png/
  corner.png/result.json/chain_record.h5/sed.csv；series 另有 series.csv/
  trl.png/series.png）。提交需登录、删除需管理员、读取公开；提交校验含
  ≥3 有效波段、拒绝全上限历元、自由度预检（探测点数须多于自由参数数）、
  采样参数上限（nsteps/series_nsteps ≤ 200000、n_workers ≤ 8、nburn < nsteps
  且 ≥100，series 短链 nburn 下限 50）与 bands=[] 拒绝（至少选一个波段）。
  fitting 侧的列表/stop/delete 按 model_name 含冒号过滤与防护，两子系统
  任务操作互不越界。
- **同步端点公开策略**：build/epochs/models/closure(+closure_plot)/bolometric
  等同步端点公开是有意豁免——均为毫秒级纯计算且有算力上限保护（GP 内插
  每波段最多取 60 点、bolometric max_epochs ≤ 30 硬截断、闭包诊断为 O(条数)
  查表）；重负载的 MCMC 拟合一律走需登录的异步任务。
- **前端**：`sed_tab.js` 照 fitting/hostfit tab 模式（惰性初始化、setTimeout
  自递归轮询、竞态防护令牌）；SED log-log 图用共享误差棒插件与框选缩放，
  上限点倒三角标记；闭包诊断可从幂律拟合结果一键填入 β。

---

### 8.28 光谱数据下载与银河系消光改正（2026-09-18，v2.19）

- **下载**：`GET /api/spectra/<id>/download`（公开，口径与 `GET /<id>` 一致）——
  响应为 `#` 注释头元数据（来源/暂现源名/filename/instrument/MJD/observer/
  reducer/u_fluxes/u_wavelengths/spec_type；改正谱另注 gext_corr/parent/
  E(B-V)/Rv）+ 两/三列空白分隔文本（波长Å 流量 [误差]），
  Content-Disposition attachment。前端光谱列表每行加下载按钮（公开可见）。
- **光谱银消改正**（可选，二级产物层级）：
  - 口径与测光点银消改正完全一致（§四）：dustmaps CSFD 尘埃图 E(B-V) +
    dust_extinction P92 模型 + Rv=3.1（复用 `extinction.py` 的 `_load()`/
    `get_ebv()`）。光谱用法差异：`P92().extinguish(λ_观测者系, Av=3.1·E(B-V))`
    得剩余流量比例，改正 `f_corr = f_obs × 10^(+0.4·A_λ)`——对 f_λ 与归一化
    流量同为乘性因子，波长列不动、误差列同乘、**不做 (1+z) 换算**（银河尘埃
    在观测者侧）。u_fluxes 为 normalized/Uncalibrated 的谱允许改正但返回
    warning（仅改变谱形）。
  - 层级：spectra 表新增 `parent_id` 自引用外键（ON DELETE CASCADE；app.py
    init_db 幂等 ALTER 列迁移）；改正谱文件 `<原名>_gextcor.json` 与原始谱
    同目录，文件 JSON 内记 gext_corr/parent_filename/gext_ebv/gext_rv/gext_at
    （全量重建 `import_spectra()` 两遍扫描回读层级，孤儿文件跳过告警）；
    原始谱是唯一可信源，改正谱可从原始谱重新生成覆盖（幂等）；删除原始谱
    级联删除改正谱（DB FK + 文件手动逐个删）；PUT spec_type 传播到全部子行
    （DB+文件同步写）。
  - 端点：`POST /api/spectra/<id>/gext_correct`（登录用户，幂等）返回
    `{spectrum, ebv, warnings}`；子谱/无坐标 400、文件缺失 404；删除改正谱
    用现有 `DELETE /<id>`。列表接口 `spectra_count` 只计原始谱
    （parent_id IS NULL）。
  - 前端：光谱列表父子分组（子行缩进 +「银消改正」徽标，title 显示
    E(B-V)/Rv/CSFD），父子各自独立勾选绘图；父行（v2.20 起登录用户，此前管理员）
    加「银消改正」按钮（已有子谱时 confirm 重新生成覆盖）；删除父行 confirm
    提示级联删除。
  - 验证：合成谱逐点 f_corr/f_obs = 10^(+0.4·A_λ) 偏差 <1e-9；真实谱
    （GRB030329A）改正 E(B-V)=0.0247（CSFD 口径，与测光一致）、级联删除 /
    幂等覆盖 / spec_type 传播均 E2E 通过。

---

### 8.29 消光派生量缓存（2026-09-21，v2.21）

- 新增三个**派生缓存列**：`filters.gext_coeff`（k_λ = Rv·P92(λ)）、`transients.gext_ebv`、
  `host_galaxies.gext_ebv`（各自坐标处的 CSFD E(B-V)），详见 §四；由 `init_db()` 的
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 自动补出，无需手工迁移。
- `extinction.filter_meta()`：滤波器元数据**进程内缓存**（4 列 + 60 s TTL + 写入口显式失效），
  宿主统计的宿主循环不再每宿主重查整张 `filters` 表（原每次约 76 ms 的 transmission JSONB 解码）。
- 新增幂等回填脚本 `scripts/backfill_gext_cache.py`（存量数据 / 直接改库后同步；先检查依赖，不可用则直接退出不改库）。
- 效果（本机实测，30 个宿主规模）：`GET /api/stats/hosts` 约 0.15 s → 0.005 s，响应逐字节不变。
- **缓存读路径必须与坐标同源**：`gext_ebv` 仅在"宿主有自身 ra/dec"时取宿主行缓存，坐标回退暂现源时才取
  暂现源行缓存（`/api/stats/hosts`、`/api/export/host_photometry`、`hostfit` 的银消改正统一此口径）；
  否则宿主测光会拿到源位置的 E(B-V)，实测 72″ 偏移处 g 波段偏差可达 0.07 mag。

### 8.30 距离模数缓存 + ETag / 迁移与口径修复 / pcigale 回退 / 滤光片总览前端（2026-09-21，v2.22）

四项互相独立的改动（同一批合并）：

1. **距离模数持久化 + ETag**：新增 `transients.gext_distmod` / `host_galaxies.gext_distmod`（红移写入时刷新，
   见 §四）；`models` 的距离模数缓存改为「进程内 z 分桶字典 + 向量化批量预热」（`prewarm_distance_modulus()`，
   实测 763 个真实红移下与旧的逐行标量实现结果完全一致）；`/api/transients` 列表、`/api/transients/meta`、
   `/api/stats/hosts` 优先用持久化值，NULL 时回退现算。
   `GET /api/stats/hosts` 新增 `ETag`（由宿主/暂现源 `updated_at` 极值 + 行数 + 滤波器条数/系数/vega2ab/波长
   派生）+ `Cache-Control: no-cache`，数据未变时直接 304（零计算零序列化）。
   ⚠️ token 必须覆盖**所有**影响输出的输入：`filters` 表没有 `updated_at`，最初只放了条数与 `sum(gext_coeff)`，
   实测「PUT `/api/filters/<id>` 改 vega2ab」会让响应体变而 ETag 不变（客户端拿到陈旧 304），已补
   `sum(vega2ab)` 与 `sum(wavelength)`。
2. **迁移/口径修复**：`init_db()` 补 `ALTER TABLE spectra ADD COLUMN IF NOT EXISTS spec_type …` —— 此前
   早于该列入库的旧库查询 spectra 全部 500（`UndefinedColumn`）；`POST /api/ingest/photometry` 写入单位由
   `mag` 改为 `magnitude`（与库内统一口径一致）；宿主统计图 tooltip 把「录入时已改正」与「本页实时改正」
   统一标注为「已银消改正」。
3. **hostfit 的 pcigale 二进制回退**：`_find_pcigale()` 改为 `AJST_PCIGALE_BIN` → `PATH` → 一组已知位置中
   **第一个存在者**（原来只回退一个写死的 conda 路径，换机器即失败）；nebular 可选模块段补 `line_list =`
   （2025.x 需要该键；空值在本机 2025.0 的 nebular 模块里被显式容忍——`line_list` 的解析带 `if name.strip()`，
   且本机 spec 模板已声明 `line_list = string()`）。
4. **滤光片总览前端**：横轴按可见波长跨度自适应（`max/min ≥ 100` 用对数轴，否则线性）；对数轴用显式
   尾数集合保证 ≥5 个刻度；≥1e6 Å 用书面科学计数法（`utils.js` 新增 `sciTickSup()`）；波长列千位分组
   （逗号包在 `user-select:none` 的 span 内，复制出来不带逗号，编辑/保存时统一 `replace(/,/g, '')`）；
   列宽按真实 DOM 度量（`font-variant-numeric: tabular-nums`，canvas `measureText` 量不准）；总览图支持
   **框选放大**（只改 x 轴，y 恒 0–1，拖动不足 5 px 视为点击）。

### 8.31 标签索引表 / T0 元数据 / MJD 权威时间与基准时刻（2026-09-22，v2.23）

1. **tags 表升级为带分类的索引表**：新增 `kind` 列（VARCHAR(8)，`main`=主标签 / `sub`=副标签，
   缺省 `main`）；唯一约束由 `name` 单列唯一改为 `UniqueConstraint('name','kind')`（唯一索引
   `uq_tags_name_kind`；`init_db` 幂等迁移：DROP CONSTRAINT IF EXISTS tags_name_key +
   CREATE UNIQUE INDEX IF NOT EXISTS）。API 扩展为完整 CRUD：`GET /api/tags` 支持
   `?kind=main|sub` 过滤（公开）；`POST /api/tags`（登录，`{name,kind,description,color?}`，
   **description 必填**，缺 400）、`PUT /api/tags/<id>`（改描述/颜色）、`DELETE /api/tags/<id>`；
   `POST /api/tags/register` 批量幂等登记（内部用）。新建/编辑源时写上的 tag 会自动登记进索引表。
   tags 随 `etl.py --dump` 落盘到 `catadata/tags.json`，全量重建时 force 回灌 +
   `register_used_tags` 扫 transients 表登记现役 tag。
2. **transients 新增三个 T0 元数据列**：`t0_ref`（TEXT）、`t0_offset`（FLOAT，秒，正=向后/负=提前）、
   `t0_offset_ref`（TEXT）。**纯元数据，不参与任何 MJD 换算与绘图基准**；POST/PUT /api/transients
   均可读写，info JSON 双向支持 `T0_ref`/`T0_offset`/`T0_offset_ref` 键。
3. **lightcurves 新增 `mjd` 列**（FLOAT 可空）：**观测时间的唯一权威依据**；`time` 降级为相对 T0 的
   秒数画图加速缓存，可随时由 mjd 重算覆盖。源无 T0 时 mjd 为 NULL、time 维持原值。存量 257391 行
   已用 `scripts/migrate_lc_mjd.py` 按 t0+time/86400 回填（0 行 NULL）。写入侧（batch、PUT、ingest、
   ETL 导入）自动互算：给 mjd 重算 time、给 time 重算 mjd（新增时两者至少给一个，否则 400）；
   PUT /api/transients 改 T0 时联动重算该源全部光变点 mjd（T0 清除则置 NULL）。lc CSV
   （`catadata/lc/*.csv`）新增 `mjd` 列（time_unit 之后），旧 CSV 无此列可正常导入（导入时补齐）；
   光变列表排序支持 `sort=mjd`。
4. **基准时刻（t_ref）贯穿三处**：单源光变图页图头新增「基准时刻」输入（MJD 数字或 UTC 时间，
   留空/`t0`=源 T0 默认），x 坐标按 (mjd−ref)×86400/zfac 重算，顶部 MJD 副轴与拟合叠加同步；
   多源对比图同样可加统一基准时刻（默认各源自己的 T0）；余辉拟合数据选取卡加基准时刻输入，
   提交时 selection 带 `t_ref_mjd`（`fitting/jobs.py prepare_data` 已支持：t=(mjd−t_ref)×86400，
   无 mjd 行用 t0+time 兜底；源无 T0 时报错）。注意模型要求 t>0，基准之后的点会被丢弃
   （与默认路径 t<=0 剔除一致）。
5. **导出基准时刻**：`GET /api/export/lightcurves/<tid>` 新增 `t_ref` 查询参数（缺省/`t0`=源 T0；
   纯数字=MJD；ISO UTC 字符串），time 列按基准重算，响应头 `X-AJST-Tref` 回显；CSV 含**全部列**
   （含 mjd）。前端详情页「导出光变」按钮改为弹窗选基准时刻。
6. **前端 tag 组件**（`frontend/js/taginput.js`）：`attachTagInput` 挂 datalist 补全（option 显示
   name—description）；`ensureTagsRegistered` 提交前对未登记 tag 逐个 prompt 要求必填文字说明并
   POST /api/tags 登记，取消则中止保存。新建事件页（create.js）新增副标签输入与三个 T0 元数据输入；
   详情页编辑面板同。首页「TAGS-标签分布」区显示各主/副 tag 的文字描述（取 /api/tags，失败静默降级）。
7. **事件列表页（list.js）**：T0 列显示完整 UTC `YYYY-MM-DD HH:MM:SS`（去 T 与小数秒）；标签列
   主 tag（badge-tag）与副 tag（badge-neutral 灰色）同列显示、颜色不同；筛选区新增「仅显示有光谱
   数据」勾选（`has_spectra=true`）、T0 日期范围（`t0_from`/`t0_to`，t0_to 含当天全天）、副标签下拉
   （`sub_tag`，选项来自 `GET /api/transients/sub_tags`）；分页改为居中横向滚轮式分页条（拖动/滚轮
   翻页，当前页 ±3 窗口，点击当前页码可直接输入页码跳转）。
8. **光变图增强（detail_lcchart.js）**：图容器宽度 80% 浏览器窗口居中（保持 aspect-ratio 3/2）；
   新增「显示光谱观测」勾选（数据由 detail.js 经 `setLCSpectra([{mjd,instrument,observation_date}])`
   注入，只用 parent_id 为空的原始谱）：在光谱观测时刻画紫色 #bc8cff 竖虚线（与「显示当前时刻」
   红线 #f85149 区分），不影响轴范围；越界谱在 chartArea 左/右上角画指向对应方向的小三角、多个纵向
   错开 14px；悬停竖线或三角显示 tooltip（观测日期 YYYY-MM-DD + 仪器）。
9. **光变数据表/上传（detail_lctable.js、lc_upload.js）**：数据表新增 MJD 列（time 之后，fmtNum
   5 位小数，null 显示 '-'）；行内编辑/新增支持 mjd 字段，新增校验改为「time 或 MJD 至少填一个」；
   CSV 上传 DB_FIELDS 加 mjd（同义词 mjdobs/obsmjd 等归 mjd，不再误归 time），time 必填改为
   time|mjd 二选一。

### 8.32 上限点开关 / 光谱波长类型 / 后台标签管理 / 列表筛选与分页修复 / 光变图操作区整理（2026-09-23，v2.24）

1. **显示上限点开关**：单源光变图波段面板与多源对比图调节区各加「显示上限点」勾选（默认勾选=
   探测点+上限点都显示；取消只显示探测点）。实现：数据集 `_isUpperLimit` 标记 + 可见性交集；
   对比页上限点原先被整体跳过，现拆为独立倒三角数据集。
2. **光谱波长类型（wavelength_type）**：spectra 表新增 `wavelength_type` VARCHAR(8) 可空列
   （'vacuum'/'air'/NULL；init_db 幂等迁移）。口径：**所有用到光谱的处理默认先把波长转真空**
   （Morton 1991 折射率公式，不动点迭代反解空气→真空，≥2000Å 才转；新文件
   `backend/wavconvert.py`）；NULL 按空气处理但不回填。转换发生在 `GET /api/spectra/<id>`
   （响应 meta 附 wavelength_type + wavelength_converted 标记）与消光改正 correct_spectrum 的
   P92 求值前；下载接口与库存文件保持原始值（download 的 # 头加 wavelength_type 行）。
   上传 body 可带 wavelength_type；`PUT /api/spectra/<id>` 接受 wavelength_type（管理员，
   DB+文件同步写，传播改正子谱）；ETL import_spectra 从文件 JSON 回读该键。前端：光谱列表加
   「波长类型」列（管理员三档下拉直改：真空/空气/留空），上传弹窗加同款下拉（默认留空）。
3. **管理员后台标签管理**：/admin 新增「标签管理」区块——主/副标签列表（行内编辑名称/说明/
   颜色，`PUT /api/tags/<id>` 本轮起支持改 name，(name,kind) 冲突 409）、新建标签（描述必填）、
   删除（confirm 提示不影响源身上同名标签）。
4. **删除导航栏统计小字**：删掉顶部「xxxx 事件 · xxxxx 数据点」（index.html 的 #navStats +
   app.js 的 loadNavStats 及调用）。
5. **事件列表分页轮盘修复**：窗口从当前页 ±3 扩到 ±10 预渲染；新增 centerPager() 当前页居中
   （含贴边收敛）；拖动时跟随鼠标的「第 X 页」浮动提示；像素↔页码换算取当前页相邻步进实测。
   另修复一个真实 bug：#pgTrack 无 transform 时页码的 offsetParent 是 BODY 导致 offsetLeft
   取到整页坐标、居中算错——已给 #pgTrack 加 position:relative 根治。
6. **事件列表筛选区三排重排**：第一排 搜索/标签/副标签/排序；第二排 红移/RA/DEC 上下限 6 窄框
   + T0 起止 2 框；第三排 3 个勾选框（仅有红移/仅有宿主/仅有光谱）+ 清除筛选。
7. **详情页「返回列表」**：由主页 `#/` 改为事件列表 `#/list`。
8. **光变图 now 线**：'now' 标签字号 11px→14px；越界时在对应图角画红色三角（与光谱紫色三角
   共用 y 槽位分配，不重叠）。
9. **手动坐标范围点溢出修复**：根因是 Chart.js v4 数据集 clip 缺省在散点有溢出量时不裁剪——
   全部数据集显式 `clip:true`，误差棒手绘插件在 lcchart 侧包了一层 beforeDatasetsDraw clip
   包装（未改共享 chart_plugins.js）。
10. **单源光变图操作区整理为两排**：第一排 数据/Y/X/顶部轴 4 select + 基准时刻输入（加宽到
    260px、placeholder 字号 0.72rem）；第二排 误差棒/显示当前时刻/显示光谱观测/静止系 4 勾选；
    「复制光变图」固定最右端；删除单源光变图的「重置缩放」按钮（与坐标范围「恢复默认」重叠且
    失效；多源对比页的重置缩放保留不动）。拟合参数输入框 placeholder 字号同步调小。
11. **编辑标签改 chip 泡泡输入**：taginput.js 新增可复用 `attachChipInput(container, kind, initial)`
    （逗号/回车固化 chip、×删除、空输入退格删尾、粘贴拆分、保留 datalist 补全）；详情页基本信息
    编辑面板主/副标签改用它，保存仍走 ensureTagsRegistered（新 tag 必填描述）。
12. **文章来源标记归一**：articles.source 的 'literature-mining'/'arxiv' 统一为 'bot'
    （存量 3746 条已 UPDATE；ETL 导入处同步归一，大小写不敏感）。
13. **多源对比页**：事件选择列表显示各源光变点数（meta 的 lc_count）；「基准时刻」从图头移除、
    改为「各源对比波段」block 每源各一个输入（留空=该源 T0），绘图按各源自己的基准换算 x，
    轴标题相应标注。

### 8.33 对比页点数 bug / 对比图裁剪 / 抠图波长类型 / 图例对齐 / 分页虚拟化（2026-09-23，v2.25）

1. **对比页事件列表光变点数全 0 修复**：根因是 `GET /api/transients/meta` 不返回 lc_count，
   前端 `t.lc_count ?? 0` 恒为 0。meta 现加一次全量 `GROUP BY transient_id` 的 lightcurves
   计数聚合，每项附 `lc_count`（接口契约新增字段，对比页「选择事件对比」列表据此显示真实点数）。
2. **对比光变图框选放大后点溢出修复**：与 v2.24 单源图同款根因（Chart.js v4 数据集 clip 缺省
   对有点半径的散点不裁剪）——compare.js 全部数据集显式 `clip: true`；误差棒手绘插件在
   compare 侧包了一层 `beforeDatasetsDraw` 的 chartArea clip 包装（id `cmpYErrBarClip`，
   共享 chart_plugins.js 不动）。
3. **抠图取数光谱波长类型**：digitizer 光谱入库表单加「波长类型」三档下拉
   （留空（默认按空气处理）/真空波长/空气波长，默认留空），仅非空时 payload 带
   `wavelength_type`（'vacuum'/'air'），与详情页上传弹窗同款口径（v2.24 后端已支持）。
4. **详情页波段面板对齐**：「显示上限点」勾选框及文字与波段图例条目统一为
   `d-inline-flex align-items-center` + 勾选框 `mt-0`（消除 form-check-input 默认 margin-top
   造成的垂直错位），四类元素中线实测完全重合。
5. **分页轮盘窗口跟随虚拟化**（替代纯 ±10 预渲染，任意拖动距离不再露出未渲染区）：
   拖动中按视觉位移实时换算虚拟中心页，其逼近渲染窗口边缘（<3 页）时以它为中心重渲染
   ±10 窗口，并按「新旧窗口起始页差 × 步进宽」补偿 transform 基准（视觉无缝；只在跨阈值时
   重渲染，天然节流）；视觉位移先夹在 [当前页→首页, 当前页→末页] 对应区间（拖到边界后继续
   拖不再位移），再做内容边缘收敛（内容宽于视口时 transform 夹到 [视口宽−内容宽, 0]，
   边界处内容贴齐视口边缘不露空白）；「第 X 页」跟随提示与松手落点换算不变（所见即所得）。

### 8.34 模板库 × K 改正（ChromaShift 引擎）（2026-09-29 新增，P0–P4 落地）

把九个超新星/千新星/余辉模板的谱能量分布曲面（ChromaShift 引擎建面）接入对比页与工具箱：
模板曲线预测（K 改正/绝对星等/域判定）、K 改正实测点（M = m_AB − μ_engine − K）、
造模板向导（库内行集 → manifest → 建面 → QC）、单项误差预算、CSV/JSON 导出。

- **引擎依赖**：ChromaShift 是**纯计算库**（不碰 DB/网络），以 editable 方式安装
  （`pip install -e <ChromaShift 路径>`）。它是**可选依赖**：缺失或依赖不达标时
  `/api/tmplib/*` 恒 200 回 `TL_ENGINE_UNAVAILABLE`，`#/tools/tmplib` 整页降级为只读说明，
  其余全部功能不受影响。要求 Python ≥ 3.12，依赖下限 numpy 2.4 / scipy 1.17 / astropy 7.2 /
  dust_extinction 1.7 / pandas 2.3 / PyYAML 6.0（`GET /api/tmplib/config` 的 `deps` 逐项自检）。
- **后端形态**：in-process 唯一后端（`backend/tmplib/` 编排层 + `backend/routes/tmplib.py`
  13 个端点，见 §五 API 表）；引擎调用全程不持 DB session（先短 session 取数关闭，再进引擎）；
  预测/建面/预算共用一把非阻塞 `BoundedSemaphore(1)`，占用时立即 429 不排队。
- **存储**：数据在 `catadata/tmplibrary/`（§2.8），随 AJST-Data 进 git 分发（仅软删回收站
  与 `library.json.bak` 留本地）；写盘只经 API-5/7/11/14。
- **元数据编辑（API-14，管理员）**：白名单字段（label/object_class/redshift/distance/
  validity/epoch_zero/citation_append）走 `tmplib/edit.py` 直接改写（校验 + 引擎自校验
  后才落盘，历史版本由 AJST-Data 的 git 历史承担），默认保存后立即重建；行集口径（rowset/policy/bands/CSV）是冻结
  数据，不可编辑，须删除后重新建面。列表/详情含 T0 口径列（epoch_zero 声明 + 对应源库内 t0）。
- **性能实测**（九模板、n_times=300、2026-09-29 本机）：误差预算 API-10 的 Monte-Carlo
  n_draws=200 每模板 13.7–18.1 s、n_draws=32 每模板 2.4–3.1 s —— 故 API-10 默认
  n_draws=32（上限 200，响应 provenance 显形）；单条预测约 0.05–0.5 s，单源建面 1.1–2.8 s。
- **守卫**：三轴 staleness（引擎输入六指纹 / 第 7 项库行指纹 / 滤光片 vendor sha）+
  `code_sha256` 钉引擎代码；读路径遇 stale 只拒绝、永不自动重建（建面唯一触发者是
  向导/重建按钮的一次点击）。引擎代码一旦改动，code 指纹变 ⇒ 全部面 stale ⇒ 需逐模板重建。
- **前端**：`#/tools/tmplib`（清单/详情 QC/预算面板/批量重建）与对比页模板层
  （`compare_template.js`：预测曲线叠加、Δμ 披露卡、域判定条、导出与复制图）。
- 详细契约与条款号（F-/T-/Q-/CA- 系列）见外部设计文档 `02_功能设计方案.md`（不入库）；
  验收测试 `tests/acceptance/test_tmplib_p0..p4.py`（全量 326 例）。

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
| astro-prospector / astro-sedpy / python-fsps（可选） | — | hostfit prospector 宿主 SED 拟合引擎（需 FSPS 数据目录，见 §8.20） |
| ChromaShift（可选，editable 安装） | — | 模板库 × K 改正引擎（纯计算库；缺失时 tmplib 功能降级只读，见 §8.34） |
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

### v2.35（2026-10-08）— 数据表「添加记录」time/MJD 交互缺陷修复（半截输入定格 MJD / 改因子不换算）

作者报障两条，同源：新增行里自动补算出的 MJD 会「定格」在 time 的首个半截输入值上，
而写入侧以 mjd 为权威列 ⇒ 错误 MJD 反压用户真实 time。

- **根因**：`detail_lctable.js` 的 `lcNewTimeSync` 挂在 time 内联 `oninput` 上、且带
  「另一侧已有值不覆盖」守卫——time 逐字输入时首个字符（如 `3`）即把 MJD 填成 T0+3s，
  此后每敲一个字符都因守卫直接返回、不再更新；`lcAddNewSave` 又把该陈旧 mjd 无条件写进
  请求体，服务端 `_sync_time_mjd` 见 `'mjd' in body` 即以 mjd 为准反算 time ⇒ 用户输入的
  `3600` 落库成 `mjd=60000.000035 / time≈3.024s`。time 的乘积因子下拉（秒/分/小时/天）
  亦无 `onchange`：先填 time 再改因子时 MJD 不重算，即用户所述「必须先勾因子才换算」。
- **修复**（只改前端 `frontend/js/pages/detail_lctable.js`，后端零改动）：
  1. 引入「来源」标记 `data-src`（用户手填=`user`、由另一侧补算=`auto`）。补算只在
     `change`（失焦 / 因子变动）时机做，**不再逐字输入即填 MJD**；已被用户手填的一侧永不覆盖。
  2. time 的乘积因子下拉挂 `onchange="lcNewTimeSync(this)"`，改因子即用 time×新因子重算 MJD。
  3. 保存侧：MJD 非「用户手填」（空 / 自动补算）时**不提交 mjd**，交由服务端按
     time×因子反算 —— 使「mjd 列权威」只在用户确实手填 MJD 时生效，用户手填的 time 始终优先。
  4. 行内编辑同缺陷一并修：改了 time 但未手改 MJD 时省略 mjd 提交（仅源有 T0 时省略），
     否则「mjd 权威」会把对 time 的修改原地压回。
- **验收（Playwright 端到端 + 反向对照）**：隔离自测实例（独立 schema，源 T0=60000.0）——
  逐字输入 `3600`：输入过程 MJD 保持空、失焦后补算 `60000.041667`、请求体不含 mjd、落库
  `time=3600 / mjd=60000.041667`；先填 time=1 再改因子 ×60：MJD 由 `60000.000012` 重算为
  `60000.000694`、落库 `time=60 / mjd=60000.000694`；只手填 MJD（time 反算 194400）、
  手填 time 与 MJD 两者（以 MJD 为准）、编辑行改 time（7200→mjd 60000.083333）、编辑行改
  MJD（60000.5→time 43200）均符合既有契约。**反向对照**：还原修复前 JS 重跑「逐字输入
  3600」⇒ 复现落库 `time≈3.024 / mjd=60000.000035`（错），确认测试确有区分度。
- **回归边界**：无 T0 源不受影响（无互算）；两侧均用户手填时仍以 MJD 为准（后端既有契约，未改）。

### v2.34（2026-10-05）— specphot 工具页可用性三轮：访客只读计算放开 / 库内谱可输入筛选 / 风格协调化

同一审查批（推进档：AJST-enrich/20261005_功能审查/00_推进状态.md）的第二轮，
作者四条指令：风格协调、访客放开、选择器可用性、打 tag 推送。

- **未登录访客只读计算放开（作者裁定）**：API-2/3/4/7/8 五个 POST 端点
  （photometry/continuum/line/parse/ebv）移除 `require_auth`。工具页全链只读
  不变（RO-1/RO-2/RO-5：无写库、不落盘、不缓存谱数组），滥用防护由既有全局
  信号量闸（ST-1/ST-12 ⇒ 429）与 ST-5 硬超时承担。前端同步解除未登录态的
  控件禁用（上传/粘贴/解析/E(B−V) 查询按钮）并删除三处「未登录…需要登录」
  缺项提示；L2 契约测试的 401 断言改写为「访客可用、空体打到参数校验 400」。
- **库内谱选择器改可输入筛选**：平铺 `<select>`（全库 113 条平铺，谱多即
  无法翻找）改 `input + datalist`（与宿主标签/波段输入同一 idioms）。候选标签
  `#id · 源名 · 仪器 · 日期`；直填 `#id`/纯数字 id 亦受理；未匹配 ⇒ 红字提示
  且不清当前装载；清单按页面挂载缓存（渲染轮次不再每轮重 `GET /api/spectra`）。
- **风格与项目整体协调化**：① specplot 主图与对照图 20 余处浅色硬编码颜色
  全部接入宿主 `theme.js chartColors()`——此前默认深色主题下网格/刻度/谱线
  观感破碎；② meta_provenance 徽章 `bg-light text-dark` ⇒ 既有 `badge-neutral`；
  ③ 来源/① 谱预览/② 波段选择/③ 参数/④ 锚点表及 S2/S3 参数面板标题行统一
  为 `.card-header`（与 tmplib/详情页同构）；④ 静态内联尺寸收编进
  `style.css` 末尾 specphot 专区，workbench 的 JS 注入样式（W-15 首列冻结+
  窄屏抽屉）一并迁入并删除。选择器名/事件绑定/文案零改动。
- **页签切换缺陷修复（Playwright 复验发现）**：`layoutPanes()` 只对 cmp/s2/s3
  分支重挂右栏，从 S2/S3 切回 S1 后 `#spRight` 残留对方参数面板且 S1「计算」
  按钮消失（P1-A 布局既有漏洞，非本轮引入）；补 s1 分支 `renderRightPanel()`。
- **验收**：全量 844 绿+4 登记 skip；preflight 无阻塞；Playwright 复验——
  未登录上下文直接计算 sid=2 出数（r/i/z=20.546/20.467/20.625，与登录态基线
  逐值一致）、筛选框输入收窄/直填 `#id`/未匹配提示三态通过、深浅双主题画布与
  徽章观感通过、S1↔S2↔S3 四轮来回切换面板严格一致、零 pageerror。

### v2.33（2026-10-05）— specphot anchored 定标修复 + 模板库 R-1 零点平移补偏

2026-10-05 功能审查（推进档：AJST-enrich/20261005_功能审查/00_推进状态.md）后的修复轮。
背景：specphot 全部验收测试用内联 golden 常量（T-* 禁用 db_cur 夹具），真实库数据
路径欠覆盖；在真实服务+真实库全量冒烟（113 条谱）后定位并修复以下问题：

- **specphot SP-A（P0）anchored 模式被原始 m_syn 合理域闸误杀**：`_row_result` 的
  `[C_MAG_SANITY_LO, C_MAG_SANITY_HI]` 闸原对**未定标原始** m_syn 判定（所有模式
  统一），anchored 下 κ* 本可把 mag 拉回合理域却在改正前被整行置 null——库内
  27/112 条谱（归一化/另有刻度）anchored 全波段 null，正是「工具不可用」的主因。
  现改为判定**模式生效后的星等**（direct 语义不变；anchored 判 κ* 改正后 mag），
  规格 02 F-14 末句按 direct 闸门语境解读。golden 基线 `cat116_anchored` 随之重建
  （四件中仅此一件变化：旧全 null ⇒ 新出数 ≈18 mag）。
- **specphot SP-B（P0）两处数值异常不再 500**：κ*≤0（病态锚定）⇒ CA-03
  （reason=`kappa_nonpositive`）+ mag 族 null（不再 log10 域错）；`delta_m` 杠杆
  收缩方差为负（h_i>1）⇒ CA-15（reason=`delta_m_var_negative`）+ null（不再
  sqrt 域错），均守 TXT-23「null+书面原因，不冒充」纪律。
- **specphot SP-C（P1）auto 无锚点出口改 E-10/409**：auto 在 direct/anchored/model
  均不可用时报 `anchor_unavailable`（reason=`auto_no_calibration_path`，文案给出
  「手加锚点或先做 S2 拟合」的下一步），不再落 model 报 E-13/501
  `feature_disabled`（该文案是 P2 期次闸门残留）。
- **specphot 前端 P1（Playwright 走查发现）**：S2/S3 页签的计算失败此前无任何
  UI 出口（`S.errorMsg` 只在 S1 参数面板渲染）——S2/S3 参数面板「⑤ 动作」下
  同挂 busyNotice/featureNotice/errorMsg 三行，失败原因逐字可见。
  遗留登记（非阻塞）：S2 的 ST-5 5 s 硬超时在冷缓存+高负载下偶发触顶（重试即过），
  口径是否放宽另议。
- **模板库 R-1（原审核 P1-3，作者裁定：平移补偏）**：API-8 kcorrected 通路把行
  时刻（相对库 t0）直接送引擎取 K，未补偿模板面零点 ≠ 库 t0 的偏移（仅
  sn2002ap/sn2006aj 两个 first_point 模板）。现复用 `predict.resolve_time_origin`
  算 `offset = 模板零点 − 库 t0`，`times_obs_days` 与 t_valid 窗口判定统一平移；
  偏移非零 ⇒ alerts 挂 CA-14（time-origin-not-event 族）+ notes 说明 + 响应新增
  `k_time_offset_days` 回显。实测 SN2002ap offset=+1.17 d（与底账一致）、
  GRB060218A +0.00164 d。
- **验收**：specphot 490 绿+4 登记 skip；tmplib 97 绿；全量 843 绿+4 skip（唯一
  失败 `test_q13_pl2_vocabulary` 为 ST-5 5 s 墙钟在外部 CPU 争用下的已登记环境
  flake，隔离跑即绿）；preflight 无阻塞；Playwright 端到端走查六项全过（S1
  anchored 出数/409 错误路径可读/S2/S3/导出/零 pageerror）。

### v2.32（2026-10-02）— 模板库（tmplib）审核修复轮（P1-3 已于 v2.33 裁定落地）

2026-10-02 全量只读代码审核（意见书：AJST-enrich/20260928_snredshift_to_fix/代码审核意见_20261002.md）
后的一次性修复；除 P1-3（S3 时刻零点口径，待作者裁定）外全部 P1/P2/P3 落地：

- **P1-1** API-8 批量通路的引擎异常改按 code 分派（`ENGINE_CODE_MAP` 收进
  `tmplib/engine.py` 共用）：越界曲线在 API-8 里返回 `TL_OUT_OF_DOMAIN`+context，
  不再被吞成 `TL_INTERNAL`（E-30/E-13/IA-3）；三条曲线通路均补日志。
- **P1-2** 可答比例口径改为引擎 `build.load_samples`（进拟合样本），与底账 01 §E.6
  **逐模板精确对账**（九模板 5813/5959 = 97.5%）；`library.json` 的 in_domain 已重烙印，
  并新增 T-35 守卫测试防再漂移。
- **P1-4** 对比页 IA-15/U-09：任一曲线零点被改过或为表首行时，X 轴标题追加
  「（各曲线零点见图例）」+ 新增 `#tplAxisNote` 说明行。
- **P1-5** `library.json` 顶层 `engine.code_sha256` 与现行引擎/每模板指纹对齐。
- **P1-6/P1-7** 引擎仓 VALIDATION.md 失引修正；golden 复验脚本路径更新到现行位置。
- **P2-1** API-5 索引登记前任何失败自愈：半成品（CSV/manifest/面/QC）全部回滚，
  同 id 可立即重试（不再 409/404 死锁）。
- **P2-2** ST-12 库配额落地：`AJST_TMPLIB_QUOTA_MB`（默认 200，0=关闭），超限
  `TL_QUOTA` 400，API-1 回显现状。
- **P2-3** ST-6 启动预热落地：`app.create_app` 后台线程 warm（读盘零写），失败经
  API-1 `warm` 键显形；测试进程跳过（T-43 静态断言不受影响）。
- **P2-6/P2-7/P2-8** 前端：vendor 徽标 tooltip 补 TXT-10 全文；域判定条收被拒行
  （IA-10 唯一出口）；TXT-3（消光未定）按 `meta.absolute_mag_is_intrinsic` 挂点。
- **P3 项** `_finite` 拒 bool；rows_sha256 排序键改 (time,band)（F-52 字面，登记值
  重烙印）；CA-01 随曲线显形；API-2 epoch_zero 摘要 mtime 缓存；API-14 编辑期
  per-template 单飞锁；gext 账本补 `kept_missing_err`/`gext_system_overrides` 披露
  （TXT-12 第三段 + Vega 计数）；行级 stale 徽标三轴分色；建面完成页「→ 对比图」
  （F-04 跨页只传 id）；TXT-18 收窄到 absmag 模式；mono(降级) 徽标、U-05 生效 z
  回显、U-07 出处三标签、IA-7 恢复取数、preview 请求令牌与路由守卫。
- **测试** 新增 `tests/acceptance/test_tmplib_audit_fixes.py` 13 条（T-01/T-02/T-21/
  T-35 守卫/P1-1 API-8 错误路径/P2-1 自愈/P2-2 配额/P1-5 指纹/ST-6 warm/P3-2/
  P3-3/P3-5/P2-8 依据）；P0–P5 存量 84 条全绿。
- **审核更正两项**：P2-9「批量重建缺失」不成立（库管理页勾选 stale 串行重建已存在）；
  P3-6「裁剪容差不对称」不成立（引擎公式即 `1e-9·max(1,|hi−lo|)`）。
- **设计文档回写**：02 新增 §12 变更注记 N-01…N-16（库根迁址、引擎改名、in_domain
  口径、rows_sha256 配方等），03 补 D-3 改道裁定。

### v2.31（2026-10-02）— 光谱 × 滤光片工具（specphot，P1–P3 分期交付）

- **新工具条目「光谱 × 滤光片」**：`backend/specphot/` 十六个模块（逻辑下沉、路由薄，见本文
  「光谱 × 滤光片」小节）+ `frontend/js/specphot/` 九个模块；页面 `#/tools/specphot`
  （空工作台）与 `#/tools/specphot/<spectrum_id>`（直达库内谱）共用一个路由分支
  （`app.js` 的 `specphotRe`），详情页光谱行加直达入口（仅装载，不自动计算），
  工具箱菜单加条目
- **八个端点**（`/api/specphot`）：`GET meta` / `curve/<filter_id>` / `health`，
  `POST parse` / `ebv` / `photometry` / `continuum` / `line`。所有响应（含错误）带
  `spec_phot_version`（口径版本戳，当前 `1.0.0`，与站点版本 vX.YZ 无关）与 `warnings[]`；
  未到期子功能一律 501 `feature_disabled` 占位（响应含 `phase`）；宿主无 501/504 兜底
  handler，一律显式 jsonify
- **S1 合成测光（P1 已上线）**：库内谱或上传件 × 波段曲线 → 合成星等，photon/energy 加权、
  direct/anchored/model 定标、锚点集、银河消光、AR(1) 误差放大
- **S2 连续谱（P2 切片 2b + P2b）**：六模型 pl/pl_dust/pl2/bb/pl_bb/dbb + poly 基线、
  `host_ext` 三态（off/fit/prescribe）、模型比较（F 检验 / ΔBIC；pl→pl2 走 Davies 参数化
  自助）、闭包三候选、`de_reddened` 纯派生曲线
- **S3 谱线（P3 切片 2）**：三步向导的计算核（线区 + `line_kind` 必选 → 基线 → 轮廓拟合
  gauss1/gauss2/lorentz/voigt，voigt 仅当 R 可得）、单线 `LineResult`（EW 三项分解 /
  线流量与 depth 互斥空值 / snr_res 门 / 自助对照）、`frame_gates` 两道独立的门；
  `vel_*`/`z_fit` 恒 null（M-6 未复核，强提交 `velocity_output=true` ⇒ E-14）
- **P1b 预处理读侧**（`preprocess.py` 是 F-80 序列化与 PreprocessedSpectrum 唯一载体）：
  响应谱级 `preprocess{}` 18 键恒在、`mask_hash`/`preprocess_hash` 双哈希进摘要行与缓存键、
  四类掩膜并集（不截断、不插值）、误差列退化判定六值闭集 {ok, all_zero, constant, nonfinite,
  negative, flat_relative}、离群点只找候选绝不自动剔除；因子/平滑的**算术**属 P2（E-13）
- **P2+ FITS/ECSV 上传分支**（F-76 / T-81，零新增依赖）：`reader.py` 增 `load_upload_fits`
  （BINTABLE/TABLE 按列名/TUNIT 识别；一维 ImageHDU 走线性 WCS，LOG/缺 WCS ⇒ E-14）与
  `load_upload_ecsv`；λ 单位换算一律 `astropy.units`，波长列无单位 ⇒ E-14（不按 Å 猜）
- **全链只读**：不写库、上传件不落盘、不改 `filters` 表——曲线口径 `curve_kind` 登记在代码侧
  `specphot/registry.py`（`etl.py --filters` 会整块覆盖 `extra_data`）
- 验收证据：`AJST_PYTHON=<conda env>/bin/python scripts/acceptance/run_all.sh` →
  **830 passed, 4 skipped in 230.26 s**（首轮曾因整轮负载让
  `test_l2_specphot_p2b_pl2.py::test_q13_pl2_vocabulary` 撞上 ST-5 5 s 硬超时拿到 504，
  单跑该文件 6 passed，重跑全绿）；`GET /api/specphot/health` → 200
  （`spec_phot_version 1.0.0`，deps astropy/dust_extinction/dustmaps 全 true）；验收资产 =
  `tests/acceptance/` 下 `conftest.py`（T-24 周期级只读快照）+ `regen_specphot_golden.py` +
  `golden_specphot_p1/*.json`（T-75① 基线）+ 24 个 `test_l1/l2_specphot_*`（L1 12 / L2 12）
- 标记与 tag：tag 打**批次末提交**（本轮四条 = `bf0b332` 后端 / `f21416c` 前端 /
  `4b03226` 验收 / 本条目所在提交）。理由见 `docs/COMMIT-CONVENTION.md` §5：标记提交在
  批次中间时，tag 会漏掉同批的后续提交

### v2.30（2026-09-29）— 主页事件日历 / T0 权限锁定 / AJST-Data 规范化与开放契约

- **主页事件日历**：GitHub 贡献日历风格热力格点图（新模块 `frontend/js/pages/home_calendar.js`），
  按事件 T0 逐日计数、绿色系 5 档色阶（`--cal-0..4` 随明暗主题切换）、以有记录的年为单位翻页、
  格点 tooltip 显示日期与事件数；数据复用 `GET /api/transients/meta`，接口失败或无 T0 记录时整卡隐藏
- **首页文案更新**；静态事实行新增第 4 格「宿主星系拟合 Host Fitting」（pcigale · prospector，
  版本取自 `GET /api/hostfit/config` 新增的 `versions` 字段，取不到只显示包名）
- **T0 写权限契约锁定**：库中已存在源的 T0 等事件字段修改仅管理员（PUT 已 `@require_admin`、
  POST 重复 id 409 无覆盖旁路）；新建源时普通用户可设定 T0。新增契约测试
  `tests/acceptance/test_l2_t0_permissions.py`（伪造 session，不写库，6 用例）
- **AJST-Data 规范化**：dump 时空值显式写 `null`（规范键恒在，见 §3.4）；数据仓库新增
  `SCHEMA.md`（数据契约唯一权威）/ `CONTRIBUTING.md`（校验·扩充·提交流程）/
  `tools/validate.py`（纯 stdlib 校验器）/ GitHub Actions CI；README 计数订正
- **tmplib 持久化修正**：`_post_build_entry`（API-7/14 共用）对 catalog-derived 模板
  catalog_rows 轴一致时不再持久化 `[]`（运行期探针注记），与建面路径统一登记 `None`
  （P0 §4.1 契约；显示与状态合成行为不变，`[]`/`None` 同为"无漂移"）
- 验收证据：T0 契约测试 6/6 过；validate.py 全量 2794 info + 2514 lc 零错误零警告；
  浏览器实测日历明暗主题/翻页/月份标签、facts 第 4 格版本显示

### v2.29（2026-09-29）— 模板库直改写 / 库根迁 catadata / 对比页模板层与模板库管理页

- **后端（`90d9475`）**：模板元数据编辑改直改写——校验 → 引擎自校验 → `tmp+rename` 原子
  整写，不再按行号外科改写、不再逐次 `templates/.bak`（库随 AJST-Data 进 git 后历史交由 git）；
  库根默认 `backend/tmplibrary` → `catadata/tmplibrary`（`paths.default_root` 与 guard
  vendor 轴同步），随 AJST-Data 分发；移除 `library.json` 的 history 族谱记录（7 个调用点），
  API-2 不再返回 `history`；predict 补 `mu`/`d_l_mpc` 直达输入与低 z 放行等口径修订
- **前端（`104f2d9`）**：多源对比页加模板层（虚线叠加 ≤8 条、逐曲线基准时刻复用既有控件、
  Δμ 口径卡、域判定条）、Y 轴新增 kcorr 模式（K 改正实测点空心 + 模板 M 曲线同图，
  dragzoom 支持负 M 框选）、`#/tools/tmplib` 新页（守卫状态条三轴分色 / 库清单 /
  四步造模板向导 / QC 与误差预算面板）；既有 flux/absmag 两模式行为逐位不变（T-29）
- **验收（`0623785`）**：tmplib 五期用例 P0–P4 共 75 条，覆盖 design 文档 T-01…T-46 的
  可执行子集——golden 锚点、三轴守卫、时间原点六类、Δμ 对账、行账/建面/软删硬删、
  部分成功协议、CSV/JSON 同批互比、无自动建面三件套、只读快照
- **文档（`5878489`）**：TECHNICAL 补 `/api/tmplib` 十三端点与 ChromaShift 依赖说明；
  AGENTS 加 tmplibrary 规则
- 验收证据：三个代码提交的正文均记录 `run_all.sh` → 335 passed（`90d9475` / `86a95cd` /
  `ab3e595`）；`ab3e595` 另记录 P5 单跑 `53 passed`
- 标记口径：tag v2.29 打在标记提交 `90d9475` 上，**不含**其后的收尾四条——`86a95cd`
  （模板库/对比页交互修订、误差预算白话注释）、`ab3e595`（P5 编辑期验收）、`aef7a2b`
  （export_filters 输出目录随迁）、`1fd2985`（文档同步迁库）——它们从 v2.30 的 tag 起进入

### v2.28（2026-09-29）— 三层验收套件与预检入库 / 模板库 ×K 改正后端（ChromaShift）

- **三层验收套件入库（`ab58757`）**：L1 纯口径 / L2 HTTP 契约 / L3 数据不变量共 251 条，
  落在 `tests/acceptance/`；新增入口 `scripts/acceptance/run_all.sh`（`1d6173b`，解释器解析
  `AJST_PYTHON` → systemd 用户单元 drop-in → `python3` 回退，绕开 conda 插件故障）
- **preflight 预检（`2ab6ef1` / `64fbc1d` / `e717de3`）**：`scripts/preflight.sh` 十组护栏，
  逐组对应已踩过的坑；改「本机名单驱动」（公开脚本不再出现源名）；新增库↔文件一致性只读检查
- **仓库治理（`7fb7d34` / `347df71`）**：AGENTS.md 拆两份（入库通用规则 / 本机信息
  `AGENTS.local.md`）；新增 `docs/COMMIT-CONVENTION.md` 提交约定与 `CLAUDE.md` 薄指针
- **etl / sedfit**：`--dump` 增一致性报告与 `--prune`（默认不清理、有安全闸、可逆，`670f42b`，
  口径见 `588e8fe`）；sedfit 闭包关系系数表复核原文后升 v1.2（系数未动，只补说明与 ref，`c16815f`）
- **模板库 ×K 改正后端（`e033872`，ChromaShift 引擎）**：`backend/tmplib/` 包 +
  `/api/tmplib` 十三个端点（config/guards/templates/predict/preview/compare/export/budget/
  rebuild/delete/in_domain 等）；三轴 stale 守卫（引擎输入六指纹 / 库行指纹 `rows_sha256` /
  vendor 滤光片 sha）、Δμ 成因归因、域内可答比例、强制声明向导建面、响应零绝对路径（T-30）；
  ChromaShift 为可选依赖（editable 安装），旧引擎 `SN_redshift_v2` 保留为对照组
- 验收证据：提交正文记录 `run_all.sh` → 326 passed；与旧引擎数值等价 375/375 golden 逐点一致
  （锚点 sn2006aj B z=0.05 ⇒ [18.9688, 18.9459]）；生产实例重启后 API-1/2/6 实测通过
- 标记口径：tag v2.28 打在标记提交 `e033872`（后端）上，**不含**同批三条后续——`104f2d9`
  （对比页模板层 + 模板库管理页）、`0623785`（tmplib 五期验收）、`5878489`（文档）——
  它们从 v2.29 的 tag 起进入

### v2.27（2026-09-24）— MJD/time 口径收尾：无 T0 可只录 MJD / 新增行实时互算

- 光变写入放开「源无 T0 只给 mjd」的 400 限制：允许入库、time 留 NULL（新建校验改为
  time/mjd 至少其一）；此类点默认 time 轴不绘制（单源图与对比图对 time 为空的点跳过，
  不再落到 x=0），仅在自定义基准时刻（有 mjd）下参与绘制；PUT 编辑无 T0 源只给 mjd
  时 time 保持原值
- 数据表新增行 time ↔ MJD 实时互算：源有 T0 时填一侧自动填另一侧（MJD→time 秒、
  time 按当前乘积因子→MJD），另一侧已有值不覆盖；保存后整页重渲染显示服务端互算值
- 验收证据：`_sync_time_mjd` 六用例进程内测试全过（无 T0 只给 mjd 入库、有 T0 互算、
  双缺省报错、双给以 mjd 为准、PUT 不动 time）；playwright 冒烟：mjd=60700.25 →
  time 自动填 1445372s、time=3600 → mjd 自动填 T0+1/24、小时因子换算一致、
  已有值不被覆盖、取消不落库

### v2.26（2026-09-23）— 外部 PR 批（#6–#9）+ 光谱竖线弹窗 / 分页静态化 / 搜索扩列

- 合并外部 PR 四个（GitHub Cooper-J2000/AJST #6–#9）：子目录反代 API 前缀推导
  （`api.js` 按页面路径推导 `API_BASE`）、列表/详情视图状态同步进 URL（hash 查询串，
  `history.replaceState`）、光变数据表整页重建时保留其它行的未保存编辑（快照→回填）、
  各处标签徽章可点击跳转列表并按标签/副标签筛选
- PR #6 收尾修复：`detail_spectra.js` 光谱下载链接由硬编码 `/api/...` 改用导出的
  `API_BASE`（子目录部署下原链接 404）；admin.html 返回主页/品牌链接改相对路径
- 光变图「显示光谱观测」紫色竖虚线与越界三角改为可点击：悬停指针反馈 + 提示加
  「点击查看光谱」；点击（与框选缩放共存，位移 >5px 判定为拖动不触发）弹出横纵比 4:3
  小窗（`aspect-ratio:4/3`，Esc/遮罩/关闭钮退出），窗内 Chart.js 绘制该条光谱
  （波长-流量折线 + 误差棒，标题含源名/文件名/MJD/日期/仪器）；detail.js 注入光变图的
  光谱点新增 `id` 字段供取数
- 事件列表分页轮盘删去按住拖动翻页（含窗口跟随虚拟化、浮动提示、抑制误触逻辑），
  保留滚轮翻页 / 点击页码 / 点当前页输入跳转 / 首尾快跳；页码窗口由 ±10 静态化为 ±5，
  centerPager 居中逻辑保留（`#pgTrack` 的 `position:relative` 仍不能去掉）
- 事件列表搜索（ID/别名/引用）扩展匹配「触发仪器」列（后端 `Transient.trigger_instrument
  .ilike`，筛选框标签同步更新）
- 验收证据：`node --check` 全过；接口实测 `search=Fermi` 549 条且行内含 Fermi 仪器；
  浏览器冒烟：首页码 1..6、goPage(20) 页码 15..25、点击竖线弹窗宽高比 1.333、窗内
  canvas 有绘制、Esc 关闭；PR 合并后 `node --check` 与页面渲染无回归

### v2.25（2026-09-23）— 对比页点数 bug / 对比图裁剪 / 抠图波长类型 / 图例对齐 / 分页虚拟化

- 修复多源对比页「选择事件对比」列表光变点数全 0：`GET /api/transients/meta` 新增
  `lc_count` 字段（全量 GROUP BY 聚合，见 §8.33）
- 对比光变图框选放大后数据点越界修复：数据集显式 `clip: true` + 误差棒插件 compare 侧
  clip 包装（`cmpYErrBarClip`）
- 抠图取数（digitizer）光谱入库支持设置波长类型（真空/空气/留空，默认留空）
- 详情页光变图例区「显示上限点」勾选框与波段图例垂直对齐（inline-flex 居中 + 去默认 margin）
- 事件列表分页轮盘改「窗口跟随」虚拟化：拖动中按需以虚拟中心页重渲染 ±10 窗口并补偿
  transform 基准，视觉位移区间钳制 + 内容边缘收敛，任意距离拖动全程无空白，落点与浮动
  提示保持一致
- 验收证据：meta 接口实测 2794 源中 2514 个 lc_count 非零、对比页列表显示真实点数；对比图
  x 轴缩放至 40%–60% 后 13 个数据集 0 个点越界；图例区四类元素中线差 0px；goPage(28) 后
  拖 −3000px 窗口变迁 (18,38)→(27,47)→(36,56) 全程 0 空白、提示「第 56 页」落点 56/56，
  反向拖回提示「第 1 页」落点 1/56

### v2.24（2026-09-23）— 上限点开关 / 光谱波长类型 / 后台标签管理 / 列表筛选与分页修复 / 光变图操作区整理

- 单源光变图与多源对比图新增「显示上限点」勾选（默认开；对比页上限点拆为独立倒三角数据集，见 §8.32）
- spectra 表新增 `wavelength_type` 列（vacuum/air/NULL）：处理路径（`GET /api/spectra/<id>`、
  银消改正 P92 求值前）默认先把波长转真空（Morton 1991，`backend/wavconvert.py`），文件与下载
  保持原始值；上传/PUT/ETL 全链路支持，前端列表与上传弹窗加三档下拉
- /admin 新增「标签管理」区块（主/副标签行内编辑/新建/删除；`PUT /api/tags/<id>` 起支持改名，冲突 409）
- 事件列表：筛选区三排重排、分页轮盘修复（±10 预渲染、centerPager 居中、拖动页码提示；根治
  #pgTrack 缺 position:relative 导致 offsetParent=BODY 的居中算错 bug）；删除导航栏统计小字；
  详情页「返回列表」改 `#/list`
- 光变图：now 线标签 14px + 越界红色三角；手动坐标范围点溢出修复（数据集显式 clip:true）；
  操作区整理为两排、删除失效的「重置缩放」按钮；多源对比页基准时刻改为每源各一个输入、
  事件选择列表显示光变点数；编辑标签改 chip 泡泡输入（attachChipInput）
- articles.source 归一：'literature-mining'/'arxiv' → 'bot'（存量 3746 条 UPDATE + ETL 导入归一）
- 验收证据：浏览器冒烟 20/20；分页居中修复根因 offsetParent（#pgTrack 加 position:relative）；
  波长转换 Hα 6562.8→6564.61Å 验证；wl_type 上下线 PUT 实测；pager page40 居中 0 偏移

### v2.23（2026-09-22）— 标签索引表 / T0 元数据 / MJD 权威时间与基准时刻

- tags 表新增 `kind` 列（main/sub），唯一约束改 `(name,kind)`；`/api/tags` 扩展为完整 CRUD
  （GET 支持 `?kind=`，POST 的 description 必填）+ `POST /api/tags/register` 批量登记；
  tags 随 `--dump` 落盘 `catadata/tags.json`，全量重建 force 回灌 + 登记现役 tag（详见 §8.31）
- transients 新增 `t0_ref`/`t0_offset`/`t0_offset_ref` 三个 T0 元数据列（纯元数据不参与换算）；
  lightcurves 新增 `mjd` 列作为观测时间唯一权威依据，`time` 降级为画图加速缓存，
  写入侧自动互算，改 T0 联动重算全源光变点
- 基准时刻（t_ref）贯穿单源光变图、多源对比图、余辉拟合数据选取（`t_ref_mjd`）与导出接口
  （`t_ref` 参数 + `X-AJST-Tref` 回显，导出 CSV 含全部列）
- 前端：新组件 `js/taginput.js`（datalist 补全 + 未登记 tag 提交前强制补描述）；事件列表页
  T0 完整 UTC 显示、主/副 tag 同列异色徽标、新筛选（has_spectra/t0 范围/sub_tag）、滚轮式分页条；
  光变图 80% 宽居中、光谱观测紫色竖虚线 + 越界三角 + 悬停 tooltip；数据表/CSV 上传支持 mjd 列
- 验收证据：存量 257391 行 mjd 回填 0 行 NULL；`etl.py --sync` 无更新 + GRB221009A 单源往返
  mjd 保留；`prepare_data` t_ref_mjd 612 点平移 +86400s 精确；浏览器冒烟 22+ 项全过
  （tag 描述/新筛选/滚轮分页/T0 完整显示/图 80% 宽/光谱竖线与越界三角/悬停 tooltip/导出弹窗/
  对比与拟合基准输入）

### v2.22（2026-09-21）— 距离模数缓存 + 宿主统计 ETag / 迁移与口径修复 / pcigale 回退 / 滤光片总览前端

- 新增 `transients.gext_distmod` / `host_galaxies.gext_distmod` 派生缓存列（见 §四、§8.30）；
  `models` 距离模数改「z 分桶字典 + 向量化预热」，`/api/transients` 列表与 meta 批量预热后再取值。
- `GET /api/stats/hosts` 加 `ETag` + `Cache-Control: no-cache`，数据未变时 304（零计算）；token 覆盖
  宿主/暂现源 `updated_at`、行数，以及滤波器条数与 `gext_coeff` / `vega2ab` / `wavelength`。
- 修复 `init_db()` 漏迁 `spectra.spec_type`（旧库查询 spectra 返回 500）；ingest 写入单位统一为 `magnitude`。
- hostfit 的 pcigale 二进制改为有序存在性回退；nebular 段补 `line_list`。
- 滤光片总览：自适应对数/线性横轴、显式刻度规则、千分位波长列（复制不带逗号）、框选放大（只改 x 轴）。
- 验收证据：763 个真实红移下 μ 新旧实现 0 差异；持久化 μ 前后 `/api/stats/hosts` 响应逐字节一致；
  ETag 审计三种写入（宿主测光 / 宿主红移 / 滤波器 vega2ab）均触发新 token 且 304 语义正确（无 body）；
  `spec_type` 迁移在缺列的库上复现 500 → 迁移后 200；回填脚本扩展覆盖 μ 且幂等（第二次全 0）；
  前端在真实浏览器验证（对数轴与标题、y 固定 0–1、千分位波长列、框选放大后 x 范围与轴型切换）。

### v2.21（2026-09-21）— 消光派生量缓存 / 宿主统计提速

- 新增三个**派生缓存列**：`filters.gext_coeff`（k_λ = Rv·P92(λ)）、`transients.gext_ebv`、
  `host_galaxies.gext_ebv`（见 §四、§8.29）；读路径对 NULL 一律回退现算，不跑回填也正确。
- `extinction.filter_meta()` 进程内滤波器元数据缓存（60 s TTL + 写入口失效）；
  `/api/stats/hosts` 的宿主循环改为一次性预取 `(id, ra, dec, gext_ebv)`。
- 新增幂等回填脚本 `scripts/backfill_gext_cache.py`；`--dump` 不导出这三列，全量重建后需回填（仅影响速度）。
- 收口修复：E(B-V) 缓存与测光改正坐标必须**同源**（宿主有自身坐标时不得回退到暂现源缓存，`hostfit` 同）；
  ETL 导入宿主、`POST /api/transients` 新建源时补填 `gext_ebv`；回填脚本先检查 dustmaps 依赖再写库。
- 效果（本机实测，30 个宿主）：`GET /api/stats/hosts` 约 0.15 s → 0.005 s，响应逐字节不变；
  全量 `extinction.run()`（257391 行）改正结果与旧实现逐行等价（仅 2 ULP 级浮点末位差异）。

### v2.20（2026-09-18）— 拟合引擎升级（多起点+emcee）/ 银消护栏 / 列表默认 T0 倒序 / 多项 UI 优化

- 经验函数拟合引擎升级（详见 §8.19）：核心抽为纯函数 `fit_lightcurve_model()`；最少点数
  放宽为参数数（N==参数数走退化拟合，返回 `degenerate: true`，无误差字段）；fred/bpl/sbpl
  多起点最小二乘取 cost 最小，非退化加 emcee 第二阶段（16 walkers，种子固定）；响应新增
  `samples`（~200 后验样本）与 `sampler`，param_errors 改后验 16/84 分位半宽、param_cov
  改样本协方差；前端置信带优先走 samples 逐样本分位带（修复 bpl 带不连续、sbpl 奇异值、
  fred 多模），缺失回退 Jacobian 路径
- 银河系消光护栏（仅光学，见 §四）：光学窗口 1000 Å–1 mm + 统一判定 `_optical_alambda()`，
  波段超窗/P92 报错按跳过处理不再 500；run 统计新增 `skipped_not_optical`，run/clear
  响应附中文 `note`；光谱改正 P92 越界 → 400；宿主测光超窗行 `reason='not_optical'`
  按未改正处理
- 事件列表默认按 T0 倒序（前端初始状态/下拉/表头箭头 + 后端缺省 `sort='t0'`/`order='desc'`，
  T0 为 NULL 排最后）
- 光谱银消改正 `POST /api/spectra/<id>/gext_correct` 放开登录用户（此前仅管理员；
  删除/spec_type 仍管理员）
- 数据表「添加记录」后自动滚动到底部新行（requestAnimationFrame 包裹 scrollTop）；
  光变点 tooltip 增加波段与望远镜信息；GCN 阅读工具测光录入的时间单位下拉框遮挡修复
  （select 宽度 64px→88px + padding-right 调整）
- 验证：退化/多模/样本带连续性 E2E 通过，extinction/run 响应含 note 与
  skipped_not_optical，匿名 gext_correct 401 / 普通用户 200，列表默认 T0 倒序；
  浏览器冒烟（GCN 下拉、tooltip、添加记录滚动、SBPL UI 拟合置信带）全过

### v2.19（2026-09-18）— 光谱数据下载 + 光谱银河系消光改正

- 新增 `GET /api/spectra/<id>/download`（公开）：`#` 注释头元数据 + 两/三列
  空白分隔文本，attachment 下载；前端光谱列表每行加下载按钮
- 光谱银河系消光改正（可选，二级产物层级，详见 §8.28）：口径与测光银消完全一致
  （CSFD + P92 + Rv=3.1，复用 extinction.py），改正为乘性因子
  `f_corr = f_obs × 10^(+0.4·A_λ)`、不做 (1+z) 换算；spectra 表新增 `parent_id`
  自引用外键（级联删除），改正谱文件 `<原名>_gextcor.json` 与原始谱同目录、
  可幂等重新生成；新端点 `POST /api/spectra/<id>/gext_correct`（登录用户，幂等）；
  前端列表父子分组 + 「银消改正」按钮/徽标；合成谱逐点偏差 <1e-9、真实谱
  （GRB030329A，E(B-V)=0.0247）E2E 通过

### v2.18（2026-09-18）— hostfit prospector 引擎 + 光变页/数据表增强 + SED 模型精简

- hostfit 新增 prospector 拟合引擎（与 pcigale 并列可选）：`backend/hostfit/runner_prospector.py`
  （惰性 import prospect/sedpy/fsps，缺包不影响服务启动；FSPS 数据目录取 `SPS_HOME` /
  `AJST_SPS_HOME`）；parametric_sfh（delayed-tau）+ 可选 use_nebular/use_duste/use_igm 组件，
  dynesty 或 emcee 采样，红移 fixed/photoz；输出派生量 m_star/mass_formed/sfr/Av/tage
  （≤200 后验子样本取中位数与 1σ），产物含 corner 角图；fitting_results.model_name
  按引擎为 `pcigale_host`/`prospector_host`；端到端验证：GRB170817A 宿主（NGC 4993）
  文献测光跑通，m_star≈1.2×10¹¹ M☉ 与文献一致；详见 §8.20
- hostfit API：`GET /api/hostfit/config` 改按引擎分节返回（前端兼容旧扁平结构），
  `POST /api/hostfit/jobs` config 新增 `engine`，任务简报/详情带 `engine` 字段，
  files 新增 `corner` 类；前端拟合配置卡按引擎切换参数区、任务列表与结果区显示引擎徽标
- 光变曲线页：「复制光变图」按钮（PNG 复制到剪贴板，非安全上下文/剪贴板不可用时回退下载）、
  「显示当前时刻」红色竖虚线开关（默认关，无 T0 禁用，随静止系开关 (1+z) 换算）、
  勾选误差棒时探测点加绘水平时间误差棒、时间轴刻度改科学计数法
- 经验函数拟合：新增 FRED 脉冲模型（Norris+2005，最少 5 点）；`fit_model` 响应新增
  `param_cov`（输出参数基协方差，失败为 null），前端显示每参数 ±1σ 并叠加 1σ 置信带
  （数值 Jacobian 传播）
- 数据表「添加记录」表单：波段可输入+下拉建议、单位下拉、时间/时间误差乘积因子下拉
  （前端换算为秒入库）；添加记录后停留在数据表页（批量删除等整页刷新同样生效）
- SED 分析：移除「光学+X 双幂律」模型（powerlaw_xray）及前端关联配置（ν_split 配置项、
  暗暴判据高亮等）；现有模型 powerlaw_dust / powerlaw_2seg / powerlaw_3seg / blackbody /
  2blackbody / bb_powerlaw / blackbody_series

### v2.17（2026-09-13）— 暂现源 SED 分析

- 详情页新增「SED 分析」标签页：SED 构建器（时段取值 / GP 内插两种同时化模式）、
  模型拟合（幂律+宿主消光、两段/三段平滑幂律、单/双黑体、黑体+幂律，
  MCMC 复用 custom_mcmc）、多历元黑体序列（T/R/L(t) 三联图）、闭包关系 α–β 诊断、
  伪玻尔兹曼光变（L_obs/L_bb/L_bc 交叉检验），详见 §8.27
- 后端新增 `backend/sedfit/` 包与 `/api/sed/*` 路由；任务复用 `fitting_results` 表
  （model_name='sed_*'），产物存 `fitting_store/<tid>/sed_<jobid>/`

### v2.16（2026-09-04）— 宿主星系统计子页增强

- `#/stats/hosts` 新增「恒星质量 — 红移」「恒星形成率 — 红移」散点图（纵轴对数；
  `/api/stats/hosts` 新增 `m_star_points`/`sfr_points`），页面重排为
  绝对星等—红移图通栏 → M*/SFR 分布并排 → M*—z/SFR—z 并排
- 绝对星等—红移图新增可选等视星等参考虚线（红色 dashed，M(z)=m−μ(z)，
  前端 Planck18 数值积分与后端 astropy 偏差 <0.001 mag）
- 每张统计图头部新增「数据」按钮下载当前绘图 CSV（文件名带日期）；绝对星等图按勾选波段
  过滤且含银消改正前后星等（`abs_mag_points` 新增 mag_raw/mag_corr/mag_sys/gext_corr）

### v2.15（2026-09-04）— 宿主测光银河系消光改正标记与应用

- 宿主测光表每行新增 `gext_corr`（是否已做银河系消光改正，PUT 必填，缺失 400）；前端测光表加「银消已改正」是/否列，新行不预选、保存时显式提醒，缺键存量行显示为「否（未改正）」
- 下游统一使用改正后数据：`/api/stats/hosts` 绝对星等点（新增 `gext_applied`/`gext_Alambda`，tooltip 标注）与 hostfit pcigale 拟合（run.log 记录改正点数）对未改正行按 CSFD+Rv3.1+P92 实时改正（`extinction.correct_host_phot`，只算不写；坐标宿主优先、回退暂现源）
- 新增 `GET /api/export/host_photometry/<tid>`（需登录）：宿主测光 CSV/JSON 导出，含改正后星等 `mag_gextcor` 列；宿主信息卡新增「下载数据表」按钮

### v2.14（2026-09-03）— 坐标时分秒输入 + 宿主测光/统计增强

- 事件与宿主星系坐标输入支持十进制度与时分秒格式，入库统一转度（`backend/coords.py`，前端 `js/coords.js` 即时提示）
- 宿主测光表：band 下拉/模糊补全 + 未入库提示；新增 `upperlimit` 列（不参与拟合）；`mag_err` 可空，缺省按 σ=0.2 mag 处理（不落库）
- 全局统计宿主子页：删除「宿主 z vs 暂现源 z」散点，新增「宿主星系绝对星等 — 红移」图（`/api/stats/hosts` 返回 `abs_mag_points`，astropy Planck18 距离模数；Y 轴星等线性 / mJy 对数可切，波段勾选/色阶/符号/误差棒与光变图同规则）
- 修复详情页 Aladin 组件被 sticky 页眉遮挡（全屏/弹层按钮不可点击）的层叠问题
- 波段工具（频率排序/光谱色阶/AB↔mJy/滤波器缓存）抽为共享模块 `frontend/js/bands.js`

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
