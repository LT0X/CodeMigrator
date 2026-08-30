# core

## 负责

稳定公共 ID、枚举、错误码、Pydantic 契约、纯函数和版本化静态策略资源。

## 不负责

不负责 HTTP、SQL、Git、进程、环境读取、语言 grammar 或领域编排。

## 允许依赖

仅使用标准库及 `pyproject.toml` 声明的公共契约依赖；不依赖其他 `codemigrator` 子包。

## 公共入口

从 `codemigrator.core` 导出公共类型和策略加载器。

## 观测契约

`SecretRegistry` 是跨 API、日志、事件和 exporter 的只写脱敏契约，负责四种编码扫描、敏感字段结构过滤和 fail-closed 结果；它不提供 secret 枚举或读回能力。`CORE_METRIC_DESCRIPTORS` 发布八项固定指标的名称、标签值域、Histogram bucket 和 descriptor hash，诊断指标与核心集合分离，避免运行时装配改变公共契约。
