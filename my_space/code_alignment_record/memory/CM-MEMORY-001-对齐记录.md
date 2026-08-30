# CM-MEMORY-001 对齐记录

> 用途：本文件是任务 `CM-MEMORY-001`（模块 M-14 记忆与上下文管理·统一基建）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段     | 内容                                                                                     |
| ------ | -------------------------------------------------------------------------------------- |
| 任务ID   | `CM-MEMORY-001`                                                                        |
| 模块编号   | M-14（V6 方向对齐版 fb11 收敛·全类型会话统一上下文基建）                                                    |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_记忆与上下文管理.md` |
| 主任务表   | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3                   |
| 对齐轮次   | 第 2 轮（Wave 2+3 全线对齐轮）                                                                  |
| 对齐日期   | 2026-08-29                                                                             |
| 对齐参与者  | 用户 + TRAE agent                                                                        |
| 对齐状态   | 已对齐                                                                                    |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/` 上下文基建（M-01：app 内横切；装配器落 runtime 组合根，契约与纯逻辑可入 core）——全类型会话统一上下文管理：

* **Context Pack identity 与冻结**：五元组（run/phase/session/slice 引用/四冻结哈希）任一变化即新 identity、旧 pack 失效归档；失效信号含 PlanRevision/Skill catalog hash 变化。

* **SessionKind 九值**：M-14 八值 + **补** **`RepairSession`**（D-01 文档缝隙机械补全——V6 修复会话在 M-12 授权行/M-14 装配节均有但枚举未列）。

* **SessionBudgetProfile 预算档 v1（2026-08-29 重对齐：纯结构性轮数预算，推翻首轮 token 计量式）**：字段重构为 `{session, max_rounds, eviction_watermark_pct}`（作废 initial\_pack\_token\_cap/session\_token\_cap）；轮=一次模型调用（消费累积上下文+发出工具请求或文本）；上限取**防爆炸级宽限**（DSH 哲学：编码复杂度不可预测，计量式 token cap 必误伤——结构性上限确定、可持久化、重启安全）：

| SessionKind               | max\_rounds（轮） | 逐出水位 |
| ------------------------- | -------------: | ---: |
| AnalyzeAuxiliary          |             30 |  80% |
| PlanAuxiliary             |             50 |  80% |
| Contract                  |            300 |  75% |
| Implementation            |            500 |  75% |
| TestTranslation           |            300 |  75% |
| TestGeneration            |            300 |  75% |
| ExploreCoordinator        |            200 |  80% |
| ExecuteSupervisor         |             30 |  80% |
| RepairSession             |            500 |  75% |
| Drafting（CreateRun 前特殊阶段） |            200 |  80% |

> 口径：轮数为防爆炸兜底而非精确控制（500 轮对标复杂实现 Slice 工具循环；Supervisor 单次决策 30 轮富余）；可配常数、版本化配置资源（core://session-budget/v1）、随 Run 创建冻结。**上下文窗口是物理边界不是预算**——净输入上限公式（context\_window−reserved\_output−tool\_schema\_tokens−envelope\_margin）与逐出治理保持（超限 CONTEXT\_BUDGET\_EXCEEDED 不可截断语义不变）；initial\_pack 装配受物理上限约束。token 精确计数仅保留一个用途：Run 级钱包断路器 ledger（见 CM-RUNTIME 重对齐）。Run 级 input/output/cost 三项为用户钱包兜底层（M-00 契约，定位收窄——不承担会话控制）。
>
> **分段续作配套（预算耗尽≠死亡）**：轮数上限是**分段点不是终止判决**——耗尽时经 checkpoint 保存进度后由 actor 判定续作资格（CM-RUNTIME 重对齐 D-06）；RecoveryBrief 扩展**分段进度摘要**段（已完成内容清单+剩余任务提示，从 checkpoint diff 与账本确定性派生，discarded\_turns=0 区分于崩溃重建）——续作会话的定向增量段注入。续作次数上限 3 次/generation（可配，与 generation 计数、修复重试计数三者独立）。

