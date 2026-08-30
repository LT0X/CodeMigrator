# CM-CORE-001 公共契约层实施计划

> **For agentic workers:** 本计划按任务步骤执行；每个步骤均以 pytest 契约测试或资源校验作为反馈点。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-CORE-001
- **所属模块/crate**：M-00 / `codemigrator.core`
- **关联交付物**：详细设计 `my_space/codemigrator_design_doc/detailed_coding_design/core/CM-CORE-001-公共契约层详细设计.md`、迭代记录 `my_space/codemigrator_dev_progress/core/CM-CORE-001-公共契约层迭代记录.md`

## 1. 目标与范围

**目标：** 交付 `codemigrator.core` 作为 CodeMigrator V6 公共契约唯一 owner，提供所有下游子包复用的身份、枚举、错误码、模型、纯函数和静态策略资源。

**完成定义（DoD）：** M-00/CM-CORE-001 对齐条款具备源码与测试证据；core/contracts 测试全通过；源码可编译；静态类型与 lint 检查通过；四件收口文档按模板同步；PR 经独立审查通过并合入 `develop`。

**范围排除：** 不实现状态机转移、actor、事务、API 事件、Planner 校验器、工具网关、沙箱、Git、验证归因、模型调用或工程依赖基线；依赖登记由 `CM-INFRA-001` 完成。

**架构：** 按身份/枚举/错误码/资源策略/模型域拆分模块，由 `__init__.py` 提供唯一公共导出入口。所有 Pydantic 模型继承统一的严格基类；公共契约只依赖 Python 标准库、Pydantic v2、`uuid-utils` 和 `semver`，不读取环境、不连接外部服务、不创建后台任务。

**技术栈：** Python 3.12、Pydantic v2、`uuid-utils` UUID v7、`semver`、`pytest`、`importlib.resources`、RFC 8785 canonical JSON 序列化适配。

**依据：** `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_垂类设计原则与架构哲学.md` 的公共契约、Phase 工具授权和 V6 收敛章节；`my_space/code_alignment_record/core/CM-CORE-001-对齐记录.md` 全文；`CM-PLAN`、`CM-VERIFY`、`CM-MEMORY`、`CM-WORKSPACE`、`CM-SUPERVISOR`、`CM-REPAIR` 对 core 的联动条款。

**跨任务差异：** `SourceToolchain.runtime_image_digest` 按 `CM-VERIFY-001` D-06 的 parity 源端运行时镜像决策纳入 core；M-00/M-01 架构正文尚未回填，后续由 CM-VERIFY-001 实施联动负责同步和行为验收，本计划不删除该字段。

## 2. 前置条件

- [x] 工作分支：从已提交 `develop` 切出 `feature/core-contracts`。
- [x] 设计文档：已保存 CM-CORE-001 详细设计并登记架构差异。
- [x] 对齐记录：已完整阅读 `my_space/code_alignment_record/core/CM-CORE-001-对齐记录.md`。
- [x] 隔离测试环境：已具备 Python 3.12、Pydantic v2、`uuid-utils`、`semver`、RFC 8785 适配和 pytest。
- [ ] 工程依赖登记：由后续 `CM-INFRA-001` 完成，不在本任务重复建立 `pyproject.toml`/`uv.lock`。

### 2.1 全局约束

- Python 最低版本为 3.12；`src/codemigrator/core/` 不读环境变量、不访问数据库/文件系统（静态包资源读取除外）、不创建后台任务或线程。
- 公共状态机、枚举、错误码和 Phase 工具策略只在 core 定义；下游只能导入，不复制第二套。
- 所有 Pydantic 模型使用 `ConfigDict(extra="forbid")`；判别联合使用 discriminator，判别字段之外拒绝未知字段。
- UUID 标识为 UUID v7 NewType；JSON 使用小写连字符 UUID，SQL 映射由下游持有 UUID 类型。
- `CandidateGeneration` 只允许 0、1、2；物理重派不增加 generation。
- `RepoRelativePath` 规范化后使用正斜杠、去重，并按 UTF-8 原始字节排序；`.git`、遍历、绝对路径由 core 的路径纯函数拒绝。
- `VERIFY` 和 `REPORT` 的模型工具集合为空；`PLAN` 和 `EXECUTE` 的集合必须与 M-00 exact-match。
- `StableErrorCode` 与 `FailureReason` 是两个独立枚举族；`DOSSIER_INCONSISTENT` 只属于 `FailureReason`。

## 3. 实施步骤

### 3.1 文件结构

