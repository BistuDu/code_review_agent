"""将 AgentScope 状态、工具和事件转换为本项目的阶段结果。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, cast

from agentscope.agent import Agent, InjectionConfig, ModelConfig, ReActConfig
from agentscope.event import ModelCallStartEvent, ReplyEndEvent
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolCallBlock, ToolResultState, UserMsg
from agentscope.model import ChatModelBase, ChatResponse, FinishedReason
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import ToolBase, ToolChunk, Toolkit
from jsonschema import ValidationError

from ..contracts import JsonObject
from ..tools.registry import ToolRegistry
from .context import ReviewContextMiddleware, context_config
from .model_factory import TrackedChatModel
from .recording import ToolRecordingMiddleware


@dataclass(frozen=True)
class AgentRun:
    text: str
    completed: bool
    state: JsonObject
    events: tuple[JsonObject, ...]
    error: str | None = None
    tool_calls: tuple[JsonObject, ...] = ()


async def run_single_model(
    model: ChatModelBase,
    system_prompt: str,
    user_prompt: str,
    *,
    tools: tuple[JsonObject, ...],
) -> AgentRun:
    """单次 AgentScope 模型请求；结果工具只作声明，不启动工具循环。"""
    messages = [Msg(name="system", role="system", content=[TextBlock(text=system_prompt)])]
    messages.append(UserMsg(name="user", content=user_prompt))
    if isinstance(model, TrackedChatModel):
        model.timeout_deadline = time.monotonic() + 300
    try:
        async with asyncio.timeout(300):
            response = await model(messages=messages, tools=cast(list[dict[str, Any]], list(tools)))
        if not isinstance(response, ChatResponse):
            raise ValueError("Single-call stages require a non-streaming model")
        if response.finished_reason == FinishedReason.INTERRUPTED:
            raise asyncio.CancelledError
        calls = tuple(
            {"name": block.name, "arguments": block.input}
            for block in response.content
            if isinstance(block, ToolCallBlock)
        )
        text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
        messages.append(
            AssistantMsg(name="reflection", content=[block for block in response.content])
        )
        return AgentRun(text, True, {}, (), tool_calls=cast(tuple[JsonObject, ...], calls))
    except Exception as error:
        return AgentRun("", False, {}, (), f"{type(error).__name__}: single model call failed")


class RegisteredTool(ToolBase):
    def __init__(self, name: str, registry: ToolRegistry) -> None:
        super().__init__()
        definition = registry.definitions[name]
        self.registry, self.name = registry, name
        self.description = cast(str, definition["description"])
        self.input_schema = cast(dict[str, Any], definition["parameters"])
        self.is_concurrency_safe = False
        self.is_read_only = True
        self.is_state_injected = False

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        # 只注册本项目的受限工具；读取路径和输入校验仍由 registry 强制执行。
        return PermissionDecision(
            PermissionBehavior.ALLOW, "Registered snapshot-backed review tool"
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            result = self.registry.invoke(self.name, cast(JsonObject, kwargs))
            return ToolChunk(content=[TextBlock(text=result)], state=ToolResultState.SUCCESS)
        except (KeyError, TypeError, ValueError, OSError, ValidationError) as error:
            return ToolChunk(content=[TextBlock(text=str(error))], state=ToolResultState.ERROR)


async def run_agent(
    model: ChatModelBase,
    system_prompt: str,
    user_prompt: str,
    *,
    stage: str,
    registry: ToolRegistry | None = None,
    max_iterations: int = 100,
    timeout_seconds: float = 300,
    tool_result_chars: int = 50000,
    allow_compression: bool = True,
    pinned_context: str = "",
) -> AgentRun:
    state = AgentState()
    toolkit = Toolkit(
        tools=[RegisteredTool(name, registry) for name in registry.definitions] if registry else []
    )
    collector = registry.collector if registry else None

    def current_facts() -> str:
        if collector is None:
            return ""
        return json.dumps(
            {
                "version": collector.input.snapshot_id,
                "paths": sorted(collector.target_paths),
                "confirmed_candidates": [asdict(item) for item in collector.candidates],
            },
            ensure_ascii=False,
        )

    if isinstance(model, TrackedChatModel):
        model.timeout_deadline = time.monotonic() + timeout_seconds
    recording = ToolRecordingMiddleware(
        model.record_sink if isinstance(model, TrackedChatModel) else None,
        stage,
        model.unit_id if isinstance(model, TrackedChatModel) else None,
        model.tool_owners if isinstance(model, TrackedChatModel) else {},
    )
    agent = Agent(
        name=stage,
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        state=state,
        middlewares=[
            ReviewContextMiddleware(allow_compression, pinned_context, current_facts),
            recording,
        ],
        model_config=ModelConfig(max_retries=0),
        context_config=context_config(tool_result_chars),
        react_config=ReActConfig(max_iters=max_iterations, interruption_raise_cancelled_error=True),
        injection_config=InjectionConfig(inject_runtime_state=False),
    )
    final_text, completed, error = "", False, None
    collector = registry.collector if registry else None
    reasoning_calls = 0
    stream = agent.reply_stream(UserMsg(name="user", content=user_prompt), yield_final_msg=True)
    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in stream:
                if isinstance(event, ModelCallStartEvent):
                    if not (collector and collector.finished):
                        if reasoning_calls >= max_iterations:
                            error = "Tool iteration limit exceeded"
                            break
                        reasoning_calls += 1
                # 到下一次模型调用前，上一批工具已全部落入状态；此时安全结束 task_done。
                if collector and collector.finished and isinstance(event, ModelCallStartEvent):
                    completed = not collector.failed
                    break
                if isinstance(event, Msg):
                    final_text = event.get_text_content() or ""
                else:
                    if isinstance(event, ReplyEndEvent):
                        completed = str(event.finished_reason) == "completed"
                        if event.error:
                            error = event.error.message
            if collector:
                completed = collector.finished and not collector.failed and error is None
    except TimeoutError:
        error = "Stage timeout"
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"
    finally:
        await stream.aclose()
    return AgentRun(final_text, completed, {}, (), error)
