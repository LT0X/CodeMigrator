# feat: implement draft orchestration and artifact freeze

关联任务：`CM-DRAFT-001`

模块迭代记录：CM-DRAFT-001 起草期编排与四件工件迭代记录（仓库开发记录）

## 背景

依据 M-16 起草流程、M-04 理解会话、M-14 V6 工件边界及 CM-DRAFT-001 对齐记录 D-01～D-06，补齐起草期确定性探索、四件工件双轨版本、AskUser 对齐账本、试译校准与确认冻结契约，供后续 CM-PLAN/CM-RUNTIME/CM-LOOP/CM-API 接入。

## 变更点

- `src/codemigrator/runtime/draft_models.py`：新增封闭的 FocusBrief、ExploreReassignment、探索报告、域骨架、TaskDraftRevision、AskUser、试译和冻结回执模型。
- `src/codemigrator/runtime/draft_validation.py`：新增确定性切域、扇出限制、恰好一次覆盖和 D-01 档案一致性纯核对。
- `src/codemigrator/runtime/draft.py`：新增阶段门控起草流、append-only 问答账本、JCS 工件摘要、core `FrozenArtifactBundle` 回执和零副作用试译。
- `tests/draft/`：新增 25 项规则/契约测试，覆盖 V-M16-V4-012/013/014 与 D-01/D-02/D-03/D-04/D-06。
- 开发记录：同步实施计划、详细设计、迭代记录和主任务表。
- BREAKING：无；不修改 M-00 core 公共模型，仅复用既有类型和错误语义。

## 自测证据

- `uv run --offline --frozen --group dev pytest -q tests/draft` → `25 passed`。
- `uv run --offline --frozen --group dev pytest -q` → `196 passed`。
- `ruff check src tests`、`mypy src`、`python -m compileall -q src tests`、`git diff --check`、`lint-imports --config pyproject.toml` → 通过；import-linter 为 3 kept / 0 broken。
- `V-M16-V4-012` ✓ 起草授权面仅允许 ReadFile/QuerySourceAst/只读 Exec；试译无写端口。
- `V-M16-V4-013` ✓ 未确认阶段不产生 Run、run_events、Slice、candidate 或托管输出。
- `V-M16-V4-014` ✓ 确认冻结当前 revision、四件摘要、FrozenArtifactBundle 和问答指针；过期 revision 拒绝。
- 未进行真实模型调用；本任务只使用确定性规则测试。

## 风险与回滚

- 账本当前为 runtime 内存替身，后续由 CM-API/CM-RUNTIME 接入持久化；core 工件仍是唯一公共定义。
- 试译结果只保存在会话对象，未开放文件、候选工作区或 Run 写入能力。
- 回滚使用本分支提交的 `git revert`，不改写远程历史。
