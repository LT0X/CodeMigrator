# CM-WEB-001 对齐记录

> 用途：本文件是任务 `CM-WEB-001`（模块 M-15 Web 体验与可视化工作台）编码前与用户对齐的契约记录，goal 模式下实现 agent 以本文件为自主开发依据。
> 维护规则：问答记录与变更记录 append-only（只增不改）；再对齐须经用户确认并走主任务表 §8 变更流程；禁止写入敏感凭证。

## 0. 元信息

| 字段 | 内容 |
|---|---|
| 任务ID | `CM-WEB-001` |
| 模块编号 | M-15 |
| 对应设计文档 | `my_space/codemigrator_design_doc/architecture_module_design/CodeMigrator_Web体验与可视化工作台.md`（V6 方向对齐版） |
| 主任务表 | `my_space/codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` §7.3 |
| 对齐轮次 | 第 1 轮（Wave 1 mock → Wave 4 真实联调） |
| 对齐日期 | 2026-08-29 |
| 对齐参与者 | 用户 + TRAE agent |
| 对齐状态 | 已对齐 |

## 1. 任务理解

### 1.1 范围（做什么）

交付 `web/`（独立前端 package）+ `apps/codemigrator-cli/`（Python+rich，D-02）：

- **Wave 1 mock 层**（D-01）：
  - Vite+React+TypeScript 项目骨架（M-15 前端包结构：app/features×5/entities/shared×6）。
  - **design token 体系**（复用 v14 原型资产 `ref/codemigrator-web-prototype/`：colors_and_type.css + 吉祥物 SVG + 四状态动画骨架）：四组语义色（--cm-state-verified/agent/waiting/failure+soft ↔ 汇流口/作业区/等待区/重生成位四分区一一对应）、暖白画布、1px 实线边框与留白分层、等宽字体承载 OID/路径/CheckId/错误码；状态=文字+图标+颜色三重表达。
  - **persona 舞台状态机与事件→动作映射表**（`shared/stage/`，唯一真相）：八条舞台规则（persona 绑定 `(slice_id, generation)` 作业会话非固定角色；作业区卡片数=活跃会话数≤并发公式；四动作四场分区一一对应无死角；verified 庆祝一次退场；事件驱动无事件不动画；默认跟随+锁定+脉冲提示；低饱和契约阻塞占位卡；重生成占同一执行预算）；mock 事件流（run_events 样本回放）驱动验证四场分区语义——V-M15-V4-005/007 在 mock 层可单测。
  - **CLI 骨架**：`codemigrator migrate start <spec>` / `run watch` / `--no-follow` / `--output human|json|jsonl` 输出模式、TTY 持久过程流+固定摘要骨架（rich：Header/Process Stream/Live Summary）、exit code 表、Ctrl+C 两段取消语义；消费 mock SSE/REST。
  - 快照/SSE 前端归约骨架（sequence cursor、重复/缺口/未知事件处理、五态连接状态）。
- **Wave 4 真实联调层**（D-01）：接真实 REST/SSE（CM-API 投影）；证据页/报告页/会话输入/系统页完整实现；V6 全局修复会话独立呈现位与 Supervisor 只读决策视图；性能预算达标（200 Slice/500 边/10k 事件/20 活动对象）；reduced-motion/响应式三档/无障碍完善。
- **v14 原型衔接纪律**：复用 7 项（token/吉祥物四动画/胶囊卡片/舞台布局/检查器骨架/ticker/降级）+ 重做 9 项（固定吉祥物常驻→动态 persona；点击驱动→事件驱动；散布胶囊→四场分区；verified 常驻→一次庆祝；V3 术语→V4 事实；同语言场景→跨语言；闭包占位；聚焦策略；证据页）。
- **渲染完备性义务**：新增事件类型必须与定义它的同一变更集交付渲染 case 或可理解 default 呈现（V-M15-V4-029；40.4% 无渲染教训成文）。

### 1.2 边界（不做什么）

