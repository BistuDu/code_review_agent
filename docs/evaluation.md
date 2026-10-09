# 本项目与 Claude Code 的 AACR 评测

评测整套代码评审流程：本项目调用生产 Python/AgentScope review，Claude 使用原生 Agent 和固定本地任务，统一最终 comments JSON，再由匿名独立 Judge 判分。评测入口直接执行本项目与 Claude Code 两套评审器。

## 数据集与源码关系

默认只读 `/Users/dyh/Desktop/code-review/code_review_agent/dataset` 的 `positive_samples.json` 和 `negative_samples.json`，参数 `--dataset-dir` 可替换目录。两份文件已按原始字节复制到本项目；默认从项目根解析路径，不依赖兄弟目录，也不下载 Hugging Face 数据。正文件 196 个 PR 条目/1506 条评论，负文件 155 个 PR 条目/639 条评论；合并后 2145 条评论、50 仓库/200 PR。文件分别赋 label=1/0，保留重复行，以两份文件 SHA256 建立 `local:` 数据身份。文件变动需要新的 run-id；不能复用历史 HF 身份的运行。

数据集保存评审样本和参考评论，不是源码压缩包。seed=42 正式清单仍为 10 仓库/35 PR；`--repo gofr-dev/gofr` 选择其 6 PR，`--limit 1` 仅运行排序后的一个 PR。默认一次，`--repetitions 3` 表示每 PR、每评审器分别生成三次。旧 Hugging Face 缓存和评测记录保留为历史；本地版本与旧 HF 数据有一条参考标签不同，以当前输入文件为准。

`dataset.json.audit` 描述完整数据；`selection` 只描述本次实际入选范围，并与 `run.json.selected_prs` 一致。显式 `--repo` 使用 `mode=explicit_repository`，仅保存 repo/limit，不执行默认十仓库抽样；抽样使用 `mode=sampled_repositories`，保存 repo-count/seed/limit。两种模式的 selected_repos、final_prs 和计数均已应用全局 limit；抽样五仓库但 limit=1 时，实际入选可能只有一个仓库。旧运行文件保持原样，新运行采用上述结构。

| 字段 | 使用方式 |
|---|---|
| githubPrUrl → pr_url | 解析 owner/repository 为 clone URL；聚合同 PR 的参考行；作为样本身份。PR 不是单次 commit |
| source_commit → pr_source_commit | SOURCE / base_sha，直接 Diff 的起点和 left 源码 |
| target_commit → pr_target_commit | TARGET / head_sha，checkout 的实际版本和 right 源码 |
| path / side / from_line / to_line | 评分的文件、侧及区间约束，不作为发现提示 |
| note / label / category | 参考评论、正例资格及诊断；只供评分，不给评审 Agent |

例如 GoFR PR 1325：SOURCE=`7f1760b083ea23ffd95fdf83467bdfa21735df81`，TARGET=`b9cdcbb2b9fc78499d15d51ffbb7c861592b0efe`。评审的是这两个版本之间的差异，不查询 PR 当前是否合并，也不用默认分支最新 HEAD 替代。

按路径、版本和语言校验同 PR 元数据。保留参考重复行及 ordinal 身份；异常 side/区间/空正文和未知 label 单独审计。只有有效 label=1 进入 N。repo-count 可在可用仓库数内设置；恰好每语言一个时保留原有确定性抽样顺序。

## clone、checkout 与执行保护

源码仅在 `.cache/benchmark-repositories/`：新目录为 `owner__name`，现有受管哈希目录优先原地复用。首次普通 `git clone URL PATH`，无 depth/filter/bare/single-branch；已有副本 `fetch --all --tags`，缺端点显式 fetch SHA，检查提交/树/blob。旧 shallow 可解除浅边界，旧 partial 补齐端点；不承诺所有 fork/PR refs/LFS/submodule 下载完整。

每任务：取得 repo 稳定跨进程锁 → 核验 owner、origin、真实路径与独立 .git → reset/clean → checkout TARGET → 核验 HEAD 和干净状态 → 评审 → 保存结果 → 回收子进程 → finally 清理/核验 → 解锁。网络对象准备、锁等待、评审及清理耗时分别记录。

prepare 结束缓存只展开最后一个 TARGET；每个实际任务仍重新切换并核验。同仓库 PR、reviewer、重复任务依照 run.json 串行，双评审器顺序按 PR/轮次交错。仓库间默认并发 1，可用 `--repository-concurrency` 调整；project 内部分组并发仍生效。跨运行使用同一 repo 锁，活跃 CLI 进程 lease 会阻止再次 checkout。清理只作用于受管缓存，不修改用户仓库或参考源码，不移动旧缓存，不创建 benchmark-workspaces。

