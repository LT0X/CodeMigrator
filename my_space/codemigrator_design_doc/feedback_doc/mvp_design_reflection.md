# CodeMigrator MVP 实践反思与设计迭代建议

> 审计基准：以磁盘现状为准（src 20 文件 / 3,089 行 TS + tools/pytest_lite.py 86 行；运行现场 `.codemigrator/runs/01a03318-3e16-7aa2-8bf2-38474872780c`；`full-run.log`）。
> 设计基线：`mvp_design_contract.md`、`ts_mvp_contract_extract.md`（V4 契约提取），必要时对照 M-00/M-07/M-08/M-10/M-12 原文。
> 标注约定：〔盘证〕=文件:行可复核；〔会话〕=现场已被后续运行覆盖、仅会话内可考（均已注明被清理原因）。

## 1. 实际运行事实总览

共发起 6 次运行尝试，终态分布：2 次未进管线（环境）、1 次 ANALYZE 后快速失败、2 次手动终止于 EXECUTE、1 次进行中（审计时点）。无一次假 COMPLETED——所有失败都被状态机/门禁如实投影为 FAILED 或人工干预，这本身是有效机制的证据。

| # | 运行 | 终态 | 根因分类 | 证据 |
|---|---|---|---|---|
| 1-2 | pwsh-2/3 | 未进管线 | A 环境：PS5.1 读 BOM-less UTF-8 中文注释按 ANSI 解码吞换行，赋值行并入注释 → LLM env 未加载 | 〔会话〕load-env.ps1 现为纯 ASCII（脚本首行注释自述） |
| 3 | pwsh-4 | 手动终止(EXECUTE) | B 模型设施：单次调用挂起至 240s×3 重试；且档案 JSON 形状不符静默 fallback | 〔会话〕修复痕迹可见：client.ts 超时参数化、session.ts:201-211 errStreak、dossier.ts catch 现输出 stderr |
| 4 | （审查后未启动即杀） | 未运行 | D 实现 bug：对抗性审查 19 条（3×P0），其中 checkpoint 全量镜像越界属"必现级"，启动必失败 | 〔盘证〕审查结论已落进实现：workspace.ts:97-115 双参 checkpoint |
| 5 | pwsh-6 | 手动终止(EXECUTE) | 混合：契约 gen0 失败→定向重生成 gen1 正常续跑（机制首次实战生效） | 〔会话〕日志当时含 `REGENERATING (gen=0)`→`RUNNING (gen=1)` |
| 6 | 01a03318 | 进行中(EXECUTE) | 首次全绿前段：LLM 档案成功 + 同代重派一次 | 〔盘证〕dossier.json `"provenance": "llm-reasoning"`、语义组 money-core/pricing-tiers/cart-model/checkout-flow/slug-utils；full-run.log 09:29:03 档案冻结→11 slices DAG 冻结→09:42:15 契约 RUNNING(gen=0) 重复出现=同代重派 |

失败根因分类频次：A 环境 2 ｜ B 模型设施 ≥2 ｜ C 协议（JSON 动作不可用，见 F-01）1 类系统性 ｜ D 实现 bug 19 条（审查期捕获，未烧运行成本）｜ E 设计语义缺口 6 处（§3）｜ F 数据问题 1 例（F-08）。

## 2. 被实战验证有效的设计机制

以下 V4 机制在真实故障中"救了场"，应视为已验证资产，正式版不要动其语义：

