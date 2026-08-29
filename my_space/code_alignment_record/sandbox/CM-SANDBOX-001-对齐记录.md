# CM-SANDBOX-001 对齐记录

> 用途：本文件是任务 `CM-SANDBOX-001`（模块 M-09 沙箱与执行环境）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-SANDBOX-001` |
| 模块编号 | M-09 |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_沙箱与执行环境.md`（V5 方向对齐版；V4 UDS/worker/overlay 体系已退役） |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 1 轮（Wave 1 轮） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐（含 V5 验收基线适配清单，D-01） |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `src/codemigrator/sandbox/` 子包——app 内 bwrap 执行适配（无独立服务 entry point）：

- **bwrap 执行适配**：subprocess 直接调 bubblewrap（`--unshare-all`/`--new-session`/`--die-with-parent`/`--clearenv`/`--cap-drop ALL`/工具链镜像 rootfs ro-bind/最小 `/dev`/受控 `/proc`/tmpfs `/tmp`/临时验证目录 bind/依赖 cache ro-bind/seccomp BPF/白名单环境+固定 argv——M-09 参数边界 10 条）；`PR_SET_PDEATHSIG` + 专属 cgroup 域进程组治理；app 崩溃→内核回收，5 秒内未清空→Run 基础设施失败。
- **三个执行面**：① 裁决层检查（tested commit 临时物化目录，default-deny 零网络，用毕销毁，active-attempt gate 接纳）；② Shell/Exec 反馈（Slice 长驻沙箱卷，差异化 seccomp 网络档+veth 受控出口）；③ Scaffold（输出基线初始化，受信提取产物，禁控制面）。
- **命令实例化**：冻结 `CheckCommandTemplate` → `program+argv`（canonical 顺序固定，模型零输入，无 shell 拼接）；执行前核验模板摘要与镜像 digest。
- **长驻沙箱卷**：工作区即卷（M-08 共享挂载）；生命周期与 Slice 对齐（创建建卷/集成或废弃销毁）；构建缓存与依赖跨命令驻留。
- **网络两档**：裁决/Scaffold 零网络；Shell 档放行 `AF_INET socket()/connect()`，netns 唯一出口=宿主白名单代理（veth 对端），允许域由目标端描述符声明——**app 内 Python asyncio 前向代理子进程**（D-03）。
- **资源边界**：单次检查沙箱 4 GiB/2 CPU/10 GiB；stdout/stderr 各 256 MiB（第 256MiB+1byte 终止进程组）；验证目录单文件 64 MiB；超时全走模板 `timeout_secs`。
- **三池公式**：沙箱执行池=`max(1, min(4, floor(host_memory_gib/4), floor(host_cpu_cores/2)))` 约束同时活跃 bwrap 位，按需取用用毕归还（卷与缓存保留）；Slice 会话不占执行位。
- **终止归约**：非取消终止按 `output limit → timeout → infrastructure failure → seccomp denial → process exit` 唯一主事实；seccomp denial 伴随 exit 0 仍 `Failed`；外部取消只产生 TerminationReceipt。Shell 通道不经此归约（退出码=模型反馈）。
- **启动预检**：内核 ≥5.15/cgroup v2/bubblewrap ≥0.8/userns/磁盘/架构/镜像摘要，任一不符拒绝执行不降级。
- **compose 容器能力**：最小权限组合（D-02）。

### 1.2 边界（不做什么）

- 不做 Run/Slice 状态归约、重派决策、接纳决定（归 CM-RUNTIME actor；本任务回显执行事实）。
- 不做验证语义/fingerprint/归因（M-10 归 CM-VERIFY-001）。
- 不做六工具协议面/frame/hook（M-12 归 CM-WORKSPACE-001；本任务只提供执行底座）。
- 不做候选工作区生命周期/Git/checkpoint（M-08/M-11 归 CM-WORKSPACE/CM-GIT；本任务提供卷与执行隔离）。
- 不连 PostgreSQL、不持数据库凭据（M-01 sandbox 禁止事项）；不拥有命令面外裁决。
- 不实现 Exec 嵌入式 JS 引擎（app 进程内、M-12 联动归 CM-WORKSPACE；选型 quickjs/py-mini-racer 为实施期细化项）。
- 长驻卷内容管理（工作区文件）归 M-08；本任务管卷的隔离与进程治理。

### 1.3 产出物

