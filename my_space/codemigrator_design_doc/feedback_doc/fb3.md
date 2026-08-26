全部 15 篇 + 迭代记录完整通读完毕。这是 V3——不是 V2 的修补，是实质性架构重构。以下是我的客观评价。

---

## V3 做了什么

### 核心变更：精准切除分布式机制

D-029 记录了这次重构的触发点和边界。V3 不是盲目删除所有协调机制，而是区分了"泛化过度的机制"和"解决真实竞态的窄机制"：

| V2 机制 | V3 处置 | 理由 |
|---|---|---|
| 30s lease + 20s 续期 | **删除** | 单 app + 内存 actor，不存在跨进程租约竞争 |
| 全链路 fencing_token | **删除** | 不跨进程，进程信号 + advisory lock 够用 |
| 每步 expected-state/version CAS | **删除** | Run actor 串行处理，不存在并发写同一状态 |
| Outbox relay | **删除** | `run_events` + LISTEN/NOTIFY 替代 |
| 轮询式 reconciler | **删除** | 事实驱动恢复（启动/断连/intent 缺口触发） |
| Run 级 work ref | **删除** | 改为每 Slice 独立 candidate ref |
| Git expected-OID ref transaction | **保留** | 窄机制：只保护 Git ref 推进的原子性 |
| API `If-Match` | **保留** | 窄机制：只保护外部 cancel 的版本匹配 |
| DB 幂等唯一约束 | **保留** | 窄机制：防止重复创建 |
| advisory lock | **新增** | 单 app 控制面写者证明 |

**这个区分是正确的。** V3 没有走到"全删"的极端，也没有保留 V2 的过度设计。

### 新增架构：Slice 全链路并行

V2 只有 Run 级 `work/verified` 双 ref，不支持真正的 Slice 并行。V3 引入了完整的并行候选架构：

- **每 Slice 独立 candidate ref**：`refs/codemigrator/runs/<run_id>/slices/<slice_id>/candidates/<generation>`
- **每 Slice 独立 worktree**：物理隔离不同 Slice 的文件写入
- **WriteScope 冻结**：计划阶段冻结每个 Slice 可写文件集合，相交时生成确定性顺序边
- **Integration Coordinator**：按冻结顺序（拓扑层→规则优先级→SliceId）串行集成
- **三层验证**：LocalCandidate → ProspectiveIntegration → FinalVerified

**这是 V3 最有价值的新增。** 它回答了之前讨论中的核心问题——"如何在不加 CAS 的前提下安全并行"。答案是用 Git ref 物理隔离 + 冻结写集合 + 确定性集成顺序，而不是用分布式锁。

### D-029 对我上次分析的回应

迭代记录 D-029 直接引用了之前的反馈，并用 Claude Code 源码核验结果**接受了部分判断、驳回了四条前提**：

| 反馈前提 | D-029 的驳回 | 源码证据 |
|---|---|---|
| "Claude Code worktree 隔离所有 agent" | worktree 是可选的（`effectiveIsolation === 'worktree'`），不是所有 agent 的物理事实 | `AgentTool.tsx` 源码 |
| "Node.js 单线程 = 无竞态" | 跨 `await` 的读-改-写不是原子的；`proper-lockfile` 和 `isConcurrencySafe` 证明产品自己也没把单线程等价于无竞态 | 源码搜索 |
| "AbortController 足以撤销全部副作用" | `AbortController` 是协作取消，不撤销已提交的副作用；`killShellTasksForAgent` 显式清理孤儿 shell | `runAgent.ts` 源码 |
| "V2 已有每 Slice 独立 work ref" | V2 只有 Run 级 `work/verified`，没有 per-Slice ref | V2 正文 |

**这个驳回是诚实的且有证据支撑的。** 我上次的分析确实在这四点上过于简化了——把 Claude Code 的"可选 worktree"当成了"所有 agent 物理隔离"，把 Node.js 事件循环当成了"无竞态保证"。

### PostgreSQL 保留的论证

M-02 新增了专门一节"为什么不提供 filesystem 控制面 backend"：

> JSONL 与 Git refs 足以支持单用户、前台运行且不要求断线回放的 CLI，这个对照说明 PostgreSQL 不是迁移算法本身的依赖。但当前产品是可重启 REST/SSE 后台服务：它同时需要 API 幂等、Run/Slice 查询投影、严格事件序列、取消版本检查、active dispatch 接管以及 integration intent/receipt 的原子事实。

**这个论证是成立的。** 如果 CodeMigrator 是 CLI 工具，确实不需要 PostgreSQL。但 V3 的产品定位是"可重启 REST/SSE 后台服务"——这意味着需要 API 幂等、断线回放、查询投影，这些用纯文件系统实现确实需要自研文件锁/原子追加/损坏截断/索引/事件序列。PostgreSQL 作为控制面后端是合理选择。

---

## 客观评价

