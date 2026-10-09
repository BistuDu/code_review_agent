# 单 JSONL 会话与恢复

每次实际评审生成一个文件：`.state/sessions/<session_id>.jsonl`。Preview 不生成会话。旧 `.state/sessions/<session_id>/` 八文件目录保留，但新版本不读取或恢复它；查询列表会显示跳过原因。单文件约束只针对评审会话，`.state/benchmarks` 的数据集、评分产物及用户显式导出的报告仍独立保存。

## 每一行保存什么

所有行通过 AgentScope `CustomEvent` 构造，顶层为 `type: CUSTOM`、`id`、`created_at`、`metadata`、`name`、`value`。业务协议为 `metadata.schema = code-review.session/v2`。SDK 在运行时继续产生事件，本文件不逐条保存 SDK START/DELTA/END，不保存整个 AgentState。

| name | 保存的信息 | 作用 |
| --- | --- | --- |
| `session_start` | 有效参数/非敏感配置、仓库和版本、全部可读文件的指纹、入选文件、规则/提示词/模型身份、父会话、历史用量 | 在最终结果产生前提供输入校验与恢复条件 |
| `model_call` | 一次逻辑模型调用的实际 SDK messages、工具声明、请求配置、完整响应或错误、模型/服务身份、重试诊断、usage 和耗时 | 审查模型看到什么、返回什么、失败在哪里；包含压缩与 direct calls |
| `tool_execution` | 一次实际执行的工具名、校验后参数、tool_call_id、最终输出/错误、状态和耗时 | 查看工具交互；模型只提出或选择结果工具不等于实际执行 |
| `review_item` | 一个文件的 completed/failed/reused 状态、候选、定位/复核结果、coverage、warnings 和结果引用 | 文件级 checkpoint；completed/reused 才可复用，零缺陷也有完成记录 |
| `scan_batch_finalized` | Scan 批次身份、最终展示引用和必要的合并评论改写 | 区分安全恢复输入与跨文件去重后的展示结果 |
| `session_end` | 状态、coverage、项目摘要、warnings、执行统计和最终评论引用顺序 | 查询完整结果，不再复制全部候选和工具记录 |

例如一次 `file_read` 的记录结构如下。示例省略 SDK 输出块的辅助字段：

```json
{
  "type": "CUSTOM",
  "id": "unique-event-id",
  "created_at": "2026-10-09T16:00:00",
  "metadata": {
    "schema": "code-review.session/v2",
    "session_id": "session-id",
    "seq": 3,
    "unit_id": "file-group-id",
    "stage": "review.round1",
    "operation_id": "tool-operation-id",
    "parent_operation_id": "model-operation-id"
  },
  "name": "tool_execution",
  "value": {
    "tool_call_id": "sdk-tool-id",
    "name": "file_read",
    "arguments": {"file_path": "a.py"},
    "output": {"state": "success", "content": [{"type": "text", "text": "1|return x"}]},
    "error": null,
    "status": "success",
    "started_at": "2026-10-09T08:00:00+00:00",
    "ended_at": "2026-10-09T08:00:00.01+00:00",
    "duration_seconds": 0.01
  }
}
```

216 个 SDK 事件不会再原样变成 216 条过程记录。持久化条数由逻辑模型调用、实际工具执行和业务边界决定；一次调用内部重试增加 attempt_count，不增加逻辑调用数。模型响应中的工具声明不计入工具执行数。usage 不可取得时记为 unknown/null，各 attempt 的已知用量仍可查阅。

并发 Agent 的记录仍在同一个文件，但用 unit_id、stage、operation_id 和 tool_call_id 区分来源；seq 表示写入顺序，不代表业务按序执行。工具的异步评论提交确认不表示后台定位已经完成。

## Resume 恢复什么

Resume 读取业务 checkpoint，复用已完成文件的评审结果，不给 Agent 加载旧 messages，也不执行旧工具。Diff 的执行单位仍可以是多文件组，但恢复以文件为单位：排除已完成文件后，重新分组未完成文件，并为它们创建新的 Agent 对话。

文件只有完成主评审及本模式必要后处理之后才记 completed；定位失败后保留评论、Reflection 异常保留评论属于原有 settled 降级结果，不会因缺少有效位置而强制重跑。外层轮次中断形成的 provisional findings 不能复用，failed 记录只提供失败诊断。

Scan 在本批定位和去重完成后先保存安全的文件结果，再保存批次展示引用。跨文件合并保留来源文件的必要发现，避免一个文件重跑时丢失另一文件的问题。批次写入中途停止，已经保存的文件仍可复用；恢复时可能重新执行批次去重和项目摘要，这些调用属于本次用量。未最终化的父批次结果在查询中明确标记，不能当成最终去重输出。

每次 Resume 生成新 JSONL，写 parent_session 及完整 reused 结果，使子会话不依赖父日志才能再次恢复。历史用量在启动记录继承一次，当前用量来自当前模型记录，不按文件重复累加共享组的模型调用。

## 恢复前提与中断边界

- range/commit 使用冻结的完整 SHA，从保留的磁盘仓库重新构造输入；移动分支不会改变被恢复的版本。Git 对象缺失时明确报错。
- Scan 从磁盘重建，全部工具可读文件的指纹和文件集合都要一致；未入选但可读的上下文文件改变，也会拒绝复用。
- 输入、规则、提示词资源、模型及生效配置必须一致；工作区跨运行 Resume 不支持。
- 不保存独立完整源码快照。实际模型请求和工具输出中仍可能包含必要代码片段，记录使用现有凭据脱敏；源码身份从磁盘和未脱敏内容指纹验证，不能拿脱敏日志恢复源码。
- 启动、文件结果、批次最终化与结束边界同步落盘；模型/工具完整交互结束即追加。进程被硬杀时，正在执行或最近未同步的交互可能缺失，未完成文件会重跑。
- 不要求有 session_end。非换行结尾的最后一条视作未提交并忽略；已提交记录损坏或引用非法则拒绝恢复。父 JSONL 始终只读，恢复创建新文件。

## 查询入口与代码位置

```sh
.venv/bin/python session.py --action list --format json
.venv/bin/python session.py --action show --session-id SESSION_ID --format json
.venv/bin/python session.py --action resume --session-id SESSION_ID --config config.local.json --format json
```

`sessions/records.py` 定义记录契约，`journal.py` 负责追加/读取和落盘，`results.py` 编解码文件 checkpoint，`recorder.py` 写业务边界，`replay.py` 还原结果视图。`runtime/model_factory.py` 聚合逻辑模型调用，`runtime/recording.py` 使用 SDK on_acting 获取最终工具结果；`application/review_service.py` 和 `session_service.py` 分别提供运行/恢复与查询。

不启用外部 Trace 服务。当前只是必要的调用关联、耗时、状态与 journal 记录。旧真实候选对话存档入口和 B0/模块实验已移除；本项目/Claude Code 评测的结果及评分在 benchmark 目录，project 评审调用仍生成同样的单 JSONL。旧正常 baseline=false/缺省会话兼容恢复，baseline=true 会话明确拒绝。

本契约对应 OpenSpec `simplify-session-jsonl-storage`，取代此前 `python-agentscope-core-review` 中多文件会话、整组恢复及候选历史存档的旧规划。后续同步规格应避免重新引入这些旧条款。
