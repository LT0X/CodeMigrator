# CM-ANALYSIS-001-源端分析与知识图谱详细设计

> 本设计依据 M-06《代码分析与 AST 引擎》与 `my_space/code_alignment_record/analysis/CM-ANALYSIS-001-对齐记录.md`。analysis 是源端结构事实唯一产地：确定性、只读、可重建；runtime 仅通过端口承接物理投影与资源 I/O。

## 0. 任务边界

- **交付**：冻结快照读取抽象、F1-F4 事实模型与提取器、PSF-1/2/3 结构、QuerySourceAst 八操作服务、grammar registry/cache/熔断、text-fallback、ProjectionStore 与审计框架端口。
- **不交付**：目标代码/命令、源快照写入、网络/PG、SQLite 物理 ProjectionStore、M-12 工具注册、CreateRun 预检、DRAFT 评审会话、Planner/Slice/验证消费。
- **上游**：CM-CORE 的 ID/枚举/错误码/描述符模型，CM-SPEC 的范围与描述符锁定事实。

## 1. 公共事实模型

所有模型继承 `CoreModel(extra="forbid")`，输入不接受自由命令、prompt 或候选工作区字段。路径只接受 repository-relative POSIX 形式；结果按 UTF-8 字节排序。

- `SourceRange`：文件路径、起止行列；所有导航命中必须有 file:range。
- F1：`ModuleFact`、`ExportSummary`，记录模块 id、文件路径、Source/Test 角色、Manifest/Directory/File 边界、导出摘要、Full/TextFallback 能力与 degraded 文件。
- F2：`ImportEdge`，记录 from 模块、Module/External 目标、Static/Unknown 可信度、Unknown 原因和证据范围；动态/反射/无法解析不猜目标。
- F3：`CoverageEntry`、`ModuleCoverageStatus`、`TestConservationBaseline`，测试识别遵循描述符声明；ImportGraph 优先、DirectoryConvention 降级、Uncovered 保留；Covered/EmptyTestSuite/Undetermined 三态不混淆。
- F4：`ManifestSummary`、`ArtifactFact`，manifest 依赖/脚本/入口为摘要；工件分类完全由描述符 `artifact_rules` 模式驱动，不写语言分支。

## 2. 分析管线与确定性

1. 输入只读 `SnapshotSource`，枚举路径并拒绝候选/verified；单文件超过 64 MiB 标记跳过并记录 `SOURCE_FILE_TOO_LARGE`，不阻断全仓。
2. `GrammarRegistry` 按 snapshot OID、文件路径和 grammar SHA 使用有界 LRU 缓存解析器；`ParserCircuitBreaker` 对每 grammar 连续两次崩溃熔断并返回 `ANALYSIS_INFRA_ERROR`，其他 grammar 不受影响。含 ERROR/MISSING 的树仍尽力提取并记入 degraded_files。
3. F1/F2/F4 确定性提取后派生 F3、PSF-2、PSF-3；不使用模型，不做 AST 改写、pretty-printer 或 AST 落盘序列化。
4. text-fallback 只按描述符提供的正则/目录模板产出路径级 F1/F2/F3/F4，能力显式为 TextFallback，不生成 PSF-2，符号查询返回 `TEXT_FALLBACK_UNSUPPORTED`。
5. `AnalysisResult` 包含 snapshot OID、descriptor/grammar 摘要、事实和 canonical bytes；所有集合按稳定键排序，重建同键逐字节相等。ID、路径、工件类型和稳定错误码均引用 `codemigrator.core` 的唯一公共契约，不在 analysis 复制定义。

## 3. PSF 三层

- PSF-1 是进程内不可变解析树缓存，键为 snapshot OID + 文件路径 + grammar 摘要，采用有界 LRU，不写入投影。
- PSF-2 是 F1+F2 派生的 SymbolBinding/ReferenceSite 双向索引。唯一绑定归属；同名多绑定标记 ambiguous；别名按 import 目标归属；通配导入和成员表达式不产出符号级条目，宁缺勿误；import 规则显式携带导入符号以避免整模块误挂。
- PSF-3 是 import/coverage/containment 三类模块级复合图；包含边以 `file_path → to_module` 表达，覆盖边方向为测试模块到被测源模块；Unknown 边保留证据，不自动成为 Requires。

## 4. QuerySourceAst

采用 discriminator=`kind` 的八变体 closed-schema：FindSymbol、GotoDefinition、FindReferences、FindCallers、FindCallees、FindImpact、SearchContext、ExtractSubtree。请求 extra-forbid，拒绝自由 query/写字段；仅查源快照与 PSF 端口。路径越界抛出带 `PATH_OUTSIDE_SNAPSHOT` 的边界错误，单次 60 秒超时抛出 `QUERY_TIMEOUT`，命中超过 200 或文本超过 256 KiB 返回截断标记；text-fallback 的符号级操作返回 `TEXT_FALLBACK_UNSUPPORTED`。

## 5. 端口与物理边界

- `ProjectionStore`：write/read/rebuild/cleanup 端口；runtime 实现 SQLite+FTS5，每投影独立文件，写失败重试一次且整体原子，不读半成品，7 天后清理。
- `SnapshotSource`：只读路径/字节端口；分析层不取得写句柄。
- `GrammarProvider`：按 grammar id/SHA 获取解析器或 text-fallback 能力；analysis 不扫描宿主资源。
- 端口只描述事实和生命周期，不在本任务实现 SQLite、PG、文件扫描 registry 或工具注册。

## 6. 对抗审计框架

只交付确定性的 `AuditSample`、`AuditDiff`、`AuditRound`、`AuditRecord` 结构和最多三轮状态机：随机样本与对抗样本不相交；双向 diff；规则修订而非修样本；三轮失败/同类复发升级用户。评审会话及上下文隔离归 CM-DRAFT。

## 7. 验收映射

| 条款 | 本设计验证 |
| --- | --- |
| V-M06-V4-001/002/010/012 | SnapshotSource 只读、ProjectionStore 端口、稳定 canonical/rebuild/cleanup 契约 |
| V-M06-V4-003/004/009/015/016/018 | F2/F3/PSF-2 确定性提取、Unknown/ambiguous、降级和守恒模型 |
| V-M06-V4-005/006/007/008/014 | QuerySourceAst closed-schema、路径/超时/上限/text-fallback 服务 |
| V-M06-V4-011/017 | 静态边界扫描、artifact_rules 驱动且无语言分支 |
| V-M06-V4-019 | click-video fixture 接口与机械候选集/锚点审计结构；具体 fixture 随资源落盘 |

## 8. 偏差与交接

本任务不修改 M-06 架构模块设计。物理 SQLite/FTS5 由对齐 D-01 锁定但实现归 runtime；analysis 只提供端口。M-07/M-10 消费 Unknown/ambiguous/EmptyTestSuite 时必须保留降级事实。
