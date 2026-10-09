"""最终结果资格、终端文本和兼容 JSON 字段；此模块不发起评审。"""

from __future__ import annotations

from typing import cast

from ..contracts import JsonObject, JsonValue, ReviewResult, RunStatus
from ..review.qualification import eligible_ids
from ..sessions.store import redact


def eligible_comments(result: ReviewResult) -> list[JsonValue]:
    locations = {item.candidate_id: item.location for item in result.locations}
    qualified = eligible_ids(result)
    comments: list[JsonValue] = []
    for item in result.candidates:
        location = locations.get(item.candidate_id)
        if item.candidate_id not in qualified:
            continue
        comments.append(
            {
                "candidate_id": item.candidate_id,
                "path": location.path if location else item.path,
                "side": location.side.value if location else item.side.value,
                "start_line": location.start_line if location else item.proposed_start or 0,
                "end_line": location.end_line if location else item.proposed_end or 0,
                "content": result.rewritten_content.get(item.candidate_id, item.content),
                "category": item.category,
                "severity": item.severity,
                "existing_code": location.snippet if location else item.existing_code,
                "suggestion_code": item.suggestion_code,
            }
        )
    return comments


def output_json(
    result: ReviewResult, public_config: JsonObject, secrets: tuple[str, ...] = ()
) -> JsonObject:
    value = result.to_json()
    value.update(
        {
            "schema_version": 1,
            "session_id": result.manifest.session_id,
            "llm": public_config.get("reviewer"),
            "comments": eligible_comments(result),
            "summary": {
                "selected_files": len(result.coverage),
                "reviewed_files": sum(item == "reviewed" for item in result.coverage.values()),
                "candidate_count": len(result.candidates),
                "comment_count": len(eligible_comments(result)),
            },
            "resume": {
                "parent_session": result.manifest.parent_session,
                "reused": result.reused_units,
                "rerun": result.rerun_units,
            },
        }
    )
    return cast(JsonObject, redact(value, secrets))


def output_text(value: JsonObject, language: str) -> str:
    english = language == "en"
    heading = "Review status" if english else "审查状态"
    lines = [f"{heading}: {value['status']}", f"Session: {value.get('session_id', '')}"]
    comments = cast(list[JsonObject], value.get("comments", []))
    for comment in comments:
        lines.append(
            f"{comment['path']}:{comment['start_line']}-{comment['end_line']} [{comment['severity']}] {comment['content']}"
        )
    if not comments:
        lines.append(
            "No publishable findings."
            if english
            else "无可发布评论；状态和候选诊断说明审查是否完成。"
        )
    for warning in cast(list[str], value.get("warnings", [])):
        lines.append(("Warning: " if english else "提示: ") + warning)
    if value.get("project_summary"):
        lines.append(str(value["project_summary"]))
    return "\n".join(lines)


def exit_code(status: RunStatus) -> int:
    return 3 if status == RunStatus.PARTIAL else (1 if status == RunStatus.FAILED else 0)
