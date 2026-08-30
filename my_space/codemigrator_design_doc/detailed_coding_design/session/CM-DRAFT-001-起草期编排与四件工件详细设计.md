# CM-DRAFT-001-起草期编排与四件工件详细设计

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-DRAFT-001
- **所属模块/crate**：M-16 起草 + M-04 起草 + M-14 / `codemigrator-runtime`
- **依据的架构文档**：M-16《会话与运行时修正编排》起草流程、TaskDraftRevision、工具边界与确认冻结；M-04《Agent Loop 设计》理解会话/探索协调；M-14《记忆与上下文管理》V6 工件/探索报告与上下文边界
- **关联交付物**：实施计划 `Implementation_plan_doc/session/CM-DRAFT-001-起草期编排与四件工件实施计划.md`、迭代记录 `codemigrator_dev_progress/session/CM-DRAFT-001-起草期编排与四件工件迭代记录.md`

## 1. 需求与边界

- **本任务做什么（一句话）**：在 runtime 提供起草期的确定性探索域骨架、机械档案一致性核对、四件工件草稿版本/问答账本、确认冻结事实和无副作用试译校准的内存编排契约。
- **不做什么（显式排除项）**：不实现模型调用循环、常驻 Agent、统一 Context Manager、Spec 能力门、CreateRun、图谱构建、API/SSE、运行期纠偏/PlanRevision、持久化 SQL 和 Skill catalog。
- **覆盖的验收条款**：`V-M16-V4-012`、`V-M16-V4-013`、`V-M16-V4-014`；对齐记录 D-01、D-02、D-03、D-04、D-06。

## 2. 契约引用

| 引用事实 | Owner 文档 | 引用形式 |
| --- | --- | --- |
| `MigrationSpec`、`UnderstandingDossier`、`TargetProjectBlueprint`、`MigrationRulebook` | M-00/M-05 | 直接使用 `codemigrator.core.models` 现有模型，不复制工件定义 |
| `FrozenArtifactBundle`、`Advice`、`AdviceKind.ExploreReassignment` | M-00/M-03 | 复用 core 类型；本任务只生成 Advice payload 投影数据，不执行收养 |
| `QuestionId`、`TaskDraftRevisionId`、UUID v7 | M-00 | 复用 `codemigrator.core.ids` |
| `SpecScope.includes`、repository-relative path 规则、JCS canonical JSON | M-05/M-00 | 复用 `SpecScope`、`normalize_repo_relative_paths`、`canonical_json_bytes` |
| 起草工具边界 | M-16/M-04 | 只允许 ReadFile、QuerySourceAst、只读 Exec 的编排描述；WriteFile/EditFile/Shell 不进入起草授权集合 |

## 3. 详细设计

### 3.1 结构与职责

- `runtime/draft_models.py`：封闭的 FocusBrief、ExploreReassignment、探索锚点/报告/归并结果、域骨架、TaskDraftRevision、AskUser、只读 Exec 请求和试译结果模型；路径字段统一复用 core 的安全路径规则。
- `runtime/draft_validation.py`：纯函数 `build_domain_skeleton`、`validate_exact_coverage`、`check_dossier_consistency`。输入是有限路径/报告/现有 core 工件，输出只有校验事实，不创建 Run 或写外部存储。
- `runtime/draft.py`：`DraftLedger` 负责 TaskDraftRevision 与 Question/Answer append-only 账本；`DraftFlow` 负责机器域骨架校验、阶段顺序、只读工具请求校验、报告归并/Advice 记录、试译和确认门。
- TaskDraftRevision 的工件字段直接引用 core 的 `SpecArtifact` 与三件工件模型；runtime 仅复用已通过 M-05 的 canonical bytes 计算 SHA-256、revision id 和问答指针，不建立第二套公共工件类型。

### 3.2 关键机制

1. **确定性切域**：按 UTF-8 字节序处理 module boundary 和文件；单域不超过 20 个文件保持一个域，超过 20 个文件按第二级目录分组；生成域总数超过配置扇出上限（默认 6）则拒绝派发；Flow 完成探索时再次通过 `build_domain_skeleton` 验证报告域归属、扇出和骨架，扁平文件集合与输入集合必须恰好一致。
2. **探索与切域 Advice**：FocusBrief 只含域路径、风险热点/大文件/import 重量标注和正整数预算提示；ExploreReassignment 只含 `merge|split|refocus`、涉及域、理由和更新后的 brief。协调者记录 Advice，零直接写入权。
3. **档案一致性**：纯函数逐条解析 `file + start_line + end_line` 锚点；非 advisory 条目不得空锚点；语义模块锚点文件必须同时属于 Spec scope 与 F1 文件集合；未解决冲突数必须为零。失败结果携带 `DOSSIER_INCONSISTENT` 语义，CreateRun 的拒绝与副作用边界留给 CM-RUNTIME。
4. **双轨账本**：输入必须是 M-05 已接受的 `SpecArtifact`，并核验其 canonical bytes/hash 一致；四件工件内容的摘要 tuple 改变才创建新的 revision；问答以 Question/Answer 追加记录，不改变 revision。回答必须绑定当前 revision；相同 question/revision 的相同回答幂等，冲突回答和过期 revision 拒绝。revision 对外返回工件深拷贝，防止调用方修改已摘要内容。
5. **确认冻结**：确认门只接受当前 revision，且所有绑定问题均有回答；冻结回执保存 revision、四件工件摘要、`FrozenArtifactBundle` 引用和问答 ID。引用的摘要来自现有 JCS canonical JSON，媒体类型保持为 JSON；工件持久化由后续存储层负责。未确认阶段没有 Run、run event、Slice、candidate 或托管输出写入端口。
6. **试译校准**：从风险热点中确定性选择 2–3 个代表文件，接收约束版与自由版并标记 core `ModelProfile.Code`，会话内并排返回；结果不含路径写入能力，不落盘、不建 candidate、不触碰 Run，校准结论由上层转化为下一次工件 revision。

