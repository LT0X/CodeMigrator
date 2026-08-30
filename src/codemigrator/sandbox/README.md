# sandbox

## 负责

app 内 bubblewrap/cgroup 适配、长期卷、验证临时物化和资源边界。

## 不负责

不作为独立服务、不连接 PostgreSQL、不定义 Run/Slice 状态或验证归约。

## 允许依赖

允许依赖 `codemigrator.core` 与受控系统调用适配；不依赖 API/数据库。

## 公共入口

由后续 CM-SANDBOX-001 定义 app 内 sandbox 适配端口。
