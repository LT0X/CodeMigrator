# CM-GIT-001 对齐记录

> 用途：本文件是任务 `CM-GIT-001`（模块 M-11 工作空间与 Git 集成）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-GIT-001` |
| 模块编号 | M-11（V5 方向对齐版；V6 修复 FIFO 由本对齐 D-03 补充） |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_工作空间与Git集成.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/workspace/` 子包 Git 侧（M-01：M-08/M-11/M-12 共用 workspace 子包；Git 底层=**git CLI subprocess 薄封装**，D-01）：

- **双 Git 根事实**：源快照（`snapshot_oid`，Run 创建一次冻结/RegisteredProject 复用项目 snapshot 对象本地拷贝、RemoteRepository 一次 fetch；全程零写入零推进）+ 输出基线（`refs/codemigrator/runs/<run_id>/base` 空 tree root commit，一次初始化不可变）；输出历史根=空基线不含源 commit。
- **托管 clone 形态**（D-02）：每 Run 一个裸仓库 `~/.codemigrator/runs/<run-id>/repo.git`；候选工作区（目录即卷，CM-WORKSPACE D-04）经环境变量 `GIT_DIR/GIT_WORK_TREE` 操作——工作区零 `.git` 痕迹（沙箱侧不可见不可写，M-08 信任模型字面落地）；验证临时物化/export 用 `git archive`/checkout 到新目录。
- **ref 八族布局**：base/verified/slices candidates/integration scratch/failed/abandoned/用户交付分支 + **repair ref 族**（D-03：`refs/codemigrator/runs/<run>/repairs/<repair_session_id>/candidates/<n>`）。
- **checkpoint ref 推进**：`git update-ref <ref> <new> <expected-old>` 原生 CAS（冲突→`CANDIDATE_REF_CONFLICT` 零副作用）；tree 条目=blob 指针（消费经 blob 解引用）。
- **集成五步事务**（意图—Git—回执）：读取 verified OID→应用队首 checkpoint 输出文件集建立 scratch（纯文件集落位、无三方合并无补丁）→ProspectiveIntegration 增量验证→事务 1 持久化 IntegrationIntent→CAS 推进 verified→事务 2 receipt+run_event→删 scratch 与 candidate ref；`applied_paths` 恰等于 checkpoint tree 相对 base_verified 全部输出路径（越界=控制面完整性错误）；崩溃窗口 A/B 处置 + `RECOVERY_LEDGER_INCONSISTENT`。
- **修复 FIFO 集成**（D-03，V6 P-09 ref 侧落地）：修复会话候选走 repair ref 族；集成序=修复会话完成 FIFO（不进冻结 integration_rank 队列、不占原 Slice generation 0-2）；基线每次取当时最新 verified 分叉；集成走同一 CAS+intent+receipt 事务；修复集成编排细节归 CM-REPAIR 对齐消费。
- **重生成与终态取证**：generation 0-2 语义（物理重派只换 attempt）；failed/abandoned ref 30 天；`SLICE_REGENERATION_EXHAUSTED` 恰一次；预算耗尽 checkpoint 后归档。
- **远端交付**：PushGuard（branch_prefix 派生固定分支名、无 `+` refspec、首次要求远端不存在、`frozen_last_pushed_oid==expected==observed`、远端移动先 fetch 重建 intent、绝不 force push）；`REMOTE_REF_MOVED`；push/PR 失败只改 code_delivery_status；PR=DeliveryAdapter 投影。**凭据机制**（D-04）：环境注入 git credential helper 指向受信存储（凭据来自部署配置/受信文件、权限 600、不进代码/事件/指标；receipt 脱敏经 M-13 出口）。
- **export 物化**：Run 终态按 verified head export（COMPLETED 完整/PARTIAL 依赖闭合子集+失败清单/取消失败仅有有效进展才物化）；临时目录+原子 rename；export 不写源仓库。
- **源项目零写入**：托管 clone 外零写入；不在源目录创建任何元数据。

### 1.2 边界（不做什么）

- 不做集成编排/队列消费调度（Coordinator 编排归 M-03/CM-RUNTIME；本任务提供 Git 事务原语与应用函数）。
- 不做验证裁决（M-10/CM-VERIFY；prospective 验证经其端口）。
- 不做候选工作区生命周期/checkpoint 触发（M-08/CM-WORKSPACE 已对齐；本任务提供 ref 推进与 commit 构建原语）。
- 不做修复会话编排/修复简报/联合域判定（M-04/M-07/CM-REPAIR 对齐；本任务提供 repair ref 族与 FIFO 集成原语）。
- 不做输出目录派生物化触发（M-16 输出物化规则；本任务提供 export 原语）。
- 不做 30 天清理调度（M-13/CM-OBS 清理器；本任务提供 ref 枚举与删除原语）。
- 不做 intent/receipt 的 PostgreSQL 事务执行（M-03 owner；本任务提供事务编排所需 Git 事实与幂等重试原语）。