### 3.3 数据与接口

- `build_domain_skeleton(module_files, max_files_per_domain=20, max_fanout=6) -> tuple[DomainSkeleton, ...]`
- `validate_exact_coverage(skeleton, expected_files) -> CoverageResult`
- `check_dossier_consistency(dossier, spec_scope, f1_files, unresolved_conflict_count) -> DossierConsistencyResult`
- `DraftLedger.create_revision(artifacts) -> TaskDraftRevision`
- `DraftFlow.seed_artifacts(artifacts) -> TaskDraftRevision`、`finalize_alignment() -> None`
- `DraftLedger.revise(revision_id, artifacts) -> TaskDraftRevision`；内容摘要不变时返回原 revision
- `DraftLedger.append_question(question) -> AskUserQuestion`
- `DraftLedger.answer_question(answer) -> AskUserAnswer`；同答案幂等，过期/冲突拒绝
- `DraftLedger.freeze(revision_id) -> DraftFreezeReceipt`（含 core `FrozenArtifactBundle` 引用）
- `DraftFlow.validate_exec(request: DraftExecRequest) -> DraftExecRequest`（仅 ReadFile/QuerySourceAst/AskUser 的闭合参数）
- `DraftFlow.trial_translate(paths, constrained, freeform) -> tuple[TrialTranslation, ...]`

本任务不增加 PostgreSQL 表、REST/SSE DTO 或外部端口；后续 CM-API、CM-RUNTIME 使用这些内存契约接入各自存储/副作用边界。

### 3.4 错误处理

- 不新增稳定错误码；档案核对结果引用 `DOSSIER_INCONSISTENT` 语义，由 CM-RUNTIME 负责将其映射到 core `FailureReason.DossierInconsistent`。
- 不安全路径、非法锚点、超出扇出、覆盖不恰好一次、过期 revision、重复冲突回答、未回答确认问题均在 runtime 边界以 `ValueError` 或专用 `DraftConflictError` 拒绝。
- 试译输入不完整或文件数不在 2–3 范围时拒绝；拒绝不产生外部副作用。

## 4. 测试设计

- `tests/draft/test_validation.py`：确定性排序、>20 文件二级目录拆分、扇出上限、恰好一次覆盖。
- `tests/draft/test_dossier_consistency.py`：锚点解析、非 advisory 空锚点、语义模块越界、未解决冲突和通过路径。
- `tests/draft/test_ledger.py`：工件修改发新 revision、问答不发版、幂等回答、过期/冲突拒绝、确认冻结指针。
- `tests/draft/test_flow.py`：阶段顺序、工具授权、Advice 投影、2–3 文件双版本试译且无写端口。
- `tests/draft/test_side_effects.py`：未确认/试译阶段外部副作用计数保持零，确认只返回冻结事实。

| 验收条款 | 用例映射 |
| --- | --- |
| `V-M16-V4-012` | `test_draft_tool_authorization_is_read_only`、`test_trial_translation_has_no_write_side_effect` |
| `V-M16-V4-013` | `test_unconfirmed_draft_has_zero_run_side_effects` |
| `V-M16-V4-014` | `test_freeze_binds_current_revision_and_answers`、`test_stale_revision_cannot_be_frozen` |
| D-01 | `test_dossier_consistency_*` |
| D-02 | `test_domain_skeleton_*`、`test_exact_coverage_*` |
| D-03 | `test_focus_brief_and_reassignment_are_closed_contracts` |
| D-04 | `test_trial_translation_is_side_by_side_and_discarded` |
| D-06 | `test_artifact_change_creates_revision_but_answer_does_not` |

## 5. 与架构文档的差异记录

- 无。实现细化严格采用 `CM-DRAFT-001-对齐记录.md` D-01～D-06；不回写 M-16/M-04/M-14。

## 6. 影响面

- 新增 runtime 起草期纯内存契约与确定性测试；不改变既有 core 公共模型、数据库、compose、API 或 sandbox。
- CM-RUNTIME 后续消费一致性结果和冻结回执，CM-PLAN 消费冻结四件工件，CM-LOOP/CM-API/CM-MEMORY 分别接入会话、投影和预算边界。
- 版本摘要采用现有 `canonical_json_bytes`，避免与 CM-SPEC 的 canonical/hash 规则分叉。
