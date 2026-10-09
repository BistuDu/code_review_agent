"""Chat Completions 适配；SDK 重试内收集 attempt，逻辑调用末尾记录一次。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import asdict
from typing import Any, cast

from agentscope.credential import OpenAICredential
from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse, FinishedReason, OpenAIChatModel
from pydantic import SecretStr

from ..config import ModelSettings
from ..contracts import JsonObject, UsageRecord
from .recording import RecordSink, interaction, json_value, now


class TrackedChatModel(OpenAIChatModel):
    def __init__(self, settings: ModelSettings, stage: str, usage: list[UsageRecord]) -> None:
        settings.require(stage)
        self.stage, self.usage_records = stage, usage
        self.provider_identity: JsonObject = {
            "base_url": settings.base_url,
            "protocol": "chat_completions",
        }
        self.record_sink: RecordSink | None = None
        self.unit_id: str | None = None
        self.tool_owners: dict[str, str] = {}
        self.timeout_deadline: float | None = None
        self._attempts: list[JsonObject] | None = None
        self._operation_id: str | None = None
        super().__init__(
            credential=OpenAICredential(
                api_key=SecretStr(settings.api_key), base_url=settings.base_url
            ),
            model=settings.model,
            parameters=OpenAIChatModel.Parameters(
                max_tokens=settings.max_output_tokens,
                temperature=settings.temperature,
                parallel_tool_calls=False,
            ),
            stream=False,
            max_retries=settings.retries,
            context_size=settings.context_tokens,
            client_kwargs={"timeout": settings.timeout_seconds, "max_retries": 0},
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._logical_call(super().__call__, args, kwargs)

    async def generate_structured_output(self, *args: Any, **kwargs: Any) -> Any:
        return await self._logical_call(super().generate_structured_output, args, kwargs)

    def _cancel_status(self) -> str:
        return (
            "timeout"
            if self.timeout_deadline and time.monotonic() >= self.timeout_deadline
            else "cancelled"
        )

    async def _logical_call(
        self, call: Callable[..., Awaitable[Any]], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        if self._attempts is not None:
            return await call(*args, **kwargs)
        started, started_at = time.monotonic(), now()
        self._attempts, self._operation_id = [], uuid.uuid4().hex
        response, error, status = None, None, "success"
        try:
            response = await call(*args, **kwargs)
            if getattr(response, "finished_reason", None) == FinishedReason.INTERRUPTED:
                status = self._cancel_status()
            return response
        except asyncio.CancelledError:
            status, error = self._cancel_status(), "Model invocation cancelled"
            raise
        except Exception as failure:
            status = (
                "timeout"
                if isinstance(failure, TimeoutError) or "Timeout" in type(failure).__name__
                else "error"
            )
            error = f"{type(failure).__name__}: {failure}"
            raise
        finally:
            attempts, operation_id = self._attempts, self._operation_id
            self._attempts, self._operation_id = None, None
            if attempts:
                usages = [item["usage"] for item in attempts]
                totals: dict[str, int | None] = {}
                for key in ("input_tokens", "output_tokens", "cached_tokens"):
                    values = [item.get(key) if isinstance(item, dict) else None for item in usages]
                    totals[key] = (
                        sum(cast(int, value) for value in values)
                        if all(type(value) is int for value in values)
                        else None
                    )
                duration = time.monotonic() - started
                usage = UsageRecord(
                    self.stage,
                    totals["input_tokens"],
                    totals["output_tokens"],
                    totals["cached_tokens"],
                    duration,
                )
                self.usage_records.append(usage)
                # 请求通常不变；SDK 结构化纠错改变请求时只记录差异项。
                final_response = json_value(response)
                request = attempts[0]["request"]
                for item in attempts:
                    if item.get("request") == request:
                        item.pop("request", None)
                    if item.get("response") == final_response:
                        item.pop("response", None)
                payload: JsonObject = {
                    "model": self.model,
                    "provider": self.provider_identity,
                    "request": request,
                    "response": final_response,
                    "error": error,
                    "status": status,
                    "started_at": started_at,
                    "ended_at": now(),
                    "duration_seconds": duration,
                    "attempt_count": len(attempts),
                    "usage": cast(JsonObject, asdict(usage)),
                    "attempts": cast(Any, attempts),
                }
                if isinstance(response, ChatResponse):
                    for block in response.content:
                        if isinstance(block, ToolCallBlock):
                            self.tool_owners[block.id] = operation_id
                if self.record_sink:
                    self.record_sink(
                        interaction(
                            "model_call",
                            payload,
                            stage=self.stage,
                            operation_id=operation_id,
                            unit_id=self.unit_id,
                        )
                    )

    async def _call_api(
        self, *args: Any, **kwargs: Any
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        started = time.monotonic()
        request: JsonObject = {
            "model": args[0] if args else self.model,
            "parameters": json_value(self.parameters),
            **cast(JsonObject, json_value(kwargs)),
        }
        response, error, status = None, None, "success"
        try:
            response = await super()._call_api(*args, **kwargs)
            return response
        except asyncio.CancelledError:
            status, error = self._cancel_status(), "Model attempt cancelled"
            raise
        except Exception as failure:
            status, error = "error", f"{type(failure).__name__}: {failure}"
            raise
        finally:
            usage = response.usage if isinstance(response, ChatResponse) else None
            measured: JsonObject = {
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "cached_tokens": usage.cache_input_tokens
                if usage and usage.cache_input_tokens
                else None,
            }
            if self._attempts is not None:
                self._attempts.append(
                    {
                        "request": request,
                        "response": json_value(response)
                        if isinstance(response, ChatResponse)
                        else None,
                        "status": status,
                        "error": error,
                        "usage": measured,
                        "duration_seconds": time.monotonic() - started,
                    }
                )


def create_model(settings: ModelSettings, stage: str, usage: list[UsageRecord]) -> TrackedChatModel:
    return TrackedChatModel(settings, stage, usage)
