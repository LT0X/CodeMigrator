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
