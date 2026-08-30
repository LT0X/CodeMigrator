# CM-WORKSPACE-001-候选工作区与工具网关实施计划

> 本计划依据 M-08/M-12 当前 V6 设计与 `CM-WORKSPACE-001-对齐记录.md` 制定；代码仅在 `feature/workspace-gateway` 工作树完成，主工作区只接收合并后的提交。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-WORKSPACE-001
- **所属模块/crate**：M-08 + M-12 / `codemigrator-workspace`
- **关联交付物**：详细设计 `codemigrator_design_doc/detailed_coding_design/workspace/CM-WORKSPACE-001-候选工作区与工具网关详细设计.md`、迭代记录 `codemigrator_dev_progress/workspace/CM-WORKSPACE-001-候选工作区与工具网关迭代记录.md`

## 1. 目标与范围

- **完成定义（DoD）**：交付 workspace 子包的六工具 closed-schema 门禁与执行端口、候选工作区生命周期、结构化写账本、checkpoint 双轨 scope 校验/幂等恢复、动作编码和生成 action 纯逻辑；对应确定性安全/恢复/授权测试通过，Ruff、mypy、import-linter、compileall 与 diff 检查通过。
- **范围排除项**：不实现 PostgreSQL/Git 真实 adapter、bwrap 物理适配、PSF 查询索引、QuickJS 第三方绑定、验证裁决、Run actor、会话循环和 API；这些能力以协议端口接入，真实 adapter 分别由 CM-GIT、CM-SANDBOX、CM-ANALYSIS、CM-INFRA、CM-VERIFY、CM-RUNTIME 承接。

## 2. 前置条件

- [x] 工作分支：从合并后的 `develop` 切出 `feature/workspace-gateway`。
- [x] 环境就绪项：Python 3.12+/uv 环境可用；本任务规则测试不依赖真实模型、Docker 或网络。
- [x] 上游依赖：CM-CORE、CM-ANALYSIS、CM-PLAN、CM-SANDBOX 已冻结/合入；CM-GIT 以端口形式后续接入。
- [x] 设计文档：已阅读 M-08/M-12 与对齐记录，并建立本详细设计文档。

## 3. 实施步骤

1. - [x] 建立闭合工具调用/输出、错误 facts、授权上下文、工作区与审计模型（`src/codemigrator/workspace/models.py` → V-M12-V4-001/002/003/005/006/012、V-M08-V4-004）。
2. - [x] 以工作区根 dirfd 为锚实现路径字符串门、逐段 no-follow 解析、设备边界与安全根绑定（`src/codemigrator/workspace/paths.py` → V-M12-V4-011、V-M08-V4-005）。
3. - [x] 实现 ToolGateway 的 policy 单次加载/phase 与会话授权、schema admission、ReadFile/WriteFile/EditFile、QuerySourceAst/Shell/Exec 端口调度及拒绝传播（`gateway.py`、`protocol.py` → V-M12-V4-001~007、009、011~018）。
4. - [x] 实现候选工作区 Provisioned/Iterating/Checkpointing/Frozen/Discarded 生命周期、独立目录/沙箱卷端口、成功结构化写账本和幂等清理（`lifecycle.py` → V-M08-V4-001~005、011~012、015）。
5. - [x] 实现相对种子基线的 diff 变更集、build excludes、write scope 双轨校验、checkpoint manifest/receipt、CAS 与恢复窗口端口（`checkpoint.py` → V-M08-V4-006~010、V-M12-V4-010/013）。
6. - [x] 实现 `[cm:*]` 分段动作与严格 JSON 回退、生成代码从源头执行的 action 端口和 Exec 工具桥回执序（`protocol.py`、`generated.py` → V-M08-V4-016、V-M12-V4-014~017）。
7. - [x] 以测试先行补齐路径攻击、授权矩阵、原子写/编辑匹配、账本脱敏、双轨越界、幂等/CAS/恢复、动作解析和桥接回执测试（`tests/workspace/`、`tests/security/`）。
8. - [ ] 同步三份收口文档与主任务表，执行专项/全量测试及质量门；提交、推送并创建唯一 PR，完整等待一次审查终态后一次性修复并直接合并。

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| 专项测试 | `/home/xtc/env/codemigrator-plan/bin/pytest -q tests/workspace tests/security` | 全部通过 | V-M08/V-M12 全部可施工条款 |
| 全量测试 | `/home/xtc/env/codemigrator-plan/bin/pytest -q` | 零失败 | 回归 |
| 静态检查 | `/home/xtc/env/codemigrator-plan/bin/ruff check src tests` | `All checks passed!` | — |
| 类型检查 | `/home/xtc/env/codemigrator-plan/bin/mypy src` | `Success: no issues found` | — |
| 依赖边界 | `/home/xtc/env/codemigrator-plan/bin/lint-imports --config pyproject.toml` | 所有 contracts kept、0 broken | M-01 |
| 编译/差异 | `python -m compileall -q src tests && git diff --check` | 命令成功 | — |

- 沙箱/Git ref/恢复类验证在本机 WSL2 以 fake port 和临时目录完成；不声称已完成真实 bwrap、PostgreSQL、Git CAS 或 QuickJS 联调。

## 5. 风险与回滚

- **风险点及缓解**：路径解析必须保持 no-follow 与根设备边界；所有结构化写先写同目录临时文件并 fsync；checkpoint 只接受受信 diff provider；真实执行能力均通过协议注入，避免 workspace 导入 runtime/API 或复制 core 契约。
- **回滚方式**：保留该 feature 分支与工作树；如合并后发现回归，使用 GitHub revert 该 PR，不改写主干，不 force-push。

## 6. 收尾清单（对应 AGENTS.md §4 四件套）

- [x] 模块迭代记录已按模板生成于 `codemigrator_dev_progress/workspace/`。
- [ ] 主任务表 `CodeMigrator开发任务规划与进度跟踪.md` 已更新。
- [ ] 详细设计文档已保存于 `detailed_coding_design/workspace/`。
- [x] 本实施计划已保存于 `Implementation_plan_doc/workspace/`。
- [x] 本任务不修改架构模块设计文档，无架构差异回写项。
