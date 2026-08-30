# CodeMigrator

CodeMigrator 是一个面向跨语言源代码迁移的确定性、证据驱动型 Agent 系统。它将
源代码分析、迁移规格、迁移计划、候选工作区、受控工具调用、隔离执行、多层验证
和可审计运行编排组织为一套统一的工程体系，最终生成可复核、可验证、可交付的
目标项目。

CodeMigrator 不把迁移理解为不受约束的代码生成，而是把一次迁移建模为一组可冻结、
可验证、可回放的事实与操作：输入规格和工具链先经过契约校验，计划被拆分为具有
依赖关系和明确写入边界的切片，候选修改通过受控工作区产生，执行结果以回执和
证据保存，只有满足验证条件的结果才能进入后续集成与交付。

## 项目目标

- 为跨语言迁移提供统一的规格、计划、执行和验证抽象。
- 以规范化契约、稳定标识和显式状态转换保证结果可重复。
- 以证据、诊断归因和验证指纹支撑失败分析与安全修复。
- 以冻结的 write scope 限制所有候选工作区写入。
- 通过 sandbox、资源池和受控工具网关隔离高风险操作。
- 通过事件账本、执行回执和集成事实保留完整的审计链路。
- 通过声明式 descriptor 支持不同语言与工具链，避免把语言知识散落在代码分支中。

## 设计原则

### 确定性优先

公共 ID、枚举、错误码、Pydantic 数据契约、规范化序列化、路径规则和策略资源
由 `codemigrator.core` 统一定义。领域层优先使用纯函数和不可变输入，避免同一
事实在不同模块中产生不一致的解释。

### 证据驱动推进

候选版本、验证结果和集成状态只能由受信执行事实、结构化诊断、验证 guard 与
集成回执推动。原始输出与派生结论分离保存，派生结论不能替代原始执行事实。

### 写入边界明确

迁移计划冻结后，每个 Slice 都拥有明确的文件写入范围。工作区、工具网关和 Git
操作必须遵循该范围；模型输出不能直接获得任意文件系统写权限。

### 组合根集中副作用

环境读取、数据库连接、进程生命周期、sandbox、Git、模型 provider 和观测适配器
由 runtime 组合根装配。分析、规划和验证等领域包保持可测试、可复用的边界，
不自行连接数据库或启动外部进程。

### 安全失败

契约缺失、资源事实不完整、版本冲突、诊断无法归因、执行回执迟到或 checkpoint
损坏时，系统采取拒绝推进、记录审计事实或重建的策略，不把不确定性解释为成功。

## 系统架构

项目采用 Python `src` 布局和八个边界清晰的子包。依赖方向以 `core` 为底层契约
中心，`runtime` 负责组合各类端口和适配器，`api` 负责外部投影与命令入口。

| 子包或目录 | 架构职责 |
| --- | --- |
| `src/codemigrator/core/` | 公共 ID、状态、错误码、Spec、路径规范和无副作用领域契约。 |
| `src/codemigrator/analysis/` | 基于只读源端事实构建模块、依赖、测试覆盖图并提供分析投影。 |
| `src/codemigrator/planning/` | 校验迁移提案，生成 DAG、Slice、write scope 和集成顺序。 |
| `src/codemigrator/workspace/` | 候选工作区、checkpoint、Git 输出历史和集成原语。 |
| `src/codemigrator/sandbox/` | bubblewrap/cgroup 适配、长期卷、验证临时目录和资源限制。 |
| `src/codemigrator/verification/` | 检查执行结果归一、诊断解析、指纹与失败归因。 |
| `src/codemigrator/runtime/` | Run actor、调度、会话编排、恢复、事务适配和组合根。 |
| `src/codemigrator/api/` | REST/SSE DTO、认证、幂等、If-Match、事件回放和 Problem Details。 |
| `descriptors/` | 源端与目标端语言、工具链及检查能力的声明式资源。 |
| `apps/codemigrator-cli/` | CLI 产品入口资产。 |
| `web/` | Web 产品入口资产。 |
| `migrations/` | PostgreSQL 版本化迁移脚本。 |
| `deploy/` | 应用、数据库和沙箱的部署基线。 |

Run 的控制面遵循单 Run 单写者原则：actor 串行接收 typed mailbox 消息并作出状态
决定；模型、工具、Git、sandbox 和检查命令等耗时操作通过端口在 actor 外部执行，
再以带身份信息的回执返回。API 只提交命令和读取投影，不绕过 runtime 直接修改
领域事实。

## 迁移生命周期

一次标准迁移遵循以下受控链路：

1. 从 `descriptors/` 注册并校验源端、目标端语言和工具链事实。
2. 冻结迁移规格、描述符摘要和 required checks。
3. 对源仓库执行只读分析，生成模块、依赖和测试覆盖事实。
4. 校验迁移提案，冻结包含 DAG 依赖、Slice 和 write scope 的迁移计划。
5. 创建候选工作区，通过批准的工具网关和 sandbox 执行迁移操作。
6. 依次执行局部、集成和最终验证，保存原始回执及派生证据。
7. 对失败进行诊断归因，在规则允许的边界内重生成或修复候选版本。
8. 通过受控 Git 集成推进 verified 结果，并生成确定性报告。

```text
源项目快照
    │
    ├─ 项目分析与结构事实
    ├─ Spec、理解档案、目标蓝图、迁移规则冻结
    │
    └─ CreateRun
         │
         ├─ 描述符与工件预检
         ├─ 计划生成、机器校验与 Slice 冻结
         ├─ 候选生成、局部验证与有序集成
         ├─ 最终验证与证据归档
         └─ 报告交付 / 目标代码交付
```

