# CM-<模块缩写>-<序号>-<简述>实施计划

> 使用说明：编码任务开工前复制本模板到 `Implementation_plan_doc/<模块缩写>/CM-<模块缩写>-<序号>-<简述>实施计划.md` 填写。步骤必须拆到可直接执行的原子粒度；每步标注涉及的仓库路径与验收条款。计划执行中若需新增/调整主表任务，先经提问工具与用户确认（AGENTS.md §3.1）。

## 0. 元信息

- **日期**：YYYY-MM-DD
- **执行 Agent**：
- **关联任务编号**：T-Bxxx（主表编号）
- **所属模块/crate**：M-xx / `codemigrator-xxx`
- **关联交付物**：详细设计 `codemigrator_design_doc/detailed_coding_design/<模块缩写>/…_详细设计.md`、迭代记录 `codemigrator_dev_progress/<模块缩写>/…_迭代记录.md`

## 1. 目标与范围

- 完成定义（DoD）：可观测的完成标志（如"某 V 条款全部通过 + 编译零警告"）
- 范围排除项：

## 2. 前置条件

- [ ] 工作分支：从 `develop` 切出 `feature/<模块缩写>-<简述>`（AGENTS.md §2.3）
- [ ] 环境就绪项：所需工具链/基础设施（凭证与版本查 `my_space/.env`；沙箱相关验证在本机 WSL 完成，注意 100G 配额约束）
- [ ] 上游依赖：依赖的 M-xx 契约或前置任务已冻结/合入
- [ ] 设计文档：详细设计已评审并保存至 detailed_coding_design 目录

## 3. 实施步骤

> 每步一个原子动作；格式：`- [ ] 步骤描述（涉及文件 → 对应验收条款）`

1. - [ ] 
2. - [ ] 
3. - [ ] 

## 4. 验证计划

| 验证项 | 命令 | 通过标准 | 关联条款 |
| --- | --- | --- | --- |
| 编译 | `cargo build --workspace` | 零错误 | — |
| 测试 | `cargo test -p codemigrator-xxx` | 全部通过 | |
| 条款勾选 | 人工核对 | `V-Mxx-V4-nnn ✓` 逐条 | |

- 沙箱/Git ref/恢复类验证的环境说明（本机 WSL2；如涉及其余环境须注明）

## 5. 风险与回滚

- 风险点及缓解：
- 回滚方式（分支删除 / revert 提交）：

## 6. 收尾清单（对应 AGENTS.md §4 四件套）

- [ ] 模块迭代记录已按模板生成于 `codemigrator_dev_progress/<模块缩写>/`
- [ ] 主任务表 `CodeMigrator开发任务规划与进度跟踪.md` 已更新
- [ ] 详细设计文档已保存于 `detailed_coding_design/<模块缩写>/`
- [ ] 本实施计划已保存于 `Implementation_plan_doc/<模块缩写>/`
- [ ] 若设计与架构文档有差异：架构文档冲突部分已同步回写（§3.4）
