# CM-CORE-001 对齐记录

> 用途：本文件是任务 `CM-CORE-001`（模块 M-00 垂类设计原则与架构哲学）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-CORE-001` |
| 模块编号 | M-00 |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_垂类设计原则与架构哲学.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 1 轮（Wave 0+1 轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/core/` 子包——公共契约唯一 owner（M-00 为契约真相，AGENTS.md §2.1）：

- **ID 族（UUID v7 NewType）**：RunId/SpecId/SliceId/TaskId/CheckId/ReceiptId/RequestId/DispatchAttemptId/SessionId/MessageId/QuestionId/TaskDraftRevisionId/CorrectionIntentId/PlanRevisionId/ProjectId/ProjectSnapshotId/OutputWorkspaceId/ProjectModuleId/RepairDecisionId/AdviceId。JSON 小写连字符字符串、SQL UUID；`CandidateGeneration` 为受验证 int NewType（仅 0/1/2）。
- **枚举族（20 个，值字符串严格照抄 M-00 代码块）**：MigrationSessionStatus/InteractionStatus/CorrectionIntentStatus/SliceKind/ArtifactKind/DossierBudgetTier/SliceAttemptStatus/PlanEdgeKind/RunStatus（四阶段 9 态）/FailureReason（含 V6 新增 DossierInconsistent）/DeliveryChannelStatus/ModelProfile（两档）/Phase/ResidentRole/AdviceKind/ModuleBoundaryStrategy/CheckAction/DiagnosticSeverity/CheckStatus。
- **pydantic 模型族**：M-00「公共契约：身份、代次和状态只有一个定义」节代码块全量（ArtifactRef、UnderstandingDossier/DossierEntry、MigrationRulebook/RulebookEntry、TargetProjectBlueprint、PlanProposal/PlanValidation、WriteScopeOut/WriteScope、MigrationSlice、PlanEdge、SliceCandidate、ActiveDispatch、GitRunRefs、FrozenArtifactBundle、CreateRun、RemoteRepository/RegisteredProject、Advice/RepairDecision/GlobalRepairSession、TreeSitterGrammarRef/ManifestParserRef/SourceToolchain/TargetToolchain/ToolchainDescriptor、CheckCommandTemplate/RequiredCheck/ContractArtifact、Diagnostic 判别联合（FileLine/TestIdentity/Unknown）与 DiagnosticMapping、CheckResult、VerificationSubject 判别联合（LocalCandidate/ProspectiveIntegration/FinalVerified）、VerificationOutcome/DerivedVerificationGuard/IntegrationIntent 等）。基座为 pydantic v2 BaseModel（文档代码块事实）。
- **稳定错误码**：`StableErrorCode`（见 D-03）。
- **Phase 工具授权矩阵**：`core://phase-tool-policy/v2`（见 D-04）。
- **派生纯函数**（见 D-06）：BranchPrefix 校验、RepoRelativePath UTF-8 字节规范化、CandidateGeneration 边界校验、integration key（`integration_rank ASC → SliceId ASC`）比较、canonical JSON 序列化。
- **契约测试**：见 §4。

### 1.2 边界（不做什么）

- 不实现状态机转移逻辑、调度、事务、actor（M-03 runtime）；core 只定义状态与枚举。
- 不定义 run_events 事件名词汇与投影 schema（M-02 api，D-05）。
- 不实现 PlanValidation 四重护栏校验器（M-07 planning）；write scope 两两互斥判定不在 core 交付（D-06）。
- 不实现 verification_fingerprint 派生（M-03 Harness 编排层所有；core 仅提供 canonical JSON 底座，D-06）。
- 不定义语言映射决策表（M-01 唯一 owner）。
- 不含描述符/grammar 资源内容（`descriptors/` 数据；core 只交付类型）。
- 不复制第二套状态机/枚举/错误码定义（AGENTS.md §2.1 红线）。
- core 零运行时状态：不读环境变量、不建后台任务/线程、不持进程级缓存（import-linter 契约「runtime 之外任何子包不读环境」含 core）。

### 1.3 产出物

