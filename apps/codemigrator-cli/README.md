# CodeMigrator CLI

CLI 是 CodeMigrator 的终端入口，面向本地演示、Run 跟踪、自动化流水线和明确退出码。
它以 Python 3.12 与 Rich 为基础，所有 renderer 共用事件归约和脱敏边界。

## 安装与使用

```bash
uv pip install -e apps/codemigrator-cli
codemigrator migrate start path/to/spec.json --follow --output human
codemigrator migrate start path/to/spec.json --no-follow --output json
codemigrator run create path/to/create-run.json --idempotency-key draft-confirmation-17
codemigrator run watch <run_id> --follow --output jsonl
codemigrator run show <run_id> --output json
codemigrator run cancel <run_id> --if-match <version>
```

`migrate start` 始终运行不依赖服务的确定性本地演示，不读取或上传 spec；配置
`CODEMIGRATOR_API_URL` 与 `CODEMIGRATOR_API_TOKEN` 后，`run create`、`run watch`、`run show` 和
`run cancel` 使用认证 REST/SSE 适配器。`run create` 将请求文件中的既有 `CreateRun` JSON 提交到
`POST /api/v1/migrations`，并发送用户提供的 `Idempotency-Key`；请求体由 API 校验，响应只投影
Run ID、状态、版本和站内链接。要重试同一次创建，请复用同一个幂等键。

host 配齐生产组件后，Run 创建由 `RunCreationOwner` 和 durable `RunWorkflowGraph` 执行，PLAN 阶段使用
`PlanAgentRun`、M-07 验证与 Actor receipt gate。CLI 只提交已经冻结的创建请求，不在本地组装 Run、Actor、
PlanAgentRun 或 receipt。创建请求的 Draft/项目快照物化，以及 host 的 preflight、规划输入 loader、
ToolGateway、Actor factory 和阶段 services，仍须由生产 host 提供；缺少这些能力时 API 必须 fail closed。

`migrate project` 中的 `full` V6 文件流水线和 `legacy` runner 仅用于显式兼容场景，必须传入
`--workflow full` 或 `--workflow legacy`。两者不启动生产 Run graph。输出不包含模型推理、提示词、源码正文、
完整日志、宿主路径、ArtifactRef 或凭据。

取消命令只提交带 `If-Match` 版本的取消请求并等待 Run actor 的持久化确认；它不会在本地
直接终止沙箱、写数据库或修改 Git。
