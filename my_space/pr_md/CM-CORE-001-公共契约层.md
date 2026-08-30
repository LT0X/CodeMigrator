# feat(core): add core contracts

## 1. 标题与关联

- PR 标题：`feat(core): add core contracts`
- 任务编号：`CM-CORE-001`
- 任务分支：`feature/core-contracts`
- 模块迭代记录：`my_space/codemigrator_dev_progress/core/CM-CORE-001-公共契约层迭代记录.md`

## 2. 背景

实现 M-00 公共契约层，为后续 CodeMigrator 子包提供唯一的 ID、状态机、枚举、稳定错误码、Pydantic 类型族、路径规则、规范化函数和内置策略资源；覆盖 CM-CORE-001 对齐记录及对应 V6/V4 验收条款。

## 3. 变更点

- `src/codemigrator/core/`：新增公共 ID、枚举、错误码、模型、路径工具和版本化策略资源。
- `tests/core/`、`tests/contracts/`：新增公共契约、校验、序列化和资源完整性测试。
- `my_space/`：新增任务实施计划、详细设计和迭代记录。
- BREAKING：无；本 PR 建立初始公共契约，未改变已有运行时接口。
- 对 M-00 公共契约的影响：`codemigrator.core` 成为稳定契约唯一 owner，下游模块只应引用该包。

## 4. 自测证据

- [x] `PYTHONPATH=src python -m pytest -q tests/core tests/contracts`：审查修订后 55 passed。
- [x] `PYTHONPATH=src python -m compileall -q src tests`：通过。
- [x] `git diff --check`：通过。
- [x] 核对 `feature/core-contracts` 相对 `develop` 的差异，仅包含 CM-CORE-001 范围及其交付文档。
- [x] 未执行真实模型测试：本任务只包含确定性公共契约与资源校验，不需要 provider/会话行为验证。

审查修订补充：

- 在 CreateRun、路径、验证 subject/file line、descriptor 排除路径、contract artifact 和 dossier entry 模型边界落实语义校验，并补公共 facade 导出。
- 资源摘要采用 `SHA-256(JCS(payload))`；`ToolchainDescriptor.descriptor_version` JSON 输出为 semver 字符串。
- 补齐 `CONTEXT_BUDGET_EXCEEDED`、`CONTEXT_CAPABILITY_INVALID`、`RECOVERY_LEDGER_INCONSISTENT` 三个跨模块稳定错误码。
- 进一步补齐 `MODEL_BINDING_INVALID`、`PHASE_STATUS_MISMATCH`、`CANDIDATE_REF_CONFLICT`、`REMOTE_REF_MOVED`、`DEPENDENCY_UNAVAILABLE`；修复 `PlanEdge` 默认 alias、semver JSON round-trip/Schema、路径列表错误语义，并导出 `validate_candidate_generation`。
- `uuid-utils`、RFC 8785 等工程依赖仍由 CM-INFRA-001 登记，本 PR 不重复建立工程基线。

## 5. 风险与回滚

- 风险：下游任务将依赖本 PR 中的字段、枚举和值；后续契约扩容必须遵循对齐记录和兼容性审查。
- 回滚：关闭或回滚本 PR 的提交即可，不涉及数据库迁移或外部服务状态。
