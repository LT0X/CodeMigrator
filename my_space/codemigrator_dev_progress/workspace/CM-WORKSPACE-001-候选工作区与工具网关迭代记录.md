# CM-WORKSPACE-001-候选工作区与工具网关_迭代记录

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-WORKSPACE-001
- **关联模块/文档**：M-08 + M-12；`my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_候选工作区与工具网关.md`；`CodeMigrator_工具系统与Hook.md`

## 1. 变更动机

依据 `CM-WORKSPACE-001-对齐记录.md` 和 M-08/M-12 当前 V6 设计，补齐候选工作区作为长期沙箱卷的执行侧、六工具 closed-schema 网关、结构化写账本、checkpoint 双轨 scope 校验与恢复窗口。实现必须保持结构化工具越界即时拒绝、Shell 越界提交期裁决、Exec 底层调用逐笔回网关及 VERIFY/REPORT 零模型工具面。

## 2. 变更内容

- 新增 `src/codemigrator/workspace/` 执行面：
  - `models.py`：六工具判别联合、严格输入/输出、稳定错误、审计事件、工作区句柄和结构化写账本。
  - `paths.py`：根 dirfd、POSIX 相对路径门、逐段 `O_NOFOLLOW`、设备边界、同目录临时文件 + `fsync` + 原子替换。
  - `gateway.py`：core policy 单次加载、phase/session 授权、ReadFile/CAS、WriteFile/EditFile、QuerySourceAst/Shell/Exec 端口、脱敏审计及账本 sink。
  - `protocol.py`：严格 JSON/`[cm:action]` 多段解析、重复键/非有限数字拒绝、CAS/查询/Shell/Exec 端口及受限工具桥。
  - `lifecycle.py`：每 generation 独立目录和沙箱卷端口、Provisioned→Iterating→Checkpointing→Frozen 状态、重建与清理。
  - `checkpoint.py`：相对种子基线 diff、build excludes、symlink 目标域判定、结构化账本事故识别、scope 批量裁决、checkpoint manifest/receipt、CAS 冲突与 pending commit 恢复。
  - `generated.py`：`ArtifactKind.GeneratedCode` 仅调用受信 scaffold，从源工件重新生成，不翻译生成物。
- 新增确定性测试：`tests/workspace/` 37 项、`tests/security/test_workspace_gateway.py` 安全门用例；覆盖授权矩阵、路径攻击、原子写、编辑匹配、审计脱敏、双轨越界、幂等/CAS、动作协议、Exec 桥和生成 action。
- 同步 `Implementation_plan_doc/workspace/CM-WORKSPACE-001-候选工作区与工具网关实施计划.md`、对应详细设计和主任务表；未修改架构模块设计文档。
- 关键实现决策：
  - 复用 `codemigrator.core` 的 phase、session、error、WriteScope 和资源契约，不复制第二套公共定义（V-M12-V4-001/002）。
  - 文件内容不进入审计账本；Exec 脚本按 M-12 契约保留全文并同时记录哈希，Shell 仅记录命令/输出摘要（V-M08-V4-004、V-M12-V4-012）。
  - 结构化写入走逐笔 scope 门；Shell 的最终写效果由 checkpoint diff 批量校验，CAS 推进禁止 force update（V-M08-V4-007~010、V-M12-V4-010/013）。

## 3. 自测与验证结果

- `PYTHONPATH=src /home/xtc/env/codemigrator-plan/bin/pytest -q tests/workspace tests/security/test_workspace_gateway.py`：`37 passed`。
- `PYTHONPATH=src /home/xtc/env/codemigrator-plan/bin/pytest -q`：`274 passed`。
- `ruff check src tests`：通过。
- `mypy src`：`Success: no issues found in 61 source files`。
- `lint-imports --config pyproject.toml`：3 contracts kept、0 broken。
- `python -m compileall -q src tests`、`git diff --check`：通过。
- 对照验收条款：
  - `V-M12-V4-001/002/003/005/006/011/012/014/015/016/017 ✓`：网关、路径、协议、安全和审计测试覆盖。
  - `V-M08-V4-001/002/003/004/005/006/007/008/009/010/011/012/015/016 ✓`：生命周期、账本、checkpoint、CAS/pending、生成 action 测试覆盖。
  - `V-M12-V4-007/009/018`、真实 `bwrap`/Git/QuickJS/PG 联调：未在本任务伪造完成；通过 Query/Shell/Exec/CandidateRef/Sandbox 协议与确定性替身验证，分别由 CM-ANALYSIS、CM-SANDBOX、CM-GIT、CM-INFRA、CM-LOOP/RUNTIME/API 后续接入。

## 4. 影响面与风险

- 新增 workspace 子包公共执行端口，仅依赖 core，不改变既有子包边界或数据库/API；runtime、Git、sandbox、analysis、verification 可按既定 Protocol 接入。
- 审计字段只保存路径/命令/脚本摘要和必要 Exec 脚本全文，不保存结构化文件正文、Shell stdout/stderr 原文；checkpoint 越界清单仅作为 Agent 自纠事实返回。
- 真实 QuickJS、bwrap、Git CAS、quiesce 进程组和 PostgreSQL 事务仍需下游 adapter 联调；本地规则测试不调用真实模型、不访问网络。

## 5. 后续行动

- [ ] 完成提交前最终验证，创建本任务唯一 PR。
- [ ] 启动一次审查 agent 并等待其最终终态；按该次反馈在原分支一次性修复后直接合并，不追加同一 PR 审查。
- [ ] 合并后在主工作区 `develop` 执行 `git pull --ff-only origin develop`，再按主任务表领取下一项任务。

## 6. 附录（可选）

- 分支：`feature/workspace-gateway`。
- 本任务不进行真实模型测试；以规则测试、端口替身、路径攻击和恢复窗口测试为主。
