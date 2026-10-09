# 核心语义迁移映射

原版仅作静态参考，评审和评分实现均在本项目，Claude 使用已安装的原生 CLI。资源逐文件摘要见 `resources/migration_manifest.json`。

| 原版能力 | 本项目位置 | 保留或适配 |
| --- | --- | --- |
| Git diff / scan | inputs | 冻结目标版本，工作区 staged/unstaged/untracked 最终内容 |
| 默认扩展与排除 | resources/filters、inputs/selection.py | 迁移原始有序资源，显式 include 和 exclude |
| 自定义/项目/全局/内置规则 | review/rules.py | 首次匹配、merge_system_rule、Objective-C 嗅探；全局层改为项目内显式配置 |
| 各阶段模板 | resources/prompts | 保存原始文本，适配层提供阶段变量及输出语言指令 |
| 六种审查工具 | tools、runtime | 参数名称、限额和阶段白名单；PCRE 使用有 PCRE2 的 ripgrep |
| task_done | tools/findings.py | state 保留；新增 reviewed_paths，使多文件单元覆盖显式可追溯 |
| code_comment | tools/findings.py | 批量提交、稳定候选 ID；新增 side 以支持旧侧评测 |
| 关联分组与规划 | review/grouping.py、engine.py、prompts.py | 校验分区、补齐遗漏、失败按文件回退；规划单独白名单 |
| effort 多轮与并发 | review/engine.py | 一/二/三轮；Diff 每轮先定位与复核，再反馈保留发现并提示不重复；第二轮起移除计划，无新增保留发现或达到 30 条时停止；保留后轮失败前的候选 |
| 扫描编排/去重/项目摘要 | review/scan_engine.py、inputs/scan_files.py、scan_template.json | Git ls-files / 根忽略规则；批次顺序、批内并发；每文件一次主循环，无 Reflection；定位后替换去重，再保存检查点和生成摘要 |
| Reflection 过滤 | reflection/reviewer.py、reflection/tools.py | Diff 每轮按组一次请求，原版 system/user，仅 Diff+评论、两个结果工具；不要求证据锚点；失败保守保留；不存档候选对话，旧 real_history 入口停止支持 |
| 评论坐标 | location | 已有正行号接受；新侧再旧侧 Hunk 首个命中 → 全新文件 → 唯一跨文件（仅 Diff）→ Re-location 第一个代码块；失败仍输出 |
| 会话与恢复 | sessions、application/review_service.py | 单 JSONL + SDK CustomEvent，完整交互聚合、文件 checkpoint、磁盘输入身份验证；不恢复对话，Go 与旧 Python 历史不兼容 |
| Agent 历史压缩 | runtime/context.py、agentscope_adapter.py | 原场景化五维提示词，AgentScope 状态压缩，固定版本/目标/规则/背景/候选重注入；调用纳入用量 |
| 双评审器评测 | evaluation、application/benchmark_service.py | 本项目生产 review / Claude 原生 CLI，共享 AACR direct 任务、独立 Judge、一对一匹配；参考项目不参与运行 |
| MAX_TOKENS / Go 预算派发 | 配置与编排 | 原始模板仅作来源资源；不读取其总预算常数，保留单次上下文与输出容量 |
| MCP、宿主委托、SARIF、Viewer、遥测、安装 CLI | 不提供 | 按提议明确排除 |

原始资源保持字节摘要和许可。扫描按文件审查；分批控制执行顺序、定位等待、去重及检查点。当前功能通过离线验收，效果指标等待真实服务验证；详细记录在 reports/verification，实验限制见 evaluation.md。

当前评测入口为 prepare/review/score/report/all、project/claude/both。旧 run/subset/development-repo/freeze/pool/module/variant 参数不再支持；旧 B0/R0/R1/定位实验实现已移除，历史产物保持原位。

仓库准备由旧对象缓存改为 AACR 风格普通 clone→fetch→checkout TARGET→评审→清理，同仓库全生命周期锁。旧 owner/hash 缓存原地兼容，不移动或删除；前一评测提议的任务 worktree 设计被固定共享目录替代。日常范围仍默认 merge-base，只有 benchmark 显式 direct。
