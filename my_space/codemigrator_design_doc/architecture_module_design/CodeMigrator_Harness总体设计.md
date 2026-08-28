# CodeMigrator Harness：Run actor、派发接管与事实恢复

> 文档状态：V6 方向对齐版  
> 适用范围：单写者控制面、单个 Run 的命令归约、Planner 冻结计划后的并行 Slice 调度（依赖闭包就绪即启动，无全局屏障）、app 直接管理 bwrap、取消、预算终止与崩溃恢复  
> V6 演进说明：引入三层架构——权力层 Harness / 判断层常驻主 Agent / 执行层工作会话，以及 actor 对判断层建议（Advice）的两级收养规则。V5 的确定性控制面、单写者、dispatch 接管与恢复语义保持不变；V5 对齐段（见下）留存为追溯。  
> 契约真相：[M-00 垂类设计原则与架构哲学](CodeMigrator_垂类设计原则与架构哲学.md)拥有 `RunStatus`、`SliceAttemptStatus`、`CandidateGeneration`、`DispatchAttemptId`、失败原因、Git refs 与固定保留期  
> 关联文档：[系统后端架构](CodeMigrator_系统后端架构.md)、[Agent Loop](CodeMigrator_Agent_Loop设计.md)、[迁移计划生成器](CodeMigrator_迁移计划生成器.md)、[候选工作区与工具网关](CodeMigrator_候选工作区与工具网关.md)、[沙箱与执行环境](CodeMigrator_沙箱与执行环境.md)、[验证引擎](CodeMigrator_验证引擎.md)、[工作空间与 Git 集成](CodeMigrator_工作空间与Git集成.md)、[Web 体验与迁移可视化工作台](CodeMigrator_Web体验与可视化工作台.md)

Harness 编排层的职责不是把所有写操作包进一种并发协议，而是让每个事实只有一个决定者。一个 app 负责控制面写入，每个非终态 Run 由一个内存 actor（asyncio 任务）串行处理命令；并行发生在模型会话池、沙箱执行池和裁决派发池中。PostgreSQL 保存可重启事实，Git 保存代码事实，bwrap 执行结果带物理派发身份返回。CLI 是写控制命令的产品入口；Web 只把 actor 已持久化的投影和 `run_events` 呈现为迁移现场。

## V5 当前对齐

Harness 现在编排三类资源池：模型会话池（不进入沙箱，受 provider 配额与预算约束）、沙箱执行池（app 直接管理的 bwrap 活跃位，受物理资源公式约束）和裁决派发池（active-attempt gate）。不再派发到独立 worker，也不再依赖 UDS。Slice 依赖闭包就绪即启动；计划由 Planner 提案、机器校验并自动冻结，运行期只在 M-16 安全点吸收经用户确认的未集成重规划。

## 单 app 是部署约束，不是分布式选主

`app` 启动时用一条专用 PostgreSQL session 连接获取控制面 advisory lock。只有持锁进程可以通过 readiness、恢复非终态 Run 或接受迁移写命令；第二个 app 即使进程存活，也必须拒绝 ready，不能进入只写一部分事实的降级模式。

| app 生命周期事件 | 立即动作 | 后续动作 | 禁止行为 |
|---|---|---|---|
| 获得 session lock | 开放恢复流程，仍保持 readiness 关闭 | 恢复完成且 bwrap/PG 预检通过后 ready | 未恢复 actor 就接受 CreateRun |
| 未获得 lock | 保持 not-ready | 退出，由 Compose 按部署策略处理 | 作为热备读取后偷偷执行命令 |
| 持锁连接丢失 | 关闭 readiness，拒绝新命令，停止并回收 app 直接管理的活动 cgroup | app 随后退出，由 Compose 重启 | 重新连库后在原进程中继续写 |
| PostgreSQL 暂时不可用 | 保持已提交投影可诊断，但不接受写命令 | 退出并由 Compose 重启 | 先写内存、文件或 Git，稍后补库 |

advisory lock 只证明“当前有一个控制面写者”，不进入 Run、Slice、bwrap 执行回执或 Git commit。系统不保留多 app 水平扩展所需的周期协调机制；如果未来改变部署边界，必须重新设计而不是复用这把 session lock。

## 每个 Run 由一个 actor 串行决定

