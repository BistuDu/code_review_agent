"""单会话 JSONL 的 SDK CustomEvent 契约；不存储 SDK 分块。"""

from __future__ import annotations

from typing import cast

from agentscope.event import CustomEvent

from ..contracts import JsonObject

SCHEMA = "code-review.session/v2"
NAMES = {
    "session_start",
    "model_call",
    "tool_execution",
    "review_item",
    "scan_batch_finalized",
    "session_end",
}
DURABLE = {"session_start", "review_item", "scan_batch_finalized", "session_end"}
REQUIRED: dict[str, dict[str, type]] = {
    "session_start": {
        "manifest": dict,
        "input": dict,
        "arguments": dict,
        "configuration": dict,
        "historical_usage": list,
    },
    "model_call": {
        "model": str,
        "request": dict,
        "status": str,
        "duration_seconds": float,
        "attempt_count": int,
        "usage": dict,
    },
    "tool_execution": {
        "tool_call_id": str,
        "name": str,
        "arguments": dict,
        "status": str,
        "duration_seconds": float,
    },
    "review_item": {"file_path": str, "status": str, "input_fingerprint": str, "payload": dict},
    "scan_batch_finalized": {"batch": dict, "display": list},
    "session_end": {"status": str, "coverage": dict, "display_refs": list},
}


def make_record(name: str, value: JsonObject, metadata: JsonObject) -> JsonObject:
    event = CustomEvent(name=name, value=value, metadata=metadata)
    return cast(JsonObject, event.model_dump(mode="json"))


def validate_record(record: JsonObject, session_id: str, seq: int) -> None:
    """读写共享验证；具体 finding 引用由 replay 检查。"""
    event = CustomEvent.model_validate(record)
    if event.type != "CUSTOM" or event.name not in NAMES:
        raise ValueError("Unsupported journal event type")
    metadata = event.metadata
    if metadata.get("schema") != SCHEMA:
        raise ValueError("Unsupported journal schema")
    if metadata.get("session_id") != session_id or type(metadata.get("seq")) is not int:
        raise ValueError("Invalid journal identity or sequence")
    if metadata["seq"] != seq:
        raise ValueError("Journal sequence mismatch")
    for key, expected in REQUIRED[event.name].items():
        value = event.value.get(key)
        if expected is float:
            valid = isinstance(value, (float, int)) and not isinstance(value, bool) and value >= 0
        else:
            valid = type(value) is expected
        if not valid:
            raise ValueError(f"Invalid {event.name}.{key}")
    if event.name in {"model_call", "tool_execution"}:
        if event.value["status"] not in {"success", "error", "timeout", "cancelled"}:
            raise ValueError("Invalid interaction status")
        if not metadata.get("operation_id") or not metadata.get("stage"):
            raise ValueError("Missing interaction correlation")
    if event.name == "model_call" and event.value["attempt_count"] < 1:
        raise ValueError("Model call must contain an actual attempt")
    if event.name == "review_item" and event.value["status"] not in {
        "completed",
        "failed",
        "reused",
    }:
        raise ValueError("Invalid review item status")