### 1.3 产出物

workspace 子包 Git 侧：git CLI 封装端口（CAS update-ref/commit tree 构建/blob 解引用/fetch/archive/push/PushGuard）、双根事实初始化、集成应用函数（FileSetApplication）、修复 ref 族与 FIFO 原语、恢复对账原语（窗口 A/B/LEDGER_INCONSISTENT）、export 物化、credential helper 受信注入；tests/git/（CAS 冲突/集成五步事务/恢复窗口/八族 ref 生命周期/PushGuard 用例）；模块迭代记录（dev_progress/workspace/）。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | Git 底层库选型（CM-WORKSPACE D-06 归属本任务） | git CLI subprocess / dulwich / GitPython | **git CLI subprocess 薄封装**：`git update-ref <ref> <new> <expected-old>` 原生 CAS 与 M-11 语义完美匹配；fetch/push/auth 行业标准；结构化错误解析封装 | 对齐问答 Q-01（2026-08-29） |
| D-02 | 托管 clone 物理形态（M-08 外部 GIT_DIR 信任模型） | 裸仓库+环境变量 / git worktree 机制 | **每 Run 裸仓库 `repo.git` + 环境变量 GIT_DIR/GIT_WORK_TREE**：候选工作区零 `.git` 痕迹（沙箱不可见不可写）；验证物化/export 用 git archive | 对齐问答 Q-02 |
| D-03 | 全局修复 ref 布局与集成序（V6 P-09；M-11 V5 未细化） | repair ref 族+FIFO / 混入冻结队列 / 押后 | **repair ref 族**：`refs/codemigrator/runs/<run>/repairs/<repair_session_id>/candidates/<n>`；集成序=修复完成 FIFO（不进冻结队列、不占 generation 0-2）；基线取最新 verified；同一 CAS+intent+receipt 事务 | 对齐问答 Q-03 |
| D-04 | 远端交付凭据机制 | 环境注入 credential / 押后 Wave 4 | **环境注入 git credential helper**：凭据来自部署配置/受信文件（权限 600，app 数据目录受信区），不进代码/事件/指标；receipt 脱敏 | 对齐问答 Q-04 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（GitRunRefs/CandidateGeneration/IntegrationIntent/RunStatus/VerificationOutcome/DeliveryChannelStatus/幂等键构成）；CM-PLAN（冻结 write scope/integration_rank）；CM-WORKSPACE（工作区目录即卷/GIT_DIR 外部持有约定）；CM-INFRA（app 镜像装 git、repo.git 目录布局）。

### 3.2 下游消费

- CM-RUNTIME/M-03（Coordinator 集成编排消费 Git 事务原语/Recovery Coordinator 对账）；CM-VERIFY/M-10（prospective/最终验证物化原语）；CM-REPAIR（repair ref 族+FIFO 原语）；M-16/M-15（export 物化）；CM-API/M-02（交付状态投影）。

### 3.3 跨模块接口边界

