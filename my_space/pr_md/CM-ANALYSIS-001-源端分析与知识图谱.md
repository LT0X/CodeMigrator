# feat: implement source analysis graph

- 任务：`CM-ANALYSIS-001`
- 模块：M-06 `codemigrator.analysis`
- 分支：`feature/analysis-graph`
- 迭代记录：`my_space/codemigrator_dev_progress/analysis/CM-ANALYSIS-001-源端分析与知识图谱_迭代记录.md`

## 背景

依据 M-06 设计文档的 F1-F4、PSF-1/2/3、QuerySourceAst 与预索引时序要求，实现冻结源快照上的确定性机械分析层。对齐记录为 `CM-ANALYSIS-001-对齐记录.md`，覆盖 V-M06-V4-001～019；本 PR 不实现 runtime 的 SQLite+FTS5 物理投影、CreateRun、工具注册或下游 Planner/验证编排。

## 变更点

- `src/codemigrator/analysis/`：新增冻结事实模型、描述符规则、F1-F4 分析管线、AST 适配/缓存/熔断、PSF 索引/关系图、实际引用与调用边、只读查询、快照与 ProjectionStore 端口、三轮终态对抗审计框架。
- `tests/analysis/`、`tests/contracts/`、`test_fixtures/clickvideo-analysis/`：新增确定性、只读、AST 节点、实际引用/调用、路径/大小门禁、Unknown/ambiguous、覆盖降级、重建、查询硬上限/截断、投影重试、审计终态和金标准回归测试。
- `pyproject.toml` 与对应基线测试：修正 import-linter 层级顺序，使 `core` 作为最低公共契约层，符合 M-01 的 `analysis → core` 依赖方向。
- 公共契约：复用 `codemigrator.core` 的 ID、路径、工件枚举和稳定错误码；新增分析层模型与查询 closed-schema，不修改既有 core 定义。

## 自测证据

- 唯一一次独立审查返回 `REQUEST_CHANGES`；已在原分支一次性修复，未追加审查。
- `uv run --frozen pytest -q` → `151 passed`
- `uv run --frozen lint-imports` → `3 contracts kept, 0 broken`
- `uv run --frozen ruff check src tests` → passed
- `uv run --frozen mypy src` → `Success: no issues found`
- `uv run --frozen python -m compileall -q src tests` → passed
- `git diff --check` → passed
- `uv run --frozen pytest tests/analysis tests/contracts -q` → `44 passed`
- 真实模型、Docker、沙箱和 PG：按任务边界不适用；分析实现保持确定性和源快照零写入。

## 风险与回滚

- 当前 grammar/parser 为端口注入；runtime 组合根接入实际 tree-sitter grammar 与 SQLite+FTS5 实现时，须保持 snapshot OID + 文件路径 + grammar 摘要键及整批写入语义。
- Unknown、ambiguous、EmptyTestSuite/Undetermined 均保留在事实结果中，由 M-07/M-10 按对齐记录执行降级消费。
- ReferenceSite 使用实际符号使用点，CallEdge 使用实际 caller/callee 范围；查询超过固定契约上限或返回内容超限时显式返回 `TRUNCATED`。
- analysis projection 使用独立 canonical 规则，ProjectionStore 通过 rebuild 原子发布；Audit `CLEAN`/`ESCALATE` 为终态。
- 如需回滚，使用本分支提交的 `git revert`；不直接改写 `develop`。