API、bwrap 执行回执、预算事件和恢复命令都进入相应 Run actor 的有序邮箱（asyncio.Queue）。actor 负责检查当前 Run/Slice 投影、作出确定性归约，并按 M-02 拥有的 `next_event_sequence` 规则把状态变化和 `run_events` 在一个 PostgreSQL 事务提交。数据库 `version` 仍对外投影，供 GET 响应 ETag 和 DELETE 的 `If-Match` 使用；事件序列的分配细节由 M-02 唯一定义，本篇不复制。内部步骤不对每次写入重复 expected-state/version 比较。

```mermaid
flowchart LR
    API["API commands 接口命令\nCreate 创建 / Cancel 取消"] --> Actor["Run actor 运行执行器\n单 Run 决策串行"]
    Exec["bwrap 执行结果\n带 DispatchAttemptId 派发尝试 ID"] --> Actor
    Budget["Usage / budget facts 用量与预算事实"] --> Actor
    Recovery["Startup / disconnect recovery 启动与断连恢复"] --> Actor
    Actor --> Tx["PostgreSQL transaction 事务\nprojection 投影 + run_events 运行事件"]
    Actor --> Scheduler["Ready Slice scheduler 就绪切片调度器\n跨 Run 公平轮转"]
    Scheduler --> Pools["三类资源池\nmodel / sandbox / decision"]
    Actor --> Integrator["Integration Coordinator 集成协调器\n冻结顺序"]
    Integrator --> Git["Git expected-OID refs 预期 OID 引用"]
```

| actor 拥有的决定 | 输入事实 | 可写事实 | 不直接执行 |
|---|---|---|---|
| Run 阶段推进 | M-00 状态边、当前 Slice 投影、验证 receipt | Run projection、failure reason、event | HTTP DTO、Git 命令、沙箱进程 |
| Slice 调度 | M-07 四类 Slice DAG ready 集、write scope 互斥、全局资源许可 | active dispatch 集合、Slice attempt 投影 | 扩大 write scope、改变集成顺序 |
| 集成准入 | 冻结队首、局部验证 receipt、integration intent/receipt | 集成投影与 event | 跳过 M-10 prospective checks |
| 取消 | 已持久化外部命令、当前 active attempts | `cancel_requested`、取消投影与 event | 把取消归约成部分完成 |
| 终态 | 已集成 Slice、类型化 terminal failures、最终验证 | Run terminal projection | 改写报告或代码交付状态 |

actor 邮箱除命令、执行回执、预算事件与恢复命令外，新增 `Advice` 建议消息类型，来自判断层常驻主 Agent（见下文"判断层接入与减法降级"）。actor 对 Advice 按两级收养处理：

| 建议类别 | 收养方式 | 代表 | 行为约束 |
|---|---|---|---|
| 约束内建议（actor 白名单自动收养） | 机械校验兜底、自动执行，无人值守不打断 | EXECUTE Supervisor 的修复决策、探索员改派 | 仅落在 E-ACT-XX 白名单内；`proposal_hash` 核验通过方可收养，未达门槛不改写既定归约 |
| 边界性建议（actor 转化为既有确认门/提问通道） | 注入既有确认门或提问通道 | PlanRevision 建议、AskUser 建议 | 不自动写入事实；经用户确认或既有安全点后才切换 |

决策权始终在 Harness；Advice 只是建议，不携带写权限。建议消息必须带 `proposal_hash`，供 actor 在收养前核验内容未被篡改；结构性与计划类建议的 hash 生成规则、白名单门槛与 `Advice` 消息的 schema 由实施期对齐后在 M-16 相关章节唯一定义，本篇不复制。

一个 Run actor 的阻塞不得阻塞其他 Run。scheduler 在不同 Run 间公平轮转；同一 Run 只选择 DAG ready 且 write scope 互斥的 Slice，若 Planner 选择了 Contract Slice，则依赖该契约的 Slice 在其依赖闭包满足后 ready，不存在固定的契约波/实现波屏障，也不为 `RunStatus` 增加规划分组状态。真正耗时的模型调用、Agent 工具箱执行（M-12 的 `ReadFile/WriteFile/EditFile/QuerySourceAst/Shell/Exec` 六工具，作用于本 Slice 候选工作区即沙箱卷，write scope 双轨防护）、Git 集成事务和 sandbox 检查在 actor 外执行，完成后以不可变 receipt 回到邮箱，actor 不在等待 I/O 时阻塞事件循环。

