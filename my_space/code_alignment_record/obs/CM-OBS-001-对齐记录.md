# CM-OBS-001 对齐记录

> 用途：本文件是任务 `CM-OBS-001`（模块 M-13 可观测性系统）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-OBS-001` |
| 模块编号 | M-13 |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_可观测性系统.md`（V6 方向对齐版） |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 1 轮（Wave 1 骨架 → Wave 4 完善） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付观测横切体系（落点：core 契约 + runtime 装配 + app 内横切消费，D-03）：

- **Wave 1 默认链路全量**（D-01）：
  - **SecretRegistry 脱敏边界**（SEC-LOG-01）：write-only 注册表（不可枚举/读回）；对 raw/JSON-escaped/base64/percent-encoded 四编码值扫描 + 结构字段类过滤（content/old_text/new_text/prompt/messages/api_key/authorization/cookie/password/private_key/database_url 等）；fail-closed（payload 丢弃所有 sink 接收数为 0）；启动前全出口（stdout/JSONL/run_events/SSE/Problem Details/tool output/sandbox 输出/report/delivery/exemplar/CLI renderer）四编码哨兵套件，任一明文命中 server 不 ready；运行期每 10,000 事件内存哨兵+出口复验。
  - **structlog JSON 日志**：已脱敏 JSONL（64 MiB segment 封闭+SHA-256，新段 offset 0）+ stdout 降级；本地不可写退 stdout、dropped 计数。
  - **核心八指标**（descriptor 静态常量，core 发布）：run_total（terminal_status:4）/run_duration_seconds（4+bucket 10 档）/phase_duration_seconds（phase 4 值 PLAN/EXECUTE/VERIFY/REPORT × result 3=12，V6 收敛无 ANALYZE）/slice_first_pass_total（kind 4×result 2）/check_total（action 5×result 5）/sandbox_termination_total（reason:6）/budget_ratio（kind:3）/observation_dropped_total（sink:9）；71 logical labelset 上限、251 exporter series 上限（profile 无关）；指标只从 run_events 同事务投影领域事实派生（零第二通道）。
  - **60 秒 JSON 指标快照**（进程内，无外部服务依赖）。
  - **span 模型**：每 Run root span + 四 Phase 子 span（span 名固定 `migration.run/phase/slice`，ID/路径/错误文本不入名）；W3C Trace Context；UTC RFC 3339。
  - **预算告警三条**：ratio≥0.80 Warning / ≥1.00 Critical（每 Run 每 kind 一次）/ redaction fail-closed≥1 Critical；观测层只记录不代行（不关 gate/不写 checkpoint/不改 RunStatus）。
  - **V6 判断层事件观测**：advice.proposed/adopted、repair.decision、repair.session.*、探索协调者切域事件——仅事件流与 SSE 投影/即席查询，不新增第九项核心指标。
- **观测技术栈**（D-02）：structlog + prometheus-client + opentelemetry-sdk 三库齐引（纯 Python 库、零外部服务；Prometheus/Grafana/Jaeger/MinIO 均为可选 compose profile，默认全关）。
- **告警族/诊断指标收缩纪律**：模块诊断指标恰五项（integration_queue_depth/checkpoint_commit_total/test_outcome_total/attribution_regen_total/contract_drift_total），默认不启用诊断 scope；其余信号走 run_events 即席查询。
- **事件序列化上限 64 KiB**（超出转 ArtifactRef+SHA-256）；run_events 分页 `(run_id, sequence)` 单页 200、导出 10 MiB。
- **留存与清理器**：引用 M-00 留存策略表（Run 终态 30 天/AST 索引 7 天/孤儿宽限 24h）；清理器每日 UTC 02:00、每批≤1000、单批事务 5 秒。
- **Wave 4 完善范围**（D-01）：Prometheus/OTLP exporter（4096 有界队列、满丢最旧+dropped 计数）、profile 联调（metrics/dashboards/tracing/object-store）、诊断指标启用。

### 1.2 边界（不做什么）