- `src/codemigrator/core/__init__.py`：唯一公共导出门面，不持有第二套契约定义。
- `src/codemigrator/core/_base.py`：统一 Pydantic 配置和通用模型行为。
- `src/codemigrator/core/ids.py`：所有 UUID v7 NewType、ID 生成器和 `CandidateGeneration`/`BranchPrefix`/路径别名。
- `src/codemigrator/core/enums.py`：M-00 全部枚举以及已在联动记录中收口的 `SessionKind`、验证可靠性枚举。
- `src/codemigrator/core/errors.py`：集中 `StableErrorCode`，不放 `FailureReason` 的重复定义。
- `src/codemigrator/core/paths.py`：BranchPrefix、RepoRelativePath、generation、集成键和 canonical JSON 纯函数。
- `src/codemigrator/core/policy.py`：通过 `importlib.resources` 加载并校验四类静态资源；只提供数据读取，不执行策略。
- `src/codemigrator/core/models/`：按 run/slice/plan/descriptor/verification/context/advice 域拆分模型。
- `src/codemigrator/core/resources/`：`phase-tool-policy/v2`、`verification-policy/v1`、`session-budget/v1` 和 `session-templates/v1` 四类版本化 JSON 资源；其中模板资源对象本身是十槽位 manifest。
- `tests/core/`：core 单测；`tests/contracts/`：跨包公共契约 exact-match 和资源地址测试。

### 3.2 任务 1：基础类型和 UUID 身份

**文件：**

- 新建：`src/codemigrator/core/_base.py`、`src/codemigrator/core/ids.py`、`src/codemigrator/core/paths.py`
- 测试：`tests/core/test_ids.py`、`tests/core/test_paths.py`

**接口：**

- 产出所有 ID NewType：`RunId`、`SpecId`、`SliceId`、`TaskId`、`CheckId`、`ReceiptId`、`RequestId`、`DispatchAttemptId`、`SessionId`、`MessageId`、`QuestionId`、`TaskDraftRevisionId`、`CorrectionIntentId`、`PlanRevisionId`、`ProjectId`、`ProjectSnapshotId`、`OutputWorkspaceId`、`ProjectModuleId`、`RepairDecisionId`、`AdviceId`。
- 产出 `new_uuid7()` 与按 ID 类型命名的生成器；底层唯一调用 `uuid_utils.uuid7()`，不实现第二个 UUID 算法。
- 产出 `validate_candidate_generation(value) -> int`、`validate_branch_prefix(value) -> str`、`normalize_repo_relative_paths(values) -> list[str]`、`integration_key(integration_rank, slice_id) -> tuple[int, bytes]`、`canonical_json_bytes(value) -> bytes`。

- [x] 编写合法 UUID、UUID JSON round-trip 和 ID 生成单测。
- [x] 编写 generation `-1/3` 拒绝、`0/1/2` 接受以及 branch prefix 1/32/33 字节、空段、`.`、`..`、`.git` 用例。
- [x] 编写相对路径绝对路径、反斜杠、NUL、遍历、`.git`、重复路径和 UTF-8 字节排序用例。
- [x] 运行首轮红灯测试后实现最小纯函数，并重新运行测试通过。

### 3.3 任务 2：枚举族和稳定错误码

**文件：**

- 新建：`src/codemigrator/core/enums.py`、`src/codemigrator/core/errors.py`
- 测试：`tests/core/test_enums.py`、`tests/core/test_errors.py`

**接口：**

- 实现 M-00 的 `MigrationSessionStatus`、`InteractionStatus`、`CorrectionIntentStatus`、`SliceKind`、`ArtifactKind`、`DossierBudgetTier`、`SliceAttemptStatus`、`PlanEdgeKind`、`RunStatus`、`FailureReason`、`DeliveryChannelStatus`、`ModelProfile`、`Phase`、`ResidentRole`、`AdviceKind`、`ModuleBoundaryStrategy`、`CheckAction`、`DiagnosticSeverity`、`CheckStatus`。
- 实现跨任务收口的 `SessionKind` 九值：`ANALYZE_AUXILIARY`、`PLAN_AUXILIARY`、`CONTRACT`、`IMPLEMENTATION`、`TEST_TRANSLATION`、`TEST_GENERATION`、`EXPLORE_COORDINATOR`、`EXECUTE_SUPERVISOR`、`REPAIR_SESSION`；`Drafting` 作为模板资源的第十槽位，不伪造为 Run 内 SessionKind。
- 实现验证可靠性三值 `RELIABLE`、`UNCERTAIN`、`DYNAMIC`。
- `StableErrorCode` 集中包含 M-00/M-02/M-05/M-07/M-10/M-11/M-12/M-14 已登记的稳定码，包括工具/路径/读写/查询/Shell/Exec、Spec/descriptor、PLAN 八码、验证集合四码、上下文能力码、模型绑定/阶段一致性/候选与远端 ref/依赖能力码、恢复账本码及 API 幂等码；不把 `FailureReason` 成员并入其中。