- Web 零写通道升级：只读运行投影 + 会话 message/AskUser answer/TaskDraft confirm/ImpactPreview confirm 四类提交；无 CreateRun/Cancel/Git 写/交付重试入口（M-15 产品边界）。
- 不拥有 HTTP 契约（M-02 owner——本任务消费其 7 个只读投影）；不定义公共状态/DTO/指标 descriptor（M-00/M-13 owner）。
- 前端零判定：persona 归属、全局修复集构成、GENERATED 标注、等价信心分级、守恒归因全部消费后端投影，不自行计算。
- 不修改验证/归因/DAG 语义（M-10/M-07 owner，只呈现）。
- 本对齐不冻结状态库选型与 React/Vite/Flow/Motion 具体版本（D-03，留 Web 详细编码设计）。
- CLI 不直接终止 bwrap/写 PG/改 Git；只经 REST（If-Match Cancel）。
- 舞台不呈现 VERIFYING 最终验证 persona（进度走阶段条+时间线）。

### 1.3 产出物

`web/`（Vite 项目、token、shared/stage 映射表+状态机、mock 数据源、组件骨架）；`apps/codemigrator-cli/`（Python+rich：API/SSE client、三种 renderer、sequence cursor、命令层）；tests（舞台状态机单测——mock 事件序列断言四场分区/庆祝一次/不重播）；模块迭代记录。

## 2. 关键实现决策与确认结论

> 仅记录经用户确认的结论；每条注明依据。agent 不得替用户决定。

| # | 决策点 | 可选项 | 用户确认结论 | 依据 |
|---|---|---|---|---|
| D-01 | Wave 1 mock 交付范围 | 骨架+状态机 mock / 仅静态组件 / 全押后 | **骨架+状态机 mock**：Vite+React 骨架、v14 token 复用、舞台状态机+事件映射表（mock 事件流驱动、四场分区语义可单测）、CLI 骨架命令与输出模式；Wave 4 真实联调+证据页完善 | 对齐问答 Q-01（2026-08-29） |
| D-02 | CLI 实现语言与 TTY 框架（M-01 未定语言） | Python+rich / Python+textual / TypeScript 共享 | **Python + rich**：与后端同 uv 工具链零 Node 依赖；rich 支撑 Header+Process Stream+Live Summary 混合过程流；CLI 不进核心子包依赖图不受影响 | 对齐问答 Q-02 |
| D-03 | 前端状态管理方案冻结时机 | 留 Web 详细编码设计 / 现在冻结 | **留详细设计**（M-15 原文明示「不写状态库实现，进入后续 Web 详细编码设计」）；本对齐只锁技术基线（React/TS/Vite/React Flow/Motion）与事件归约架构 | 对齐问答 Q-03 |

## 3. 接口与依赖契约摘要

### 3.1 上游输入

- CM-API（M-02）七个只读投影：GET migrations{,/{id},/{id}/workspace,/{id}/events,/{id}/report,/{id}/evidence/{receipt_id},/system/health} + SSE `migration.event` v1 信封。
- 事件词汇（RunEventType 常量，api owner——CM-API-001 D-05）：本任务 `shared/stage/` 映射表消费其全部 V5+V6 值。
- v14 原型资产（`ref/codemigrator-web-prototype/pages/run-workspace-v14.html` + `colors_and_type.css` + `吉祥物.html`——对齐时已确认在仓）。

### 3.2 下游消费

- 终端用户（CLI 主操作+Web 观察）；M-13 观测与 Web 无耦合（Web 不消费观测栈）。

### 3.3 跨模块接口边界

- 事件→动作映射表是前端唯一舞台真相（M-15 表格逐行实现）；新增事件类型只扩展映射表行 + 渲染完备性义务（V-M15-V4-029）。
- advice.*/repair.* 事件 = 可选默认呈现（独立会话呈现位 / EXECUTE 只读决策视图），不插入 Slice 四场分区（M-15 V6 注）。
- CLI `--output` 三模式只改渲染不改订阅/sequence/权限；exit code 表（0/2/3/4/5/130）冻结。
- workspace 聚合是展示投影非运行真相（M-02 owner 语义）。
- `migration.session.event` 持久 vs `assistant.delta` 短暂——delta 不推进舞台状态（V-M15-V4-022）。

## 4. 验收条款映射

