"""运行关联与 SDK 实际工具执行聚合；不改变 Agent 的事件流。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any, cast

from agentscope.agent import Agent
from agentscope.message import ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from ..contracts import JsonObject, JsonValue
from ..sessions.records import make_record

UNIT_ID: ContextVar[str | None] = ContextVar("review_unit", default=None)
RecordSink = Callable[[JsonObject], None]


@contextmanager
def unit_scope(unit_id: str) -> Iterator[None]:
    token = UNIT_ID.set(unit_id)
    try:
        yield
    finally:
        UNIT_ID.reset(token)


def now() -> str:
    return datetime.now(UTC).isoformat()


def json_value(value: Any) -> JsonValue:
    """SDK 对象仅在这个序列化边界展开，不转储整个 AgentState。"""

    def encode(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if is_dataclass(item):
            return asdict(cast(Any, item))
        raise TypeError(f"Unsupported SDK value: {type(item).__name__}")

    return cast(JsonValue, json.loads(json.dumps(value, default=encode, ensure_ascii=False)))


def interaction(
    name: str,
    value: JsonObject,
    *,
    stage: str,
    operation_id: str,
    unit_id: str | None,
    parent_operation_id: str | None = None,
) -> JsonObject:
    metadata: JsonObject = {"stage": stage, "operation_id": operation_id}
    if unit_id:
        metadata["unit_id"] = unit_id
    if parent_operation_id:
        metadata["parent_operation_id"] = parent_operation_id
    return make_record(name, value, metadata)


class ToolRecordingMiddleware(MiddlewareBase):
    """on_acting 返回最终 ToolResponse，SDK 已替我们完成分块合并。"""

    def __init__(
        self, sink: RecordSink | None, stage: str, unit_id: str | None, tool_owners: dict[str, str]
    ) -> None:
        self.sink, self.stage, self.unit_id = sink, stage, unit_id
        self.tool_owners = tool_owners

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        call = input_kwargs["tool_call"]
        started, started_at = time.monotonic(), now()
        response: ToolResponse | None = None
        status, error = "error", None
        try:
            async for chunk in next_handler(**input_kwargs):
                if isinstance(chunk, ToolResponse):
                    response = chunk
                    status = (
                        "success"
                        if chunk.state == ToolResultState.SUCCESS
                        else "cancelled"
                        if chunk.state == ToolResultState.INTERRUPTED
                        else "error"
                    )
                yield chunk
        except asyncio.CancelledError:
            status, error = "cancelled", "Tool execution cancelled"
            raise
        except Exception as failure:
            error = f"{type(failure).__name__}: {failure}"
            raise
        finally:
            if self.sink:
                arguments = json_value(call.input)
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                self.sink(
                    interaction(
                        "tool_execution",
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": arguments,
                            "output": json_value(response) if response else None,
                            "error": error,
                            "status": status,
                            "started_at": started_at,
                            "ended_at": now(),
                            "duration_seconds": time.monotonic() - started,
                        },
                        stage=self.stage,
                        operation_id=uuid.uuid4().hex,
                        unit_id=self.unit_id,
                        parent_operation_id=self.tool_owners.get(call.id),
                    )
                )
