# CM-INFRA-001-工程基线实施计划

> 本计划依据 M-01《核心目录与描述符资源架构》与 `my_space/code_alignment_record/infra/CM-INFRA-001-对齐记录.md` 编制。工程基线只建立项目边界、依赖和可启动骨架，不提前实现下游领域逻辑。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-INFRA-001
- **所属模块/crate**：M-01 / `codemigrator-infra`
- **关联交付物**：详细设计 `my_space/codemigrator_design_doc/detailed_coding_design/infra/CM-INFRA-001-工程基线详细设计.md`、迭代记录 `my_space/codemigrator_dev_progress/infra/CM-INFRA-001-工程基线迭代记录.md`

## 1. 目标与范围

- 完成定义（DoD）：单包 Python 3.12+ 工程可由 uv 锁定安装；`src/codemigrator/` 恰有 8 个子包且具备边界 README；只有 `codemigrator-app` console script；Go→Python 描述符、grammar 摘要、target-python 镜像构建清单、app+PostgreSQL 17 Compose、版本化 SQL 迁移目录、import-linter 配置、CI 和三类冻结测试目录均可被确定性检查。
- 范围排除项：不实现分析、规划、工作区、沙箱、验证、运行时、API 业务逻辑；不实现描述符 registry/摘要校验语义；不实现 bwrap、advisory lock 或迁移执行器；不建立 `docs/` 公开设计文档目录；不加入第二语言对。

## 2. 前置条件

- [x] 工作分支：从已合并并同步的 `develop` 创建 `feature/infra-python-skeleton`；CM-CORE-001 已合入远程 `develop`。
- [x] 环境就绪项：已在 `/home/xtc/env/codemigrator-infra/` 隔离环境复用 uv 0.12.7；Docker Compose 已可用，镜像构建/PG 冒烟按本机能力执行。
- [x] 上游依赖：CM-CORE-001 已提交，core 公共模型、错误码和资源已冻结。
- [x] 设计文档：本详细设计保存于 `my_space/codemigrator_design_doc/detailed_coding_design/infra/`。

## 3. 实施步骤

1. - [x] 先编写工程树、pyproject、入口、子包 README、描述符、Compose、迁移、部署和 CI 的失败验收测试（`tests/infra/`、`tests/contracts/` → V-M01-V4-001/004/006/007/011、V5 增量）。
2. - [x] 在 `/home/xtc/env/codemigrator-infra/` 安装/复用 uv，并生成单包 `pyproject.toml` 与 `uv.lock`（→ D-02、D-03、uuid-utils 联动、V-M01-V4-011）。
3. - [x] 建立 `src/codemigrator/` 恰 8 个子包、四项 README 骨架和唯一 app entry point stub（→ V-M01-V4-001/007/011）。
4. - [x] 编写 import-linter 的 layer/forbidden/independence 三类契约和 CI 静态审查步骤（→ V-M01-V4-001/002/007/008）。
5. - [x] 生成 Go→Python 描述符、grammar 载体摘要与 target-python Dockerfile/构建清单，确保资源只承载声明事实（→ V-M01-V4-004/006、D-01、D-06）。
6. - [x] 添加 PostgreSQL 17 的 app+PG Compose 健康检查、最小版本化 SQL schema 和 app Dockerfile（→ V5 增量、D-04、D-05）。
7. - [x] 添加 `tests/contracts`、`tests/recovery`、`tests/security` 与各子包测试目录，补根 README 与工程使用说明（→ D-07、D-08）。
8. - [x] 运行 uv lock/install、pytest、import-linter、ruff、mypy、compileall、docker compose config；记录无法运行的镜像/服务验证项及原因（→ §4 验证矩阵）。
9. - [ ] 更新迭代记录、主任务表 §6/§7.3/§11，执行完成前复核分支范围并提交、推送本任务分支。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| 工程契约测试 | `uv run pytest tests/infra tests/contracts -q` | 基线结构、依赖、资源、Compose、迁移检查全通过 | V-M01-V4-001/004/006/007/008/011、V5 |
| 全量规则测试 | `uv run pytest -q` | 已存在 core 测试与本任务测试全部通过 | CM-CORE 回归、M-01 |
| import-linter | `uv run lint-imports` | 三类契约无违例 | V-M01-V4-001/007 |
| 静态质量 | `uv run ruff check .`、`uv run mypy src` | 零错误；若基线依赖导致非阻断提示，记录原因 | D-03、技术栈映射 |
| 编译 | `uv run python -m compileall -q src` | 零错误 | 工程基线 |
| Compose | `docker compose config`；可用时 `docker compose up --build -d` 健康检查 | 仅 app+PostgreSQL 17，app/PG healthcheck 正常 | V5 增量、D-05 |
| 依赖锁定 | `uv lock --check` | lock 与 pyproject 一致 | D-02、uv 纪律 |

- Docker/PG 验证在本机 WSL2 执行；Compose 密码通过进程环境或调用方提供的有效 `--env-file` 注入，仓库不携带密码；未执行项必须在迭代记录中明确写出，不以静态检查冒充运行冒烟。

## 5. 风险与回滚

- 风险点及缓解：grammar shared library 可能需编译环境，优先使用 Go grammar wheel 的真实制品并计算摘要；工具链镜像构建可能受 Docker 网络限制，构建失败则保留可复核 Dockerfile/manifest 并明确未完成；uv 锁定依赖与 core 已安装的临时环境可能不同，使用 uv 结果作为工程事实；不把凭据写入 pyproject、Compose、CI 或描述符。
- 回滚方式：只在 `feature/infra-python-skeleton` 使用 `git revert` 回滚本任务提交；不重置 `develop` 或 `feature/core-contracts`，不删除用户已有工作树变更。

## 6. 收尾清单（对应 AGENTS.md §4 四件套）

- [x] 模块迭代记录已按模板生成于 `my_space/codemigrator_dev_progress/infra/`
- [x] 主任务表 `CodeMigrator开发任务规划与进度跟踪.md` 已更新
- [x] 详细设计文档已保存于 `my_space/codemigrator_design_doc/detailed_coding_design/infra/`
- [x] 本实施计划已保存于 `my_space/Implementation_plan_doc/infra/`
- [ ] 若设计与架构文档有差异：已按确认流程完成回写；当前仅登记 D-07 偏差，不直接修改架构文档
