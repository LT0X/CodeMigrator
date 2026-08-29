# CM-VERIFY-001 对齐记录

> 用途：本文件是任务 `CM-VERIFY-001`（模块 M-10 验证引擎）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-VERIFY-001` |
| 模块编号 | M-10（V6 收敛版 fb11·三层 Oracle + 两级修复路由执行侧） |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_验证引擎.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐（用户跳过逐项确认，六项决策采纳推荐方案——见 §6 溯源说明） |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/verification/` 子包（M-01 领域层：纯逻辑与归约，不启动进程/不推进 ref/不连数据库——执行经端口）：

- **三层检查集实例化**：局部（Compile+TypeCheck）/集成（全部非 Scaffold：Compile+Lint+TypeCheck+Test 在场门控）/最终（Test 模板全集）；`frozen_required_checks_sha256`（CheckId 字节升序 canonical）；集合完整性四错码（CHECK_MISSING/CHECK_DUPLICATE/CHECK_UNEXPECTED/INVOCATION_HASH_MISMATCH——进 core StableErrorCode）。
- **Test 空集预派发跳过**：派发前确定性查表（Git tree+冻结计划），空集零 bwrap 零目录，Passed/SkippedEmpty typed receipt 入 fingerprint。
- **执行事实归一**：七行优先级映射（取消/输出上限/超时/基础设施/seccomp→Failed/exit0→Passed/exit≠0→Failed）；launch 前登记 canonical empty ArtifactRef；seccomp denial 伴 exit0 仍 Failed。
- **诊断归因（P-09）**：受信诊断解析器（file:line/TestIdentity/Unknown + severity/stable code/message_hash）；符号级主路径（PSF-2 覆盖边+ReferenceSite 落点符号化+引用闭包判定）→文件级两步降级→守恒信号辅助三分支→其余统一；`TEST_FAILURE_ATTRIBUTED` 事件；归因禁猜硬边界。
- **可靠域直通+其余统一（V6 收敛）**：机械归因输出候选修复集+可靠性分类（D-03：Reliable/Uncertain/Dynamic 三值+strong_coupling/cross_generation_recurrence 两布尔）；静态唯一命中且无强耦合→直通（原 Slice 重生占 generation 0-2 带升级包）；其余（多命中/动态失败/守恒后多义/复发/强耦合）→统一唤醒 Supervisor；**强耦合信号=D-01 定义**（同次失败诊断证据同时命中接口签名定义处与调用处 ≥2 Slice write scope）；**lint 噪音过滤=D-01**（仅 severity=Error 诊断参与直通与归因，Warning 入报告不驱动重生成）。
- **失败处理优先级表**（五行唯一命中）+ 层×归因矩阵（反馈修复 2 次上限/定向重生成/其余统一升级）。
- **全局修复重试上限**（D-02，§9.1 开放项收口）：**总尝试 3 次**（首次+2 重试，与 generation 0-2 同构；可配常数）；每次新证据驱动；预算断路器兜底；耗尽才 VerificationTerminal→FAILED 或 IndependentSliceTerminalFailure→PARTIALLY_COMPLETED。
- **验证策略资源**（D-04）：core 静态资源 `core://verification-policy/v1`（JSON+sha256：flaky 重跑 2 次共 3 执行/多数阈值 2/3/反馈修复上限 2/守恒带宽 [0.5,2.0]/修复重试 3/超时默认档）；启动核验/Run 冻结/运行期零变更（与 phase policy 同机制）。
- **flaky 重跑**：仅 Test/Failed/集成与最终层；同 tested commit 同模板新目录重跑 2 次共 3 次 per-test 多数判定；归一在 CheckResult 发布前；漂移→NONDETERMINISTIC。
- **诊断解析器**（D-05）：内置注册表（按 CheckAction+program 匹配常见工具结构化输出：pytest/mypy/ruff/go vet 等首对必用集）；未匹配→诊断落 Unknown（Error 级计入 guard——文档既有兜底语义，宁阻断不猜测）；注册表扩展走 M-01 注册表扩展档。
- **语义 fingerprint**：canonical(tested_commit_oid+frozen_required_checks_sha256+semantic_results)；诊断 semantic hash 规范化（剥离时间戳/绝对路径归一/稳定排序）；Final vs Prospective 共有 CheckId 逐项比较；GENERATED 标注维度不入 fingerprint。
- **Oracle 派生门**：DerivedVerificationGuard=exact-set∧全 Passed∧Error UNKNOWN=0；纯派生零外部提交。
- **生成测试验证语义**：同执行面同判据；GENERATED 标注贯通四处；LOW_QUALITY 门槛（非平凡断言 AST 确定性判定）；双档分级。
- **结构守恒计算**：目标端 tree-sitter（经 CM-ANALYSIS 端口）统计 verified 树 per-module 三比值，[0.5,2.0] 带宽离群；不参与 pass/fail；失败证据模糊时第三信号。
- **行为 parity 场景对比**：用户确认场景（起草期冻结）；Final 通过后源副本隔离环境+目标项目同场景 diff；**源侧运行环境=D-06**（源端描述符可选 `runtime_image_digest` 字段——声明源语言运行时镜像；未声明/源不可运行→手段缺席如实披露）。
- **裁判可信前提**：收集完备性交叉核对（目标用例数 vs 源侧基线逐模块）+源侧基线冒烟校验（置信如实降级披露）。
- **最终验证闭环**：先收齐持久化→稳定性比较优先→漂移短路重生成→普通失败走归因路由；定向重生成 RunStatus 保持 VERIFYING。
- **验证边界声明**：三边界外维度（性能/安全/生态）+共谋盲区披露。

