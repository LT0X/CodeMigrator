# CM-WORKSPACE-001-候选工作区与工具网关详细设计

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-WORKSPACE-001
- **所属模块/crate**：M-08 + M-12 / `codemigrator-workspace`
- **依据的架构文档**：《CodeMigrator_候选工作区与工具网关》生命周期、工具网关执行侧、checkpoint commit、恢复与中断窗口、V4 验收基线；《CodeMigrator_工具系统与Hook》授权、六工具、路径安全门、错误传播和最小审计点位。
- **关联交付物**：实施计划 `Implementation_plan_doc/workspace/CM-WORKSPACE-001-候选工作区与工具网关实施计划.md`、迭代记录 `codemigrator_dev_progress/workspace/CM-WORKSPACE-001-候选工作区与工具网关迭代记录.md`

## 1. 需求与边界

- **本任务做什么（一句话）**：将候选工作区即沙箱卷、六工具门禁与执行端口、结构化文件账本、checkpoint 双轨校验和恢复窗口落为可组合、可测试的 workspace 纯 Python 执行面。
- **不做什么**：不定义 core 的 Phase/WriteScope/错误码，不实现 M-06 PSF 索引、M-09 bwrap、M-11 Git 底层事务、M-10 验证裁决、M-03/M-04 runtime 编排、M-02 API/数据库或 QuickJS 外部依赖。
- **覆盖的验收条款**：`V-M08-V4-001`～`V-M08-V4-016`、`V-M12-V4-001`～`V-M12-V4-018`（其中真实 bwrap、Git、数据库、QuickJS 联调通过端口测试与后续 owner 交付，不在本任务伪造完成）。

## 2. 契约引用

| 引用事实 | Owner 文档 | 引用形式 |
| --- | --- | --- |
| `Phase`、`SessionKind`、`WriteScope`、`StableErrorCode` | M-00 / `codemigrator.core` | 直接导入 core，不在 workspace 重定义 |
| `MigrationSlice`、`SliceCandidate`、`ArtifactRef` | M-00 / `codemigrator.core.models` | 作为生命周期与 checkpoint 输入 |
| phase policy 与资源摘要 | M-00 / `core://phase-tool-policy/v2` | `load_resource` 单次读取，保存版本+hash |
| PSF 查询请求/结果 | M-06 / `codemigrator.analysis.query` | `QuerySourceAstPort` 协议消费，不复制索引实现 |
| bwrap Shell 执行 | M-09 / `codemigrator.sandbox` | `ShellRunner` 协议消费，不在 workspace 组装命令面 |
| candidate ref/CAS | M-11 | `CandidateRefPort` 协议消费 |

## 3. 详细设计

### 3.1 结构与职责

- `models.py`：闭合工具调用/输出、`ToolError`、授权上下文、审计事件、工作区状态、账本、checkpoint manifest/receipt；所有公有 Pydantic 模型 `extra="forbid"`。
- `paths.py`：`SecureRoot`/`SecurePath` 绑定根 dirfd；先做字符串 1～4 规则归约，再用 `O_NOFOLLOW` 逐段解析并检查 `st_dev`，不跟随 symlink、不穿 ` .git`/挂载点。
- `gateway.py`：`ToolGateway` 固定门禁次序为 schema → phase/session → path → scope → execution；policy 在构造时读取一次，拒绝路径不打开文件、不调用下游；结构化写结果可通过 `operation_sink` 转入生命周期账本。
- `lifecycle.py`：`WorkspaceManager` 管理每 generation 一目录、空基线/最近 checkpoint 重建、Iterating 期结构化写及清理；长期沙箱通过 `SandboxVolumePort` 注入。
- `checkpoint.py`：`WorkspaceDiffPort` 提供宿主受信变更集；`CheckpointService` 在一次 checkpoint 中执行 quiesce、scope 批量校验、commit、CAS 推进和 receipt，拒绝不推进。
- `protocol.py`：`[cm:action]` 分段解析和严格 JSON 回退；`ExecToolBridge` 只将底层调用重新交给 `ToolGateway`，不暴露 Python/OS API。
- `generated.py`：`GeneratedCode` 只调用受信 `ScaffoldPort`，从源工件重新生成，不转换生成物。

### 3.2 关键机制