## 同输入与 CLI 隔离

共同排除符号链接评审目标，并保留过滤参考的路径/ID；这些正参考仍留在召回分母。共同任务包含 direct SOURCE/TARGET、筛选/排除文件、Diff、背景、语言、按适用文件映射的有效规则正文（相同正文只存一份）。project 继续生产 Plan、工具循环、多轮、定位和 Reflection；Claude 不复制这些内部阶段，比较的是流程效果而非等量推理。两种协议、系统提示词、内部压缩、effort/迭代限制不宣称完全相同。

需已安装支持必要 flags 的 Claude CLI。默认继承 reviewer 模型/凭据，DeepSeek 官方入口映射为 `https://api.deepseek.com/anthropic`；其它服务必须显式设置 endpoint。`--claude-config claude.local.json` 支持 model/base_url/api_key/command，环境 `CODE_REVIEW_CLAUDE_MODEL/BASE_URL/API_KEY/COMMAND` 优先；它们不改变普通 Settings 身份。例子见 `claude.example.json`，密钥勿提交。

本机 2.1.149 实测 bare 会跳过显式 hook，因此采用非 bare、独立 CLAUDE_CONFIG_DIR、空 setting-sources、显式 settings、strict 空 MCP、禁用 slash/Chrome、自动 CLAUDE.md/记忆和会话持久化。只开放 Read/Grep/Glob/有限 Bash，PreToolUse 检查源码路径和只读命令，拒绝越界/链接外逃/写入/shell 组合。固定主/子默认模型，不自动切换其它模型；原始返回的多模型偏离标记失败。任务中的 @ 转义，避免 CLI 自动文件注入。SessionStart 守卫未初始化时结果失败。

2026-10-09 原生 CLI 隔离探测：3 次工具调用触发守卫、1 次越界 Read 被拒绝，CLAUDE.md 测试规则和私有答案均未出现在发送给模型的请求中，合法读取与 structured_output 成功。探测使用本机模拟 Anthropic 服务；真实服务效果另在 GoFR 验收中记录。

最终只接受 structured_output，或 result 的完整 JSON/单一完整 JSON fence。空 comments 合法；未知 side/坐标保留 null；非法字段、路径、区间、退出错误或错误 envelope 明确失败，不从自然语言猜测。每条生成 ordinal ID，保留重复评论作为 G，不额外语义去重。stdout/stderr 和 provider 用量脱敏保留，无逐块事件文件。

## 阶段与恢复

```sh
# 从 code_review_agent 目录运行。先验证一个 PR。
.venv/bin/python benchmark.py --config config.local.json --run-id gofr-test --stage all --repo gofr-dev/gofr --limit 1 --format json
# 分阶段；参数与首次评审身份保持一致。
.venv/bin/python benchmark.py --config config.local.json --run-id formal-test --stage prepare --repo-count 10 --seed 42
.venv/bin/python benchmark.py --config config.local.json --run-id formal-test --stage review --reviewer both
.venv/bin/python benchmark.py --config config.local.json --run-id formal-test --stage score --k 1
.venv/bin/python benchmark.py --config config.local.json --run-id formal-test --stage report --format json
```

prepare 读取本地 JSON 并 clone/fetch/checkout 所选仓库源码；review 可补齐缺失的端点。每个 PR、每个评审器默认获得外层 1800 秒（`--review-timeout`，配置 `review_timeout_seconds`）；日常 main/session 入口默认不设外层总超时。项目内部按 Go 为每个 Diff 组独立分配 `--timeout` 分钟乘最大轮数，默认 15/30/45 分钟；Scan 每个文件默认 15 分钟。时间从取得并发槽位后开始，Plan、主评审各轮、压缩、定位和 Reflection 共享组截止时间，不按轮重置，组超时不取消其他组。阶段采用组和外层中更早的截止时间；`--timeout 0` 只关闭组限制，评测外层仍生效。源码准备与 Judge 评分另计。单次 API 默认超时 300 秒，工具迭代上限独立生效。超时保留完成文件及提交评论，区分 Group timeout 与 Review timeout，未完成结果不作为完整评分。已完成 reviewer 不再调用 Git/模型；失败/中断任务重新执行且追加 attempt，不复用旧对话。实现、规则、模型或任务配置改变需新 run-id。一个 run 同时只能有一个 prepare/review/score 执行者。外置规则正文及引用文件内容参与身份校验，不仅比较规则路径。

