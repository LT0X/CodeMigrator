# CM-INFRA-001-工程基线详细设计

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-INFRA-001
- **所属模块/crate**：M-01 / `codemigrator-infra`
- **依据的架构文档**：M-01《核心目录与描述符资源架构》“八个子包：依赖无环，副作用归位”“描述符资源目录”“部署与目录树”及 V5 可验收增量
- **关联交付物**：实施计划 `my_space/Implementation_plan_doc/infra/CM-INFRA-001-工程基线实施计划.md`、迭代记录 `my_space/codemigrator_dev_progress/infra/CM-INFRA-001-工程基线迭代记录.md`

## 1. 需求与边界

- 本任务做什么（一句话）：建立可锁定、可检查、可启动的 Python 单包工程骨架，并交付 Go→Python 声明式描述符、app+PostgreSQL 17 部署基线、SQL 迁移入口目录、依赖边界契约和 CI。
- 不做什么（显式排除项）：不实现八个子包的领域逻辑；不实现 registry、迁移执行器、advisory lock、bwrap、REST/SSE、数据库 repository、CLI 交互、Web 页面或描述符三拒绝语义；不建 `docs/`；不引入 sandbox-worker、UDS/Protobuf 或 plugins。
- 覆盖的验收条款：`V-M01-V4-001`、`V-M01-V4-002`、`V-M01-V4-004`、`V-M01-V4-006`、`V-M01-V4-007`、`V-M01-V4-008`、`V-M01-V4-011`，以及 M-01 “V5 可验收增量”三项和 D-01～D-08 相关工程检查。

## 2. 契约引用

| 引用事实 | Owner 文档 | 引用形式 |
| --- | --- | --- |
| 8 子包及允许依赖 | M-01 | pyproject import-linter 三类契约；不在各子包复制依赖图 |
| `ArtifactKind`、`CheckAction`、`ModuleBoundaryStrategy`、描述符字段 | M-00 / CM-CORE-001 | `codemigrator.core` 类型；描述符 JSON 仅承载声明数据 |
| Go→Python 首对 | CM-INFRA-001 对齐 D-01 | `descriptors/source/go/` + `descriptors/target/python/` |
| Python 3.12、GitHub Actions、PG 17 | CM-INFRA-001 对齐 D-02/D-03/D-05 | pyproject、compose、workflow 的固定检查 |
| 纯 SQL 迁移 | CM-INFRA-001 对齐 D-04 | `migrations/NNNN_*.sql`，执行器归 runtime |
| 不建 docs/、三类测试目录+子包测试目录 | CM-INFRA-001 对齐 D-07/D-08 | 目录契约测试 |

## 3. 详细设计

### 3.1 结构与职责

- `pyproject.toml` 是唯一项目元数据与依赖声明；`uv.lock` 是解析后的安装事实；只暴露 `codemigrator-app`，指向 runtime 的可启动 stub，后续由 CM-RUNTIME-001 替换实现。
- `src/codemigrator/` 保留 core 已实现内容，并补齐 analysis、planning、workspace、verification、sandbox、runtime、api 七个空业务骨架。每个子包含 `__init__.py` 和 README，README 固定声明负责、不负责、允许依赖、公共入口。
- `descriptors/` 以 source/target + language-id 分目录；Go grammar 使用真实 shared-library 制品和同文件摘要，描述符中 `language_role`/grammar 载体等目录元数据由未来 registry 读取，不变成 Python import。
- target descriptor 的 `build_excludes` 使用 core `RepoRelativePath` 可接受的规范化路径（例如 `.venv`、`__pycache__`、`.pytest_cache`，不带尾斜杠）；消费方按目录/模式语义解释这些排除项，避免把空路径段写入公共契约。`allowed_domains` 声明 Python 依赖安装代理允许访问的最小域名集合。
- `migrations/` 只放顺序命名 SQL；第一份迁移只建立迁移版本表，具体领域表由 runtime/各任务按对齐记录追加。
- `deploy/` 放 app Dockerfile、target-python Dockerfile、seccomp 声明与镜像构建清单；不放凭据、不挂载 UDS/Docker socket。
- `.github/workflows/ci.yml` 执行锁定安装、pytest、import-linter、ruff/mypy 和 runtime 之外环境读取静态检查；CI 不实现运行时语义。

### 3.2 关键机制

工程依赖边界以 import-linter 检查，目标关系如下：

```text
core
↑ analysis, planning, verification
↑ sandbox, workspace
↑ runtime, api
```

实际允许边：analysis→core；planning→analysis/core；verification→core；sandbox→core；workspace→sandbox/core；runtime→全部下游；api→core。层内禁止互依，core 不反向导入任何业务包。`descriptors/` 和 `deploy/` 是运行期/部署期资源，不进入 import 图。

