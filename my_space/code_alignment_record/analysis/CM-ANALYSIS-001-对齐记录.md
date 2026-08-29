# CM-ANALYSIS-001 对齐记录

> 用途：本文件是任务 `CM-ANALYSIS-001`（模块 M-06 代码分析与 AST 引擎）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-ANALYSIS-001` |
| 模块编号 | M-06 |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_代码分析与AST引擎.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 1 轮（Wave 1 轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/analysis/` 子包——源端结构事实唯一产地（机械完备层，M-00 信息分层原则）：

- **预索引前移管线（V6）**：项目注册/起草会话开启时（探索开始前）对冻结快照跑完整机械完备层管线，产出 F1-F4 与 PSF-2/3 投影；键 = `snapshot OID + 描述符摘要`，7 天可重建。CreateRun 复用投影（不重复解析）+ 冻结校验（投影描述符摘要与 Spec 锁定摘要断言）+ 档案一致性断言输入（`DOSSIER_INCONSISTENT` 语义归 CreateRun/CM-RUNTIME，本任务提供机械事实核对函数）。
- **四类事实**：F1 模块清单（ModuleFact/ExportSummary，module_boundary_strategy 三档 + 文件级降级）、F2 import 图（ImportEdge，四分派解析、Static/Unknown + 原因与证据、零启发式回退）、F3 测试清单与覆盖映射（CoverageEntry 三级派生 + ModuleCoverageStatus 三态 + TestConservationBaseline 守恒基线确定性计数）、F4 构建清单摘要与工件识别（ManifestSummary/ArtifactFact，按 artifact_rules 模式，零内建语言分支）。
- **PSF 三层**：PSF-1 语法森林（逐文件不可变 AST、进程内 LRU 键=snapshot OID+文件路径+grammar 摘要、容错解析 degraded_files）、PSF-2 项目索引（SymbolBinding/ReferenceSite 双向索引 + 符号级覆盖边 + 引用归属消解四规则：唯一绑定/同名多绑定 ambiguous/别名导入/通配不产出，宁缺勿误挂）、PSF-3 关系图（import 边/覆盖边/包含边复合图）。
- **PSF 物理投影**：SQLite 文件 + FTS5（D-01）；每投影一库，存 app 数据目录；`ProjectionStore` 端口（D-02）——analysis 产出内存事实结构、runtime 组合根实现物理写与读；写入失败重试 1 次后整体失败不存部分事实。
- **QuerySourceAst 导航服务**：八操作 closed-schema 判别联合（FindSymbol/GotoDefinition/FindReferences/FindCallers/FindCallees/FindImpact/SearchContext/ExtractSubtree）；上限 200 命中/256 KiB 文本/60 秒；`PATH_OUTSIDE_SNAPSHOT`/`QUERY_TIMEOUT`/`TRUNCATED`/`TEXT_FALLBACK_UNSUPPORTED` 边界码；经端口查 PSF-2 索引，子树提取按需解析 PSF-1。
- **tree-sitter 集成**：py-tree-sitter 进程内调用 + 崩溃捕获 + 每 grammar 熔断器（连续两次崩溃熔断 `ANALYSIS_INFRA_ERROR`）（D-03）；grammar 按 `grammar_sha256` 缓存句柄；单文件 64 MiB 门禁（`FILE_SKIPPED_TOO_LARGE` 不阻断）。
- **text-fallback 档**：无 grammar 语言正则级 import 提取 + 路径级模块清单；无 PSF-2 条目；capability=TextFallback 显式标注。
- **对抗审计框架**（D-04）：双怀疑派抽样规则（随机+对抗指派不相交）、双向 diff 结构、三轮规则状态机、审计记录 schema 与归档结构；评审会话编排归 CM-DRAFT-001（本任务只交付框架与记录结构）。
- **修订与重分析**：仅 PlanRevision 改变分析输入或更换源基线触发整批重建替换，新旧投影按修订标识并存。

