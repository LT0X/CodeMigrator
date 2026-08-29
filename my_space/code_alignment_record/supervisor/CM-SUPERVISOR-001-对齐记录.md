# CM-SUPERVISOR-001 对齐记录

> 用途：本文件是任务 `CM-SUPERVISOR-001`（判断层 EXECUTE Supervisor·新增）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-SUPERVISOR-001` |
| 模块编号 | M-03/M-04/M-00/M-12（判断层·V6 新增任务；不新增子包——落 runtime 会话框架，M-01 V6 协调归属） |
| 对应设计文档 | M-03 判断层接入与减法降级节 + M-04 协调会话类型/EXECUTE Supervisor 节 + M-00 三层架构节 + M-12 V6 会话级授权行（均已在 wave23 轮通读） |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐（用户跳过逐项确认，决策采纳推荐方案——沿用惯例，见 §6） |

## 1. 任务理解

### 1.1 范围（做什么）

交付判断层 EXECUTE Supervisor 会话类型（落 runtime 会话框架实例化——CM-LOOP 通用框架 + 模板槽位 + 预算档；无独立子包）：

- **事件触发式新会话**：两条触发事件（CM-RUNTIME D-04 已接线）：①归因多义/双错（静态多命中+动态测试全失败，候选修复集>1）→全局修复决策建议；②Slice 会话失败停止→异常语义路由建议。每次触发=新会话（ExecuteSupervisor SessionKind），统一装配器定向装配（CM-MEMORY 契约：基线态势快照机器算不入上下文+定向事件投影）；无跨次常驻状态与滚动摘要。
- **仅两个职责**：①归因多义/双错→全局修复决策（RepairDecision 建议：全局修复会话或单 Slice 委派重生）；②Slice 会话失败停止→异常语义路由建议。不设防御性触发（M-00 Supervisor 精简定案）。
- **输出契约（D-01）**：只出 Advice 零直写——两种 Advice payload 经归一器收敛：
  - `RepairDecision`（M-00 契约类型）：decision_id/run_id/repair_set（SliceId 集）/domain_split（Slice→路径分配或联合域整体）/brief_refs（每修复会话一份修复简报 ArtifactRef）。
  - `RouteSuggestion`（**本对齐新增最小 schema**）：{trigger_event_refs（触发事件 run_events 序列引用）、failure_class（失败类别）、suggested_route: `delegate_regen` | `global_repair` | `terminal_fail` | `clarify`、target_slice_id?、rationale}。
- **定向事件投影内容（D-02）**：触发时装配——RepairEvidence（CM-VERIFY D-03：候选修复集+可靠性分类+两布尔）+失败测试身份与诊断摘要+相关 Slice 状态投影+同 Run 前次修复决策历史（若有，防重复决策）。
- **advice.*/repair.* 事件 data schema（D-03）**：回填 CM-API D-03 两档占位的判断层档：
  - `advice.proposed` data={advice_id, kind, role, tier(约束内/边界性), proposal_hash, payload 摘要}；
  - `advice.adopted` data={advice_id, proposal_hash, adoption_result, 影响摘要}；
  - `repair.decision` data={repair_decision_id, 修复集 Slice 摘要, 域分配摘要}。
- **收养对接**：产出 Advice 入 actor 邮箱→CM-RUNTIME D-01 四步收养（hash 核验→RepairDecision 约束内机械校验：修复集⊆归因候选集+联合域成员有效性→自动执行；RouteSuggestion 边界性→转参考）。
- **减法降级**：判断层缺席（模型故障/预算耗尽）→机械归约（控制面完整性不变，M-03 增益层语义）；VERIFY/REPORT 零模型硬边界不破。
- **模板正文结构骨架（D-04）**：角色定义（EXECUTE Supervisor·判断层）+两职责分支说明+证据消费格式（RepairEvidence 输入形态）+输出格式约束（Advice 编码+归一契约）——正文实施期填充（模板槽位 CM-LOOP D-03）。
- **工具面**：只读零写权（M-12 会话级授权行：基线态势快照/定向事件投影/只读查询）。

### 1.2 边界（不做什么）

- 不新增子包（M-01 V6 不改子包边界——runtime 会话框架实例化）。
- 不做收养决策（actor 白名单收养归 CM-RUNTIME——已对齐；本任务只产出）。
- 不做全局修复会话执行（归 CM-REPAIR——本任务只出 RepairDecision 建议与 brief_refs 装配输入）。
- 不做触发判定（归 actor 归约——CM-RUNTIME D-04 已接线；本任务消费触发）。
- 不做上下文装配器（归 CM-MEMORY——已对齐；本任务定义定向投影内容需求）。
- 不做修复简报 schema（归 CM-REPAIR——CM-MEMORY D-04 已登记；本任务的 RepairDecision.brief_refs 只引用其 ArtifactRef）。
- 不做探索协调者（起草期判断层——归 CM-DRAFT 已对齐）。
- 不做态势快照计算（Harness 机器算——CM-RUNTIME 侧；本任务消费派生态势）。

### 1.3 产出物

runtime 侧：Supervisor 会话类型实例化（触发消费/定向投影需求/归一输出 RepairDecision·RouteSuggestion/Advice 投递）；模板正文（结构骨架+实施期填充）；core 侧：RouteSuggestion payload schema 建议（经 Advice.payload 占位对齐登记）；api 侧：advice.*/repair.* 事件 data 模型回填（CM-API 占位档落地）；tests/supervisor/（触发→决策→投递→收养全链 stub 测试/降级路径/归一失败）；模块迭代记录（dev_progress/supervisor/）。

## 2. 关键实现决策与确认结论

> 用户跳过逐项确认（沿用惯例）——按推荐方案采纳并如实标注。

| # | 决策点 | 可选项 | 采纳结论（推荐方案） | 依据 |
|---|---|---|---|---|
| D-01 | Supervisor 输出契约（RouteSuggestion payload 为文档未定项） | 最小 schema / 押后 | **RepairDecision 引 M-00 契约；RouteSuggestion 定最小 schema**：{trigger_event_refs/failure_class/suggested_route: delegate_regen\|global_repair\|terminal_fail\|clarify/target_slice_id?/rationale} | 推荐采纳（2026-08-29） |
| D-02 | 定向事件投影内容（M-14 定向装配的具体输入） | 四件套 / 全量事件流 | **四件套**：RepairEvidence（CM-VERIFY D-03）+失败测试身份与诊断摘要+相关 Slice 状态投影+前次修复决策历史（防重复） | 推荐采纳 |
| D-03 | advice.*/repair.* 事件 data schema（CM-API D-03 占位档回填） | 本任务回填 / 押后 Wave 4 | **本任务回填三事件模型**（advice.proposed 五字段/advice.adopted 四字段/repair.decision 三字段摘要——见 §1.1）——CM-API 记录变更行联动 | 推荐采纳 |
| D-04 | Supervisor 模板正文结构 | 骨架先行 / 押后 | **结构骨架**（角色+两职责分支+证据格式+输出约束四段）——正文实施期填充；模板槽位复用 CM-LOOP D-03 | 推荐采纳 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 契约（Advice/AdviceKind/ResidentRole/RepairDecision/SessionKind.ExecuteSupervisor）；CM-RUNTIME（触发事件接线/收养接口/态势快照派生）；CM-VERIFY（RepairEvidence 证据接口）；CM-MEMORY（定向装配器/预算档 24k/200k/80%）；CM-LOOP（会话框架/归一器）；CM-API（advice.*/repair.* 事件常量与投影）。

### 3.2 下游消费

- CM-RUNTIME（Advice 收养执行——RepairDecision 全局修复派发或委派重生；RouteSuggestion 参考）；CM-REPAIR（RepairDecision.brief_refs 装配输入+修复会话派发依据）；CM-WEB/M-15（advice.adopted 只读决策视图消费）；CM-OBS（advice/repair 事件观测——已对齐）。

### 3.3 跨模块接口边界

- **Advice 产出↔收养接口**（并行纪律 4 交叉点收口）：产出侧本任务（归一后契约形状）；收养侧 CM-RUNTIME D-01（hash+机械校验）——两侧契约经本对齐闭环：RepairDecision 机械校验项（修复集⊆候选集/联合域成员）已定 RUNTIME 侧；RouteSuggestion 边界性转参考不自动执行。
- **api 事件回填联动**：CM-API 记录追加变更行（判断层占位档→本任务 D-03 schema）——append-only。
- **brief_refs 联动**：修复简报内容 schema 归 CM-REPAIR 对齐（本任务 RepairDecision 类型只持 ArtifactRef 引用）。
- **预算档**：ExecuteSupervisor 档（24k/200k/80%——CM-MEMORY D-01 v1 表）——单次决策会话低累计。
- 事件常量（advice.proposed/adopted/repair.decision）owner=api（CM-CORE D-05 惯例）——本任务回填 data 模型。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V6 增量（M-03/M-04/M-00） | 事件触发式新会话/仅两职责/只出 Advice 零直写/减法降级/VERIFY·REPORT 零模型 | 全链 stub 测试（触发→决策→投递→收养；降级路径：模型故障→机械归约） |
| M-13 判断层事件观测 | advice.proposed 全量进 run_events（含 proposal_hash）/advice.adopted 收养结果 | 事件契约测试（D-03 schema） |
| M-15 只读决策视图 | 已收养修复决策与路由结论投影（无收养/否决操作入口） | 归 CM-WEB Wave 4 联调（本任务提供事件源） |
| D-01 输出契约 | RepairDecision/RouteSuggestion 归一形状；RouteSuggestion 四路由值域 | 归一单测 |
| D-02 定向投影 | 四件套装配；无全量事件流；无跨次状态 | 装配联动单测（memory 档位） |
| 收养闭环 | 修复集越界（⊄候选集）→校验失败不收养；hash 篡改→丢弃+审计 | 收养联动单测（runtime 侧用例复用） |
| 白名单终值 | 约束内{ExploreReassignment,RepairDecision}/边界性{RouteSuggestion,PlanRevision,AskUser}（CM-RUNTIME D-01 已定——本任务确认产出侧一致性） | 契约断言 |

## 5. 风险与注意点

- **推荐方案未经逐项确认**（用户跳过）：D-01~D-04 标「采纳推荐」——RouteSuggestion 路由值域（四值）建议用户复审（影响失败处理分支覆盖）。
- **RouteSuggestion 的 `terminal_fail` 与 M-10 语义关系**：路由建议仅供 actor/M-10 参考（边界性建议不自动执行）——`terminal_fail` 建议不直接产生终态（终态归 actor 归约：全局修复重试耗尽才 VerificationTerminal）——模板正文须明确该约束防模型越权表述。
- **降级测试必要性**：判断层缺席路径（provider 故障→机械归约→Run 达合法终态）是 P-09 V6 闭环验收场景——M-00/主表 §10 场景 5 联动。
- **与探索协调者的对称性**：两者同为判断层但阶段/模板/触发不同（起草交互式 vs 执行事件触发式）——实现共享会话框架但模板与装配路径分开（勿混用装配逻辑）。
- **repair.decision 事件 vs RepairDecision 实体**：事件 data 为摘要投影（脱敏+摘要哈希）；完整实体经 actor 收养落账——投影不含正文（M-13 脱敏出口）。
- Supervisor 会话配额计入模型会话池（M-00/M-14）——多 Run 并发时触发频次受池约束。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01~Q-04 | RouteSuggestion schema/定向投影内容/事件回填/模板骨架（拟提问批） | **用户未接受提问**（沿用跳过惯例）——按推荐方案采纳并如实标注 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-03/M-04/M-00/M-12 判断层相关节（wave23 轮已通读）与主任务表 §7.3；用户跳过逐项确认，四项决策按推荐方案采纳（§2）；主表 §7.3「Advice 白名单枚举待定」备注经 CM-RUNTIME D-01+本任务产出侧确认闭环；CM-API 判断层事件占位档经 D-03 回填（联动变更行） |