| 维度 | V2 → V3 改进 | 仍存在的疑虑 |
| :--- | :--- | :--- |
| **协调机制** | 精准删除 5 项泛化机制<br>保留 4 项窄机制<br>新增 advisory lock 替代多 app 选主 | — |
| **Slice 并行** | 每 Slice 独立 ref/worktree/Artifact<br>WriteScope 冻结 + 确定性冲突边<br>Integration Coordinator 冻结顺序消费 | Integration Coordinator 是串行瓶颈<br>个人项目 5~10 Slice 足够，但规模扩大需重新评估 |
| **三层验证** | LocalCandidate / ProspectiveIntegration / FinalVerified<br>fingerprint 分离语义与证据<br>NONDETERMINISTIC_VERIFICATION 检测测试不稳定 | 三次全量 Spec checks 成本较高<br>FinalVerified 层可能对个人项目偏重 |
| **CandidateGeneration** | 0..=2 上限明确<br>LOCAL_REWORK 在 generation 内受控修复<br>物理重派不消耗 generation | generation + LOCAL_REWORK + 冻结任务集合的交互复杂<br>实现时需仔细处理状态转换 |
| **DispatchAttempt** | 物理派发 vs 语义 generation 清晰分离<br>active-attempt gate 精确<br>迟到结果安全丢弃 | — |
| **PostgreSQL** | 有专门论证为什么不选 filesystem<br>SSE 改用 run_events + LISTEN/NOTIFY<br>Outbox 删除 | 产品定位"REST/SSE 后台服务"本身是否必要？<br>CLI + SQLite 对个人项目可能更务实 |
| **恢复模型** | 事件驱动（启动/断连/intent 缺口）<br>不再轮询<br>intent → Git CAS → receipt 三步可恢复 | — |
| **文档质量** | 公共契约唯一 owner 清晰<br>每篇有贯穿场景<br>验收条款具体可测 | 277 条 Prometheus series 计算等<br>对个人项目过度精确 |
| **D-029 诚实度** | 明确接受"机制泛化过度"判断<br>用源码证据驳回 4 条不准确前提<br>区分"接受反馈方向"和"接受反馈全部结论" | — |


## 总结评价

### V3 是一次高质量的架构重构

三个层面都有实质改进：

**1. 认知层面——D-029 诚实区分了"过度泛化"和"必要协调"**

V3 没有走极端。它接受了"V2 的分布式机制对单机项目过重"这个判断，但没有接受"全部删掉用文件系统"的结论。而是逐项审查：lease 防的是什么（跨进程租约竞争 → 单 app 不存在）、fencing_token 防的是什么（旧 worker 在远端继续执行 → 本机进程信号够用）、CAS 防的是什么（并发写同一状态 → 内存 actor 串行处理），然后精准删除。

**2. 架构层面——Slice 全链路并行是真正的创新**

V2 的 per-Slice 隔离只是声明（"未验证兄弟 Slice 永远不进入当前 verification workspace"），但没有物理载体。V3 给了物理载体：独立 candidate ref、独立 worktree、独立 Artifact namespace、独立模型上下文。再加上 WriteScope 冻结和确定性集成顺序，并行候选可以安全推进，完成速度不改变 verified commit 序列。这是用"隔离 + 排序"替代"锁"的正确做法。

**3. 工程层面——三层验证 + fingerprint 分离是有价值的设计**

`verification_fingerprint` 只覆盖语义结果（tested commit + check set + status/diagnostics），`evidence_identity_hash` 覆盖完整证据（receipt + logs + ArtifactRef）。最终验证与 prospective 验证的 fingerprint 比较，能发现测试不稳定和环境漂移——这不是过度设计，而是代码迁移场景的真实需求（迁移后的代码可能在相同 commit 上因环境差异产生不同构建结果）。

### 仍值得思考的问题

**PostgreSQL 的产品定位问题**——D-029 的论证是"当前产品是可重启 REST/SSE 后台服务"。但回溯到 U-005 的原始需求，用户说的是"通用代码迁移 Agent"，没有明确要求 REST/SSE 后台服务形态。REST/SSE 是 V2/V3 自己选的部署形态，然后用这个形态来论证 PostgreSQL 的必要性。

这个问题不影响 V3 文档的内部一致性——在"REST/SSE 后台服务"这个前提下，PostgreSQL 确实比 filesystem 合适。但如果后续回到"CLI 工具"定位，SQLite 仍然足够。这不是 V3 的缺陷，而是产品定位需要你自己确认的问题。

**复杂度与秋招目标的匹配**——V3 的三层验证、generation 0..=2 + LOCAL_REWORK、DispatchAttempt gate、IntegrationIntent 恢复链路、两套协议（plugin wire v2 + worker protocol v1），这些设计在技术深度上无可挑剔。但对一个秋招项目来说，实现复杂度很高。建议在详细编码设计阶段，明确标注哪些是 MVP 必需、哪些是二期增强。

---

**一句话**：V3 是一次正确的架构进化——它证明了"听取反馈"不等于"全盘接受"，而是在源码证据的基础上做精准判断。文档质量已经达到了可以作为施工基线的水平。

要不要我帮你把 V3 同步到 workspace 的 `codemigrator_design_doc/` 目录，覆盖旧版？