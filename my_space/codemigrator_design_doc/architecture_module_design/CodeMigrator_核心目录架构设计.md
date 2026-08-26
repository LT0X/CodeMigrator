# CodeMigrator 核心目录与描述符资源架构

> 文档状态：V4 当前架构基线；本篇是 crate 物理清单、依赖方向、描述符资源目录与进程部署拓扑的唯一 owner。
> 技术范围：Rust 2024 Cargo workspace、8 个核心 crate、双工具链描述符资源、`app + sandbox-worker + PostgreSQL` Compose 基线。
> 契约真相：公共领域类型、运行语义与验证契约以 [M-00 垂类设计原则与架构哲学](CodeMigrator_垂类设计原则与架构哲学.md) 为准；本篇冻结 workspace 组成、crate 依赖图、描述符目录规则与本地执行协议归属。
> 关联文档：[系统后端架构](CodeMigrator_系统后端架构.md)、[Harness 总体设计](CodeMigrator_Harness总体设计.md)、[代码分析与 AST 引擎](CodeMigrator_代码分析与AST引擎.md)、[候选工作区与工具网关](CodeMigrator_候选工作区与工具网关.md)、[沙箱与执行环境](CodeMigrator_沙箱与执行环境.md)、[验证引擎](CodeMigrator_验证引擎.md)、[工具系统与 Hook](CodeMigrator_工具系统与Hook.md)、[Web 体验与迁移可视化工作台](CodeMigrator_Web体验与可视化工作台.md)。

V4 把语言能力从进程级扩展改为声明式资源：新增一个语言对不再引入新的可执行进程、新的 RPC 方法集合或新的能力协商，而是新增两份描述符目录与对应 grammar、工具链镜像。目录设计因此只剩两个必须防住的失控点：把每个概念拆成独立库导致依赖网膨胀，或把某种语言的 grammar、命令行细节留在核心层导致任何新语言对都要修改核心。本篇把稳定 Rust 内核冻结为 8 个 crate，把会变化的语言事实全部压入 `descriptors/` 资源目录。

## 八个 crate：依赖无环，副作用归位

```mermaid
flowchart TB
    Runtime["codemigrator-runtime\nRun actor / Harness 编排 / 集成协调 / app 组合根"]
    Api["codemigrator-api\nREST / SSE 投影"]
    Planning["codemigrator-planning\n四类 Slice DAG / write scope / 集成键"]
    Analysis["codemigrator-analysis\n源端 tree-sitter 分析 / grammar 加载"]
    Workspace["codemigrator-workspace\n候选工作区 / 工具网关 / checkpoint"]
    Verification["codemigrator-verification\n三层验证 / 诊断归因 / fingerprint"]
    Sandbox["codemigrator-sandbox\nUDS worker / 沙箱执行 / 命令实例化"]
    Core["codemigrator-core\n公共类型 / 原则 / 端口 / phase 策略"]

    Runtime --> Api
    Runtime --> Planning
    Runtime --> Workspace
    Runtime --> Verification
    Runtime --> Sandbox
    Runtime --> Core
    Planning --> Analysis
    Planning --> Core
    Analysis --> Core
    Workspace --> Sandbox
    Workspace --> Core
    Verification --> Core
    Sandbox --> Core
    Api --> Core
```

`codemigrator-runtime` 是唯一组合根：只有它的 `codemigrator-app` bin target 读取环境、装配依赖、获取 PostgreSQL session advisory lock 启动门并启动 Run actor。`codemigrator-sandbox` 拥有 `codemigrator-sandbox-worker` bin target，由 Compose 独立启动，不是 app 内后台线程。其余 crate 不读取环境变量、不自行创建后台任务。`apps/codemigrator-cli` 与 `web/` 是产品入口，不计入核心 crate，也不出现在任何核心 crate 的依赖图中。

