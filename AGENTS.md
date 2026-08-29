# 项目规则 (AGENTS.md)

> 角色：负责 CodeMigrator（跨语言代码迁移 Agent 系统）的实现。
> 核心工作目录：`crates/codemigrator-*`（核心代码，见 M-01）、`descriptors/`（双工具链声明式资源）、`apps/codemigrator-cli` 与 `web/`（产品入口）、`migrations/`（PostgreSQL schema）。
> 契约真相：`my_space/codemigrator_design_doc/architecture_module_design/`（M-00～M-16）。设计文档可能演进，一切实现与解释以文档当前版本为准。

***

## 1. 工作空间

### 1.1 个人空间 `my_space/`（绝对路径：`/home/dev/project/CodeMigrator/my_space`，已 gitignore，仅本机可见）

> 用途：存放辅助文件、设计文档、进度记录、临时文件。敏感凭证统一存放于此，**不得写入 AGENTS.md**。
>
> goal 模式不允许使用提问工具。

| 子目录                                                    | 用途                          | 关键约束                                                      |
| ------------------------------------------------------ | --------------------------- | --------------------------------------------------------- |
| `codemigrator_dev_progress/`                           | 迭代更新记录，供各 agent 快速了解进度      | 按 §1.3 分类命名；记录内容按 `CodeMigrator迭代记录模板.md` 生成              |
| `codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md` | 整体任务规划与进度跟踪主表               | 任务新增/调序先经提问工具与用户确认                                        |
| `codemigrator_design_doc/architecture_module_design/`  | 整体架构与模块设计文档                 | 平铺存放；修改先用提问工具与用户对齐                                        |
| `codemigrator_design_doc/detailed_coding_design/`      | 详细编码与实现设计文档                 | 按 §1.3 分类命名；内容按 `CodeMigrator详细设计模板.md` 生成；更新时同步相关文档与迭代记录 |
| `codemigrator_design_doc/feedback_doc/`                | 历史评审反馈与对齐记录                 | 只读，不改写既有内容                                                |
| `temp/`                                                | 临时文件、临时代码、环境脚本              | —                                                         |
| `Implementation_plan_doc/`                             | agent 工作前生成的执行/实施计划文档       | 按 §1.3 分类命名；内容按 `CodeMigrator实施计划模板.md` 生成                |
| `.env`                                                 | 本机环境与基础设施凭证（Docker 服务/DB 等） | 有环境更新须在此记录                                                |
| `model_api_key.json`                                   | 可真实调用大模型的 API key           | 仅本机使用                                                     |
| `pr_md/`                                               | PR 说明文档                     | 书写前先读 `pr_md/README.md` 规范                                |

### 1.2 开发环境

**总体形态**：编程、基础设施（PostgreSQL 等 Docker 服务）与沙箱验证统一在本地 WSL2（Ubuntu 24.04）单环境完成。sandbox-worker 仅可运行于 Linux（bubblewrap/cgroup v2）；本机内核 ≥5.15、systemd 与 cgroup v2 均满足运行前提，沙箱相关验证直接在本机完成。

**环境凭证统一路径**：`my_space/.env`。

**可供测试接入的大模型和 key**：`my_space/model_api_key.json`。需要真实调用大模型测试时使用。

#### 本机环境（WSL2 Ubuntu）

- 环境根目录：`~/env`
- **安装新环境前**：先检查 `~/env` 下是否已有对应文件夹；无则先创建子文件夹隔离，**禁止在** **`~/env`** **根目录直接安装**。

#### 本机基础设施访问

- Docker 服务端口直接通过 localhost 访问，**无需 SSH 隧道**。
- 基础设施以仓库根 `compose.yaml` 为准。
- **环境或配置更新后**：须在 `my_space/.env` 记录。

### 1.3 文档目录分类与命名规范

以下三个文档目录共用同一套任务模块分类文件夹，保持一一对应：

- `codemigrator_dev_progress/`（迭代记录）
- `Implementation_plan_doc/`（实施计划）
- `codemigrator_design_doc/detailed_coding_design/`（详细设计）

**分类规则：**

- 以任务所属模块名为分类文件夹名（对照 `architecture_module_design/` 中模块编号 M-00～M-16 及 crate 名取简短缩写，如 `sandbox`、`planning`；跨模块基础设施归 `infra`）。
- 新任务须**先判断归属哪个模块**，存在则直接用，不存在则新建对应分类文件夹（三目录同步）。
- 分类文件夹下**不得再随意新建子文件夹**。

**命名规范：** `CM-<模块缩写>-<序号>-<描述><后缀>.md`

- 后缀按目录区分：迭代记录 `_迭代记录`；实施计划 `实施计划`；详细设计 `详细设计`。

**例外：**

- `architecture_module_design/` 为整体架构文档，**不按模块分类**，平铺存放。
- `codemigrator_dev_progress/` 根目录的全局文件（主跟踪表、迭代模板、其他更新记录）不进模块子目录。

***

## 2. 行为约束（必须遵守）

### 2.1 知识解释原则

- 解释代码仓库或设计**必须先阅读源码相关部分与对应设计文档**，做到每条解释都有依据，**禁止凭空解释**。
- 公共契约（状态机、枚举、错误码等）以设计文档为唯一来源，**只允许引用，禁止在代码中复制第二套定义**。

### 2.2 文档更新边界

- **允许更新**：`my_space/` 与 `crates/`、`apps/`、`web/`、`descriptors/` 下的文档。
- **禁止更新**：其他合作者的文档。
- **架构模块设计文档**（M-00～M-16）修改前，**须先使用提问工具与用户确认**。
- 根目录其他文件夹（如 `migrations/`、`deploy/`）如需修改，**须先使用提问工具与用户确认**后再动。

