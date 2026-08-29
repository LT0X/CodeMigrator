# CM-PLAN-001 对齐记录

> 用途：本文件是任务 `CM-PLAN-001`（模块 M-07 迁移计划生成器）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-PLAN-001` |
| 模块编号 | M-07（V6 收敛版） |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_迁移计划生成器.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/planning/` 子包——纯逻辑（零模型调用，D-05）：

- **PlanProposal 结构化 schema**（D-01，M-00 占位定稿）：提案内 Slice 用局部引用（local_ref 如 CT/A），冻结时服务端分配 UUIDv7 SliceId；条目={local_ref、kind（四类）、source_modules、write_paths、create_roots、rationale、工件派生项}；edges={from、to、kind（Requires/OrderedBefore）、provenance（五值）}；integration_ranks；planner_rationale（DossierEntry 格式）。pydantic 严格校验（extra=forbid）。
- **机器校验器（四重护栏 + 拓扑一致性 + 规模）**：两两 write scope 不相交、Blueprint 合规、in-scope 源文件覆盖恰好一次、DAG 无环（合并全部提案边环检查）；**integration_rank 严格拓扑序校验**（D-06：每条边 from.rank < to.rank，违反→PLAN_RANK_INCONSISTENT——防 FIFO 集成队列死锁）；规模上限四值（D-03）；校验通过自动冻结（Slice/边/write scope/integration_rank/plan hash 原子持久化）。
- **拒绝码体系**（D-02）：PLAN_CYCLE/PLAN_SCOPE_CONFLICT/PLAN_BLUEPRINT_VIOLATION/PLAN_COVERAGE_INVALID/PLAN_SIZE_EXCEEDED/PLAN_EDGE_INVALID/PLAN_RANK_INCONSISTENT/PLAN_PROPOSAL_INVALID 八码进 core StableErrorCode（CM-CORE 扩容联动）。
- **规模上限**（D-03，可配常数）：Slice ≤ 100、PlanEdge（Requires+OrderedBefore 总数）≤ 500、单 Slice write_paths ≤ 200、总 write scope ≤ 2000 文件——PC 居中档（Slice 数是磁盘/验证成本大头；100 Slice 长驻卷在 WSL2 100G 配额下安全；覆盖个人项目 500-2000 文件量级）。
- **提案失败重试**（D-04）：解析/schema/校验失败→带校验错误反馈重试最多 3 次（可配），耗尽→Run FAILED(PlanFailed) 且计划零持久化；provider 物理故障按 M-00 物理重派 30s/60s/120s 退避不计次数。
- **四类 Slice 派生语义**：Contract 可为 0、Implementation 按分解策略分组、TestTranslation（Covered 双轨）、TestGeneration（EmptyTestSuite 驱动、GENERATED 全链路标注、最低质量门槛非平凡断言、信息防火墙不含被测实现目标正文、ambiguous 锚点退回模块级导出摘要）。
- **三类工件派生**：GeneratedCode 零翻译（生成 action 归承载 Slice）、DeclarativeConfig 承载 Slice 翻译等价物、ResourceFile 复制/轻转换不入翻译 Slice write scope；Planner 零内建语言分支（只按 artifact_rules 消费 F4 识别事实）。
- **依赖边提案与校验**：边端点校验/provenance/边语义/DAG；测试类 Slice 不对被测实现加边（在场门控保序 V-M10-V4-027）；写冲突不得用 OrderedBefore 掩盖。
- **涟漪计算**（符号级三步查表）：PSF-2 ReferenceSite 符号引用闭包（ambiguous/text-fallback 降级模块级并标注）→ PSF-3 依赖闭包 → source_modules→Slice 映射；产出涟漪预览（作废/重建/预计 Slice 数/符号清单/集成状态分布）供 M-16 ImpactPreview 消费；只读不改计划。
- **条件化联合域例外（语义侧）**：write scope 互斥的条件安全精化语义与 schema 不变（判定算法归 CM-REPAIR 对齐收口）；初译计划内 Slice 仍两两不相交。
- **组名规范化函数**：唯一实现+导出正门归本篇（M-08/M-10/M-15 消费方禁止私有复制）。
- **plan hash**：四件工件 hash+快照 OID+全部 Slice canonical+边及 provenance+write scope+integration_rank+PlanValidation+集成键；canonical 序列化用 core 工具（CM-CORE D-06）。

### 1.2 边界（不做什么）

