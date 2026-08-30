# CM-PLAN-001 迁移计划生成与校验实施计划

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-PLAN-001
- **所属模块/crate**：M-07 / `codemigrator.planning`
- **关联交付物**：详细设计 `codemigrator_design_doc/detailed_coding_design/planning/CM-PLAN-001-迁移计划生成与校验详细设计.md`、迭代记录 `codemigrator_dev_progress/planning/CM-PLAN-001-迁移计划生成与校验迭代记录.md`

## 1. 目标与范围

- **完成定义（DoD）**：交付零模型调用的 planning 纯逻辑包，完成严格封闭的 `PlanProposal`、四重护栏/严格 rank 拓扑/规模校验、校验通过后的 UUIDv7 冻结与 plan hash、四类 Slice/三类工件派生、PSF-2/PSF-3 涟漪只读预览、唯一组名规范化和 3 次反馈重试；专项与全量规则测试、Ruff、mypy、import-linter、compileall、diff check 全部通过。
- **验收映射**：`V-M07-V5-001`～`V-M07-V5-003`；`V-M07-V4-006`、`V-M07-V4-007`、`V-M07-V4-012`～`V-M07-V4-014` 的当前 V6 可施工语义；D-01～D-06。
- **范围排除项**：不实现 LLM Planner 会话/工具授权、DAG ready 调度、运行期 write scope 拦截、验证归因 schema、联合域安全判定、Blueprint 字段扩展、PlanRevision 编排、目标文件写入或数据库/API 接入。

## 2. 前置条件

- [x] 工作分支：从已合并 `origin/develop` 切出 `feature/plan-validator`。
- [x] 环境就绪项：Python 3.12 隔离环境 `/home/xtc/env/codemigrator-plan`；优先规则测试，不调用真实模型。
- [x] 上游依赖：CM-CORE、CM-INFRA、CM-SPEC、CM-ANALYSIS、CM-DRAFT 已合入；复用 core 的 ID、路径、canonical JSON、Slice/Edge、Spec、四件工件和 analysis F1～F4/PSF 模型。
- [x] 设计与契约：已完整阅读 M-07 `CodeMigrator_迁移计划生成器.md`、`CM-PLAN-001-对齐记录.md` 及项目进度/最新 session 记录。

## 3. 实施步骤

1. - [x] 建立 planning 任务详细设计，并冻结模块文件职责、输入输出模型和拒绝/零副作用边界（本目录详细设计 → D-01～D-06）。
2. - [x] 编写提案 schema、校验违规模型、规划输入和规模配置的 RED 测试；验证缺失实现时按预期失败（`tests/planning/test_models.py` → D-01/V-M07-V5-001）。
3. - [x] 实现封闭提案 schema 与模型级路径、局部引用、唯一性和工件策略校验；运行模型专项测试（`src/codemigrator/planning/models.py` → D-01/D-03）。
4. - [x] 编写四重护栏、边端点/语义、DAG、rank 拓扑和规模边界 RED 测试，包含相交范围经 `OrderedBefore` 也拒绝及拒绝零写入（`tests/planning/test_validation.py` → V-M07-V5-002/D-02/D-03/D-06）。
5. - [x] 实现确定性 `PlanValidator`、`PlanLedger`、UUIDv7 冻结和不可变 `FrozenPlan`；只在全部校验通过后持久化（`src/codemigrator/planning/validator.py` → V-M07-V5-002/003）。
6. - [x] 编写并实现 plan hash、integration key、组名规范化与 3 次反馈重试归约测试（`tests/planning/test_freeze.py`、`test_retry.py` → D-01/D-04）。
7. - [x] 编写并实现四类 Slice、三类工件、ambiguous 锚点/测试生成信息防火墙语义的纯函数测试（`tests/planning/test_derivation.py`、`src/codemigrator/planning/derivation.py` → V-M07-V4-012/013）。
8. - [x] 编写并实现 PSF-2 符号闭包→PSF-3 依赖闭包→Slice 映射的只读涟漪预览测试（`tests/planning/test_ripple.py`、`src/codemigrator/planning/ripple.py` → V-M07-V4-014）。
9. - [x] 导出 planning 正门、补充包 README/任务 PR 说明，核对未新增 planning→runtime/API 依赖（`src/codemigrator/planning/__init__.py`、`README.md`、`my_space/pr_md/` → M-01/D-05）。
10. - [x] 同步迭代记录和主任务表，执行专项/全量 pytest、Ruff、mypy、import-linter、compileall、diff check，并逐条核验 DoD。
11. - [ ] 提交并推送当前任务分支，创建 PR；只启动一次审查并完整等待审查 agent 终态，审查结束后在原分支一次性修复并直接合并，绝不对同一 PR 发起第二次审查。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| planning 专项规则测试 | `/home/xtc/env/codemigrator-plan/bin/pytest -q tests/planning` | 全部通过 | D-01～D-06、V-M07-V5-001～003、V-M07-V4-006/007/012～014 |
| 全量规则测试 | `/home/xtc/env/codemigrator-plan/bin/pytest -q` | 无回归 | 工程质量门 |
| 静态检查 | `/home/xtc/env/codemigrator-plan/bin/ruff check src tests`；`/home/xtc/env/codemigrator-plan/bin/mypy src` | 零错误 | M-01、代码质量 |
| 架构边界 | `/home/xtc/env/codemigrator-plan/bin/lint-imports --config pyproject.toml` | 0 broken contracts | M-01/D-05 |
| 编译与空白 | `/home/xtc/env/codemigrator-plan/bin/python -m compileall -q src tests`；`git diff --check` | 无错误/空白问题 | 工程质量门 |
| 真实模型测试 | 不执行 | planning 为纯逻辑，禁止无必要模型调用 | D-05 |

- 本任务不涉及沙箱/Git ref/恢复基础设施；在本机 WSL2 以确定性内存账本验证零副作用。

## 5. 风险与回滚

- **Blueprint 占位字段语义模糊**：只读取现有 `module_boundaries` 的目标路径前缀和布局原则可解释摘要；无可解释前缀的占位条目不臆造新字段，具体契约仍由 core 占位模型持有。
- **公共契约漂移**：完整计划 proposal/validation/edge/artifact 模型统一由 core 持有，planning 只引用并 re-export；若发现现有模型与对齐记录冲突，先停止实现并追加对齐记录，不私自复制定义。
- **UUIDv7 与 hash**：验证失败时不分配 SliceId、不调用账本写入；哈希只使用 core `canonical_json_bytes`，冻结对象通过深拷贝/只读访问防止漂移。
- **回滚方式**：保留任务分支提交历史；必要时使用 `git revert <commit>` 回退，不改写远程历史。

## 6. 收尾清单（对应 AGENTS.md §4 四件套）

- [x] 模块迭代记录已按模板生成于 `codemigrator_dev_progress/planning/`。
- [x] 主任务表 `CodeMigrator开发任务规划与进度跟踪.md` 已更新。
- [x] 详细设计文档已保存于 `codemigrator_design_doc/detailed_coding_design/planning/`。
- [x] 本实施计划已保存于 `Implementation_plan_doc/planning/`。
- [x] 对齐记录与架构文档当前无新增冲突；如实施发现冲突，先追加对齐变更并按 §3.4 同步。