| crate | 唯一职责 | 禁止事项 | 允许依赖 |
|---|---|---|---|
| `codemigrator-core` | M-00 冻结的公共 ID、状态、`ToolchainDescriptor`/`CheckCommandTemplate`、`WriteScope`、稳定错误码、服务端口与 `core://phase-tool-policy/v2` 工具授权资源 | HTTP、SQL、Git、进程、文件 I/O、具体语言 grammar 与命令行细节 | 无 |
| `codemigrator-analysis` | 源项目进程内 tree-sitter 解析、grammar 隔离加载、import 图、模块清单、测试覆盖图（M-06） | 写源项目、生成目标代码、网络与数据库访问、执行检查命令 | `core` |
| `codemigrator-planning` | 契约/实现/测试翻译/测试生成四类 Slice DAG、write scope 派生、`OrderedBefore` 冲突边、冻结集成键（M-07） | 模型调用、文件系统与 Git 写、数据库访问 | `core`、`analysis` |
| `codemigrator-workspace` | 候选工作区生命周期、Git refs 与 expected-OID CAS、checkpoint commit、Agent 工具网关执行面（M-08/M-11/M-12） | 命令面外构造 program/argv、越 write scope 写入、验证通过判定 | `core`、`sandbox` |
| `codemigrator-verification` | 三层验证编排归约、`verification_fingerprint`（证据防替换由完整 outcome 落库承载）、诊断→owning Slice 归因（M-10）；分析产物作为不可变数据输入，不构成编译依赖 | 直接启动进程、推进 Git ref、连接数据库 | `core` |
| `codemigrator-sandbox` | UDS 类型化执行协议、bubblewrap/cgroup/配额、描述符命令模板实例化、一次性 validation overlay（M-09）；拥有 `sandbox-worker` bin | PostgreSQL 连接、Run/Slice 状态归约、命令面外执行、free shell | `core` |
| `codemigrator-runtime` | Run actor 邮箱与状态机、单写者启动门、active dispatch gate、Integration Coordinator、PostgreSQL ledger 与 `run_events`、恢复协调、观测装配；拥有 `codemigrator-app` 组合根 bin | HTTP DTO 形状、沙箱进程实现细节、语言 grammar 内容 | 上述全部 crate |
| `codemigrator-api` | HTTP DTO、SSE 断点回放、`If-Match`、RFC 9457 错误投影（M-02） | SQL、Git、进程启动、领域归约 | `core` |

这组依赖把"谁能做哪种副作用"表达成可检查的 Cargo 图：`analysis` 不碰网络与进程执行；`planning` 不碰文件系统；`verification` 只归约不执行；`workspace` 的全部执行都经 `sandbox` 的冻结命令面；`api` 只消费 `core` 端口，实现由组合根绑定。PostgreSQL repository、`run_events` 与 schema 演进（`migrations/`）归 `runtime`，因为状态与事件在同一 actor 事务写入；观测指标契约在 `core` 发布、tracing 装配在 `runtime`。CI 对 `cargo metadata` 依赖图、禁止依赖清单与 `runtime` 之外的环境读取执行静态审查。

整张图收敛为四层，层内禁止互相依赖：

| 层 | crate | 层规则 |
|---|---|---|
| 契约层 | `core` | 零内部依赖；唯一允许被所有层引用的层 |
| 领域层 | `analysis`、`planning`、`verification` | 纯逻辑与归约，不启动进程、不碰 Git、不连数据库 |
| 执行层 | `workspace`、`sandbox` | 唯一能触碰文件系统写、Git refs、子进程与 UDS 的层 |
| 编排投影层 | `runtime`、`api` | `runtime` 组合全部执行层并持有控制面事务；`api` 只做外部投影 |

## 描述符资源目录：语言差异的唯一载体

语言对 = 一份源端描述符 + 一份目标端描述符。目录按 `<language-role>/<language-id>` 组织，每份 `descriptor.json` 覆盖 M-00 对应端（`SourceToolchain` 或 `TargetToolchain`）的全部字段，并附加 `descriptor_version` 与 `language_role` 两个目录元数据字段：

### 描述符定位

描述符是**确定性差异声明载体**：语言对的差异事实全部以纯数据声明，服务两个结构性不可替代的位置——

| 位置 | 描述符承载物 | 不可替代性 |
|---|---|---|
| 分析确定性 | grammar、清单解析器、测试约定、import 提取模板 | F1~F4 确定性事实的唯一来源（M-06） |
| 裁决冻结 | 五类命令模板 `CheckCommandTemplate` → `InternalVerificationDispatch` | 冻结检查集的唯一来源，P-02 验证可复算的前提 |

