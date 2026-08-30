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

- 可复现基线命令 `PYTHONPATH=src /tmp/codemigrator-infra/.venv/bin/python -m pytest tests/core tests/contracts -q`：最终 55 passed（历史首轮 49 passed、第一轮审查修订后 53 passed）。
- `PYTHONPATH=src /tmp/codemigrator-infra/.venv/bin/python -m compileall -q src tests`：通过。
- `rg -n "runtime|os\.environ|asyncio\.create_task|threading" src/codemigrator/core`：命中 `runtime_image_digest` 描述符数据字段；经人工复核不是运行时依赖，其余无越界依赖命中。
- 条款已覆盖：V6 契约新增、V5 四件工件、V-M00-V4-002、V-M00-V4-004、V-M00-V4-008、V-M00-V4-017、BranchPrefix、Phase policy exact-match、判别联合纪律。
- import-linter/CI 未执行：工程基线与 CI 归 CM-INFRA-001；本任务以 compileall、pytest 和静态扫描替代验证。

## 3.1 独立审查修订

- 两份独立审查均指出并核实了 CreateRun 分支前缀、RepoRelativePath 模型边界、验证 subject/OID 一致性、公共 facade 导出和非 advisory 空 anchors 问题；已补失败测试并修复。
- 第二轮审查新增问题已核实并修复：资源摘要改为 `SHA-256(JCS(payload))`；`ToolchainDescriptor.descriptor_version` 通过 serializer 稳定输出 semver 字符串；中心错误码补齐 `CONTEXT_BUDGET_EXCEEDED`、`CONTEXT_CAPABILITY_INVALID`、`RECOVERY_LEDGER_INCONSISTENT`。
- 资源测试不再声称存在独立 manifest 文件：`session-templates/v1.json` 对象本身承载十槽位 manifest，摘要针对解析后的 canonical payload。
- 工程依赖登记仍由 `CM-INFRA-001` 负责；本任务未在 core 分支新增第二套 `pyproject.toml` 或锁文件。
- 后续独立审查发现并修复：`PlanEdge` 默认 alias 序列化、semver 字符串输入/JSON Schema、路径列表 malformed container 的 Pydantic 错误语义、`validate_candidate_generation` facade 导出，以及 `MODEL_BINDING_INVALID`、`PHASE_STATUS_MISMATCH`、`CANDIDATE_REF_CONFLICT`、`REMOTE_REF_MOVED`、`DEPENDENCY_UNAVAILABLE` 五个架构引用码。
- 文档收口补充：`SourceToolchain.runtime_image_digest` 是 CM-VERIFY-001 D-06 已确认的 parity 源端运行时镜像可选字段，属于跨任务契约扩展；M-00/M-01 正文尚未回填，后续由 CM-VERIFY-001 实施联动负责同步与验收，本任务保留字段并登记该差异。
- Pydantic 兼容性修订：PlanEdge 不依赖 Pydantic 2.11 的 `serialize_by_alias` 配置，改用 v2 通用 model serializer；因此不需在 core 任务额外设定 Pydantic 最低版本，JSON alias 由回归测试锁定。

## 4. 影响面与风险

- core 将成为全部下游模块的公共类型依赖，错误码或枚举值漂移会影响 API、运行时、验证、工具网关和前端投影。
- 当前架构文档的旧预算字段和 SessionKind 列表与最新对齐记录存在时间差；实施以最新对齐记录为准，并在详细设计中登记差异。
- UUID v7、canonical JSON 和资源摘要验证依赖工程基线登记；不得用未对齐的替代实现掩盖依赖缺失。

## 5. 后续行动

- [x] 先为基础类型、枚举/错误码、模型和资源加载编写失败测试。
- [x] 实现 core 并运行 core/contracts 测试。
- [x] 补齐四类收口文档和主任务表证据。
- [ ] 进入 CM-INFRA-001：登记 `uuid-utils`/RFC 8785 依赖并建立工程基线。
