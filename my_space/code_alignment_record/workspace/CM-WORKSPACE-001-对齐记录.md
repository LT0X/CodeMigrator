# CM-WORKSPACE-001 对齐记录

> 用途：本文件是任务 `CM-WORKSPACE-001`（模块 M-08+M-12 执行面）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-WORKSPACE-001` |
| 模块编号 | M-08（候选工作区与工具网关·唯一 owner）+ M-12（工具系统与 Hook·执行面承接） |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_候选工作区与工具网关.md`、`CodeMigrator_工具系统与Hook.md` |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 2 轮（Wave 2+3 全线对齐轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐（含两处文档偏差登记：Shell 统一超时语义；其余按文档） |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/workspace/` 子包（M-08 候选工作区生命周期 + M-12 工具网关执行面；M-01 归属：M-08/M-11/M-12 共用 workspace 子包）：

- **ToolGateway 门禁链**（M-12 owner 语义）：phase 成员测试（core policy `core://phase-tool-policy/v2`，启动核验 payload sha256、Run 冻结、零二次加载）→ schema admission（closed-schema 判别联合 extra=forbid；`TOOL_SCHEMA_INVALID`/`TOOL_NOT_FOUND`）→ 路径安全门（7 规则：形态规范化/绝对与 `~` 拒绝/遍历拒绝/`.git` 拒绝/符号链接 no-follow O_NOFOLLOW 逐段解析/根绑定 dirfd+st_dev 挂载边界/句柄 inode 竞争标注）→ 域校验（可读根/冻结 write scope 表判定）。
- **六工具执行侧**（L1-L4 分层）：ReadFile（三可读根+`cas://` 数据块第二形态、64MiB/256KiB/2000 次上限）、WriteFile/EditFile（临时文件+fsync+原子 os.replace；逐写路径门+账本）、QuerySourceAst（查 PSF-2 索引，M-06 服务经端口）、Shell（长驻沙箱卷自由命令）、Exec（app 进程内嵌入式 JS 引擎编排，工具桥逐笔回网关）。
- **V6 会话级授权行**（phase 之上叠加）：协调会话（探索协调者/Supervisor）=只读零写权；修复会话=EXECUTE 六工具+升级读视野（D-03）+联合域写；VERIFY/REPORT 对任何会话任何工具恒 `TOOL_PHASE_DENIED`。
- **候选工作区生命周期**：Provisioned（目录即卷，D-04）→ Iterating（自由迭代、账本逐次记录、Harness 零介入内容）→ Checkpointing（quiesce→枚举→批量校验→commit→CAS 推进→receipt）→ Frozen→集成/废弃销毁；崩溃/断连/取消→脏工作区整体丢弃、同 generation 物理重派从最近 checkpoint 重建（无则空基线）。
- **write scope 防护双轨**：事前（结构化工具逐写路径门）+事后（`checkpoint.pre` 批量校验：Git diff∖build_excludes ⊆ 冻结 write scope；diff 锚点=宿主受信 git 外部 GIT_DIR、沙箱侧不可见；变更集口径=相对种子基线、symlink 目标参与域判定、删除域内合法/域外越界、create_roots 仅新建权）；纯 Shell 越界=可自纠失败（拒绝提交零推进、越界清单回上下文、generation 不消耗）；结构化通道越界审计存在=基础设施事故（工作区丢弃重派）。
- **checkpoint 提交四步**（quiesce 终止进程组→批量校验→commit 创建→expected-OID CAS 推进→receipt+run_events 同事务）+ 幂等键（run/slice/generation/candidate OID/内容摘要）+ 恢复窗口六行表处置。
- **WorkspaceFileOperation 账本**：每次成功结构化写一条（工具/路径/字节/disposition），先于工具结果返回落账，与 tool.call.pre/post 同事务投影；零正文。
- **审计点位五类**（tool.call.pre/post、checkpoint.pre、advice.proposed/adopted——M-12 最小集）。
- **生成 action 执行侧**：GeneratedCode 从源头重新生成（scaffold 档模板、Harness 受信提取、不经 Agent 工具面）。
- **静态会话模板库**（D-02）：core 静态资源 `core://session-templates/v1`（每会话类型一模板+manifest sha256，与 phase policy 同机制：启动核验/Run 冻结/运行期零变更；不经 ToolGateway）。
- **`[cm:*]` 标记分段动作编码**：默认协议（行首 `[cm:action]`/`[cm:/action]` 定界、单轮多段、逐段执行逐段回灌、解析失败回灌纠偏）+ 严格 JSON 兼容回退；两种形态同一 schema admission。
- **Exec 引擎**（D-01）：quickjs 同步桥——引擎在工作线程同步执行、工具桥同步实现（subprocess/直接文件操作）、Promise.all 退化顺序执行（与 M-12「脚本内 Shell 调用串行为主」一致）、零环境权威（不暴露 FS/网络/进程 API，唯一出口=工具桥）。
- **Shell 超时统一语义**（D-05，文档偏差）：统一等待至命令完成；缺省=上限=600s（可配常数）；模型传 `timeout_secs>600` 拒绝（TOOL_SCHEMA_INVALID）；超时→SHELL_TIMEOUT+facts（已运行时长+拆步建议）。

