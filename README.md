# CodeMigrator

CodeMigrator 是一个面向跨语言代码迁移的确定性、可审计 Agent 系统。它以冻结的源项目快照、声明式工具链描述符和结构化迁移工件为输入，通过规划、受控执行、分层验证与证据化交付，生成可复核的目标项目。

项目强调“事实先于叙述”：迁移过程中的状态、事件、检查结果、集成回执和交付状态均以结构化事实保存；模型只参与被明确授权的会话与决策环节，不能绕过安全边界直接修改源项目、控制面或交付历史。

## 核心能力

- **跨语言迁移**：以源语言和目标语言描述符为边界，支持从源项目快照生成目标项目。
- **确定性规划**：根据模块、依赖、测试覆盖和输出路径生成可校验的 Slice DAG，并冻结集成顺序。
- **受控 Agent 执行**：模型通过受限工具网关访问候选工作区，所有文件写入、Shell 调用和检查执行均受安全策略约束。
- **分层验证**：支持 Scaffold、Compile、Test、Lint、TypeCheck 等检查，并将诊断归因到具体 Slice 或测试迁移单元。
- **可恢复运行**：Run、Slice、候选代次、验证回执和 `run_events` 形成可重建账本；进程重启或执行中断不以进程内状态冒充已提交事实。
- **证据化交付**：验证结果、变更摘要、报告和代码交付状态分离记录，可独立重试和审计。
- **REST/SSE 控制面**：CLI 与 Web 共享同一组 REST 投影和 `migration.event` v1 事件流，支持严格游标回放与断线恢复。

## 工作流概览

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

源快照始终只读。目标代码写入由系统托管的输出工作区，目标 Git 历史从空输出基线开始；源仓库历史、宿主路径、凭据和大对象正文不会通过公共 API 泄露。

## 系统结构

CodeMigrator 采用 Python `src` 布局，并以清晰的依赖边界组织核心能力：

| 目录 | 职责 |
| --- | --- |
| `src/codemigrator/core/` | 公共 ID、状态、错误码、Spec、路径规范和无副作用领域契约 |
| `src/codemigrator/analysis/` | 源项目结构分析、语法森林、项目索引、覆盖映射和 AST 查询 |
| `src/codemigrator/planning/` | Slice 派生、DAG、write scope、集成顺序和计划校验 |
| `src/codemigrator/workspace/` | 候选工作区、checkpoint、Git 输出历史和集成原语 |
| `src/codemigrator/sandbox/` | 冻结命令面、资源限制、bubblewrap 生命周期和执行隔离 |
| `src/codemigrator/verification/` | 检查执行结果归一、诊断解析、指纹与失败归因 |
| `src/codemigrator/runtime/` | Run actor、调度、会话编排、恢复、事务适配和组合根 |
| `src/codemigrator/api/` | REST/SSE DTO、认证、幂等、If-Match、事件回放和 Problem Details |
| `descriptors/` | 源端/目标端语言与工具链的声明式资源 |
| `apps/codemigrator-cli/` | CLI 产品入口 |
| `web/` | Web 产品入口 |
| `migrations/` | PostgreSQL 版本化迁移脚本 |
| `deploy/` | 应用、数据库和沙箱的部署基线 |

核心域包保持纯逻辑或受控边界；SQL、Git、进程和环境配置由 runtime 组合根持有。API 只消费注入端口，不直接连接数据库、操作 Git 或启动执行进程。

## HTTP API

API 默认以 `/api/v1` 为前缀，使用单一部署令牌和固定主体。主要资源包括：

- Spec 与迁移：创建 Spec、创建/取消 Run、Run 列表与详情、Workspace、Changes、Output。
- 验证与交付：Report、Evidence、Descriptors、Skills、系统健康摘要。
- 项目与会话：项目注册、会话消息、AskUser 答案、任务草稿确认和修正确认。
- 事件流：迁移事件与会话事件均通过 SSE 提供，事件正文使用 `migration.event` v1 六字段信封。

写请求要求使用 `Idempotency-Key`。幂等范围为主体、路由和键的组合，正文采用 canonical JSON 比对；相同正文重放首次响应，不同正文返回冲突。取消请求使用 `If-Match` 传递 Run 版本，由 Run actor 执行并发判断。错误统一返回 `application/problem+json`。

SSE 从已提交事件账本读取数据，`Last-Event-ID` 只作为严格的回放游标；通知机制只负责唤醒，不能作为事件正文来源。连接具有进程级上限和有界待发送队列，终态事件交付后自动关闭。

## 安全与数据边界

- 源仓库只读，输出工作区与源项目物理隔离。
- HTTP body、Spec 正文、事件队列和检查资源均有明确容量限制。
- 仓库地址、分支前缀、相对路径和工具命令执行面进行严格校验。
- API、日志、事件、指标和报告引用不包含 bearer token、仓库凭据、源码正文、完整提示词、宿主路径或完整工具输出。
- 工具链事实通过锁定的声明式描述符提供，不使用可动态注入的语言插件执行任意命令。
- PostgreSQL 保存控制面事实；大对象通过受控 ArtifactRef 引用，不能凭任意引用读取正文。
- 私有设计、对齐记录、凭证、参考工程和临时资料仅用于本地工作空间，不属于发布内容。

## 环境要求

- Python 3.12 或更高版本
- `uv`：依赖与锁文件管理
- Docker Compose：运行 PostgreSQL 等本地基础设施
- Linux 环境：运行 bubblewrap、cgroup v2 和沙箱相关验证

本项目的运行凭证由部署环境注入，不应写入源码、配置提交或文档。

## 安装与运行

使用锁定依赖创建开发环境：

```bash
uv sync --dev
```

启动默认应用入口：

```bash
uv run codemigrator-app
```

启动本地基础设施时，请在仓库外提供数据库密码等凭证，再执行：

```bash
POSTGRES_PASSWORD='<由部署环境提供>' docker compose up --build
```

实际部署参数、数据库连接和模型提供方配置应由部署系统或本机凭证管理器提供；仓库不提供默认秘密值。

## 测试与质量检查

确定性规则测试优先于真实模型调用。常用检查命令如下：

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run lint-imports
python -m compileall -q src tests
```

涉及数据库事务、事件序列、恢复和交付适配器的测试，应在隔离的本地基础设施中运行。真实模型调用只用于验证 provider 行为、token 计数或必要的端到端会话，不作为普通回归测试的默认依赖。

## 描述符与扩展

语言和工具链差异应通过 `descriptors/` 中的声明式资源表达，包括语言标识、grammar、检查模板、资源摘要、镜像摘要和安全能力。核心 Python 包不通过新增语言插件分支承载语言知识；规划器与执行器消费已锁定的描述符事实。

扩展功能应保持以下原则：

1. 公共状态、错误码和 ID 只在 `core` 定义一份。
2. 外部输入使用封闭 schema，未知字段默认拒绝。
3. 副作用经过明确的端口和组合根，不从纯逻辑包直接访问环境或基础设施。
4. 每项变更同时覆盖成功路径、边界条件、失败路径和安全约束。
5. 可观察事实进入统一事件账本，展示层不自行创造领域状态。

## 项目许可

许可证信息以仓库发布时附带的许可文件和发行说明为准。