* **三段式 KV-cache 前缀装配**：稳定前缀段（角色系统提示+冻结工件引用+Run 级固定事实·Run 生命周期字节绝对稳定）/演进段（verified 演进摘要·每集成一 Slice 段尾追加自身条目·历史条目零变动）/定向增量段（本次失败证据/定向投影·每次不同）；统一装配器承载不分会话类型；逐出只作用于定向增量段内旧轮次工具结果，稳定前缀段+演进段入不可逐出集合。

* **演进段持久化**（D-02，§9.1 开放项收口）：append-only 条目表（PostgreSQL：entry\_index/slice\_id/摘要文本，写入后不可变）+ 渲染模板版本随 Run 冻结（模板不变+条目不变→重放/重建字节一致）；prefix cache provider 适配（cache\_control/自动前缀）归 provider adapter（CM-LOOP 对齐）；命中率度量归 M-13 观测。

* **事件触发式协调会话装配**：Supervisor 触发时定向装配——基线态势快照（Harness 机器确定性计算·本身不入上下文·派生态势来源）+本次决策定向事件投影（目标事件非全量流）；可回溯 run\_events；无持久观察状态。

* **修复会话导航索引式装配**：修复简报=必要输入不可静默截断；其余以导航索引（涉及文件+位置清单）给出、Agent 按需 ReadFile；超限外置 cas\://；重生升级包注入修复简报（前代终态诊断+定向修复事实）。

* **运行期数据块边界**：七类数据进上下文统一边界（源码 256KiB 分段/导航 200 条/Shell 摘要头尾双窗+CAS 外置/Exec 汇总回执/完整日志不进上下文/ToolError facts/契约引用）；ArtifactRef 外置原则+cas\:// 受控取回。

* **预算治理**：净输入上限四量公式（context\_window-reserved\_output-tool\_schema\_tokens-envelope\_margin，adapter 精确计算）；不可截断集合（系统提示/任务简报/契约块/约定块/理解档案摘录/诊断摘要）；80% 恰一次告警；100% 无例外；`CONTEXT_BUDGET_EXCEEDED`/`CONTEXT_CAPABILITY_INVALID`。

* **逐出与摘要**：不可逐出集合（+当前编辑目标最近读取）；水位触发自最旧结构化摘要替换（路径/行范围/结论行）；逐出决策入会话审计；显式重读。

* **会话重建**：材料三源（最近 checkpoint/run\_events 回放/dispatch 冻结 pack 来源事实）；RecoveryBrief（CheckpointSummary/CheckFeedbackSummary/discarded\_turns）确定性派生零叙述；对话历史不回放；重建复用 dispatch 冻结契约集合（不取最新 verified）。

* **重生成历史注入**：前代失败诊断摘要（不可截断）+前代 checkpoint diff 摘要（超限降级 ArtifactRef）恰两段；计入初始装配预算精确计量。

* **规则手册装配**：相关章节随 pack、消费版本冻结锁定、不热更新。

* **归一器唯一边界**：模型输出经归一器收敛契约形状；provider json mode=可选加速非正确性依赖；归一失败不静默降级（根因走 run\_events）。

* **无自由记忆**：主 Agent 记忆=审计事实结构化投影（run\_events 可重建）；零跨 Run 学习（规则手册受控追加通道除外）；pack/审计不含上会话对话史（RecoveryBrief 摘要除外）。

### 1.2 边界（不做什么）

* 不做会话内容构成定义（什么内容进哪类会话——M-04 owner，CM-LOOP 对齐；本任务叠加预算档与重建策略）。

* 不做工具调用规范与输出上限（M-12/M-06 owner——CM-WORKSPACE 已对齐；本任务定义进入上下文后的治理）。

* 不做 provider adapter 实现（tokenizer/context window/模型客户端/归一器实现——归 CM-LOOP-001 对齐收口，D-03；本任务经端口消费计数）。