## DispatchAttempt 只识别一次物理派发

每次 app 启动物理 bwrap 执行都会生成新的 UUIDv7 `DispatchAttemptId`。actor 在 PostgreSQL 维护 active dispatch 集合，唯一键固定为 `run_id + canonical(ExecutionSubject identity) + check_id`；每个键恰有一个 active attempt，不同 Slice、不同 check，以及 prospective/final subject 可以按资源许可并行。entry 保存 attempt、完整判别 subject、CheckId 与 `tested_commit_oid`；物理中断后的重派只替换该 entry 的 attempt，不消耗语义 generation。物理重派范畴显式包含模型基础设施故障（provider 5xx/超时/断流）：会话级模型调用失败按 30s/60s/120s 逐次退避做**同代重派**——不消耗 generation，每次中断以 `dispatch.interrupted` 审计事件记入 `run_events`（语义追认 owner 为 M-00）。

| 返回校验 | 匹配 | 不匹配 |
|---|---|---|
| `run_id + subject identity + check_id` | 找到唯一 active entry | 记录 `LATE_DISPATCH_RESULT`，不归约业务结果 |
| `dispatch_attempt_id` | 继续检查 | 视为取消、断连或重派后的迟到结果 |
| `tested_commit_oid` | 可归约 CheckResult 或失败事实 | candidate/scratch/verified revision 已变化，结果失效 |

迟到成功与迟到失败采用相同行为：只写低基数审计事件，不生成 `CheckResult`，不推进 candidate 或 verified，也不改变当前 attempt 的重试计数。这个窄 gate 保护异步返回边界，不流入外部执行协议、Agent 文件操作或普通数据库写入。

**边界例：**同一 candidate 同时执行 Compile 与 Test，它们拥有两个 active keys 和两个 attempt。某个 bwrap 执行位中断后，actor 分别将对应 entry 记为 `INTERRUPTED` 并为同一 tested OID 创建新临时验证目录。旧 Compile 随后返回“通过”；即使 tested OID 未改变，attempt 已不是对应 key 的 active 值，结果仍被丢弃，也不会影响新 Test。

## bwrap 执行中断即接管，不等待时间窗口

app 直接派生 bwrap 并持有每个执行的 cgroup、PDEATHSIG 和 active-attempt 身份。bwrap 不连接 PostgreSQL、Git 或控制面，也不能在 app 不可用时继续孤立工作。

| 故障方向 | bwrap 侧 | app 侧 | 恢复结果 |
|---|---|---|---|
| app 崩溃或控制面失效 | PDEATHSIG/cgroup 终止全部活动进程组 | 新 app 获锁后重建 actor | 未完成 entry 标为 `INTERRUPTED`，按同一 generation 重派 |
| bwrap 执行中断 | 该执行位退出 | 对应 active entry 记为 `INTERRUPTED` | Run 未取消且 subject 有效时，以新 attempt 和新临时验证目录重派 |
| 单一沙箱进程异常 | 终止其进程组并返回类型化失败 | 按 active attempt gate 接收 | 由错误类别决定同 attempt 失败或物理重派 |

取消传播由 app 直接终止对应 bwrap cgroup；清理上限由沙箱模块拥有。无论执行位是否及时返回确认，actor 一旦持久化取消就不得创建新 generation、dispatch 或 integration。

## 取消先成为事实，再成为 HTTP 成功

`DELETE /api/v1/migrations/{run_id}` 的 API 层只校验 `If-Match` 的语法并构造 `CancelCommand(expected_version)`，不在 actor 之外先读 version 作决定。actor 处理该命令时才比较 expected version 与当前 Run version；不匹配返回 `STALE_VERSION` 且写入数为零，匹配则在同一 PostgreSQL 事务写入 `cancel_requested=true`、新 version 与对应 `run_events`，提交后才向 API 确认接受。这是外部取消命令的并发保护，不被复用为内部步骤的通用 CAS。CLI 的首次 Ctrl+C 同样只能走这条命令链：它不能直接终止 bwrap、写 PostgreSQL 或修改 Git；`STALE_VERSION` 时客户端最多刷新一次投影并以新 version 重试一次，是否继续等待终态属于 [M-15](CodeMigrator_Web体验与可视化工作台.md) 的交互边界。