- `src/codemigrator/core/` 子包（按域分模块，见 D-02）。
- 包内静态资源 `core://phase-tool-policy/v2`（JSON 数据文件，见 D-04）。
- core 契约测试（pytest，落 `tests/` 下 contracts 分组，目录归属与 CM-INFRA-001 对齐）。
- 模块迭代记录（`my_space/codemigrator_dev_progress/core/CM-CORE-001-*_迭代记录.md`）。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | UUID v7 生成器实现（Python 3.12 stdlib 无 uuid7） | 标准库自实现 / uuid-utils 库 / 未来 stdlib | **引入 uuid-utils 库**（C 实现性能好，core 接受该运行时依赖；需在 CM-INFRA-001 pyproject 登记依赖） | 对齐问答 Q-01（2026-08-29） |
| D-02 | core 内部文件组织 | 按域分模块 / 单文件 contracts.py / 按机制四文件 | **按域分模块**：`ids.py`/`enums.py`/`errors.py`/`policy.py` + models 按域拆分（run/slice/plan/verification/descriptor/advice 等）+ `__init__.py` 统一导出 | 对齐问答 Q-02 |
| D-03 | 稳定错误码 Python 形态 | str 枚举 / 常量模块 / 异常类层级 | **`StableErrorCode(str, Enum)` 集中定义**（可枚举、可序列化、单一 owner）；各子包自行决定异常包装方式 | 对齐问答 Q-03 |
| D-04 | `core://phase-tool-policy/v2` 静态资源形态 | JSON 数据文件 / Python 常量模块 / TOML | **JSON 数据文件**随包发布、importlib.resources 读取；CI 可对文件做 exact 校验 | 对齐问答 Q-04 |
| D-05 | run_events 事件名常量归属 | api(M-02) / core | **归 api（M-02）**：事件词汇随投影 schema 在 api 子包定义；core 只定义 Advice/RepairDecision 等类型本体 | 对齐问答 Q-05 |
| D-06 | 派生纯函数交付范围 | 最小集+canonical / 仅最小集 / 加互斥判定 | **最小集+canonical**：BranchPrefix 校验、RepoRelativePath UTF-8 规范化、CandidateGeneration 边界、integration key 比较、canonical JSON 序列化（fingerprint 公共底座，M-03/M-10 复用）；互斥判定归 M-07 | 对齐问答 Q-06 |
| D-07 | pydantic extra 字段纪律 | 全局 forbid / 仅文档明文处 | **全局 `extra="forbid"`**：所有 BaseModel 统一 ConfigDict(extra="forbid")，判别联合自然满足 | 对齐问答 Q-07 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- 无（core 是 8 子包依赖图唯一源头）。第三方依赖：pydantic v2、uuid-utils（D-01）、semver（ToolchainDescriptor 字段类型，M-00 代码块事实）。

### 3.2 下游消费

- 全部 7 个子包（analysis/planning/workspace/verification/sandbox/api/runtime）+ `apps/codemigrator-cli` + `web/` 经 api 消费 core 类型。
- 关键消费关系：M-03 runtime 消费 RunStatus/SliceAttemptStatus/Advice/两档收养契约类型；M-07 planning 消费 PlanProposal/PlanValidation/WriteScope/MigrationSlice；M-10 verification 消费 VerificationSubject 判别联合/CheckResult/fingerprint 语义类型；M-12 工具系统消费 phase policy 与错误码；M-02 api 消费 DTO 基础类型并自持事件词汇（D-05）。

### 3.3 跨模块接口边界

