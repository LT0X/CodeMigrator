# feat: implement candidate workspace and tool gateway

- **任务**：CM-WORKSPACE-001
- **模块**：M-08 + M-12 / `codemigrator.workspace`
- **分支**：`feature/workspace-gateway`
- **迭代记录**：`my_space/codemigrator_dev_progress/workspace/CM-WORKSPACE-001-候选工作区与工具网关迭代记录.md`
- **设计依据**：M-08《候选工作区与工具网关》、M-12《工具系统与Hook》及 `code_alignment_record/workspace/CM-WORKSPACE-001-对齐记录.md`

## 背景

交付候选工作区即长期沙箱卷的执行侧，以及六工具 closed-schema 网关。实现遵循 M-08/M-12 的门禁次序、结构化写入即时 scope 校验、Shell checkpoint 批量校验、Exec 工具桥回网关、CAS non-force 推进和审计脱敏要求，对应 V-M08/V-M12 的可施工验收条款。

## 变更点

- 新增 workspace 模型、dirfd/no-follow 路径原语、原子文件写入和安全边界检查。
- 新增 `ToolGateway`：core policy 单次加载、phase/session 授权、ReadFile/CAS、WriteFile/EditFile、QuerySourceAst、Shell、Exec 端口及审计摘要。
- 新增候选工作区生命周期、结构化写账本 sink、checkpoint diff/scope 双轨、幂等 receipt、CAS 冲突和 pending commit 恢复。
- 新增 `[cm:action]` 多段动作协议、严格 JSON 回退、受限 Exec bridge 和 GeneratedCode scaffold action。
- 新增 workspace/security 规则测试；同步实施计划、详细设计、迭代记录、主任务表和 workspace README。
- 根据 PR #8 唯一审查报告一次性补齐：闭合 M-06 查询 schema/错误码透传、policy 摘要冻结、symlink 双路径 scope、pending 内容复核、CAS 分叉冻结、intent/receipt 与工作区重启恢复、幂等清理、FIFO 非阻塞、Shell 默认超时、行级 marker 和 UTF-8 字节限制；checkpoint port 接收受信不可变文件清单。
- **BREAKING**：无；不修改 `codemigrator.core` 公共契约、数据库 schema、API 或架构模块设计文档。

## 自测证据

- `PYTHONPATH=src /home/xtc/env/codemigrator-plan/bin/pytest -q tests/workspace tests/security/test_workspace_gateway.py`：48 passed。
- `PYTHONPATH=src /home/xtc/env/codemigrator-plan/bin/pytest -q`：285 passed。
- `ruff check src tests`：通过。
- `mypy src`：61 个源文件无问题。
- `lint-imports --config pyproject.toml`：3 contracts kept、0 broken。
- `python -m compileall -q src tests`、`git diff --check`：通过。
- 覆盖：`V-M08-V4-001~016`、`V-M12-V4-001~007/009~018` 的确定性路径/授权/协议/恢复/端口替身测试；真实 bwrap、Git CAS、QuickJS、PostgreSQL 联调由对应 owner 后续接入，未将替身测试冒充真实联调。

## 风险与回滚

- 真实 bwrap/quiesce、Git ref/CAS、PSF、QuickJS 与 PostgreSQL adapter 尚未在本任务接入；Protocol 边界已冻结，后续 owner 可替换内存替身。
- Shell 非零退出仍作为正常反馈；越界写在 checkpoint 拒绝且不推进 candidate/verified。
- 若合并后发现回归，使用 GitHub revert 本 PR；不 force-push 主干。

## 审查流程

- PR #8 仅启动 Galileo 一次审查，并完整等待最终结论。
- 审查反馈在原分支一次性修复、重新验证并提交；不再对同一 PR 启动第二次审查，随后直接合并。