取消事实提交后：未派发 Slice 保持不启动，活动 attempt 收到终止请求，集成队列停止消费，未验证 candidate 归入 abandoned 证据，已经推进的 verified 主线保持不变。Run 最终恒为 `CANCELLED`，即使已有一个或多个 Slice 集成成功，也只在终态投影中展示这些事实，不改写为 `PARTIALLY_COMPLETED`。

## checkpoint 加速恢复，不证明完成

checkpoint 仍按每 10 个任务或 60 秒生成，保存 actor 可重建的游标、已提交 receipt 引用和候选索引；它不是任务完成证明，也不是 [M-08](CodeMigrator_候选工作区与工具网关.md) 候选工作区的 checkpoint commit——后者把 Agent 文件操作后的工作区文件集提交为代码事实，由 Git refs 与 checkpoint 幂等键保护。完整控制事实来自 PostgreSQL，代码事实来自 Git，正文事实来自 host CAS。checkpoint 损坏时允许忽略并重建，不得因为缓存内容领先而推进状态。

| 事实/派生物 | 恢复 owner | 恢复动作 | 幂等边界 |
|---|---|---|---|
| 非终态 Run 与 Slice | PostgreSQL | 重建 actor、ready 集和 frozen integration order | 唯一 Run/Slice ID |
| active dispatch 集合 | PostgreSQL + app 内 bwrap 执行事实 | 启动时逐 entry 标为 `INTERRUPTED`，必要时创建新 attempt/临时验证目录 | `(run, subject identity, check_id)` 唯一约束 |
| candidate/verified/scratch | Git refs | 由 M-11 判定 candidate 继续、失败归档或集成恢复 | expected OID ref transaction |
| candidate checkpoint commit 已写、receipt 缺失 | Git candidate ref 已是 checkpoint commit | 按 M-08 checkpoint 幂等键补写 receipt | 不重复应用工作区文件集 |
| integration intent 无 receipt | PG intent + Git ref | 比对 expected/new OID，重试推进或补写 receipt | intent/receipt 唯一键 |
| checkpoint | PG/CAS 引用 | 可验证则加速；损坏则舍弃 | 不作为终态 guard |

Recovery Coordinator 只由 app 启动、bwrap 执行中断或已知 integration intent 缺口触发，不常驻扫描。若 Git verified 已等于 intent 的 new OID 而 receipt 缺失，恢复只补写 receipt；若仍等于 expected OID，则按原 intent 幂等重试；若两者都不等，停止该 Run 并报告 ref drift，不猜测覆盖。

## 预算耗尽关闭新工作

Run usage ledger 累计模型 token 与成本。达到 80% 只写一次告警事件；任一上限达到 100% 时，actor 关闭新模型、工具、generation、dispatch 与 integration，保存 checkpoint，归档未验证 candidate，再以 `FailureReason.BudgetExhausted` 进入 `FAILED`。checkpoint I/O 失败会形成 `CHECKPOINT_WRITE_FAILED` 证据，但不能重新开放预算门或启动 provider。

报告正文生成仍属于 `REPORTING`。正文生成、脱敏或持久化失败使 Run 进入 `FAILED`；终态后报告投递与 push/PR 只改变统一枚举 `DeliveryChannelStatus` 的对应交付 ledger 投影（`report_delivery_status` / `code_delivery_status`，两列分立互不影响）。这些投影的重试可使用幂等唯一键，但不经过 Run actor 重开迁移主线。

## 贯穿场景：并行候选在 app 重启后继续汇合

一个 TS→Python 翻译 Run 进入 `EXECUTING` 后，Planner 冻结的 integration_rank 决定 A、B 的集成序；二者 write scope 不相交且依赖闭包就绪时，actor 同时为 generation `0` 创建独立候选工作区和长期沙箱卷，并在 active dispatch 集合中分别建立 active entry。bwrap 执行中断时，对应 entry 被记为 `INTERRUPTED`，actor 在不改变 generation 的情况下为该键创建新临时验证目录并重派；迟到结果只产生审计事件。

