# Planning

`codemigrator.core` owns the canonical closed migration-plan proposal,
validation, edge-evidence, and core graph contracts. `planning` is their
deterministic, side-effect-free execution boundary: it validates proposals
against the frozen Spec, Blueprint, analysis facts, DAG/rank and scale limits;
only an accepted proposal is converted to UUIDv7-backed core slices and edges
in `FrozenPlan`.

The package deliberately does not call a model, schedule work, write a
repository, or intercept tools. Runtime owns the planner conversation and
physical retry policy; workspace owns write enforcement; verification owns
diagnostic attribution. `RipplePreview` is a read-only PSF-2/PSF-3 projection
for the runtime correction protocol.

## 负责

- core 提案契约的 planning re-export、机器护栏、冻结、plan hash、派生和只读涟漪预览。

## 不负责

- 模型会话、调度、目标仓库写入、运行期 scope 拦截、验证归因或数据库/API。

## 允许依赖

- `codemigrator.core` 公共契约和 `codemigrator.analysis` 冻结事实。

## 公共入口

- 从 `codemigrator.planning` 导入 proposal、validator、ledger、derivation 和 ripple API。
