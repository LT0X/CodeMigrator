# CM-DRAFT-001 对齐记录

> 用途：本文件是任务 `CM-DRAFT-001`（模块 M-16 起草 + M-04 起草节 + M-14 相关节）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-DRAFT-001` |
| 模块编号 | M-16（起草部分，owner：会话流程/草稿数据模型/确认语义）+ M-04 起草节（会话类型/工具面/循环）+ M-14（上下文预算治理） |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_会话与运行时修正编排.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付起草期多 Agent 理解与四件工件确认编排（落点：runtime 编排 + 复用会话框架，D-05）：

- **起草会话流程（V6 重排）**：用户自然语言输入 → 探索扇出（机器骨架切域 + 探索协调者切域调整建议 + Harness 机器校验后派发）→ 探索员只读探索提交带 file:range 锚点/覆盖率/置信度理由报告 → 主会话（探索协调者）归并（含冲突/风险热点/语义模块划分建议）→ **多轮 AskUser 对齐**（逐项对齐语义模块划分、依赖判定分歧、风险处置、测试策略、蓝图原则；全周期不限次）→ 对齐结论固化为四件工件草稿 → **试译-弃稿校准**（D-04）→ 用户显式确认冻结。
- **四件工件**：MigrationSpec（经 M-05 canonical）、UnderstandingDossier、TargetProjectBlueprint、MigrationRulebook——一次确认分别记录版本与 hash，一并冻结为 Run 输入（FrozenArtifactBundle）。
- **切域骨架算法**（D-02）：module_boundary_strategy 机械模块候选为初始域；单域超 20 文件（可配常数）按目录二级拆分；探索扇出上限默认 6（可配常数）；覆盖恰好一次由 Harness 校验。
- **focus brief 与切域建议 schema**（D-03）：FocusBrief{域路径集、重点标注（风险热点/大文件/import 重量）、预算提示}；ExploreReassignment Advice payload{op: merge|split|refocus、涉及域、理由摘要、focus brief 更新}。
- **档案一致性断言核对集**（D-01，§9.1 开放项收口）：供 CreateRun 消费的机械核对纯函数——①DossierEntry 锚点 file:range 全部可解析且非 advisory 空锚点违规 ②semantic_modules 成员 ⊆ Spec 范围∩F1 文件清单 ③归并报告未解决冲突标记数=0；失败即 `DOSSIER_INCONSISTENT` 零副作用（核对函数归本任务，断言执行归 CM-RUNTIME CreateRun）。
- **TaskDraftRevision 双轨版本**（D-06）：工件草稿内容变更→新 revision（内容版本）；AskUser 对齐结论→Question/Answer 会话账本（append-only 不膨胀 revision）；确认门冻结「当前 revision + 关联问答指针」。
- **试译校准**（D-04）：协调者按归并报告风险热点选 2-3 代表性文件，同一文件按 Rulebook 约束与自由发挥各生成一版（Code 档），会话内并排呈现差异；结论修订四件工件；产物仅会话呈现、零落盘、零候选工作区触碰、零 Run 副作用。
- **起草会话边界**：工具面=ReadFile/QuerySourceAst/Exec（仅编排只读工具）+ AskUser（非模型工具）；WriteFile/EditFile/Shell 零接纳；Agent 只起草不提交（确认权在用户）；草稿不是第二种 Spec 输入（描述符锁由系统从资源账本解析写入）。

### 1.2 边界（不做什么）

- 不实现会话 Agent 循环机制/调用循环本体（M-04 owner，归 CM-LOOP-001——本任务复用其会话框架）。
- 不实现统一上下文装配/预算档数值（M-14 owner，归 CM-MEMORY-001——本任务消费其契约）。
- 不实现 Spec 四道门/canonical 化（M-05 owner，CM-SPEC-001 已对齐——Spec 草稿确认后走其 canonical 流程）。
- 不实现 CreateRun 预检编排与 DOSSIER_INCONSISTENT 拒绝流程（归 CM-RUNTIME；本任务提供核对纯函数）。
- 不实现知识图谱构建/PSF 投影（M-06 owner，CM-ANALYSIS-001 已对齐——探索消费其只读投影）。
- 不实现会话 REST 路由/SSE 投影（M-02 owner，CM-API-001 已对齐）。
- 不含运行期修正/PlanRevision/契约漂移协议（M-16 运行期部分，归 CM-RUNTIME/CM-PLAN 消费侧；起草会话只改变任务构造方式不改变冻结修正规则）。
- 不定义 Skill catalog 内容清单（首批内置 Skill 具名清单为实施期待办，M-16 明示）。

### 1.3 产出物

runtime：起草编排（探索扇出派发/协调者归并/AskUser 调度/确认门）+ TaskDraftRevision 账本模型与存储；core 侧：FocusBrief/切域建议 payload schema 建议（经 CM-CORE Advice.payload 占位对齐）；档案一致性核对纯函数；tests/draft/（核对集用例/切域骨架确定性/版本粒度/试译零副作用）；模块迭代记录（dev_progress/session/）。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | 档案一致性断言核对集（§9.1 开放项；fb11 已定一条断言无分档） | 最小机械核对集 / 加覆盖自述比对 / 押后 | **最小机械核对集**：①锚点全可解析且非 advisory 空锚点违规 ②semantic_modules 成员 ⊆ Spec 范围∩F1 清单 ③未解决冲突标记=0；零模型纯机械 fail-fast | 对齐问答 Q-01（2026-08-29） |
| D-02 | Harness 确定性切域骨架算法 | 语义模块域+二次拆分 / 顶层目录均分 / 押后 | **语义模块域+二次拆分**：机械模块候选为初始域；>20 文件（可配）按目录二级拆分；扇出上限默认 6（可配） | 对齐问答 Q-02 |
| D-03 | focus brief 与切域建议 payload schema（M-16 实施期开放项） | 冻结最小 schema / 押后 | **冻结最小 schema**：FocusBrief{域路径集/重点标注/预算提示}；ExploreReassignment payload{op: merge\|split\|refocus/涉及域/理由摘要/focus brief 更新}——CM-CORE Advice.payload 与 api 事件投影同步落地 | 对齐问答 Q-03 |
| D-04 | 试译-弃稿校准执行形态 | 热点双版本对比 / 押后 | **热点双版本对比**：协调者选 2-3 风险热点文件，Rulebook 约束版 vs 自由发挥版（Code 档）会话内并排呈现；结论修订工件；零落盘零副作用 | 对齐问答 Q-04 |
| D-05 | 起草编排子包落点 | runtime 复用会话框架 / 归 analysis / 新建子包 | **runtime 编排复用会话框架**（V6 不新增子包，M-01 协调归属；会话循环归 CM-LOOP；工件类型引 core；TaskDraftRevision 账本落 runtime 存储） | 对齐问答 Q-05 |
| D-06 | TaskDraftRevision 版本粒度 | 双轨（草稿变更才发版） / 每轮都发版 | **双轨**：工件草稿变更→新 revision；AskUser 对齐结论→Question/Answer 会话账本；确认门冻结「当前 revision+问答指针」 | 对齐问答 Q-06 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（UnderstandingDossier/TargetProjectBlueprint/MigrationRulebook/FrozenArtifactBundle/MigrationSessionStatus/QuestionId/TaskDraftRevisionId/Advice/AdviceKind.ExploreReassignment）；CM-ANALYSIS PSF 投影（经 ProjectionStore 端口——CM-ANALYSIS D-02）；CM-SPEC registry 端口（描述符锁解析）；AskUser 交互通道（M-00 InteractionStatus）。

### 3.2 下游消费

- CM-RUNTIME CreateRun（档案一致性核对函数 + FrozenArtifactBundle + TaskDraft 确认事实）；CM-PLAN Planner（四件冻结工件为输入）；CM-LOOP（起草会话类型消费本任务编排契约）；CM-MEMORY（起草会话上下文预算档）；CM-API（sessions 路由消费 TaskDraftRevision/Question 账本——CM-API-001 D-01 已定骨架）。

### 3.3 跨模块接口边界

- 探索协调者只出 Advice（ExploreReassignment，约束内建议——actor 白名单自动收养，机器校验兜底）；探索员/主会话零直写权。
- 起草会话模型档：探索员/协调者=Reasoning 档（M-06 信息分层"语义消解层=Reasoning 理解会话"）；试译=Code 档（D-04）。
- 会话事件：`migration.session.event` v1 持久 + `assistant.delta` 短暂（不推进 sequence）；复用 CM-API 事件常量。
- 与 CM-SUPERVISOR 的边界：探索协调者（起草期）与 EXECUTE Supervisor（执行期）同为判断层但阶段不同（ResidentRole 两值已定 M-00）；切域建议收养接口语义归 CM-RUNTIME 对齐细化。
- Blueprint 字段形状（M-00 占位注释"实施期定稿"）——本任务按当前占位形状消费，字段细化在 CM-PLAN 对齐时与用户收口（跨任务协调点）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M16-V4-012（可施工） | 起草会话 WriteFile/EditFile/Shell 零接纳；Exec 仅只读编排逐笔过网关；草稿经 TaskDraftRevision 账本持久化 | 编排单测（工具授权面断言 + 账本写入断言） |
| V-M16-V4-013（可施工） | 未确认草稿 Run/run_events/Slice/candidate/托管输出新增均为 0 | 集成测试（确认门副作用计数） |
| V-M16-V4-014（可施工） | 确认后 canonical Spec Artifact/hash 与 M-05 规则一致；能力门预检失败不建 Run 草稿可续改 | 契约测试（对接 CM-SPEC 门函数） |
| V6 起草流程重排（内嵌） | 扇出→归并→多轮 AskUser→草稿→试译校准→确认冻结；多轮不限次 | 流程编排单测（阶段序列断言） |
| D-01 核对集 | 三项机械核对失败→DOSSIER_INCONSISTENT 语义（零副作用由 CreateRun 断言） | 核对函数单测（违规锚点/越界成员/未解决冲突用例） |
| D-02 骨架确定性 | 同输入切域结果逐字节一致；覆盖恰好一次；>20 文件二次拆分；扇出≤6 | 骨架单测（确定性+覆盖断言） |
| D-04 试译 | 会话内呈现、零落盘、零候选触碰、零 Run 副作用 | 试译环节单测（文件系统与账本计数） |
| D-06 版本粒度 | 问答不产生 revision；草稿变更产生；确认冻结指针正确 | 账本单测 |

> V-M16-V4-001~011/015~018 为运行期修正/输出物化/契约漂移条款——归 CM-RUNTIME/CM-GIT/CM-PLAN 对齐验收面，本任务仅类型与流程支撑。

## 5. 风险与注意点

- **探索协调者与 CM-LOOP 会话框架耦合**：本任务编排依赖会话循环契约（M-04 owner）——CM-LOOP 对齐晚于本任务；实现期如两者接口冲突（如会话类型枚举），以 CM-LOOP 对齐结论为准并在本记录追加变更行。
- **Blueprint 字段待定**：TargetProjectBlueprint 字段形状是 M-00 占位（"实施期定稿"）——本任务不臆造字段；CM-PLAN 对齐时收口（主表备注已标"Blueprint 字段待定"）。
- **切域可配常数**（20 文件/扇出 6）为默认值非契约——实现走配置注入（runtime），核心逻辑不硬编码。
- **AskUser 纪律**：QuestionId/互斥选项/推荐项/影响说明/绑定 revision；无法从 Git/manifest/描述符/Spec 推导的缺口才提问；相同回答幂等、过期 revision 冲突拒绝（M-16 AskUser 节全文纪律照抄）。
- **描述符锁系统解析**：TaskDraft/message/会话上下文不能指定描述符锁（M-16 边界三条）——编排层从 registry 端口解析写入，防用户侧注入。
- 起草会话计入模型会话池与预算治理（M-00/M-14）——预算档数值归 CM-MEMORY 对齐。
- 探索扇出并行度上限与沙箱公式无关（模型会话不经沙箱——wave01 CM-ANALYSIS Q-05 澄清口径一致）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | 档案一致性断言机械核对集（§9.1 开放项收口） | 最小机械核对集（锚点可解析+模块成员⊆范围∩F1+冲突清零） |
| Q-02 | 切域骨架算法 | 语义模块域 + 20 文件二次拆分 + 扇出上限 6 |
| Q-03 | focus brief 与切域建议 payload schema | 冻结最小 schema（FocusBrief 三字段 + ExploreReassignment payload 四字段） |
| Q-04 | 试译-弃稿校准执行形态 | 热点双版本对比（Code 档、会话内呈现、零副作用） |
| Q-05 | 起草编排子包落点 | runtime 编排复用会话框架（不新增子包） |
| Q-06 | TaskDraftRevision 版本粒度 | 双轨（草稿变更才发版；问答入会话账本） |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-16 V6 方向对齐版（起草会话节/起草流程重排/试译校准节）与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-06；§9.1「档案一致性阈值」开放项经 D-01 收口 |