1. **write scope 三冻结 + diff 校验面**（tools.ts:171-199 gateWrite；workspace.ts:97-115 只校验 changed 集）。越界写入零副作用，且把校验对象定义为"相对种子的变更集"后，非契约 Slice 不再被自身基线误杀。
2. **expected-OID CAS + intent 先落账**（workspace.ts:131-136；run.ts:398-413 intent append 先于 advanceVerified）。并发候选下 verified 单线推进无一例分叉。
3. **generation 0..=2 定向重生成 + 前代诊断注入**（run.ts:339-374；pack 注入 priorDiagnostics run.ts:276）。pwsh-6 中契约 gen0 失败后 gen1 自动携带诊断续跑，无需人工。
4. **三层验证分层**（局部语法 LocalCandidate / 集成编译 ProspectiveIntegration / 最终 TEST 全集 FinalVerified，run.ts:309-331、391-395、510-518）。免费慢端点下该分层把最贵的测试执行收敛到一次终验，成本结构正确。
5. **描述符=数据**（descriptor.json 的 SCAFFOLD/COMPILE/TEST 模板 + pytest_lite.py 替换真实 pytest）：主链零改动换掉测试执行器，P-08 得到最强实证。
6. **事件信封版本化规则**（M-02："新增 type 不构成版本变更"）：MVP 全程只加不改 envelope（analyze.*、plan.* 等 MVP 扩展类型），规则确实免掉了协议升级负担。
7. **Context Pack 注入冻结目标映射**（run.ts:651-668 targetMappingFor；session.ts:90-94）：给出 `源→目标` 路径对照后，测试翻译的 import 不再臆测——这是"知识经由正门进入会话"的直接收益案例。
8. **heuristic 兜底档案**（dossier.ts heuristicDossier + provenance 标注）：LLM 缺席时 ANALYZE/PLAN 仍可确定性演示，信息分层原则的 text-fallback 档名副其实。

## 3. 设计落地摩擦点

格式：[编号] 描述｜证据｜章节｜定性｜建议。

**[F-01] 单 JSON 动作协议对多行文件内容系统性失败。**
模型不转义 `\n` 内嵌内容，write_file 内容段必然截断/损坏；改 `[cm:*]` 标记分段协议（session.ts:36-50 定义、121-172 parseAction，内容段逐字保留免转义）+ session_probe.ts 探针验证后一次通过。｜M-12 只定义了网关侧工具语义，未定义"模型↔Harness 动作编码"；这是全文最大的欠设计点。｜**欠设计**。｜改文档：M-12 新增"动作编码协议"小节，规定两档——原生 function calling（provider 能力允许时）与 marker 分段档（能力降级保底），并明令禁止要求模型转义多行载荷。

**[F-02] 自研工作区把 blob 哈希当内容再哈希，污染 verified 树。**
prospective 合并时 verified tree 的值是 blob sha256，直接与输出内容合并会把哈希串当文件内容写进 commit（workspace.ts:118-128 现有注释即为此修复而写）。｜M-00/M-11 默认 commit 对象由成熟 Git 承担，从未声明"tree 存哈希、合并需先经 blob 表还原"。｜**语义缺口**（依赖了未言明的 Git 实体行为）。｜改文档：M-11 加一句——凡自建轻量工作区实现，tree↔blob 两层必须显式分离，合并操作以内容域为准。

**[F-03] SliceAttemptStatus 缺 INTEGRATED→REGENERATING 边，终验归因回路第一步就抛 ILLEGAL_TRANSITION。**
M-00 正文写了"VERIFYING 内走完整候选流程与重集成"（V-M10-V4-014 语义），但公共契约的转移枚举没有这条边；MVP 补上了（run.ts:78，注释标明唯一受控重开）。｜M-00 公共契约节。｜**语义缺口**（正文与枚举表不同步）。｜改文档：M-00 转移表补 INTEGRATED→REGENERATING，作用域限定 FinalVerified 归因子回路。

**[F-04] checkpoint 批量校验的对象是"全量镜像"还是"变更集"文档没说清。**
按字面实现成前者时，任何非契约 Slice 都会被自己种子里的 pyproject/contracts 键判越界（审查 P0-1）；正确语义是后者（现 workspace.ts:100-109 双参签名固定了这一点）。｜M-08 "Git diff 必须全落 write scope"隐含 diff 语义但未定义基线与提交面的关系。｜**表述歧义**。｜改文档：M-08 明确三句话——种子=base verified 物化；校验面=相对种子的变更集；commit 面=种子∪变更集。

