# DSH 预算与循环设计哲学总结

> 主题：DSH（DeepSeek Harness）在"预算档位 / 轮数限制 / 主从 agent 关系"上的设计哲学。
> 依据：`<dsh>` 安装包源码（`@deepseek-ai/dsh` 0.1.0-rc.7）各内置插件的 README 与类型定义。
> 归纳视角：**结构性的、确定性的、可组合的预算，取代按 token / 货币的计量式预算。**

***

## 0. 一句话总纲

> **DSH 不计量"钱 / token / 时间"，它只划"确定性的上限"（轮数、深度、字节、超时）。**
> 一个会话、一条消息能跑多久，由模型行为和策略层决定，而不是由内置看门狗决定。

***

## 1. 核心哲学：结构性预算 > token / 货币计量

源码里反复出现同一句话，构成 DSH 预算观的基石：

| 出处                      | 原文                                                                                                                                                                                                       |
| :---------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dsh-goal`              | **Round-count budget only** — `maxGoalRounds` does not meter tokens, currency, wall time, or provider quotas.                                                                                            |
| `dsh-goal-round-driver` | **Round cap, not resource budget** — token, currency, time, and provider quota policies remain independent.                                                                                              |
| `dsh-workflow`          | **No token-budget vocabulary** — engines cap concurrency, items, and children, but neither the request nor result accounts for model tokens across children.                                             |
| `dsh-tool-ralph`        | **Only round count bounds aggregate effort** — token, price, and elapsed-time budgets are deferred.                                                                                                      |
| `dsh-agent-loop`        | **No built-in turn budget** — tool calls or steering continue the current turn; a policy that bounds runaway turns must cancel from an existing lifecycle extension point such as `agent/turn-stopping`. |

### 解读

* **结构上限是确定、可组合、可持久化、重启安全的**：轮数、深度、字节数、超时毫秒数，都是可验证的整数，能随会话持久化，能跨重启存活。

* **token / 货币 / 配额是提供方侧的事**：DSH 刻意不替你做"每个 agent 花了多少 token / 多少钱"的账，也不据此自动停机。

* **"什么时候算完成 / 该不该停"属于模型和策略层**：DSH 只提供**扩展点**（如 `agent/turn-stopping`、`agent/request`、`agent/pre-step`），而不是内置一个"步数到了就掐断"的看门狗。

***

## 2. 推理档位（reasoning effort）——最接近"预算档位"的机制

### 2.1 档位集合（pi-ai 定义）

```ts
ThinkingLevel     = "minimal" | "low" | "medium" | "high" | "xhigh" | "max"
ModelThinkingLevel = "off" | ThinkingLevel
ThinkingBudgets   = { minimal?, low?, medium?, high? }   // 思考 token 预算
```

### 2.2 按模型的 `reasoningEfforts` 声明

* 每个键 = 一个档位，值 = 线上拼写（`high: high` 透传，`max: ultra` 给网关改名）。

* `off` 是三态键：不写 → 不提供 Off；写 `off:`（空值）→ 提供 Off 且不发送参数；写 `off: none` → 发送该值。

* 未声明的档位一律**不提供**；`false` = 无推理模型；空声明被拒。

* **手工声明且未写** **`reasoningEfforts`** **的模型 = 没有任何档位**（界面连选择器都不给）。

### 2.3 档位优先级与硬限制

```
请求级 GenerateOptions.reasoningEffort
  > 路由级 profile reasoning（部署默认）
  > 模型 defaultEffort
  > 提供方默认
