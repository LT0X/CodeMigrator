# CM-REPAIR-001 对齐记录

> 用途：本文件是任务 `CM-REPAIR-001`（全局修复会话·V6 新增）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-REPAIR-001` |
| 模块编号 | M-03/M-04/M-07/M-11（全局修复会话·V6 新增任务；不新增子包——修复会话落 runtime 会话框架，联合域语义归 M-07，ref 侧归 M-11） |
| 对应设计文档 | M-04 修复会话节 + M-07 条件化联合域例外节 + M-11 修复 ref 族（wave23 轮已通读）+ M-03 全局修复集成序与重试边界节 + M-00 P-09 |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐（用户跳过逐项确认，决策采纳推荐方案——沿用惯例，见 §6） |

## 1. 任务理解

### 1.1 范围（做什么）

交付全局修复会话编排（落 runtime：会话类型实例化+派发+联合域判定+FIFO 集成接线+重试计数；无独立子包）：

- **修复会话身份与生命周期**：`(run_id, repair_decision_id)`，不属原 Slice、不占原 Slice generation 0-2；SessionKind.RepairSession（CM-MEMORY D-01）；派发自 Supervisor 修复决策收养（CM-RUNTIME 收养流程）或简单场景下的重生升级包（单 Slice 委派走原 Slice 重生——归 CM-LOOP 构成）。
- **读视野**：全境读（源快照+契约+本域工作区+verified head 只读根——CM-WORKSPACE D-03）；写=联合域。
- **联合域安全判定算法（D-01，§9.1 开放项收口）**：派发时点冻结的机械查表——①修复集内全部 Slice 状态为已终态集成（INTEGRATED/TERMINAL——M-07「已全部集成时天然满足」）；②联合域（修复集 write scope 并集）与当前全部活跃在途写者（RUNNING/REGENERATING/CheckpointPending 态 Slice 的冻结 write scope）求交=空集；③判定通过→Harness 派发时冻结 `GlobalRepairSession.joint_write_scope` 写入；判定失败→等待（在途 Slice 终态收敛后重判）或 Supervisor 改单 Slice 委派。纯查表零模型。
- **修复简报 schema（D-02，§9.1 开放项收口；CM-MEMORY D-04 消费契约对接）**：`RepairBrief` = {attribution: RepairEvidence 摘要（候选修复集+可靠性分类+两布尔——CM-VERIFY D-03）、failure_facts: 失败测试身份+诊断摘要（正文外置 CAS+导航索引）、scope_index: 联合域涉及文件+位置清单（导航索引式）、repair_history: 前次修复决策与结果（防重复）、constraints: write scope 边界+验证要求}——必要输入不可静默截断；超限部分外置 `cas://` 受控取回。
- **态势快照（D-05，§9.1 归属收口）**：`SituationalSnapshot` = {slice_states, verified_oid, active_dispatches, budget_ratio, 前次修复历史}——Harness 机器确定性计算、不入上下文、仅作决策派生参考；派生物不持久化（可从 run_events 重建）。
- **FIFO 集成与队列头交互（D-03，CM-RUNTIME 登记归本任务收口）**：失败 Slice 经 Supervisor 全局修复决策收养后，其集成队列占位**撤销**（失败 prospective 作废、其域内容改由修复会话产出承载）；队列头推进（其余 Slice 集成不被修复阻塞——M-03「修复不阻塞并行」）；修复条目按完成序 **FIFO 追加**进 Coordinator 单写者串行通道（无冻结 rank——CM-GIT D-03 repair ref 族）；修复集成 prospective 建立在集成时最新 verified；集成承载原 Slice 域内容后原 Slice 以 superseded-by-repair lineage 归档（ModuleChangeRecord）。
- **修复闭环**：checkpoint→局部验证→集成（同普通 Slice 裁决链，不跳过 M-10 prospective checks）；集成后对新 head 重新验证（触发方归 actor）。
- **独立重试上限**：消费 CM-VERIFY D-02（总尝试 3 次=首次+2 重试，可配）；每次修复由新归因证据驱动（同证据不重试）；Run 预算断路器兜底；耗尽→原失败 Slice 终态归约（IndependentSliceTerminalFailure 判定→PARTIALLY_COMPLETED 或 VerificationTerminal→FAILED）。
- **免确认门语义**：归因驱动内部闭环免 ImpactPreview（M-16 V6 边界——与用户发起设计级变更区分）；修复全量审计进 run_events（repair.session.* 事件——CM-SUPERVISOR D-03 schema）。
- **重生升级包对比**：简单静态唯一命中→原 Slice 重生（占 generation 0-2+升级包=全境读+修复简报）；复杂多义→本任务全局修复（不占 generation）——两分支路由归 CM-VERIFY/CM-RUNTIME，本任务实现全局侧。

### 1.2 边界（不做什么）