`src/codemigrator/sandbox/`（bwrap 适配器/cgroup 治理/三池执行池/网络两档与代理子进程/临时物化与销毁/启动预检/终止归约）、compose 能力配置语义（D-02，落盘归 CM-INFRA compose.yaml 联动）、deploy/ seccomp policy 与 rootfs 配置（与 CM-INFRA 协同）、tests/sandbox/ + tests/security/（隔离/路径/网络/回收）、模块迭代记录。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | M-09 V5 验收基线（主表标注待补；V4 条款块标退役） | 本对齐完成适配清单 / 等文档补齐 / 照搬 V4 | **本对齐完成适配清单**：V4-001~020 剔除 UDS/worker/overlay 已退役条款，改写为 app 直管 bwrap 语义（见 §4 V-M09-V5-001~017）；本清单即验收口径，实施期按 §8 流程同步回填 M-09 文档 | 对齐问答 Q-01（2026-08-29） |
| D-02 | app 容器沙箱能力配置（Docker 默认 seccomp 阻断 CLONE_NEWUSER） | 最小权限组合 / privileged / 宿主直跑 | **最小权限组合**：`security-opt seccomp=unconfined + cap-add SYS_ADMIN + cgroup v2 委派目录挂载`（compose.yaml 由 CM-INFRA 落盘，语义本任务定义） | 对齐问答 Q-02 |
| D-03 | Shell 受控网络出口代理形态（M-09 标注实施期细化项） | app 内 Python 代理 / 独立 squid / 押后 | **app 内 Python asyncio 前向代理子进程**（veth 宿主端、域名白名单由目标端描述符注入、连接审计）——符合 app+PG 两服务拓扑 | 对齐问答 Q-03 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- core 公共契约（CheckCommandTemplate/CheckAction/CheckStatus/ExecutionSubject/DispatchAttemptId/CandidateGeneration/Sha256/GitOid）；CM-SPEC registry 端口（镜像 digest/模板摘要核验）；CM-INFRA（compose 能力配置 D-02、deploy seccomp policy、target-python 工具链镜像）。

### 3.2 下游消费

- CM-RUNTIME（actor 派发接口、执行回执、PDEATHSIG 回收、三池调度）；CM-VERIFY/M-10（冻结检查执行底座）；CM-WORKSPACE/M-08（长驻卷底座、Shell/Exec 执行）；CM-OBS（执行指标——M-13 归属）。

### 3.3 跨模块接口边界

- **执行回执契约**：本任务回显 DispatchAttemptId + CheckSubject + tested_commit_oid + 执行事实（stdout/stderr ArtifactRef、CheckStatus、终止主事实）；接纳决定永远在 Run actor（active-attempt gate 语义归 M-00/M-03）。
- **两通道平行**：裁决通道（冻结模板实例化+gate+临时目录）与 Shell 通道（自由命令+长驻卷+无 gate）无交集；Scaffold 仅 Harness 触发。
- **临时物化目录来源**：`tested_commit_oid` Git tree 物化（物化操作归 M-08/M-11 协同）；本任务管目录的挂载/隔离/销毁。
- **compose 联动**：D-02 能力配置写入 compose.yaml（CM-INFRA 落盘时引用本决策）。
- 空闲治理阈值（非活跃态终止进程组归还池位、保留卷）为实施期参数——待定，不臆造数值。

## 4. 验收条款映射（V5 适配清单，D-01）

> V4-001/002/003（UDS 方法面/握手/目录）随 UDS 体系整体退役，无对应条款。

