# CodeMigrator

CodeMigrator 是一个面向跨语言源代码迁移的确定性、证据驱动型系统。它将
源代码分析、迁移规格约束、迁移计划、候选工作区、受控工具调用、隔离执行、
多层验证和可审计运行编排组织为一套统一的工程体系。

CodeMigrator 不把迁移理解为不受约束的代码生成，而是把一次迁移建模为一组
可冻结、可验证、可回放的事实与操作：输入规格和工具链先经过契约校验，计划
被拆分为有依赖关系且具有明确写入边界的切片，候选修改通过受控工作区产生，
执行结果以回执和证据保存，只有满足验证条件的结果才能进入后续集成与交付。

## 项目目标

- 为跨语言迁移提供统一的规格、计划、执行和验证抽象。
- 以规范化契约、稳定标识和显式状态转换保证结果可重复。
- 以证据、诊断归因和验证指纹支撑失败分析与安全修复。
- 以冻结的 write scope 限制所有候选工作区写入。
- 通过 sandbox、资源池和受控工具网关隔离高风险操作。
- 通过事件账本、执行回执和集成事实保留完整的审计链路。
- 通过声明式 descriptor 支持不同语言与工具链，避免把语言插件逻辑散落在
  Python 包中。

## 设计原则

### 确定性优先

公共 ID、枚举、错误码、Pydantic 数据契约、规范化序列化、路径规则和策略资源
由 `codemigrator.core` 统一定义。领域层优先使用纯函数和不可变输入，避免同一
事实在不同模块中产生不一致的解释。

### 证据驱动推进

候选版本、验证结果和集成状态只能由受信执行事实、结构化诊断、验证 guard 与
集成回执推动。原始输出与派生结论分离保存，派生结论不能替代原始执行事实。

### 写入边界明确

迁移计划冻结后，每个 Slice 都拥有明确的文件写入范围。工作区、工具网关和
Git 操作必须遵循该范围；模型输出不能直接获得任意文件系统写权限。

### 组合根集中副作用

环境读取、数据库连接、进程生命周期、sandbox、Git、模型 provider 和观测适配器
由 runtime 组合根装配。分析、规划和验证等领域包保持可测试、可复用的边界，
不自行连接数据库或启动外部进程。

### 安全失败

契约缺失、资源事实不完整、版本冲突、诊断无法归因、执行回执迟到或 checkpoint
损坏时，系统采取拒绝推进、记录审计事实或重建的策略，不把不确定性解释为成功。

## 系统架构

项目采用 Python `src` 布局和八个边界清晰的子包。依赖方向以 `core` 为底层
契约中心，`runtime` 负责组合各类端口和适配器，`api` 负责外部投影与命令入口。

| 子包 | 架构职责 |
| --- | --- |
| `codemigrator.core` | 稳定 ID、枚举、错误码、Pydantic 契约、canonicalization、路径与 scope 原语，以及版本化策略资源。 |
| `codemigrator.analysis` | 基于 tree-sitter 的只读源端分析，构建模块、依赖、测试覆盖图并提供分析投影。 |
| `codemigrator.planning` | 对迁移提案执行确定性校验，将合格提案冻结为有向无环计划、Slice 和依赖边。 |
| `codemigrator.workspace` | 候选工作区、checkpoint、安全路径、写入范围、工具网关及受控文件/Git 操作边界。 |
| `codemigrator.sandbox` | app 内 bubblewrap/cgroup 适配、长期卷、验证临时目录和资源限制。 |
| `codemigrator.verification` | 检查集筛选、执行事实归一、诊断解析、归因证据、验证 fingerprint 与 guard。 |
| `codemigrator.runtime` | Run actor、事务编排、调度、集成、恢复、生命周期和应用组合根。 |
| `codemigrator.api` | REST/SSE 外部投影、幂等命令、`If-Match` 版本控制和 RFC 9457 错误映射。 |

Run 的控制面遵循单 Run 单写者原则：actor 串行接收 typed mailbox 消息并作出
状态决定；模型、工具、Git、sandbox 和检查命令等耗时操作通过端口在 actor 外部
执行，再以带身份信息的回执返回。API 只提交命令和读取投影，不绕过 runtime 直接
修改领域事实。

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

各阶段的状态转换、失败原因、幂等键、事件顺序和验收语义以 `core` 契约及相应
模块设计为准。

## 目录结构