### 1.2 边界（不做什么）

- 不启动 bwrap/进程（M-09 owner——CM-SANDBOX 已对齐；经端口派发 InternalVerificationDispatch 语义）。
- 不推进 Git ref/不写 candidate/verified（M-11/M-03 owner）。
- 不做 Supervisor 决策/Advice（M-03/M-04 owner——CM-SUPERVISOR 对齐；本任务输出候选修复集+可靠性分类作为证据输入）。
- 不做全局修复会话执行（CM-REPAIR 对齐；本任务提供修复重试计数契约与重验证入口）。
- 不做修复简报 schema（归 CM-REPAIR——CM-MEMORY D-04 已登记）。
- 不做 Shell 自检（M-12 owner——自检=反馈不裁决不入本引擎）。
- 不做检查命令模板内容（M-00/M-01 owner——描述符资源）。
- 不做 run_events 写入（M-02/M-03 owner——本任务产出事实经 actor 投影）。
- 不做报告拼装（REPORT 确定性模板——归 CM-RUNTIME/M-02 侧；本任务产出证据页素材事实）。

### 1.3 产出物

verification 子包：三层实例化器/集合完整性校验/执行事实归一映射/诊断解析器注册表（首对工具集）/P-09 归因器（符号级+文件级+守恒辅助）/可靠性分类器/失败处理归约/flaky 归一/fingerprint 组装/守恒计算（经 analysis 端口）/parity 执行器/裁判可信前提核对/边界声明常量；core 侧：verification-policy/v1 资源+四集合错码与重试上限常量登记；tests/verification/（归因用例矩阵/flaky 用例/空集跳过/漂移比较/守恒带宽/反向自检流程）；模块迭代记录（dev_progress/verification/）。

## 2. 关键实现决策与确认结论

> 本任务决策确认提问被用户跳过——六项按推荐方案采纳并如实标注（用户可经再对齐修订，走主任务表 §8 流程）。