**[F-05] "模型供应商故障"在重派语义里无处安放。**
端点 503/挂起既非 worker 断连也非语义失败；MVP 打补丁：同代幂等重入 RUNNING 不耗 generation（run.ts:249-250 幂等进入、338-357 infraRetries≤2 退避；full-run.log 09:42:15 RUNNING(gen=0) 重复出现即活体证据）。｜M-00 只有"物理 worker 中断不消耗 generation"。｜**语义缺口**。｜改文档：M-03 把"模型基础设施错误"显式归入物理重派类，并给出与 SLICE_REGENERATION_EXHAUSTED 的边界（基础设施重试不计代）。

**[F-06] 归因 fallback 曾做无证据的 Slice 级猜测，错杀集成序最前的测试 Slice 至三代耗尽。**
旧 findTestOwner 直接返回 rank 最前者（〔会话〕，现场已清理）；现行版先做 FileLine/TestIdentity 与 write_scope/target_files 匹配，fallback 仅作最后手段（run.ts:681-698）。｜P-09 符号级归因需要 PSF-2 覆盖边，MVP 只有文件级两步归因；M-00 说"无法唯一归属时进入 Run 级终态兜底"，却没禁止中间层自行猜测。｜**语义缺口 + 实现越权**。｜改文档：M-00 归因规则补一条硬约束——无证据 fallback 只允许 Run 级兜底，禁止 Slice 级猜测；符号级归因的成本评估：文件级匹配已解决 ~80% 场景，PSF-2 符号闭包只在"失败证据落在实现文件而非测试文件"时才有增量价值，可作为正式版的可选精化而非门槛。

**[F-07] 测试收集完备性盲区：目标侧只收集到 4/21 用例仍 PASSED。**
翻译产物用了 Test* 类风格，旧收集器只认模块级 test_* 函数（pytest_lite.py:49-67 现已双风格收集；该次失败现场已被清理〔会话〕）。更深一层：即便收集数远低于源基线（20 用例守恒事实就在手边），Oracle 依然放行——守恒信号只进了报告没进门。｜M-10 定义了 SkippedEmpty 却没定义"收集完备性"；结构守恒被明确排除在 pass/fail 之外。｜**欠设计**。｜改文档：M-10 增加"可疑通过"披露位图——Final PASSED 且 translated_tests/source_tests 越出 [0.5,2.0] 时，证据页强制黄牌并降一档信心（不必阻断，但不能无声）。

**[F-08] 源测试自身的数学错误被忠实翻译放大为主证失败。**
fixture checkout.test.ts 原断言 subtotal 1500 分期待命中 2000 分档（阈值之下不可能），第一代 Agent 忠实翻译→终验必败；另一代 Agent 在诊断驱动下修正数据并留注释理由（fixtures/ts-shop-cart/tests/checkout.test.ts:16-23 现为修正版，注释"恰好命中最低档"即修复痕迹）。｜P-02 的哲学前提是"源套件在源项目真实跑通过=可信历史事实"（M-00/D-033），但跑通≠数学正确；主证证明的是"翻译保持源行为"，不是"源行为正确"。｜**设计哲学边界反例**。｜改文档：M-10 证据页在"通过路径共谋盲区"旁并列第二条固定声明——"源测试缺陷传导盲区：主证不校正源侧错误，忠实翻译会原样保留它"；实现侧可选增强：纯函数模块提供源/目标黑盒 parity 场景对照（M-10 行为 parity 机制恰好是现成挂点）。

**[F-09] Reasoning 会话输出形状不稳定，需要确定性归一层。**
LLM 档案各数组字段偶发非数组形状导致 for..of 抛错（full-run.log 可见 `[analyze] LLM dossier failed -> heuristic fallback: entries is not iterable`）；normalizeDossier 防御层（dossier.ts）落地后当轮运行 provenance 即为 llm-reasoning。早期 catch 静默吞掉根因，延迟了两轮排查——可观测性欠账直接变成排障成本。｜M-14 未规定理解档案产出的 schema 归一义务，也未规定降级必须带因上报。｜**欠设计**。｜改文档：M-14 两句——理解档案产出必须过确定性 schema 归一器；归一失败降级 heuristic 并发 `analyze.dossier_fallback{reason}` 事件。

