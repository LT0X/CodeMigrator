# CM-CORE-001 公共契约层详细设计

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-CORE-001
- **所属模块/crate**：M-00 / `codemigrator.core`
- **依据的架构文档**：`CodeMigrator_垂类设计原则与架构哲学.md` 的“公共契约：身份、代次和状态只有一个定义”“Run 状态只表达全局阶段”“Phase 工具授权与固定资源边界”章节；M-01 核心目录架构设计。
- **关联交付物**：`my_space/Implementation_plan_doc/core/CM-CORE-001-公共契约层实施计划.md`、`my_space/codemigrator_dev_progress/core/CM-CORE-001-公共契约层迭代记录.md`

## 1. 需求与边界

- **本任务做什么**：建立 Python 版 `codemigrator.core`，作为 V6 公共身份、枚举、错误码、Pydantic 模型、Phase policy 和纯函数的唯一 owner，并让下游可以通过稳定导出入口消费这些契约。
- **本任务不做什么**：不实现 Run 状态转移、actor、事务、API 事件词汇、Planner 四重校验、工具网关、沙箱、Git、验证归因或模型调用；不读取环境变量，不创建后台任务，不保存进程级运行状态。
- **覆盖的验收条款**：
  - V6 契约新增：`ResidentRole`、`AdviceKind`、`Advice`、`RepairDecision`、`GlobalRepairSession`、`FailureReason.DossierInconsistent`。
  - V5 契约面增量：`FrozenArtifactBundle`、`UnderstandingDossier`、`TargetProjectBlueprint`、`MigrationRulebook`。
  - `V-M00-V4-002` 的错误码与稳定值存在性；行为由 workspace/tool gateway 验收。
  - `V-M00-V4-004` 的 CandidateGeneration 0/1/2 边界。
  - `V-M00-V4-008` 的 ToolchainDescriptor/资源摘要类型支撑；行为由 API/Spec 验收。
  - `V-M00-V4-017` 的 core 静态依赖与无运行时扫描副作用纪律。
  - M-00 BranchPrefix 1～32 字节 ASCII 小写字母/数字/`-`/`/`、空段/`.`/`..`/`.git` 拒绝规则。
  - `core://phase-tool-policy/v2` 的 PLAN/EXECUTE/VERIFY/REPORT exact-match。
  - 判别联合 `DiagnosticTarget`、`VerificationSubject` 的 variant 分派和 `extra="forbid"`。

## 2. 契约引用

| 引用事实 | Owner 文档 | 实现形式 |
| --- | --- | --- |
| RunStatus、Phase、SliceAttemptStatus、错误码及状态语义 | M-00 | `codemigrator.core.enums` / `errors` 定义，其他包只导入 |
| ID 类型、UUID v7 与 generation 语义 | M-00 + CM-CORE D-01 | `ids.py` 中 `NewType` 与 `uuid-utils` 生成器 |
| `PlanEdge` JSON `from` 别名 | M-00 公共契约代码块 | Pydantic `Field(alias="from")` |
| Phase 工具矩阵 | M-00 + CM-CORE D-04 | 包内 JSON，通过 `importlib.resources` 加载 |
| `SessionKind`、结构性 SessionBudgetProfile | CM-MEMORY D-01/变更记录 | core 枚举、模型与 `session-budget/v1` 资源 |
| 验证集合错误码和验证策略资源 | CM-VERIFY D-04 | core 错误码与 `verification-policy/v1` 资源 |
| 静态模板资源地址 | CM-WORKSPACE D-02 / CM-LOOP D-03 | `session-templates/v1` 资源 manifest；正文由会话任务填充 |
| 事件名词汇 | M-02、CM-CORE D-05 | 不在 core 定义，保留给 `codemigrator.api` |

## 3. 详细设计

### 3.1 结构与职责

