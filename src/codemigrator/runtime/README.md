# runtime

## 负责

Run actor、事务编排、调度、集成、恢复、观测装配和唯一 app 组合根。

## 不负责

不复制 HTTP DTO、语言 grammar 或 sandbox 进程实现细节。

## 允许依赖

可组合其他七个 `codemigrator` 子包，并由此持有控制面副作用。

## 公共入口

唯一 console script `codemigrator-app = codemigrator.runtime:main`。