| V5 条款 | 来源 | 内容摘要 | 验证方式 |
|---|---|---|---|
| V-M09-V5-001 | V4-004 改写 | bwrap 内检查进程可达 PostgreSQL/Git 控制面/socket 数为 0（app 直管，无 worker） | security 测试（挂载表+网络探测断言） |
| V-M09-V5-002 | V4-005 改写 | app 实例化的 program/argv/timeout/template_sha256 与冻结模板逐字节一致；模型注入命令字段时执行派发数为 0 | 单测（实例化冻结断言） |
| V-M09-V5-003 | V4-006 改写 | 镜像 digest 与 Run 冻结值不匹配时执行拒绝且零 cgroup、零临时目录创建 | 单测 |
| V-M09-V5-004 | V4-007 改写 | 旧 attempt/跨 subject/错 check/旧 tested OID/cancel 后返回不产生 CheckResult，只追加丢弃审计（STALE_DISPATCH_RESULT） | 契约测试（迟到结果注入） |
| V-M09-V5-005 | V4-008 改写 | app 崩溃后 PDEATHSIG/cgroup 使 bwrap 进程组 5 秒内清空；清理失败 Run 进基础设施失败 | recovery 测试（kill app 进程） |
| V-M09-V5-006 | V4-009 改写 | 物理重派只换 DispatchAttemptId 不增 generation；在途 Shell 以 InfrastructureError 返回模型上下文不写验证账本；卷与缓存保留 | recovery 测试 |
| V-M09-V5-007 | V4-010 改写 | 临时验证目录在完成/超时/取消后销毁；tested commit 不吸收构建输出 | 单测（目录生命周期断言） |
| V-M09-V5-008 | V4-011 改写 | 候选工作区/integration scratch/verified 不挂载给验证进程（长驻卷 Shell 共享挂载除外，写效果 checkpoint 兜底） | security 测试（挂载表断言） |
| V-M09-V5-009 | V4-012 改写 | Scaffold 仅输出基线初始化（Harness 触发），产物受信提取应用；Agent 无可达 Scaffold 路径 | 单测+静态扫描 |
| V-M09-V5-010 | V4-013 保留 | bwrap 参数 10 条逐项包含（unshare-all/new-session/die-with-parent/clearenv/cap-drop ALL/ro rootfs/最小 dev/受控 proc/seccomp）；Docker socket/SSH agent/宿主凭据不在挂载表 | security 测试（argv 快照比对） |
| V-M09-V5-011 | V4-014 保留 | 4 GiB/2 CPU/10 GiB/三池公式活跃位；按需取用用毕归还；会话不占执行位 | 单测（池行为+公式断言） |
| V-M09-V5-012 | V4-015 保留 | 256 MiB/64 MiB/超时触发进程组清空且 CheckStatus 不可能 Passed | 单测 |
| V-M09-V5-013 | V4-016 保留 | seccomp denial+exit 0 仍 Failed；终止归约唯一主事实优先序 | 单测 |
| V-M09-V5-014 | V4-017 改写 | 派发消费者恰两项（内部验证+Scaffold）；模型工具触发的裁决派发数为 0；两键空间分离每键一 active attempt | 契约测试 |
| V-M09-V5-015 | V4-018 保留 | 长驻卷生命周期与 Slice 对齐；缓存/依赖跨命令驻留（重复构建零冷启动） | 单测 |
| V-M09-V5-016 | V4-019 保留 | Shell 自检自由执行不进 fingerprint/不产 CheckResult/不推进状态；checkpoint 后冻结检查集独立派发 | 契约测试 |
| V-M09-V5-017 | V4-020 保留 | 裁决/Scaffold 零网络（syscall 全拒、cache miss 不联网）；Shell 档 AF_INET 放行且唯一出口 veth 宿主代理（D-03），直连路由数 0、代理外目标连接数 0、HTTP(S)_PROXY 注入指向代理 | security 测试（网络分档断言） |
| V5 增量 | 文档 | app 直接调 bwrap+PDEATHSIG/cgroup/命名空间/受控代理/default-deny 验证档 | 上述覆盖 |

## 5. 风险与注意点

- **文档同步义务（D-01）**：V5 适配清单须在实施期（代码落盘后）按 AGENTS.md §3.4 经用户确认回填 M-09（替换已退役 V4 条款块；引用本记录）。
- **WSL2 内 bwrap-in-Docker 实测风险**：Docker CE（WSL2 内）+ seccomp=unconfined + userns 组合需真实冒烟验证（V-M09-V5-010 前置）；失败则回 D-02 备选方案（需重新对齐）。
- **cgroup v2 委派**：compose 需挂载委派的 cgroup 子树且 app 容器有写权限；systemd 在 WSL2 的 delegate 配置需实测。
- **代理白名单配置**：允许域由目标端描述符声明（Go 首对目标端为 python——描述符需含允许域字段；CM-INFRA 首对描述符联动）；代理连接审计入 M-13 观测（脱敏）。
- **空闲治理阈值待定**：非活跃态终止进程组归还池位的阈值（实施期参数）不臆造，届时对齐。
- **Exec 引擎选型联动**：quickjs/py-mini-racer 与资源基准归 CM-WORKSPACE-001（M-12 联动），本任务只保「不占沙箱位」约束。
- seccomp BPF 策略文件（deploy/）与加载前摘要核验——与 CM-INFRA deploy/ 协同落盘。
- 主表 §9.1「M-09 V6 验收基线补齐」开放项由本对齐 D-01 关闭（记录为已对齐结论）。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | M-09 V5 验收基线建立方式（主表标注待补） | 本对齐完成适配清单（V4 剔退役改写为 app 直管 bwrap 语义；实施期回填文档） |
| Q-02 | app 容器沙箱能力配置（Docker 默认 seccomp 阻断 CLONE_NEWUSER） | 最小权限组合（seccomp=unconfined + SYS_ADMIN + cgroup v2 委派） |
| Q-03 | Shell 受控网络出口代理形态 | app 内 Python asyncio 前向代理子进程（域白名单描述符注入） |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-09 V5 方向对齐版设计文档（V5 执行面不变量/bubblewrap 参数边界/网络出口/资源边界节）与主任务表 §7.3；用户经提问工具逐项确认 D-01~D-03；V5 验收基线适配清单（V-M09-V5-001~017）经用户确认成为验收口径 |