```text
codemigrator.core
├── __init__.py       # 唯一公共导出
├── _base.py          # CoreModel: extra=forbid
├── ids.py            # UUID NewType、primitive aliases、generation
├── paths.py          # 规范化和 canonical 纯函数
├── enums.py          # 所有公共枚举
├── errors.py         # StableErrorCode
├── policy.py         # 静态资源加载与摘要校验
├── models/
│   ├── common.py     # ArtifactRef、anchor、档案/规则基础类型
│   ├── run.py        # Run、CreateRun、refs、Advice/repair 关联的 run facts
│   ├── slice.py      # Slice、scope、candidate、dispatch
│   ├── plan.py       # PlanProposal、PlanValidation、PlanEdge
│   ├── descriptor.py # 双工具链、命令模板、工件类型
│   ├── verification.py # Diagnostic、Check、Subject、Outcome
│   ├── context.py    # SessionKind、ContextPack identity、预算档
│   └── advice.py     # Advice、RepairDecision、GlobalRepairSession
└── resources/
    ├── phase-tool-policy/v2.json
    ├── verification-policy/v1.json
    ├── session-budget/v1.json
    └── session-templates/v1.json
```

`CoreModel` 只设置公共 Pydantic 配置。模型字段依赖 core 中的 NewType/枚举，不在模型文件重新声明枚举或错误码。模型按域拆分仅改变文件组织，不改变 JSON 字段名和公共身份。

Primitive 字符串事实（`Sha256`、`GitOid`、`LanguageId`、`RepositoryUrl`、`GitRefName`、`RepoRelativePath`）使用单一类型别名；语义验证集中在 `paths.py` 或对应模型的 field validator，避免每个下游复制路径规则。对于 M-00 尚未定稿的高层 payload，保留文档指定的 `dict`/`list[dict]`，不加入推测字段。

### 3.2 关键机制

#### 身份和代次

`new_uuid7()` 只调用 `uuid_utils.uuid7()`；各业务 ID 是其返回 UUID 的静态 NewType。Pydantic 在 JSON 输出时使用 UUID 标准小写连字符表示。`validate_candidate_generation` 在独立函数和含该字段的模型入口都执行 0～2 检查。

#### 路径和集成键

BranchPrefix 先做 ASCII 与 UTF-8 字节长度检查，再按 `/` 分段，拒绝空段、`.`、`..` 和 `.git`。RepoRelativePath 先拒绝 NUL、反斜杠、绝对路径和遍历段，再使用正斜杠形式去重；排序使用每一项的 UTF-8 原始字节。内核 no-follow、根绑定和挂载边界属于 workspace，不在 core 重复实现。

集成键为 `(integration_rank, SliceId.bytes)`，排序只依赖冻结 rank 和 Slice UUID 字节，不依赖完成时间、拓扑层或候选生成时间。

#### canonical JSON

`canonical_json_bytes` 是公共底座：先将 Pydantic model 转为 JSON-compatible 数据，再调用 RFC 8785 适配器并返回 UTF-8 bytes。它不计算 verification fingerprint；fingerprint 的字段选择和派生归 Harness/verification 所有。若 canonical 库不可用，导入/调用显式失败，不静默退回不兼容的 `sort_keys` JSON。

#### Phase policy 和版本化资源

资源路径以 `importlib.resources.files("codemigrator.core.resources")` 为根，资源加载器只允许内置相对资源名，读取 bytes、计算 SHA-256、解析 JSON 并返回不可变副本。Phase policy 的内容必须是：

```json
{
  "PLAN": ["ReadFile", "QuerySourceAst", "Exec"],
  "EXECUTE": ["ReadFile", "WriteFile", "EditFile", "QuerySourceAst", "Shell", "Exec"],
  "VERIFY": [],
  "REPORT": []
}
```

`verification-policy/v1`、`session-budget/v1` 和 `session-templates/v1` 采用相同的版本化/摘要/只读加载机制。预算资源使用最新结构性轮数表：AnalyzeAuxiliary 30、PlanAuxiliary 50、Contract 300、Implementation 500、TestTranslation 300、TestGeneration 300、ExploreCoordinator 200、ExecuteSupervisor 30、RepairSession 500；水位分别按对齐记录的 75%/80% 值存储。`Drafting` 是模板资源第十槽位，不改九值 SessionKind。

