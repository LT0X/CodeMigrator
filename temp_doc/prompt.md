
Alt + Z
按一下开启「跟随编辑器屏幕宽度软换行」

1. 现在需要启动harness项目框架的搭建工作，具体任务包括设计并实现对应的架构文件夹结构。首先，需全面读取位于"my_space\\harness_dev_progress"目录下的所有设计文档，以深入理解项目的整体架构设计理念、各功能模块的职责划分、模块间的交互关系及数据流向，同时明确项目的总体技术要求和实现标准。在充分理解设计文档后，在"services\\harness-core"文件夹中按照文档规定的架构设计搭建对应的文件夹结构，确保各模块文件夹的命名规范、层级关系与设计文档保持一致，并为后续的代码开发和模块集成奠定基础。 harness核心构建文件夹为services\harness-core，只负责haness  agent核心代码的实现，业务接口逻辑是这里services\api。


2. 设计并编写haeness代码实现的任务规划与进度跟踪文档，用于指导agent执行代码实现工作。该文档应保存于路径D:\\work\\project\\Policy-Analysis-Platform\\my_space\\harness_dev_progress\\，请修改原文件名"总体进度.md"为更具描述性且符合项目规范的名称，以准确反映文档内容性质。
在编写文档前，必须详细阅读并理解位于D:\\work\\project\\Policy-Analysis-Platform\\my_space\\harness_design_doc目录下的所有设计文档，确保任务规划与设计要求保持一致。
文档内容应包含以下核心要素：
1 总体任务概述：明确haeness的开发目标、范围
2.模块划分：将haeness系统开发 按照的功能模块 来划分开发任务。
3.依赖关系分析：详细分析各模块间的依赖关系，使用图表形式直观展示模块间的调用和依赖路径
4.并行开发规划：明确标识可独立并行开发测试的模块，以及需要按依赖顺序开发的模块
5.进度跟踪机制：设计进度跟踪表格，包含任务ID、任务描述、状态（未开始/进行中/已完成）、开始日期，实际完成日期和备注字段
6.变更管理流程：明确agent 因为开发实际情况需要动态变更，每次变更必须与我相对齐并记录变更原因、内容和影响范围
进行书写文档，确保agent能够准确理解任务要求和进度跟踪方法。


3. 根据任务文档和设计文档，在当前feature/harness-core-foundation分支上执行第一部分开发任务。具体要求如下：1. 严格遵循my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md中第一部分的任务描述和技术要求；2. 参考my_space\harness_design_doc目录下的相关设计文档进行实现；3. 确保代码符合项目编码规范。

4. 我现在有一种情况，我在旧的开发分支 拉取后，重新在这个分支切了 现在的开发分支，并更新了内容，我现在分支写的内容已经本地提交， 并且现在远程开发分支已经有了新内容了，现在需要本地开发分支同步远程开发分支，然后再更新到我当前的分支，现在帮我在这个分支中更新新的开发分支的内容rebase。


5. (新领取开发任务) 当前本地分支已提交pr,并成功合并至远程开发分支仓库。请执行以下操作：1. 切换至本地开发分支；2. 从远程开发分支拉取最新开发分支更新；3. 查阅文件"my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md" 和my_space\harness_dev_progress 目录下最新的文件夹里面的内容，理解和领取下一段开发任务内容；本次在新分支要开发的任务范围预计是(`HC-MEM-001`, c ) 以及之前受此次任务范围影响未完成进行中的任务 4. 基于更新后的开发分支创建新的功能分支，分支命名需符合项目规范；5. 切换至新创建的功能分支，

(新领取开发任务，分支未合并) 当前本地分支已提交pr,但是还没有合并至远程开发分支仓库。请执行以下操作：1. 查查阅文件"my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md" 和my_space\harness_dev_progress 目录下最新的文件夹里面的内容，理解和领取下一段开发任务内容；本次在新分支要开发的任务范围预计是(| `HC-SBX-001`，``, ， 以及之前受此次任务范围影响未完成进行中的任务) ； 2. 在当前的分支切出创建新的功能分支，分支命名需符合项目规范；3. 切换至新创建的功能分支， 后面开发分支合并更新后我然后在这个分支进行同步更新。


6. （开始任务模板）现在完成 本次分支涉及到的  `HC-DLV-001` 任务以及相关部分。 查阅文件"my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md" 和my_space\harness_dev_progress 目录下根据开发模块分类最新的文件夹里面的内容，理解和领取下一段开发任务内容，制定开发计划，然后进行代码开发工作。请基于指定的任务文档（路径：my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md）和设计文档目录（路径：my_space\harness_design_doc\architecture_module_design）中的内容，执行相关任务的开发工作。开发过程中需严格遵循文档中规定的功能需求、技术规范、架构设计和要求，确保实现的代码符合项目标准以及能通过严格全面联系的系统测试,同时完成文档的同步工作。

6. （草稿）现在完成 本次分支涉及到的  `HC-DLV-001` 任务以及相关部分。 查阅文件"my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md" 和my_space\harness_dev_progress 目录下根据开发模块分类最新的文件夹里面的内容，理解和领取下一段开发任务内容，制定开发计划，然后进行代码开发工作。请基于指定的任务文档（路径：my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md）和设计文档目录（路径：my_space\harness_design_doc\architecture_module_design）中的内容，执行相关任务的开发工作。开发过程中需严格遵循文档中规定的功能需求、技术规范、架构设计和要求，确保实现的代码符合项目标准以及能通过严格全面联系的系统测试,同时完成文档的同步工作。



. 本地分支（origin）按创建时间排序
git for-each-ref --sort=committerdate refs/heads/ --format='%(committerdate:short) %(refname:short)'

 git branch -D feature/orchestrator-core