### 2.3 Git 工作流

- 新功能/修复从 `develop` 切出，分支命名：`feature/<模块缩写>-<简述>` 或 `fix/<模块缩写>-<简述>`。
- 合并前须在 feature/fix 分支通过编译与本地测试。
- **禁止**直接 push 到 `main`。
- **不主动**执行 git commit/push，除非用户明确要求。
- **禁止** `git push --force` 到主干分支。

### 2.4 Commit 规范

- 格式：`<type>: <简要描述>`
- type ∈ `feat | fix | refactor | test | docs | chore`
- 代码更新**只允许在 feature/fix 分支**进行，**禁止直接提交到 develop 与 main**。

### 2.5 对齐机制（所有 agent 必须遵守）

- 执行任务遇需对齐情况，**随时使用提问工具**对齐，确保任务不偏离（goal 模式下除外）。

### 2.6 编码与设计原则

- 编码和设计**尽可能选择优雅且有效的方案**，避免冗余实现与过度设计。

  <br />

### 2.6.1 思想要求：编码优雅与测试完备

- **编码优雅**：追求清晰、克制的实现——命名准确、结构对称、逻辑最小，每处代码都值得保留。
- **测试完备**：测试与代码同权，边界与失败路径必须覆盖；杜绝"能跑但没测"。
- **验证以规则测试为主**：优先用确定性单测/契约测试/回归测试锁定行为（可复现、零成本、快反馈）。
- **真实模型测试必要才做**：仅当需验证 token 计数、provider 行为或端到端模型会话时，才占用真实模型调用；以最小必要调用换取最高信息量，避免高成本低回报。

### 2.7 Skill 匹配：

- 工作前或工作过程中时，必须先查看 `.dsh/skills` 是否有匹配技能文件；匹配则**必须**调用 skill 完成任务（可能需多个 skill 搭配）。

***

## 3. 任务执行工作流(必须遵守)

### 3.1 任务领取与进度查询

1. 查看当前进度：读 `codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md`（任务规划+进度）及 `codemigrator_dev_progress/` 下任务对应模块的记录文档，可根据文件夹时间排序查看最新记录。
   - **适用场景**：上下文被压缩丢失时恢复上下文；执行任务中缺少项目相关上下文；新 agent 接手时快速了解情况；任务领取前的进度查询。
   <br />
2. 完成任务文档中的任务后：在主表中按既有写法更新记录。

   <br />

### 3.2 编码任务完成后（迭代记录）

1. 在 `codemigrator_dev_progress/<模块分类>/` 中找到对应任务的迭代记录文件（分类与命名见 §1.3）。
2. 若不存在：在对应模块分类文件夹下创建记录文件（命名遵循 §1.3）。
3. 记录内容**必须按** `CodeMigrator迭代记录模板.md` 填写，做到每次修改都有记录。

### 3.3 执行计划文档管理

- 保存在 `Implementation_plan_doc/<模块分类>/`，分类与命名遵循 §1.3。
- 计划内容**必须按** `CodeMigrator实施计划模板.md` 填写（原子步骤 + 验收条款映射）。
- 详细设计文档保存在 `codemigrator_design_doc/detailed_coding_design/<模块分类>/`，内容**必须按** `CodeMigrator详细设计模板.md` 填写。

### 3.4 需求对齐与设计文档同步

- 执行代码更新前，若需求对齐阶段最终确认结果与原设计文档**不同**：代码更新后，**必须更新设计文档冲突部分及目录中相关文档**。

***

## 4. 任务交付清单（完成验证）

任务成功完成后，须产出以下文档：

| # | 文档     | 位置                                                       | 要求                                          |
| - | ------ | -------------------------------------------------------- | ------------------------------------------- |
| 1 | 模块迭代记录 | `codemigrator_dev_progress/<模块分类>/`                      | 按 §1.3 分类与命名 + 按 `CodeMigrator迭代记录模板.md` 生成 |
| 2 | 主任务表更新 | `codemigrator_dev_progress/CodeMigrator开发任务规划与进度跟踪.md`   | 更新任务记录数据与更新记录                               |
| 3 | 任务设计文档 | `codemigrator_design_doc/detailed_coding_design/<模块分类>/` | 按 §1.3 命名 + 按 `CodeMigrator详细设计模板.md` 生成    |
| 4 | 任务执行计划 | `Implementation_plan_doc/<模块分类>/`                        | 按 §1.3 命名 + 按 `CodeMigrator实施计划模板.md` 生成    |

> 验证项：
>
> - 检查迭代记录是否按 `CodeMigrator迭代记录模板.md` 生成。
> - 检查详细设计与实施计划是否分别按 `CodeMigrator详细设计模板.md`、`CodeMigrator实施计划模板.md` 生成。
> - 检查三个目录是否均存在该任务的交付文档，且模块分类归属一致（同一任务三目录共用同一分类名）。
> - 若本次任务新建了分类文件夹，确认三目录已同步创建。

***

## 5. TRAE IDE 专用约束（仅 TRAE IDE 代理遵守，其他 IDE 代理忽略）

1. **Skill 匹配**：工作前或工作过程中时，必须先查看 `.trae/skills` 是否有匹配技能文件；匹配则**必须**调用 skill 完成任务（可能需多个 skill 搭配）。
2. **Spec 模式流程**：使用 spec 模式时，**必须先调用** `.trae\skills\brainstorming` skill 以及提问工具对齐需求，确认无误后再书写 spec 相关文件。

