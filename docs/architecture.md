# 目录与模块职责

新项目独立设计目录，不导入或执行参考 Go 项目及其他 Python 实现。

| 位置 | 职责 | 相关 OpenSpec 任务 |
| --- | --- | --- |
| main.py / session.py / benchmark.py | 参数解析、调用 application 服务、退出码 | 8、9 |
| application | 一次运行的输入、流程、会话和结果协调 | 5、8、9 |
| contracts.py | 候选、快照、位置、复核和运行记录的数据契约 | 1.4 |
| config.py / project_paths.py | 配置优先级、校验和写入边界 | 1.3、8.3 |
| inputs | Git 输入、diff、冻结快照、文件筛选和扫描 | 2 |
| resources | 有来源与摘要的规则、提示词和工具 schema | 3.1 |
| tools | 基于冻结输入的只读检索、评论收集和结束工具 | 3 |
| runtime | AgentScope 适配、模型、上下文与用量 | 4 |
| review | 分组、规划、多轮、覆盖和单元调度 | 5 |
| reflection | 隔离事实复核和保守结果处理 | 7 |
| location | Hunk、全文件、跨文件、Re-location 与验证 | 6 |
| sessions | 单 JSONL 契约、交互/业务记录、文件 checkpoint 和结果回放 | 存储变更 |
| output | text/JSON 格式与最终状态呈现 | 8 |
| evaluation | 数据、基线、实验、Judge、匹配与统计 | 9、10、12 |
| tests | 业务场景、可控模型和临时仓库夹具 | 11 |

Diff 主流程：入口 → application → 冻结输入 → 分组/规划 → 单元多轮审查（提交评论即启动后台定位）→ 每轮等待定位并批量隔离复核 → 最终输出与检查点。Scan 主流程：冻结输入 → 分批 → 依次执行各批文件并发审查 → 本批定位完成 → 语义去重替换评论 → 检查点 → 最终摘要。公共数据契约不依赖业务服务；正式审查不能导入 evaluation。AgentScope 专用消息和 API 适配集中在 runtime，不在核心契约传播框架类型。

新增工具应修改 tools 与 resources/tool_schemas，并验证白名单与读取边界；新增定位阶段应修改 location/pipeline.py 及该阶段模块，并验证独立消融和失败分母；更改指标应修改 evaluation 并维护冻结的评分协议与手算测试。

## 顺着业务阅读

1. `main.py` 初始化项目路径后调用 `application/script_entry.py`，参数覆盖在 `application/arguments.py`。
2. `application/review_service.py` 组装 `inputs/snapshots.py` 和 `review/rules.py`，冻结版本和有效配置，再创建会话与 StageRunner。
3. `review/engine.py` 负责 Diff 分组与多轮；`review/scan_engine.py` 负责 Scan 批次和单文件循环，去重后替换实际评论集合，另以 `raw_candidates` 保存去重前记录。`inputs/scan_files.py` 枚举 Git/非 Git 文件，`inputs/scan.py` 构造语言、首级目录或文件批次。
4. `runtime/stages.py` 渲染/执行阶段，`agentscope_adapter.py` 使用真实 AgentScope 状态和工具循环，`model_factory.py` 记录每次请求用量，`context.py` 负责场景压缩。
5. `tools/findings.py` 收集候选并调度定位任务，`location/pipeline.py` 按 Go 顺序定位，`reflection/reviewer.py` 构造组内 Diff 和评论的单次批量复核，`reflection/tools.py` 声明两个结果工具。定位失败不影响评论输出资格；复核拒绝和扫描去重仍会过滤。
6. `sessions/journal.py` 追加单会话 JSONL，`records.py` 使用 SDK CustomEvent 定义完整交互/业务记录，`results.py` 编解码文件 checkpoint，`recorder.py` 写业务边界，`replay.py` 还原结果并提供 Resume 输入。`sessions/store.py` 仅用于 benchmark artifacts。output/render.py 只呈现，不发起模型调用。
7. `benchmark.py` → `application/benchmark_service.py` 协调阶段；evaluation 的 dataset/repositories 负责准备，task 构造共享安全任务，reviewers/project 调用生产 review，reviewers/claude 调用原生 CLI；judge/matching 负责评分，metrics/reporting 负责重算。

数据契约和 UnitResult 在 contracts.py，不依赖 AgentScope。application 允许协调正式审查和评测，正式 review/reflection/location 不导入 evaluation。评测报告与业务调度分别维护，避免指标逻辑进入正常审查。

测试按业务职责命名。新增行为先选对应测试文件：输入/规则工具/runtime/location/reflection/orchestration/review_sessions/evaluation/scripts_benchmark/boundaries。Ruff 覆盖项目源码、薄脚本和测试，mypy 严格检查全部业务源码；脚本仅在导入顺序处允许 E402，以确保第三方缓存初始化前设置路径。

`.venv` 为依赖环境，`.state` 为运行记录，`.cache` 为数据与临时文件，`reports` 按运行 ID 保存评测证据。资源内迁移 manifest 记录参考文件、目标文件与 SHA-256；参考路径只作来源说明，不作运行依赖。

### Diff 多轮评审的反馈顺序

