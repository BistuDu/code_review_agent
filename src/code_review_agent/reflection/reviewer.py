"""按组批量、Diff 限定的单次复核；解析失败保留本轮候选。"""

from __future__ import annotations

import json
import re

from ..contracts import FindingCandidate, InputMode, ReflectionDecision, ReviewInput
from ..inputs.selection import RESOURCE_ROOT
from ..runtime.agentscope_adapter import AgentRun
from ..runtime.stages import StageRunner, parse_json_response
from .tools import FILTER_TOOLS

REFLECTION_POLICY = "go-group-diff-v1"


def comment_indices(ids: object, total: int) -> set[int] | None:
    if ids is None:
        return set()
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        return None
    indices: set[int] = set()
    for item in ids:
        match = re.match(r"c-\s*([+-]?\d+)", item)
        if match and 0 <= (index := int(match.group(1))) < total:
            indices.add(index)
    return indices


def parse_filter_result(result: AgentRun, total: int) -> tuple[set[int] | None, tuple[str, ...]]:
    indices: set[int] | None = None
    analysis: list[str] = []
    for call in result.tool_calls:
        if call.get("name") == "approve_all_comments":
            if indices is None:
                indices = set()
        elif call.get("name") == "report_incorrect_comments":
            try:
                arguments = call.get("arguments")
                value = json.loads(arguments) if isinstance(arguments, str) else arguments
                if not isinstance(value, dict):
                    continue
                selected = comment_indices(value.get("comment_ids"), total)
                if selected is None:
                    continue
                if indices is None:
                    indices = set()
                indices.update(selected)
                entries = value.get("analysis")
                if isinstance(entries, list):
                    analysis.extend(item for item in entries if isinstance(item, str))
            except (ValueError, TypeError):
                continue
    if indices is None:
        try:
            indices = comment_indices(parse_json_response(result.text), total)
        except (ValueError, TypeError):
            pass
    return indices, tuple(analysis)


async def reflect_group(
    candidates: tuple[FindingCandidate, ...],
    review_input: ReviewInput,
    runner: StageRunner,
    *,
    target_paths: tuple[str, ...],
) -> list[ReflectionDecision]:
    # Go 按组内当前路径收集；跨文件重新归属到组外的评论不交给本组复核。
    selected = tuple(item for path in target_paths for item in candidates if item.path == path)
    decisions = {
        item.candidate_id: ReflectionDecision(item.candidate_id, "keep", "Refiled outside group")
        for item in candidates
        if item.path not in target_paths
    }
    if selected:
        files = {file.path: file for file in review_input.targets}
        diff = "\n\n".join(
            f'<file path="{path}">\n'
            + (files[path].content if review_input.mode == InputMode.SCAN else files[path].diff)
            + "\n</file>"
            for path in target_paths
            if path in files
        )
        comments = json.dumps(
            [
                {
                    "id": f"c-{index}",
                    "path": item.path,
                    "content": item.content,
                    **({"existing_code": item.existing_code} if item.existing_code else {}),
                }
                for index, item in enumerate(selected)
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        system = (RESOURCE_ROOT / "prompts/review_filter_task_system.md").read_text()
        user = (RESOURCE_ROOT / "prompts/review_filter_task_user.md").read_text()
        user = (
            user.replace("{{path}}", ", ".join(target_paths))
            .replace("{{diff}}", diff)
            .replace("{{comments}}", comments)
        )
        try:
            result = await runner.run(
                "reflection.isolated",
                system,
                user,
                single_call=True,
                tools=FILTER_TOOLS,
                allow_compression=False,
            )
            indices, analysis = (
                parse_filter_result(result, len(selected)) if result.completed else (None, ())
            )
            for index, item in enumerate(selected):
                decision = (
                    "undecided" if indices is None else "reject" if index in indices else "keep"
                )
                if indices is None:
                    reason = result.error or "Filter response could not be parsed"
                elif analysis:
                    reason = "\n".join(analysis)
                elif decision == "reject":
                    reason = "Reported incorrect by group filter"
                else:
                    reason = "Approved by group filter"
                decisions[item.candidate_id] = ReflectionDecision(
                    item.candidate_id, decision, reason, analysis
                )
        except Exception as error:
            decisions.update(
                {
                    item.candidate_id: ReflectionDecision(
                        item.candidate_id,
                        "undecided",
                        f"{type(error).__name__}: group filter failed",
                    )
                    for item in selected
                }
            )
    return [decisions[item.candidate_id] for item in candidates]