```

* 请求点名不支持的档位 → **网络 I/O 之前** `UNSUPPORTED_REASONING_EFFORT` 失败，绝不自动降档。

* `xhigh` / `max` 在 token 预算型提供方（Anthropic/Bedrock）**钳制为** **`high`**。

* 默认思考 token 预算：`minimal=1024, low=2048, medium=8192, high=16384`。

* 输出预算会被思考预算挤压：`maxTokens = min(base + thinkingBudget, modelMaxTokens)`，且永远留 ≥1024 输出。

### 2.4 界面层的刻意取舍

* **档位是"按模型"的能力**，不是按 agent、更不是按 provider：同一 provider 下各模型互不认同。

* 因此 provider 卡片**故意不做档位控件**（`CustomProviderCard.d.ts` 注释原文）；只有模型选择器按模型列出该模型自己的档位。

***

## 3. Token 预算（每请求的硬上限）

| 字段              | 含义             | pi-ai 默认     |
| :-------------- | :------------- | :----------- |
| `contextWindow` | 输入（请求+响应）上下文上限 | 262144（未配置时） |
| `maxTokens`     | 单次输出上限         | 32768（未配置时）  |

* 对主 agent 和子 agent **一视同仁**；子 agent 默认继承父的同一套模型与上限。

* 输出顶满即以 `token limit` 终结，这是"单条消息"的一个自然停止条件。

***

## 4. 结构性预算一览

| 预算          | 机制                                  | 默认 / 上限                       |
| :---------- | :---------------------------------- | :---------------------------- |
| 委托递归深度      | `delegationDepth`（持久化）+ `maxDepth`  | 默认 `3`；`0` = 禁止委托             |
| Goal 自动续跑   | `max_goal_rounds`                   | 部署默认 `256`（可请求级覆盖）            |
| Ralph 循环    | `maxRounds`                         | code preset 默认 `64`（受部署天花板约束） |
| Workflow 编排 | `maxTotalAgents`                    | 部署天花板；另限并发/条目/子数              |
| 工具调用超时      | `timeoutMs`（仅声明了的工具）                | 如 web=30s、grep/glob=30s       |
| 流空闲超时       | `streamIdleTimeoutMs`               | 默认 300000ms（5 分钟无输出即 abort）   |
| 输出字节        | `dsh-spill-policy` / retention      | 超限溢出为预览+定位器                   |
| 指令字节        | `dsh-agent-instructions` `maxBytes` | 渲染严格不超限                       |

> 注意：**bash / read / write / edit 故意不声明超时**，只随 `exec.signal` 取消。

***

## 5. 主 agent 与子 agent 的关系设计

### 5.1 Agent 的"预算身份"只有三个字段

```ts
AgentOptions = { provider?, model?, maxTokens? }
```

**推理档位不在** **`AgentOptions`** **里**——它是按模型 + 按请求解析的，会话首请求时由适配器按模型默认冻结进 `LlmCallConfig`。

### 5.2 子 agent ≠ 父 agent

| 维度    | 父    | 子                                             |
| :---- | :--- | :-------------------------------------------- |
| 会话    | 自己的  | **全新独立会话**（run 永不改写父会话）                       |
| 上下文   | 完整历史 | **空白**（spawn 需独立 prompt；fork 继承父已完成回合）        |
| 模型路由  | 自己的  | 默认**继承**父，可被 `agentOptions` 覆盖                |
| 委托深度  | 顶层 0 | 父深度 + 1（`maxDepth` 默认 3）                      |
| 工具面   | 自己的  | 加入父的 preset 组合，可 `toolFilter` 收窄、可换 `persona` |
| 审批/沙箱 | 按父策略 | 审批钉死 `never`；沙箱取委托时父的显式覆盖                     |

### 5.3 子 agent 的档位/预算如何决定

* **默认继承**：`resolveChildAgentOptions` 复制父的 `provider/model/maxTokens` + 打上 `subagentDepth`。

* **档位不继承父"运行中手动选"的值**：子按自己模型路由的默认档位重新解析（父同模型 → 通常同默认档位）。

* **要差异化，四个通道**：

  1. `tool-subagent` 的 `agentOptions`（换 model/route → 默认档位与预算随之变）
  2. workflow 每阶段独立 `provider`/`model`
  3. `agent/request` 瀑布按会话/按请求改 `reasoningEffort`
  4. 委托深度（子专属的结构性递归预算）

### 5.4 为什么子 agent 默认继承

* **行为 / 成本一致性**：委托出去的工作应与父"长得一样"，避免意外漂移。

* **不对称是显式决定**："主贵子便宜 / 主深子浅"都可配，但那是配置覆盖，不是默认。

***

## 6. 为什么没有内置轮数 / 步数上限

### 6.1 事实

* **正常对话**：会话轮数**无上限**；发一条消息，内部"调工具 → 结果 → 再调"的迭代次数**也无上限**（源码中不存在 `maxSteps` / `maxIterations` / `maxTurns` 这类常量）。

* 有上限的只有第 4 节的**机制性轮数**（goal / ralph / workflow / 深度）。

### 6.2 一条消息的内部循环何时停

| 停止条件                                  | 性质             |
| :------------------------------------ | :------------- |
| 模型给出最终答案（不再调工具）                       | 正常结束           |
| 出错 / 拒绝                               | 终止             |
| 输出顶满 `maxTokens`                      | `token limit`  |
| 提供方配额 / 限流                            | 终止             |
| 声明了 `timeoutMs` 的工具超时                 | `TOOL_TIMEOUT` |
| 流空闲超时（5 分钟无输出）                        | abort          |
| 用户取消 / `job_kill` / `interrupt_agent` | 手动             |

### 6.3 设计权衡（为什么不做内置看门狗）

* **单一 agent 的循环往往语义上要求"继续到完"**：编码、多步检索、迭代修正，都是同一回合内的多步工作。内置步数上限会误伤这些合理工作流。

* **"完成"无法由框架判定**：只有模型（给出最终答案）和策略层（`agent/turn-stopping` 等扩展点）能判断。

* **把控制权交给策略层**：要"N 步强制停"，应写一个 `agent/turn-stopping` 或 `agent/pre-step` 监听器按步数/时长取消——这正是 DSH 预留的扩展点。

* **结构性预算负责"防爆炸"**：深度封顶防递归套娃，工具/流超时防单次挂死，字节/上下文预算防内存与上下文失控；轮数则留给"目标机制"（goal/ralph/workflow）各自封顶。

***

## 7. 对使用者的结论

1. 你正常发一条消息 → 内部迭代次数**没有上限**；它停是因为模型停 / 出错 / token 用完 / 超时 / 你打断，而不是因为"轮数到了"。
2. "轮数的限定"只存在于 **Goal（默认 256）/ Ralph（64）/ Workflow / 委托深度（3）** 这几类自动多轮机制。
3. 想限制单条消息的迭代：写一个基于 `agent/turn-stopping` / `agent/pre-step` 的计数取消策略（DSH 未内置）。
4. 想给子 agent 不同档位/预算：用 `tool-subagent.agentOptions`、workflow 阶段覆盖、或 `agent/request` 瀑布。
5. 手工声明模型的档位需在 `settings.yaml` 声明 `reasoningEfforts`（必要时配 `compat.thinkingFormat`），否则界面不提供档位选择。