### 1.2 边界（不做什么）

- 不做 phase policy 资源内容与加载机制（core owner——CM-CORE D-04；本任务消费）。
- 不做 QuerySourceAst 行为/索引（M-06 owner——CM-ANALYSIS 已对齐；经端口消费）。
- 不做 bwrap 物理隔离/三池/网络两档（M-09 owner——CM-SANDBOX 已对齐；本任务调其适配端口）。
- 不做 Git 底层事务/ref 布局（M-11 owner——CM-GIT 对齐；本任务经端口请求 commit/diff/ref 推进，Git 库选型归 CM-GIT-001，D-06）。
- 不做会话循环/调用轮次/会话失效（M-04 owner——CM-LOOP 对齐；本任务提供工具执行面）。
- 不做验证裁决（M-10 owner；裁决层不经网关模型工具通道）。
- 不做上下文数据块边界/摘要（M-14 owner——CM-MEMORY 对齐；本任务按其边界输出）。
- 不做 advice 产生/收养判断（判断层 owner——CM-SUPERVISOR；本任务只提供审计点位执行侧）。
- 不定义 Skill catalog 内容（M-16/实施期待办）。
- 模板内容（各会话类型提示词正文）不在本任务定稿——形态冻结（D-02），正文随各会话类型任务实施期填充。

### 1.3 产出物

workspace 子包：ToolGateway（门禁链六工具）、工作区生命周期管理器（卷创建/迭代/checkpoint/清理/恢复）、文件操作账本、checkpoint 执行侧（quiesce+批量校验+幂等）、生成 action 执行侧、Exec 引擎宿主（quickjs 桥）、静态模板库加载端口；core 侧：会话模板资源目录骨架+manifest 机制、模板库类型常量；tests/workspace/（路径门 7 规则用例/双轨越界用例/幂等与恢复窗口用例/Exec 桥防护用例）；模块迭代记录（dev_progress/workspace/）。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | Exec 嵌入式 JS 引擎选型（M-09/M-12 明示实施期选型；用户质疑是否仍需 JS） | quickjs 同步桥 / pythonmonkey / py-mini-racer / 受限 Python / 砍掉 Exec | **保留 Exec=quickjs 同步桥**：经澄清（JS 非 Rust 遗留，而是 LLM 生成代码的零权威沙箱需求——注入威胁路径下构造性安全边界；Exec 编排 L1/L2 索引能力 Shell 不具备）后裁决保留 | 对齐问答 Q-01/Q-05（2026-08-29） |
| D-02 | 静态会话模板库资源形态（§9.1 开放项） | core 静态资源 / DB 存储 / 押后 | **core 静态资源 `core://session-templates/v1`**：每会话类型一模板+manifest sha256，importlib.resources 加载，启动核验+Run 冻结+运行期零变更（与 phase policy JSON 同机制） | 对齐问答 Q-02 |
| D-03 | ReadFile 读视野条件化扩展判定（§9.1 开放项） | verified head 全境读 / 闭包限定读 / 押后 | **verified head 全境读**：修复会话（全局修复+重生升级包）可读根追加「当前 verified head 只读根」；判定=会话类型（零精细闭包计算）；写权限仍限本域/联合域 | 对齐问答 Q-03 |
| D-04 | 沙箱卷物理实现（M-08「Docker volume 模式」字面 vs Docker socket 禁令） | 目录即卷 bind / Docker volume API | **目录即卷 bind**：app 数据目录 run/slice/generation 命名空间独立目录；bwrap --bind 该目录为可写根，宿主 app 直接操作同一路径；零 Docker API 依赖（「Docker volume 模式」仅为共享挂载语义隐喻） | 对齐问答 Q-04 |
| D-05 | Shell 长命令超时设计（用户否决模型自定+后台轮询：多一个模型选择且不好控制） | 统一等待 / 自定+后台化轮询 / 固定 600s / 后台 job 机制 | **统一等待语义（Claude Code 式）**：缺省=上限=600s（可配常数）；模型传 timeout_secs>600 拒绝；超时 facts 引导拆步；**文档偏差登记**（M-00「模型工具 60s 档」/M-12「缺省 60s」→Shell 统一 600s，实施期同步 M-12/M-00 相关表述） | 对齐问答 Q-06/Q-07 |
| D-06 | Git 底层库选型归属 | 归 CM-GIT / 本任务定 | **归 CM-GIT-001**（M-11 owner，紧接本任务对齐）；本任务经端口消费 commit/diff/ref 推进能力 | 对齐问答 Q-08 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（WriteScope/RepoRelativePath/路径规范化函数/phase policy 资源/错误码——CM-CORE）；CM-SANDBOX（bwrap 适配端口/长驻卷隔离/临时物化）；CM-ANALYSIS（PSF-2 查询端口）；CM-PLAN（冻结 write scope 表/checkpoint 幂等键构成）；CM-INFRA（quickjs 依赖登记）。

