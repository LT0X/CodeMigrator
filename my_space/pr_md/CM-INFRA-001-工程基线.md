# feat(infra): establish Python project baseline

## 1. 标题与关联

- PR 标题：`feat(infra): establish Python project baseline`
- 任务编号：`CM-INFRA-001`
- 任务分支：`feature/infra-python-skeleton`
- 模块迭代记录：[CM-INFRA-001-工程基线迭代记录](my_space/codemigrator_dev_progress/infra/CM-INFRA-001-工程基线迭代记录.md)

## 2. 背景

依据 M-01《核心目录与描述符资源架构》及 CM-INFRA-001 对齐记录 D-01～D-08，建立后续任务共同使用的 Python 3.12 单包工程基线：固定八个子包、依赖边界、声明式 Go→Python 描述符、Compose 拓扑、迁移入口、部署资产和 CI 质量门。

## 3. 变更点

- 工程元数据：新增 `pyproject.toml`、`uv.lock`、`.dockerignore` 和根 `README.md`；仅暴露 `codemigrator-app` console script。
- 包边界：补齐 `analysis`、`planning`、`workspace`、`verification`、`sandbox`、`runtime`、`api` 七个骨架子包及 README；配置 import-linter layer/forbidden/independence 三类契约，并将 `api`、`runtime` 作为有序独立层以表达 runtime→api。
- 声明式资源：新增 Go 源端 descriptor、真实 tree-sitter-go grammar 与 SHA-256；新增 Python 目标端 descriptor、五类命令模板、artifact rules、最小 `allowed_domains` 白名单、build excludes 和 target-python 镜像 digest 清单。
- 部署与数据：新增 app Dockerfile、target-python Dockerfile、seccomp 声明、包含 seccomp/capability/cgroup 委派前置配置且仅含 app+PostgreSQL 17 的 `compose.yaml` 和 `migrations/0001_schema_migrations.sql`。
- 测试与 CI：新增 infra/recovery/security 及各子包测试目录、基础设施契约测试和 GitHub Actions 质量工作流。
- BREAKING：无；保留 CM-CORE-001 已合并公共契约，未实现任何下游业务逻辑或运行时执行器。

## 4. 自测证据

- [x] `uv lock --check`：通过。
- [x] `uv run --frozen pytest -q`：70 passed。
- [x] `uv run --frozen lint-imports`：3 contracts kept，0 broken。
- [x] `uv run --frozen ruff check .`：通过。
- [x] `uv run --frozen mypy src`：通过。
- [x] `uv run --frozen python -m compileall -q src tests`：通过。
- [x] `POSTGRES_PASSWORD=compose-test-only CODEMIGRATOR_CGROUP_DELEGATED_DIR=/sys/fs/cgroup docker compose config --quiet`：通过；密码仅为命令行临时值，不写入仓库。
- [x] target-python Dockerfile 工具版本已与 `uv.lock` 对齐（pytest 8.4.2、ruff 0.16.5、mypy 1.20.2）；新版本镜像重建因 Docker/PyPI 外部下载无响应中止，现有旧版本本地镜像未作为新版本证据。
- [x] `uv build --wheel --sdist`：wheel/sdist 构建通过，包内 core 资源已核验。
- [ ] app 镜像完整构建与 app+PostgreSQL 运行冒烟：未完成。Docker daemon 在依赖下载阶段因外部 registry/PyPI 网络无响应，已中止；未将静态检查冒充为运行通过，详见模块迭代记录。
- [x] 未执行真实模型测试：工程基线不包含 provider、token 或模型会话行为。

## 5. 风险与回滚

- 风险：app/target-python 镜像和 PostgreSQL 运行冒烟仍需在可联网 Docker 环境复核；target-python 清单当前标记 `rebuild-required`，发布前须以新版本实际镜像 ID 更新清单与描述符。
- M-01 目录树已按对齐 Q-08/D-07 同步移除公开 `docs/`，设计、对齐与迭代文档仅保存在 `my_space/` 私有空间。
- 按流程，本 PR 已完成唯一一次独立审查并等待其返回；本次反馈已在原分支修复，不再追加审查，复核后直接合并。
- 回滚：使用本分支提交的 `git revert`；不重置 `develop`，不覆盖主工作区用户修改。
