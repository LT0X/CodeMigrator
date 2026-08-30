# CM-SANDBOX-001-内联 bwrap 沙箱适配详细设计

> 本设计依据 M-09《沙箱与执行环境》与 `my_space/code_alignment_record/sandbox/CM-SANDBOX-001-对齐记录.md`。`codemigrator.sandbox` 是 app 内的受信执行适配层：不拥有 Run/Slice 状态、不接纳 CheckResult、不连接 PG/Git，不将命令控制权交给模型。

## 0. 边界与接口

- **负责**：冻结检查命令实例化、bwrap 参数构造、启动预检、PDEATHSIG/cgroup/进程组治理、验证临时目录、资源与输出边界、三池容量公式、终止事实归约、Shell 受控代理。
- **不负责**：命令模板来源与 Run 冻结、tested commit 的 Git 物化、active-attempt gate、CheckResult/fingerprint/状态推进、候选工作区/checkpoint、工具 frame、compose/deploy 物理落盘。
- **输入信任分层**：裁决通道只接受上游已冻结的 `CheckCommandTemplate`、镜像 digest、物化目录和策略摘要；Shell 通道接受 M-12 的自由命令，但不复用裁决通道的命令对象。

## 1. 冻结命令与 bwrap argv

`FrozenCommand` 从 `CheckCommandTemplate` 实例化，要求调用方提供已核验的模板摘要与镜像摘要，并拒绝 program/argv/timeout/env 的额外注入。`BwrapPolicy` 仅允许受信侧构造挂载和环境；`ShellCommand` 是独立的自由反馈命令类型，不可转换为裁决命令：

1. 固定 bwrap executable；
2. `--unshare-all`；
3. `--new-session`、`--die-with-parent`；
4. `--clearenv`、`--cap-drop ALL`；
5. 工具链 rootfs `--ro-bind`；
6. 最小 `/dev`、受控 `/proc`、`--tmpfs /tmp`；
7. 唯一 validation temp directory bind；
8. 依赖 cache 只读 bind；
9. seccomp policy fd/路径摘要核验；
10. 白名单环境和冻结 argv，绝不经 shell 拼接。

路径统一校验为 repository-independent 的安全绝对路径：禁止 NUL、相对段、根目录、控制 socket 与覆盖 sandbox-managed target 的未声明挂载；环境键只允许 `PATH`、`HOME`、`LANG` 及显式代理键。裁决档网络默认为 deny；Shell 档仅由策略显式开启受控出口并注入代理变量。

## 2. 启动、生命周期与执行事实

`Preflight` 只读核验 kernel、cgroup v2、bwrap 版本、user namespace、架构、磁盘和镜像/seccomp 摘要；任一不符返回拒绝事实，不启动 cgroup、不创建验证目录。启动前使用 `prctl(PR_SET_PDEATHSIG, SIGKILL)`，并可将 bwrap 进程组放入显式委派的专属 cgroup；app 退出时内核回收，清理窗口为 5 秒。未提供委派 cgroup 时不伪造 cgroup receipt，部署组合由 CM-INFRA 交接。

每次裁决执行创建独立临时验证目录，源内容由上游物化；执行完成、超时、取消或基础设施失败均按“先清空进程组、再移除目录”收口，构建输出不回流 tested commit。长驻 Slice 卷只由 M-08 管理，本层只提供执行绑定描述。

## 3. 资源、池与终止归约

裁决实例固定 4 GiB memory、2 CPU、10 GiB writable disk；stdout/stderr 每流 256 MiB，验证目录单文件 64 MiB，模板 timeout 是唯一超时来源。`ResourceLimits` 保存这些上限，不复制 core 的 timeout 契约；cgroup/filesystem quota receipt 由执行部署接入。执行池容量为：

`max(1, min(4, floor(host_memory_gib / 4), floor(host_cpu_cores / 2)))`。

池位只约束活跃 bwrap，不占用 Slice 会话；获取和归还必须成对，取消/异常也通过上下文管理器归还。非取消终止按 `output limit → timeout → infrastructure failure → seccomp denial → process exit` 选择唯一主事实；seccomp denial 即使 exit 0 仍 Failed，只有无更高优先级事实且 exit 0 才可 Passed。外部取消只生成 `TerminationReceipt`，不伪造验证结果。

## 4. Shell 受控网络档

Shell 命令直接在 Slice 长驻卷执行，不进入冻结检查命令和 active-attempt gate。需要外联时只允许 AF_INET 经 veth 到 app 内 asyncio forward proxy；代理以声明式域白名单拒绝未知目标，拒绝结果只保留脱敏连接审计摘要。裁决与 Scaffold 档仍使用 default-deny seccomp；代理配置只注入 `HTTP_PROXY`/`HTTPS_PROXY`，禁止把宿主凭据和控制面 socket 带入沙箱。

## 5. 测试与交接

规则测试锁定 argv 顺序、路径/环境拒绝、摘要 mismatch fail-closed、预检零副作用、资源归约、池容量/归还、临时目录销毁、Shell/裁决命令分离、代理白名单/转发和安全挂载表。真实 bwrap/cgroup/代理部署由运行环境和 CM-INFRA 联动验证；本任务不改 `compose.yaml` 与 `deploy/`。
