# CM-SPEC-001-Migration-Spec能力门实施计划

> 本计划依据 M-05《Migration Spec 抽象层》与 `my_space/code_alignment_record/spec/CM-SPEC-001-对齐记录.md` 编制。任务只交付 Spec v3 的纯校验/规范化契约、registry/持久化端口和迁移 DDL，不提前实现 runtime 文件 I/O、repository SQL 或完整 API 路由。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-SPEC-001
- **所属模块/crate**：M-05 / `codemigrator-core` + `codemigrator-api` 入口契约
- **关联交付物**：详细设计 `my_space/codemigrator_design_doc/detailed_coding_design/spec/CM-SPEC-001-Migration-Spec能力门详细设计.md`、迭代记录 `my_space/codemigrator_dev_progress/spec/CM-SPEC-001-Migration-Spec能力门迭代记录.md`

## 1. 目标与范围

- 完成定义（DoD）：四道门按固定顺序短路；字节/JSON/Schema/范围/检查集/资源锁规则可确定性测试；JCS canonical hash 与 insert-or-get 端口稳定；registry Protocol、问题响应、Spec v3 类型和 `migration_specs` DDL 可被下游消费；全量测试、import-linter、Ruff、mypy、compileall 通过。
- 范围排除项：不实现 descriptor registry 文件扫描、grammar 加载、镜像验证、repository SQL、完整 `POST /api/v1/specs` 路由、CreateRun 编排、M-06 快照扫描和 artifact 识别；不新增稳定错误码（CM-CORE 已完成码族）。

## 2. 前置条件

- [x] 工作分支：从已合并的 `develop` 切出 `feature/spec-capability-gate`；CM-INFRA-001 已合入 `d482d88`。
- [x] 环境就绪项：复用 `/home/xtc/env/codemigrator-infra/` 的 Python 3.12/uv 环境与锁定依赖。
- [x] 上游依赖：CM-CORE-001 与 CM-INFRA-001 已合入；公共类型、错误码、JCS 依赖和 import-linter 基线可用。
- [x] 设计文档：已完整阅读 M-05 架构设计与 CM-SPEC-001 对齐记录；本任务详细设计随本计划建立。

## 3. 实施步骤

1. - [x] 更新主表：标记 CM-INFRA-001 已完成并登记 CM-SPEC-001 开工（主表 §6/§7.3/§11）。
2. - [x] 先编写四道门、问题响应、范围匹配、canonical/hash、registry stub 与 DDL 失败测试（`tests/spec/`、`tests/contracts/` → V-M05-V4-001~012）。
3. - [x] 实现闭合 Spec v3 Pydantic 模型、范围纯函数与四道门短路编排（`src/codemigrator/core/models/spec.py`、`src/codemigrator/core/spec.py`）。
4. - [x] 实现 JCS canonical 规范化、摘要和内存 insert-or-get 端口替身（`src/codemigrator/core/spec.py`、`tests/contracts/`）。
5. - [x] 实现 `DescriptorRegistry`/`SpecRepository` Protocol 与资源门 stub，不触碰文件 I/O/SQL（`src/codemigrator/core/ports.py`）。
6. - [x] 添加 `migration_specs` 纯 SQL 迁移及迁移资源契约测试（`migrations/0002_migration_specs.sql`、`tests/spec/`）。
7. - [x] 更新 core facade、模块详细设计、迭代记录、主任务表和 PR 说明，逐条核对 V-M05 条款并复核分支只含本任务及其必要进度同步。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| Spec 契约测试 | `uv run pytest tests/spec tests/contracts -q` | 门、短路、模式、规范化、端口和 DDL 全部通过 | V-M05-V4-001~012 |
| 全量规则测试 | `uv run pytest -q` | CM-CORE 回归与 CM-SPEC 全部通过 | M-00/M-05 |
| import-linter | `uv run lint-imports` | 3 contracts kept，0 broken | M-01 |
| 静态质量 | `uv run ruff check .`、`uv run mypy src` | 零错误 | 工程质量门 |
| 编译 | `uv run python -m compileall -q src tests` | 零错误 | 工程基线 |
| 差异检查 | `git diff --check`、分支文件清单审查 | 无空白错误、无越界文件/凭据 | AGENTS.md §2/§4 |

## 5. 风险与回滚

- 风险点及缓解：Schema 与范围模式约束容易被宽松库语义放大，匹配器只实现 M-05 有限规则并用反例锁定；registry 文件/镜像事实未在本任务臆造，以 Protocol + stub 测试交接；DDL 与 runtime repository 分离，避免重复持久化真相。
- 回滚方式：只在 `feature/spec-capability-gate` 使用 `git revert`；不重置 `develop`、不覆盖主工作区用户修改。

## 6. 收尾清单（对应 AGENTS.md §4 四件套）

- [x] 模块迭代记录已按模板生成于 `my_space/codemigrator_dev_progress/spec/`
- [x] 主任务表 `CodeMigrator开发任务规划与进度跟踪.md` 已更新
- [x] 详细设计文档已保存于 `my_space/codemigrator_design_doc/detailed_coding_design/spec/`
- [x] 本实施计划已保存于 `my_space/Implementation_plan_doc/spec/`
- [x] 若设计与对齐结果有差异：已按确认结论同步架构文档；当前不新增架构偏差
