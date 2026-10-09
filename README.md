# Code Review Agent

Python + AgentScope 独立代码审查项目。默认中文，支持英文、终端文本和 JSON，直接运行 Python 脚本。支持 Git 增量评审、全仓 Scan，以及本项目与原生 Claude Code 的 AACR 对比评测。Go/AACR 源码仅作只读参考。

历史 B0/B1 报告保留在原目录，属于旧实现；当前已移除单 Agent 基线与 Reflection/定位模块实验，历史数值不代表当前流程。现阶段不宣称误报拦截率 30.09%→52.63% 或定位准确率 >97%。

## 环境和配置

依赖仅安装到本项目 `.venv`。最低 Python 3.11；当前实测 macOS / Python 3.12.2 / AgentScope 2.0.7.post1。Python 3.11 兼容性依据依赖元数据审核，未实测其他系统。

```sh
python3 -m venv .venv
mkdir -p .cache/pip .cache/tmp
PIP_CACHE_DIR="$PWD/.cache/pip" TMPDIR="$PWD/.cache/tmp" .venv/bin/python -m pip install -r requirements.lock
cp config.example.json config.local.json
```

填写配置 reviewer / reflection / judge 的 `base_url`、`model`。仅支持 OpenAI 兼容 Chat Completions。密钥可使用 `CODE_REVIEW_REVIEWER_API_KEY`、`CODE_REVIEW_REFLECTION_API_KEY`、`CODE_REVIEW_JUDGE_API_KEY` 环境变量。Reflection 的空服务字段继承 reviewer，默认使用新消息进行单次批量复核；Judge 必须显式配置，正式判分要求 temperature=0。

优先级为参数 > `CODE_REVIEW_*` 环境变量 > 项目内 JSON > 默认值。例如 `CODE_REVIEW_LANGUAGE=en`、`CODE_REVIEW_MAX_CONCURRENCY=4`；列表环境变量使用 JSON 数组。配置、自定义/全局规则和输出路径在本项目，外部被审查仓库只读。缓存、临时文件、会话和报告定向 `.cache`、`.state`、`reports`。需要 Git 和支持 PCRE2 的 ripgrep。

## 执行方式

```sh
# 预览不需要模型，给出文件筛选原因。
.venv/bin/python main.py --repo /path/to/repo --preview --format json
# staged/unstaged/untracked 合并后的最终工作区变更。
.venv/bin/python main.py --config config.local.json --repo /path/to/repo
.venv/bin/python main.py --config config.local.json --repo /path/to/repo --from main --to feature --effort high --language en --format json
.venv/bin/python main.py --config config.local.json --repo /path/to/repo --commit COMMIT_SHA
# Git 或无历史目录的全文件扫描。
.venv/bin/python main.py --config config.local.json --mode scan --repo /path/to/source --batch-strategy by-language --batch-size 50
.venv/bin/python session.py --action list --format json
.venv/bin/python session.py --action show --session-id SESSION_ID --format json
.venv/bin/python session.py --action resume --session-id SESSION_ID --config config.local.json --format json
```

`--scan-path` 限制扫描范围；`--include` / `--exclude` 可重复；`--rule-file`、`--global-rule-file` 指定项目内规则；`--background` 提供需求背景；`--output` 保存结果。完整参数见各脚本 `--help`。

Diff 的 low / medium / high 为一/二/三轮，后轮无新增保留发现提前停止；多文件组以 `task_done.reviewed_paths` 明确覆盖。Scan 使用独立编排，每个文件只执行一次主评审循环，不随 effort 增加外层轮次。`code_comment` 提交时启动后台定位；Diff 每轮结束等待定位完成后，使用组内 Diff 和本轮新增评论单次批量隔离复核，仅提供报告错误评论/全部批准两个结果工具。Scan 不执行 Reflection；批次顺序执行、批内逐文件并发，等待本批定位完成后进行语义去重，替换实际评论集合并保存检查点，全部批次结束后生成摘要。Git 扫描使用 `git ls-files`，非 Git 目录遍历并解析根 `.gitignore`；目录分批依据第一级目录。`--no-plan`、`--no-dedup`、`--no-summary` 可关闭对应 Scan 阶段，`--dedup-min-comments` 默认 4。定位失败仍输出评论，未知行号为 `0`；复核拒绝或扫描去重的候选只保留在诊断中。复核异常为 undecided，保守保留。