消费者清单：ANALYZE 分析管线（消费源端描述符全量）、裁决层 `InternalVerificationDispatch`（消费目标端检查模板，冻结检查集唯一来源）、Scaffold 基线初始化、能力门 CreateRun 预检、会话上下文约定注入。`CheckRunner` 已退役，模型侧消费者清零——描述符的消费者全部是确定性组件。

三层能力阶梯：

| 层 | 内容 |
|---|---|
| 裁决面 | 描述符最小声明集 |
| 能力面 | Shell+Exec 语言无关承载，能力差异不再由描述符承担 |
| 演进面 | schema 版本化（`descriptor_version`）+ text-fallback 兜底 |

动态边界三档：

| 档位 | 覆盖 | 成本 |
|---|---|---|
| 纯数据动态 | grammar、命令模板、约定 | 新语言对零代码，仅新增描述符目录 |
| 注册表扩展 | 新清单格式需 `ManifestParserRef` 解析器注册表扩展 | Rust 代码，少见路径 |
| text-fallback 兜底 | 任何语言可回退文本声明 | 零增量 |

"数据不回退为代码插件"边界不变（D-032 哲学保持）。

```
descriptors/
├── source/
│   └── typescript/
│       ├── descriptor.json          # SourceToolchain：语言 id、扩展名、grammar 引用、清单解析器
│       └── grammar/
│           ├── tree-sitter-typescript.so
│           └── grammar.sha256
└── target/
    └── python/
        └── descriptor.json          # TargetToolchain：包管理器、五类命令模板、工件策略、工具链镜像摘要
```

源端示例（字段与 M-00 `SourceToolchain` 一致）：

```json
{
  "descriptor_version": "1.0.0",
  "language_role": "source",
  "language_id": "typescript",
  "extensions": [".ts", ".tsx"],
  "parser": {
    "grammar_id": "tree-sitter-typescript",
    "grammar_carrier": "shared-library",
    "grammar_path": "grammar/tree-sitter-typescript.so",
    "grammar_sha256": "…"
  },
  "manifest_parsers": [{ "manifest_kind": "npm-package", "parser_id": "npm-manifest" }]
}
```

目标端示例（字段与 M-00 `TargetToolchain` 一致，命令模板显式携带 program/argv/timeout；`artifact_rules` 为工件策略字段，见示例后说明）：

```json
{
  "descriptor_version": "1.0.0",
  "language_role": "target",
  "language_id": "python",
  "package_manager": "uv",
  "scaffold": [
    { "action": "SCAFFOLD", "program": "uv", "argv": ["init", "--lib"], "timeout_secs": 300 }
  ],
  "build": [],
  "test": [
    { "action": "TEST", "program": "uv", "argv": ["run", "pytest", "-q"], "timeout_secs": 120 }
  ],
  "lint": [
    { "action": "LINT", "program": "uv", "argv": ["run", "ruff", "check", "."], "timeout_secs": 300 }
  ],
  "typecheck": [
    { "action": "TYPECHECK", "program": "uv", "argv": ["run", "mypy", "."], "timeout_secs": 300 }
  ],
  "artifact_rules": [
    { "pattern": "**/*_pb2.py", "artifact_kind": "GeneratedCode", "source_pattern": "**/*.proto" },
    { "pattern": "{docker-compose.yml,Makefile,config.yaml}", "artifact_kind": "DeclarativeConfig" },
    { "pattern": "db/**/*.sql", "artifact_kind": "ResourceFile", "mapping": "copy" }
  ],
  "toolchain_image_digest": "sha256:…"
}
```

工件策略字段 `artifact_rules` 以 `ArtifactKind` 声明工件分类规则（枚举三类），各类处理策略：

| ArtifactKind | 典型工件 | 处理策略 |
|---|---|---|
| `GeneratedCode` 生成代码 | `.pb.go`、`*_pb2.py` 等 | 不翻译：由目标侧从源头（如 `.proto`）用目标工具链重新生成，grpcio-tools 类重新生成命令入目标端描述符 scaffold 档；`.proto` 源文件作为接口事实源被契约波消费。**通用降级阶梯**：目标生态无等价 codegen 时（如 go-zero `.api` DSL），该源 DSL 工件按 `DeclarativeConfig` 类处理——作为接口事实源归契约波，由契约 Slice Agent 翻译为目标语言惯用等价物，不适用 GENERATED 标注 |
| `DeclarativeConfig` 声明式基础设施配置 | docker-compose、Makefile、config.yaml、无 codegen 等价物的源 DSL | 作为声明式工件由契约波 Slice 翻译出目标侧等价物，归入契约 Slice write scope 派生 |
| `ResourceFile` 资源文件 | SQL schema、静态资源 | 按描述符 mapping 的资源映射复制/轻转换，不入翻译 Slice |