### 3.3 数据与接口

对外公共接口集中从 `codemigrator.core` 导出：

- 身份：全部 ID NewType、`new_uuid7`、generation/path/canonical 纯函数。
- 枚举：M-00 全量枚举、`SessionKind` 和验证可靠性枚举。
- 模型：M-00 公共契约代码块列出的所有模型，以及 Context/RepairEvidence 联动模型。
- 资源：`load_phase_tool_policy`、`load_verification_policy`、`load_session_budget`、`load_session_templates`。

`PlanEdge` 使用 `from_` 作为 Python 属性、`from` 作为 JSON 属性；`CreateRunSource` 是 `RemoteRepository | RegisteredProject` 判别联合；`DiagnosticTarget` 的 discriminator 是 `kind`，`VerificationSubject` 的 discriminator 也是 `kind`。模型 JSON 输出不携带 Python-only 的别名字段。

core 不新增数据库表，不定义 REST/SSE DTO，不定义 run_events 事件名，不定义异常层次；这些接口分别由 runtime/api/下游 owner 实现。

### 3.4 错误处理

`StableErrorCode(str, Enum)` 集中承载稳定网关/能力/校验拒绝码，当前包括：

- 工具和路径：`TOOL_PHASE_DENIED`、`TOOL_NOT_FOUND`、`TOOL_SCHEMA_INVALID`、`PATH_DENIED`、`READ_OUT_OF_SCOPE`、`WRITE_SCOPE_VIOLATION`、`EDIT_TARGET_NOT_FOUND`、`EDIT_AMBIGUOUS`、`READ_LIMIT_EXCEEDED`、`WRITE_LIMIT_EXCEEDED`、`QUERY_TIMEOUT`、`TRUNCATED`、`PATH_OUTSIDE_SNAPSHOT`、`TEXT_FALLBACK_UNSUPPORTED`、`SHELL_TIMEOUT`、`SHELL_LIMIT_EXCEEDED`、`EXEC_TIMEOUT`、`EXEC_SCRIPT_ERROR`。
- 控制面/执行事实：`STALE_VERSION`、`IDEMPOTENCY_CONFLICT`、`STALE_DISPATCH_RESULT`、`CHECKPOINT_WRITE_FAILED`、`OUTPUT_LIMIT_EXCEEDED`、`SOURCE_FILE_TOO_LARGE`、`ANALYSIS_INFRA_ERROR`。
- Spec/描述符：`SPEC_TOO_LARGE`、`SPEC_JSON_INVALID`、`SPEC_DUPLICATE_KEY`、`SPEC_DEPTH_EXCEEDED`、`SPEC_SCHEMA_UNSUPPORTED`、`SPEC_SCHEMA_INVALID`、`CHECK_ACTION_UNSUPPORTED`、`CHECK_SET_INCOMPLETE`、`DESCRIPTOR_NOT_FOUND`、`DESCRIPTOR_DIGEST_MISMATCH`、`TOOLCHAIN_IMAGE_UNAVAILABLE`、`SPEC_IN_USE`。
- Planner：`PLAN_CYCLE`、`PLAN_SCOPE_CONFLICT`、`PLAN_BLUEPRINT_VIOLATION`、`PLAN_COVERAGE_INVALID`、`PLAN_SIZE_EXCEEDED`、`PLAN_EDGE_INVALID`、`PLAN_RANK_INCONSISTENT`、`PLAN_PROPOSAL_INVALID`。
- 验证集合：`CHECK_MISSING`、`CHECK_DUPLICATE`、`CHECK_UNEXPECTED`、`INVOCATION_HASH_MISMATCH`。