* 不做修复简报具体 schema（前代诊断/导航索引字段定义——归 CM-REPAIR-001 对齐收口，D-04；本任务定消费契约：必要输入不可截断+导航索引式+超限外置）。

* 不做模板正文内容（各会话类型提示词文本——机制已定 CM-WORKSPACE D-02，正文随各会话任务实施期填充）。

* 不做 checkpoint/工作区恢复窗口（M-08 owner——已对齐；本任务提供重建材料与 RecoveryBrief）。

* 不做 run\_events schema（M-02 owner——已对齐；只消费审计事实）。

* 不做脱敏（M-13 owner——已对齐；进入 provider 的源码正文不加工）。

* 不做预算 100% 收敛编排（M-03 owner——Harness checkpoint+归档；本任务提供计量事实）。

### 1.3 产出物

core 侧：SessionKind（含 RepairSession）/ContextPackIdentity/SessionBudgetProfile/预算档 v1 配置资源（core://session-budget/v1 JSON 版本化）；runtime 侧：统一装配器（三段式前缀/模板加载/规则手册与摘录装配）、演进段 append-only 表（migration DDL）+冻结渲染模板、数据块边界治理（头尾双窗摘要/CAS 外置）、逐出引擎、RecoveryBrief 生成、重生成历史注入、归一器端口契约；tests/memory/（预算档冻结/三段式字节稳定/逐出边界/重建零叙述/不可截断用例）；模块迭代记录（dev\_progress/memory/）。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| #    | 决策点                           | 可选项                           | 用户确认结论                                                                                                                                                                                                 | 依据                                         |
| ---- | ----------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| D-01 | 预算档 v1 数值（首轮 token 计量式）       | 确认数值表 / 押后 / 调整               | **（2026-08-29 重对齐修订·用户确认）纯结构性轮数预算**：首轮按 256k/1M 窗口设计的 token 计量表（初始装配/会话累计 token cap）经用户质疑推翻——编码复杂度不可预测，计量式预算必误伤（DSH 哲学：结构性上限取代计量）；重构为 max\_rounds 轮数表（见 §1.1）+分段续作配套；SessionKind 补 `RepairSession` 值不变 | 对齐问答 Q-01/Q-04/Q-05 + 重对齐 Q-06（2026-08-29） |
| D-02 | 演进段字节稳定性持久化保证（§9.1 开放项）       | append-only 表+冻结模板 / 重渲染 / 押后 | **append-only 条目表（PostgreSQL）+ 渲染模板版本随 Run 冻结**：条目写入后不可变+模板不变→重放字节一致；prefix cache 适配归 provider adapter（CM-LOOP）；命中率度量归 M-13                                                                            | 对齐问答 Q-02                                  |
| D-03 | provider adapter 归属           | 归 CM-LOOP / 本任务定              | **归 CM-LOOP-001**（M-04 会话循环 owner：模型客户端+tokenizer+context window 探测+归一器实现）；本任务经端口消费计数与净输入上限                                                                                                            | 对齐问答 Q-03                                  |
| D-04 | 修复简报/态势快照 schema 归属（§9.1 开放项） | 归 CM-REPAIR / 本任务定            | **schema 归 CM-REPAIR-001**（M-04/M-16 owner 侧）；本任务定消费契约：必要输入不可静默截断+导航索引式装配+超限外置 cas\://                                                                                                                 | 对齐问答 Q-03（第二问）                             |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

* core 公共契约（RunId/Phase/CandidateGeneration/Sha256/ArtifactRef）；M-04 会话构成定义（CM-LOOP 对齐后回填模板清单——CM-WORKSPACE D-02 联动）；M-12 工具输出（数据块边界输入）；M-08 checkpoint/receipt（重建材料）；run\_events 审计（M-02）；模板库资源（CM-WORKSPACE D-02 core://session-templates/v1）。

### 3.2 下游消费

