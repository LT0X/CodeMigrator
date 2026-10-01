# `codemigrator.api`

## 负责

提供 CodeMigrator 的 REST/SSE 外部控制面：封闭 DTO、静态令牌认证、If-Match 并发控制、幂等命令、RFC 9457 Problem Details，以及从 `run_events` 读取的严格序列事件流。

## 不负责

请求 backend 不绕过 owner/store 端口写 SQL、Git、CAS 或执行进程，不读取环境变量，也不实现 Run/Slice 领域状态归约。生产 ASGI 工厂只负责创建池、初始化 store、持有应用 advisory lock，并在恢复后绑定 backend。

## 允许依赖

API 只消费 `codemigrator.core` 公共契约和本地 Protocol；根级 `codemigrator.asgi` 组合模块绑定 runtime store 与 owner 实现。API 不定义第二套状态机、错误码或语言工具链知识，`codemigrator.api` 不导入 `codemigrator.runtime`。

## 公共入口

通过 `create_app()` 创建 FastAPI 应用，`route_surface()` 提供稳定的路由清单。Run 与 Session SSE 分别使用 `migration.event` 和 `migration.session.event` v1 信封，沿用严格的 `Last-Event-ID` 回放、心跳补读和有界连接队列；写请求使用主体、路由和幂等键组成的 24 小时幂等范围。

`ProductionApiBackend` 仅开放有持久 owner 能力的 CreateRun 命令与 Run/Draft 事件流读取；CreateRun 需要注入真实的门禁与 graph starter。根级 `codemigrator.asgi` 可用 `run_components_factory(store, pool, write_connection)` 接收应用生命周期持有的同一 RuntimeStore、连接池和 advisory-lock write connection，并返回经校验的 `ProductionRunComponents`；其 `RuntimeGraphAssembly` 必须绑定相同 store，宿主还要显式确认 checkpointer 可跨进程恢复。该 factory 同时注入 RunActor factory，创建和恢复共用它。成功响应表示 PostgreSQL 已原子提交 RunCreated 事实、API 幂等回执和待启动 handoff，并已安排后台图任务；它不等待 PLAN→EXECUTE→VERIFY→REPORT 全图结束。PENDING handoff 只有在图任务完成后才标记 STARTED；关闭或中断会保留 PENDING，供下次启动恢复或相同幂等请求重排。生产启动会在 readiness 前读取并安排待恢复 handoff，但不等待迁移图运行完成。`create_production_app()` 管理 PostgreSQL pool、schema 初始化、单实例锁、handoff 恢复和关闭；RuntimeStore 的 PostgreSQL 事实写入共用持锁的专用连接并串行执行，读取与事件监听继续使用连接池。锁连接断开时 PostgreSQL 会中止该 session 上的在途写事务，再释放 advisory lock，使新实例只有在旧写事务结束后才能获锁。Draft 写入及其他未接入 owner 的路由使用既有安全依赖错误 fail closed，不返回占位 projection。

Run 事件目录允许 `agent_run.started` 与 `agent_run.terminal` 作为已提交进度投影，不新增公共 Run 状态或 AgentRun 查询资源。API 只保留 opaque `agent_run_id`、既有 phase/session kind，以及可选且成对出现的 `slice_id`/`generation`；terminal 事件再保留既有 exit 与受限回执类别。内部 receipt key、thread/checkpoint/CAS 引用、AgentState、提示词、源码、工具正文及 provider 错误正文均由 DTO allowlist 丢弃。AgentRun 终态事件不会结束 Run SSE；连接是否终止仍由既有 Run terminal event 决定。SessionEvent 可复用相同 DTO 投影，但当前还没有接通生产 Draft AgentRun lifecycle event publisher。