`review/engine.py` 每轮收集原始候选后，等待定位 worker，按组一次复核本轮新增评论。使用迁移的 system/user 模板，输入仅为组内 Diff 和 c-N 评论列表，不携带主评审历史、完整源码、规则或背景。`runtime/agentscope_adapter.py` 通过 AgentScope 模型单次调用，暴露 report_incorrect_comments/approve_all_comments，不执行调查工具或后续模型循环。两类删除依据与保护类别由原版提示词规定；程序按返回 ID 过滤，不额外要求证据行号锚点。

工具返回优先，兼容文本 JSON ID 列表；非法/越界 ID 跳过，请求或解析失败记录 undecided 并保留全组。组外重定位评论不交给原组复核。只有 keep/undecided 的发现进入下一轮提示词；reject 仍保存在原始候选台账和复核记录中。`review/prompts.py` 构造与原版一致的发现摘要：路径、最多 200 字符代码、最多 300 字符说明，并明确要求 `Do not repeat them`。第二轮起移除初始计划；没有新增保留发现或累计达到 30 条时停止后续轮次。这是提示词防重复，不保证语义重复必然被删除。

文件检查点保存已完成的定位和组内批量复核结论，最终汇总和恢复复用记录，不重复发出批量请求。Scan 不执行 Reflection 或外层多轮。定位、复核及 Scan 策略版本参与会话缓存身份，旧策略会话需重新运行。历史 GoFR 报告属于修改前版本，不能用作此次修复后的效果结果。

### Go 兼容定位

任一已有正行号直接接受并标记 `provided`，不重新匹配或检查文件长度。不完整或逆序坐标原样输出，但不构造有效 `Location` 区间。文本定位先检查所有 Hunk 新侧，再检查旧侧，取首个匹配；全文件阶段只扫描新文件，忽略空行并保留真实行号。跨文件先执行同样的 Hunk/全文件匹配，每个文件取首个位置，仅唯一命中文件时迁移路径与位置。Re-location 提取响应中第一个代码块，只重试原文件；失败保留原片段。

定位任务使用独立的有界并发，避免与主评审的调度锁相互等待。每轮复核前排空任务，取消时回收任务并保留已提交候选。`provided`、`verified`、`unlocated`、`ambiguous` 仅描述定位依据；定位失败的评论仍可输出，未知行号为 `0`。定位策略版本参与会话及定位实验缓存身份，旧策略会话需重新运行，不能复用旧结论。

### Scan 批次与恢复

批次严格顺序执行，批内按 `max_concurrency` 并发处理单文件。每文件可选一次 Plan（单次请求、无工具），随后一次带只读上下文工具的主评审循环。评论提交时启动定位，不启用 Diff 的跨文件定位；本批全部文件和定位任务结束后才去重。去重需要达到 `dedup_min_comments`，使用完整 c-N 分区，解析失败保留原评论；成功时实际替换 `candidates`，不依赖 suppressed 或展示内容重写。

同文件合并的检查点保存合并评论；跨文件合并的检查点保存各文件原评论，恢复后重新执行批次去重，防止单文件重跑丢失发现。检查点在本批去重后保存，项目摘要只读取最终评论集合。`raw_candidates` 为本次输入到去重的审计记录；恢复输入可能已经来自同文件合并后的检查点，完整原始台账可查看父会话。

## 会话存储契约

每次评审一个 `.state/sessions/<session_id>.jsonl`，使用 SDK CustomEvent 格式，记录完整模型调用、实际工具执行与业务边界。运行时消费的 SDK START/DELTA/END 不逐条落盘，AgentState 和候选对话历史不归档。Diff 仍按组执行，恢复按文件排除已完成工作再分组；新 Agent 不接续旧对话。输入保留内存快照，磁盘只存全量上下文文件指纹和 full SHA，Resume 从磁盘验证。

Scan 文件的安全恢复候选与批次最终展示分别记录，`session_end` 保存引用和全局摘要，避免重复整份结果。查询与恢复只读 journal，旧八文件目录明确不支持。完整字段、边界和示例见 [单 JSONL 会话与恢复](sessions.md)。本契约取代此前未归档 OpenSpec 的多文件存储规划。

## 双评审器评测边界

`evaluation/config.py` 独立处理 Claude 配置，不加入普通 Settings 身份。`dataset.py` 保存含标注的样本；`task.py` 仅向评审器提供完整 SOURCE/TARGET、Diff、过滤范围、背景、语言和去重规则正文。标注只传给独立 Judge。

`repositories.py` 普通 clone/fetch，补齐端点对象并核验归属；任务进入同一仓库生命周期锁，清理后 checkout TARGET，两边使用同目录，退出时回收 CLI 进程组并清理。project 从对象构造 direct 冻结快照，Claude 从 TARGET 磁盘文件和受控 Git 查询读取。跨仓库受 semaphore 限制，同仓库 PR/reviewer/重复任务顺序执行。

run.json 保存任务身份与尝试，review artifacts 保存最终评论与原始 CLI 字段，score artifacts 保存匿名判分和版本。已完成任务复用不访问 Git；score/report 不调用评审器。项目内部模型与工具仍按现有单 JSONL 记录，无额外评测 chunk/event 日志。
