# verification

## 负责

三层验证归约、诊断归因、验证 fingerprint 和修复证据。

## 不负责

不启动进程、不推进 Git ref、不连接数据库、不直接写 run_events。

## 允许依赖

仅依赖 `codemigrator.core` 公共契约和受信执行端口类型。

## 公共入口

由后续 CM-VERIFY-001 定义纯验证归约端口。