| # | 决策点 | 可选项 | 采纳结论（推荐方案） | 依据 |
|---|---|---|---|---|
| D-01 | 可靠域直通「无强耦合」精确判定+lint 噪音过滤（§9.1 开放项） | 定义处×调用处+Error 级 / import 链扩展 / 押后 | **强耦合=同次失败诊断证据同时命中接口签名定义处与调用处 ≥2 Slice write scope（机械查表）；lint 过滤=仅 severity=Error 参与直通与归因（Warning 入报告不驱动重生成）** | 对齐问答 Q-01（用户跳过，采纳推荐） |
| D-02 | 全局修复独立重试上限数值（§9.1 开放项） | 3 / 5 / 押后 | **总尝试 3 次（首次+2 重试，与 generation 0-2 同构；可配常数）**；新证据驱动+预算断路器 | 对齐问答 Q-02（用户跳过，采纳推荐） |
| D-03 | 归因可靠性分类 schema（M-07/M-10 开放项；CM-PLAN 登记归本任务） | 三值枚举+两布尔 / 数值置信度 / 押后 | **三值枚举（Reliable=静态唯一命中零推测/Uncertain=静态多命中或守恒后仍多义/Dynamic=动态测试失败本质推测）+ 两布尔（strong_coupling/cross_generation_recurrence）**——Supervisor 机器可读证据输入 | 对齐问答 Q-03（用户跳过，采纳推荐） |
| D-04 | 验证策略资源形态 | core 静态资源 / 硬编码 | **core 静态资源 `core://verification-policy/v1`**（JSON+sha256；启动核验/Run 冻结/零变更——M-10「编译时冻结策略资源」落地） | 对齐问答 Q-04（用户跳过，采纳推荐） |
| D-05 | 诊断解析器实现形态 | 内置注册表+Unknown 兜底 / 描述符声明 / 押后 | **内置注册表**（按 CheckAction+program 匹配首对工具集结构化输出；未匹配落 Unknown·Error 级入 guard——宁阻断不猜测）；扩展走 M-01 注册表扩展档 | 实施推导（推荐方案，用户跳过提问后采纳） |
| D-06 | parity 源侧运行环境 | 源端描述符可选字段 / 宿主直跑 / 押后 | **源端描述符扩展可选 `runtime_image_digest` 字段**（源语言运行时镜像；未声明→parity 缺席分支既有语义；文档同步候选——SourceToolchain 契约扩展随实施回填 M-00/M-01） | 实施推导（推荐方案，用户跳过提问后采纳） |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（CheckResult/VerificationSubject/VerificationOutcome/DerivedVerificationGuard/DiagnosticMapping/CheckStatus/fingerprint 语义类型）；CM-PLAN（write scope 查表映射/required checks/在场门控覆盖映射）；CM-ANALYSIS（PSF-2 索引/F3 覆盖映射/F2 import 图/守恒基线/目标端 tree-sitter 端口）；CM-SANDBOX（InternalVerificationDispatch 执行端口/临时物化）；描述符命令模板（registry 端口）。

### 3.2 下游消费

- CM-RUNTIME/M-03（guard 消费/失败处理归约/Supervisor 唤醒触发/重试计数）；CM-SUPERVISOR（候选修复集+可靠性分类证据输入）；CM-REPAIR（重验证入口/修复闭环）；M-15/REPORT（证据页素材：通过率/失败清单/flaky/覆盖映射/GENERATED 标注/双档分级/边界声明/parity/守恒）；CM-MEMORY（检查日志外置 CAS/重生简报素材）。

### 3.3 跨模块接口边界

- **core 资源新增**：verification-policy/v1（D-04）+ 集合四错码+修复重试常量进 StableErrorCode/契约（CM-CORE 变更行追加登记）。
- **Supervisor 证据接口**：本任务输出 `RepairEvidence{candidate_slice_set, reliability: 三值, strong_coupling, cross_generation_recurrence, 守恒信号摘要}`——CM-SUPERVISOR 对齐时消费（schema 若需扩展在其记录登记）。
- **修复重试计数**：计数归 Run actor（M-03 owner）；本任务提供「每次修复须新证据」的判定函数与耗尽语义。
- **守恒计算端口**：目标端 tree-sitter 解析复用 CM-ANALYSIS 基建（grammar registry/熔断）——经端口调用不复制。
- **parity 环境注入**：源端 runtime_image_digest（D-06）由描述符 registry 提供；执行经 CM-SANDBOX bwrap（源侧与目标侧同隔离纪律）。
- **诊断解析器与描述符联动**：首对（pytest/mypy/ruff/go vet）内置解析器；新语言对工具输出→注册表扩展档（M-01 动态边界第三档）。
- 在场门控（V-M10-V4-027）消费 M-07 冻结覆盖映射——联调证据归本任务与 CM-PLAN 双方登记。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V6 收敛-001 | 可靠域直通分界（唯一命中+无强耦合→直通；其余统一 Supervisor） | 归因器单测（D-01 判定用例矩阵：唯一/多命中/强耦合/复发） |
| V6 收敛-002 | 机械归因证据化（候选修复集+可靠性分类=输入参考非裁决） | 归因输出 schema 单测 |
| V6 收敛-003 | 修复重试上限与 Run 终态边界（耗尽才终态） | 重试计数单测（D-02：3 次耗尽归约） |
| V-M10-V4-001~003 | 三层实例化正确（局部/集成/最终模板筛选） | 实例化单测 |
| V-M10-V4-005 | 集合完整性四错码阻断零推进 | 集合校验单测 |
| V-M10-V4-006 | 每 check 独立临时目录（经 sandbox stub 断言） | 端口契约测试 |
| V-M10-V4-007/008 | TimedOut≠Failed 不入 flaky；flaky 3 次多数判定 | flaky 单测 |
| V-M10-V4-009~011 | 静态/符号级/文件级归因+禁猜硬边界 | 归因用例矩阵 |
| V-M10-V4-012~014 | 反馈修复 2 次上限；generation 语义；终态保持 VERIFYING | 归约单测 |
| V-M10-V4-015 | Final vs Prospective 共有 CheckId 漂移→NONDETERMINISTIC 零重生成 | 稳定性比较单测 |
| V-M10-V4-016 | Shell 自检不入 fingerprint | 隔离单测 |
| V-M10-V4-017~019 | 迟到结果零写入；部分完成判定；UNKNOWN 门 | 契约测试 |
| V-M10-V4-021 | Test 空集零 bwrap 零目录 SkippedEmpty | 空集单测 |
| V-M10-V4-022 | 守恒计算确定性逐字节一致零模型 | 守恒单测 |
| V-M10-V4-023/024 | 生成测试同判据+GENERATED 标注+双档 | 执行面单测 |
| V-M10-V4-025 | 守恒辅助三分支 | 辅助归因单测 |
| V-M10-V4-026 | 边界声明进证据页素材 | 常量断言 |
| V-M10-V4-027 | 在场门控（覆盖映射查表） | 门控单测 |
| V-M10-V4-028 | Oracle 反向自检（变异注入 Failed） | 一次性验收流程（人工归档） |
| V-M10-V4-029 | parity 场景对比（源副本+目标 diff；缺席分支） | parity 单测（D-06 环境 stub） |
| D-04 策略资源 | sha256 核验/Run 冻结/零变更 | 资源加载单测 |
| D-05 解析器 | 首对工具输出归一正确；未匹配落 Unknown | 解析器单测 |

