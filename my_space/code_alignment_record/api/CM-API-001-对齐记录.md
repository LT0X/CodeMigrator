# CM-API-001 对齐记录

> 用途：本文件是任务 `CM-API-001`（模块 M-02 系统后端架构）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-API-001` |
| 模块编号 | M-02 |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_系统后端架构.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 1 轮（Wave 1 骨架 → Wave 4 完善） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/api/` 子包 + runtime 存储落点（M-01：api 只消费 core 端口、禁 SQL/Git/进程；PostgreSQL repository/run_events/schema 归 runtime，组合根绑定）：

- **REST 路由全集（19 条，M-02 路由表冻结）**：`POST /api/v1/specs`、`POST /api/v1/migrations`、`DELETE /api/v1/migrations/{run_id}`（If-Match→CancelCommand）、`GET /api/v1/migrations{,/{run_id},/{run_id}/workspace,/{run_id}/events,/{run_id}/report,/{run_id}/evidence/{receipt_id},/{run_id}/changes,/{run_id}/output}`、`GET /api/v1/descriptors`、`GET /api/v1/system/health`、`POST /api/v1/projects/register`、`GET /api/v1/projects`、`POST /api/v1/sessions`、`POST /api/v1/sessions/{id}/messages|answers|confirm`、`POST /api/v1/sessions/{id}/corrections/{cid}/confirm`、`GET /api/v1/sessions/{id}/events`、`GET /api/v1/skills`。
- **DTO 投影（Pydantic v2，字段以 M-02 为准）**：SpecView/DescriptorLockView/RequiredCheckView、SliceView（含 kind/write_scope/integration_rank）、MigrationView、workspace/report/evidence 聚合、`migration.event` v1 信封（schema/version/type/data/sequence/timestamp_utc 六字段）。
- **SSE 投影与回放**：sse-starlette（D-02）；15s 心跳先补读后心跳、`Last-Event-ID` 严格向后、内存游标 `last_sent_sequence`、有界队列 64（满则断连）、单进程连接上限 100（429）、LISTEN/NOTIFY 仅唤醒、终态后关连接。
- **幂等**：`(principal_id, route, key)` 作用域、24h 留存、canonical body 匹配；同键同 body 重放首响应、同键异 body `409 IDEMPOTENCY_CONFLICT`。
- **CreateRun 流程**：body/认证/schema/BranchPrefix 校验 → 描述符预检（三码拒绝归 M-05）→ 投影存在断言 + 档案一致性断言（失败 `DOSSIER_INCONSISTENT` 零副作用）→ Run/幂等记录/首事件（`run.status_changed`→PLANNING）同事务写入。
- **四投影纪律**：run_status / verification_outcome / report_delivery_status / code_delivery_status 互不篡改。
- **安全面**：单静态令牌+loopback 绑定、`principal_id` 固定常量、401/404 遮蔽、body 1 MiB、Spec 256 KiB、HTTPS-only repository_url、RFC 9457 Problem Details（扩展字段仅 request_id/run_id/retryable）。
- **事件名常量**：`RunEventType(str, Enum)` 归 api（CM-CORE-001 D-05 延续），含 V5 全集 + V6 判断层事件（advice.proposed/adopted、repair.decision、repair.session.started/completed 等）。
- **事件 data schema**：分两档（D-03）。

### 1.2 边界（不做什么）

- api 子包不读环境变量（令牌/DSN/配置经 runtime 组合根注入依赖）；不连 SQL/Git、不起进程（M-01 禁止事项）。
- repository/表结构/事务实现归 runtime（CM-RUNTIME-001）；本任务交付端口 Protocol 与投影消费。
- Spec 四道门语义/拒绝码 owner 为 M-05（CM-SPEC-001）；本任务只做路由与 DTO 投影。
- 事件语义 owner：`tool.call.*`/`checkpoint.pre` 归 M-12/M-08 网关点位；`verification.completed`/`test.*` 归 M-10；`advice.*/repair.*` 生产归判断层——api 只做信封与只读投影。
- Web/CLI 展示归约归 M-15；会话输入编排归 M-16。
- filesystem/JSONL 控制面 backend 不进入 feature flag/Protocol 实现/测试矩阵（M-02 明文）。
- MinIO/观测 profile 只作可选 adapter，不在默认闭环。