* CM-LOOP/M-04（会话循环消费装配器/预算档/逐出/数据块治理）；CM-RUNTIME/M-03（Supervisor 触发装配/预算计量事实）；CM-SUPERVISOR/CM-REPAIR（定向装配/修复简报消费契约/导航索引）；CM-DRAFT（起草会话工具结果治理——数据块边界与预算规则约束其上下文方式）；CM-VERIFY/M-10（检查日志外置消费）。

### 3.3 跨模块接口边界

* **adapter 端口**：本任务定义 `TokenCounter`/`NetInputCap` 计算端口与净输入上限公式语义；实现归 CM-LOOP provider adapter——token 计数接口是跨模块契约（实现侧零近似计数）。

* **演进段 DDL**：append-only 表归本任务 migration（CM-SPEC DDL 先例惯例）；渲染模板版本入 ContextPackIdentity 冻结哈希族（M-14 identity 扩展登记——实施期同步 M-14 identity 字段注释）。

* **SessionKind 扩容联动**：`RepairSession` 值补入 M-14 枚举（实施期文档同步候选——机械补全非语义变更）；CM-LOOP 对齐时消费九值定义模板类型。

* **修复简报契约**：`RepairDecision.brief_refs`（M-00）指向的 ArtifactRef 内容 schema 归 CM-REPAIR；本任务保证"必要输入不可静默截断+导航索引式装配"。

* **起草会话**：CreateRun 前阶段不入 Context Pack identity 体系（M-14 明文）——本任务数据块边界与预算治理规则仍约束其工具结果。

* 重生成历史注入两段与 M-04/M-16（V-M04-V4-019 联动）——注入内容从审计事实派生，CM-RUNTIME/CM-LOOP 消费。

## 4. 验收条款映射

| 条款                    | 内容摘要                                                     | 验证方式                      |
| --------------------- | -------------------------------------------------------- | ------------------------- |
| V-M14-V4-001\~003（追溯） | 初始 pack 零源码正文；档随 Run 冻结；精确 tokenizer 零近似                 | 装配单测（adapter stub 精确计数断言） |
| V-M14-V4-004          | 不可截断集合零逐出零截断；超限 CONTEXT\_BUDGET\_EXCEEDED                | 装配单测                      |
| V-M14-V4-005/006      | 数据块边界（256KiB/200 条/双窗摘要）；日志正文零进上下文；Exec 逐笔回执只入审计         | 边界治理单测                    |
| V-M14-V4-007          | 逐出只作用非必要结果+摘要替换+决策入审计+可重读                                | 逐出引擎单测                    |
| V-M14-V4-008          | 80% 恰一次告警；100% 零新调用                                      | 预算单测                      |
| V-M14-V4-009\~011     | 重建材料三源；对话历史零回放；RecoveryBrief 零叙述可回溯；重建复用 dispatch 冻结契约集合 | 重建单测                      |
| V-M14-V4-012/013      | pack 缓存 key 全字段；跨 Run 零复用；零跨会话记忆写入                       | 缓存单测                      |
| V-M14-V4-014/015      | V3 残留零扫描；重生成注入恰两段+精确计量+diff 降级                           | 静态扫描+注入单测                 |
| V6 增量（M-14 六条）        | 事件触发协调会话/三段式前缀/逐出交叉约束/模板库/修复导航装配/重生简报/零自由记忆              | 装配器单测（三段字节稳定+定向投影用例）      |
| D-01 预算档              | v1 数值表随 Run 冻结；运行期档位变更 0；九值枚举含 RepairSession             | 配置资源契约测试                  |
| D-02 演进段              | append-only 条目不可变；重放渲染字节一致；模板版本冻结                        | 演进段单测（追加+重放比对）            |

## 5. 风险与注意点

* **预算数值与真实模型校准**：v1 表按 256k 窗口设计、1M 头富余——真实 provider（model\_api\_key.json 模型）接入后按实测微调（版本化配置资源升级走版本递增，Run 冻结不受影响）。

