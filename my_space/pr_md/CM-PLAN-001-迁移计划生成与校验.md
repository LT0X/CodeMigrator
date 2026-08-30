# feat: implement planning validation and freeze

- **关联任务编号**：CM-PLAN-001
- **关联模块迭代记录**：`my_space/codemigrator_dev_progress/planning/CM-PLAN-001-迁移计划生成与校验迭代记录.md`

## 背景

依据 M-07 V6 当前对齐段和 `CM-PLAN-001-对齐记录.md` D-01～D-06，交付规划期纯逻辑能力：Planner 提案须通过范围、Blueprint、源文件覆盖、DAG、rank 和规模护栏后才能自动冻结；失败路径不得产生部分计划。

## 变更点

- `src/codemigrator/planning/`：新增封闭 local-ref proposal schema、五值 edge provenance、四件冻结输入、结构化校验报告、UUIDv7 冻结、不可变 ledger、canonical plan hash 和配置化规模边界。
- `src/codemigrator/planning/derivation.py`：实现四类 Slice 双轨派生、三类 artifact action、GENERATED/信息防火墙元数据、唯一组名和符号锚点降级。
- `src/codemigrator/planning/ripple.py`：实现 PSF-2 符号引用闭包、PSF-3 反向依赖闭包、Slice 映射和只读 ImpactPreview。
- `src/codemigrator/planning/retry.py`：实现最多 3 次反馈重试、PlanFailed 零持久化和物理故障透传。
- `tests/planning/`：32 项模型、护栏、冻结、派生、涟漪、规模和重试契约测试。
- `my_space/`：同步实施计划、详细设计、迭代记录和主任务表。
- **公共契约影响**：不新增 core 公共枚举/字段，不改变既有 core `PlanEdge`；planning 只引用 core/analysis。

## 自测证据

- `/home/xtc/env/codemigrator-plan/bin/pytest -q tests/planning` → 32 passed。
- `/home/xtc/env/codemigrator-plan/bin/pytest -q` → 228 passed。
- `/home/xtc/env/codemigrator-plan/bin/ruff check src tests` → 通过。
- `/home/xtc/env/codemigrator-plan/bin/mypy src` → 通过。
- `/home/xtc/env/codemigrator-plan/bin/lint-imports --config pyproject.toml` → 0 broken contracts。
- `/home/xtc/env/codemigrator-plan/bin/python -m compileall -q src tests`、`git diff --check` → 通过。
- `V-M07-V5-001`～`V-M07-V5-003`、`V-M07-V4-006/007/012/013/014`、D-03/D-04/D-06：✓。
- 未调用真实模型：D-05 要求 planning 零模型调用。

## 风险与回滚

- Blueprint 目标路径前缀仍按 M-00 占位字段解释；字段扩展留给后续对齐，不在本 PR 臆造。
- 规划器不负责 runtime 调度、工具 scope 拦截、验证归因、联合域安全算法、PlanRevision 或目标路径写入。
- 回滚使用 `git revert`，不改写远程历史。