- 不产生领域事件（事件由 M-12/M-10/M-03 等运行模块产生；本篇只脱敏记录与稳定观测）。
- 不改变 Run 终态/不代行预算关闭/不重放副作用（观测是投影不是真相）。
- 不定义留存策略（M-00 owner；本任务引用）。
- 不实现 Web 呈现（M-15 消费 REST/SSE，与观测栈无关）。
- 不为 V6 判断层新增第九项核心指标（V6 明示；诊断指标补充为开放项按需对齐）。
- 默认部署不依赖任何外部观测服务；exporter 故障只丢投影。

### 1.3 产出物

core：八指标 descriptor 常量 + SecretRegistry 类型与扫描纯函数（零 I/O）；runtime：structlog 装配/MetricRegistry/JSONL writer/60s 快照/最小 span 模型/哨兵套件/清理器/exporter 接口（Wave 1 本地+JSONL 出口）；tests/obs/（哨兵四编码/指标 descriptor hash/降级行为）；模块迭代记录。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | Wave 1 骨架交付范围 | 默认链路全量 / 仅脱敏底座 / 全量含 exporter | **默认链路全量**：SecretRegistry+哨兵+structlog JSONL+八指标+60s 快照+预算告警（V-M13-V4-001 可验）；exporter/profile 联调/诊断指标补全归 Wave 4 | 对齐问答 Q-01（2026-08-29） |
| D-02 | 观测技术栈（对齐中先澄清库 vs 服务 distinction） | 精简仅 structlog / 三库齐引 | **三库齐引**：structlog+prometheus-client+opentelemetry-sdk（Wave 1 即引入，纯库零服务；251 series/OTLP 协议不重造；Wave 4 开 profile 零增量） | 对齐问答 Q-02/Q-04 |
| D-03 | 观测体系子包落点 | core 契约+runtime 装配 / 全归 runtime | **core 契约+runtime 装配**：core 发布八指标 descriptor 常量+SecretRegistry 类型与扫描纯函数；runtime 装 structlog/MetricRegistry/JSONL/快照/exporter/哨兵套件/清理器（严格对齐 M-01「指标契约在 core 发布、装配在 runtime」） | 对齐问答 Q-03 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（RunStatus/SliceKind/CheckStatus/CheckAction/FailureReason/BudgetGate 语义类型）；run_events 领域事实（同事务投影——api/M-02 事件写入后本任务消费）。
- 依赖（CM-INFRA pyproject 登记）：structlog、prometheus-client、opentelemetry-sdk（D-02 联动）。

### 3.2 下游消费

- 全部运行模块（指标埋点消费 core descriptor 常量）；CM-API/SSE 与 Problem Details（共享 SecretRegistry 出口）；CM-WEB/M-15（不消费观测栈，走 REST/SSE）；部署侧 profile（Wave 4）。

### 3.3 跨模块接口边界