- 不新增子包（M-01 V6 不改边界）。
- 不做修复决策判断（归 Supervisor——CM-SUPERVISOR 已对齐；本任务消费 RepairDecision）。
- 不做归因/可靠性分类（归 CM-VERIFY——已对齐；消费 RepairEvidence）。
- 不做收养流程（归 CM-RUNTIME——已对齐；机械校验项含联合域成员有效性——D-01 判定函数由本任务提供）。
- 不做联合域 write scope 语义/schema（归 M-07/M-00——GlobalRepairSession 类型已定；本任务实现判定算法）。
- 不做 repair ref 物理事务/CAS（归 CM-GIT——已对齐 D-03 repair ref 族；本任务消费）。
- 不做重试计数归约（归 actor——CM-VERIFY D-02 数值+M-03 语义；本任务提供「新证据驱动」判定输入）。
- 不做修复会话循环/工具面（归 CM-LOOP 框架+CM-WORKSPACE 授权行——已对齐）。
- 不做上下文装配/预算（归 CM-MEMORY——已对齐 RepairSession 档 64k/600k/75%）。
- 不做验证裁决（归 CM-VERIFY——已对齐）。

### 1.3 产出物

runtime 侧：修复会话派发编排（联合域判定→冻结 joint_write_scope→会话派发→checkpoint→集成接线）、RepairBrief 装配器（Supervisor 决策 brief_refs 的内容生产）、SituationalSnapshot 派生器、FIFO 入队接线、重试证据判定输入、superseded-by-repair lineage 归档；core 侧：RepairBrief/SituationalSnapshot schema 建议（经 Advice.payload/RepairDecision.brief_refs 契约对齐登记）；tests/repair/（联合域判定矩阵/队列头撤销无死锁用例/FIFO 序/重试耗尽归约/免确认门边界）；模块迭代记录（dev_progress/repair/）。

## 2. 关键实现决策与确认结论

> 用户跳过逐项确认（沿用惯例）——按推荐方案采纳并如实标注。

| # | 决策点 | 可选项 | 采纳结论（推荐方案） | 依据 |
|---|---|---|---|---|
| D-01 | 联合域安全判定算法（§9.1 开放项；M-07「精确判定算法实施期开放，本篇只定语义」） | 派发时冻结查表 / 运行时动态校验 / 押后 | **派发时点冻结机械查表**：①修复集内 Slice 全部已终态集成 ②联合域∩全部在途写者（RUNNING/REGENERATING/CheckpointPending 的 write scope）=∅ ③通过→冻结 joint_write_scope；失败→等待重判或改委派；纯查表零模型 | 推荐采纳（2026-08-29） |
| D-02 | 修复简报 schema（§9.1 开放项；CM-MEMORY D-04 登记归本任务） | 五段 schema / 押后 | **RepairBrief 五段**：attribution（RepairEvidence 摘要）/failure_facts（外置+导航索引）/scope_index（联合域文件+位置清单）/repair_history（前次决策防重复）/constraints（域边界+验证要求）；必要输入不可截断+超限外置 cas:// | 推荐采纳 |
| D-03 | FIFO 队列头交互（CM-RUNTIME 登记归本任务收口；死锁消解） | 撤销占位+FIFO 追加 / 阻塞等待 / 插队 | **失败 Slice 队列占位撤销**：全局修复决策收养后其 prospective 作废、域内容由修复产出承载、队列头推进（不阻塞其余集成）；修复条目完成序 FIFO 追加（不插队越过普通队首）；集成承载后原 Slice superseded-by-repair 归档；修复耗尽→原 Slice 终态归约 | 推荐采纳 |
| D-04 | 态势快照 schema 与持久化（§9.1 归属收口） | 派生不持久化 / 持久化表 | **SituationalSnapshot 五字段派生物**（slice_states/verified_oid/active_dispatches/budget_ratio/前次修复历史）；不入上下文仅决策派生；不持久化（run_events 可重建） | 推荐采纳 |
| D-05 | 修复验证闭环接线 | 标准链复用 / 特殊通道 | **标准裁决链复用**：checkpoint→局部→集成不跳过 prospective checks（M-03「不因会话来源跳过」）；集成后新 head 重验证触发归 actor | 推荐采纳 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 契约（GlobalRepairSession/RepairDecision/WriteScope/SessionKind.RepairSession）；CM-SUPERVISOR（RepairDecision 建议含 brief_refs 装配输入）；CM-VERIFY（RepairEvidence+重试上限 3 次）；CM-RUNTIME（收养后派发指令+重试计数）；CM-GIT（repair ref 族+FIFO 集成原语）；CM-LOOP（会话框架）；CM-WORKSPACE（全境读授权行+联合域写）；CM-MEMORY（RepairSession 预算档+导航索引装配契约）。

### 3.2 下游消费

- CM-RUNTIME（修复集成完成事件+重试归约输入）；CM-VERIFY（修复集成后的 prospective/新 head 重验证）；M-16/ModuleChangeRecord（superseded-by-repair lineage）；CM-API/M-15（repair.session.* 事件投影+Web 独立呈现位）；CM-OBS（修复事件观测——已对齐）。

### 3.3 跨模块接口边界