**[F-10] 组名规范化函数散落两处且规则不一致。**
planner sanitize 与 report slug 各写一份，GENERATED 目标文件名反查曾查错文件得全 0 计数（假离群）；现统一由 planner 导出 sanitizeName 供 report 反查（planner.ts:269+；report.ts:72,94-98）。｜纯实现冗余，非文档问题。｜**冗余**。｜教训落 M-07 一句话：模块组标识的规范化函数属 core 公共契约，消费方不得私有复刻。

**[F-11] 免费端点不支持 response_format/json mode。**
客户端被迫走"指令约束+健壮解析"路线（client.ts 注释；llm_probe.ts/session_probe.ts 两个探针即为此服务）。｜Q-V4-001 只说 provider/model 未定，未提能力分级假设。｜**欠设计**。｜改文档：M-05 增加 provider 能力矩阵维度（json_mode/tools/streaming/context 长度），核心逻辑声明为"仅依赖必选能力"。

**[F-12] 终验检查集口径曾被实现走样**（混入 SCAFFOLD/COMPILE，审查 #17；现 run.ts:510-518 收敛为 TEST 全集）。M-00 表格本身清晰，属实现偏差，文档无需改。

**[F-13] dispatch.interrupted 事件入了流但终端渲染器没有对应 case，同代重派在过程视图里只能看到诡异的重复 RUNNING 行**（terminal.ts default 分支不渲染该类型；full-run.log 09:42:15）。｜M-02 事件集与 M-15 展示归约之间缺"每个事件类型必须有归约或显式折叠"的验收项。｜**欠设计**。｜改文档：M-15 验收清单加一条展示完备性条目。

## 4. MVP 简化清单与回归正式版的路径

| MVP 形态 | 正式版归宿 | 回归路径上的注意点 |
|---|---|---|
| run_events.jsonl（finalize 一次性 flush） | PostgreSQL append-only 同事务投影 | flush 时机意味着中途崩溃丢事件账本；PG 版必须恢复"同事务写入"语义 |
| OutputWorkspace state.json（内存 Map+整文件快照） | Git internal refs + host CAS | persist 串行链（workspace.ts:157-173）只是止血，Git ref 事务天然解决 |
| spawnSync `--version` 能力门 | 工具链镜像摘要预检 | probe 只能证明"存在"，不能证明版本/摘要一致，正式版回到摘要比对 |
| pytest_lite.py | 描述符声明的真实 pytest | 双风格收集/SkippedEmpty/单文件导入隔离三个补丁要随描述符迁移进模板约定 |
| findTestOwner 文件级两步归因 | PSF-2 ReferenceSite 符号级 | 按 F-06 结论做成可选精化；先把"禁猜测"写进文档 |
| heuristic dossier | 仅保留为离线演示/text-fallback 档 | normalizeDossier 归一器无论哪档都必须前置 |
| `[cm:*]` marker 协议 | function calling 优先 + marker 保底 | 两档并存而非替换——F-01 已证明 marker 档是供应商无关的确定性下界 |
| run.ts 单文件编排（730 行 actor+调度+三阶段） | M-03 Run actor / IC / 验证引擎分体 | MVP 合并降低了跳读成本，拆分时机=引入恢复语义时 |

## 5. 优雅性与效率评估

