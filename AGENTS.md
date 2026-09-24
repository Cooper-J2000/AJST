# AJST 暂现源目录系统 — agent 与维护者规则

> 本文件**随代码入库**，任何机器都能读到，因此**不含本机路径/端口/凭据位置**——
> 那类信息在该机器的 `AGENTS.local.md`（不入库），**若存在先读它**。
> 公开说明 `README.md`；**技术文档唯一权威源 `docs/TECHNICAL.md`**。历史细节与未发布源名单
> 都是**本机文件、不入库**（合作者只需 PR 与 git 历史）。

## 1. 仓库布局

```
AJST_Transient_lc_Cata/   ← 代码仓库（remote Cooper-J2000/AJST）
├── backend/ frontend/ docs/ scripts/ tests/ README.md requirements.txt
└── catadata/             ← 数据仓库（remote Cooper-J2000/AJST-Data），就地共存
    ├── info/ lc/ spectra/ filters.json …  ← 数据文件
    ├── gcn/archive/      ← 可再生成的 GCN 存档（不进 git）
    └── backups/          ← 本地 pg_dump 备份（不进 git）
```

- 代码仓库的 `.gitignore` 忽略整个 `catadata/`。
- **两份 `.gitignore` 自己都不进 git**。换机器/新 worktree 后第一件事是按 §6 重建，否则
  `git add -A` 会把 EP 数据、GCN 存档、备份全部提交（不可逆）。

## 2. 数据的"真相方向"（最重要）

```
网页端/API 修改 → PostgreSQL ──etl.py --dump──▶ catadata/ 文件 ──git──▶ AJST-Data
catadata/ 文件  ──etl.py（全量）/ --sync（增量）──▶ PostgreSQL
```

- **平时数据库是权威**；网页端改动只写库、不回写文件（例外：光谱上传/删除）。
- **发布前**：`python3 backend/etl.py --dump` 把库导出覆盖到 `catadata/` 再提交。
- **危险**：裸 `python3 backend/etl.py` 会 TRUNCATE 库再从文件导入 → 未 dump 的网页端改动被文件旧
  数据覆盖；跑前想清楚哪边权威，不确定先 `pg_dump` 留底。`--sync` 也是「文件→库」。
- **`--dump` 只写不删**：删除整个源、或把某源光变点删光后，需手动删 `catadata/info/<源名>.json`
  与 `lc/<源名>.csv` 再提交，否则仓库残留陈旧数据。
- **全量重建 / `--sync` 之后**：跑 `python3 scripts/backfill_gext_cache.py` 恢复派生缓存
  （幂等 ~5 s）。不跑也正确（读路径回退现算），只是宿主统计/消光热路径变慢；这些列不在文件中，`--dump` 不导出。

## 3. 日常发布流程

```bash
cd <repo>/backend && python3 etl.py --dump      # 库 → 文件（info/lc/filters.json）
cd ../catadata && git status                    # 不应出现未发布源数据（见 §4）
git add -A && git commit -m "data: …（vX.YZ）" && git push
```
代码改动同理，在代码仓库根提交推送（格式见 `docs/COMMIT-CONVENTION.md`）。

**合并外部 PR**：`gh pr merge <n> --rebase`；合并后 `git pull --ff-only`。

## 4. 不进 GitHub 的数据（本地保留）

| 内容 | 排除方式 |
|---|---|
| 未发布源数据（info/lc/spectra） | 本机名单 → catadata/.gitignore |
| `gcn/archive/`（`fetch_gcn_archive.sh` 可重拉） | catadata/.gitignore |
| `backups/`、`backup_*.sql` | catadata/.gitignore |
| `backend/fitting_store/`（拟合产物） | 代码仓库 .gitignore |
| `AGENTS.local.md`、归档的 `技术文档.md`、`.gitignore` 本身 | 代码仓库 .gitignore |

要发布某个未发布源：从本机名单与 `catadata/.gitignore` 各删对应行后 `git add`；撤下已发布的必须改写
git 历史并 force push。

## 5. 各子系统权威方向（一句话规则）

| 子系统 | 权威在哪 | 回写方式 / 注意 |
|---|---|---|
| filters.json | 数据库 | `--dump` 自动覆盖回文件；只改文件跑 `etl.py --filters` |
| articles | info JSON 的 `articles` 字段 | `--dump` 写出；导入按字段全量替换（自增 id 定位） |
| host_galaxies | info JSON 的 `host_galaxy` 字段 | 导入按字段 upsert（null=删除）；拟合历史不入文件 |
| spectra | **文件**（`catadata/spectra/`） | 上传/删除同步维护文件+表；`import_spectra()` 两遍扫描重建 |
| tags | 数据库 | `--dump` 落 `catadata/tags.json`；`register_used_tags` 登记现役 tag |
| users | 纯数据库 | 无文件对应 |
| relations.json | 代码仓库静态文件 | 非用户数据，随代码提交 |
| 消光/距离模数缓存 | 数据库 | 不落盘；重建后跑 `backfill_gext_cache.py` |
| GCN 存档 | 可再生成 | `scripts/fetch_gcn_archive.sh`；不进 git |