### 1.3 产出物

`src/codemigrator/api/`（dto 按域、routes 按资源 router、events.py 信封+常量、sse.py、deps.py 端口注入、problems.py RFC 9457 handler）、runtime 侧端口 Protocol 定义与 stub 实现（供骨架联调）、tests/api/（mock 单测）+ tests/contracts/（真实 PG 契约）、模块迭代记录。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | Wave 1 骨架交付范围 | 全路由骨架+核心链路 / 最小四路由 / 全量一次到位 | **全路由骨架+核心链路**：全部路由声明与 DTO、SSE/幂等/四投影/CreateRun 断言流程（对接 stub actor）；判断层事件投影与 evidence/skills 细节 Wave 4 完善 | 对齐问答 Q-01（2026-08-29） |
| D-02 | SSE 实现技术 | 手写 StreamingResponse / sse-starlette | **sse-starlette 库**（引入依赖；心跳/补读循环仍按 M-02 语义定制覆盖其默认行为） | 对齐问答 Q-02 |
| D-03 | 事件 data schema 深度（M-02 声明为开放项） | 分两档 / 全部占位 / 全量定模型 | **分两档**：V4 表已列明 data 关键字段的事件（run/slice/candidate/dispatch/verification/test/tool/checkpoint/integration/verified/report/delivery）按所列字段定 Pydantic 模型；判断层事件（advice.*/repair.*）仅定 type 常量与最小占位，字段形状实施期与用户对齐后补（开放项纪律） | 对齐问答 Q-03 |
| D-04 | API 测试 PostgreSQL 接入 | mock 单测+真实 PG 契约 / 全 mock / 全真实 PG | **mock 单测+真实 PG 契约**：路由/DTO/幂等逻辑 httpx ASGI + mock 端口单测；事务/回放/sequence 连续性契约测试对真实 PG 17（compose 或 testcontainers） | 对齐问答 Q-04 |

> 以下为沿用既有决策/文档事实的默认项（未单独提问）：Problem Details 用 FastAPI 自定义 exception handler（零依赖）；测试异步后端 anyio；api 内部组织沿用按域分模块风格（CM-CORE-001 D-02 延续）；`/api/v1` 前缀固定。

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（SpecId/RunId/SliceId/RunStatus/SliceAttemptStatus/SliceKind/WriteScope/CandidateGeneration/CheckAction/Sha256 等）；runtime 注入的端口（RunCommand 端口、投影读取端口、事件读取端口——Protocol 定义在 api，实现绑定在 runtime 组合根）。

### 3.2 下游消费

- `apps/codemigrator-cli` 与 `web/` 只消费本任务 REST/SSE 投影（M-01：不进核心子包依赖图）。
- CM-OBS 消费 run_events 投影做指标；CM-WEB（Wave 4）消费判断层事件只读呈现。

### 3.3 跨模块接口边界

- 事件词汇（RunEventType 常量）owner = api（M-02 事件集表 + CM-CORE-001 D-05）；生产方（actor/M-10/M-12/判断层）写库时引用。
- 四投影互不篡改（M-02 表）；取消 If-Match→`STALE_VERSION` 归 actor 比对。
- 三码拒绝（DESCRIPTOR_NOT_FOUND/DESCRIPTOR_DIGEST_MISMATCH/TOOLCHAIN_IMAGE_UNAVAILABLE）语义 owner = M-05/M-02 联合（CM-SPEC-001 对齐时细化路由侧呈现）。
- `DOSSIER_INCONSISTENT` 为 CreateRun 后置断言分支错误码（M-00 FailureReason 成员，零副作用拒绝）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M02-V4-005（可施工） | 投影与 run_events 同事务；注入失败共同回滚（含 next_event_sequence）；sequence 连续无缺口无重复 | 真实 PG 契约测试（D-04） |
| V-M02-V4-006（可施工） | SSE 断线按 Last-Event-ID 严格向后补读；杀 NOTIFY 后 heartbeat 补读零丢失 | 契约测试（PG + SSE 客户端模拟） |
| V-M02-V4-010（可施工） | 幂等键作用域/24h/同键同 body 重放/异 body 409 | 契约测试（真实 PG 幂等表） |
| V-M02-V4-001（追溯） | Spec DTO v3 校验拒绝（program/argv/prompt/write scope 字段零出现） | DTO 单测（unknown-field deny 用例） |
| V-M02-V4-002（追溯） | 描述符预检失败三码、无 run_id、零副作用 | 骨架 stub 预检单测（真实 registry 语义归 CM-SPEC/ANALYSIS） |
| V-M02-V4-003/004（追溯） | Slice 投影携带 kind 与输出路径 write scope；check 来源为模板实例化记录 | DTO/聚合单测 |
| V-M02-V4-007/008/009（追溯） | contract_wave 恰一条派生事件；tool.call 成对；归因/flaky 事件与账本一一对应 | 骨架期以事件常量+信封测试覆盖；行为验收归 runtime/M-10/M-12 任务 |
| V-M02-V4-011（追溯） | 双交付通道隔离（push/PR 失败只改 code_delivery_status） | 投影归约单测 |
| V-M02-V4-012（追溯） | 全文档零残留（plugins/GuardedPatch/edit intent 等） | 路由表扫描审查 |
| V5 增量 | CreateRun 四件工件 ref/hash + 断言分支 + 首事件 PLANNING | mock actor 骨架集成测试 |
| V6 增量 | advice.*/repair.* 事件 type 常量与只读投影占位 | 事件常量测试（data 两档占位，D-03） |

