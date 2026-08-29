# CM-INFRA-001 对齐记录

> 用途：本文件是任务 `CM-INFRA-001`（模块 M-01 核心目录架构设计）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段     | 内容                                                                                     |
| ------ | -------------------------------------------------------------------------------------- |
| 任务ID   | `CM-INFRA-001`                                                                         |
| 模块编号   | M-01                                                                                   |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_核心目录架构设计.md` |
| 主任务表   | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3                   |
| 对齐轮次   | 第 1 轮（Wave 0+1 轮）                                                                      |
| 对齐日期   | 2026-08-29                                                                             |
| 对齐参与者  | 用户 + TRAE agent                                                                        |
| 对齐状态   | 已对齐                                                                                    |

## 1. 任务理解

### 1.1 范围（做什么）

交付 Python 工程基线骨架（M-01 冻结）：

* **单包 src-layout**：`pyproject.toml`（uv 管理；`requires-python >= 3.12`，锁定 3.12——D-02；`[project.scripts]` 仅 `codemigrator-app`）+ `uv.lock`。

* **8 子包骨架**：`src/codemigrator/{core,analysis,planning,workspace,verification,sandbox,runtime,api}/` 恰 8 个；每子包根 README 固定四项（负责/不负责/允许依赖/公共入口）。

* **import-linter 契约**：layer/forbidden/independence 与 M-01 冻结依赖图 exact-match（四层：契约层 core / 领域层 analysis+planning+verification / 执行层 workspace+sandbox / 编排投影层 runtime+api）；CI 违例拒绝合并。

* **descriptors/ 首对 Go→Python**（D-01）：`descriptors/source/go/descriptor.json`（SourceToolchain 字段 + tree-sitter-go grammar 制品与 sha256）+ `descriptors/target/python/descriptor.json`（TargetToolchain 五类命令模板 + artifact\_rules + build\_excludes + toolchain\_image\_digest）。

* **compose.yaml**：app + PostgreSQL 17 两服务基线（D-05）；凭据经 `my_space/.env`，不进仓库；MinIO/观测为可选 profile 不在本任务落盘。

* **migrations/**：纯 SQL 版本化迁移文件 + runtime 内置顺序执行器（D-04）。

* **tests/**：contracts/recovery/security 三冻结目录 + 子包单测目录（D-08）。

* **deploy/**：seccomp policy、自建 target-python 工具链镜像（D-06：python:3.12-slim 基础 + uv/pytest/ruff/mypy；构建后 digest 写入镜像构建清单，描述符登记该 digest）、app 镜像构建文件；不含凭据。

* **CI（GitHub Actions）**（D-03）：pytest + import-linter 契约检查 + runtime 外环境读取静态审查。

* **技术栈映射落地**（M-01 映射决策表，唯一 owner）：FastAPI / SQLAlchemy 2.0(async)+asyncpg / Pydantic v2 / py-tree-sitter / structlog+OpenTelemetry / asyncio / 异常层次+冻结错误码。

### 1.2 边界（不做什么）

* 不实现任何子包业务逻辑（各归 CM-ANALYSIS/PLAN/… 任务）；骨架子包仅含包结构与 README。

* 不交付第二语言对（TS→Python 仅文档说明性案例，按需后补）。

* 不建 `docs/` 目录（D-07，与 M-01 冻结树的偏差，见 §5 同步义务）。

* 描述符 registry 扫描/摘要校验逻辑归消费子包（M-05/M-06 语义）；本任务只交付资源文件与结构。

* 三码拒绝（DESCRIPTOR\_NOT\_FOUND 等）语义归 M-05/M-02 owner；本任务不实现。

* advisory lock / readiness 语义归 M-03 runtime；本任务仅提供 entry point 声明。

* sandbox bwrap 执行适配归 CM-SANDBOX-001；compose 中 app 不含沙箱实现细节。

* 不含 Web/CLI 应用代码（apps/codemigrator-cli、web/ 仅建目录占位，实现在 CM-WEB-001）。

### 1.3 产出物

`pyproject.toml`、`uv.lock`、`src/codemigrator/` 8 子包骨架（含 README×8）、`descriptors/source/go/`（descriptor.json + grammar/ 制品与 sha256）、`descriptors/target/python/descriptor.json`、`migrations/`（初始 schema 骨架或空基线 + 执行器接口预留归 runtime）、`tests/{contracts,recovery,security,core,…}/`、`compose.yaml`、`deploy/`（seccomp、target-python 镜像构建、镜像构建清单）、`.github/workflows/ci.yml`、根 README、模块迭代记录。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| #    | 决策点                                                           | 可选项                          | 用户确认结论                                                                                    | 依据                         |
| ---- | ------------------------------------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------- | -------------------------- |
| D-01 | descriptors 首对（验收载体 click-video Go→Python vs 文档主案例 TS→Python） | Go→Python / TS→Python / 双对   | **Go→Python 单对**（直配 §10 click-video 靶场；TS 案例仅文档说明性）                                       | 对齐问答 Q-01（2026-08-29）      |
| D-02 | Python 解释器基线                                                  | 3.12 / 3.13 / 3.12+矩阵        | **3.12**（Ubuntu 24.04 原生，requires-python >=3.12）                                          | 对齐问答 Q-02                  |
| D-03 | CI 运行形态                                                       | GitHub Actions / 仅本地脚本 / 双轨  | **GitHub Actions**（仓库已托管 github.com/LT0X/CodeMigrator）                                    | 对齐问答 Q-03                  |
| D-04 | PostgreSQL schema 迁移机制                                        | 纯 SQL+内置执行器 / Alembic / 外部工具 | **纯 SQL 版本化迁移文件 + runtime 内置顺序执行器**                                                       | 对齐问答 Q-04                  |
| D-05 | PostgreSQL 服务版本                                               | 17 / 16                      | **PostgreSQL 17**（LISTEN/NOTIFY、advisory lock、UUID 全支持）                                   | 对齐问答 Q-06                  |
| D-06 | 目标端工具链镜像提供方式                                                  | 自建 / 官方 uv 镜像扩展 / 外部直 pin    | **自建**：deploy/images/target-python/（python:3.12-slim + uv/pytest/ruff/mypy），digest 写入构建清单 | 对齐问答 Q-07                  |
| D-07 | 骨架 docs/ 目录（M-01 树含 docs/ vs 设计文档实际在 my\_space gitignore）     | 不建 / 建放公开文档 / 设计文档入库         | **不建 docs/**：设计文档继续留 my\_space（私有），仓库不进设计文档                                               | 对齐问答 Q-08（含 M-01 树偏差，见 §5） |
| D-08 | 子包单元测试目录组织                                                    | 三类+子包目录 / 仅三类 / 镜像 src       | **三类冻结目录 + 子包单测目录**（tests/core/、tests/analysis/…；DoD 按目录选跑）                               | 对齐问答 Q-09                  |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

* CM-CORE-001 公共契约（子包骨架消费 core 类型）；pyproject 依赖登记：pydantic v2、uuid-utils（CM-CORE-001 D-01 联动）、semver、fastapi、sqlalchemy\[asyncio]+asyncpg、py-tree-sitter、structlog、opentelemetry-\*。

### 3.2 下游消费

* 全部 Wave 1+ 任务在本骨架上开发；CM-ANALYSIS 消费 descriptors/source/go grammar；CM-SANDBOX 消费 target-python 工具链镜像与 seccomp；CM-API/RUNTIME 消费 compose 拓扑与 migrations。

### 3.3 跨模块接口边界

* 语言映射决策表唯一 owner 为 M-01（本篇），实现语言词汇一律引用不另立。

* 描述符三码拒绝制 owner 为 M-05/M-02；registry 语义消费归 M-05/M-06。

* compose 拓扑与 `[project.scripts]` 唯一 entry point（codemigrator-app）归本任务落盘；启动语义（advisory lock、readiness）归 M-03。

* migrations owner: runtime（执行器实现归 CM-RUNTIME-001，本任务交付目录与文件规范）。

## 4. 验收条款映射

| 条款               | 内容摘要                                                                                      | 验证方式                                                                              |
| ---------------- | ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| V-M01-V4-001（追溯） | import-linter 契约与冻结清单 exact-match：恰 8 子包、无环、无清单外依赖边                                       | CI import-linter 检查（GitHub Actions）                                               |
| V-M01-V4-002（追溯） | plugins/、八方法 RPC、长度前缀帧、能力协商、进程身份零残留                                                       | 仓库结构审查 + 依赖图检查                                                                    |
| V-M01-V4-004/010 | grammar 摘要不符不入可用集；内容变更未递增 descriptor\_version 启动拒绝                                        | 描述符资源测试（结构 + sha256 一致性文件级校验；registry 逻辑测试归 M-05/M-06）                            |
| V-M01-V4-006     | 新增语言对 src/ 零 diff                                                                         | 以 Go→Python 首对交付验证（骨架任务本身即证据）                                                     |
| V-M01-V4-007     | CLI/web 不出现在核心子包依赖图                                                                       | import-linter forbidden 契约 + CI                                                   |
| V-M01-V4-008     | sandbox 无 SQL 依赖、挂载表不含 UDS/Docker socket/宿主凭据                                             | 静态审查 + deploy 挂载配置审查（沙箱完整验收归 CM-SANDBOX-001）                                      |
| V-M01-V4-011（调整） | 仅 `codemigrator-app` 一个 entry point（V6 无 sandbox-worker）；第二实例 advisory lock 失败归 M-03 行为验收 | pyproject `[project.scripts]` 审查                                                  |
| V5 可验收增量（本任务面）   | app+PG 两服务 compose 健康检查通过；descriptors 只提供语言事实/工具链/命令模板                                    | compose config + `docker compose up` 冒烟（app 骨架进程可启动）；描述符 schema 与 M-00 类型一致（契约测试） |
| D-05/D-06 落地     | PG 17 镜像 pin；target-python 自建镜像 digest 登记构建清单                                             | compose.yaml 审查；镜像构建脚本 + digest 清单一致性校验                                           |

## 5. 风险与注意点

* **M-01 树偏差同步义务**：D-07「不建 docs/」与 M-01 冻结树 docs/ 条目冲突——实施期（代码落盘后）须按 AGENTS.md §3.4 经用户确认同步修订 M-01 目录树节（本对齐已获用户确认结论，修订时引用本记录 Q-08）。

* **uuid-utils 依赖联动**：CM-CORE-001 D-01 引入，本任务 pyproject 登记（跨任务协调点，勿遗漏）。

* **grammar 制品来源**：tree-sitter-go shared library（.so）需预编译或随 py-tree-sitter 构建流程产出；`grammar_carrier` 声明与 sha256 计算须在描述符交付时完成（WSL 本机编译，注意 R-06 磁盘配额约 100G）。

* **compose 凭据纪律**：PostgreSQL 密码等一律经 `my_space/.env` 环境注入，compose 文件与环境快照零凭据（AGENTS.md §1.1）。

* **CI 静态审查范围**：runtime 之外子包环境读取检查（os.environ 等 import 扫描）+ import-linter 契约 + pytest；ruff/mypy 进 CI（工具链映射表事实）。

* **uv 迁移纪律**：依赖变更必须走 `uv lock` 更新；镜像与 lock 版本 pin 一致。

* 分支纪律：本任务实施从 develop 切 `feature/infra-python-skeleton` 类分支（AGENTS.md §2.3）。

* WSL2 环境现状：Python 3.12+/uv 环境待建（主表 R-01），本任务实施时先建 `~/env` 隔离环境（AGENTS.md §1.2：禁止在 \~/env 根目录直接安装）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| #    | 问题                                                 | 用户结论                                                                                       |
| ---- | -------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Q-01 | descriptors 首对建哪个（Go→Python 靶场 vs TS→Python 文档主案例） | Go→Python 单对                                                                               |
| Q-02 | Python 解释器基线版本                                     | 3.12                                                                                       |
| Q-03 | CI 运行形态                                            | GitHub Actions                                                                             |
| Q-04 | PostgreSQL schema 迁移机制                             | 纯 SQL + runtime 内置执行器                                                                      |
| Q-05 | （用户主动提问）骨架文件夹是否应最先对齐、是否阅读了设计文档                     | 澄清：骨架=CM-INFRA-001 本体，正按主表 Wave 0 顺序（CORE→INFRA）对齐中；对齐阶段不创建任何文件/代码；M-01 已完整通读，骨架树已在对话中呈现确认 |
| Q-06 | compose PostgreSQL 版本                              | PostgreSQL 17                                                                              |
| Q-07 | 目标端工具链镜像提供方式                                       | 自建（python:3.12-slim + uv/pytest/ruff/mypy）                                                 |
| Q-08 | M-01 树 docs/ 与 my\_space 设计文档落点冲突                  | 不建 docs/（设计文档留 my\_space）；（用户追问）确认对齐流程为逐任务读取对应设计文档                                         |
| Q-09 | 子包单元测试目录组织                                         | 三类冻结目录 + 子包单测目录                                                                            |

## 7. 变更记录

| 日期         | 变更   | 说明                                                                 |
| ---------- | ---- | ------------------------------------------------------------------ |
| 2026-08-29 | 建立记录 | 依据 M-01 V6 设计文档（目录树/映射决策表/描述符资源节）与主任务表 §7.3；用户经提问工具逐项确认 D-01\~D-08 |