## 6. `.gitignore` 重建清单（换机器/新 worktree 必做）

代码仓库根 `.gitignore` 必需项：`catadata/`、`backend/fitting_store/`、`*.log`、
`AGENTS.local.md`、`技术文档.md`、`docs/ops-history/`、`.gitignore`、`__pycache__/`、`*.pyc`、`.env`、`*.pem`、`*.key`。
`catadata/.gitignore` 必需项：**见本机名单 `catadata/.sensitive_sources`**（逐行一条模式，须原样出现在 `.gitignore` 里）＋
`gcn/archive/`、`backups/`、`.gitignore`。**公开文件里不写具体源名**。
`scripts/preflight.sh` 第 2/4 组逐项核对（缺项 FAIL）。

## 7. 运维与部署（通用规则；本机具体值见 `AGENTS.local.md`）

- **部署顺序（涉及新列时）**：改后端代码 → 重启服务（启动跑 `init_db()` 幂等列迁移、补出新列）→
  **再**跑 `etl.py`/`backfill_gext_cache.py`。反了会报 `UndefinedColumn`（新列不存在）。
- **跑生产回填/etl 前务必 `env -u PGOPTIONS`**：残留的 `PGOPTIONS='-c search_path=review,public'`
  会让命令**静默写进 review 副本而非生产库**（踩过一次）；核对一律 `public.` 限定表名。
- **监听地址只允许 `127.0.0.1`**：`start.sh` 对非 loopback 的 `AJST_HOST` 直接拒绝并 exit 1；需要
  外部可达走 ssh 隧道，**不要移除该护栏、不要改回 `0.0.0.0`**。自测实例也绑 loopback 且换端口。
- **密码/token 不得写进服务读不到的地方**：systemd 不读 shell 配置，必须经单元 `EnvironmentFile=`
  注入（曾因此让 ingest 一直 503）。凭据不入代码、不入库。
- **`/api/stats/hosts` 的 ETag 自检**：改过 `filters` 表后，带旧 `If-None-Match` 应回 **200**；仍
  304 说明有影响输出的输入没进 token（`vega2ab` 曾漏过）。往该响应加字段前先想清楚其输入要不要进 token。
- **验收与护栏**：`scripts/acceptance/run_all.sh`（L1 口径/坐标/时间/消光 + L2 HTTP 契约 + L3 数据
  不变量；只读）；`preflight.sh`（9 组，退出码 1 = 有阻塞项）。
- **环境重建**：`scripts/bootstrap_env.sh` + `requirements-lock.txt`（后者是全量锁）。

## 8. 给 agent 助手的硬性规则

1. **先读本文件 + 本机 `AGENTS.local.md`（若存在）再动手**；改完代码涉及部署/运维约定时同步更新
   本文件与 `docs/TECHNICAL.md`（公开版**不含本地路径与密码**）。
2. **永远不要**在未确认方向时跑裸 `etl.py`（全量重建会清库）；先 `pg_dump` 备份。
3. **永远不要** `git add -f` 被 `.gitignore` 排除的文件（未发布源、gcn/archive、fitting_store、备份）。
4. 推送前检查：**暂存区不得出现任何被 `.gitignore` 排除的文件**（preflight 第 4 组按本机名单核对）。
5. git push 超时是常态（网络原因），直接重试。
6. 涉及删除数据、改写 git 历史、force push 的操作，必须先向作者确认。
7. 开工前先跑 `preflight.sh`：有 FAIL（退出码 1）不许开工。它拦的是写错库（PGOPTIONS 残留、
   DATABASE_URL 不对）、`.gitignore`/本机名单缺失致未发布源被提交、绑非 loopback、库被清空。
8. **入口文件（`CLAUDE.md` 等）只允许是指针**：规则、命令、说明一律写进本文件（preflight 会检查）。

## 9. 文档索引

| 文件 | 用途 |
|---|---|
| `docs/TECHNICAL.md` | **技术文档唯一权威源**（表结构 / API / 公式 / ETL） |
| `docs/COMMIT-CONVENTION.md` | 提交格式、13 个范围、tag/trailer |
| `docs/ops-history/` | **本机**（不入库）：历史沿革与实现细节 |
| `AGENTS.local.md` | **不入库**：本机路径/端口/服务/凭据位置/备份 |
