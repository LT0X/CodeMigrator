# CM-LOOP-001 对齐记录

> 用途：本文件是任务 `CM-LOOP-001`（模块 M-04 执行 + M-16 执行节·Agent Loop 执行会话）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-LOOP-001` |
| 模块编号 | M-04（V6 收敛版 fb11·四阶段模型会话编排与调用循环唯一 owner）+ M-16 执行节 |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_Agent_Loop设计.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐（用户跳过逐项确认，决策采纳推荐方案——沿用惯例，见 §6） |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/runtime/` 会话循环部分（M-01：runtime 组合根；会话编排+provider adapter 落 runtime；M-04 唯一 owner 的循环语义）：

- **四阶段编排**：档位绑定（PLAN→Reasoning/EXECUTE→Code；VERIFY/REPORT 零模型）；ANALYZE 已并入 CreateRun 非模型 phase；阶段入口检查（phase/status 配对/取消标记/冻结 binding/预算门；EXECUTE 另匹配身份三元组）。
- **EXECUTE 调用循环**：会话状态机六态（Created→Running→CheckpointPending→Closed/Invalidated）；一 Slice generation 一会话绑定候选工作区+Code 档；循环=模型请求工具→Gateway 执行回写→继续；**会话出口四类（2026-08-29 重对齐：三→四）**：①声明完成→checkpoint 提交；②失败停止→失败事实移交；③Run 钱包断路（100%）→唯一直达 BudgetExhausted 归约的预算路径；④**轮数耗尽/模型提前停→分段续作**（M-04 原三出口的「预算节点」语义重定义——轮数上限是分段点不是终止判决：checkpoint 保存进度→actor 判定续作资格（CM-RUNTIME 重对齐 D-06）→同 generation 续作会话（从 checkpoint 重建+分段进度摘要注入定向增量段）；「模型提前停」=模型输出自由文本停顿而非工具调用/声明完成——复杂任务的正常分段现象，同走续作）；单轮多段动作编码逐段执行逐段回灌（前段失败不吞并后段）；会话身份失效零发布。
- **会话循环并发模型（D-02）**：每会话一个 asyncio 任务；模型调用 await（不阻塞 actor 事件循环——M-03 邮箱模式）；工具调用经 ToolGateway（CM-WORKSPACE 已对齐）；Loop 不拥有状态机（归 actor）。
- **provider adapter（D-01，CM-MEMORY D-03 归属收口；2026-08-29 重对齐修订）**：轻量自研——OpenAI 兼容 chat completions 主协议（httpx 异步客户端）+Anthropic Messages 可选副协议；**token 计数单轨化**（重对齐简化原「双轨」）：事后=provider usage 回执精确累计→Run 钱包 ledger（唯一 token 计量用途）；事前装配门只需**物理防溢出**（净输入≤窗口的粗校验——字符启发+安全余量 20% 即可，非预算职责；原「M-14 零近似偏差登记」随预算体系重构失效——上下文窗口重定位为物理边界，粗校验天然合规）；**轮数计数归会话循环**（每轮模型调用+1，达 max_rounds 触发分段续作出口——CM-MEMORY 重对齐档位）。locked binding 四字段（provider/model/config revision/context window+output cap 探针）不变。
- **归一器（D-05）**：模型输出双形态（[cm:*] 标记分段+严格 JSON 兼容回退）收敛到 schema admission；provider json mode=可选加速非正确性依赖；归一失败不静默降级（根因走 run_events）。
- **KV-cache 前缀 provider 适配（D-04）**：OpenAI 兼容=自动前缀缓存（无显式控制，字节稳定即命中）；Anthropic=`prompt_caching` cache_control 显式标记（稳定前缀段+演进段尾）——adapter 侧实现；命中率观测归 M-13；三段式装配契约消费 CM-MEMORY D-02。
- **会话类型模板清单（D-03）**：`core://session-templates/v1` manifest 条目=SessionKind 九值（CM-MEMORY D-01）+Drafting（起草会话——CreateRun 前特殊阶段独立模板槽位）共十个模板槽位（正文实施期随各会话类型任务填充）；会话差异只在模板+预算档（统一装配器不分类实现）。
- **四类 Slice 会话上下文构成**：契约/实现/测试翻译/测试生成四类 pack 构成表（M-04 唯一 owner）；**测试类信息防火墙**（不含被测实现目标正文——装配侧 M-14 强制+运行期工具边界强制）；dispatch 冻结契约引用不随 verified 漂移。
- **重生成历史注入**：前代失败诊断摘要+前代 checkpoint diff 摘要恰两段（CM-MEMORY 已对齐装配侧）；规则条目提案出口（系统性误译→归因引用条目→Harness 审计→Rulebook 版本递增）。
- **协调会话类型**：探索协调者（交互式贯穿起草全程，切域调整建议权+focus brief——CM-DRAFT D-03 消费）；EXECUTE Supervisor（事件触发式新会话，两职责——CM-RUNTIME D-04 触发接线消费）；只出 Advice 零直写；只读工具面。
- **修复会话/重生升级包（M-04 侧构成）**：修复会话身份 (run_id, repair_decision_id) 不属原 Slice 不占 generation 0-2；读=全 Run verified 树+源快照+失败测试+PSF-2 调用链+修复简报（必要输入）；写=联合域（CM-REPAIR 对齐收口判定）；验证走 checkpoint→局部→集成。重生升级包=常规重生成 pack+全境读视野+修复简报（仍占原 generation）。
- **P-05 落地**：数据地位声明（system 提示）+源码永不入 system message+动作侧防线；五来源使用边界表。
- **会话 Agent 与迁移 Agent 隔离**：输出通道 actor 处汇合互不直连；AskUser 不在 phase 工具面。
- **失败传播**：七行表（provider 重试冻结策略/工具 frame 非法零执行/越界类型化拒绝/Shell·Exec 归约可自纠/会话失效零发布/预算 100% 停止/取消 gate）；provider 基础设施错误 30s/60s/120s 退避同代重派（M-03 归约）。