终端 stderr 实时输出 dataset、每 PR 的源码准备、project/claude 开始与结束、Judge 判分和报告路径；stdout 仍只输出最终结果。`--limit 1` 是一个 PR，并非一次模型请求；生产评审会执行分组、Plan、工具循环、多轮和 Reflection，同仓库两种评审器串行执行。首次运行还需 clone，模型与网络耗时不能以单次 API 延迟估计。

score/report 只读保存范围，不因 CLI 默认 repo/limit 改分母，不准备源码或执行评审。可更换 Judge 或 k 生成新的评分记录，旧评分保留；decided 语义对比可缓存，undecided 下次重试。CLI 模型字段检查在执行前进行，不依赖个人登录。旧 schema benchmark 不导入为新评测；只能复用冻结 dataset 清单。旧 freeze、run、subset、development-repo、pool/module/variant 和 baseline 参数明确拒绝。

## 评分口径与限制

路径和 left/right 必须一致，两个有效行区间距离不超过 k（默认 1）；未知侧/位置不能命中。Judge 仅接收匿名 A/B 技术评论和版本，不接收系统身份或主评审历史，返回 match/no_match/undecided 和理由。稳定最大基数一对一匹配防止重复输出多次命中同一参考；移除了旧官方兼容贪心辅表。

- M：已确认匹配数；G：最终评论数，包括重复和未知位置；N：全计划有效正参考数。
- Precision=M/G，Recall=M/N，F1=2M/(G+N)。整数计数汇总 micro；先按仓库计算再平均为 repo macro；Precision/Recall 零分母显示未定义，G=N=0 时 F1 约定为 0。
- 失败 M=0、N 保留；完整 G 不可得时显示 G_unknown_jobs、G_known 与未知 Precision/F1；Recall 和完成率仍可给出全计划保守结果。
- Judge 未判定用已确认/潜在关系计算 M_lower/M_upper，标记 incomplete，不按确定 no_match 处理。Mock 或 `--mirror` 测试数据永远标记流程模拟，不作为正式成果；`--dataset-dir` 本地原始数据可用于真实评测。
- 提供每 PR/重复轮次、side Recall、共同成功辅表、多轮均值/波动（单轮不造方差）。只有两边完整、Judge 无未判定且非模拟时给 project-minus-claude F1 百分点差。

此 Precision 是参考覆盖口径；未匹配评论可能是真实的新缺陷，不能称为实际误报率。行号附近参考命中不等于全量评论定位准确率，不能据此证明 >97%，也不能得到 Reflection 的特定拦截率提升。

效率包含每次失败/重试的评审耗时和用量。Claude usage 是整次 CLI 运行聚合，calls 的 count_unit 明确为 CLI aggregate，不能当作模型请求次数。项目覆盖分组、Plan、review、Reflection、定位、压缩；Claude OpenAI prompt_tokens 已含 cache 不再加，Anthropic uncached+cache-read+cache-creation 为总输入。缺字段是未知，不当零；按阶段保留已知 subtotal 和未知次数。Judge、准备、锁等待和清理单列，不估算未经确认的价格。

## 产物

| 位置 | 内容与用途 |
|---|---|
| .state/benchmarks/RUN_ID/dataset.json | 完整数据审计与含标注样本；selection 为本次实际评测范围，不传给 Agent |
| 同目录 run.json | 实现/模型/CLI/数据身份、保存范围、任务顺序、状态、attempt、checkout SHA/路径/耗时、最新 score ID |
| reviews/project或claude/JOB_ID.json | 统一最终评论；project session ID/usage，或 Claude 原始 envelope/stdout/stderr/守卫计数 |
| scores/SCORE_ID.json | 独立 Judge/k/结果摘要身份、匹配关系/理由/上下界、Judge 用量；重算不覆盖旧评分 |
| judge-cache/KEY.json | 已确认语义关系的可复用缓存；不永久缓存未判定 |
| reports/SCORE_ID.json | 从冻结评分重算的报告；非另一次模型实验 |
| reports/benchmarks/RUN_ID/SCORE_ID.md | 可阅读的对比表、状态和指标限制 |
| .state/sessions/SESSION_ID.jsonl | project 生产评审的单文件完整交互与文件检查点；不重复建立 benchmark events.jsonl |
| .cache/benchmark-repositories/... | 实际 Git 源码副本；owner/锁/进程 lease 在目录外，不被 clean 删除 |
| .cache/claude-review/JOB_ID/... | 临时显式配置、hook 配置和累计守卫计数，不保存 chunk 流 |

历史 benchmark 和报告保留原位，不能拿旧 B0/B1 数值当成新两套流程的成果。10 仓库全量真实评测需后续明确执行，单 PR 验收只证明实现链路和兼容性。
