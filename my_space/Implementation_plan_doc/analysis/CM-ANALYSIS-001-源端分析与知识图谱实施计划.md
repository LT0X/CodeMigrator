# CM-ANALYSIS-001-源端分析与知识图谱实施计划

> 本计划依据 M-06《代码分析与 AST 引擎》及 `my_space/code_alignment_record/analysis/CM-ANALYSIS-001-对齐记录.md`。本任务交付 analysis 纯事实层、PSF 结构、QuerySourceAst 服务行为与 ProjectionStore/grammar 端口；不越界实现 runtime 的 SQLite 物理读写、Git/PG、工具注册或 CreateRun 编排。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-ANALYSIS-001
- **所属模块/crate**：M-06 / `codemigrator.analysis`
- **关联交付物**：详细设计 `my_space/codemigrator_design_doc/detailed_coding_design/analysis/CM-ANALYSIS-001-源端分析与知识图谱详细设计.md`、迭代记录 `my_space/codemigrator_dev_progress/analysis/CM-ANALYSIS-001-源端分析与知识图谱迭代记录.md`

## 1. 目标与范围

- 完成定义（DoD）：以确定性、只读、可重建的分析管线产出 F1-F4、PSF-1/2/3、有限 QuerySourceAst 查询服务、text-fallback、grammar 熔断与审计框架端口；覆盖 V-M06-V4-001～019 中本任务可独立验收的契约，并通过全量测试、import-linter、Ruff、mypy、compileall。
- 范围排除项：不写源快照，不执行目标代码/检查命令，不访问网络/PG，不实现 ProjectionStore SQLite 物理层、M-12 工具注册、CreateRun、对抗评审会话编排及 M-07/M-10 下游消费。

## 2. 前置条件

- [x] 工作分支：从已合并 `develop` 切出 `feature/analysis-graph`。
- [x] 环境就绪项：复用 `/home/xtc/env/codemigrator-infra/` 的 Python 3.12/uv 锁定环境；本任务不需要真实模型或 Docker。
- [x] 上游依赖：CM-CORE-001、CM-INFRA-001、CM-SPEC-001 已合并至 `develop`。
- [x] 设计文档：已完整阅读 M-06 架构设计与 CM-ANALYSIS-001 对齐记录；本任务详细设计随本计划建立。

## 3. 实施步骤

1. - [x] 同步主任务表中 CM-SPEC-001 合并事实并登记本分支范围（主表 §1.4/§6.2/§7.3/§11 → V6 进度一致性）。
2. - [x] 先建立 F1-F4、PSF、SourceRange/Query closed-schema、端口与错误语义的确定性失败测试（`tests/analysis/`、`tests/contracts/` → V-M06-V4-001～019）。
3. - [x] 实现只读快照抽象、文件大小门禁、确定性 canonical 事实模型和 F1 模块清单/导出摘要。
4. - [x] 实现 F2 import 四分派与 Static/Unknown 证据、F3 测试识别/覆盖/守恒基线、F4 manifest 摘要/描述符 artifact_rules 分类。
5. - [x] 实现 PSF-1 grammar parser/cache 熔断、PSF-2 符号索引/引用归属、PSF-3 复合图与 text-fallback 降级。
6. - [x] 实现 QuerySourceAst 八操作 closed-schema、上限/超时/路径安全/索引查询与 ProjectionStore/grammar 端口；不实现 runtime 物理 I/O。
7. - [x] 实现双怀疑派审计记录结构与终态三轮规则状态机，补齐重建/留存/零写入契约测试及 click-video fixture 金标准回归。
8. - [x] 根据唯一一次审查反馈在原分支完成一次性修复：AST 节点提取、实际引用/调用边、查询契约边界、投影重建端口、analysis canonical 与审计终态。
9. - [x] 更新详细设计、迭代记录、主任务表、PR 说明，完成全量质量门并复核分支只含本任务及必要进度同步。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| 分析契约测试 | `uv run --frozen pytest tests/analysis tests/contracts -q` | 44 passed（含 AST、实际引用/调用边、路径、熔断、PSF-3、降级、重建和 fixture 回归） | V-M06-V4-001～019 |
| 全量规则测试 | `uv run --frozen pytest -q` | 151 passed | M-00/M-06 |
| import-linter | `uv run --frozen lint-imports` | 3 contracts kept，0 broken；层级顺序为 runtime→…→core | M-01 |
| 静态质量 | `uv run --frozen ruff check .`、`uv run --frozen mypy src` | 零错误 | 工程质量门 |
| 编译与差异 | `uv run --frozen python -m compileall -q src tests`、`git diff --check` | 零错误、无空白问题 | AGENTS.md §4 |

- 沙箱/Git/PG/真实模型验证：不适用；分析层必须保持纯函数和源快照零写入。

## 5. 风险与回滚

- 风险点及缓解：语言差异只能来自描述符，不写语言分支；未知 import 宁缺勿误并保留证据；tree-sitter 崩溃由 grammar 级熔断器隔离；投影只经端口，不在 analysis 直写文件；所有输出排序并 canonical 化以保证重建等价。
- 回滚方式：仅在 `feature/analysis-graph` 使用 `git revert`；不重置 `develop`，不删除主工作区用户修改。

## 6. 收尾清单

- [x] 模块迭代记录已按模板生成于 `my_space/codemigrator_dev_progress/analysis/`
- [x] 主任务表已同步开工与上游合并事实
- [x] 详细设计文档已保存于 `my_space/codemigrator_design_doc/detailed_coding_design/analysis/`
- [x] 本实施计划已保存于 `my_space/Implementation_plan_doc/analysis/`
- [x] 若设计与对齐结果有差异：以对齐记录为准，analysis canonical 明确独立于 Spec JCS；架构模块设计无新增偏差
