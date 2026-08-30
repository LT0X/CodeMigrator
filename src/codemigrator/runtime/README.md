# runtime

## 负责

Run actor、事务编排、调度、集成、恢复、观测装配和唯一 app 组合根。

## 不负责

不复制 HTTP DTO、语言 grammar 或 sandbox 进程实现细节。

## 允许依赖

可组合其他七个 `codemigrator` 子包，并由此持有控制面副作用。

## 公共入口

唯一 console script `codemigrator-app = codemigrator.runtime:main`。

## 观测装配

`codemigrator.runtime.observability` 提供运行时观测组合件：事件经统一的 `SecretRegistry` 脱敏后，以 structlog JSONL、进程内核心指标、60 秒快照、固定名称 trace span 和可选的有界 exporter 投影。JSONL 按 64 MiB 分段并写 SHA-256 校验；事件正文上限为 64 KiB，超限只能外置为受控 ArtifactRef。exporter 队列容量为 4096，故障或积压只增加 dropped 计数，不反向修改 Run 状态。

启动哨兵覆盖已注册的日志、事件、SSE、问题详情、工具/沙箱输出、报告交付和 CLI renderer 出口；哨兵失败时应用不进入 ready。核心指标 descriptor 由 `codemigrator.core` 发布，runtime 只负责 registry 和投影装配，禁止在本包复制状态机、错误码或动态高基数标签。

## 统一上下文管理

`codemigrator.runtime.memory.ContextManager` 为全部会话类型提供同一套上下文组合根：稳定前缀、演进前缀和定向增量按固定顺序装配；静态模板目录和结构性预算档随会话冻结。精确 token 计数与物理净输入上限通过 `TokenCounter`、`NetInputCap` 端口接入，缺少 provider 能力时 fail closed。

运行期工具结果先经过统一数据块治理：源码按 256 KiB 分段，AST 导航最多保留 200 条，Shell 超大输出采用头尾双窗并以 `ArtifactRef` 外置，完整日志不进入模型上下文。逐出只替换定向增量段内的非必要旧结果，稳定与演进前缀保持字节不变；`RecoveryBrief` 从审计事实派生，不回放对话历史。演进摘要通过 runtime schema 的 append-only 表保存，缓存键覆盖完整冻结身份且不跨 Run 复用。

`AgentLoop` 与 `SupervisorSession` 在组合时接收同一个 `ContextManager` 和锁定 provider 的精确计数端口；每次请求在发送前复核净输入上限。未注入精确能力时保留既有兼容守卫，新的 M-14 路径不会以字符估算冒充精确计数。