A prospective head 验证通过后，Integration Coordinator 写入 intent、以 expected verified OID 推进 Git，再准备写 receipt；此时 app 崩溃。PDEATHSIG/cgroup 清理 bwrap 执行位。新 app 取得 session lock，从 PostgreSQL 重建 actor，发现 Git 已是 intent 的 new OID，遂补写 receipt 而不重复集成 A。B 从原 candidate generation 继续。最终所有可集成 Slice 处理后，Run 单向进入 `VERIFYING` 执行翻译后全套测试的最终全量验证，再进入 `REPORTING`。

## 可检查的运行性质

- 第二个 app 无法获得 session advisory lock 时 readiness 为失败，且控制面写入数为零。
- 任一 Run 的状态、Slice 调度、集成和终态决定只经该 Run actor；不同 Run 仍可并行。
- 旧 DispatchAttempt 在取消、断连或重派后返回成功时，不生成 CheckResult，不推进 candidate/verified。
- bwrap 执行中断立即产生 `INTERRUPTED` 并创建新 attempt，不依赖周期时间窗口；物理重派不增加 generation。
- app 失效后由 PDEATHSIG/cgroup 清空沙箱进程组；清理失败进入基础设施失败，不由孤立进程接管。
- cancel API 只在 `cancel_requested` 与事件提交后确认；此后新 generation、dispatch 和 integration 数均为零。
- Git 已推进、receipt 缺失时恢复只补 receipt；intent 已写、Git 未推进时按 expected OID 幂等重试。
- 损坏 checkpoint 可仅由 PostgreSQL、Git 与 CAS 引用重建，不被解释为完成事实。
- 预算达到 100% 后新模型与工具调用数为零，且 checkpoint/归档先于 `FAILED`。
- 当前实现不存在周期任务续权、全链路代次令牌、字节级前置哈希守卫、Run 级共享候选引用、内部通用状态 CAS、独立事件中继或常驻轮询恢复器。

## 会话输入只在安全点改变调度

CLI 的 Ctrl+C 仍通过 `CancelCommand(expected_version)` 进入本 actor；会话 message、AskUser answer 与 correction confirmation 则由 [M-16](CodeMigrator_会话与运行时修正编排.md) 先持久化。actor 收到新的 CorrectionIntent 后把 InteractionStatus 置为 `PAUSING_FOR_INPUT`，停止新模型调用、dispatch、候选工作区 Agent 文件操作、generation 与 integration，并让已有原子操作抵达安全点。只有 intent 已写时，actor 先完成 expected-OID Git 对账与 receipt 恢复，随后才进入 `WAITING_FOR_USER` 或 `APPLYING_CORRECTION`。

局部修正应用后 actor 切换到新的 PlanRevision；结构修正只在 preview hash 被确认后切换。它不接受来自 CLI/Web 的 bwrap 终止、Git ref 写入或数据库直写请求。Final Verify 开始后，输入成为后续 TaskDraft，当前 actor 保持 `VERIFYING → REPORTING` 的单向主线。

## 判断层接入与减法降级

判断层常驻主 Agent 以 Advisor 身份存在，不进入执行层工作会话，也不改写控制面事实；它对 actor 输出 `Advice` 建议（见上文"每个 Run 由一个 actor 串行决定"）。EXECUTE Supervisor 触发事件集仅两条：

- **归因多义/双错**：候选修复集 >1 时，触发全局修复决策建议（避免 actor 机械归约无法在多义间取舍）。
- **Slice 会话失败停止**：Slice 会话失败且停止时，触发异常语义路由建议（把失败原因映射到既有异常处置路径）。

Supervisor 观察为被动唤醒式：基线态势快照由机器计算并持久化，不注入上下文；仅在触发事件到达时的间歇性注入态势快照与定向事件投影，供主 Agent 生成 Advice。

关键约束：主 Agent 是增益层不是依赖层。判断层缺席（模型故障、预算耗尽）时，控制面完整性与机制完全相同；系统退回机械归约，优雅降级，不成为新的单点。VERIFY/REPORT 的零模型硬边界不变。

> 设计演进、历史缺陷处置和变更理由见：[文档迭代记录](文档迭代记录.md)。