### 1.2 边界（不做什么）

- 不做状态机/调度/集成/终态归约（M-03 owner——CM-RUNTIME 已对齐；Loop 只消费指令开新会话）。
- 不做工具网关/六工具执行（M-08/M-12 owner——CM-WORKSPACE 已对齐；Loop 传递请求）。
- 不做 Context Pack 装配/预算档/逐出（M-14 owner——CM-MEMORY 已对齐；Loop 消费装配结果与治理规则）。
- 不做 checkpoint 事务（M-08/M-11 owner；声明完成后移交）。
- 不做 Planner 提案逻辑（M-07 owner——CM-PLAN 已对齐纯逻辑；PLAN 会话循环用本任务通用会话框架实例化，提案 schema/校验消费 planning 端口）。
- 不做起草会话流程语义（M-16 owner——CM-DRAFT 已对齐；本任务提供其工具面与循环边界+模板槽位）。
- 不做 Supervisor 决策语义/Advice 产出（CM-SUPERVISOR 对齐；本任务提供会话框架实例化）。
- 不做修复会话编排/联合域判定（CM-REPAIR 对齐；本任务提供会话类型构成与循环）。
- 不做验证裁决（M-10 owner——已对齐）。
- 不做报告拼装（归 CM-RUNTIME D-05）。

### 1.3 产出物

runtime 会话循环：通用会话框架（状态机+循环+身份失效+单轮多段解析回灌）、provider adapter（双协议客户端/usage 计数/重试退避/prefix cache 适配）、归一器（双形态收敛）、locked binding 管理；core 侧：模板 manifest 十槽位登记；tests/loop/（循环状态机/身份失效/防火墙/多段回灌/失败传播/provider 重试——adapter stub）；模块迭代记录（dev_progress/agent-loop/）。

## 2. 关键实现决策与确认结论

> 用户跳过逐项确认（沿用惯例）——按推荐方案采纳并如实标注。

