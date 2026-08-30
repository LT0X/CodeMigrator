# CM-SPEC-001-Migration-Spec能力门详细设计

> 本设计依据 M-05《Migration Spec 抽象层》与 `my_space/code_alignment_record/spec/CM-SPEC-001-对齐记录.md`。它拥有本任务的实现落点说明；稳定公共类型与拒绝码仍只由 `codemigrator.core` 既有契约拥有。

## 0. 元信息

- **日期**：2026-08-30
- **执行 Agent**：Codex
- **关联任务编号**：CM-SPEC-001
- **关联架构**：M-05 Migration Spec 抽象层、M-02 API 入口约束

## 1. 任务理解

- **做什么**：实现 Spec v3 的字节/JSON、Schema、资源、检查集四道门；有限仓库路径模式匹配；有序问题响应；RFC 8785 JCS 规范化与 SHA-256；DescriptorRegistry 与 SpecRepository 端口；`migration_specs` DDL。
- **不做什么**：不实现资源账本扫描/grammar 或镜像验证、不执行 SQL、不提供完整 HTTP 路由、不编排 CreateRun、不扫描快照、不识别 artifact_rules。

## 2. 数据模型与边界

### 2.1 Spec v3 typed model

`MigrationSpec` 使用 `CoreModel(extra="forbid")`，字段为 `schema`、`version`、`name`、可选 `description`、源/目标语言、四字段 `DescriptorLock`、`SpecScope`、`RequiredCheckSelection[]` 与可选 `Decomposition`。名称按 UTF-8 字节限制 1..128，描述按 UTF-8 字节限制不超过 1024；语言 id 为非空小写 slug 且源/目标必须不同。

`SpecScope` 的 include 至少一项，exclude 可空；两者在 Schema 门校验为有限模式。`RequiredCheckSelection` 只允许 action 与模板摘要，命令正文、参数、超时和 write scope 由 extra-forbid 拒绝。分解策略只作为 Planner 提示，`max_parallelism` 只能收窄且不改变沙箱公式。

### 2.2 四道门

`validate_spec_bytes` 固定执行：

1. 字节/JSON 门：拒绝超过 262144 bytes、UTF-8 BOM/非法编码、重复 key、超过 32 的嵌套深度；返回首门问题后停止。
2. Schema 门：严格 JSON 顶层与嵌套模型、版本/语言/范围/模式/字段边界；问题按 JSON Pointer UTF-8 字典序、最多 100 条，超出置 `truncated`。
3. 资源门：调用 `DescriptorRegistry` 端口核对源/目标存在、版本/双资源摘要/镜像摘要、grammar 与镜像可验证状态；只接受 registry 已冻结结果，不在 core 做 I/O。
4. 检查集门：按 `(action, template_sha256)` 查目标端模板覆盖，Compile 与 Test 各至少一项，选择对不能重复；通过后才规范化。

任意门失败都不产生 Spec 正文或持久化副作用。门之间短路，上传与 CreateRun 复用同一纯门函数。

### 2.3 范围模式

匹配器不使用 `fnmatch`/`glob`。允许字面目录前缀（尾 `/`）、字面文件、以及前缀目录下最后一段至多一个 `*`；拒绝 `**`、`?`、字符类、大括号、正则、绝对路径、空段、`.`/`..` 和 `.git` 前缀。exclude 必须被至少一个 include 包含；`.git/` 永远不进入范围。匹配是纯函数，供 M-06 快照扫描期调用。

### 2.4 规范化与端口

Schema 门成功后复制业务字段：include/exclude 去重后按 UTF-8 字节排序，required checks 按 action 值与摘要排序，decomposition 缺省不写入业务正文。使用 `core.canonical_json_bytes`（RFC 8785 JCS）计算 `SHA-256(canonical_bytes)`；不把 SpecId/时间写入 hash。`InMemorySpecRepository` 仅是测试替身，实现 hash 相同 canonical bytes 的 insert-or-get，不伪装成 runtime SQL。

`DescriptorRegistry` 暴露无副作用的 `resolve(source_language_id, target_language_id)`，返回包含语言对、描述符版本、三份摘要、检查模板覆盖及 grammar/镜像可用性事实的 `DescriptorResolution`；core 不读取文件或镜像。`SpecRepository` 暴露 insert-or-get 与引用感知 delete Protocol。runtime 后续实现文件/数据库事实时必须复用这些端口。

## 3. 持久化设计

`migrations/0002_migration_specs.sql` 只建立 `migration_specs` 表，保存原始 JSON、canonical JSON、canonical SHA-256、语言对、descriptor lock、规范化 scope、canonical checks、decomposition 与创建时间；`canonical_sha256` 唯一约束支持 insert-or-get。表不提供 UPDATE 触发器/路径；删除引用由 runtime repository 按 `SPEC_IN_USE` 端口语义处理。DDL 不复制 Spec 门逻辑。

## 4. 测试设计与验收映射

| 条款 | 测试覆盖 |
| --- | --- |
| V-M05-V4-001/002 | 字节上限、BOM/编码、重复 key、深度、版本、extra-forbid |
| V-M05-V4-003/007 | registry stub 的缺失、资源摘要/版本、grammar、镜像三态和零副作用计数 |
| V-M05-V4-004/006 | Compile/Test 完整性、模板覆盖和摘要不匹配 |
| V-M05-V4-005/010 | 所有层级命令/write scope/辅助字段拒绝 |
| V-M05-V4-008 | object/array 顺序、路径去重排序与 JCS hash 稳定矩阵 |
| V-M05-V4-009 | 有限匹配器允许/拒绝表及 `.git` 永久排除 |
| V-M05-V4-011/012 | insert-or-get、引用删除、预检拒绝零副作用 |

## 5. 设计同步与交接

- 与 M-05 对齐记录一致，不新增稳定码、不改变公共子包边界。
- M-06 接收规范化 scope 与源端解析器引用；M-07 接收 canonical Spec/hash、descriptor lock、checks、decomposition；M-09/M-10 接收冻结检查集；M-03 接收资源收据与 Spec hash。
- registry 文件 I/O 与 repository SQL 留给 CM-RUNTIME-001；完整 API envelope 留给 CM-API-001。