```text
.
├── apps/codemigrator-cli/       应用入口资产
├── descriptors/                源端与目标端工具链声明式事实
├── deploy/                     容器、目标工具链和 sandbox 部署资产
├── migrations/                 PostgreSQL 版本化迁移脚本
├── src/codemigrator/           Python 应用包
├── test_fixtures/              确定性分析与契约测试夹具
├── tests/                      单元测试、契约测试和边界测试
├── compose.yaml                本地 app 与 PostgreSQL 服务
├── pyproject.toml              构建、依赖、测试、lint 和类型检查配置
└── uv.lock                     锁定的 Python 依赖解析结果
```

设计文档、需求对齐记录、迭代记录、临时文件、凭证、agent 指令和其他内部工作
材料属于本地工作区，不是项目源代码的一部分，不能提交或通过 `git add -f`
强制加入版本库。

## 运行环境

- Python 3.12 或更高版本。
- [uv](https://docs.astral.sh/uv/)：Python 环境和依赖管理工具。
- 支持 Compose 的 Docker：运行本地 app 与 PostgreSQL 服务。
- Linux、bubblewrap 和 cgroup v2：运行 sandbox 相关工作流。

项目的运行时依赖和开发依赖均在 `pyproject.toml` 中声明，推荐使用 `uv.lock`
对应的锁定环境执行检查。

## 安装与入口

在项目根目录执行：

```bash
uv sync --dev
```

验证包可以被正确导入：

```bash
uv run python -c "import codemigrator"
```

应用控制台入口由项目脚本提供：

```bash
uv run codemigrator-app
```

环境变量、数据库凭证和宿主机路径必须由本地运行环境提供，不得写入源码、
descriptor、Compose 文件或项目文档。

## 本地服务

`compose.yaml` 定义两个服务：

- `app`：CodeMigrator 应用容器，包含部署模型要求的 sandbox 权限和 delegated
  cgroup 挂载。
- `postgres`：PostgreSQL 17，用于控制面账本、事件历史和相关持久化事实。

示例（敏感值只从 shell 或未纳入版本控制的本地环境文件提供）：

```bash
POSTGRES_PASSWORD='change-me' \
CODEMIGRATOR_CGROUP_DELEGATED_DIR='/path/to/delegated/cgroup' \
docker compose up --build
```

`POSTGRES_PASSWORD` 是数据库服务的必填项；启用 sandbox 生命周期管理时，
`CODEMIGRATOR_CGROUP_DELEGATED_DIR` 必须指向可委派的 cgroup v2 目录。授予额外
宿主机能力前，应先审阅 `compose.yaml` 与 `deploy/` 中的部署配置。

## 测试与质量门

优先执行可复现的规则测试、契约测试和边界测试：

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run lint-imports
uv run python -m compileall -q src
```

按模块执行聚焦测试：

```bash
uv run pytest -q tests/core
uv run pytest -q tests/verification
```

每项代码变更都应覆盖正常路径、边界条件、失败路径、幂等性和安全不变量。
只有在规则测试和本地替身无法验证 provider 特有行为时，才使用最小范围的真实
模型调用，例如确认 provider usage 回执或完整模型会话行为。

## Descriptor 约束

`descriptors/` 是语言和工具链事实的声明式来源，可描述 grammar、镜像摘要、
能力、检查集和相关资源引用。descriptor 不得包含凭证、可执行应用逻辑、模型
prompt、任意命令注入内容或工作区写入范围；这些内容分别由契约、运行时、sandbox
和 workspace 边界负责。Python 包通过已验证的公共契约消费 descriptor 事实。

## 安全边界

CodeMigrator 将源仓库、模型输出、生成文件、工具输出、执行回执和验证结论视为
不同信任域：

- 模型输出在进入计划或工作区动作前必须经过契约和能力校验。
- 文件操作仅允许安全的仓库相对路径，并受冻结 write scope 约束。
- sandbox 工作负载不应获得 PostgreSQL、宿主 Git 或控制面网络访问。
- 大型日志和正文通过受控 artifact reference 管理，事件只保留必要摘要。
- 凭证、内部设计材料和本地运行规则必须被版本控制与 Docker build context 排除。
- 修改契约、写入边界、sandbox 权限、数据库 schema 或事件语义时，必须同步更新
  测试和文档。

## 贡献约定

领域逻辑应尽量保持纯函数、确定性和最小副作用；环境读取与适配器装配集中在
runtime 组合根。优先复用 `codemigrator.core` 的公共契约，禁止在其他子包中
复制平行枚举或错误码。提交变更前应完成质量门检查，并检查差异中是否包含凭证、
本地工作区材料、生成文件或违反依赖边界的导入。

## 许可证

本项目采用 MIT License，完整授权条款见 [LICENSE](LICENSE)。
