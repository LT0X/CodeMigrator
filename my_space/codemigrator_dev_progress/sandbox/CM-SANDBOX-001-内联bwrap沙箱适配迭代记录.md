# CM-SANDBOX-001-内联 bwrap 沙箱适配_迭代记录

> 本记录按 `CodeMigrator迭代记录模板.md` 维护，记录 M-09 app 内 sandbox 执行底座实现与验证。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-SANDBOX-001
- **关联模块/文档**：M-09、`feature/sandbox-bwrap`、`my_space/code_alignment_record/sandbox/CM-SANDBOX-001-对齐记录.md`

## 1. 变更动机

CM-ANALYSIS-001 已合并。下一项为 M-09 app 内 bwrap 执行适配，必须把不可信目标代码的命令、挂载、网络、资源和回收边界固化为可测试的纯 Python 接口，供后续 Runtime/Verify/Workspace 组合。

## 2. 变更内容

- 已建立 `feature/sandbox-bwrap` 隔离分支、实施计划和详细设计。
- 按 D-01 使用 V5 验收条款；compose/deploy 根目录变更明确交接 CM-INFRA，本任务不直接修改。
- 按 TDD 红-绿循环实现 `command.py`、`preflight.py`、`lifecycle.py`、`limits.py`、`pool.py`、`termination.py`、`executor.py` 与 `proxy.py`；新增 sandbox/security 确定性规则测试。
- `FrozenCommand` 与 `ShellCommand` 分离；bwrap argv 固定 namespace、rootfs/cache 挂载、临时验证目录、seccomp、环境与 argv 顺序；摘要 mismatch 在 cgroup/子进程创建前 fail-closed。
- 执行回执覆盖输出流上限、模板超时、进程组回收、取消终止回执与可选委派 cgroup；Shell 代理支持严格域/子域白名单、连接审计和 HTTP 转发。

## 3. 自测与验证结果

- 当前阶段：代码与测试完成，进入最终质量门、PR 唯一审查和直接合并流程。
- 基线：全量 pytest `151 passed`。
- Sandbox/security：`16 passed`；其中含本地 asyncio 代理转发回归。
- 真实模型测试：不需要；sandbox 规则和安全测试优先。

## 4. 影响面与风险

- 仅影响 `codemigrator.sandbox`、对应测试、`my_space/` 交付文档；不触碰 PG、Git、compose/deploy 或用户主工作区修改。
- WSL2/cgroup/bwrap 能力差异通过预检 fail-closed 暴露，不提供弱隔离降级。

## 5. 后续行动

- [x] 先写并确认冻结命令、argv 安全边界与预检的失败测试。
- [x] 实现执行生命周期、资源池、终止归约和 Shell 代理端口。
- [ ] 完成四份文档、质量门、PR、唯一一次审查、反馈一次修复、直接合并与主工作区拉取。

## 6. 附录

- 上游 develop 合并基线：CM-ANALYSIS-001 PR #4，`360a1e8`。