| # | 决策点 | 可选项 | 采纳结论（推荐方案） | 依据 |
|---|---|---|---|---|
| D-01 | provider adapter 实现与 token 计数（CM-MEMORY D-03 归属收口；用户模型 256k/1M） | 轻量自研双协议 / litellm 网关 / SDK | **轻量自研**：OpenAI 兼容 chat completions 主协议+Anthropic Messages 可选副协议（httpx 异步）；计数双轨（事后 usage 回执精确累计=ledger 严格；事前装配门保守字符估算+20% 余量防溢出，官方 tokenizer 可得时优先）——装配门估算为登记偏差（M-14 实施期同步「零近似」表述限定 ledger 侧） | 推荐采纳（2026-08-29） |
| D-02 | 会话循环并发模型 | asyncio 任务每会话 / 线程池 | **asyncio 任务模型**：每会话一任务、模型调用 await、工具经 gateway；与 actor 邮箱（M-03）非阻塞协同 | 推荐采纳 |
| D-03 | 会话类型模板清单（CM-WORKSPACE D-02 联动收口） | 十槽位 / 九值+起草特殊 | **manifest 十槽位**：SessionKind 九值（CM-MEMORY D-01 含 RepairSession）+Drafting 独立槽位（CreateRun 前特殊阶段）；正文实施期随任务填充 | 推荐采纳 |
| D-04 | KV-cache 前缀 provider 适配（CM-MEMORY D-02 联动收口） | adapter 双策略 / 押后 | **双策略**：OpenAI 兼容=自动前缀缓存（字节稳定即命中）；Anthropic=cache_control 显式标记稳定段+演进段尾；命中率观测归 M-13 | 推荐采纳 |
| D-05 | 归一器实现 | 双形态收敛+json mode 加速 / 单形态 | **双形态收敛**（[cm:*] 分段+严格 JSON 回退→同一 schema admission）；json mode 可选加速非正确性依赖；失败不静默降级 | 推荐采纳 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（Phase/ModelProfile/SessionKind/错误码）；CM-MEMORY（装配器端口/预算档/token 计数端口/模板加载）；CM-WORKSPACE（ToolGateway/六工具执行面）；CM-RUNTIME（dispatch 指令/会话派发端口/usage 回执归约）；CM-PLAN（Planner 提案 schema 端口）；CM-DRAFT（起草会话工具面约束）；CM-SANDBOX（Shell 执行）。

### 3.2 下游消费

- CM-RUNTIME（会话终止移交/审计事实）；CM-SUPERVISOR/CM-REPAIR（会话框架实例化）；CM-VERIFY（自检不入账本的边界——P-02 隔离）；CM-MEMORY（工具结果治理输入）。

### 3.3 跨模块接口边界

- **会话框架统一实例化**：PLAN 会话（Run 级 Reasoning 短会话——Planner LLM 循环消费 planning 提案端口，CM-PLAN D-05 纯逻辑对接）/EXECUTE 四类/协调两类/修复/起草——同一循环机制，差异=模板+预算档+pack 构成。
- **adapter 计数端口**（CM-MEMORY D-03 联动）：TokenCounter/NetInputCap/usage 回执三接口——本任务实现；M-14 装配器消费。
- **token 估算偏差登记**：M-14「零近似」限定 ledger 侧精确（usage 回执）；装配门保守估算防溢出（20% 余量）——实施期 M-14 同步表述（引用本记录 D-01）。
- **模型重试归 actor**（M-03）：会话级 provider 失败→邮箱归约 30s/60s/120s 退避同代重派——adapter 提供可重试错误分类。
- **模板正文回填联动**：十槽位 manifest 先行（本任务）；各会话类型任务实施期填正文并递增 manifest 版本。
- **规则条目提案通道**：会话产出→Harness 审计→Rulebook 受控追加（M-00 契约；不构成写通道）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M04-V4-001/005/010 | 六工具面/两阶段只读/VERIFY·REPORT 空集/两档 profile；CheckRunner TOOL_NOT_FOUND | 循环单测（对接 gateway） |
| V-M04-V4-002/003/011/012 | 越界零副作用/沙箱宿主零触碰/零控制面写/并行零共享 | 联动安全测试（workspace/sandbox） |
| V-M04-V4-004 | 自检不入 fingerprint | 隔离单测 |
| V-M04-V4-006/007/021 | 四类 pack 不混用；system 零源码；信息防火墙 | 装配联动单测（memory） |
| V-M04-V4-008/009 | 会话失效零发布；取消后零接纳 | 状态机单测 |
| V-M04-V4-013/014 | 回执溯源（含 Exec 内序）；自由文本不进事实通道 | 审计单测 |
| V-M04-V4-015 | 闭包就绪即启动；并行数=ready 互斥数 | 调度联动单测（runtime） |
| V-M04-V4-016 | Exec 逐笔过网关防护不降级 | Exec 桥单测（workspace 联动） |
| V-M04-V4-017 | 测试生成 pack 恰三件+GENERATED 标注 | 装配单测 |
| V-M04-V4-018 | 起草会话零写接纳/Exec 只读编排 | 起草循环单测（draft 联动） |
| V-M04-V4-019 | 重生注入恰两段 | 装配联动单测 |
| V-M04-V4-020 | checkpoint 待定态生命周期+自纠上限 | 状态机单测 |
| V-M04-V4-022 | 档案质量纪律（锚点 100%/覆盖自述/advisory 标注） | 起草联动单测 |
| V5/V6 增量 | 四件确认/扇出探索/无子 agent/三段式/修复路由收敛 | 上述覆盖 |
| D-01 adapter | 双协议/计数双轨/重试退避/binding 四字段 | adapter 单测（stub provider） |
| D-03 模板槽位 | manifest 十槽位 sha256 核验 | 资源加载单测 |

