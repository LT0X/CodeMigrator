# core

## 负责

稳定公共 ID、枚举、错误码、Pydantic 契约、纯函数和版本化静态策略资源。

## 不负责

不负责 HTTP、SQL、Git、进程、环境读取、语言 grammar 或领域编排。

## 允许依赖

仅使用标准库及 `pyproject.toml` 声明的公共契约依赖；不依赖其他 `codemigrator` 子包。

## 公共入口

从 `codemigrator.core` 导出公共类型和策略加载器。