* **演进段 identity 扩展**：渲染模板版本须入 ContextPackIdentity 冻结哈希（M-14 identity 六哈希字段未列模板版本——实施期 M-14 同步候选，机械补全）。

* **adapter 端口时序**：CM-LOOP 对齐晚于本任务——token 计数端口先行 stub（精确计数语义用测试锁定）；LOOP 对齐后替换实现（并行纪律 2）。

* **头尾双窗摘要实现**：头部窗保 mypy 首错/栈顶帧类信号（M-14 明文语义）；摘要内容确定性（同输入同摘要——审计可复算）。

* **CAS 外置与 cas\:// 取回联动**：外置写经 CM-OBS CAS 账本；取回经 CM-WORKSPACE 网关（M-12 ReadFile 第二形态）——三方契约联调证据归后完成方。

* **演进段表与 run\_events 同事务**：Slice 集成事件与演进段条目同事务追加（避免事件/演进段漂移）——与 CM-RUNTIME 集成事务对齐。

* 不可截断集合与不可逐出集合是两个概念（前者=必要输入防截断；后者=三段式前两段防逐出）——实现勿混用。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| #    | 问题                                                                | 用户结论                                                                                      |
| ---- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Q-01 | 预算档 v1 数值（首轮）                                                     | （先追问：预算档位调整什么、是 token 吗）经澄清三字段语义（初始装配/会话累计/逐出水位）后继续                                       |
| Q-02 | 演进段字节稳定性持久化保证                                                     | append-only 表+冻结渲染模板；prefix cache 适配归 adapter（CM-LOOP）                                    |
| Q-03 | provider adapter 归属 / 修复简报 schema 归属                              | adapter 归 CM-LOOP-001；修复简报 schema 归 CM-REPAIR-001（本任务定消费契约）                               |
| Q-04 | （用户追问）预算档位语义                                                      | 澄清：三字段=初始装配 token 上限/会话累计 token 上限/逐出水位百分比；Run 级三项总预算为另一层                                 |
| Q-05 | 预算档 v1 数值（重算确认）                                                   | （告知模型上下文为 256k/1M）按 256k 基线重算 v1 表写入记录；数值可配可版本化升级                                         |
| Q-06 | （重对齐·用户发起）预算形态重新裁决：token 计量不可行（编码复杂度不可预测）+Slice 未完成 agent 停下的续作机制 | 纯结构性轮数预算（推翻 token 表）+Run 钱包断路器定位+分段续作机制+变更行修订（四项推荐全确认，参照 temp\_doc/dsh\_design.md DSH 哲学） |

## 7. 变更记录

| 日期         | 变更                      | 说明                                                                                                                                                                                                                                                                                                |
| ---------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-08-29 | 建立记录                    | 依据 M-14 V6 方向对齐版设计文档与主任务表 §7.3；用户经提问工具逐项确认 D-01\~D-04（含预算档语义澄清与 256k/1M 上下文重算）；§9.1「演进段字节稳定性」经 D-02 收口（prefix cache 适配部分归 CM-LOOP）、「修复简报 schema」归属经 D-04 登记（CM-REPAIR 收口）                                                                                                                         |
| 2026-08-29 | 预算体系重对齐（用户发起·参照 DSH 哲学） | ①D-01 修订：token 计量式预算档推翻，重构为纯结构性轮数预算（max\_rounds 十档表·§1.1）——编码复杂度不可预测，计量式预算必误伤；②上下文窗口重定位为物理边界（净输入公式+逐出不变，非预算）；③token 计数仅保留 Run 钱包 ledger 用途；④新增分段续作配套：RecoveryBrief 扩展分段进度摘要段（discarded\_turns=0 区分崩溃重建）、续作上限 3 次/generation；⑤M-14 文档偏差登记（实施期同步：预算治理节定位+SessionBudgetProfile 字段重构——用户经重对齐 Q-06 确认） |

