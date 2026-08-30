# feat: implement inline bwrap sandbox adapter

- 任务：`CM-SANDBOX-001`
- 模块：M-09 `codemigrator.sandbox`
- 分支：`feature/sandbox-bwrap`
- 迭代记录：`my_space/codemigrator_dev_progress/sandbox/CM-SANDBOX-001-内联bwrap沙箱适配迭代记录.md`

## 背景

依据 M-09 V5 执行面不变量、bubblewrap 参数边界、资源边界与网络分档，以及对齐记录 D-01～D-03 和 V-M09-V5-001～017，交付 app 内直接管理 bwrap 的执行底座。模块不拥有 Run/Slice 状态、CheckResult 接纳、Git/PG、compose/deploy 或工具协议。

## 变更点

- `src/codemigrator/sandbox/`：新增冻结 `CheckCommandTemplate` 的 `FrozenCommand`、独立 Shell `ShellCommand`、固定 bwrap argv、路径/环境/挂载安全门、镜像/模板摘要 gate、启动预检、PDEATHSIG、必需委派 cgroup、进程组回收、临时验证目录、固定资源策略、三池执行位、终止事实归约、执行回执和 asyncio 前向代理。
- `tests/sandbox/`、`tests/security/`：新增 argv 快照、摘要 mismatch 零启动、路径/环境、预检、生命周期、池公式、输出/终止、Shell/裁决分离、代理白名单/转发和审计测试。
- `my_space/`：新增实施计划、详细设计、迭代记录与本 PR 说明；按 D-01 回填 M-09 V5 验收矩阵；主任务表同步 CM-SANDBOX-001 状态。
- 公共契约：复用 `codemigrator.core` 的 `CheckAction`、`CheckStatus`、`CheckCommandTemplate`；不修改 M-00 公共定义，不新增运行时状态或错误码。
- BREAKING：无。Shell 自由命令使用独立类型，禁止把自由命令当作裁决检查命令；真实 cgroup 委派、seccomp policy、Compose 能力与工具链镜像由 CM-INFRA 联动。

## 自测证据

- `uv run --offline --frozen --group dev pytest -q` → `171 passed`
- `uv run --offline --frozen --group dev pytest -q tests/sandbox tests/security` → `20 passed`
- `uv run --offline --frozen --group dev ruff check src tests` → passed
- `uv run --offline --frozen --group dev mypy src/codemigrator` → passed
- `uv run --offline --frozen --group dev lint-imports` → `3 contracts kept, 0 broken`
- `uv run --offline --frozen --group dev python -m compileall -q src tests` → passed
- `git diff --check` → passed
- Linux 能力只读探测：bubblewrap `0.9.0`、cgroup v2；未执行依赖真实工具链镜像、Docker/Compose 权限委派或真实外联代理的集成冒烟。
- 对照 V-M09-V5-001～017：本 PR 锁定执行适配层的 argv、摘要、预检、目录、资源、池、终止和代理规则；active-attempt gate、Run/Slice 状态、tested-commit 物化、长期卷生命周期、Scaffold 受信提取、seccomp/deploy 与 Compose 物理联动由其拥有模块接入。
- 唯一审查：PR #5 的审查 agent 已等待至终态并返回 `REQUEST_CHANGES`；反馈已在原分支一次性修复，未启动第二次审查。
- 真实模型测试：不需要，规则/契约测试已覆盖本任务确定性行为。

## 风险与回滚

- WSL2/Docker 环境的 user namespace、cgroup 委派、seccomp 加载、veth 与工具链镜像需要部署联调；能力不足时通过 preflight fail-closed，不降级为弱隔离。
- 执行器只负责事实回执；适配层重算并核验模板摘要，调用方必须提供已核验的镜像摘要和委派 cgroup，不能据此直接接纳验证结果。
- 如需回滚，使用本分支提交的 `git revert`；不直接改写 `develop`，也不覆盖主工作区已有修改。