### 3.2 下游消费

- CM-LOOP/M-04（会话循环调六工具——工具面契约消费方）；CM-VERIFY/M-10（checkpoint 后移交局部验证）；CM-RUNTIME/M-03（Harness 编排调 checkpoint 执行侧）；CM-MEMORY/M-14（数据块边界+模板装配）；CM-OBS/M-13（审计点位事件源）；CM-SUPERVISOR/CM-REPAIR（advice 审计点位+修复会话授权行+联合域写）。

### 3.3 跨模块接口边界

- **M-08/M-12 边界一句话**（文档原文）：M-12 拥有「调用是否被允许」全部判断；M-08 拥有「允许之后发生什么」——本任务是两 owner 的执行面合一交付（workspace 子包内网关与执行侧）。
- **Shell 超时偏差联动**（D-05）：M-00 P-01 注（「模型工具调用 60 秒」默认档）与 M-12 Shell 节（缺省 60s）实施期同步——Shell 工具行改为「统一 600s 等待」；其余工具（ReadFile/QuerySourceAst/Exec 缺省）仍 60s 档。
- **模板库与 CM-LOOP**：会话类型清单（每类型一模板）归 M-04 会话类型定义——CM-LOOP 对齐时收口类型枚举并回填模板清单；本任务只冻结机制与加载端口。
- **advice 审计点位**：advice.proposed（Advice 全量+proposal_hash）/advice.adopted（收养判定+hash 关联回溯）——判断层产生/收养，本任务提供点位执行侧与 run_events 投影。
- **quiesce 联动**：Shell 长命令在 checkpoint 时被终止（统一 600s 上限内命令自然完成为主）；后台残留进程（模型用 `&` 自发后台化）由 quiesce 兜底清理。
- checkpoint 幂等键/恢复窗口语义与 M-00 恢复协议/M-11 CAS 事务对齐（CM-GIT/CM-RUNTIME 联动验收）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M12-V4-001/008 | VERIFY/REPORT 任何工具 TOOL_PHASE_DENIED 零执行；CheckRunner TOOL_NOT_FOUND | 网关单测 |
| V-M12-V4-002 | policy payload 哈希不匹配不 ready；零二次加载 | 启动单测 |
| V-M12-V4-003/004 | 写域外 WRITE_SCOPE_VIOLATION 三零；原子写无半写 | 网关单测+崩溃注入 |
| V-M12-V4-005 | EditFile 零命中/多命中/occur 语义 | 网关单测 |
| V-M12-V4-006 | 可读根外 READ_OUT_OF_SCOPE；ANALYZE/PLAN 拒工作区路径 | 网关单测 |
| V-M12-V4-007 | QuerySourceAst 约束与 M-06 一致；索引应答 | 端口联调（CM-ANALYSIS 契约测试） |
| V-M12-V4-009/018 | Shell 沙箱卷执行宿主零触碰；缓存复用；自检不进 fingerprint | 集成测试（对接 CM-SANDBOX stub） |
| V-M12-V4-010/013 | Shell 越界 checkpoint 拒绝零推进；回退后二次提交通过；结构化越界=事故路径 | checkpoint 单测（双轨用例） |
| V-M12-V4-011 | 路径安全门七规则注入样本全拒 | 安全测试（symlink/遍历/跨根用例） |
| V-M12-V4-012 | tool.call.pre/post 成对+checkpoint.pre+拒绝零原文 | 审计单测 |
| V-M12-V4-014 | 零用户 Hook 入口/六工具外工具名/V3 残留 | 静态扫描 |
| V-M12-V4-015/016/017 | Exec 零宿主 API；逐笔过网关防护不降级；超时/脚本错误可自纠；配额计入 | Exec 桥单测（quickjs） |
| V-M08-V4-001/002 | 一 generation 一工作区；空基线/checkpoint 重建 | 生命周期单测 |
| V-M08-V4-003~005 | 零逐键审查；账本逐次入账零正文；写入只落本工作区 | 迭代期单测 |
| V-M08-V4-006~010 | checkpoint 触发/拒绝/幂等/CAS 窗口恢复 | checkpoint 单测+恢复窗口用例 |
| V-M08-V4-011/012 | 崩溃丢弃重建；清理零残留 | recovery 测试 |
| V-M08-V4-013~016 | 自检不写账本；V3 残留零；卷生命周期与缓存复用；GeneratedCode 零翻译 | 集成测试+静态扫描 |
| V5 增量（M-08/M-12） | 六工具面/PSF 导航/cas 读取/双轨/Exec 桥 | 上述覆盖 |
| D-05 Shell 统一超时 | 缺省 600s 等待；>600 拒绝；超时 facts | 网关单测 |
| D-02 模板库 | manifest sha256 核验；Run 冻结版本；运行期零变更 | 加载单测 |
| V6 增量 | 会话级授权行（协调只读/修复升级读+联合域写）；advice 点位 | 网关单测（授权矩阵用例） |

