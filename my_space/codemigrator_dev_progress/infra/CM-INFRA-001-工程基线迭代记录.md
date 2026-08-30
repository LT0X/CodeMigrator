# CM-INFRA-001-工程基线_迭代记录

> 本记录按 `CodeMigrator迭代记录模板.md` 维护，记录 CM-INFRA-001 的工程基线实现、验证证据和未完成的环境项。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-INFRA-001
- **关联模块/文档**：M-01、`feature/infra-python-skeleton`、`my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_核心目录架构设计.md`

## 1. 变更动机

CM-CORE-001 已合并，需要在其上建立可锁定、可检查、可启动的 Python 3.12 工程基线，供后续任务消费。实现范围依据 M-01 与 `my_space/code_alignment_record/infra/CM-INFRA-001-对齐记录.md` 的 D-01～D-08 及 V-M01 验收条款。

## 2. 变更内容

- 新增 `pyproject.toml`、`uv.lock`、根 `README.md` 和 `.dockerignore`，建立 uv 单包 src-layout、唯一 `codemigrator-app` 入口及构建上下文边界。
- 新增 `src/codemigrator/` 七个空业务子包及 README，连同既有 `core` 组成恰好八个固定子包；新增 import-linter 三类契约。
- 新增 Go→Python 描述符、真实 tree-sitter-go shared library 与摘要文件；新增 target-python 工具链 Dockerfile 和构建 digest 清单。
- 新增 `compose.yaml`（仅 app + PostgreSQL 17）、`migrations/0001_schema_migrations.sql`、app Dockerfile、seccomp 声明和 GitHub Actions CI。
- 新增 `tests/infra/`、`tests/recovery/`、`tests/security/` 及各子包测试目录；补充基础设施契约测试和主任务表开工记录。
- 关键取舍：Compose 不声明本机 `env_file`，而要求 `POSTGRES_PASSWORD` 由 Compose 进程环境或调用方显式 `--env-file` 注入，避免将本机说明性文件或凭据送入构建上下文；app 镜像显式复制 `README.md`、`descriptors/` 和 `migrations/`。
- 唯一一次独立审查返回 `REQUEST_CHANGES` 后，在原分支修正 import-linter 层级、Compose 沙箱前置配置、目标端 `allowed_domains`、锁文件工具版本和 M-01 `docs/` 目录树，并强化描述符契约测试；不追加同一 PR 的审查轮次。

## 3. 自测与验证结果

- `/home/xtc/env/codemigrator-infra/bin/uv lock --check`：通过。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen pytest -q`：70 passed。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen lint-imports`：3 contracts kept，0 broken。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen ruff check .`：通过。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen mypy src`：通过。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen python -m compileall -q src tests`：通过。
- `POSTGRES_PASSWORD=compose-test-only CODEMIGRATOR_CGROUP_DELEGATED_DIR=/sys/fs/cgroup docker compose config --quiet`：通过；未向仓库写入凭据，未读取本机说明性 `my_space/.env`。
- target-python Dockerfile 已与 `uv.lock` 的 pytest 8.4.2、ruff 0.16.5、mypy 1.20.2 对齐；按新版本重建镜像时因 Docker/PyPI 外部下载无响应中止。现有本地镜像仍为旧版本（pytest 8.3.5、ruff 0.9.10、mypy 1.15.0），其旧 digest 不作为新 Dockerfile 的最终构建证据。
- 验收条款：`V-M01-V4-001 ✓`、`V-M01-V4-002 ✓`、`V-M01-V4-004 ✓`、`V-M01-V4-006 ✓`、`V-M01-V4-007 ✓`、`V-M01-V4-008 ✓`、`V-M01-V4-011 ✓`（由 infra/contracts 测试和静态检查覆盖）。
- 未覆盖项：完整 app 镜像构建与 app+PostgreSQL 运行冒烟未完成。Docker daemon 在首次构建时因外部 registry/依赖下载网络无响应，构建进程长期等待后终止；Dockerfile、Compose 健康检查和配置解析均已保留并通过静态/配置验证。不得以静态检查替代该运行证据。

## 4. 影响面与风险

- 固定八子包和 import-linter 契约成为后续模块的工程边界；新增业务逻辑必须落在对应任务，不得把骨架扩展为跨层实现。
- 描述符和 grammar 是声明式资源；摘要/registry 语义由 CM-SPEC/CM-ANALYSIS 消费任务实现，本任务不复制其逻辑。
- Compose 密码只接受外部注入；当前本机 `my_space/.env` 含说明性内容而非 Docker env 文件，因此未修改该用户文件。
- app 镜像构建和 target-python 镜像重建仍受外部 registry/PyPI 网络影响；发布前需在可联网环境重新构建并执行 PostgreSQL healthcheck 冒烟，同时更新 target 镜像清单与描述符 digest。

## 5. 后续行动

- [ ] 在可联网 Docker 环境执行 target-python/app 镜像构建，并运行 `POSTGRES_PASSWORD=<external-value> CODEMIGRATOR_CGROUP_DELEGATED_DIR=<delegated-cgroup-dir> docker compose up --build -d`，记录 app/PG 健康检查结果后清理服务。
- [x] 完成分支范围复核，提交并推送 `feature/infra-python-skeleton`；当前待将本次反馈修订提交并推送。
- [x] 按用户流程对本任务 PR 只执行一次独立审查并等待其完成；反馈已在原分支修复，后续不追加审查轮次，直接合并。

## 6. 附录

### CHG-20260830-06：CM-INFRA-001 唯一审查反馈修复

* 时间：2026-08-30
* 变更类型：审查修订/验证收口
* 变更原因：唯一一次独立审查完成后返回 `REQUEST_CHANGES`；按用户流程只修复该次反馈，不重新发起审查。
* 变更内容：拆分 `api`/`runtime` import-linter 层级；补齐 Compose 的 seccomp、`SYS_ADMIN` 与 cgroup v2 委派目录；为 Python 目标描述符声明最小依赖域白名单；将 target-python 工具版本对齐 `uv.lock`；同步 M-01 目录树并强化契约测试及凭据构建边界。
* 验证：70 passed；import-linter 3 contracts kept；Ruff、mypy、compileall 和 Compose config 通过。target-python 新版本构建因 Docker/PyPI 下载无响应中止，现有旧版本本地镜像未作为新版本证据。
* 后续行动：提交并推送当前分支，直接合并 PR #2；合并后拉取 `develop`。联网环境下重建 target/app 镜像并完成 PG 冒烟。

- `CM-CORE-001` PR #1 已合并提交：`683f398`；本任务基于合并后的 `develop` 创建。
- 旧 infra WIP 通过 stash 可恢复方式迁移到当前分支，未重置或覆盖用户主工作区修改。
