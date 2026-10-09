"""场景化压缩提示词与基本 Agent 的上下文容量约束。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.agent import Agent, ContextConfig
from agentscope.middleware import MiddlewareBase

from ..inputs.selection import RESOURCE_ROOT
from .model_factory import TrackedChatModel

SUMMARY_FIELDS = (
    "identified_code_issues",
    "tool_call_conclusions",
    "completed_tasks",
    "pending_tasks",
    "current_focus",
)


def context_config(tool_result_chars: int) -> ContextConfig:
    prompt = (RESOURCE_ROOT / "prompts/memory_compression_task_system.md").read_text()
    prompt += "\nReturn JSON with these string fields: " + ", ".join(SUMMARY_FIELDS)
    return ContextConfig(
        compression_prompt=prompt,
        summary_schema={
            "type": "object",
            "properties": {name: {"type": "string"} for name in SUMMARY_FIELDS},
            "required": list(SUMMARY_FIELDS),
            "additionalProperties": False,
        },
        summary_template="\n".join("### " + name + "\n{" + name + "}" for name in SUMMARY_FIELDS),
        tool_result_limit=max(1, tool_result_chars // 4),
    )


class ReviewContextMiddleware(MiddlewareBase):
    def __init__(
        self,
        allow_compression: bool,
        pinned_context: str = "",
        fact_provider: Callable[[], str] | None = None,
    ) -> None:
        self.allow_compression = allow_compression
        self.pinned_context = pinned_context
        self.fact_provider = fact_provider

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        token_count = await agent.model.count_tokens(agent.state.context, tools=None)
        limit = agent.model.context_size * agent.context_config.trigger_ratio
        if not self.allow_compression and token_count > limit:
            raise ValueError("Stage input exceeds context capacity; no history compression allowed")
        if not self.allow_compression:
            return
        model = agent.model
        previous = model.stage if isinstance(model, TrackedChatModel) else None
        if isinstance(model, TrackedChatModel):
            model.stage += ".compression"
        try:
            await next_handler(**input_kwargs)
            pinned = self.pinned_context + (self.fact_provider() if self.fact_provider else "")
            if pinned and token_count > limit:
                summary = agent.state.summary
                if isinstance(summary, str):
                    agent.state.summary = pinned + "\n\n" + summary
        finally:
            if isinstance(model, TrackedChatModel) and previous is not None:
                model.stage = previous
