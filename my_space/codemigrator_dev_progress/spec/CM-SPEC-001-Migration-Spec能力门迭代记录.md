# CM-SPEC-001-Migration-Spec能力门_迭代记录

> 本记录按 `CodeMigrator迭代记录模板.md` 维护，记录 CM-SPEC-001 的 Spec v3 能力门、纯端口、DDL、验证证据和后续交接。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-SPEC-001
- **关联模块/文档**：M-05、`feature/spec-capability-gate`、`my_space/code_alignment_record/spec/CM-SPEC-001-对齐记录.md`

## 1. 变更动机

CM-CORE-001 与 CM-INFRA-001 已合并，下一 Wave 需要把迁移意图收敛为可审计、可哈希、可在 Run 前拒绝的 Spec v3。实现范围依据 M-05 与 CM-SPEC-001 对齐记录 D-01～D-04、V-M05-V4-001～012。

## 2. 变更内容

- 已实现：Spec v3 闭合模型、四道门、有限范围匹配器、JCS canonical/hash、registry/repository Protocol、内存 insert-or-get 替身。
- 已实现：`migration_specs` 版本化 SQL DDL；明确不实现 runtime repository、registry 文件 I/O 或完整 API。
- 文档：实施计划、详细设计、主任务表、PR 说明与本迭代记录均已同步。

### 2.1 唯一一次审查后的修订

PR 唯一审查返回 `REQUEST_CHANGES` 后，在原分支一次性完成以下修订，未再次启动审查：严格拒绝 `version`/`max_parallelism` 的 bool、float、字符串；在 JSON 解析前预检病理嵌套深度并捕获递归异常；将单星匹配改为非空段并修正 exclude containment；将 grammar/image 能力事实改为 fail-closed；在 JSON 门拒绝孤立 surrogate 并兜底 canonical 异常；拒绝范围模式 NUL；冻结 Spec 嵌套模型，保证仓储记录不可被浅层变更污染。

## 3. 自测与验证结果

- 当前阶段：CM-SPEC-001 已完成唯一一次 PR 审查及反馈修订，等待直接合并。
- DoD：V-M05-V4-001～012 已由专用门测试、契约测试与全量回归覆盖；全量 pytest、import-linter、Ruff、mypy、compileall 均通过。
- 验证结果：`uv run --frozen pytest -q`（101 passed）；`lint-imports`（3 contracts kept，0 broken）；Ruff、mypy、compileall 与 `git diff --check` 通过。
- 真实模型测试：不需要；本任务为确定性门逻辑和持久化契约。

## 4. 影响面与风险

- Spec 是 M-06/M-07/M-09/M-10/M-03 的冻结输入；门逻辑必须保持纯函数，拒绝路径不得创建 Spec/Run/Git 副作用。
- 资源 registry 与 repository 仅以端口交接，避免 core 读取环境、文件或数据库；范围匹配器不能退化为 glob/fnmatch。
- 若实施发现 M-05 已确认决策不足，必须先按对齐流程收口；不得静默扩大 Spec 字段或门语义。

## 5. 后续行动

- [x] 先写门、短路、规范化、端口替身和 DDL 契约测试。
- [x] 实现并运行 CM-SPEC-001 全部验证命令。
- [ ] 完成一次独立审查并等待返回；若有反馈只在本分支修复一次，然后直接提交、推送、合并，不追加审查。

## 6. 附录

- 上游 CM-INFRA-001 已合并提交：`d482d88`；当前分支从该 `develop` 基线创建。