- [x] 以设计文档和所有已对齐记录中的值表建立 exact-match 期望集合。
- [x] 编写每个枚举的名称、值、字符串 JSON 序列化和错误族隔离测试。
- [x] 运行首轮红灯测试后实现枚举，确认没有重复值或大小写“美化”。
- [x] 重新运行枚举/错误码测试通过。

### 3.4 任务 3：严格模型和判别联合

**文件：**

- 新建：`src/codemigrator/core/models/common.py`、`run.py`、`slice.py`、`plan.py`、`descriptor.py`、`verification.py`、`context.py`、`advice.py`、`src/codemigrator/core/models/__init__.py`
- 修改：`src/codemigrator/core/_base.py`、`src/codemigrator/core/__init__.py`
- 测试：`tests/core/test_models.py`、`tests/contracts/test_core_contracts.py`

**接口：**

- 实现 M-00 代码块中的 `ArtifactRef`、理解档案/规则手册/蓝图、`PlanProposal`/`PlanValidation`、`WriteScope`、`MigrationSlice`、`PlanEdge`、候选/dispatch/ref、`FrozenArtifactBundle`、`CreateRun`、注册项目、描述符、检查命令、诊断、`VerificationSubject`、`VerificationOutcome`、`DerivedVerificationGuard`、`IntegrationIntent`。
- 实现 V6 公共模型 `Advice`、`RepairDecision`、`GlobalRepairSession`；`Advice.payload` 保持 M-00 的受控 `dict` 形状，RouteSuggestion 具体字段由已对齐的 Supervisor 消费方在后续归一器中实现。
- 实现 `SliceGenerationRef`、`ContextPackIdentity`、结构性 `SessionBudgetProfile`（`session/max_rounds/eviction_watermark_pct`）、`ContextPack` 和 `RepairEvidence` 的公共事实字段。
- `PlanEdge.from_` 使用 `Field(alias="from")` 与 `populate_by_name`；`DiagnosticTarget` 和 `VerificationSubject` 使用 discriminator；所有模型的未知字段都拒绝。
- 在 `CreateRun`、写作用域、验证 subject/file line、descriptor 排除路径、contract artifact 和 dossier entry 等模型边界复用 core 语义校验；`tested_commit_oid` 必须与其 subject OID 一致，空 anchors 只有 advisory 条目可接受。`ToolchainDescriptor.descriptor_version` 保持 `semver.Version` 类型，并以字符串形式 JSON 序列化。

- [x] 为公共模型编写最小合法构造、闭合字段和 JSON round-trip 测试。
- [x] 为所有已知 discriminator variant 编写正确分派、未知 variant、variant 外未知字段拒绝测试。
- [x] 为 `PlanEdge` 编写 JSON `from` 别名和 Python `from_` 入口测试。
- [x] 为 `CreateRun` 双 source variant、`Advice` proposal hash 字段、`GlobalRepairSession` 联合 scope 和 `SessionBudgetProfile` 字段纪律编写契约测试。
- [x] 运行首轮红灯测试后完成实现，模型/契约测试通过。

### 3.5 任务 4：静态策略资源和公共导出

**文件：**

- 新建：`src/codemigrator/core/policy.py`、`src/codemigrator/core/resources/phase-tool-policy/v2.json`、`verification-policy/v1.json`、`session-budget/v1.json`、`session-templates/v1.json`
- 修改：`src/codemigrator/core/__init__.py`
- 测试：`tests/core/test_policy.py`、`tests/contracts/test_policy_resources.py`

**接口：**

- `load_phase_tool_policy() -> dict[str, list[str]]` 读取 `core://phase-tool-policy/v2`，并 exact-match：PLAN 三工具、EXECUTE 六工具、VERIFY 空集合、REPORT 空集合。
- `load_verification_policy()`、`load_session_budget()`、`load_session_templates()` 读取各自版本化 JSON；资源内容只承载已对齐的事实，使用方在 Run 创建时冻结，运行期不重新解释或修改。
- 所有公共类型和纯函数从 `codemigrator.core` 导出；子模块仍可被内部测试直接导入，但下游文档使用统一门面。

