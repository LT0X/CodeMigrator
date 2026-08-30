# CM-SANDBOX-001-内联 bwrap 沙箱适配实施计划

> 本计划依据 M-09《沙箱与执行环境》与 `my_space/code_alignment_record/sandbox/CM-SANDBOX-001-对齐记录.md`。本任务只实现 app 内执行底座；不改根目录 compose/deploy 基础设施文件，不实现 Run/Slice 状态裁决、Git/checkpoint、工具协议或数据库。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-SANDBOX-001
- **所属模块/crate**：M-09 / `codemigrator.sandbox`
- **关联交付物**：详细设计 `my_space/codemigrator_design_doc/detailed_coding_design/sandbox/CM-SANDBOX-001-内联bwrap沙箱适配详细设计.md`、迭代记录 `my_space/codemigrator_dev_progress/sandbox/CM-SANDBOX-001-内联bwrap沙箱适配迭代记录.md`

## 1. 目标与范围

- **完成定义（DoD）**：提供不可注入的冻结命令实例化、默认拒绝网络/受控 Shell 网络档 bwrap argv 构造、启动预检、PDEATHSIG/cgroup/进程组治理抽象、4 GiB/2 CPU/10 GiB/256 MiB/64 MiB 资源策略、三池容量公式、验证临时目录生命周期、终止事实唯一归约与 asyncio 代理白名单端口；规则测试、安全 argv 快照测试、全量测试、Ruff、mypy、import-linter、compileall 通过。
- **范围排除项**：不修改 `compose.yaml`、`deploy/`、`migrations/`；不实现 PostgreSQL/Git/Run actor/CheckResult 接纳/verification fingerprint；不提供 M-12 Shell/Exec 工具 frame；不接入真实工具链镜像或真实代理部署。

## 2. 前置条件

- [x] 工作分支：从已合并 `develop` 切出 `feature/sandbox-bwrap`。
- [x] 环境就绪项：复用 `/home/xtc/env/codemigrator-infra/` 的 Python 3.12/uv 锁定环境；bwrap/cgroup 实机能力通过可注入预检与规则测试覆盖。
- [x] 上游依赖：CM-CORE-001、CM-INFRA-001、CM-SPEC-001、CM-ANALYSIS-001 已合并至 `develop`。
- [x] 设计文档：已完整阅读 M-09 架构设计与 CM-SANDBOX-001 对齐记录。

## 3. 实施步骤

1. - [x] 先写失败测试：冻结命令、镜像摘要、路径/环境/argv 注入拒绝与十类 bwrap 参数快照（源码与测试 → V-M09-V5-002/003/010）。
2. - [x] 实现命令实例化与 argv builder：固定可执行入口、`--unshare-all`、`--new-session`、`--die-with-parent`、`--clearenv`、`--cap-drop ALL`、只读 rootfs/cache、最小 `/dev`/受控 `/proc`、tmpfs `/tmp`、seccomp 与白名单环境；Shell 自由命令使用独立 `ShellCommand`（`command.py` → V-M09-V5-002/010/017）。
3. - [x] 先写失败测试并实现启动预检、PDEATHSIG/cgroup/进程组治理和临时验证目录 owned lifecycle（`preflight.py`、`lifecycle.py` → V-M09-V5-003/005/007/008）。
4. - [x] 先写失败测试并实现资源策略、三池公式、输出上限与唯一终止事实归约；超时仍只取冻结模板字段，文件上限作为配额策略字段保留给挂载/配额实现（`limits.py`、`termination.py`、`pool.py` → V-M09-V5-006/011/012/013）。
5. - [x] 先写失败测试并实现 app 直接执行适配：固定 argv 通过 subprocess、PDEATHSIG pre-exec 注入、输出流上限、超时/取消清理、可选 cgroup 和结构化执行事实（`executor.py` → V-M09-V5-004/006/014/016）。
6. - [x] 先写失败测试并实现 asyncio 前向代理的域白名单解析/连接拒绝/审计摘要及 Shell 网络档配置（`proxy.py` → V-M09-V5-017）。
7. - [x] 更新四份交付文档与主任务表，记录 compose/deploy 联动留给 CM-INFRA；完成质量门与 PR 说明（`my_space/`、`tests/sandbox/`、`tests/security/` → V-M09-V5-001～017）。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| Sandbox/security 规则测试 | `uv run --frozen pytest tests/sandbox tests/security -q` | 全部通过；argv、路径、网络、资源、清理与池公式均有确定性断言 | V-M09-V5-001～017 |
| 全量测试 | `uv run --frozen pytest -q` | 零失败 | M-00/M-09 |
| 静态质量 | `uv run --frozen ruff check src tests`、`uv run --frozen mypy src/codemigrator` | 零错误 | 工程质量门 |
| 架构边界 | `uv run --frozen lint-imports` | 3 contracts kept，0 broken | M-01 |
| 编译/差异 | `uv run --frozen python -m compileall -q src tests`、`git diff --check` | 零错误、无空白问题 | AGENTS.md §4 |
| 实机能力 | `bwrap --version`、`stat -fc %T /sys/fs/cgroup`（只读探测） | 仅作为环境证据；能力不足由 Preflight 拒绝，不降级 | V-M09-V5-003/005/010/017 |

- 不执行真实模型、Docker、PG 或真实外联代理测试；真实 bwrap 执行通过显式注入的 runner/规则测试保持可复现，宿主能力验证仅做只读探测。

## 5. 风险与回滚

- 风险点及缓解：WSL2/cgroup 委派和 bwrap-in-Docker 能力可能随宿主变化；所有执行在能力预检失败时 fail-closed，禁止弱隔离降级；代理只允许声明域并保留拒绝审计摘要；命令对象不接受模型自由字段。
- 回滚方式：仅在 `feature/sandbox-bwrap` 使用 `git revert`；不重置 `develop`，不删除主工作区用户修改。

## 6. 收尾清单

- [x] 模块迭代记录已按模板生成于 `codemigrator_dev_progress/sandbox/`
- [x] 主任务表 `CodeMigrator开发任务规划与进度跟踪.md` 已更新
- [x] 详细设计文档已保存于 `detailed_coding_design/sandbox/`
- [x] 本实施计划已保存于 `Implementation_plan_doc/sandbox/`
- [x] 若设计与架构文档有差异：本任务不修改架构模块设计；compose/deploy 联动事项已明确交接 CM-INFRA