### 1.2 边界（不做什么）

- 不做语义消解/模块边界裁决（机械候选 ≠ 语义权威；最终权威=用户确认的 UnderstandingDossier/TargetProjectBlueprint）。
- 不产出任何目标代码/目标命令（P-01；目标语义归 EXECUTE）。
- 不做 CreateRun 预检编排与 DOSSIER_INCONSISTENT 拒绝流程（归 CM-RUNTIME；本任务提供一致性核对纯函数）。
- 不实现 ProjectionStore 的 SQLite 物理读写（归 CM-RUNTIME 组合根，D-02）。
- 不实现 QuerySourceAst 工具注册/frame/hook（M-12 归 CM-WORKSPACE-001；本任务交付服务行为与查询 API）。
- 不实现对抗审计的评审会话编排（归 CM-DRAFT-001，D-04）。
- 不碰网络/数据库（PG）/源项目写入/检查命令执行（M-01 analysis 禁止事项）；分析全程源快照零写句柄。
- 不引入原生语义 AST 引擎（tsc/gopls 类，M-06 AST/CST 选型边界）；不做 AST 改写/pretty-printer/落盘序列化（V3 定位器体系零残留）。
- 不消费计划/Slice（只供事实输入）。

### 1.3 产出物

`src/codemigrator/analysis/`（流水线、F1-F4 提取器、PSF-2 索引构建、QuerySourceAst 服务、grammar registry+熔断、text-fallback 档、审计框架、ProjectionStore 端口）；runtime 侧端口契约（stub 先行）；tests/analysis/（提取规则用例+金标准 fixture）+ tests/contracts/（重建逐字节等价）；`test_fixtures/clickvideo-analysis/` 金标准（V-M06-V4-019）；模块迭代记录。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | PSF 投影物理存储（M-06 明示开放项；对齐中先澄清 PSF 作用后裁决） | SQLite 文件+FTS5 / PostgreSQL 表 / 纯内存缓存 | **SQLite 文件+FTS5**：每投影一库（键=snapshot OID+描述符摘要，存 app 数据目录）；FTS5 支撑 SearchContext；7 天清理=删文件；与 PG run ledger 解耦 | 对齐问答 Q-01/Q-05（2026-08-29） |
| D-02 | 投影物理写入与查询落点（M-01 层规则：执行层唯一碰文件写） | 端口注入 runtime 写 / analysis 直写 / 全归 runtime | **analysis 产出内存事实结构（纯函数），物理读写经 `ProjectionStore` 端口由 runtime 组合根实现**；QuerySourceAst 查询服务在 analysis 经端口读 | 对齐问答 Q-02/Q-05 |
| D-03 | tree-sitter 调用隔离（M-01 明示可选子进程硬隔离） | 进程内+熔断 / 一次性子进程 | **进程内 + 崩溃捕获 + 每 grammar 熔断器**（连续两次崩溃→`ANALYSIS_INFRA_ERROR`，app 存活） | 对齐问答 Q-03 |
| D-04 | 完备性对抗审计交付归属 | 框架归本任务+执行归起草 / 全押后 / 全归本任务 | **框架归本任务**（双怀疑派抽样规则/双向 diff 结构/三轮规则/审计记录 schema）；**评审会话编排归 CM-DRAFT-001** | 对齐问答 Q-04 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（ProjectModuleId/RepoRelativePath/ArtifactKind/ModuleBoundaryStrategy/SliceKind 消费面/SourceToolchain/TreeSitterGrammarRef/ManifestParserRef）；Spec 范围与描述符锁（CM-SPEC-001）经 registry 端口（CM-SPEC-001 D-03）取得 grammar 与解析器。
- 冻结 base snapshot（ProjectSnapshotId + commit OID，快照冻结归项目注册流程/CM-RUNTIME）。

### 3.2 下游消费

