# CodeMigrator Web 工作台

这是 CodeMigrator 的浏览器观察入口。它以 REST/SSE 运行投影为事实来源，展示迁移
汇流场、Slice 状态、验证过程和 Verified 主线；不直接修改代码、Git、Run 或交付状态。

## 本地运行

```bash
npm ci
npm run dev
```

检查与构建：

```bash
npm test -- --run
npm run typecheck
npm run build
```

当前入口包含确定性的 mock 事件源，用于验证 sequence cursor、重复/缺口事件处理、四场
动作归约、聚焦锁定和 verified 一次性庆祝。真实 REST/SSE 接入应替换 `shared/api` 的
transport 边界，不应在组件中创建第二套运行状态。