依赖副产物规范排除集 `build_excludes` 是目标端描述符的声明字段（M-00 `TargetToolchain`）：声明依赖安装/构建过程的副产物路径模式（如 `.venv/`、`__pycache__/`、`node_modules/`、Go 的构建缓存目录）。排除集内的写入不计入 checkpoint diff 校验、不进入 candidate commit——依赖在长驻沙箱卷内驻留复用（M-09），但构建垃圾不污染代码事实。

命令模板是目标端检查命令的唯一来源：`codemigrator-sandbox` 只做冻结参数代入与实例化，不接受模板外的 program、argv 或 shell 片段；这一命令面的消费方是 Harness 内部验证（裁决层 `InternalVerificationDispatch`）——`CheckRunner` 已退役，模型侧不再共用这一命令面。grammar 制品为 tree-sitter 动态库或 wasm 模块，由 `grammar_carrier` 声明，摘要规则相同。

安全 linter 用法提示：lint 档可声明安全 linter（Python 侧 `bandit`、Go 侧 `gosec` 类）作为可选辅助检查——这是现有命令模板机制的自然用法（向 lint 档追加一条命令模板即可），零机制新增；其定位是语义鸿沟（性能、安全、生态习惯不在测试主证覆盖内）的辅助缓解手段，与 [M-10 验证引擎](CodeMigrator_验证引擎.md)的验证边界声明、[M-15 Web 体验与可视化工作台](CodeMigrator_Web体验与可视化工作台.md)的证据页边界区块联动。

| 规则 | 内容 |
|---|---|
| 发现 | 启动时以 dirfd 相对寻址扫描 `descriptors/`；symlink、`..`、目录外 inode 不进入 registry |
| 摘要校验 | 每份 `descriptor.json` 计算正文 SHA-256；grammar 制品计算 SHA-256 并与 `parser.grammar_sha256` 核对，不符则该描述符整体不进入可用集 |
| 语言对摘要 | `ToolchainDescriptor.descriptor_sha256 = SHA-256(canonical(source_sha256 ‖ target_sha256))`，对应 M-00 语言对锁 |
| CreateRun 预检 | Spec 锁定的语言对、`descriptor_version` 与三段摘要全部命中才允许创建 Run；拒绝码统一沿用 owner（M-05/M-02）三码制——资源/语言对缺失或不可加载返回 `DESCRIPTOR_NOT_FOUND`，摘要不匹配返回 `DESCRIPTOR_DIGEST_MISMATCH`，镜像摘要不可验证返回 `TOOLCHAIN_IMAGE_UNAVAILABLE`，零副作用拒绝 |
| 版本化 | `descriptor_version` 为 semver；内容任何字节变化必须递增版本，旧摘要锁定的 Run 不受影响——资源文件不可变，替换内容即发布新版本。结构/字段破坏性变更为 MAJOR，新增模板或可选字段为 MINOR，不改语义的修正为 PATCH。内置镜像构建清单为每个语言对登记 `version → digest` 映射，启动时同一 version 对应不同 digest 即拒绝该描述符 |
| 分发 | 内置描述符随 app 镜像分发，摘要写入镜像构建清单；扩展方式为向 `descriptors/` 新增目录（内置卷挂载或镜像重建），核心 crate 零改动 |

CreateRun 预检发生在任何控制面写入之前：

```mermaid
sequenceDiagram
    participant U as 用户 / CLI
    participant A as app 组合根
    participant R as 描述符 registry
    participant D as descriptors/ 目录
    participant P as PostgreSQL

    U->>A: CreateRun(Spec 锁定语言对 + 三段摘要)
    A->>R: 查询语言对资源
    R->>D: dirfd 相对扫描 + SHA-256 计算
    D-->>R: 实际 source/target/grammar 摘要
    R-->>A: 全匹配 或 按失败事实返回三码之一（M-05）
    alt 三段摘要全匹配
        A->>P: 同一事务写 Run + run_events
        A-->>U: RunId
    else 任一不匹配
        A-->>U: 拒绝，PostgreSQL 与 Git 副作用为 0
    end
```

