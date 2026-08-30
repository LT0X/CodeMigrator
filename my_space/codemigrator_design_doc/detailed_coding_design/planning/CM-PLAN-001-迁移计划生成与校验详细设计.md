# CM-PLAN-001 迁移计划生成与校验详细设计

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-PLAN-001
- **所属模块/crate**：M-07 / `codemigrator.planning`
- **依据的架构文档**：`my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_迁移计划生成器.md`
- **依据的对齐记录**：`my_space/code_alignment_record/plan/CM-PLAN-001-对齐记录.md`（D-01～D-06）
- **关联交付物**：实施计划 `my_space/Implementation_plan_doc/planning/CM-PLAN-001-迁移计划生成与校验实施计划.md`、迭代记录 `my_space/codemigrator_dev_progress/planning/CM-PLAN-001-迁移计划生成与校验迭代记录.md`

## 1. 需求与边界

- **本任务做什么**：在 `src/codemigrator/planning/` 提供严格封闭的 Planner 提案 schema、确定性机器校验、服务端 UUIDv7 冻结、plan hash、四类 Slice/三类工件派生、组名规范化、重试归约和只读涟漪预览。
- **不做什么**：不调用模型，不拥有 LLM 对话循环/工具授权；不调度 DAG、不执行写 scope 拦截、不实现验证归因或联合域安全算法；不扩展 Blueprint 字段、不落 PostgreSQL、不写目标仓库。
- **实现原则**：提案是候选事实，只有校验器全部通过才生成冻结计划；校验失败在 ID 分配和 ledger 持久化之前结束，禁止部分计划。

## 2. 契约引用

| 引用事实 | Owner | 使用方式 |
| --- | --- | --- |
| `MigrationSlice`、`WriteScope`、`PlanEdge`、`SliceKind`、`ArtifactKind`、`RequiredCheck` | M-00/core | 冻结输出直接构造 core 模型，不复制字段定义 |
| `MigrationSpec`、`TargetProjectBlueprint`、`FrozenArtifactBundle`、`DossierEntry` | M-00/M-05/core | 规划输入引用四件冻结工件和 Spec 范围 |
| F1～F4、PSF-2/PSF-3 | M-06/analysis | 复用 `AnalysisResult` 的 module/import/coverage/artifact/symbol/reference/relation 模型 |
| `canonical_json_bytes`、`integration_key`、`new_uuid7`、安全相对路径 | core | 哈希、集成序、ID 和路径规则唯一来源 |
| 八个 PLAN 稳定拒绝码 | core `StableErrorCode` | 校验违规与 `PlanRejected` 暴露该枚举，不新增第二套错误码 |

## 3. 模块设计

### 3.1 文件职责

- `core/models/plan.py`：唯一承载 `PlanSliceProposal`、`PlanEdgeProposal`、`PlanEdgeEvidence`、`ArtifactTask`、`PlanProposal`、`PlanViolation`、`PlanValidation` 等严格 `extra=forbid` 公共模型；`planning/models.py` 只承载 `PlanningInputs`、`PlanningLimits`、`FrozenPlan` 并 re-export 这些类型。
- `validator.py`：`PlanValidator` 执行路径/端点/语义、范围互斥、Blueprint、源文件恰好覆盖、DAG、rank 和规模护栏；`PlanLedger` 只在成功冻结后记录对象。
- `derivation.py`：根据现有分析事实派生四类 Slice 和三类工件动作；所有决策数据驱动，不按语言写分支；提供组名规范化和测试生成锚点降级结果。
- `ripple.py`：只读计算符号引用闭包、反向依赖闭包和冻结 Slice 映射，返回失效/重建/预计数量/符号/集成分布，不改变冻结计划。
- `retry.py`：将 schema/校验错误转成结构化反馈并重试最多 3 次；provider 物理故障立即交给 runtime，不消耗反馈次数。
- `__init__.py`：唯一公共导出正门；不导出模型会话、运行时或 API 依赖。

### 3.2 提案与冻结模型

