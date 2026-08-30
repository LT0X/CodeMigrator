# CM-DRAFT-001-起草期编排与四件工件实施计划

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-DRAFT-001
- **所属模块/crate**：M-16 起草 + M-04 起草 + M-14 / `codemigrator-runtime`
- **关联交付物**：详细设计 `codemigrator_design_doc/detailed_coding_design/session/CM-DRAFT-001-起草期编排与四件工件详细设计.md`、迭代记录 `codemigrator_dev_progress/session/CM-DRAFT-001-起草期编排与四件工件迭代记录.md`

## 1. 目标与范围

- **完成定义（DoD）**：在 runtime 内交付可测试的起草期最小闭环：确定性切域骨架与恰好一次覆盖校验、探索报告/切域建议契约、档案一致性纯核对、四件工件的 TaskDraftRevision 双轨账本、多轮问答确认冻结、2–3 个风险热点的 Rulebook 约束/自由 Code 试译并排结果；对应 `V-M16-V4-012`、`V-M16-V4-013`、`V-M16-V4-014` 与对齐记录 D-01/D-02/D-03/D-04/D-06 测试通过，全量规则测试通过。
- **范围排除项**：不实现 Agent Loop/call loop、统一 Context Manager 与预算数值、Migration Spec 四道能力门、CreateRun/Run 副作用、图谱/PSF 构建、REST/SSE、runtime correction/PlanRevision、Skill catalog 和真实模型调用。

## 2. 前置条件

- [x] 工作分支：从已合并 `develop` 切出 `feature/draft-orchestration`。
- [x] 环境就绪项：Python 3.12 与锁定 uv 环境；优先离线规则测试，不使用真实模型调用。
- [x] 上游依赖：`CM-CORE-001`、`CM-INFRA-001`、`CM-SPEC-001`、`CM-ANALYSIS-001`、`CM-SANDBOX-001` 已合入 `develop`；复用 core 工件、ID、Advice/枚举和 analysis 投影契约。
- [x] 设计文档：已阅读 M-16、M-04、M-14、`CM-DRAFT-001-对齐记录.md`；本任务详细设计同步保存于 detailed_coding_design。

## 3. 实施步骤

1. - [x] 建立 runtime 起草契约模型：FocusBrief、ExploreReassignment、探索锚点/报告、域骨架与阶段状态（`src/codemigrator/runtime/draft_models.py` → D-02/D-03）。
2. - [x] 编写确定性域骨架和恰好一次覆盖校验（`src/codemigrator/runtime/draft_validation.py`、`tests/draft/test_validation.py` → D-02）。
3. - [x] 编写档案锚点/语义模块/冲突的最小机械一致性核对纯函数（`src/codemigrator/runtime/draft_validation.py`、`tests/draft/test_dossier_consistency.py` → D-01）。
4. - [x] 建立四件工件摘要与 TaskDraftRevision 双轨账本；让工件变更发新 revision，问答仅追加 ledger，确认冻结当前 revision 与问答指针（`src/codemigrator/runtime/draft.py`、`tests/draft/test_ledger.py` → D-06/V-M16-V4-014）。
5. - [x] 实现起草阶段顺序和只读工具授权断言，接入探索报告归并/切域建议记录及试译校准（`src/codemigrator/runtime/draft.py`、`tests/draft/test_flow.py` → V-M16-V4-012/D-03/D-04）。
6. - [x] 用零副作用计数替身验证未确认草稿不创建 Run、事件、Slice、candidate 或托管输出，并验证确认只返回冻结事实（`tests/draft/test_side_effects.py` → V-M16-V4-013/V-M16-V4-014）。
7. - [x] 同步迭代记录、主任务表和任务设计文档，执行 runtime/draft 专项、全量 pytest、Ruff、mypy、import-linter、compileall、diff check（`my_space/`、`tests/` → §4）。
8. - [ ] 从当前分支提交并推送，创建 PR；仅启动一次审查并等待子 agent 终态，按该次结论一次性修复后直接合并，不对同一 PR 发起第二次审查。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| 起草专项规则测试 | `/home/xtc/env/codemigrator-infra/bin/uv run --offline --frozen --group dev pytest -q tests/draft` | 全部通过 | D-01/D-02/D-03/D-04/D-06、V-M16-V4-012~014 |
| 全量测试 | `/home/xtc/env/codemigrator-infra/bin/uv run --offline --frozen --group dev pytest -q` | 无回归 | V-M16-V4-012~014 |
| 静态检查 | `ruff check src tests`、`mypy src` | 零错误 | 工程质量门 |
| 架构边界 | `lint-imports --config pyproject.toml` | 0 broken contracts | M-01/M-16 |
| 字节/编译检查 | `python -m compileall -q src tests`、`git diff --check` | 无错误/空白问题 | 工程质量门 |
| 条款勾选 | 人工核对本计划与详细设计映射 | 条款与测试证据一致 | V-M16-V4-012~014 |

- 沙箱/Git ref/恢复类验证不属于本任务；本任务在本机 WSL2 使用确定性替身验证零副作用，不进行真实模型调用。

## 5. 风险与回滚

- **风险点及缓解**：core 工件保持 opaque，runtime 只保存其版本/摘要和确认指针；通过对齐记录冻结的最小 schema 避免臆造 M-04/M-14 实现；账本/API 使用 UUID 与摘要校验保证幂等和过期 revision 拒绝；试译只接受内存字符串，不开放文件写入。
- **回滚方式**：保留分支提交历史；若验证或审查失败，在当前 feature 分支一次性修复；必要时使用 `git revert <commit>` 回退，不改写远程历史。

## 6. 收尾清单（对应 AGENTS.md §4 四件套）

- [ ] 模块迭代记录已按模板生成于 `codemigrator_dev_progress/session/`。
- [ ] 主任务表 `CodeMigrator开发任务规划与进度跟踪.md` 已更新。
- [ ] 详细设计文档已保存于 `codemigrator_design_doc/detailed_coding_design/session/`。
- [x] 本实施计划已保存于 `Implementation_plan_doc/session/`。
- [x] 对齐记录与架构文档无新增冲突；若实现发现冲突，先追加对齐变更并按 §3.4 回写架构文档。