- [x] 编写 URI 可寻址、JSON UTF-8、资源版本、SHA-256(JCS(payload)) 和 missing resource 拒绝测试。
- [x] 编写 Phase policy exact-match 测试，确认 VERIFY/REPORT 为空列表而不是缺键。
- [x] 编写十槽位会话模板 manifest 和十档预算 profile 的覆盖测试。
- [x] 编写资源 JCS 摘要、描述符 semver JSON round-trip/Schema 和默认 `PlanEdge` alias 输出测试。
- [x] 运行资源测试通过。

### 3.6 任务 5：集成验证和收尾文档

**文件：**

- 修改：`src/codemigrator/core/__init__.py`（仅修正公共导出）
- 测试：`tests/core/`、`tests/contracts/`
- 交付：`my_space/codemigrator_dev_progress/core/CM-CORE-001-公共契约层迭代记录.md`、本计划、`my_space/codemigrator_design_doc/detailed_coding_design/core/CM-CORE-001-公共契约层详细设计.md`、主任务表

- [x] 运行 `PYTHONPATH=src /tmp/codemigrator-infra/.venv/bin/python -m pytest tests/core tests/contracts -q`，初始实现 49 passed，第一轮审查修订后 53 passed，第二轮审查修订后 55 passed。
- [x] 运行 import 编译检查：`PYTHONPATH=src /tmp/codemigrator-infra/.venv/bin/python -m compileall -q src tests`。
- [x] 做静态审查：core 不导入 `runtime`、不读取环境变量、不创建线程/任务、不定义 API 事件常量；`runtime_image_digest` 命中仅为描述符数据字段，已人工复核不属于运行时依赖。
- [x] 处理独立审查反馈：CreateRun 分支前缀、RepoRelativePath 模型边界、subject/OID 一致性、公共导出、DossierEntry 锚点规则、JCS 资源摘要、semver JSON round-trip/Schema、PlanEdge 默认 alias、模型边界异常语义和跨模块稳定错误码。
- [x] 按 core 对齐记录逐条登记验收证据；import-linter/CI 未执行并说明归属 CM-INFRA-001。
- [x] 更新模块迭代记录、主任务表状态/日期/证据指针和 §6/§11 统计。
- [ ] 核对工作树仅包含本任务范围，完成后按用户授权提交分支并记录提交证据。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| Core 单测 | `PYTHONPATH=src pytest tests/core -q` | 全部通过 | V6 契约、generation、路径、资源 |
| 跨包契约 | `PYTHONPATH=src pytest tests/contracts -q` | 全部通过 | 公共 owner、判别联合、Phase exact-match |
| 编译 | `PYTHONPATH=src python -m compileall -q src/codemigrator/core` | 零错误 | 工程基线 |
| 依赖静态审查 | `rg -n "runtime|os\.environ|asyncio\.create_task|threading" src/codemigrator/core` | 无越界命中（允许文档字符串中的下游名称需人工复核） | V-M00-V4-017 |
| 资源摘要 | `PYTHONPATH=src pytest tests/core/test_policy.py tests/contracts/test_policy_resources.py -q` | URI、内容和 canonical payload SHA-256 摘要一致；模板对象保留十槽位 manifest | D-04、CM-VERIFY D-04、CM-WORKSPACE D-02、CM-MEMORY D-01 |

## 5. 风险与回滚

- `uuid-utils`、RFC 8785 库尚未由当前工程基线安装时，先在隔离 Python 环境补齐依赖；不以第二套自实现静默替代已确认依赖。
- M-00 旧代码块与 Wave 2/3 最新对齐记录存在 `RepairSession`、结构性预算和新资源的时间差；实现以最新对齐记录为准，并在详细设计的差异节登记。
- Blueprint、RouteSuggestion、RepairBrief 等仍由下游任务消费的未定业务字段保持设计指定的 `dict`/`ArtifactRef` 边界，不在 core 猜测扩展。
- 回滚仅使用本任务分支上的可审阅提交或 `git revert`；不重置用户的 `develop` 工作树。

## 6. 收尾清单（对应 AGENTS.md §4 四件套）

- [x] core 模块迭代记录按模板生成。
- [x] 主任务表 CM-CORE-001 已更新为已完成并带当前验证证据；PR 合并为最后交付动作。
- [x] core 详细设计文档保存于 `my_space/codemigrator_design_doc/detailed_coding_design/core/`。
- [x] 本实施计划保存于 `my_space/Implementation_plan_doc/core/`。
- [x] 架构文档差异已登记；未直接修改架构模块设计文档。
- [ ] 按已完成审查结论直接合并 PR #1，并在主工作区执行 `git fetch origin` 与 `git pull --ff-only origin develop`；不再追加审查轮次。
