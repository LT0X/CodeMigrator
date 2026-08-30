# CodeMigrator CLI

CLI 是 CodeMigrator 的终端观察入口，面向迁移创建、Run 跟踪、自动化流水线和明确退出码。
它以 Python 3.12 与 Rich 为基础，所有 renderer 共用事件归约和脱敏边界。

## 安装与使用

```bash
uv pip install -e apps/codemigrator-cli
codemigrator migrate start path/to/spec.json --follow --output human
codemigrator migrate start path/to/spec.json --no-follow --output json
codemigrator run watch <run_id> --follow --output jsonl
codemigrator run show <run_id> --output json
codemigrator run cancel <run_id> --if-match <version>
```

无服务配置时，CLI 使用确定性的本地 source 进行规则验证；配置
`CODEMIGRATOR_API_URL` 与 `CODEMIGRATOR_API_TOKEN` 后，`run watch`、`run show` 和
`run cancel` 使用认证 REST/SSE 适配器。两种 source 共用同一 `EventSource` 边界。输出不包含模型推理、提示词、源码正文、完整日志、
宿主路径、ArtifactRef 或凭据。

取消命令只提交带 `If-Match` 版本的取消请求并等待 Run actor 的持久化确认；它不会在本地
直接终止沙箱、写数据库或修改 Git。
