好问题，这确实挑战了我"Claude Code 是单写者"的前提。但仔细想下来，结论反而更强了。

Claude Code 的 fork agent 实际怎么工作的
Claude Code spawn subagent 时：

Text


        
主 agent
  ├── spawn subagent A → "去分析这个模块的依赖"
  ├── spawn subagent B → "去改那个文件的 API 调用"
  └── 等结果回来 → 自己决定怎么整合

      
关键细节：

subagent 之间不协调——没有数据库，没有锁，没有 CAS。各干各的。
文件冲突靠 git 兜底——如果 A 和 B 都改了同一个文件，commit 时 git 会报 conflict，模型自己解决。
subagent 是短命临时工——做完任务返回结果就销毁，不持有长期资源。
主 agent 是唯一整合者——subagent 返回的是"结果"（文字/patch），主 agent 决定怎么落地。
没有 CAS，没有 fencing_token，没有 lease。并发控制完全靠 git + 模型判断。

回到 CodeMigrator：V2 的 Slice 隔离已经比 Claude Code 强了
这里有一个被忽略的事实——V2 的 Git ref 结构本身就已经提供了 Slice 级隔离：

Text


        
base commit (冻结)
  ├── verified ref ──→ Slice A verified commit
  ├── work ref     ──→ Slice B candidate commit（独立分支）
  └── work ref     ──→ Slice C candidate commit（独立分支）

      
V2 明确要求：

每个 Slice 的 candidate commit 是隔离的
未验证兄弟 Slice 永远不进入当前 verification workspace

这意味着即使多个 Slice 并行执行，它们的 Git work ref 指向不同的 commit，不存在同一文件的并发写入。

所以问题变成：

如果 Slice 之间是隔离的（V2 已保证）	CAS 保护的是什么？
Slice A work ref 和 Slice B work ref 指向不同 commit	Slice 级文件写入无冲突
Run 状态只有 Harness 一个写者推进	Run 级状态无冲突
sandbox-worker 不直接写数据库	跨进程无冲突

答案：CAS 保护不了任何实际存在的冲突场景。因为 Git ref 隔离已经从物理上杜绝了并发写入。

那 fencing_token 保护什么？
V2 说 fencing_token 是为了防这个场景：

worker A 拿到 lease 开始执行 → Run 被 cancel → A 不知道 → A 继续执行 → 产生副作用

Claude Code 怎么处理等价问题？

Text


        
# Claude Code 的做法
subagent A 在干活
  → 用户 Ctrl+C
  → 主进程退出
  → subagent A 被操作系统 SIGTERM
  → 文件系统留下半成品
  → git checkout 恢复

      
没有 fencing_token。进程信号就够了。

CodeMigrator 完全可以一样：

Text


        
cancel Run
  → SIGTERM sandbox worker 进程组
  → git reset work ref 到 verified ref
  → 完了

      
fencing_token 的前提是"worker 可能在另一台机器上，SIGTERM 传不过去"。但 V2 是单机部署，sandbox-worker 就在本机，进程信号一定能送达。

两种并发协调模式的本质对比

关键洞察
这里有一个逻辑上的不对称：

Claude Code 的 fork agent 共享同一个工作目录 → 冲突概率实际比 CodeMigrator 高 → 但它不需要 CAS 就能工作。

CodeMigrator V2 的 Slice 已经通过 Git ref 物理隔离 → 冲突概率为零 → 却加了 CAS + fencing + lease 防冲突。

这就像：

一栋楼里每个房间都有独立锁（Git ref 隔离），但还是在大门口装了安检+门禁+巡逻队（CAS/fencing/lease），防止有人闯进别人的房间。但房间门已经锁了，闯不进去。

为什么 Claude Code 的方式能工作
因为代码迁移的冲突模式不是"数据库事务冲突"，而是"语义冲突"：

文件级冲突：两个 agent 改同一文件 → git merge conflict → 模型解决
语义级冲突：A 改了函数签名，B 还在用旧签名 → 类型检查失败 → 模型修复
依赖级冲突：A 升级了依赖版本，B 的代码不兼容 → 测试失败 → 模型修复
这三种冲突都不是 CAS 能防的。CAS 防的是"同一个数据库行被两个事务同时写"——但代码迁移的冲突发生在语义层面，不是行级写入冲突。无论有没有 CAS，语义冲突都得靠验证引擎（build/test）来发现、靠模型来修复。

结论
Claude Code 的 fork agent 恰好证明了这一点：

多 agent 并发不需要 CAS——git + 乐观并发就够
CodeMigrator V2 的 Slice 隔离比 Claude Code 更强——物理上不可能并发写同一文件
CAS/fencing/lease 保护的是一个不存在的问题——冲突已经被 Git ref 隔离解决了
真正的冲突（语义级）靠验证引擎发现——不是靠 CAS 预防
所以 CodeMigrator 完全可以：

保留 Slice 级 Git ref 隔离（这比 Claude Code 强）
保留验证引擎（这是核心价值）
去掉 CAS + fencing + lease + reconciler + Outbox（Claude Code 证明了不需要）
用 SIGTERM + git reset 处理 cancel 和崩溃恢复
Claude Code 的 fork agent 是"共享目录 + 无锁"都跑得起来。CodeMigrator 有独立 Git ref 隔离，更没理由需要 CAS。

要不要我基于这个认知，出一份"V2 → V3 精简方案"？主要砍 PostgreSQL → SQLite/文件系统、砍 fencing/lease/reconciler、砍 Outbox，保留插件进程 + Slice 隔离 + 验证引擎 + Spec 驱动。