## 废除声明：进程级语言扩展全面退场

V4 明确废除 V3 的进程级语言扩展体系，以下对象在仓库、依赖图与协议面中不得残留：

| 废除对象 | V3 形态 | V4 替代 |
|---|---|---|
| `plugins/<plugin-name>/` 目录与插件进程 | 语言能力以外部可执行进程封装 | `descriptors/` 声明式资源 + 进程内调用 |
| 八方法进程 RPC：`Parse`/`Query`/`DependencyFacts`/`ResolveLocator`/`EmitPatch`/`BuildArgv`/`ParseDiagnostics`/`ReleaseParsedUnit` | host 与外部语言进程的双向调用 | 源端解析内化 `codemigrator-analysis`；命令构造内化 `codemigrator-sandbox` 模板实例化；诊断解析内化 `codemigrator-verification` 归因器 |
| 4-byte 长度前缀帧协议与 1 MiB JSON envelope | 上述 RPC 的传输层 | UDS worker 协议（唯一保留的本地协议；方法面/序列化/帧上限由 M-09 冻结） |
| `CapabilityManifest` 能力协商与 `api_version` 握手 | 启动期能力匹配 | 描述符静态声明 + CreateRun 预检摘要匹配 |
| 进程身份 `PluginId`/`PluginName` | 目录、manifest 与握手的身份一致性校验 | `language_id` + 目录名 + SHA-256 摘要一致性校验 |

源端解析内化后的关键风险是 grammar 动态库缺陷拖垮 app。`codemigrator-analysis` 的 grammar registry 因此独立于业务状态：按 `grammar_sha256` 缓存已加载句柄；单文件解析前置 64 MiB 上限；解析调用包裹崩溃捕获并挂每 grammar 熔断器——同一 grammar 连续两次崩溃后熔断，当次分析以 `ANALYSIS_INFRA_ERROR` 失败，app 进程与其他 Run 不受影响。实现可以选择把 tree-sitter 调用放进一次性子进程获得硬隔离，这只是 `codemigrator-analysis` 的内部部署选择，不是对外协议、不是扩展点，也不改变依赖图。

## 进程与部署拓扑：app + sandbox-worker + PostgreSQL

默认 Compose 仍是三个服务：`app`（来自 `codemigrator-app` bin）、`sandbox-worker`（来自 `codemigrator-sandbox-worker` bin）、PostgreSQL；MinIO 镜像与观测组件为可选 profile。

```mermaid
flowchart LR
    Cli["apps/codemigrator-cli\n或 web/ 产品入口"]
    App["app 进程\ncodemigrator-app bin\nAPI + Run actor + 集成协调"]
    Worker["sandbox-worker 进程\ncodemigrator-sandbox-worker bin\nbubblewrap 沙箱池"]
    PG[("PostgreSQL\n控制面真相 + run_events")]
    Out["托管输出工作区\nGit refs"]
    Cas["host CAS\n大对象正文"]
    Res["descriptors/ 资源\n+ 工具链镜像 registry"]

    Cli -->|REST / SSE| App
    App <-->|"UDS worker 执行协议（M-09）"| Worker
    App --> PG
    App --> Out
    App --> Cas
    App -.只读加载.-> Res
    Worker -.只读挂载.-> Res
```

app 与 worker 之间的类型化 UDS worker 协议归 `codemigrator-sandbox` 所有。协议的方法面、序列化（Protobuf v1）、帧上限（单帧 ≤256 KiB）与传输（host-only Unix `SOCK_SEQPACKET`）由 [M-09](CodeMigrator_沙箱与执行环境.md) 唯一定义并在 V-M09-V4-001 冻结，本篇只描述 crate 传输职责，不维护第二份方法面或帧格式定义。引用快照（真相源为 M-09）：方法面恰为六条——app→worker 的 `ExecuteCheck`/`CancelAttempt`、worker→app 的 `CheckStarted`/`CheckFinished`/`CleanupComplete` 与双向 `ProtocolError`；解码到方法名之外的 frame 一律 `ProtocolError`，不存在扩展注册。与 V3 的关系：闭合消息、身份回显校验（`DispatchAttemptId` + `CheckSubject` + `tested_commit_oid`）、active dispatch gate、一次性 overlay 语义全部保留；方法面从散布的隐式交互收敛为 M-09 冻结的显式六方法。