## 5. 风险与注意点

- **推荐方案未经逐项确认**（用户跳过提问）：六项决策均标「采纳推荐」——goal 实现前用户可快速复审 §2 表格修订（修订走记录变更行+主表 §8）。
- **强耦合判定粒度**：定义处×调用处双命中需 write scope 查表两次（诊断集合分组）——实现注意契约 Slice 检索（无契约 Slice 时按实际承载 Slice，文档语义）。
- **parity 源端镜像依赖**：D-06 字段未声明时 parity 缺席——首对（Go→Python）若需 parity 须建源端 Go 运行时镜像（CM-INFRA deploy 协同）；批次 1 验收可选择缺席分支。
- **诊断解析器覆盖率**：首对工具输出格式版本差异（pytest 版本输出变动）——解析器按结构化特征（JSON 输出模式优先：`--json-report` 类开关？不可——命令面冻结不能改 argv！）→ 解析器只能解析模板给定的默认输出格式——**模板 argv 决定输出形态**，解析器与描述符模板 argv 配套设计（描述符模板若用 `pytest -q` 则解析器解析 `-q` 文本格式）。跨任务协调点：CM-INFRA 首对描述符命令模板与解析器配套。
- **守恒目标端解析**：verified 树文件经 analysis tree-sitter 端口解析（非沙箱）——注意与 PSF 投影键区分（verified 树非源快照）。
- **可靠性分类进 core**：三值枚举（AttributionReliability）进 core 公共契约（CM-CORE 变更行登记——实施期文档同步候选 M-00/M-07/M-10 三篇联动）。
- 反向自检（V-M10-V4-028）为一次性人工验收流程——实施期归档证据，不建常驻机制。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01~Q-04 | 可靠域直通精确判定/修复重试上限/可靠性分类 schema/策略资源形态（四题一批） | **用户跳过本批确认**（提问被取消）——按推荐方案采纳：定义处×调用处+Error 级 / 3 次 / 三值枚举+两布尔 / core 静态资源；D-05/D-06 为同批推导的推荐采纳 |
| — | 溯源说明 | 用户在本轮对齐中连续确认了 DRAFT/PLAN/WORKSPACE/GIT/MEMORY 五任务的全部决策点后跳过本批——推测为信任推荐方案或希望减少互动轮次；记录如实标注，未冒充用户逐项确认 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-10 V6 收敛版设计文档与主任务表 §7.3；用户跳过逐项确认提问，六项决策按推荐方案采纳并如实标注（§2/§6）；§9.1「可靠域直通精确判定」「全局修复重试上限」经 D-01/D-02 收口、「修复简报 schema」CM-MEMORY D-04 已登记归 CM-REPAIR |
