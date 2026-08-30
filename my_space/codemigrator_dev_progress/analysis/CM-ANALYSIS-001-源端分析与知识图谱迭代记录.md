# CM-ANALYSIS-001-源端分析与知识图谱_迭代记录

> 本记录按 `CodeMigrator迭代记录模板.md` 维护，记录 M-06 源端机械分析、PSF、QuerySourceAst 与端口实现。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-ANALYSIS-001
- **关联模块/文档**：M-06、`feature/analysis-graph`、`my_space/code_alignment_record/analysis/CM-ANALYSIS-001-对齐记录.md`

## 1. 变更动机

CM-SPEC-001 已合并，Wave 1 下一项需要把冻结源快照转换为可复用的机械结构事实，支撑起草期预索引、Planner 与验证下游。实现必须遵循 M-06 的源只读、零模型裁量和可重建边界。

## 2. 变更内容

- 已实现：F1-F4、PSF-1/2/3、QuerySourceAst、grammar LRU/熔断、text-fallback、审计框架与 ProjectionStore 端口；分析继续只产出内存事实，物理 SQLite+FTS5 留给 runtime。
- 已实现：`tests/analysis/` 规则/契约测试，覆盖确定性、路径边界、Unknown/ambiguous、目录覆盖降级、PSF-3 方向和投影重试。
- 已同步：主任务表中 CM-SPEC-001 PR #3 合并事实，并登记 CM-ANALYSIS-001 分支与范围。

## 3. 自测与验证结果

- 当前阶段：实现与本地收口完成，等待一次 PR 审查及合并。
- DoD：分析定向测试 25 passed；全量 pytest、import-linter、Ruff、mypy、compileall、diff check 已通过（最终命令输出在 PR 前再次刷新）。
- 真实模型测试：不需要；分析层必须保持确定性。

## 4. 影响面与风险

- analysis 只能读取冻结源快照，输出内存事实；ProjectionStore、grammar 文件/镜像事实和物理持久化均经端口交接。
- 语言差异只能来自描述符声明；动态依赖/歧义宁缺勿误并保留证据，不能用启发式静默补边。
- PSF 是可重建缓存而非业务真相；M-07/M-10 必须保留 Unknown/ambiguous/Undetermined 降级语义。

## 5. 后续行动

- [x] 先写失败测试覆盖事实模型、只读/大小门禁、导入可信度、覆盖守恒、PSF、QuerySourceAst、端口和审计状态机。
- [x] 完成实现与全量验证，按 V-M06 条款逐条记录。
- [ ] 创建 PR 后只启动一次独立审查并等待终态；反馈只在原分支修复一次后直接提交、推送、合并，不追加审查。

## 6. 附录

- 上游 develop 合并基线：CM-SPEC-001 PR #3，`857590e`。