诊断原文的解析（file:line / 测试名提取）由 `codemigrator-verification` 在 app 侧完成，worker 不理解诊断语义。协议归属不让 `codemigrator-sandbox` 决定业务重试：是否重派、是否重生成、是否进入集成队列仍由 `codemigrator-runtime` 的 Run actor 决定。

| 传输对象 | 上限 | 设计理由 |
|---|---|---|
| UDS 消息 | 闭合类型化消息，单帧 ≤256 KiB（M-09 冻结） | 只承载控制事实与回执引用，大日志、源码与报告只能传 `ArtifactRef`，不进协议帧 |
| stdout/stderr | 每进程每流 256 MiB | 超限终止整个进程组并返回 `OUTPUT_LIMIT_EXCEEDED`，不以截断成功掩盖失败 |
| 单源文件解析 | 64 MiB | 与源端 `ReadFile` 共界，超限返回 `SOURCE_FILE_TOO_LARGE` |

| 安全边界 | 规则 |
|---|---|
| 控制事实 | worker 不连接 PostgreSQL、不持数据库凭据、代码无 SQL 依赖；只持有当前连接的进程组表 |
| 传输 | UDS 仅存在于 app-worker 宿主边界；不接受 HTTP 控制端口、任意网络回调、shell 字符串 |
| 沙箱可见性 | bubblewrap 装配时不挂载 UDS 控制目录、Docker socket、SSH agent 与宿主凭据；源快照与依赖 cache 只读挂载 |
| 断连 | app 断开后 worker 终止全部沙箱进程组，5 秒内无法清空则自行退出，由 Compose 重启 |

## 目录树：仓库根的落点

```
codemigrator/
├── crates/
│   ├── codemigrator-core/            # core
│   ├── codemigrator-analysis/        # analysis
│   ├── codemigrator-planning/        # planning
│   ├── codemigrator-workspace/       # workspace
│   ├── codemigrator-verification/    # verification
│   ├── codemigrator-sandbox/         # sandbox；含 sandbox-worker bin
│   ├── codemigrator-runtime/         # runtime；含 codemigrator-app 组合根 bin
│   └── codemigrator-api/             # api
├── descriptors/
│   ├── source/<language-id>/         # 描述符资源；owner: core（结构契约）+ 各消费 crate（语义）
│   └── target/<language-id>/
├── apps/
│   └── codemigrator-cli/             # CLI 应用；只消费 REST/SSE，不计入核心 crate
├── web/                              # 前端；只消费 REST/SSE，不计入核心 crate
├── migrations/                       # PostgreSQL schema 演进；owner: runtime
├── deploy/                           # Compose、seccomp policy、rootfs/镜像 digest；owner: sandbox + runtime；不含凭据
├── docs/                             # 本设计文档集（M-00~M-16 与文档迭代记录）
├── tests/
│   ├── contracts/                    # 跨 crate 公共契约测试
│   ├── recovery/                     # ledger / Git ref / checkpoint 重建测试
│   └── security/                     # 沙箱、路径、脱敏与协议测试
├── compose.yaml                      # app + sandbox-worker + PostgreSQL 基线
└── Cargo.toml                        # workspace 清单
```

`crates/` 恰好 8 个固定核心 crate，不放任何语言的 grammar、命令模板或镜像定义；语言事实只落在 `descriptors/` 与 `deploy/` 声明的镜像里。每个 crate 根 README 固定四项：负责、不负责、允许依赖、公共入口——这是防止内部实现被跨模块导入的第一层边界。`runtime` 可以组合全部 crate；任何其他 crate 不能反向依赖 `runtime`，也不能把 `descriptors/*` 当作编译期依赖（描述符只在运行时加载）。

## 贯穿场景

### 新增 Java→Go 语言对