- 状态机/枚举/错误码/phase 授权矩阵唯一 owner 为 core（M-00），下游只准引用、禁止复制第二套（AGENTS.md §2.1）。
- 事件名词汇归 api（M-02）——D-05；M-03 写入 run_events 时引用 api 定义的事件名常量。
- fingerprint 派生归 Harness 编排层（M-03）；core 只交付 canonical JSON 底座（D-06）。
- write scope 互斥判定归 M-07 校验器（D-06）；core 的路径规范化函数为其提供底层工具。
- PlanProposal/PlanValidation/TargetProjectBlueprint 的字段形状与精确校验规则为实施期开放项（M-00 注释明示），core 按 M-00 当前占位形状实现，不臆造字段。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V6 契约新增 | ResidentRole/AdviceKind/Advice/RepairDecision/GlobalRepairSession/DossierInconsistent（FailureReason 值）定义齐全 | 契约测试：枚举值与 M-00 代码块逐值一致；模型字段/必填项一致；JSON 序列化 round-trip |
| V5 可验收增量（契约面） | 四件冻结工件类型（FrozenArtifactBundle/UnderstandingDossier/TargetProjectBlueprint/MigrationRulebook）齐全 | 契约测试：字段一致 + extra="forbid" 生效（未知字段拒绝） |
| V-M00-V4-002 | 写白名单外路径返回 `WRITE_SCOPE_VIOLATION` | core 契约测试仅覆盖错误码存在性与值稳定性；拦截行为在 workspace/工具网关测试（M-08/M-12 侧） |
| V-M00-V4-004 | generation 0/1/2 约束（拒绝 3 及负数、物理重派不增代） | core 单测：CandidateGeneration 边界校验纯函数用例表 |
| V-M00-V4-008 | 描述符版本/grammar/镜像摘要不匹配 CreateRun 零副作用拒绝 | core 仅交付 ToolchainDescriptor/Sha256 类型支撑；行为验收归 M-02/M-05 |
| V-M00-V4-017 | 运行时扫描无周期续权/代次令牌/常驻轮询等 | import-linter 契约检查 + 静态审查（core 无环境读取、无后台任务/线程） |
| BranchPrefix 规则（M-00 明文） | 1~32 字节 ASCII 小写字母/数字/`-`/`/`；拒绝空段、`.`、`..`、`.git` | core 单测：合法/非法用例表（含边界长度 1/32/33） |
| phase policy exact（M-00 表） | PLAN={ReadFile,QuerySourceAst,Exec}；EXECUTE=六工具全量；VERIFY=∅；REPORT=∅ | 契约测试：JSON 资源内容与 M-00 授权矩阵 exact-match；`core://phase-tool-policy/v2` URI 可寻址 |
| 判别联合纪律 | VerificationSubject/DiagnosticTarget variant 外字段 extra="forbid" | 契约测试：discriminator 分派正确、未知 variant 拒绝 |

> 系统级行为条款（V-M00-V4-003/005/006/007/009~016 等）在本任务仅做类型支撑，行为验收归属 runtime/verification/api 等对应任务。

## 5. 风险与注意点