- **CAS 语义唯一实现**：expected-OID CAS（update-ref 原生）只出现在 candidate/scratch/verified/repair ref 推进——数据库 version 不替代 Git 竞争判断（M-00/M-11 明文）。
- **intent 先于 Git**：IntegrationIntent 在 CAS 前持久化（禁止先推进再补造 intent）；事务边界编排归 M-03，本任务提供事实与重试原语。
- **修复 FIFO 与 CM-REPAIR 联动**：本任务冻结 ref 族与集成原语；修复会话派发/重试上限/简报归 CM-REPAIR 对齐；独立重试上限数值（§9.1）归 CM-REPAIR/CM-VERIFY 对齐收口。
- **snapshot 复用**：RegisteredProject 从项目 snapshot 存储（M-16 `projects/<id>/snapshots/`）本地拷贝对象进 Run 裸仓库；RemoteRepository 一次 fetch——不重复下载。
- **凭据纪律**：凭据文件在受信区（不进仓库/事件/指标/git 输出）；`repository_url` HTTPS-only（M-02 边界）。
- failed/abandoned 30 天清理由 M-13 清理器调度（CM-OBS 已对齐）；本任务提供删除原语。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M11-V4-001（追溯） | 源快照与源仓库全程零写入（文件/.git/ref/mtime） | 契约测试（文件系统监控） |
| V-M11-V4-002 | base 空基线零变化；输出历史根=基线、零源 commit | 单测（commit 图断言） |
| V-M11-V4-003 | 独立 candidate ref/工作区/checkpoint 链；write scope 并行互斥 | 单测 |
| V-M11-V4-004 | checkpoint 只 CAS 推进本 Slice candidate；冲突 CANDIDATE_REF_CONFLICT 零副作用 | 单测（update-ref CAS 用例） |
| V-M11-V4-005 | prospective 路径差异恰等于冻结 write scope 输出文件集；零补丁对象 | 集成应用单测 |
| V-M11-V4-006 | 零三哈希/content_sha256 计算；集成失败唯一来源=M-10 语义失败 | 静态扫描 |
| V-M11-V4-007/008 | intent 先于 CAS 完整冻结；恢复窗口 A/B 幂等重试/只补 receipt；分叉 RECOVERY_LEDGER_INCONSISTENT | 恢复窗口单测（崩溃注入） |
| V-M11-V4-009 | 完成顺序 100 次集成序不变；非队首停 INTEGRATION_QUEUED | 归约单测（与 CM-RUNTIME 联调证据归后完成方） |
| V-M11-V4-010 | generation 0-2 约束；耗尽恰一次+failed ref | 单测 |
| V-M11-V4-011 | 用户分支只指 verified；refspec 无 +；REMOTE_REF_MOVED；交付失败只改代码通道 | PushGuard 单测 |
| V-M11-V4-012 | export 语义四态（完整/部分/边界/零输出）；export 零写源仓库 | 物化单测 |
| V-M11-V4-013 | 取消转 abandoned 30 天；取消后零推进 | 单测 |
| V5 增量 | CAS 单写者/FIFO 集成/PlanRevision 不回写/交付隔离 | 上述覆盖 |
| D-03 修复 FIFO | repair ref 族创建/推进/集成走 CAS+intent+receipt；不进冻结队列；基线取最新 | 单测（修复集成用例） |
| D-02 信任模型 | 工作区目录零 .git 痕迹；沙箱侧探测失败 | 安全测试（目录扫描断言） |
| D-04 凭据 | 凭据不入 git 输出/事件/日志；受信区权限 600 | 安全测试 |

## 5. 风险与注意点

- **git CLI 版本一致性**：app 镜像 git 版本与行为（update-ref CAS 失败输出格式）需固定解析测试；镜像 pin 由 CM-INFRA D-06 登记。
- **Windows/WSL 路径**：repo.git 与工作区卷均在 WSL 文件系统（app 容器内 Linux 路径）——无跨平台问题；但 git 配置（core.autocrlf=false、user.name/email 固定值）须显式设置避免环境差异。
- **修复 FIFO 与冻结队列的交叉**：修复集成期间冻结队列队首是否等待——集成单写者串行语义（Coordinator 唯一）；修复集成与正常集成同走 Coordinator 串行通道（M-03 联动细化，CM-REPAIR 对齐确认）。
- **intent 事务编排归属**：本任务提供原语，五步事务的 PostgreSQL 编排归 CM-RUNTIME——联调证据按并行纪律 5 登记在后完成方。
- **credential helper 实现形态**：受信脚本/文件由部署注入（凭据经 my_space/.env 或部署 secret 映射进容器，AGENTS §1.1 纪律）；代码零硬编码。
- **git archive 物化的原子性**：临时目录+os.replace 与 M-16 物化规则一致（引用其语义）。
- 空输出基线的 root commit（空 tree）——canonical 构造（tree hash 4b825dc... 固定值或 git mktree 生成）；实现用标准命令勿手拼。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | Git 底层库选型 | git CLI subprocess 薄封装（update-ref 原生 CAS） |
| Q-02 | 托管 clone 物理形态 | 每 Run 裸仓库 + 环境变量 GIT_DIR/GIT_WORK_TREE（工作区零 .git 痕迹） |
| Q-03 | 全局修复 ref 布局与集成序 | repair ref 族 + 修复完成 FIFO + 基线取最新 + 同一 CAS 事务 |
| Q-04 | 远端交付凭据机制 | 环境注入 git credential helper（受信存储/权限 600/receipt 脱敏） |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-11 设计文档与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-04；V6 修复 FIFO 的 ref 侧布局经 D-03 补充（M-11 V5 版未细化——实施期文档同步候选，随 CM-REPAIR 落地后回填 M-11） |
