# feat(spec): add migration spec capability gates

- 关联任务：CM-SPEC-001
- 关联模块：M-05 Migration Spec 抽象层（含 M-02 入口契约）
- 关联迭代记录：`my_space/codemigrator_dev_progress/spec/CM-SPEC-001-Migration-Spec能力门迭代记录.md`

## 背景

按 M-05 的 V-M05-V4-001～012 与 `my_space/code_alignment_record/spec/CM-SPEC-001-对齐记录.md`，在下游快照、计划、沙箱和验证模块之前冻结可审计的 Migration Spec v3 输入契约。Spec 必须在进入资源消费前完成字节/JSON、Schema、资源锁和检查集四道门，并生成稳定 canonical/hash 身份。

## 变更点

- `src/codemigrator/core/models/`：新增闭合的 Spec v3、描述符锁、范围、检查选择与分解提示模型；统一复用 core 稳定错误码。
- `src/codemigrator/core/spec.py`：实现四道门固定顺序短路、问题 JSON Pointer 排序/上限、RFC 8785 JCS、SHA-256、零副作用结果和内存 insert-or-get 测试替身。
- `src/codemigrator/core/scope.py`：实现不依赖 glob/fnmatch 的有限 repository-relative 模式语言及 `.git` 永久排除。
- `src/codemigrator/core/ports.py`：定义 core 与 runtime 之间的 descriptor registry/repository 端口；本 PR 不执行文件、镜像或数据库 I/O。
- `migrations/0002_migration_specs.sql`：增加不可变 Spec 存储形状及 canonical hash 唯一约束。
- `tests/spec/`、`tests/contracts/` 与基线测试：覆盖四道门、短路、边界、规范化、资源锁、检查集、删除保护、DDL 和 import contract。
- `my_space/`：同步实施计划、详细设计、迭代记录、主任务表和本 PR 说明。

唯一一次审查提出的边界反馈已在原分支修订：数值字段严格类型化、JSON 深度预检、有限通配符非空段、资源能力 fail-closed、surrogate/NUL 拒绝，以及 Spec 嵌套模型深层不可变；未追加第二次审查。

本 PR 不改变 M-00 公共状态机、枚举或稳定错误码，不实现 runtime repository、descriptor 文件扫描、完整 API、CreateRun 编排、M-06 快照扫描或真实模型调用。无 BREAKING 变更。

## 自测证据

- [x] `uv run --frozen pytest -q`：113 passed
- [x] `uv run --frozen lint-imports`：3 contracts kept，0 broken
- [x] `uv run --frozen ruff check .`：通过
- [x] `uv run --frozen mypy src`：通过
- [x] `uv run --frozen python -m compileall -q src tests`：通过
- [x] `git diff --check`：通过
- [x] V-M05-V4-001～012：由 `tests/spec/` 与 `tests/contracts/test_spec_contracts.py` 的确定性测试覆盖
- [x] 真实模型测试：不需要；本任务只验证纯门逻辑、规范化与存储契约

## 风险与回滚

- descriptor registry 的真实资源事实和 SQL 持久化仍由后续 runtime 任务实现；本 PR 只冻结端口与 DDL 边界。
- `max_parallelism` 仅作为 planner hint 做正值校验，沙箱最终公式由后续执行层收敛。
- 资源 registry 的 grammar/image 可用性默认为不可用，runtime 必须显式提供已验证事实。
- 若需回滚，在本分支对应提交上使用 `git revert`；不重置或覆盖 `develop`。
