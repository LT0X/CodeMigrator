# CM-SPEC-001 对齐记录

> 用途：本文件是任务 `CM-SPEC-001`（模块 M-05 Migration Spec 抽象层）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-SPEC-001` |
| 模块编号 | M-05（+M-02 入口） |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_Migration_Spec抽象层.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 1 轮（Wave 1 轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付 Spec v3 能力门（落点：core 类型与门逻辑 + api 入口路由；主任务表形态「core 类型 + api 入口」）：

- **四道门（固定次序、短路）**：字节与 JSON 门（256 KiB/UTF-8 无 BOM/无重复 key/深度≤32）→ Schema 门（schema=`codemigrator.migration-spec`、version=3、全层级 extra="forbid"、字段类型/语言对非空/include 非空且模式合法）→ 描述符资源门（源/目标资源存在、版本与双资源 SHA-256 与镜像摘要匹配、grammar 可用、镜像可验证）→ 检查集门（(action, template_sha256) 被目标端模板覆盖、Compile 与 Test 必选各一、去重恰一次）→ 规范化与哈希。
- **v3 顶层字段校验**：name 1..=128 字节、description ≤1024 字节（不参与门判定）、descriptor_lock 恰四子字段、scope include≥1/exclude 可省、required_checks 选择对、decomposition 三可选子字段（并行度只收窄不放大沙箱公式）。
- **范围模式匹配器**：字面目录前缀/字面文件/前缀+尾段单星（`*` 仅最后一段至多一次）；拒绝 `**`/`?`/字符类/大括号/正则/绝对路径/`..`/空段/`.git` 前缀；exclude 须在 include 内收窄；`.git/` 永久排除。自实现纯函数（fnmatch 语义不符，文档语义唯一依据）。
- **有序问题响应**：JSON Pointer 字典序升序、最多 100 条、`truncated=true` 截断、门间短路。
- **canonical 规范化与哈希**：RFC 8785 JCS（rfc8785 库，D-02）→ SHA-256；覆盖业务字段（不含 SpecId/创建时间）；checks 按 (action, template_sha256) 重排、include/exclude 去重后 UTF-8 字节升序。
- **insert-or-get 去重**：同 canonical bytes 返回既有 Spec 身份。
- **migration_specs DDL**（D-04）：按 M-02 表结构（v3 canonical JSON、canonical_sha256、语言对、descriptor_lock、规范化 scope、canonical 检查集、分解策略）落 SQL 迁移文件（migrations/ 目录）。
- **Spec 拒绝码**：进 core 单一 StableErrorCode（D-01，含 SPEC_TOO_LARGE/SPEC_JSON_INVALID/SPEC_DUPLICATE_KEY/SPEC_DEPTH_EXCEEDED/SPEC_SCHEMA_UNSUPPORTED/SPEC_SCHEMA_INVALID/CHECK_ACTION_UNSUPPORTED/CHECK_SET_INCOMPLETE/DESCRIPTOR_NOT_FOUND/DESCRIPTOR_DIGEST_MISMATCH/TOOLCHAIN_IMAGE_UNAVAILABLE/SPEC_IN_USE）。
- **registry 端口**（D-03）：core 定义 DescriptorRegistry Protocol 与资源摘要类型；runtime 实现扫描/摘要/镜像验证；Wave 1 stub 冻结行为。

### 1.2 边界（不做什么）

- 不实现描述符 registry 的文件 I/O 实现（归 CM-RUNTIME，D-03）；core 仅端口。
- 不实现 repository SQL 执行层（归 CM-RUNTIME-001；本任务交付 DDL + 端口 + 逻辑测试）。
- 不实现 POST /api/v1/specs 路由形状（CM-API-001 已对齐，本任务提供其消费的门逻辑）。
- 不含 Spec 起草会话/TaskDraft/AskUser 流程（M-16/M-04，归 CM-DRAFT-001 Wave 2）；canonical Spec 唯一，草稿不占用 Spec 语义。
- 不实现 CreateRun 预检编排（归 CM-RUNTIME；两时点同一清单语义由本任务提供门函数复用）。
- 不实现 M-06 范围求值/快照扫描（`included(file)` 判定发生在 M-06 快照扫描期；本任务只交付模式合法性校验与匹配器纯函数）。
- 不实现工件识别（artifact_rules 消费归 M-06 F4/M-07）。

### 1.3 产出物

core：spec 域模型 + 四道门纯函数 + 范围匹配器 + 问题清单模型 + canonical 工具 + DescriptorRegistry 端口；migrations/：migration_specs DDL；api 侧消费集成；tests/spec/（门用例全集）+ tests/contracts/（insert-or-get/hash 稳定性）；模块迭代记录。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | 稳定拒绝码组织（M-05 约 12 码 + M-02 码 与 core StableErrorCode 关系） | 单一枚举集中 / 分模块枚举+基类型 | **单一枚举集中**：全部稳定拒绝码进 core `StableErrorCode`（M-00 网关码 + M-05 Spec 码族 + M-02 STALE_VERSION/IDEMPOTENCY_CONFLICT 等）；唯一 owner 最严格，扩码属跨模块契约变更 | 对齐问答 Q-01（2026-08-29） |
| D-02 | RFC 8785 JCS canonical JSON 实现 | rfc8785 库 / 自实现 | **引入 rfc8785 库**（规范细节由库保证；依赖经 CM-INFRA pyproject 登记） | 对齐问答 Q-02 |
| D-03 | 描述符 registry 落点（含文件 I/O，core 被禁） | core 端口+runtime 实现 / 归 analysis / core 直接实现 | **core 定义类型与 Protocol 端口，runtime 组合根实现扫描/摘要/镜像验证**；Wave 1 用经测试冻结的 stub（并行纪律） | 对齐问答 Q-03 |
| D-04 | migration_specs DDL 归属（M-01 说 migrations owner: runtime） | DDL 随 CM-SPEC / 全归 runtime | **DDL 随 CM-SPEC-001 落盘**（按 M-02 表结构）；repository SQL 实现仍归 CM-RUNTIME-001 | 对齐问答 Q-04 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（LanguageId/Sha256/CheckAction/RequiredCheck/ToolchainDescriptor 类型——CM-CORE-001）。
- DescriptorRegistry 端口（D-03，core 定义、runtime 实现、stub 先行）。
- api 路由层（CM-API-001）调用门逻辑。

### 3.2 下游消费

- M-06（范围+源端解析器引用+测试识别配置交接）、M-07（范围/检查集/分解策略/描述符锁/Spec hash 交接）、M-09/M-10（canonical 命令模板+冻结检查集）、M-03（描述符摘要收据+Spec hash）——按 M-05 交接表，不交接自由 query/源码正文/模型 edge/可变草稿。
- CreateRun preflight 复用资源门函数（两时点同一清单、同一标准）。

### 3.3 跨模块接口边界

- **core StableErrorCode 扩容联动**（D-01）：CM-CORE-001 实现须包含 M-05/M-02 码族全集（已在 core 记录变更记录追加登记）。
- Spec 不可变：`migration_specs` 无 UPDATE 路径；删除被引用返回 `SPEC_IN_USE`；修正意图=新 Spec 新 SpecId。
- 描述符锁由系统按语言对从资源账本解析写入——TaskDraft/会话上下文不能指定（M-05 起草节）。
- 三码拒绝（DESCRIPTOR_*/TOOLCHAIN_*）路由侧呈现按 M-02（422、无 run_id、零副作用）；`codemigrator_run_preflight_side_effect_total` 恒 0 语义归 M-02/M-13 观测。
- 门间短路：前门失败不产生后门第二组问题。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M05-V4-001（可施工） | 256 KiB 接收、+1 byte 解析前 SPEC_TOO_LARGE | 门单测（字节门用例表） |
| V-M05-V4-002 | 重复 key/未知字段/深度 33/version 2 零 Spec 行 | 门单测 |
| V-M05-V4-003 | 资源摘要不匹配 DESCRIPTOR_DIGEST_MISMATCH、五类新增均为 0 | 门单测（stub registry） |
| V-M05-V4-004 | 缺 Test/Compile CHECK_SET_INCOMPLETE 不进规范化 | 门单测 |
| V-M05-V4-005 | program/argv/shell/script/timeout/prompt/prompt_template 字段 schema 拒绝 | extra=forbid 全层级单测 |
| V-M05-V4-006 | action 不在模板集/摘要不匹配 CHECK_ACTION_UNSUPPORTED | 门单测（stub registry 模板清单） |
| V-M05-V4-007 | 资源缺失/语言对不一致 DESCRIPTOR_NOT_FOUND；镜像不可验证 TOOLCHAIN_IMAGE_UNAVAILABLE；零副作用 | 门单测 + 副作用计数断言 |
| V-M05-V4-008 | 键序/数组序不变 hash 与身份；include 变则新身份 | 契约测试（canonical 稳定性矩阵） |
| V-M05-V4-009 | `**`/`?`/正则/`..`/`.git` 前缀 SPEC_SCHEMA_INVALID；.git 永久排除 | 匹配器单测用例表 |
| V-M05-V4-010 | write scope/命令正文字段 schema 拒绝 | extra=forbid 单测 |
| V-M05-V4-011 | 引用中删除 SPEC_IN_USE；二次 CreateRun 重新预检 | 契约测试（stub repository） |
| V-M05-V4-012 | preflight 副作用计数在全部拒绝路径恒 0 | 门单测全局断言 |
| 有序问题响应（M-05 明文） | Pointer 升序、≤100、truncated、门间短路 | 门单测 |

## 5. 风险与注意点

- **JCS 数字规范化**：rfc8785 库为准；core canonical 工具薄封装并锁定库版本（pyproject pin），升级视为契约变更。
- **范围模式匹配器自实现纪律**：严格按 M-05 模式表实现（`*` 仅最后一段且至多一次），禁用 fnmatch/glob 语义（`**`/`?` 必须拒绝而非展开）。
- **两级时点一致性**：上传门与 CreateRun 预检用同一门函数，不得出现「上传宽、预检严」或反向的分叉实现。
- **stub 行为冻结**：Wave 1 的 registry stub 用测试锁定（资源命中/摘要不匹配/镜像失败三态），CM-RUNTIME 实现就绪后替换对齐（并行纪律 2）。
- **依赖联动**：rfc8785 进 CM-INFRA pyproject（跨任务协调点）。
- 错误码扩码走 core 变更流程：新增码=修改 core StableErrorCode=跨模块契约事件，须同步枚举表与文档（M-00 枚举表同步义务）。
- description 字段不参与门判定但入投影——校验层勿将其纳入 hash 前的拒绝路径（长度校验除外）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | M-05 新增约 12 个稳定拒绝码与 core StableErrorCode 组织方式 | 单一枚举集中（全部进 core） |
| Q-02 | RFC 8785 JCS canonical JSON 实现 | rfc8785 库 |
| Q-03 | 描述符 registry（含文件 I/O）落点 | core 端口 + runtime 实现 + Wave 1 stub |
| Q-04 | migration_specs DDL 落盘归属 | DDL 随 CM-SPEC-001（repository 实现归 runtime） |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-05 设计文档（四道门/字段清单/范围模式/交接表节）与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-04 |
