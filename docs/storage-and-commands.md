# 目录与运行速查

项目直接执行 Python 脚本，不需要启动常驻服务。工作目录为 `/Users/dyh/Desktop/code-review/code_review_agent`，使用已有 `.venv` 与 `config.local.json`。

## 数据与源码

标注来源为 `/Users/dyh/Desktop/code-review/code_review_agent/dataset/positive_samples.json`、`negative_samples.json`。默认只读它们，也可传 `--dataset-dir`。它们提供 PR、提交和参考评论，不包含仓库源码。正/负文件赋 label=1/0，按文件 SHA256 冻结运行的数据身份。

旧仓库缓存已清理。下次评测将源码下载到 `/Users/dyh/Desktop/code-review/code_review_agent/.cache/benchmark-repositories/`，采用以下可读目录名：

| 仓库 | 本地目录名 |
|---|---|
| gofr-dev/gofr | gofr-dev__gofr |
| cline/cline | cline__cline |
| ClickHouse/ClickHouse | ClickHouse__ClickHouse |

目录采用 `owner__name`，无需根据哈希查找仓库。每个仓库一个副本，多个 PR 共用 Git 历史，每次任务切换到自己的 target_commit；当前展开的代码只是某个 TARGET，不代表最新默认分支。准备时缺仓库才 clone，缺版本对象则 fetch；owner 标记、锁、进程使用记录在仓库目录外。

## .cache 与 .state 的用途

| 路径（相对项目） | 内容 | 用途 |
|---|---|---|
| .cache/benchmark-repositories/ | Git 仓库、源码、归属/锁/使用权记录 | 评测读取真实代码，复用下载；评审过程中不手动切换或删除 |
| .cache/claude-review/ | 本次 CLI 隔离配置、工具 hook、累计计数 | 保证 Claude 按同规则、受限工具执行 |
| .cache/pip、ruff、mypy、pytest、pycache 等 | 依赖下载、静态检查和 Python 缓存 | 开发工具加速，不是评审结果 |
| .cache/test-tmp、tmp、各类 probe/smoke/alignment/verification 目录 | 临时测试仓库、兼容性与对齐探测 | 开发验证，不是正式评测样本 |
| .state/sessions/SESSION_ID.jsonl | 单次项目评审的交互、模型用量、文件检查点与结果 | 查看执行和恢复已完成文件；不恢复旧对话继续思考 |
| .state/benchmarks/RUN_ID/ | 某次评测的数据身份、任务、输出及评分 | 按 run-id 分开，支持复用完成任务和重新评分 |
| reports/benchmarks/RUN_ID/ | 可阅读的 Markdown 评测报告 | 用户查看质量指标、时间与用量 |
| reports/verification/ | 测试和真实验收证据 | 工程验证记录 |
| reports/其他旧运行目录/ | 早期 B0/B1 等报告 | 历史结果，不代表当前实现 |

2026-10-09 已按用户要求清空 `.cache` 与 `.state`，包括旧仓库、测试缓存、会话和评测结果。程序下次运行会按需重新创建目录和产物；源码、配置、虚拟环境和项目自带数据集保留。`reports` 仍保留历史报告，历史报告中的缓存/状态路径可能已经删除，不能用于恢复或重评分。

一个当前评测 RUN_ID 的结构：

```text
.state/benchmarks/RUN_ID/
  dataset.json                 转换后的 PR/参考、全量审计、本次实际范围、来源指纹
  run.json                     选中任务、attempt/状态、配置身份、checkout/耗时
  reviews/project/JOB_ID.json   本项目最终评论、会话引用、用量
  reviews/claude/JOB_ID.json    Claude 最终评论、脱敏原始结果、诊断、用量
  scores/SCORE_ID.json          Judge 关系/理由、一对一匹配、评分与未知项
  judge-cache/KEY.json          可复用的已确认语义判定
  reports/SCORE_ID.json         从已有评分计算出的结构化报告
```

`dataset.json` 是原始两个 JSON 的运行快照和转换结果，不是另一套真值。标注只给 Judge；源码工作区不放标注。

## 日常评审命令

```sh
cd /Users/dyh/Desktop/code-review/code_review_agent
REVIEW_REPO=/path/to/your/repository

# 工作区尚未提交的修改
.venv/bin/python main.py --config config.local.json --repo "$REVIEW_REPO"
# 两个版本之间的增量；日常入口默认采用 merge-base
.venv/bin/python main.py --config config.local.json --repo "$REVIEW_REPO" --from BASE_SHA --to HEAD_SHA
# 单次提交
.venv/bin/python main.py --config config.local.json --repo "$REVIEW_REPO" --commit COMMIT_SHA
# 全仓扫描；可追加 --scan-path src
.venv/bin/python main.py --config config.local.json --repo "$REVIEW_REPO" --mode scan
```

替换仓库路径和 SHA。追加 `--format json` 输出 JSON，`--language en` 切换英文，`--output reports/review.json` 保存结果。无需再次建立虚拟环境或覆盖已有配置。

## 本地数据评测命令

```sh
cd /Users/dyh/Desktop/code-review/code_review_agent

# 一个 GoFR PR，本项目 + Claude + Judge + 报告
.venv/bin/python benchmark.py --config config.local.json \
  --dataset-dir /Users/dyh/Desktop/code-review/code_review_agent/dataset \
  --run-id gofr-local-1 --stage all --reviewer both \
  --repo gofr-dev/gofr --limit 1 --format json

# 正式抽样10仓库，所有选中仓库的PR，每个评审器默认一次
.venv/bin/python benchmark.py --config config.local.json \
  --dataset-dir /Users/dyh/Desktop/code-review/code_review_agent/dataset \
  --run-id aacr-local-seed42 --stage all --reviewer both \
  --repo-count 10 --seed 42 --format json

# 对已执行的运行只重新评分，不调用评审器、不下载或切换源码
.venv/bin/python benchmark.py --config config.local.json \
  --run-id gofr-local-1 --stage score --k 1 --format json
.venv/bin/python benchmark.py --config config.local.json \
  --run-id gofr-local-1 --stage report --format json
```

`--dataset-dir` 可省略，默认就是这里指定的本地目录。`--stage prepare` 只读数据并准备源码，不调用模型；`review` 执行评审，`score` 判分，`report` 生成报告。`--repetitions 3` 创建三次独立评审，需新 run-id。`--reviewer project` 或 `claude` 仅执行其中一边，但正式对比需要双方结果。双评审器需可用的 Claude CLI；当前 DeepSeek 配置默认继承 reviewer 并映射 Anthropic 入口。

本地 JSON 与历史 HF 数据有一条标签不同，因此本地运行使用新 run-id，不覆盖旧结果。切换本地输入、代码或配置身份后用新 run-id；单次评测完成后重评分/生成报告可继续复用同一个 run-id。
