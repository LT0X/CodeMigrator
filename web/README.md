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

入口通过 `shared/api` 消费 REST/SSE 运行投影；`/demo` 提供确定性的本地演示事件源，
用于验证 sequence cursor、重复/缺口事件处理、四场动作归约、聚焦锁定和 verified 一次性
庆祝。组件不创建第二套运行状态，也不把演示数据当作服务端结论。
