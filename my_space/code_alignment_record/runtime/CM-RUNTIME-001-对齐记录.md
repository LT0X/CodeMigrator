# CM-RUNTIME-001 对齐记录

> 用途：本文件是任务 `CM-RUNTIME-001`（模块 M-03 Harness Run actor 含判断层收养）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-RUNTIME-001` |
| 模块编号 | M-03（V6 方向对齐版）+ M-02 存储落地（组合根侧） |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_Harness总体设计.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐（用户跳过逐项确认，决策采纳推荐方案——沿用 CM-VERIFY 惯例，见 §6） |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/runtime/` 子包（M-01 组合根：唯一读环境/装配依赖/entry point `codemigrator-app`）：

- **单 app 启动门**：PostgreSQL session advisory lock（专用长连接）；四行生命周期表（获锁恢复→ready/未获锁 not-ready 退出/持锁丢失关 readiness 收 cgroup 退出/PG 不可用退出重启）；readiness 流程；第二实例零写入。
- **Run actor**：每非终态 Run 一个内存 actor（asyncio 任务）串行处理邮箱；**邮箱协议（D-02）**=asyncio.Queue typed 消息六类（API 命令 CreateRun/Cancel/会话输入信号、bwrap 执行回执、预算事件、恢复命令、Advice）；状态转换+run_events 同事务（M-02 next_event_sequence 规则）；内部步骤零通用 CAS（version 只对外投影 ETag/If-Match）。
- **Advice 两级收养（D-01，§9.1 开放项收口）**：
  - **白名单两级=AdviceKind 枚举两级标注（M-00 注释即终值确认）**：约束内={ExploreReassignment, RepairDecision}（自动收养+机械校验兜底）；边界性={RouteSuggestion, PlanRevision, AskUser}（转确认门/提问通道，不自动写入）。
  - **proposal_hash=SHA-256(canonical(advice_id, kind, run_id, role, payload))**（JCS via core canonical 工具——覆盖身份+内容防篡改防重放）。
  - **收养流程四步**：①重算 proposal_hash 核验一致（不一致丢弃+审计）②kind 白名单分级③约束内→机械校验（RepairDecision：修复集⊆归因候选集+联合域成员校验；ExploreReassignment：覆盖恰好一次+扇出上限）→通过自动执行+`advice.adopted` 事件；④边界性→注入既有确认门/AskUser 通道。