1. **授权**：`ToolGateway.dispatch` 先校验工具名/输入联合，再读取已冻结 policy；VERIFY/REPORT 任何工具固定 `TOOL_PHASE_DENIED`。探索协调会话只读，修复会话只扩读不扩写；不设用户 Hook 注册表。
2. **路径**：所有路径必须是单一根下的 POSIX 相对路径；拒绝空段、`.`/`..`、绝对/`~`、反斜杠、NUL、`.git`。`SecureRoot` 保存根 dirfd 与设备号，每次解析逐段 `O_NOFOLLOW`；写目标采用同目录随机临时文件、`fsync`、`os.replace`。
3. **文件工具**：ReadFile 支持根路径和已授权 CAS digest 两种形态，单文件 64 MiB、单次 256 KiB、会话 2,000 次；EditFile 只允许已存在文件，零命中/多命中分别返回稳定错误与行号 facts；成功 WriteFile/EditFile 恰好追加一条不含正文的账本。
4. **Shell/Exec**：ShellRunner 与 ExecEngine 是不可信/外部执行端口，调用和结果均产生 pre/post 审计；Shell 非零退出是反馈，超时由 runner 负责终止进程组；Exec engine 只能拿到 `ExecToolBridge`，每一笔底层调用回到同一 gateway，因基础设施依赖缺失而不偷偷降级到宿主 Python。
5. **checkpoint**：以最近 checkpoint 或空基线为种子，quiesce 后从受信 diff provider 取得 tracked 修改/删除和 scope 内新建；去除 `build_excludes` 后验证变更集包含于 `write_paths` 或合法新建 `create_roots`，并单独处理 symlink 目标与域外删除。纯 Shell 越界返回路径清单且零 commit/ref 推进；若发现结构化越界账本则标记基础设施事故并整体丢弃。
6. **幂等/恢复**：幂等键由 run/slice/generation/expected OID/内容摘要构成；同键返回原 receipt，不重复 commit。commit 已建/ref 未推进时复用 pending commit 重试 expected/new CAS；ref 已推进/receipt 缺失时只补 receipt；不强制覆盖移动的 ref。
7. **生命周期**：`Provisioned → Iterating → Checkpointing → Frozen`；失败/取消/终态清理先 quiesce、再删除沙箱卷和目录。脏工作区不作为事实，物理重派保持 generation，从最近合法 checkpoint 重建。

### 3.3 数据与接口

- 不新增 `migrations/`、REST/SSE DTO 或数据库表；事件以 `AuditSink` 端口输出，由 M-02/M-13 事务投影。
- `QuerySourceAstPort.query(request)`、`ShellRunner.run(call, workspace_root)`、`ExecEngine.execute(script, bridge, timeout)`、`CandidateRefPort.create_checkpoint(...)` 均为 Protocol，可用内存替身完成确定性测试。
- `ToolGateway.dispatch(raw_call)` 返回成功输出或 `ToolError`，不自行重试、不代改、不降级；`ToolGateway` 对下游异常仅按对应稳定错误语义包装。
- `WorkspaceDiffPort.diff(base, root)` 只返回相对种子基线的 `WorkspaceChange`，防止将缓存/全量树误判为候选代码事实。

### 3.4 错误处理

复用 `StableErrorCode`：`TOOL_PHASE_DENIED`、`TOOL_NOT_FOUND`、`TOOL_SCHEMA_INVALID`、`PATH_DENIED`、`READ_OUT_OF_SCOPE`、`WRITE_SCOPE_VIOLATION`、`EDIT_TARGET_NOT_FOUND`、`EDIT_AMBIGUOUS`、`READ_LIMIT_EXCEEDED`、`WRITE_LIMIT_EXCEEDED`、`QUERY_TIMEOUT`、`PATH_OUTSIDE_SNAPSHOT`、`TEXT_FALLBACK_UNSUPPORTED`、`SHELL_TIMEOUT`、`SHELL_LIMIT_EXCEEDED`、`EXEC_TIMEOUT`、`EXEC_SCRIPT_ERROR`、`CANDIDATE_REF_CONFLICT`、`CHECKPOINT_WRITE_FAILED`。错误 facts 只保留类别、哈希、计数/行号和域摘要，不将正文、密钥或拒绝路径原文写入对外事件。

## 4. 测试设计

- `tests/workspace/test_models.py`：closed schema、判别联合、上限、错误 facts、生命周期模型。
- `tests/workspace/test_paths.py`：绝对、遍历、`.git`、NUL、symlink、跨设备和替换竞争；验证拒绝时目标不打开。
- `tests/workspace/test_gateway.py`：四阶段授权、policy 单次加载、读写域、原子写、EditFile 匹配、CAS、Query/Shell/Exec 端口与审计。
- `tests/workspace/test_lifecycle.py`：独立 generation、空基线、checkpoint 重建、账本无正文、幂等清理和崩溃重派。
- `tests/workspace/test_checkpoint.py`：tracked/untracked/delete/symlink/build-excludes、Shell 越界自纠、结构化越界事故、CAS/receipt 恢复窗口。
- `tests/workspace/test_protocol.py`：分段多动作、解析错误回灌事实、JSON 回退、桥接逐笔回执与无宿主 API。
- `tests/security/test_workspace_gateway.py`：安全门攻击样本和审计脱敏。

条款映射以测试名体现：`V-M12-V4-001..007 → test_gateway_*`；`V-M12-V4-011/012/014..018 → test_paths/test_protocol/test_security_*`；`V-M08-V4-001..005/011/012/015 → test_lifecycle_*`；`V-M08-V4-006..010/013/016 → test_checkpoint/test_generated_*`。

## 5. 与架构文档的差异记录

- **无架构差异**。QuickJS、真实 bwrap、Git CAS 和数据库只提供本任务端口；本任务不修改架构模块设计文档，也不把端口替身描述成真实联调完成。

## 6. 影响面

- 新增 workspace 执行面与确定性测试；只增加对 core/sandbox 的既定依赖，不改变既有包接口。
- 后续 CM-GIT、CM-LOOP、CM-RUNTIME、CM-VERIFY、CM-API 可通过 Protocol 接入；CM-INFRA 负责登记 QuickJS/真实环境依赖。
- 不进行真实模型调用；规则测试不产生外部写入、网络访问或数据库副作用。
