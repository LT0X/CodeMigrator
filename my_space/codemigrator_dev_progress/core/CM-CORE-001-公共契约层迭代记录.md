# CM-CORE-001-公共契约层迭代记录

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-CORE-001
- **关联模块/文档**：M-00；`src/codemigrator/core/`；`my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_垂类设计原则与架构哲学.md`

## 1. 变更动机

CodeMigrator V6 代码实现从 Wave 0 启动，需要先建立所有下游子包共享的公共契约唯一 owner。此次任务依据 CM-CORE-001 对齐记录和 M-00 公共契约章节，锁定身份、枚举、稳定错误码、严格模型、Phase policy 和纯函数边界。

## 2. 变更内容

- 已创建 `src/codemigrator/core/`，按身份、枚举、错误码、路径/规范化、资源策略和模型域拆分。
- 已提供 UUID v7 NewType、0～2 generation、BranchPrefix/RepoRelativePath 纯函数、集成键和 RFC 8785 canonical JSON。
- 已提供 M-00 公共模型、V6 Advice/RepairDecision/GlobalRepairSession、Context/SessionKind/RepairEvidence 联动模型。
- 已提供 `core://phase-tool-policy/v2`、`core://verification-policy/v1`、`core://session-budget/v1`、`core://session-templates/v1` 资源及摘要读取器。
- 关键决策依据：CM-CORE D-01～D-07；CM-CORE 2026-08-29 追加的 StableErrorCode、Wave 2/3 资源和结构性预算联动记录；M-00 Phase policy 与公共契约代码块。

## 3. 自测与验证结果

- `PYTHONPATH=src:/tmp/codemigrator-core-venv/lib/python3.12/site-packages python3 -m pytest tests/core tests/contracts -q`：49 passed。
- `PYTHONPATH=src:/tmp/codemigrator-core-venv/lib/python3.12/site-packages python3 -m compileall -q src/codemigrator/core`：通过。
- `rg -n "runtime|os\.environ|asyncio\.create_task|threading" src/codemigrator/core`：无命中。
- 条款已覆盖：V6 契约新增、V5 四件工件、V-M00-V4-002、V-M00-V4-004、V-M00-V4-008、V-M00-V4-017、BranchPrefix、Phase policy exact-match、判别联合纪律。
- import-linter/CI 未执行：工程基线与 CI 归 CM-INFRA-001；本任务以 compileall、pytest 和静态扫描替代验证。

## 4. 影响面与风险

- core 将成为全部下游模块的公共类型依赖，错误码或枚举值漂移会影响 API、运行时、验证、工具网关和前端投影。
- 当前架构文档的旧预算字段和 SessionKind 列表与最新对齐记录存在时间差；实施以最新对齐记录为准，并在详细设计中登记差异。
- UUID v7、canonical JSON 和资源摘要验证依赖工程基线登记；不得用未对齐的替代实现掩盖依赖缺失。

## 5. 后续行动

- [x] 先为基础类型、枚举/错误码、模型和资源加载编写失败测试。
- [x] 实现 core 并运行 core/contracts 测试。
- [x] 补齐四类收口文档和主任务表证据。
- [ ] 进入 CM-INFRA-001：登记 `uuid-utils`/RFC 8785 依赖并建立工程基线。
