# workspace

## 负责

候选工作区、checkpoint、工具网关和受控文件/Git 执行面。

## 不负责

不做验证通过裁决、不构造命令面外参数、不越过冻结 write scope。

## 允许依赖

允许依赖 `codemigrator.core` 和 `codemigrator.sandbox`。

## 公共入口

CM-WORKSPACE-001 提供 `WorkspaceManager`、`ToolGateway`、`CheckpointService`、安全根路径原语、六工具端口、动作协议和 GeneratedCode scaffold 端口；真实 bwrap、Git、PSF、QuickJS 与数据库适配由对应 owner 注入。