- 不做 Planner LLM 会话循环/模型调用/工具授权（归 runtime 编排 + CM-LOOP 会话框架——M-01 planning 禁未授权模型调用，D-05）。
- 不做 DAG ready 调度/集成序消费（M-03 runtime——校验器只冻结事实）。
- 不做 write scope 运行期拦截（M-08/M-12 工具网关）。
- 不做归因执行/可靠性分类 schema（M-10 owner——候选修复集+可靠性分类的判定与 schema 归 CM-VERIFY-001 对齐收口；本任务仅保证 write scope 查表映射持久化不变）。
- 不做联合域安全判定算法（§9.1 开放项归 CM-REPAIR-001 对齐收口；本任务只定语义）。
- 不做 Blueprint 字段形状定稿（M-00 占位——CM-DRAFT 已登记"CM-PLAN 对齐收口"；**本对齐裁定**：Blueprint 字段仍按 M-00 占位形状消费（module_boundaries/granularity_principles/target_layout_principles/parallelism_rules/generated_artifact_policy/version），蓝图合规校验按占位字段语义实现（目标路径前缀域/布局原则启发式匹配），字段扩展留实施期与用户逐项确认，不臆造）。
- 不做 PlanRevision 编排（M-16 流程 owner；本任务提供重新提案+校验的复用函数）。
- 不做目标路径实际写入（M-08）。

### 1.3 产出物

planning 子包：proposal schema（pydantic）/校验器（护栏+拓扑+规模）/涟漪计算/组名规范化/plan hash 组装；core StableErrorCode 八码扩容登记；tests/planning/（四重护栏用例/拓扑校验死锁用例/规模边界用例/涟漪三步查表用例/重试归约）；模块迭代记录（dev_progress/planning/）。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | PlanProposal 具体结构化 schema（M-00 占位定稿） | local ref+服务端分配 ID / 保持占位 / 押后 | **local ref+服务端分配 ID**：提案内局部引用，冻结时服务端分配 UUIDv7；完整结构化 pydantic schema（extra=forbid） | 对齐问答 Q-01（2026-08-29） |
| D-02 | 机器校验稳定拒绝码集合（M-07 明示待定稿） | 完整八码进 core / 最小五码 / 押后 | **完整八码**：PLAN_CYCLE/PLAN_SCOPE_CONFLICT/PLAN_BLUEPRINT_VIOLATION/PLAN_COVERAGE_INVALID/PLAN_SIZE_EXCEEDED/PLAN_EDGE_INVALID/PLAN_RANK_INCONSISTENT/PLAN_PROPOSAL_INVALID——进 core 单一 StableErrorCode | 对齐问答 Q-02 |
| D-03 | 规模上限数值（M-07「数值待定」；用户要求按 PC 居中重算并确认） | 大档 / 居中档 / 小档 | **100/500/200/2000**：Slice ≤ 100、边（PlanEdge Requires+OrderedBefore 总数）≤ 500、单 Slice write_paths ≤ 200、总 write scope ≤ 2000 文件（均可配常数） | 对齐问答 Q-03/Q-05/Q-06（含「边」定义澄清后确认） |
| D-04 | 提案失败重试与 PlanFailed 归约 | 反馈重试 3 次 / 单次 / 押后 | **反馈重试 3 次**（可配）：带校验错误反馈重试，耗尽→FAILED(PlanFailed) 零持久化；provider 物理故障走物理重派不计次数 | 对齐问答 Q-04 |
| D-05 | planning 子包落点切分 | 纯逻辑归 planning / planning 直调模型 | **纯逻辑归 planning**：schema/校验/涟漪/规范化零模型调用；Planner LLM 会话循环归 runtime 编排+CM-LOOP 会话框架 | 对齐问答 Q-07 |
| D-06 | integration_rank 拓扑一致性校验（M-07 未明文；FIFO 死锁风险） | 校验严格拓扑序 / 不校验 | **校验严格拓扑序**：每条边 from.rank < to.rank，违反→PLAN_RANK_INCONSISTENT 拒绝 | 对齐问答 Q-08 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（MigrationSlice/WriteScope/PlanEdge/SliceKind/ArtifactKind/integration key 比较函数——CM-CORE D-06）；四件冻结工件（CM-DRAFT 产出）；M-06 F1-F4/PSF-2/PSF-3（CM-ANALYSIS D-01/D-02 投影端口）；Spec 范围/检查集（CM-SPEC）。

### 3.2 下游消费

- M-03 调度（DAG ready/集成键消费）、M-08 候选工作区（write scope 拦截表）、M-10 验证与归因（write scope 查表映射、守恒基线）、M-11 集成序（integration_rank）、M-16 修正（涟漪预览/PlanRevision 重新提案）、M-15 展示（计划证据/DAG）、M-02 投影（plan hash/PlanValidation 事件）。

### 3.3 跨模块接口边界