提案使用 `local_ref`（如 `CT`、`A`），正则限制为稳定 ASCII 标识且 proposal 内唯一；`kind` 只接受 core 四类 `SliceKind`。每个 Slice 提案包含 `source_modules`、规范化后的 `write_paths`/`create_roots`、说明性 `rationale` 和 `artifact_tasks`；required checks 可由上游提供但不由 planning 复制检查定义。

边端点只引用同一 proposal 的 local ref，kind 只接受 `Requires`/`OrderedBefore`，provenance 只接受 `Structural`、`ImportStatic`、`ImportUnknown`、`Coverage`、`WriteScopeConflict`。`ImportUnknown` 不可伪装成 Requires；`WriteScopeConflict` 不是合法的初译边，范围冲突必须拒绝。两类保守边必须携带结构化 `PlanEdgeEvidence`（reason/location），并随冻结结果和 hash 保留。

冻结阶段先生成 local ref→UUIDv7 的服务端映射，再构造 core `MigrationSlice`/`PlanEdge`；integration order 为 `integration_key(rank, SliceId)` 升序，完成时间不参与。冻结对象包含输入四件工件 hash、snapshot OID、提案引用、校验结果、工件动作、计划 rationale 和 plan hash。所有输入模型在哈希前使用 core JCS canonical JSON；哈希字段变化即产生新 hash。

### 3.3 机器护栏

校验顺序固定为：schema 已由 Pydantic 完成 → 边/引用语义 → 两两 write scope → Blueprint → source file coverage → DAG → rank topology → limits。校验期间不调用 ledger。

1. **范围互斥**：`write_paths` 交集、`create_roots` 与任一 write path/root 的目录交集都拒绝；已有 OrderedBefore 不能豁免。
2. **Blueprint**：若占位 `module_boundaries` 提供 `target_path_prefix`/`target_prefix`/`path_prefix` 等既有结构化前缀，或 `target_layout_principles` 明确给出 `layout prefix/root`、`paths/files under` 路径约束，则每个 write path/root 必须落在某一前缀内；没有可解释前缀的占位条目不添加臆造约束。
3. **源文件与工件覆盖**：从 Analysis F1 的模块文件中取得 Spec scope 内文件，排除 F4 已识别工件；每个源文件映射到且仅映射到一个匹配角色的 Slice；每个 F4 工件也必须由恰好一个匹配 kind/source/artifact path 的 ArtifactTask 承载。缺失/重复/未知均为 `PLAN_COVERAGE_INVALID`。
4. **DAG**：合并 Requires 与 OrderedBefore 检查自环和任意有向环，拒绝码 `PLAN_CYCLE`。
5. **rank 拓扑**：每个 ref 必须有非负整数 rank，不能有额外 rank key；对每条边要求 `rank[from] < rank[to]`，否则 `PLAN_RANK_INCONSISTENT`。
6. **规模**：默认/对齐值为 Slice≤100、边≤500、单 Slice write_paths≤200、总 write scope 文件≤2000；`PlanningLimits` 可注入覆盖这些值。

拒绝结果保留结构化 `code`、JSON Pointer、message 和 details；`PlanLedger.records` 在所有拒绝路径保持空。通过后自动冻结，不设置逐 Slice 用户确认门。

### 3.4 派生语义

- Implementation 按给定 module groups 形成实现 Slice；没有外部分组时以单模块为最小确定性组。
- Covered 测试模块形成 TestTranslation；EmptyTestSuite 的 Source 模块形成 TestGeneration；Undetermined 不产生测试类 Slice。生成轨道设置 `generated=True`、全链路 `GENERATED` 标注义务、最低非平凡断言为 1 和 `information_firewall=True`；不会把目标实现正文加入锚点上下文。
- `ReferenceSite.ambiguous` 或 TextFallback 下无可靠符号条目时，测试生成锚点返回 module-level export summary 并带降级原因；多重 `SymbolBinding` 候选全部进入保守闭包。
- GeneratedCode 只产生 `GENERATE` 动作，DeclarativeConfig 只产生 `TRANSLATE` 等价动作，ResourceFile 只产生 `COPY`/轻转换动作；动作是 Slice 的附属工件事实，不新增 SliceKind，不按语言分支。ResourceFile 的源文件和目标复制路径不进入翻译 Slice 的 scope。