- **枚举表同步义务**：状态机正文语义任何变更必须在同一变更集同步枚举表与状态转移图（M-00 验收条款）；正文与枚举漂移视为契约缺陷。
- **AdviceId 文档缝隙**：M-00 NewType 清单未显式定义 AdviceId，但 Advice 模型引用 `advice_id: AdviceId` → 实现时补齐为 UUID v7 NewType（机械补全，非语义决策）。
- **两族错误语义不得混并**：StableErrorCode（网关/工具拒绝码）与 FailureReason（Run 终态原因）是两个族；DOSSIER_INCONSISTENT 属 FailureReason，不进 StableErrorCode。
- **枚举值大小写风格不统一是文档现状**（RunStatus="CREATED"、MigrationSessionStatus="Drafting"、DiagnosticSeverity="Error"、SliceAttemptStatus="READY"）——严格照抄 M-00，禁止"统一美化"。
- **uuid-utils 依赖联动**：D-01 引入的依赖须在 CM-INFRA-001 的 pyproject.toml 登记（跨任务协调点）。
- **序列化口径**：UUID JSON 小写连字符、SQL UUID；RepoRelativePath 去重后按 UTF-8 原始字节规范化（M-00 明文）。
- PlanEdge 的 `from` 字段 JSON 别名（Python 保留字）按 M-00 代码块 `Field(alias="from")` + `populate_by_name` 实现。
- 核心不承诺清单（M-00）：不接受用户 shell/命令行正文/system prompt、不承诺源仓库写入、不为分布式保留协调周期——契约设计不得预留违背项。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | ID 族 UUID v7 生成器实现方式（stdlib 无 uuid7） | 引入 uuid-utils 库 |
| Q-02 | core 内部文件组织方式 | 按域分模块（ids/enums/errors/policy + models 按域 + __init__ 统一导出） |
| Q-03 | 稳定错误码 Python 表示形态 | str 枚举集中定义（StableErrorCode(str, Enum)） |
| Q-04 | phase-tool-policy/v2 静态资源形态 | JSON 数据文件（importlib.resources 发布） |
| Q-05 | run_events 事件名常量归属 | 归 api/M-02（core 只定义类型本体） |
| Q-06 | 派生纯函数交付范围 | 最小集 + canonical JSON 序列化 |
| Q-07 | pydantic extra 字段纪律 | 全局 extra="forbid" |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-00 V6 设计文档（公共契约节）与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-07 |
| 2026-08-29 | StableErrorCode 扩容登记 | CM-SPEC-001 对齐 D-01（用户确认）：单一枚举集中组织，M-05 Spec 码族（SPEC_*/CHECK_*/DESCRIPTOR_*/TOOLCHAIN_*/SPEC_IN_USE）与 M-02 码族（STALE_VERSION/IDEMPOTENCY_CONFLICT）全量进 core StableErrorCode；D-03 机制不变，仅枚举成员扩容 |
| 2026-08-29 | wave23 契约扩容登记 | Wave 2+3 对齐联动的 core 扩容（各记录见 code_alignment_record/）：①PLAN 八码（CM-PLAN D-02：PLAN_CYCLE 等）；②M-10 集合四码（CM-VERIFY）；③三个静态资源新增（core://verification-policy/v1——CM-VERIFY D-04、core://session-templates/v1——CM-WORKSPACE D-02、core://session-budget/v1——CM-MEMORY D-01）；④SessionKind 补 RepairSession（CM-MEMORY D-01 文档缝隙机械补全）；⑤新 schema 建议（RouteSuggestion payload——CM-SUPERVISOR D-01、RepairBrief/SituationalSnapshot——CM-REPAIR D-02/D-04、RepairEvidence 可靠性三值+两布尔——CM-VERIFY D-03、Advice proposal_hash=SHA-256(JCS canonical)——CM-RUNTIME D-01）；实施期统一落 core 公共契约 |
| 2026-08-29 | 预算体系重对齐联动（用户发起·DSH 哲学） | core://session-budget/v1 资源字段重构（CM-MEMORY 重对齐）：SessionBudgetProfile 改 {session, max_rounds, eviction_watermark_pct}（作废 token cap 字段——结构性轮数预算取代计量式）；UsageLedger（Run 钱包断路器）语义类型不变（input/output/cost 三项 M-00 契约）；实施期随 M-00/M-14 契约表述同步 |
| 2026-08-30 | 独立审查修订登记 | 根据 M-12 的 `SHA-256(JCS(payload))` 明文要求修正资源摘要；为 `semver.Version` 增加字符串 JSON 序列化；补入 M-14/M-11 已引用但 core 漏列的 `CONTEXT_BUDGET_EXCEEDED`、`CONTEXT_CAPABILITY_INVALID`、`RECOVERY_LEDGER_INCONSISTENT`；同时将 CreateRun/路径/subject/dossier 规则落实到模型边界并补回归测试。该行是对公共 owner 完整性的机械补全，不改变 D-01～D-07 决策。 |
| 2026-08-30 | 第二轮独立审查修订登记 | 补全跨模块架构已引用的 `MODEL_BINDING_INVALID`、`PHASE_STATUS_MISMATCH`、`CANDIDATE_REF_CONFLICT`、`REMOTE_REF_MOVED`、`DEPENDENCY_UNAVAILABLE`；`PlanEdge` 默认 JSON 输出启用 `from` alias；`semver.Version` 增加字符串输入解析与 JSON Schema 兼容定义；路径列表模型边界将 malformed container 转换为 Pydantic `ValidationError`；facade 补导出 `validate_candidate_generation`。该行继续遵循“core 为稳定错误码/公共纯函数唯一 owner”，不改变 D-01～D-07 决策。 |
| 2026-08-30 | PlanEdge 兼容性修订登记 | 为避免依赖 Pydantic 2.11 新增的 `serialize_by_alias` 配置，PlanEdge 保留 `Field(alias="from")`/`populate_by_name`，改用 Pydantic v2 通用 model serializer 将默认 JSON 输出稳定为 `from`；现有 JSON round-trip、Schema 和 canonical 序列化测试继续作为契约证据，不新增最低版本要求。 |