- **core StableErrorCode 扩容联动**（D-02）：八码进单一枚举（CM-CORE 变更记录追加登记）。
- **归因可靠性分类 schema 归 CM-VERIFY-001**（M-07 明示"实施期开放项，本篇不臆造 schema"——对齐归属登记）。
- **联合域安全判定算法归 CM-REPAIR-001**（§9.1 开放项——对齐归属登记；本任务语义侧已冻结：修复集全部集成时天然满足）。
- **组名规范化函数唯一 owner**：M-08/M-10/M-15 消费方引用正门禁止复制（M-07 明文）。
- 在场门控保序（V-M10-V4-027）归 M-10——Planner 不加测试→实现边。
- Unknown 边清单/写冲突对清单/分组依据入计划证据（REPORT/M-15 消费，不重新推断）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M07-V5-001 | Planner 消费四件+M-06 事实产出可审计 PlanProposal；Contract 可为 0 | 校验器单测（合法提案用例） |
| V-M07-V5-002 | 拒绝 write scope 相交/Blueprint 外结构/覆盖缺失或重复/成环/超规模；零部分冻结 | 校验器单测（五类违规用例+副作用计数） |
| V-M07-V5-003 | 通过后自动冻结；用户不逐 Slice 确认；完成顺序不改 integration_rank | 冻结单测 |
| V-M07-V5-004 | 结构变化只经 M-16 安全点 ImpactPreview；已验证主线不重写 | 归 CM-RUNTIME/CM-GIT 联动验收 |
| V-M07-V4-001~007（追溯） | 闭包就绪即启动/边与 import 图一致/Unknown 不入 Requires/写冲突拒绝/完成顺序不变/PLAN_CYCLE 零写入/测试边规则 | 校验器单测（护栏细化用例；V5 语义为主） |
| V-M07-V4-012~014（追溯） | 测试生成派生（EmptyTestSuite 驱动/双轨互斥/Undetermined 零派生）/三类工件派生/涟漪符号级计算 | 派生单测+涟漪查表单测 |
| D-03 规模 | 100/500/200/2000 边界拒绝（含 101/501/201/2001 逐值用例） | 校验器单测 |
| D-04 重试 | 3 次反馈重试后 PlanFailed 零持久化；物理故障不计次 | 归约单测（对接 runtime 编排 stub） |
| D-06 拓扑 | rank 违反拓扑序→PLAN_RANK_INCONSISTENT（构造死锁用例：A requires B 但 rank(A)<rank(B)） | 校验器单测 |

> V-M07-V4-008/010（deterministic_plan_order_key/SCC 收缩）为 V4 历史机制，V5 已废除（文档明示"不得用于新计划"/"机器校验不自动收缩"）——不作当前验收。

## 5. 风险与注意点

- **V4 追溯条款语义偏移**：V-M07-V4-002~004/007 表述含 V4 固定派生规则（"恰好是""必有"），V5 改为提案能力+机器校验护栏——实现以 V5/V6 语义为准（提案自由+护栏拒绝），V4 编号仅作护栏行为参考。
- **rank 拓扑校验是新增强护栏**（D-06，M-07 未明文）——实施期须与 M-03 集成协调器语义对齐验证（队首 FIFO+DAG ready 联合无死锁证明用例）；如与 M-03 对齐冲突在本记录追加变更行。
- **Blueprint 合规校验启发式**：占位字段语义模糊（"布局原则"非结构化）——初版按目标路径前缀域+原则摘要匹配实现，误报时 Planner 重提案（重试预算内）；字段定稿后校验器同步（实施期用户确认）。
- **plan hash canonical 一致性**：与 core canonical JSON 工具共用（CM-CORE D-06），勿另造序列化。
- **重试反馈格式**：校验错误需结构化（拒绝码+JSON Pointer+理由）回流 Planner 会话——错误反馈模型与 M-05 有序问题响应同构（可复用设计）。
- 规模上限可配常数注入（runtime 配置），核心逻辑零硬编码。
- 事件名（plan 校验/冻结事件）归 api 常量（CM-CORE D-05 惯例）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | PlanProposal 结构化 schema 定稿 | local ref + 服务端分配 UUIDv7 + 完整 pydantic schema |
| Q-02 | 机器校验拒绝码集合 | 完整八码进 core StableErrorCode |
| Q-03 | 规模上限数值 | （要求按 PC 运行环境重算居中值）——经澄清后于 Q-06 确认 |
| Q-04 | 提案失败重试策略 | 反馈重试 3 次（可配）；物理故障不计次 |
| Q-05 | （用户追问）"边"指什么 | 澄清：PlanEdge=计划依赖边（Requires 依赖边+OrderedBefore 顺序边），持久化 plan_edges 表；量级 ≈2~5×Slice 数；Slice 数才是磁盘/验证成本大头 |
| Q-06 | PC 居中档规模上限最终确认 | 100/500/200/2000（Slice/边/单 Slice 文件/总文件） |
| Q-07 | planning 子包落点切分 | 纯逻辑归 planning；LLM 会话循环归 runtime+CM-LOOP |
| Q-08 | integration_rank 拓扑一致性校验 | 校验严格拓扑序（防 FIFO 死锁），违反→PLAN_RANK_INCONSISTENT |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-07 V6 收敛版设计文档与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-06（含「边」定义澄清与 PC 居中档规模重算）；归因可靠性分类 schema 归 CM-VERIFY、联合域判定算法归 CM-REPAIR 的对齐归属已登记 |