- **代码量 vs 设计面**：3,089 行 TS 覆盖五阶段闭环、四类 Slice DAG、三层验证、CAS 工作区、事件账本、报告八区块、六工具网关。压缩率高的根本原因是 V4 的窄核心决策（六工具+数据结构协商+契约先行）真的把垂类复杂度关在了数据（描述符/计划/契约工件）而不是代码里——这是设计文档最值得肯定的全局判断。
- **状态机**：RunStatus 10 态、SliceAttemptStatus 10 态全部有用，未出现"为分层扩状态"违规；唯一的坑是文档转移表缺边（F-03），说明状态机这类契约必须机器可校验（建议正式版从 M-00 表格生成测试）。
- **并发模型**：runningTasks + Promise.race 的 ~60 行调度循环（run.ts:447-498）替代通用 scheduler，依赖闭包就绪即启动、队首消费、自旋防护三点齐全。"不用消息协商、用数据结构协商"在这里被证明可以极简。
- **协议成本**：动作协议从 JSON 改 marker 后，解析器反而更短更稳（parseAction 52 行 vs 原 extractJson 路径的全部防御分支），"为模型设计 IO 格式"比"为程序设计"更重要——这条经验值得进 M-12。
- **冗余清单**（删/合建议）：runner.ts:65 push 包装为 no-op（message_hash 自赋值）；tools.ts isUnderSource 已退化为 normalizeRel!==null 的别名（159 行调用处）；EV 常量表与裸字符串混用（events.ts EV 表只覆盖一半发射点）。合计可净删 ~40 行并消除双轨命名。
- **过度设计检查**：未发现明显过度设计——反倒是 M-00 的部分机制（validation overlay per check、flaky 三跑、预算熔断）在 MVP 里缺席后没有任何一处出现"如果早实现就不会错"的懊悔点，说明它们的优先级排序（批次 2）是合理的。

## 6. 迭代建议优先级表

| 编号 | 建议 | 影响面 | 优先级 | 落点 |
|---|---|---|---|---|
| R-01 | M-12 新增"模型动作编码协议"章节：function-calling 档 + marker 分段档，禁转义多行载荷 | EXECUTE 全部会话稳定性 | P0 | M-12 |
| R-02 | 转移表补 INTEGRATED→REGENERATING（限 VERIFYING 归因子回路） | 终验重生成回路可用性 | P0 | M-00 公共契约 |
| R-03 | 守恒信号接入 Oracle 弱门：Final PASSED 且测试数比越带宽 → 强制黄牌披露+信心降档 | 防假阳性主证 | P0 | M-10 |
| R-04 | "模型基础设施故障=物理重派不耗 generation"显式入文 | 供应商故障韧性语义 | P1 | M-00/M-03 |
| R-05 | checkpoint 三句话定义：种子/校验面/提交面 | 自建工作区实现者 | P1 | M-08 |
| R-06 | 证据页并列第二条盲区声明"源测试缺陷传导"；parity 对照挂到行为 parity 机制 | P-02 主证哲学诚实性 | P1 | M-00 P-02 / M-10 |
| R-07 | 理解档案 schema 归一器为必选件；降级必须发因事件 | ANALYZE 稳定性与可排障性 | P1 | M-14 |
| R-08 | provider 能力矩阵（json_mode/tools/streaming），核心仅依赖必选能力 | 多供应商可移植性 | P1 | M-05/Q-V4-001 |
| R-09 | 无证据 fallback 禁止 Slice 级猜测，仅 Run 级兜底 | P-09 归因正确性 | P1 | M-00 归因规则 |
| R-10 | 自建轻量工作区时 tree/blob 显式分层、合并以内容域为准 | 非 Git 实现路径 | P2 | M-11 |
| R-11 | M-15 展示归约完备性验收（每事件类型必有渲染或折叠） | 过程视图可信度 | P2 | M-15 |
| R-12 | 组名规范化等标识函数收编 core 公共契约 | 计划/报告一致性 | P2 | M-07 |

**一句话总结**：V4 的骨架（契约先行、窄核心、数据协商、三层验证）经受了实战检验且无一被推翻；真正的缺陷集中在"文档默认了成熟基础设施的行为"（Git/provider/PSF-2）与"契约表格与正文语义不同步"两类，迭代应当以补语义缺口和协议规范为主，而不是重构架构。