- **Slice 调度**：DAG ready 集+write scope 互斥+三池资源许可；跨 Run 公平轮转 scheduler；依赖闭包就绪即启动无全局屏障；耗时操作（模型/工具/Git/检查）在 actor 外执行回执入邮箱。
- **DispatchAttempt gate**：active dispatch 集合（唯一键 run_id+canonical(subject)+check_id，每键一 attempt）；三行返回校验（LATE_DISPATCH_RESULT 零归约）；物理重派含模型 provider 故障 30s/60s/120s 退避同代重派（不消耗 generation，`dispatch.interrupted` 事件）。
- **取消协议**：CancelCommand(expected_version) actor 内比对（STALE_VERSION 零写入）；取消持久化后新 generation/dispatch/integration 零；恒 CANCELLED。
- **checkpoint 加速恢复**：每 10 任务或 60 秒（游标/receipt 引用/候选索引）；损坏舍弃重建不作完成证明（与 M-08 候选 checkpoint commit 区分——两回事）。
- **Recovery Coordinator**：触发式（启动/中断/已知 intent 缺口）；六行恢复表（非终态 Run 重建/active dispatch INTERRUPTED/Git refs 恢复/receipt 补写/intent 幂等重试/checkpoint 舍弃）；ref drift 停止报告。
- **预算终止（2026-08-29 重对齐：定位收窄为 Run 钱包断路器）**：usage ledger 累计（provider usage 回执精确累计——token 计量唯一用途）；80% 恰一次告警；100% 关闭新调用→checkpoint→归档→BudgetExhausted→FAILED；CHECKPOINT_WRITE_FAILED 证据不重开门。**定位语义**：Run 级 input/output/cost 三项=用户钱包粗粒度兜底（防烧钱爆炸，不预测不精确控制）；**不承担会话控制职责**（会话控制=结构性轮数预算，CM-MEMORY 重对齐）——100% 断路是唯一直达 BudgetExhausted 的预算路径（会话轮数耗尽走分段续作不再直达 Run FAILED）。
- **分段续作判定编排（D-06 重对齐新增）**：会话轮数耗尽/模型提前停事件入邮箱→actor 机械判定续作资格（三条件全满足）：①Run 钱包未断（<100%）②本段会话有实质进展（write scope 内有新文件产出或 checkpoint diff 非空——防磨洋工空转）③续作次数 <3 次/generation（可配，与 generation 计数、修复重试计数三者独立）；通过→派发同 generation 续作会话（从最近 checkpoint 重建+分段进度摘要——消费 CM-MEMORY RecoveryBrief 扩展与 M-08 重派重建基建）；不通过→Slice 终态失败归约（IndependentSliceTerminalFailure）；续作事实入 run_events（事件常量归 api）。
- **判断层接入**：Supervisor 触发事件集两条（D-04：归因多义候选修复集>1→全局修复决策建议；Slice 会话失败停止→异常语义路由建议）→经会话框架派发新决策会话（CM-LOOP 框架+CM-MEMORY 定向装配）；减法降级（判断层缺席→机械归约，控制面完整性不变）。
- **全局修复集成序**：Coordinator 单写者串行通道；修复条目按完成 FIFO 追加（无冻结 rank——CM-GIT D-03 对接）；不插队越过冻结队首；prospective 建立在集成时最新 verified；独立重试上限（CM-VERIFY D-02：3 次）+预算断路器。
- **会话输入安全点**：CorrectionIntent→PAUSING_FOR_INPUT→安全点收敛→WAITING_FOR_USER/APPLYING_CORRECTION；Final Verify 后输入转后续 TaskDraft 单向主线。
- **报告正文生成（D-05）**：REPORT 确定性模板拼装（零模型——M-00 定案落地归 runtime）；生成失败→FAILED；终态后交付只改 DeliveryChannelStatus。
- **三池治理**：模型会话池（provider 配额+预算）/沙箱执行池（物理公式）/裁决派发池（active gate）。

### 1.2 边界（不做什么）

- 不做 HTTP DTO/SSE（M-02 owner——CM-API 已对齐；api 子包经端口投递命令入邮箱）。
- 不做六工具执行/工作区（M-08/M-12 owner——已对齐；actor 经端口派发）。
- 不做 bwrap 物理隔离（M-09 owner——已对齐）。
- 不做 Git 事务原语（M-11 owner——CM-GIT 已对齐；Coordinator 消费其原语）。
- 不做验证裁决（M-10 owner——CM-VERIFY 已对齐；消费 guard 与归因证据）。
- 不做判断层会话本体（M-04 owner——CM-LOOP/CM-SUPERVISOR 对齐；actor 只做收养）。
- 不做全局修复会话执行（CM-REPAIR 对齐；actor 派发+重试计数+FIFO 入队）。
- 不做状态机/枚举定义（M-00 owner——core 契约）。
- 不做上下文装配（M-14 owner——CM-MEMORY 已对齐；经端口）。
- 不做观测装配细节（M-13 owner——CM-OBS 已对齐；组合根绑定）。
- 不做 POSTGRES schema 演进细节（migrations owner: runtime——CM-SPEC/CM-MEMORY DDL 先例+本任务补充 run 侧表 DDL）。

### 1.3 产出物

runtime 子包：app 组合根（advisory lock/entry point/readiness）、Run actor（邮箱归约/状态推进/调度/取消/预算/收养）、Integration Coordinator（冻结序消费+intent 编排——消费 CM-GIT 原语）、Recovery Coordinator、scheduler（跨 Run 公平轮转）、报告模板拼装器、provider 会话派发端口（对接 CM-LOOP）；run 侧 migrations DDL（runs/run_events/active dispatch/intent/receipt/usage ledger 等按 M-02 表结构）；tests/runtime/（10 条运行性质用例/收养矩阵/取消/恢复窗口/预算归约）；模块迭代记录（dev_progress/runtime/）。

## 2. 关键实现决策与确认结论

> 用户跳过逐项确认（沿用 CM-VERIFY 惯例）——按推荐方案采纳并如实标注；用户可经再对齐修订。