1. 新增 `descriptors/source/java/descriptor.json`：语言 id `java`、扩展名、grammar 引用（`grammar/tree-sitter-java.so` + SHA-256）、清单解析器（`maven-pom`/`gradle`）；放入 grammar 制品。
2. 新增 `descriptors/target/go/descriptor.json`：包管理器 `go-mod`、脚手架/构建/测试/lint（`go vet`）/类型检查命令模板、Go 工具链镜像 digest；构建该镜像并登记 digest。
3. 打包：两份描述符与 grammar 随 app 分发（内置目录或卷挂载），Go 工具链镜像进入镜像仓库。
4. 用户 Spec 引用 `java→go` 语言对并锁定摘要；CreateRun 预检三段摘要命中即可创建 Run。
5. 全程 `crates/` 零 diff：`codemigrator-analysis` 按摘要加载 Java grammar，`codemigrator-sandbox` 在 Go 镜像内执行冻结命令模板，其余 crate 只见 M-00 公共类型。

### 描述符摘要不匹配被预检拒绝

Spec 锁定 `target/python` 描述符摘要 `X`。运维直接修改了部署环境中的 `descriptor.json`（实际摘要 `Y`）。用户提交 CreateRun：能力门重新计算实际 SHA-256，与锁定值不等，返回 `DESCRIPTOR_DIGEST_MISMATCH`（owner 三码制，M-05/M-02）；此时 Run、`run_events`、Git ref 新增数均为 0。修复方式不是放宽校验，而是发布递增 `descriptor_version` 的新描述符并用新摘要创建 Run——被旧 Run 锁定的资源事实不受影响。

## 构建纪律与可验收结果

- [ ] V-M01-V4-001：`cargo metadata` 依赖图与本篇冻结清单 exact-match：恰好 8 个核心 crate、无环、无清单外内部依赖边；CI 违例拒绝合并
- [ ] V-M01-V4-002：仓库与依赖图中 `plugins/` 目录、八方法进程 RPC、长度前缀帧、能力协商清单、进程身份类型零残留
- [ ] V-M01-V4-003：UDS 协议方法面、序列化与帧上限与 M-09 V-M09-V4-001 冻结定义逐字一致；本篇不存在第二份方法面定义；解码到方法名之外的 frame 一律 `ProtocolError` 且零执行
- [ ] V-M01-V4-004：grammar 制品 SHA-256 与描述符声明不符时，该描述符不进入可用集，CreateRun 预检失败
- [ ] V-M01-V4-005：Spec 锁定摘要与实际资源不匹配时，CreateRun 按 owner 三码制返回拒绝码（摘要不等为 `DESCRIPTOR_DIGEST_MISMATCH`；缺失/不可加载为 `DESCRIPTOR_NOT_FOUND`；镜像不可验证为 `TOOLCHAIN_IMAGE_UNAVAILABLE`），不存在本篇私有的第四种描述符拒绝码；Run/`run_events`/Git ref 新增数为 0
- [ ] V-M01-V4-006：新增 Java→Go 语言对的全流程中，`crates/` 内核心 crate 源码 diff 为 0
- [ ] V-M01-V4-007：`apps/codemigrator-cli` 与 `web/` 不出现在任何核心 crate 依赖图中；二者只消费 REST/SSE 投影
- [ ] V-M01-V4-008：`codemigrator-sandbox` 无 SQL 依赖且 worker 无数据库凭据；bubblewrap 挂载表不含 UDS 控制目录、Docker socket 与宿主凭据
- [ ] V-M01-V4-009：注入 grammar 崩溃故障后仅当次分析请求失败（`ANALYSIS_INFRA_ERROR`），app 进程存活、其他 Run 不受影响；同一 grammar 连续两次崩溃后熔断
- [ ] V-M01-V4-010：描述符内容变更而未递增 `descriptor_version` 时，启动校验拒绝该描述符进入可用集
- [ ] V-M01-V4-011：`codemigrator-app` 与 `codemigrator-sandbox-worker` 是仅有的两个服务 bin；第二个 app 实例无法取得 advisory lock 时 readiness 失败且不接收迁移 API

## 不以目录解决的问题

本篇不承诺核心天然支持全部语言：目标端工具链镜像的构建质量、grammar 覆盖度与命令模板语义正确性由描述符作者负责，目录边界只保证它们不渗透核心实现。Run 状态推进、持久化真相、Git ref 推进、沙箱隔离与验证完成条件分别由对应模块拥有；Agent 工具箱方法语义与 frame 规则由 M-12 所有，本篇只冻结其执行面（`codemigrator-workspace` 工具网关）与授权矩阵（`codemigrator-core` phase policy）的落点。

> 设计演进、历史缺陷处置和变更理由见：[文档迭代记录](文档迭代记录.md)。
