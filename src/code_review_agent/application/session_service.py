"""只读 journal 查询；继续执行统一经由正式 review 服务。"""

from __future__ import annotations

from ..contracts import JsonObject, JsonValue
from ..project_paths import ProjectPaths
from ..sessions.replay import load_session


def list_sessions(paths: ProjectPaths) -> JsonObject:
    root = paths.writable(".state/sessions")
    items: list[JsonValue] = []
    skipped: list[JsonValue] = []
    for file in sorted(root.glob("*.jsonl")):
        try:
            view = load_session(paths, file.stem)
            items.append(
                {
                    **view.manifest,
                    "status": view.result().status.value,
                    "damaged_tail": view.damaged_tail,
                }
            )
        except ValueError as error:
            skipped.append({"session_id": file.stem, "reason": str(error)})
    for directory in sorted(root.glob("*")):
        if directory.is_dir():
            skipped.append(
                {"session_id": directory.name, "reason": "Unsupported legacy session format"}
            )
    return {"schema_version": 2, "sessions": items, "skipped": skipped}


def show_session(paths: ProjectPaths, identifier: str) -> JsonObject:
    view = load_session(paths, identifier)
    return {
        "manifest": view.manifest,
        "result": view.result().to_json(),
        "event_count": len(view.records),
        "damaged_tail": view.damaged_tail,
        "completed_files": [path for path in sorted(view.reusable)],
        "model_call_count": sum(record["name"] == "model_call" for record in view.records),
        "tool_execution_count": sum(record["name"] == "tool_execution" for record in view.records),
        "has_session_end": view.end is not None,
    }