源快照始终只读。目标代码写入由系统托管的输出工作区完成，目标 Git 历史从空输出
基线开始；源仓库历史、宿主路径、凭据和大对象正文不会通过公共 API 泄露。

## 目录结构

```text
.
├── apps/codemigrator-cli/       CLI 应用入口资产
├── descriptors/                 源端与目标端工具链声明式事实
├── deploy/                      容器、目标工具链和 sandbox 部署资产
├── migrations/                  PostgreSQL 版本化迁移脚本
├── src/codemigrator/            Python 应用包
├── test_fixtures/               确定性分析与契约测试夹具
├── tests/                       单元测试、契约测试和边界测试
├── compose.yaml                 本地 app 与 PostgreSQL 服务
├── pyproject.toml               构建、依赖、测试、lint 和类型检查配置
└── uv.lock                      锁定的 Python 依赖解析结果
```

## HTTP API

API 默认以 `/api/v1` 为前缀，使用单一部署令牌和固定主体。主要资源包括：

- Spec 与迁移：创建 Spec、创建或取消 Run、Run 列表与详情、Workspace、Changes、Output。
- 验证与交付：Report、Evidence、Descriptors、Skills 和系统健康摘要。
- 项目与会话：项目注册、会话消息、AskUser 答案、任务草稿确认和修正确认。
- 事件流：迁移事件与会话事件均通过 SSE 提供，事件正文使用版本化事件信封。

写请求要求使用 `Idempotency-Key`。幂等范围为主体、路由和键的组合，正文采用
canonical JSON 比对；相同正文重放首次响应，不同正文返回冲突。取消请求使用
`If-Match` 传递 Run 版本，由 Run actor 执行并发判断。错误统一返回
`application/problem+json`。

SSE 从已提交事件账本读取数据，`Last-Event-ID` 只作为严格的回放游标；通知机制
只负责唤醒，不能作为事件正文来源。连接具有进程级上限和有界待发送队列，终态
事件交付后自动关闭。

## 安全与数据边界

- 源仓库只读，输出工作区与源项目物理隔离。
- HTTP body、Spec 正文、事件队列和检查资源均有明确容量限制。
- 仓库地址、分支前缀、相对路径和工具命令执行面进行严格校验。
- API、日志、事件、指标和报告引用不包含 bearer token、仓库凭据、源码正文、
  完整提示词、宿主路径或完整工具输出。
- 工具链事实通过锁定的声明式描述符提供，不使用可动态注入的语言插件执行任意命令。
- PostgreSQL 保存控制面事实；大对象通过受控 ArtifactRef 引用，不能凭任意引用读取正文。
- 内部设计资料、对齐记录、凭证、参考工程和临时资料不属于发布内容。

## 运行环境

- Python 3.12 或更高版本。
- [uv](https://docs.astral.sh/uv/)：Python 环境和依赖管理工具。
- 支持 Compose 的 Docker：运行本地 app 与 PostgreSQL 服务。
- Linux、bubblewrap 和 cgroup v2：运行 sandbox 相关工作流。

项目的运行时依赖和开发依赖均在 `pyproject.toml` 中声明，推荐使用 `uv.lock`
对应的锁定环境执行检查。运行凭证由部署环境提供，不应写入源码、配置提交或项目文档。

## 安装与运行

在项目根目录执行：

```bash
uv sync --dev
```

启动应用入口：

```bash
uv run codemigrator-app
```

启动本地基础设施时，请从 shell 或未纳入版本控制的环境文件提供凭证：

```bash
POSTGRES_PASSWORD='<由部署环境提供>' \
CODEMIGRATOR_CGROUP_DELEGATED_DIR='/path/to/delegated/cgroup' \
docker compose up --build
```

`compose.yaml` 定义应用与 PostgreSQL 服务；启动 Compose 时必须提供
`POSTGRES_PASSWORD` 和 `CODEMIGRATOR_CGROUP_DELEGATED_DIR`。后者应指向可委派的
cgroup v2 目录。授予额外宿主机能力前，应先审阅部署配置。

## 测试与质量门

优先执行可复现的规则测试、契约测试和边界测试：

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run lint-imports
uv run python -m compileall -q src tests
```

每项代码变更都应覆盖正常路径、边界条件、失败路径、幂等性和安全不变量。涉及
数据库事务、事件序列、恢复和交付适配器的测试，应在隔离的本地基础设施中运行。
真实模型调用只用于验证 provider 特有行为、token 计数或必要的端到端会话，不作为
普通回归测试的默认依赖。

## Descriptor 与扩展约束

`descriptors/` 是语言和工具链事实的声明式来源，可描述 grammar、镜像摘要、能力、
检查集和相关资源引用。descriptor 不得包含凭证、可执行应用逻辑、模型 prompt、
任意命令注入内容或工作区写入范围；这些内容分别由契约、运行时、sandbox 和
workspace 边界负责。Python 包通过已验证的公共契约消费 descriptor 事实。

扩展功能应保持以下原则：

1. 公共状态、错误码和 ID 只在 `core` 定义一份。
2. 外部输入使用封闭 schema，未知字段默认拒绝。
3. 副作用经过明确的端口和组合根，不从纯逻辑包直接访问环境或基础设施。
4. 每项变更同时覆盖成功路径、边界条件、失败路径和安全约束。
5. 可观察事实进入统一事件账本，展示层不自行创造领域状态。

## 贡献约定

领域逻辑应尽量保持纯函数、确定性和最小副作用；环境读取与适配器装配集中在
runtime 组合根。优先复用 `codemigrator.core` 的公共契约，禁止在其他子包中复制
平行枚举或错误码。提交变更前应完成质量门检查，并检查差异中是否包含凭证、内部
工作材料、生成文件或违反依赖边界的导入。

## 许可证

本项目采用 MIT License，完整授权条款见 [LICENSE](LICENSE)。
