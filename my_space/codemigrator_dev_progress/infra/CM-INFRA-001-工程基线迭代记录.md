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

## 3. 自测与验证结果

- `/home/xtc/env/codemigrator-infra/bin/uv lock --check`：通过。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen pytest -q`：70 passed。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen lint-imports`：3 contracts kept，0 broken。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen ruff check .`：通过。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen mypy src`：通过。
- `/home/xtc/env/codemigrator-infra/bin/uv run --frozen python -m compileall -q src tests`：通过。
- `POSTGRES_PASSWORD=compose-test-only docker compose config`：通过；未向仓库写入凭据，未读取本机说明性 `my_space/.env`。
- target-python 镜像构建：`codemigrator/target-python:0.1.0` 成功，本地镜像 ID 为 `sha256:0d10938113bc70448b81f08680d1e82e33394e1911be2e8c97cd5a7a09d37fa3`，与 `deploy/image-build-manifest.json` 一致。
- 验收条款：`V-M01-V4-001 ✓`、`V-M01-V4-002 ✓`、`V-M01-V4-004 ✓`、`V-M01-V4-006 ✓`、`V-M01-V4-007 ✓`、`V-M01-V4-008 ✓`、`V-M01-V4-011 ✓`（由 infra/contracts 测试和静态检查覆盖）。
- 未覆盖项：完整 app 镜像构建与 app+PostgreSQL 运行冒烟未完成。Docker daemon 在首次构建时因外部 registry/依赖下载网络无响应，构建进程长期等待后终止；Dockerfile、Compose 健康检查和配置解析均已保留并通过静态/配置验证。不得以静态检查替代该运行证据。

## 4. 影响面与风险

- 固定八子包和 import-linter 契约成为后续模块的工程边界；新增业务逻辑必须落在对应任务，不得把骨架扩展为跨层实现。
- 描述符和 grammar 是声明式资源；摘要/registry 语义由 CM-SPEC/CM-ANALYSIS 消费任务实现，本任务不复制其逻辑。
- Compose 密码只接受外部注入；当前本机 `my_space/.env` 含说明性内容而非 Docker env 文件，因此未修改该用户文件。
- app 镜像构建仍受外部 registry/依赖网络影响；发布前需在可联网环境重新构建并执行 PostgreSQL healthcheck 冒烟。

## 5. 后续行动

- [ ] 在可联网 Docker 环境执行 app 镜像构建，并运行 `POSTGRES_PASSWORD=<external-value> docker compose up --build -d`，记录 app/PG 健康检查结果后清理服务。
- [ ] 完成分支范围复核，提交并推送 `feature/infra-python-skeleton`。
- [ ] 按用户流程对本任务 PR 只执行一次独立审查；若有问题仅在原分支修复、重新验证后直接合并，不追加审查轮次。

## 6. 附录

- `CM-CORE-001` PR #1 已合并提交：`683f398`；本任务基于合并后的 `develop` 创建。
- 旧 infra WIP 通过 stash 可恢复方式迁移到当前分支，未重置或覆盖用户主工作区修改。