## 5. 风险与注意点

- **sse-starlette 定制点**：心跳周期/补读先行语义/队列满断连必须覆盖库默认行为，逐条对齐 M-02 连接限制表；勿让库的默认 ping 语义替代补读循环。
- **开放项纪律**：判断层事件 data 字段形状不得臆造（M-02 明示实施期开放项）；占位 schema 待 CM-SUPERVISOR/CM-REPAIR 对齐后回填（届时走记录变更流程追加）。
- **幂等 body 校验时机**：canonical body 匹配在路由层做（键命中后先比对 body 再决定重放/409）；幂等记录写库在同事务边界内。
- **安全面不降级**：loopback 绑定/单令牌/404 遮蔽/HTTPS-only 是部署纪律，测试环境同样执行（除绑定地址经注入配置）。
- **路由与表结构联动**：M-02 表结构适配节（migration_specs/runs/slices/plan_edges/required_checks/check_results/run_events/artifacts）是 runtime/CM-INFRA migrations 的输入，本任务只消费不改。
- `slice.status_changed` 的 kind 投影、`execute.contract_wave_completed` 仅观测语义（非时序依据）——路由/DTO 不得将其用于任何调度判断。
- heartbeart/连接上限/队列容量为「可配常数」：默认 15s/100/64，配置注入经 runtime（api 不读环境）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | CM-API-001 Wave 1 骨架交付范围 | 全路由骨架+核心链路（判断层事件投影与细节路由 Wave 4 完善） |
| Q-02 | SSE 事件流实现技术 | sse-starlette 库（定制覆盖默认心跳/补读语义） |
| Q-03 | 事件 data schema 开放项处理 | 分两档：已列明字段定模型，判断层事件占位待定 |
| Q-04 | API 测试 PG 接入方式 | mock 单测 + 真实 PG 契约测试 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-02 V6 设计文档（路由表/SSE 协议/事件集/四投影节）与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-04 |
| 2026-08-29 | 判断层事件占位档回填（wave23 联动） | CM-SUPERVISOR-001 对齐 D-03（推荐采纳）：advice.*/repair.* 事件 data 模型回填本记录 D-03 的判断层占位档——advice.proposed{advice_id/kind/role/tier/proposal_hash/payload 摘要}、advice.adopted{advice_id/proposal_hash/adoption_result/影响摘要}、repair.decision{repair_decision_id/修复集摘要/域分配摘要}；见 code_alignment_record/supervisor/CM-SUPERVISOR-001-对齐记录.md D-03 |
| 2026-08-29 | 预算体系重对齐联动（用户发起） | 新增会话分段续作事件常量（如 slice.segment_continued：续作派发/续作计数/续作资格不满足转终态——data 摘要字段随实施定稿）；归 api 事件常量族（CM-CORE D-05 惯例）；SSE/REST 投影消费方归 CM-WEB Wave 4（渲染完备性义务 V-M15-V4-029 联动） |