应该先检查是否当前的分支的起点和 开发分支是否同步。
提交并创建分支
git push  origin  feature/memory-context-compression
 具体执行步骤：
完成所有未完成任务的开发与测试验证，确保符合实施计划文档中的质量标准
. 确认所有功能点均已实现并通过测试验证
. 同步更新所有相关文档，包括但不限于任务文档、进度跟踪文档和实施计划文档，确保文档内容与实际开发情况一致

我在旧的开发分支 拉取后，重新在这个分支切了 现在的开发分支，并更新了内容，我现在分支写的内容已经本地提交， 并且现在开发分支已经有了新内容了，现在帮我在这个分支中更新新的开发分支的内容rebase。

8. 现在这个分支所需要开发的全部任务完成了，现在需要检查当前的本地开发分支是否与远程开发分支同步。如果同步，就继续进行pr文档 书写，保存在my_space\pr_md，模板以及相关的注意事项my_space\pr_md\README.md。如果不同步，就先更新本地开发分支，然后rebase 或者合并 更新到我当前的分支，然后然后再进行pr文档 书写，保存在my_space\pr_md，模板以及相关的注意事项my_space\pr_md\README.md

现在这个分支所需要开发的全部任务完成了，现在需要检查当前的本地开发分支是否与远程开发分支同步。如果同步，就继续进行pr文档 书写，保存在my_space\pr_md，模板以及相关的注意事项my_space\pr_md\README.md。如果不同步，就先更新本地开发分支，然后rebase 或者合并 更新到我当前的分支，然后然后再进行pr文档 书写，保存在my_space\pr_md，模板以及相关的注意事项my_space\pr_md\README.md



9. （agent接手） 我这个分支涉及的任务是 “本分支任务范围：
HC-ORC-001：Worker Pool、资源门、Claim/attempt、Reconciler 启动及常驻扫描
HC-ORC-002：模型—工具—模型 Agent Loop、预算、等待、取消、进度保护及 Answer/Evidence 原子编排
继续推进 HC-RUN-005 中受 Orchestrator 影响的恢复调度部分
继续推进 HC-VRF-002 中 Worker、取消、竞争及恢复验证部分”
现在已经完成 `HC-ORC-001`和 `HC-ORC-002`任务。具体情况可以查看迭代记录文件。现在需要完成本次分支涉及到的 延续 HC-RUN-005、HC-VRF-002 中与本分支相关的部分。具体进度和 迭代记录可以 查阅文件"my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md" 和my_space\harness_dev_progress 目录下根据开发模块分类最新的文件夹里面的内容，理解下一段开发任务内容，制定开发计划，然后进行代码开发工作。请基于指定的任务文档（路径：my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md）和设计文档目录（路径：my_space\harness_design_doc\architecture_module_design）中的内容，执行相关任务的开发工作。开发过程中需严格遵循文档中规定的功能需求、技术规范、架构设计和要求，确保实现的代码符合项目标准以及能通过严格全面联系的系统测试,同时完成文档的同步工作。




9. 我现在需要详细的了解现阶段已经完成的开发任务以及项目任务设计实现，先去my_space\harness_dev_progress\Harness开发任务规划与进度跟踪.md 了解已经完成的任务有那些。因为是agent 编码和设计，我做方向的调整，同时因为复杂性和内容较多，难免我有不清楚的地方，所以为了我更好的掌控设计，我现在需要了解现阶段已经完成的任务中的设计，详细的实现细节我不关心，看不过来，我想了解他是怎么设计这个功能，有什么作用，为什么这样设计，解决的问题是什么等，读取my_space\harness_design_doc，my_space\harness_dev_progress，my_space\Implementation_plan_doc 里面的文件了解清楚，然后跟我说明。并写一个详细的文档保存在my_space\temp\project_explanation_doc

10. 在主任务中末期创建一个新任务，用于系统性识别所有任务表中存在以下情况的任务：当初因后续功能边界未明确或技术实现细节未确定，而将特定设计依赖项的实现工作向后推迟，但在大部分相关前置任务已完成或项目整体进入后期阶段后，这些被推迟的设计依赖项仍然未得到实现。该检查应包括全面审查开发进度文档和相关的项目设计和实施文档，以确认这些未实现的设计依赖项的具体内容、原定实现计划以及当前状态，然后再这个任务进行全部的设计实现。


11. 我现在的任务已经进行推进和更新到 `HC-TOL-002`部分，具体的情况和设计相关文档，可以查看一下my_space\harness_dev_progress ，my_space\harness_design_doc ， my_space\Implementation_plan_doc 文件夹中了解开发进度，设计，实施，查询完毕后，进行更新一下 my_space\temp\project_explanation_doc\Harness项目设计说明.md文档, 



13. 对于知识库的rpc 接口协议，考虑现有的rag 设计文档 和 工具的协调的原因，是一个工具一个rpc 接口，还是多个，但是目前rag的实现这一部分还有较大的不确定性，并且需要根据rag的实现情况和目前harness的搭建情况来确定。


14. 请基于另一个agent提供的审查结果进行参考分析，在保持独立思考和专业判断的前提下，评估审查结果的准确性与合理性。对于审查意见中确有价值的部分应予以采纳，对于存在争议或错误的内容需进行审慎判断。在此基础上，执行新一轮的系统对齐工作，确保最终输出既充分吸收合理建议，又能体现独立专业判断，进行设计文档的更新。


16. AGENTS copy.md  这个是我之前项目的一些开发规范，现在需要修改成适合成我们这个项目的 开发s规范，然后在my_space 创建对应的文件夹，跟我进行对齐，然后在写在D:\work\code_migrator\AGENTS.md 