Compose 只启动 `app` 与 `postgres` 两服务：PG 使用 `postgres:17`，app 使用 `deploy/Dockerfile`；app 的沙箱前置配置固定声明 `seccomp=unconfined`、`SYS_ADMIN` 和可写的 cgroup v2 委派目录挂载，委派目录由 `CODEMIGRATOR_CGROUP_DELEGATED_DIR` 外部注入。PG 密码由 Compose 进程环境或调用方显式提供的 `--env-file` 注入，Compose 文件只引用必需变量，不存放值，也不把本机说明性文件作为镜像构建输入。健康检查只报告服务可用，不提前承诺 advisory lock 或领域 API。

### 3.3 数据与接口

- pyproject 的运行时依赖覆盖 core 当前实际使用的 pydantic、uuid-utils、rfc8785、semver，并登记 M-01 技术栈映射所需 FastAPI、SQLAlchemy async/asyncpg、tree-sitter、structlog、OpenTelemetry、httpx；测试/质量工具放 optional `dev` 组。
- 源端 descriptor 包含 M-00 `SourceToolchain` 字段和 `runtime_image_digest` 可选字段；target descriptor 包含 M-00 `TargetToolchain` 字段及 M-01 的 `artifact_rules`/`allowed_domains`/`toolchain_image_digest`/`build_excludes` 声明。grammar 摘要文件与 descriptor 的 `parser.grammar_sha256` 必须一致。
- `migrations/0001_schema_migrations.sql` 仅建立 `schema_migrations(version primary key, applied_at)`；顺序执行、事务和失败恢复归 runtime，不在本任务添加执行 Python API。
- app 入口 stub 只负责返回可用于容器启动的状态；不读取环境、不获取锁、不创建 actor，避免提前越界实现。

### 3.4 错误处理

- 资源/描述符的 `DESCRIPTOR_NOT_FOUND`、`DESCRIPTOR_DIGEST_MISMATCH`、`TOOLCHAIN_IMAGE_UNAVAILABLE` 只在 M-05/M-02 消费逻辑实现，本任务不新增错误码或处理器。
- Compose/CI 层不复制 StableErrorCode；基础设施错误通过命令退出码和健康检查表现，领域错误由下游 owner 引用 `codemigrator.core`。

## 4. 测试设计

- `tests/infra/`：pyproject/entry point、恰 8 子包、README 四项、描述符目录/摘要、迁移命名、Compose 服务/PG 17、Dockerfile 与 CI 静态内容。
- `tests/contracts/`：import-linter 配置覆盖冻结依赖图、core 源码在新增骨架后无反向依赖、CLI/Web 不进入 core 依赖图。
- `tests/recovery/`、`tests/security/`：只建立冻结目录与后续任务入口；不在基线任务伪造恢复/沙箱行为验收。

| 验收条款 | 用例名 |
| --- | --- |
| `V-M01-V4-001` | `test_exact_eight_subpackages_and_import_contract_config` |
| `V-M01-V4-002` | `test_retired_plugin_and_rpc_shapes_are_absent` |
| `V-M01-V4-004` | `test_go_grammar_digest_matches_descriptor` |
| `V-M01-V4-006` | `test_descriptor_pair_is_go_to_python_only` |
| `V-M01-V4-007` | `test_product_entry_directories_are_not_core_subpackages` |
| `V-M01-V4-008` | `test_deploy_files_contain_no_socket_or_credentials_mounts` |
| `V-M01-V4-011` | `test_only_codemigrator_app_console_script_exists` |
| V5 app+PG | `test_compose_has_only_app_and_postgres_17` |

## 5. 与架构文档的差异记录

- **已同步**：M-01 目录树已按对齐 Q-08（D-07）移除公开 `docs/` 条目，并明确设计、对齐与迭代文档仅保存在 `my_space/` 私有空间。
- **有差异**：M-01 历史 V4 条款仍提及 `codemigrator-sandbox-worker`，V6 对齐 D-06/主任务表已确定 sandbox 为 app 内部适配；实现只保留 `codemigrator-app`，在测试中防止 sandbox-worker entry point 残留。

## 6. 影响面

- 该基线使后续任务可以在统一 uv/CI/import-linter 约束下并行开发；core 依赖和八子包边界成为所有下游的构建前提。
- Compose 与迁移目录只提供拓扑/版本入口，不改变控制面行为；runtime/API/Spec 等任务必须在本任务基础上追加自身 schema/执行逻辑。
- target-python 镜像 digest 在构建后写入清单；若本机无法构建，不能把 Dockerfile 内容摘要冒充真实镜像 digest。