- **脱敏单一出口**：SSE data、REST 错误、日志、指标 exemplar、CLI renderer、sandbox stdout/stderr 摘要、report、delivery 全部经 SecretRegistry——任何模块不得自行序列化可见正文。
- **指标派生单一通道**：核心指标只从 run_events 同事务投影事实派生；模块诊断指标经 registry 接纳（低基数 allowlist），不参与核心 descriptor hash。
- **事件正文上限 64 KiB**：超出转 ArtifactRef——与 M-14 数据块外置、M-12 审计事件协同。
- **phase 标签四值**（V6 收敛）：PLAN/EXECUTE/VERIFY/REPORT——与 RunStatus 四阶段对齐；ANALYZE 无独立时长。
- 告警只观察终态协议：BudgetGate 结算写 budget_usage 事件+更新 budget_ratio（M-03/M-14 owner 归约过程）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M13-V4-001（可施工） | 默认 profile 全关的 Run 到达合法终态；八指标快照与终态在四个观测服务缺席时完整 | 集成测试（无 profile compose 冒烟） |
| V-M13-V4-002 | 八 descriptor 名称集合与 hash、71 labelset、两组 bucket edges、251 series 上限全 profile 组合不变 | descriptor 契约测试（静态断言+hash 计算） |
| V-M13-V4-003 | slice_first_pass_total 只从集成终态派生；label 值域恰四×二；run_id/slice_id/path/OID 不入 label | 指标单测 |
| V-M13-V4-004 | 诊断指标经 registry 静态名称+allowlist 验证；不参与核心 hash；不进验证判定 | registry 单测 |
| V-M13-V4-005 | 四编码哨兵全出口明文命中 0；任一命中 server 不 ready | 哨兵套件测试（注入 secret 样本） |
| V-M13-V4-006 | 命名规范成立；违规注册拒绝+dropped 计数 | registry 单测 |
| V-M13-V4-007 | 日志/PG/exporter 故障只产生 dropped/降级投影；RunStatus 变更 0；不重序列化未脱敏 payload | 故障注入测试 |
| V-M13-V4-008 | 30 天/7 天/24h 孤儿宽限在时间推进测试复现 | 清理器单测（时钟注入） |
| V-M13-V4-009 | 全文档与 descriptor 零残留（V3 补丁/意图/重放指标） | 静态扫描 |
| V-M13-V4-010 | Shell 审计逐命令全量；Exec 脚本全文+逐笔回执可关联；三类漂移事件经同一脱敏出口 | 事件契约测试（依赖 M-12/M-08 事件源——联调证据归后完成方，并行纪律 5） |
| V6 收敛 | phase 标签 4 值（12 labelset）；判断层事件进 run_events/SSE 不新增核心指标 | descriptor 断言+事件常量测试 |

## 5. 风险与注意点

- **依赖联动**：三库进 CM-INFRA pyproject（D-02 跨任务协调点）；opentelemetry-sdk 的 API 面只需 trace 模型+OTLP 导出，勿引入全家桶默认配置。
- **哨兵套件覆盖面**：启动前出口清单（M-13 列举 10+ 出口）必须逐一注册——遗漏出口=安全缺口；建议以出口注册表驱动（新出口必须显式注册哨兵）。
- **JSONL 目录**：本地日志落 app 数据目录（部署卷），非仓库路径；目录只读时降级 stdout（V-M13-V4-007）。
- **descriptor hash 口径**：八指标静态常量序列化后 SHA-256——实现须固定 canonical 序（与 Spec JCS 无关，自定义固定序列化即可），跨进程稳定。
- **事件量放大**：Shell 逐命令+Exec 逐笔回执放大 run_events——依赖会话配额封顶+64KiB 上限收敛，不预设降采样（M-13 定案；批次 2 实测再议）。
- **判断层诊断指标开放项**：全局修复决策计数类如需补充，走低基数模块诊断+review 清单，不改核心 hash（届时对齐）。
- run_events 指标派生的消费时点：同事务投影=事件落库后进程内更新（非 trigger）——派生实现归 runtime 装配，勿用 PG 触发器（避免第二通道）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | CM-OBS-001 Wave 1 骨架交付范围 | 默认链路全量（exporter/profile/诊断补全归 Wave 4） |
| Q-02 | 观测技术栈选型 | （先追问三件套是否重/引入多少基础设施/是否 Web 观测台所需）澄清库 vs 服务区别、默认链路零服务、Web 与观测栈无关后裁决：三库齐引 |
| Q-03 | 观测体系子包落点 | core 契约（descriptor+SecretRegistry 纯函数）+ runtime 装配 |
| Q-04 | （用户主动追问）标准三件套重吗、是否 Web 观测台所需 | 澄清：三件套是 pip 库非服务；真正服务（Prometheus/Grafana/Jaeger/MinIO）是可选 profile 默认全关；默认链路零外部服务；Web 消费 REST/SSE 与观测栈无关 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-13 V6 方向对齐版设计文档（八指标/SEC-01/审计事件/预算告警节）与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-03（含库/服务 distinction 澄清后裁决） |