| # | 决策点 | 可选项 | 采纳结论（推荐方案） | 依据 |
|---|---|---|---|---|
| D-01 | Advice proposal_hash schema 与两级白名单终值（§9.1 开放项；M-03 说 hash 规则/门槛/schema 实施期在 M-16 定） | canonical 哈希+枚举两级 / 押后 | **proposal_hash=SHA-256(JCS canonical(advice_id/kind/run_id/role/payload))**；**白名单两级=AdviceKind 枚举两级标注终值**（约束内{ExploreReassignment,RepairDecision}/边界性{RouteSuggestion,PlanRevision,AskUser}）；收养四步流程（hash 核验→分级→约束内机械校验→执行/边界性转门） | 推荐采纳（用户跳过确认，2026-08-29） |
| D-02 | actor 邮箱协议形态 | asyncio.Queue typed 六类 / 自定义总线 | **asyncio.Queue+typed 消息六类**（API 命令/执行回执/预算事件/恢复命令/Advice/会话输入信号）；单 Run 串行；跨 Run 公平轮转 | 推荐采纳 |
| D-03 | 组合根装配内容与 advisory lock 实现 | PG session advisory lock / 应用级锁 | **PG `pg_try_advisory_lock` 专用长连接**（M-03 语义字面）；组合根装配：lock/恢复/actor 生命周期/scheduler/Coordinator/Recovery/观测绑定/配置注入（M-01 组合根定位） | 推荐采纳 |
| D-04 | Supervisor 触发接线 | 两条事件集直译 / 扩展触发 | **两条事件集直译**（M-03 定案：归因多义候选修复集>1、Slice 会话失败停止；不设防御性触发——Supervisor 精简定案）；经 CM-LOOP 会话框架派发新会话 | 推荐采纳 |
| D-05 | 报告正文生成归属 | runtime 确定性模板 / 独立模块 | **runtime 确定性模板拼装器**（REPORT 零模型定案落地；模板为版本化受信资源；素材=verified facts/证据页素材投影） | 推荐采纳 |
| D-06 | 分段续作判定编排（重对齐新增·用户确认） | 机械三条件判定 / 押后 | **actor 机械判定续作资格**（钱包未断∧实质进展∧续作<3 次/generation）→派发同 generation 续作会话（checkpoint 重建+分段进度摘要）；不通过→Slice 终态失败归约；续作计数与 generation/修复重试三计数器独立 | 重对齐 Q-06（2026-08-29·用户确认） |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（RunStatus 状态机/Advice/RepairDecision/SliceAttemptStatus/IntegrationIntent/fingerprint 语义/幂等键构成）；CM-API（命令端口/事件常量/next_event_sequence 规则）；CM-SANDBOX（bwrap 适配/执行回执）；CM-GIT（Git 事务原语）；CM-VERIFY（guard/归因证据/RepairEvidence）；CM-LOOP（会话框架/模型调用回执）；CM-MEMORY（装配器端口）；CM-PLAN（DAG 冻结事实）；CM-OBS（观测装配）。

### 3.2 下游消费

- CM-API（投影消费——run_events/四投影）；CM-SUPERVISOR（Advice 收养接口对接）；CM-REPAIR（派发+重试计数+FIFO 入队）；CM-WEB/M-15（终端呈现经 api 投影）。

### 3.3 跨模块接口边界