| 条款 | 内容摘要 | 验证方式 |
|---|---|---|
| V-M15-V4-003/004（追溯） | persona 绑定当前作业 Slice 动态转场；作业区卡片数=活跃会话数≤公式上限 | mock 事件序列单测（Wave 1 可测） |
| V-M15-V4-005（追溯） | 四动作四场分区无死角；每 SliceAttemptStatus 唯一分区归属 | 状态机单测（全状态枚举断言） |
| V-M15-V4-006（追溯） | verified 庆祝仅由 integration.completed+verified.advanced 触发、每 generation 至多一次；重放不重播 | mock 单测 |
| V-M15-V4-007（追溯） | 全部动作由事件按 sequence 归约驱动；关事件源零变化 | mock 单测 |
| V-M15-V4-008（追溯） | 默认跟随/锁定/脉冲提示/Esc 解锁 | 组件测试 |
| V-M15-V4-015/016 | 补读收敛不重演；深链接恢复焦点与锁定态且零敏感泄漏 | Wave 4 集成测试 |
| V-M15-V4-010/011/012 | 冻结集成序展示稳定；三层验证可区分；NONDETERMINISTIC 分叉提示 | Wave 4 联调 |
| V-M15-V4-013/014/017/024~028 | 证据页全区块只读投影、部分完成诚实呈现、四失败分离、GENERATED 徽标、双档分级、边界声明、守恒归因展示 | Wave 4 验收 |
| V-M15-V4-018/019 | reduced-motion 降级；性能预算（200/500/10k/20） | Wave 4 |
| V-M15-V4-020/021/022 | CLI 归约/四活动槽聚合/Ctrl+C 两段取消/exit code/delta 不推进 | Wave 1 骨架单测 + Wave 4 联调 |
| V-M15-V4-023 | 零 V3 残留扫描（固定吉祥物仅允许出现在重做清单） | 静态扫描（Wave 1 起） |
| V-M15-V4-029 | 渲染完备性：新事件类型同变更集交付渲染 case 或 default | 映射表契约测试（每个 RunEventType 值断言有映射行或 default）——Wave 1 可测 |
| V5/V6 增量 | 起草/规划/执行/证据视图；全局修复独立呈现位+Supervisor 只读决策视图+零新写通道 | Wave 4 |

## 5. 风险与注意点

- **v14 资产复用纪律**：九项「必须重做」不得混入复用（尤其固定吉祥物常驻与点击驱动状态——这是 V3 残留红线，V-M15-V4-023 扫描对象）。
- **映射表完整性**：RunEventType 常量全集（含 V6 advice/repair）每个值必须有映射行或显式 default——渲染完备性验收以契约测试固化（勿出现"入流不可见"事件）。
- **CLI Python 版本一致性**：apps/codemigrator-cli 与主包同 uv 管理（Python 3.12）；rich 依赖进 pyproject（CM-INFRA 联动）。
- **mock 数据源纪律**：mock 事件流样本须从 M-15 映射表+M-02 事件集语义生成（含缺口/重复/未知事件/乱序 arrival 用例），勿造假数据误导状态机测试。
- **SSE 客户端**：Wave 1 mock 层先固化 sequence cursor/重复/缺口补读/五态连接的纯逻辑（可测），Wave 4 换真实 EventSource 实现时逻辑零改动。
- 用户 UI 偏好与 M-15 视觉基线一致（极简/高质量实线/清晰交互反馈）——token 实现不得引入阴影渐变堆叠。
- 状态库与具体版本冻结归 Web 详细编码设计（D-03）——goal 模式实现 agent 在 Wave 1 不得擅自引入状态库依赖。

## 6. 对齐问答记录

> append-only：每轮对齐的问题与用户结论。

| # | 问题 | 用户结论 |
|---|---|---|
| Q-01 | CM-WEB-001 Wave 1 mock 交付范围 | 骨架+状态机 mock（四场分区语义可单测；真实联调归 Wave 4） |
| Q-02 | apps/codemigrator-cli 实现语言与 TTY 框架 | Python + rich |
| Q-03 | 前端状态管理方案冻结时机 | 留 Web 详细编码设计（M-15 纪律；本对齐只锁基线与归约架构） |

## 7. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-29 | 建立记录 | 依据 M-15 V6 方向对齐版设计文档（舞台八规则/映射表/v14 衔接/CLI 节）与主任务表 §7.3；v14 原型资产在仓已核实；用户经提问工具逐项确认 D-01~D-03 |
