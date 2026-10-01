# runtime

## 负责

Run actor、事务编排、调度、集成、恢复、观测装配和唯一 app 组合根。

## 不负责

不复制 HTTP DTO、语言 grammar 或 sandbox 进程实现细节。

## 允许依赖

可组合其他七个 `codemigrator` 子包，并由此持有控制面副作用。

## 公共入口

唯一 console script `codemigrator-app = codemigrator.runtime:main`。

## V7 图与 AgentRun

`RuntimeGraphAssembly` 在 runtime 组合根注入 ProviderRegistry、ContextManager、ToolGateway、RuntimeStore、CAS、usage sink 与图 checkpointer，并编译独立的 `MigrationSessionGraph` 和 `RunWorkflowGraph`。Run、Draft、AgentRun 使用彼此隔离的 checkpointer；缺少依赖、阶段工厂或 saver 隔离时 fail closed。该 assembly 不改变八子包边界，也不提供绕过现有 API/owner port 的写入口。

该 assembly 是显式注入 seam：当前没有默认 production PLAN/VERIFY/REPORT/Draft runner factories；`RuntimeApplication.from_dsn()` 不会自行创建该 assembly。生产 REST/SSE composition 位于根级 `codemigrator.asgi.create_production_app`，将 API-owned `ProductionApiBackend` 通过 Protocol 接到 `PostgreSQLRuntimeStore` 与 `RunCreationOwner`。宿主可提供 `run_components_factory`，在 PostgreSQL pool 与 advisory-lock write connection 建立后，用同一 `PostgreSQLRuntimeStore` 组装 `ProductionRunComponents`：包括确定性 preflight、`RuntimeGraphAssembly`、RunActor factory，以及 durable-checkpointer 明确声明。组合根校验 assembly/store 对象身份；生产 durable assembly 还要求 CAS reference store 与 Run/Draft/AgentRun 三个 `CasCheckpointSaver` 全部绑定同一应用 `RuntimeStore` 和 host CAS，然后创建 receipt-idempotent graph starter。actor factory 同时用于新 Run 和恢复 Run。此入口提供生产 Run 组件装配边界，但 planner/execute 等阶段实现仍由宿主注入；没有 stage factories 或创建门禁时相应写路径继续 fail closed。Draft 命令和其它未接入 owner 的路由仍 fail closed，组件/API fake 测试不等同于真实后端到 Web 的 E2E。

每个完整模型会话由一个持久 AgentRun 标识，`create_agent` 只承载会话内部模型/工具循环。固定模型绑定由 ProviderRegistry 提供，工具仅暴露授权的 ToolGateway wrapper，每次模型调用前由 M-14 middleware 组装受预算上下文。PlanAgentRun 必须在 provider 调用前取得 RunActor 提交的启动回执；EXECUTE scheduler 将启动与结果命令交回 RunActor mailbox。Actor 提交 PlanValidation/FrozenPlan 或 M-08 候选 checkpoint 等 owner 事实后，图才能消费回执并推进。VERIFY/REPORT 保持确定性。

外层图 checkpoint、AgentRun checkpoint 和候选代码 checkpoint 属于不同恢复边界。LangGraph checkpoint 正文经 host CAS 保存，PG 只保留索引与引用；Draft/只读会话可恢复既有 thread，EXECUTE/Repair 写会话从 M-08 候选代码 checkpoint 重建并创建新 AgentRun/thread。

## 观测装配

`codemigrator.runtime.observability` 提供运行时观测组合件：事件经统一的 `SecretRegistry` 脱敏后，以 structlog JSONL、进程内核心指标、60 秒快照、固定名称 trace span 和可选的有界 exporter 投影。JSONL 按 64 MiB 分段并写 SHA-256 校验；事件正文上限为 64 KiB，超限只能外置为受控 ArtifactRef。exporter 队列容量为 4096，故障或积压只增加 dropped 计数，不反向修改 Run 状态。

启动哨兵覆盖已注册的日志、事件、SSE、问题详情、工具/沙箱输出、报告交付和 CLI renderer 出口；哨兵失败时应用不进入 ready。核心指标 descriptor 由 `codemigrator.core` 发布，runtime 只负责 registry 和投影装配，禁止在本包复制状态机、错误码或动态高基数标签。

## 统一上下文管理

`codemigrator.runtime.memory.ContextManager` 为全部会话类型提供同一套上下文组合根：稳定前缀、演进前缀和定向增量按固定顺序装配；静态模板目录和结构性预算档随会话冻结。精确 token 计数与物理净输入上限通过 `TokenCounter`、`NetInputCap` 端口接入，缺少 provider 能力时 fail closed。

运行期工具结果先经过统一数据块治理：源码按 256 KiB 分段，AST 导航最多保留 200 条，Shell 超大输出采用头尾双窗并以 `ArtifactRef` 外置，完整日志不进入模型上下文。逐出只替换定向增量段内的非必要旧结果，稳定与演进前缀保持字节不变；`RecoveryBrief` 从审计事实派生，不回放对话历史。演进摘要通过 runtime schema 的 append-only 表保存，缓存键覆盖完整冻结身份且不跨 Run 复用。

`AgentLoop` 与 `SupervisorSession` 在组合时接收同一个 `ContextManager` 和锁定 provider 的精确计数端口；每次请求在发送前复核净输入上限。未注入精确能力时保留既有兼容守卫，新的 M-14 路径不会以字符估算冒充精确计数。
