# api

## 负责

REST/SSE 外部投影、If-Match、幂等命令和 RFC 9457 错误映射。

## 不负责

不直接访问 SQL/Git/进程，不实现领域状态归约。

## 允许依赖

仅消费 `codemigrator.core` 契约并经 runtime 组合根绑定控制面端口。

## 公共入口

由后续 CM-API-001 定义 FastAPI application factory 和 HTTP DTO。