- M-07（PSF-3 关系图→Planner 事实输入；导出摘要/构建摘要→契约参考）、M-10（覆盖图→P-09 归因；守恒基线→结构守恒；PSF-2→符号级归因）、M-07/M-10（PSF-2→测试生成锚点/涟漪影响面）、M-05（F4 工件分类→能力门工件声明消费）、M-04/M-12（QuerySourceAst 行为→工具注册）、M-14（事实摘要→组包，不得自行重解析）、M-13（两指标：Unknown 边率/degraded_files 计数）。

### 3.3 跨模块接口边界

- **投影存储端口**：`ProjectionStore` Protocol 归 analysis 定义（读/写/重建/清理语义：写入失败重试 1 次整体失败、重建期间消费方等待不读半成品）；SQLite 实现归 CM-RUNTIME 组合根（Wave 1 用经测试冻结的 stub）。
- **三时点共享**：起草探索/PLAN/EXECUTE 消费同一投影（索引一次处处复用）；CreateRun 断言不重跑机械管线。
- **`ANALYSIS_INFRA_ERROR`/`AnalysisFailed`**：快照读取不可用/门禁断言失败/投影最终写入失败→Run 以 AnalysisFailed 进 FAILED（语义归 M-00/CM-RUNTIME，本任务产出触发事实）。
- QuerySourceAst 八操作 closed-schema 与错误码（PATH_OUTSIDE_SNAPSHOT/QUERY_TIMEOUT/TRUNCATED/TEXT_FALLBACK_UNSUPPORTED）为 M-12 工具注册的行为依据。
- 审计记录随计划证据归档（消费 M-13/M-15 证据链）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M06-V4-001（追溯） | 源快照写句柄 0；四类事实+PSF-2 全在可重建投影 | 分析单测（文件系统监控断言） |
| V-M06-V4-002 | 删投影后按 commit OID+描述符摘要重建逐字节相等 | 契约测试（经 stub store：重建→canonical 比对） |
| V-M06-V4-003 | Unknown 边全带原因与证据；不出现在 Requires 派生 | 提取器单测（动态 import/反射/路径失败用例） |
| V-M06-V4-004 | 测试文件全覆盖映射条目；守恒基线随 F3 产出 | 提取器单测 |
| V-M06-V4-005/006 | closed-schema 拒写语义/自由正文；60s 超时/200 条/256 KiB 截断 | 服务单测 |
| V-M06-V4-007 | 快照外路径 PATH_OUTSIDE_SNAPSHOT；候选/verified 不接受查询输入 | 服务单测 |
| V-M06-V4-008 | text-fallback 正则级提取+降级标注；符号级操作 TEXT_FALLBACK_UNSUPPORTED | 提取器单测（无 grammar 语言用例） |
| V-M06-V4-009 | ERROR/MISSING 容错提取入 degraded_files；>64 MiB 跳过不阻断 | 提取器单测 |
| V-M06-V4-010 | 同 snapshot+同 grammar 两次分析 canonical 逐字节相等 | 契约测试（确定性断言） |
| V-M06-V4-011 | 零 AST 改写/pretty-printer/落盘序列化/V3 定位器残留 | 静态扫描审查 |
| V-M06-V4-012 | 7 天留存期满清理；清理后可凭冻结 commit 重建证据 | 契约测试（清理→重建，SQLite stub） |
| V-M06-V4-013 | PSF-2 确定性派生；双向查询结果一致；重建相等 | 契约测试 |
| V-M06-V4-014 | 符号级操作经索引执行结果与按需计算一致；接口不变 | 服务单测（双路径比对） |
| V-M06-V4-015 | 符号级覆盖边升级；模块级覆盖图零丢失 | 提取器单测 |
| V-M06-V4-016 | EmptyTestSuite/Undetermined 可区分；EmptyTestSuite 进测试生成派生输入 | 提取器单测 |
| V-M06-V4-017 | 工件识别仅按 artifact_rules；零内建语言分支 | 提取器单测（三类工件用例） |
| V-M06-V4-018 | 引用归属四规则（ambiguous 标记/通配不产出/宁缺勿误挂）；持久化重建一致 | 提取器单测+契约测试 |
| V-M06-V4-019 | 金标准双层验收（click-video fixture）：候选集召回 100%、Static 误报 0；档案条目抽查 | fixture 回归测试（`test_fixtures/clickvideo-analysis/`） |
| V5 增量 | 零写入/可重建/QuerySourceAst 带锚点结果/Unknown 入提案事实 | 上述覆盖 |
| V6 时序（内嵌） | 预索引前移至项目注册/起草开启；CreateRun 断言不重解析 | 时序单测（注册后投影可用、CreateRun 零解析调用计数） |
| D-03 熔断 | 连续两次 grammar 崩溃熔断，app 存活其他 Run 不受影响 | 注入崩溃用例单测 |

