# 功能模块与实现位置

项目通过固定流程组织输入、分组、规则、定位和结果持久化，通过 Agent 完成代码调查与缺陷分析。日常评审和数据集评测均可直接执行 Python 脚本。

| 功能 | 实现位置 | 核心机制 |
| --- | --- | --- |
| Git 增量与全仓扫描 | inputs | 冻结目标版本；工作区合并 staged、unstaged、untracked 内容；Git/目录文件枚举 |
| 文件筛选 | resources/filters、inputs/selection.py | 支持类型与默认排除规则；显式 include/exclude；容量检查 |
| 四层规则 | review/rules.py | 自定义、项目、全局、内置规则优先匹配；组内相同正文只注入一次 |
| 阶段提示词 | resources/prompts | 分组、规划、主评审、复核、定位与压缩分别组织上下文 |
| 只读上下文工具 | tools、runtime | 文件读取、文件查找、Diff 读取、代码搜索；参数校验、逐行行号与结果限长 |
| 评论提交与结束 | tools/findings.py | code_comment 批量提交候选；task_done 明确状态与已审查路径 |
| 文件关联分组 | review/grouping.py | 小改动合组；模型关联分组；编号校验、遗漏补齐与失败降级 |
| 多轮评审 | review/engine.py | effort 控制最多一/二/三轮；先定位和复核，再反馈保留发现；无新增发现提前停止 |
| 批次扫描 | review/scan_engine.py | 批次串行、批内单文件并发；一次主循环；定位后语义去重、检查点与项目摘要 |
| 隔离复核 | reflection | 本轮新增评论与组内 Diff 批量复核；独立上下文；失败保守保留 |
| 分层定位 | location | Hunk、全文件、唯一跨文件匹配与 Re-location；保留定位依据和失败诊断 |
| 共享评审时间 | application、runtime | 所有组与阶段共享整次评审截止时间；单次请求超时与迭代限额分别控制 |
| 上下文压缩 | runtime/context.py | 缺陷、工具结论、完成/待办任务与关注点摘要；重注入固定评审信息 |
| 会话与恢复 | sessions | 单 JSONL；完整模型/工具交互聚合、文件检查点、输入身份校验；复用完成结果 |
| 双评审器评测 | evaluation | 本项目与 Claude Code 共享任务范围和规则；独立 Judge、一对一匹配与指标统计 |

资源清单 `src/code_review_agent/resources/resource_manifest.json` 保存文件路径与 SHA-256。单元测试验证文件完整性，评测效果以真实运行报告为准。

评测分为 prepare、review、score、report 与 all。仓库执行 clone/fetch、checkout TARGET、评审与清理，同仓库任务全生命周期串行，跨仓库可并发。日常范围比较默认使用 merge-base，数据集评测直接比较 SOURCE/TARGET。