- **Advice 收养接口**（并行纪律 4 交叉点）：本任务冻结收养流程与白名单；CM-SUPERVISOR 对齐 Advice 产出侧（payload schema 与机械校验规则联动细化——若校验规则需扩展在 SUPERVISOR 记录登记并回本记录变更行）。
- **修复 FIFO 与队列头交互**：修复条目 FIFO 追加无冻结 rank（CM-GIT D-03）；「不插队越过冻结队首」与「队首 Slice 因等待修复阻塞时修复条目如何入队」的精确交互归 CM-REPAIR 对齐收口（本任务提供 Coordinator 单写者串行通道语义）。
- **重试计数器**：全局修复重试计数归 actor（CM-VERIFY D-02 数值 3 次消费）。
- **模型 provider 物理重派**：会话级失败 30s/60s/120s 退避归 actor 邮箱归约（M-00 语义）；provider adapter 实现归 CM-LOOP。
- **run_events 事务**：状态+事件同事务写库归 actor（经 storage 端口——SQLAlchemy 归本任务组合根装配，asyncpg 池）。
- **run 侧 DDL**：runs/run_events/active_dispatch/integration intent/receipt/usage/idempotency 等表 migration 归本任务（M-02 表结构适配节落地；CM-SPEC migration_specs/CM-MEMORY 演进段表先例）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| 10 条可检查运行性质（M-03 定案） | 第二实例零写入/单 actor 决定/迟到结果零归约/中断即接管零时间窗/PDEATHSIG 清空/取消后零新增/恢复窗口两向/损坏 checkpoint 重建/预算 100% 零新调用/零周期机制扫描 | 运行性质测试集（每条一用例） |
| M-03 各表 | 生命周期四行/actor 决定五行/返回校验三行/恢复六行/取消传播 | 集成测试 |
| V6 收敛 | Supervisor 新会话式触发/FIFO 同队不插队/独立重试上限+预算断路器 | 触发接线单测（D-04） |
| D-01 收养矩阵 | hash 篡改丢弃/约束内校验通过自动执行/校验失败不收养/边界性转门零自动写入 | 收养单测（kind×结果矩阵） |
| V-M00 交叉条款 | P-04 单写者/V-M00-V4-003~007（运行性质来源）/V-M09-V5-005/006（PDEATHSIG/重派） | 与 sandbox/git 联调证据（并行纪律 5） |
| V-M02-V4-005 | 状态+事件同事务回滚 | 契约测试（真实 PG） |

## 5. 风险与注意点

- **推荐方案未经逐项确认**（用户跳过）：D-01~D-05 标「采纳推荐」——goal 实现前建议用户快速复审 §2（尤其 Advice 收养矩阵——判断层核心机制）。
- **Advice 收养与 SUPERVISOR 联动**：机械校验规则（RepairDecision 修复集⊆候选集等）的精确校验项在 CM-SUPERVISOR 对齐时终定——本任务提供校验框架与 hook 点。
- **修复 FIFO 队列头交互**：归 CM-REPAIR 收口（本任务 Coordinator 语义已冻结：单写者串行/FIFO 追加/不越队首/基线取最新）。
- **actor 事务与 SQLAlchemy async**：异步 session 管理（事务边界/回执落库同事务）——run_events 写入与 M-02 幂等规则联动；asyncpg 池配置注入。
- **恢复触发不轮询**：Recovery Coordinator 严格事件触发（启动/中断/intent 缺口）——实现勿引入周期扫描。
- **checkpoint（M-03 游标型）与 checkpoint commit（M-08 代码型）同名不同义**——实现与文档命名注意区分（建议内部命名 actor_checkpoint vs candidate_checkpoint）。
- **报告模板资源**：版本化受信资源（与模板库同机制）；正文素材来源=verified facts+证据页素材（CM-VERIFY 产出）。
- 预算 usage ledger 计量来源=provider adapter usage 回执（CM-LOOP 侧）——端口先行 stub。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01~Q-05 | Advice hash/白名单终值、邮箱协议、组合根、触发接线、报告生成归属（拟提问批） | **用户未接受本批提问**（沿用 CM-VERIFY 跳过惯例）——按推荐方案采纳并如实标注；后续任务同此处理 |
| Q-06 | （重对齐·用户发起）Run 预算定位+续作判定编排 | Run 预算=钱包断路器定位（保留三项契约，收窄会话控制职责）；续作判定=actor 机械三条件（新增 D-06）——随预算重对齐四项推荐确认 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-03 V6 方向对齐版设计文档与主任务表 §7.3；用户跳过逐项确认，五项决策按推荐方案采纳（§2）；§9.1「Advice 白名单精确枚举与 proposal_hash schema」经 D-01 收口（白名单=AdviceKind 枚举两级终值确认+canonical hash 规则）；修复 FIFO 队列头交互登记归 CM-REPAIR 收口 |
| 2026-08-29 | 预算体系重对齐（用户发起·DSH 哲学） | ①预算终止定位收窄为 Run 钱包断路器（token 计量唯一用途=usage ledger；不承担会话控制——M-03 文档偏差登记：预算节定位表述实施期同步）；②新增 D-06 分段续作判定编排（actor 机械三条件：钱包未断∧实质进展∧续作<3 次/generation→同 generation 续作会话；不通过→Slice 终态失败；会话轮数耗尽不再直达 Run FAILED——M-04 出口表联动）；③续作计数器与 generation/修复重试三计数器独立（M-00/M-03 契约扩展登记） |