## 5. 风险与注意点

- **SQLite 实现依赖注入时序**：Wave 1 无 runtime 实现，stub 必须锁行为（写入失败重试/整批原子/清理语义）；CM-RUNTIME 实现就绪后替换对齐（并行纪律 2）。
- **py-tree-sitter 与 grammar .so 兼容**：grammar 制品 ABI 版本须与 py-tree-sitter 版本匹配（CM-INFRA 构建时锁定；升级即破坏性变更须重制描述符 grammar 摘要）。
- **FTS5 可用性**：Python stdlib sqlite3 编译需带 FTS5（Ubuntu python3.12 自带；仍需 CI 断言 `fts5` compile option）。
- **canonical 化逐字节等价的实现口径**：投影序列化须自定义 canonical 规则（键序/数值/数组序固定），V-M06-V4-002/010/013 三条款共用同一 canonical 函数——放 analysis 内（与 CM-SPEC 的 JCS 是两套场景，Spec 用 RFC 8785，投影用自定义二进制/表格 canonical，勿混用）。
- **同名多绑定 ambiguous 传播纪律**：ambiguous 标记必须在 M-07 涟漪/M-10 归因/测试生成锚点三处下游显式降级——跨任务交叉验收点（本任务产出标记，消费方任务负责降级路径）。
- **零模型裁量红线**：F1-F4/PSF-2 全部确定性代码路径；对抗审计的评审会话是运营流程（起草期），不得反向进入机械管线。
- **snapshot 冻结键**：投影键 = snapshot OID + 描述符摘要——与 M-06「7 天；按冻结 commit 重建」一致；实现勿引入额外键维度（如时间戳）破坏重建等式。
- 审计框架本任务只交付结构与规则状态机；两派评审上下文隔离的具体会话机制归 CM-DRAFT-001（届时对齐）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | PSF 投影物理存储引擎（开放项） | （先要求澄清 PSF 作用与重建必要性）经解释后裁决：SQLite 文件+FTS5 |
| Q-02 | 投影物理写入与查询服务落点 | （同上先澄清）analysis 纯产出 + ProjectionStore 端口 + runtime 物理实现 |
| Q-03 | tree-sitter 调用隔离模式 | 进程内 + 崩溃捕获 + 每 grammar 熔断器 |
| Q-04 | 完备性对抗审计交付归属 | 框架归本任务；评审会话编排归 CM-DRAFT-001 |
| Q-05 | （用户主动提问）PSF 的作用是什么、是否需要重建、个人项目是否有更优雅高效做法 | 澄清：PSF 投影=确定性派生的分析缓存（非真相，真相在 Git+PG），三阶段高频消费避免重复解析，可重建故可低价值留存；备选纯内存缓存（零端口零清理但重启重算）；用户最终选 SQLite 落盘+端口 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-06 V6 收敛版设计文档（PSF/F1-F4/QuerySourceAst/时序重定义节）与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-04（含 PSF 作用澄清后对 M-06 开放项的正式裁决） |
