# CM-DRAFT-001-起草期编排与四件工件_迭代记录

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-DRAFT-001
- **关联模块/文档**：M-16 起草、M-04 起草、M-14；`architecture_module_design/CodeMigrator_会话与运行时修正编排.md`、`CodeMigrator_Agent_Loop设计.md`、`CodeMigrator_记忆与上下文管理.md`；`code_alignment_record/draft/CM-DRAFT-001-对齐记录.md`

## 1. 变更动机

CM-DRAFT-001 是 CM-ANALYSIS 与 CM-CORE 合入后的 Wave 2 起草期任务，需要把 V6 已确认的探索扇出、档案核对、四件工件双轨版本、AskUser 对齐和试译校准落为可测试的 runtime 编排契约，并为后续 CM-PLAN/CM-RUNTIME/CM-LOOP/CM-API 冻结接口边界。

## 2. 变更内容

- **当前状态**：已完成唯一审查反馈的一次性修复并直接合并 PR #6；主工作区已拉取合并后的 `develop`，任务闭环完成。
- **已建立文档**：`my_space/Implementation_plan_doc/session/CM-DRAFT-001-起草期编排与四件工件实施计划.md`、`my_space/codemigrator_design_doc/detailed_coding_design/session/CM-DRAFT-001-起草期编排与四件工件详细设计.md`。
- **代码变更**：新增 `src/codemigrator/runtime/draft_models.py`、`draft_validation.py`、`draft.py`；新增 `tests/draft/` 契约测试；唯一审查修复补齐报告归并/骨架复核、SpecArtifact canonical 边界、工件深拷贝、封闭 Exec 请求、Code profile 和重复覆盖保留。
- **关键实现决策**：遵循对齐记录 D-01～D-06；runtime 只保存四件 core 工件的内容摘要、revision 和问答指针，不复制公共工件定义；试译使用内存结果，零落盘/零 Run 副作用；不实现模型调用。

## 3. 自测与验证结果

- 专项规则测试：`/home/xtc/env/codemigrator-infra/bin/uv run --offline --frozen --group dev pytest -q tests/draft` → `25 passed`。
- 全量规则测试：`/home/xtc/env/codemigrator-infra/bin/uv run --offline --frozen --group dev pytest -q` → `196 passed`。
- Ruff、mypy、compileall、`git diff --check`、import-linter（3 kept/0 broken）均通过；未进行真实模型调用。
- `V-M16-V4-012`：✓ 起草工具仅 ReadFile/QuerySourceAst/只读 Exec，试译零写入。
- `V-M16-V4-013`：✓ 未确认阶段 Run、run event、Slice、candidate、托管输出计数均为零。
- `V-M16-V4-014`：✓ 确认回执绑定当前 revision、四件工件摘要与 core `FrozenArtifactBundle`，过期 revision 拒绝。
- D-01/D-02/D-03/D-04/D-06：✓ 均有专项契约测试覆盖。
- 唯一审查反馈：已在原分支一次性修复流程门控、Harness 骨架复核、M-05 `SpecArtifact` 接入、工件深拷贝隔离、封闭 Exec 请求、Code profile、重复覆盖保留及 PR 说明路径问题；未启动第二次审查。

## 4. 影响面与风险

- 影响 `src/codemigrator/runtime/`、`tests/draft/` 和本任务三份收口文档；不修改架构模块设计文档、数据库、API 或 sandbox。
- 风险是后续运行期存储和 API 对账本字段的接入；以本记录详细设计中的 revision/hash/question pointer 契约作为跨任务协调点。
- 本次审查修复继续保持与架构文档一致，无需回写 M-16/M-04/M-14；Exec 只校验闭合的只读请求，不执行命令，真实网关和会话循环由后续任务接入。

## 5. 后续行动

- [x] 先编写起草契约、验证、账本和副作用测试并确认失败。
- [x] 实现 runtime 起草最小闭环并完成规则验证。
- [x] 提交、推送并创建 PR；仅进行一次审查并等待终态，按结论一次性修复后直接合并，再拉取 `develop`。