## 5. 风险与注意点

- **推荐方案未经逐项确认**（用户跳过）：D-01~D-05 标「采纳推荐」——**D-01 token 估算偏差**尤其建议用户复审（M-14「零近似」语义限定为 ledger 侧——涉及 M-14 文档同步）。
- **adapter 与真实模型联调**：model_api_key.json 模型接入后验证 usage 回执精确性/估算余量充分性（256k/1M 窗口下 20% 余量足够保守）；provider 差异（GLM/DeepSeek 兼容度）实测。
- **PLAN 会话归属双登记**：循环归本任务（通用框架实例化）、提案逻辑归 CM-PLAN——联调证据双方登记（并行纪律 5）。
- **修复会话构成中「联合域写」**：写权限冻结的判定归 CM-REPAIR 对齐——本任务提供会话类型构成与循环边界。
- **自纠重声明上限**（V-M04-V4-020 实施期参数）：建议默认 3 次（可配）——防无界循环；与反馈修复上限 2（M-10）语义区分（前者=checkpoint 拒绝自纠；后者=局部验证反馈修复）。
- **[cm:*] 解析器与模型兼容性**：分段协议对模型提示词要求——模板正文实施期包含编码说明；解析失败回灌纠偏格式与 OBSERVATION 语义联动。
- 起草会话（Drafting 模板槽位）不入 Run 内 ContextPackIdentity 体系（M-14 明文）——模板机制复用但 identity 独立。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01~Q-05 | provider adapter/并发模型/模板清单/前缀适配/归一器（拟提问批） | **用户未接受提问**（沿用跳过惯例）——按推荐方案采纳并如实标注；D-01 的 M-14 偏差登记建议用户复审 |
| Q-06 | （重对齐·用户发起）预算体系重构联动：会话出口四类+adapter 计数单轨化+轮数计数归循环 | 确认（随预算重对齐四项推荐）；会话出口表/续作机制/计数定位见 §1.1 修订 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-04 V6 收敛版设计文档与主任务表 §7.3；用户跳过逐项确认，五项决策按推荐方案采纳（§2）；CM-MEMORY D-03 归属的 provider adapter 经 D-01 收口（含 token 估算偏差登记）；模板十槽位清单经 D-03 收口（CM-WORKSPACE D-02 联动） |
| 2026-08-29 | 预算体系重对齐联动（用户发起·DSH 哲学） | ①会话出口三→四（原「预算节点」出口重定义为「轮数耗尽/模型提前停→分段续作」——M-04 文档偏差登记：出口表实施期同步）；②D-01 修订：token 计数单轨化（usage 回执→钱包 ledger 唯一用途；装配门改物理防溢出粗校验，原 M-14 偏差登记失效）；③轮数计数归会话循环（max_rounds 触发续作出口——消费 CM-MEMORY 重对齐档位）；④续作会话=同 generation checkpoint 重建+分段进度摘要注入定向增量段（消费 CM-MEMORY RecoveryBrief 扩展；编排判定归 CM-RUNTIME 重对齐 D-06） |