- **队列头交互闭环**（CM-RUNTIME 登记项收口）：D-03 撤销占位语义与 Coordinator 单写者串行通道对接——本任务提供撤销/追加原语语义，Coordinator 编排归 CM-RUNTIME 联调（并行纪律 5 双登记）。
- **RepairBrief ↔ brief_refs 契约**：RepairDecision.brief_refs（M-00）指向的 ArtifactRef 内容=本任务 RepairBrief（D-02）——core 契约扩展登记（CM-CORE 变更行）。
- **联合域判定与收养校验闭环**：CM-RUNTIME 收养机械校验项「联合域成员有效性」调用本任务 D-01 判定函数——接口先行冻结。
- **免确认门边界**：归因驱动内部闭环（本任务）vs 用户发起设计级变更（M-16 确认门）——判定准则=M-16 V6 边界准则（凡仅回放既有验证失败归因的内部闭环不设门）。
- **M-11 文档同步候选**：repair ref 族+撤销占位语义随实施回填 M-11（CM-GIT D-03 已登记）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V6 收敛-001/002/003（M-10 侧） | 修复路由执行侧：联合域判定/重试上限 3/新证据驱动/耗尽才终态 | 修复编排单测（判定矩阵+重试归约） |
| M-00 P-09 | 不占 generation 0-2；联合域条件安全；FIFO 集成；基线取最新 | 编排单测（generation 计数断言） |
| M-03 修复节 | FIFO 同队不插队；不跳过 prospective checks；不阻塞并行；预算断路器 | 队列交互单测（D-03 无死锁用例：失败占位撤销→队列推进→修复 FIFO 追加→集成承载→superseded 归档） |
| M-04 修复会话构成 | 全境读+联合域写+修复简报必要输入+导航索引 | 构成联动单测（memory/授权行） |
| M-07 条件化联合域 | 初译 Slice 仍两两互斥（例外仅修复）；运行期不扩大单 Slice 冻结 scope | 判定单测（例外边界） |
| D-01 判定矩阵 | 已集成+无在途→通过；在途相交→等待；修复集含未终态→等待 | 判定单测（三分支） |
| D-02 简报 | 五段齐备不可截断；超限外置；repair_history 防重复 | 装配单测 |
| D-03 队列头 | 撤销占位后零死锁；修复耗尽→原 Slice 终态恢复处置 | 队列状态机单测 |
| 主表 §10 场景 4 | 两级修复闭环（静态唯一→重生直通；多义→Supervisor→全局修复→FIFO 集成；耗尽才终态） | 端到端集成测试（Wave 4 靶场） |
| 免确认门 | 归因驱动内部闭环零 ImpactPreview 事件；用户发起变更走确认门 | 边界单测 |

## 5. 风险与注意点

- **推荐方案未经逐项确认**（用户跳过）：D-01~D-05 标「采纳推荐」——**D-03 队列头撤销语义**是本对齐最关键的推荐裁决（死锁消解的唯一自洽读法），强烈建议用户复审。
- **撤销占位与 M-10「按集成键位置重新排队」的张力**：M-10 说 owning Slice 重生成后按集成键位置重新排队——该语义适用于**定向重生**分支；全局修复分支（本任务 D-03）其占位撤销、内容由修复承载——两分支队列语义不同，实现时按 RepairDecision 分支选择（模板/编排双登记防混用）。
- **联合域判定的等待循环**：在途写者长期不终态（长会话）时修复派发等待——需观测（M-13 修复会话等待时长走 run_events 即席查询）；无超时自动改判（Supervisor 后续触发可改委派）。
- **RepairBrief 装配时序**：brief_refs 在 RepairDecision（Supervisor 决策时）即产生——简报内容装配发生在决策后派发前（Harness 装配）；归因证据快照与决策时点一致（防漂移）。
- **修复会话失败停止**：会话失败≠重试消耗（物理故障同代重派；语义失败才消耗修复重试）——与 generation 语义同构区分。
- **M-00/M-07/M-11 文档同步候选**：撤销占位语义与 repair ref 族实施期回填三篇（引用本记录 D-03/CM-GIT D-03）。
- 独立重试计数与 generation 计数是**两个独立计数器**（原 Slice generation 不因全局修复消耗）——账本分离防混用。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01~Q-04 | 联合域判定算法/修复简报 schema/队列头交互/态势快照（拟提问批） | **用户未接受提问**（沿用跳过惯例）——按推荐方案采纳并如实标注；D-03 队列头撤销语义强烈建议复审 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-04/M-07/M-11/M-03/M-00/M-10 修复相关节（wave23 轮已通读）与主任务表 §7.3；用户跳过逐项确认，五项决策按推荐方案采纳（§2）；§9.1「联合域安全判定算法」「修复简报/态势快照 schema」经 D-01/D-02 收口；CM-RUNTIME 登记的 FIFO 队列头交互经 D-03 收口（死锁消解读法）——三个残留开放项全部关闭 |
| 2026-08-29 | 预算体系重对齐联动（用户发起） | 修复会话预算档联动更新：RepairSession 档改结构性轮数（max_rounds=500，CM-MEMORY 重对齐表）；修复会话同样适用分段续作机制（复杂修复多轮工具循环的正常分段——续作判定归 actor，CM-RUNTIME 重对齐 D-06）；修复重试上限 3 次（D-02 决策层级）与会话轮数/续作次数三计数器语义独立不混淆 |
