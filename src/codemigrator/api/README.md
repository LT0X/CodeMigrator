# `codemigrator.api`

## 负责

提供 CodeMigrator 的 REST/SSE 外部控制面：封闭 DTO、静态令牌认证、If-Match 并发控制、幂等命令、RFC 9457 Problem Details，以及从 `run_events` 读取的严格序列事件流。

## 不负责

本包不直接访问 SQL、Git、CAS 或执行进程，不读取环境变量，也不实现 Run/Slice 领域状态归约。所有命令、查询和事件读取能力均经注入的 `ApiBackend` 端口提供。

## 允许依赖

API 只消费 `codemigrator.core` 公共契约；runtime 组合根负责绑定真实控制面实现。API 不定义第二套状态机、错误码或语言工具链知识。

## 公共入口

通过 `create_app()` 创建 FastAPI 应用，`route_surface()` 提供稳定的路由清单。Run 与 Session SSE 分别使用 `migration.event` 和 `migration.session.event` v1 信封，沿用严格的 `Last-Event-ID` 回放、心跳补读和有界连接队列；写请求使用主体、路由和幂等键组成的 24 小时幂等范围。

Run 事件目录允许 `agent_run.started` 与 `agent_run.terminal` 作为已提交进度投影，不新增公共 Run 状态或 AgentRun 查询资源。API 只保留 opaque `agent_run_id`、既有 phase/session kind，以及可选且成对出现的 `slice_id`/`generation`；terminal 事件再保留既有 exit 与受限回执类别。内部 receipt key、thread/checkpoint/CAS 引用、AgentState、提示词、源码、工具正文及 provider 错误正文均由 DTO allowlist 丢弃。AgentRun 终态事件不会结束 Run SSE；连接是否终止仍由既有 Run terminal event 决定。SessionEvent 可复用相同 DTO 投影，但当前还没有接通生产 Draft AgentRun lifecycle event publisher。
