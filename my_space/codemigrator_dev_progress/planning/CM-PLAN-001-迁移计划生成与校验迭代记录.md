# CM-PLAN-001-迁移计划生成与校验_迭代记录

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-PLAN-001
- **关联模块/文档**：M-07；`my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_迁移计划生成器.md`；`my_space/code_alignment_record/plan/CM-PLAN-001-对齐记录.md`

## 1. 变更动机

CM-PLAN-001 是 Wave 2 的迁移计划生成器任务。需要将已对齐的 D-01～D-06 落为 planning 纯逻辑：严格提案 schema、写范围/Blueprint/覆盖/DAG/rank/规模护栏、校验通过后的自动冻结、计划 hash、四类 Slice/三类工件派生、PSF-2/PSF-3 涟漪预览和 3 次反馈重试，并为后续 runtime/workspace/verification/git 提供不可变计划事实。

## 2. 变更内容

- **文档**：新增 `my_space/Implementation_plan_doc/planning/CM-PLAN-001-迁移计划生成与校验实施计划.md` 和 `my_space/codemigrator_design_doc/detailed_coding_design/planning/CM-PLAN-001-迁移计划生成与校验详细设计.md`。
- **模型与校验**：新增 `src/codemigrator/planning/models.py`、`validator.py`；local ref 提案、五值 provenance、ArtifactTask、四件冻结工件输入、结构化 violation、可配规模（100/500/200/2000）、UUIDv7 冻结、integration key/order、不可变 ledger 和 canonical plan hash。
- **派生与涟漪**：新增 `derivation.py`、`ripple.py`；实现四类 Slice 双轨语义、GENERATED/最低断言/信息防火墙元数据、三类工件动作、组名规范化、ambiguous/text-fallback 模块降级、符号引用→反向依赖→Slice 映射的只读预览。
- **重试**：新增 `retry.py`；schema/校验反馈初次尝试外最多重试 3 次，耗尽抛出 `PlanFailed` 且不持久化；`ProviderPhysicalFailure` 原样交回 runtime，不计反馈次数。
- **测试/入口**：新增 `tests/planning/` 32 项确定性契约测试与 planning README；planning 仅依赖 core/analysis，不接模型、runtime、API、文件系统或数据库。
- **关键实现决策**：严格沿用对齐记录 D-01～D-06；校验先于 ID 分配和 ledger 写入；相交 scope 即使存在 `OrderedBefore` 也拒绝；TestGeneration 的源模块不占翻译覆盖声明；ResourceFile 不得进入翻译 Slice scope；DAG 采用迭代式拓扑检测避免大提案递归深度风险。

## 3. 自测与验证结果

- planning 专项：`/home/xtc/env/codemigrator-plan/bin/pytest -q tests/planning` → `32 passed`。
- 全量规则：`/home/xtc/env/codemigrator-plan/bin/pytest -q` → `227 passed`。
- `/home/xtc/env/codemigrator-plan/bin/ruff check src tests` → 通过。
- `/home/xtc/env/codemigrator-plan/bin/mypy src` → 通过。
- `/home/xtc/env/codemigrator-plan/bin/lint-imports --config pyproject.toml` → 3 contracts kept, 0 broken contracts。
- `/home/xtc/env/codemigrator-plan/bin/python -m compileall -q src tests`、`git diff --check` → 通过。
- 验收条款：`V-M07-V5-001` ✓（结构化合法提案/四件输入/Contract 可为 0）；`V-M07-V5-002` ✓（范围、Blueprint、覆盖、环、规模及零 ledger 写入）；`V-M07-V5-003` ✓（自动 UUIDv7 冻结、integration order/hash、不可变访问）；`V-M07-V4-006` ✓；`V-M07-V4-007` ✓（测试→被测实现边禁用）；`V-M07-V4-012` ✓；`V-M07-V4-013` ✓；`V-M07-V4-014` ✓；D-03/D-04/D-06 ✓。
- 未执行真实模型测试：planning 明确为零模型调用纯逻辑，符合 D-05。

## 4. 影响面与风险

- 影响 `src/codemigrator/planning/`、`tests/planning/`、planning README 及本任务三份收口文档；主任务表已标记进行中，收尾时更新为已完成。
- 不修改 M-07 架构文档、core 字段、数据库、API、runtime 或目标仓库；边 provenance 作为 planning 冻结输出的并行审计事实保留，兼容现有 core `PlanEdge`。
- Blueprint 仍按 M-00 占位字段解释目标路径前缀，不臆造字段；联合域安全算法和验证归因 schema 继续由对齐记录指定的后续任务负责。

## 5. 后续行动

- [x] 完成 planning schema、机器校验、冻结/hash、派生、涟漪和重试的确定性测试与实现。
- [x] 完成专项与全量质量验证。
- [ ] 在当前分支提交并推送，创建 PR；只启动一次审查并完整等待终态。
- [ ] 对审查结论集中一次修复并直接合并；不启动第二次审查，随后主工作区拉取 `develop`。