### 3.5 涟漪预览

以变更符号集合查 PSF-2 `ReferenceSite`，通过引用 site 文件反查 module；ambiguous 或 text fallback 时保守降级到 module-level 并显式标记，多重 `SymbolBinding` 候选全部纳入。再沿 PSF-3/Analysis 的 `IMPORT` 与 `COVERAGE` 关系边反向计算传递依赖闭包，最后用冻结 proposal 的 `source_modules → local_ref` 映射命中 Slice。未集成 Slice 进入 invalidated/rebuilt，已集成 Slice 进入 compensation/rebuild 候选；返回 affected symbols、degraded reasons、estimated count 和 integration rank distribution。整个函数只读，不变更 ledger 或 FrozenPlan。

### 3.6 重试归约

`PlanRetryReducer` 首次调用 proposer；proposal 的 Pydantic/schema 失败或机器校验失败生成结构化反馈。`max_retries=3` 表示初次尝试之外最多三次反馈重试；耗尽返回/抛出 `PlanFailed`，不调用 persist。`ProviderPhysicalFailure` 不属于 planning 反馈错误，直接向 runtime 抛出并由其物理重派策略处理，不递增反馈计数。

## 4. 接口签名

```python
class PlanValidator:
    def validate(self, proposal: PlanProposal, inputs: PlanningInputs) -> PlanValidation: ...

class PlanLedger:
    def freeze(self, proposal: PlanProposal, inputs: PlanningInputs) -> FrozenPlan: ...
    @property
    def records(self) -> tuple[FrozenPlan, ...]: ...

def compute_plan_hash(payload: Mapping[str, object] | FrozenPlan) -> str: ...
def validate_plan(proposal: PlanProposal, inputs: PlanningInputs) -> PlanValidation: ...
def freeze_plan(proposal: PlanProposal, inputs: PlanningInputs) -> FrozenPlan: ...
def derive_plan_proposal(inputs: PlanningInputs) -> PlanProposal: ...
def derive_artifact_tasks(facts: Sequence[ArtifactFact], ...) -> tuple[ArtifactTask, ...]: ...
def calculate_ripple(plan: FrozenPlan, analysis: AnalysisResult, changed_symbols: Sequence[str], ...) -> RipplePreview: ...
```

## 5. 测试设计与验收映射

| 测试文件 | 覆盖 |
| --- | --- |
| `test_models.py` | closed schema、local ref、路径、artifact action、provenance、extra forbid（D-01） |
| `test_validation.py` | 范围/Blueprint/coverage/cycle/rank/limits、测试→被测实现边禁用、零 ledger 写入（V-M07-V5-002、D-02/03/06） |
| `test_freeze.py` | UUIDv7 server allocation、自动冻结、integration order/hash、不可变快照（V-M07-V5-003） |
| `test_derivation.py` | 四类 Slice 双轨、GENERATED、防火墙、三类工件和组名唯一（V-M07-V4-012/013） |
| `test_ripple.py` | PSF-2→PSF-3→Slice 三步闭包、降级标记、集成分布、只读（V-M07-V4-014） |
| `test_retry.py` | 3 次反馈重试、PlanFailed 零持久化、物理故障不计次（D-04） |

## 6. 与架构文档的差异记录

- 无。Blueprint 仍使用 M-00 既有占位字段；严格 rank 校验、规模常数、八码拒绝码和重试次数按对齐记录 D-02～D-06 收口，未回写架构模块设计文档。

## 7. 影响面与风险

- 新增 planning 纯逻辑子包与 `tests/planning/`；不改变 core/analysis 的既有字段、不接入运行时副作用。
- 下游 M-03/M-08/M-10/M-11/M-16 可消费 FrozenPlan、scope/edge/rank/hash/ripple 数据；运行时接入时仍须通过各自 owner 的授权与持久化边界。
- 风险集中在 Blueprint 占位字段启发式解释和下游对冻结输出的持久化映射；通过结构化 violation、全量测试和文档指针控制。