没有总 Token 预算，保留单次上下文/输出容量、请求超时和工具迭代限额。文件过大或超过保守上下文容量估计时明确排除，不截断源码后冒充已审查。模型没有任意 shell 或写文件工具。

stdout 只输出最终结果，stderr 输出进度/错误。退出码：0=completed/no_files/preview，1=failed，2=参数/配置错误，3=partial，130=中断；没有发现不等于失败，不按发现自动触发 CI 门禁。

range / commit / scan 可恢复，工作区不可跨运行恢复。恢复校验输入、规则、提示词、模型和生效配置，复用原审查及完成的后处理，输出 reused/rerun 数量；扫描整合/摘要可能重新生成。历史与本次用量分列。每次评审只保存 `.state/sessions/<session_id>.jsonl`（0600），不保存独立源码快照或逐块 SDK 事件；恢复从磁盘重新构造并校验输入，复用已完成文件结果，不恢复旧对话。旧八文件会话不支持读取或恢复，原目录保持不变。详见 [单 JSONL 会话与恢复](docs/sessions.md)；本次存储改造通过 136 项离线测试，见 [存储验收记录](reports/verification/session-jsonl.md)。

## 评测和开发

默认只读项目自带的 `dataset/positive_samples.json` 与 `dataset/negative_samples.json`，也可用 `--dataset-dir` 指定本地 AACR 目录；不再由脚本下载 Hugging Face 数据。正、负文件分别赋 label=1/0，按文件 SHA256 固定数据身份；默认数据、规则、提示词、缓存和运行产物均位于本项目，不依赖 Go/AACR 参考目录。seed=42 正式清单：10 仓库、35 PR。支持显式仓库和数量子集，默认评审一次，可指定重复次数。直接比较原始 `source_commit` 与 `target_commit`，评测前将真实受管副本 checkout 到 TARGET；同仓库所有任务串行，仓库间可配置并发。

```sh
# 一个 GoFR PR：prepare → project + Claude → Judge → report。
.venv/bin/python benchmark.py --config config.local.json --run-id gofr-comparison --stage all --repo gofr-dev/gofr --limit 1 --format json
# 分阶段执行；review/score/report 复用保存的样本范围。
.venv/bin/python benchmark.py --config config.local.json --run-id gofr-comparison --stage review --reviewer claude
.venv/bin/python benchmark.py --config config.local.json --run-id gofr-comparison --stage score --k 1
.venv/bin/python benchmark.py --config config.local.json --run-id gofr-comparison --stage report --format json
# 正式清单，按需选择三次独立评审。未运行即无正式成果。
.venv/bin/python benchmark.py --config config.local.json --run-id aacr-comparison --stage all --repo-count 10 --seed 42 --repetitions 3
```

需预先安装可用的 `claude` CLI。默认 Claude 继承 reviewer 模型/凭据；DeepSeek 已知入口自动映射至 `/anthropic`，其它服务显式设置 `CODE_REVIEW_CLAUDE_BASE_URL`。可用 `--claude-config claude.local.json` 提供评测专用配置；不影响普通 review 配置。非 bare 启动使用独立配置、显式工具守卫和最终 JSON，无 MCP；不加载个人配置、自动 CLAUDE.md 或记忆。

旧缓存和运行状态已按用户要求清理；下一次评测重新 clone，仓库目录为 `.cache/benchmark-repositories/owner__name`。不会为每个 PR 重 clone，不创建任务 worktree。标注、配置和输出均位于源码目录之外。完整流程、指标分母、失败/未知处理和产物用途见 [评测说明](docs/evaluation.md)。

仓库位置、缓存与持久化文件的用途，以及日常评审/本地数据评测命令见 [目录与运行速查](docs/storage-and-commands.md)。

变量使用描述性英文 snake_case，类使用 PascalCase，公共接口具备类型标注；中文注释说明原因、边界和失败处理。按职责拆模块，不建立混杂的 utils/common 或不必要的框架。动态 JSON 在边界转换，正式审查不依赖 evaluation。目录和修改入口见 [架构说明](docs/architecture.md)，能力适配见 [迁移映射](docs/migration.md)。

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
.venv/bin/python tests/verify_project.py
```

测试使用可控响应，包含本机临时 HTTP 端点；禁止监听端口的沙箱需允许本机监听。测试只验证功能，不能当作效果指标。检查记录在 `reports/verification/`，检查缓存与临时仓库在项目 `.cache`。