`FailureReason` 单独承载 `ANALYSIS_FAILED`、`DOSSIER_INCONSISTENT`、`PLAN_FAILED`、`EXECUTION_FAILED`、`VERIFICATION_TERMINAL`、`REPORT_GENERATION_FAILED`、`BUDGET_EXHAUSTED`、`RESOURCE_EXHAUSTED`、`OUTPUT_LIMIT_EXCEEDED`、`SLICE_REGENERATION_EXHAUSTED`、`NONDETERMINISTIC_VERIFICATION` 等 Run 终态原因；禁止以稳定错误码替代它。

## 4. 测试设计

- `tests/core/test_ids.py`：UUID v7 身份、NewType、generation、UUID 序列化。
- `tests/core/test_paths.py`：BranchPrefix、相对路径、去重、UTF-8 排序、集成键、canonical JSON。
- `tests/core/test_enums.py`：枚举 exact-match、大小写和值稳定性。
- `tests/core/test_errors.py`：StableErrorCode/FailureReason 分族与字符串值。
- `tests/core/test_models.py`：模型构造、round-trip、别名、extra forbid、判别联合。
- `tests/core/test_policy.py`：资源加载、URI、版本和摘要。
- `tests/contracts/test_core_contracts.py`：核心公共导出和跨域模型字段。
- `tests/contracts/test_policy_resources.py`：Phase exact-match、十槽位模板、十档预算资源。

| 验收条款 | 用例名 |
| --- | --- |
| V6 新增契约 | `test_v6_advice_and_repair_contracts` |
| V5 四件冻结工件 | `test_frozen_artifact_bundle_round_trip` |
| V-M00-V4-002 | `test_write_scope_error_code_is_stable` |
| V-M00-V4-004 | `test_candidate_generation_accepts_only_zero_one_two` |
| V-M00-V4-008 | `test_toolchain_descriptor_contract_supports_resource_digests` |
| V-M00-V4-017 | `test_core_has_no_runtime_side_effect_imports` |
| BranchPrefix 规则 | `test_branch_prefix_boundary_and_reserved_segments` |
| Phase policy | `test_phase_policy_matches_m00_exactly` |
| 判别联合纪律 | `test_discriminated_targets_reject_unknown_variants_and_extra_fields` |

行为型验收（write scope 拦截、CreateRun 零副作用、状态转移、验证归因）不在 core 测试中重复实现，而在对应 workspace/api/runtime/verification 任务中引用 core 契约并验证。

## 5. 与架构文档的差异记录

- **有差异（以最新对齐记录为有效实施契约）**：M-14 架构正文旧代码块仍列 `initial_pack_token_cap` 与 `session_token_cap`，而 CM-MEMORY 最新重对齐已将预算模型改为 `max_rounds` 与 `eviction_watermark_pct`；本任务按最新对齐记录实现，并在实施收尾登记架构文档同步事项。
- **有差异（机械补全）**：M-14 旧 `SessionKind` 代码块未列 `RepairSession`，但 V6 修复会话和 CM-MEMORY/CM-CORE 变更记录已明确九值；本任务加入 `RepairSession`。
- **有差异（联动扩容）**：M-00 原始代码块没有后续对齐新增的 PLAN/验证集合错误码、三类静态资源和 `RepairEvidence`；本任务按 CM-CORE 追加变更记录及下游对齐记录实现。
- 架构模块设计文档的回写需要遵守 AGENTS.md 对架构文档修改的确认流程；当前 goal 模式不另行扩大本任务范围，先以对齐记录和本详细设计保存差异证据。

## 6. 影响面

- `codemigrator.core` 将成为全部七个下游子包的类型依赖源，任何公共枚举/错误码变更都必须回到 core 并同步契约测试。
- `uuid-utils` 和 canonical JSON 适配依赖需由 CM-INFRA-001 登记到项目依赖；core 任务不创建第二套工程基线。
- 三类静态资源随包发布，CM-VERIFY、CM-LOOP、CM-MEMORY、CM-WORKSPACE 在 Run 创建时读取并冻结摘要，运行期不热加载。
- 资源正文和高层 payload 的未定业务字段保持最小边界，后续任务只能扩充已对齐的契约变更，并需追加记录。