## 5. 风险与注意点

- **Shell 超时文档偏差**（D-05）：实施期须同步 M-00（P-01 注「模型工具调用 60 秒」）与 M-12 Shell 节（缺省 60s）表述——引用本记录 Q-07；其余工具缺省仍 60s 档不变。
- **quickjs 依赖联动**：进 CM-INFRA pyproject（跨任务协调点）；引擎线程与 asyncio 事件循环的桥接（run_in_executor）在 CM-LOOP 会话循环对齐时细化。
- **模板类型清单依赖 CM-LOOP**：会话类型枚举未定（M-04 owner）——本任务交付机制+占位 manifest；CM-LOOP 对齐后回填类型清单（记录变更行追加）。
- **diff 锚点信任模型**：宿主外部 GIT_DIR 持有候选树 Git 元数据、沙箱侧不可见——具体目录布局与 M-11 对齐（CM-GIT）；本任务锁定「沙箱内无 .git」语义。
- **quiesce 超时数值**：进程不可终止按基础设施事故——超时阈值（建议默认 30s）为可配常数，不臆造契约。
- **`[cm:*]` 分段解析器**：逐段定位错误+回灌纠偏；与 M-04 会话循环 OBSERVATION 语义联动（CM-LOOP 对齐细化回灌格式）。
- 路径安全门规则 5/6（O_NOFOLLOW 逐段/dirfd+st_dev）在 WSL2 文件系统上的行为需实测（符号链接/跨挂载样本）。
- 会话配额（ReadFile 2000 次/Shell 配额）数值归 M-14/M-03 预算档（CM-MEMORY/CM-RUNTIME 对齐）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | Exec 嵌入式 JS 引擎选型 | （用户主动质疑：JS 是否为 Rust 编译遗留、直接 Python 是否更优雅）经澄清零权威沙箱本质后裁决：保留 quickjs 同步桥 |
| Q-02 | 静态会话模板库资源形态 | core 静态资源 `core://session-templates/v1`（phase policy 同机制） |
| Q-03 | ReadFile 读视野条件化扩展判定 | verified head 全境读（修复会话可读根追加 verified 只读根） |
| Q-04 | 沙箱卷物理实现 | 目录即卷 bind（零 Docker API） |
| Q-05 | （用户质疑回合）JS 引擎是否必要 | 澄清：JS 选择与 Rust 无关——①LLM 生成代码零权威沙箱（注入威胁下构造性边界 vs Python AST 沙箱不可靠）②Exec 编排 L1/L2 索引能力 Shell 不具备（一次模型调用 N 步省 token） |
| Q-06 | Shell 长命令超时设计（首轮：自定+后台化轮询方案） | 否决：长命令模式多一个模型选择且不好控制，要统一 |
| Q-07 | Shell 超时最终设计 | 统一等待语义（Claude Code 式）：缺省=上限=600s 可配；传值超限拒绝；超时 facts 引导拆步 |
| Q-08 | Git 底层库选型归属 | 归 CM-GIT-001（M-11 owner）；本任务经端口消费 |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-08/M-12 设计文档与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-06（含 Exec JS 必要性澄清与 Shell 超时两轮收敛）；§9.1「静态会话模板库资源形态」「读视野条件化扩展」经 D-02/D-03 收口；Shell 统一超时为登记的文档偏差 |
