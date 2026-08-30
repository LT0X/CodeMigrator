# planning

## 负责

Planner 提案适配、DAG、write scope、集成序和确定性机器校验。

## 不负责

不直接调用模型、不访问文件系统/Git/数据库、不持有运行时状态。

## 允许依赖

允许依赖 `codemigrator.core` 和 `codemigrator.analysis`。

## 公共入口

由后续 CM-PLAN-001 定义 Planner 与校验端口。
