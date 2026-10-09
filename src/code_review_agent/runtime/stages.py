"""阶段模型执行、原模板渲染与 JSON 边界。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from jinja2 import Environment, StrictUndefined

from ..config import ModelSettings, Settings
from ..contracts import JsonObject, JsonValue, UsageRecord
from ..inputs.selection import RESOURCE_ROOT
from ..tools.registry import ToolRegistry
from .agentscope_adapter import AgentRun, run_agent, run_single_model
from .model_factory import TrackedChatModel, create_model
from .recording import UNIT_ID

ModelFactory = Callable[[ModelSettings, str, list[UsageRecord]], TrackedChatModel]


def render_prompt(name: str, variables: dict[str, str]) -> str:
    template = (RESOURCE_ROOT / "prompts" / name).read_text()
    return (
        Environment(undefined=StrictUndefined, autoescape=False)
        .from_string(template)
        .render(**variables)
    )


def parse_json_response(text: str) -> JsonValue:
    fenced = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", text, re.DOTALL)
    return cast(JsonValue, json.loads(fenced.group(1) if fenced else text))


@dataclass
class StageRunner:
    settings: Settings
    model_factory: ModelFactory = create_model
    usage: list[UsageRecord] = field(default_factory=list)
    records: list[JsonObject] = field(default_factory=list)
    record_sink: Callable[[JsonObject], None] | None = None

    _recording_error: Exception | None = field(default=None, init=False)

    def _emit(self, record: JsonObject) -> None:
        if self._recording_error:
            raise self._recording_error
        if self.record_sink:
            try:
                self.record_sink(record)
            except Exception as error:
                self._recording_error = error
                raise
        self.records.append(record)

    def check_recording(self) -> None:
        if self._recording_error:
            raise self._recording_error

    async def run(
        self,
        stage: str,
        system: str,
        user: str,
        *,
        registry: ToolRegistry | None = None,
        allow_compression: bool = True,
        pinned_context: str = "",
        single_call: bool = False,
        tools: tuple[JsonObject, ...] = (),
    ) -> AgentRun:
        if stage.startswith("reflection"):
            settings = self.settings.reflection
        elif stage.startswith("judge"):
            settings = self.settings.judge
        else:
            settings = self.settings.reviewer
        language = "中文" if self.settings.language == "zh" else "English"
        system += f"\n\nOutput natural-language explanations in {language}. Keep JSON keys and enumerations unchanged."
        stage_usage: list[UsageRecord] = []
        model = self.model_factory(settings, stage, stage_usage)
        model.record_sink = self._emit
        model.unit_id = UNIT_ID.get()
        try:
            if single_call:
                result = await run_single_model(model, system, user, tools=tools)
            else:
                result = await run_agent(
                    model,
                    system,
                    user,
                    # 复核使用独立角色名称。
                    stage="reflection" if stage.startswith("reflection") else stage,
                    registry=registry,
                    max_iterations=self.settings.max_tool_iterations,
                    timeout_seconds=300,
                    tool_result_chars=self.settings.tool_result_chars,
                    allow_compression=allow_compression,
                    pinned_context=pinned_context,
                )
            if self._recording_error:
                raise self._recording_error
            return result
        finally:
            self.usage.extend(stage_usage)
            await model.client.close()
            if self._recording_error:
                raise self._recording_error

    async def json(
        self,
        stage: str,
        system: str,
        user: str,
        *,
        registry: ToolRegistry | None = None,
    ) -> JsonValue:
        # 明确参数接口保持在 run；JSON 包装不做修复式解析或关键词推断。
        result = await self.run(stage, system, user, registry=registry)
        if not result.completed:
            raise ValueError(result.error or f"Incomplete stage: {stage}")
        return parse_json_response(result.text)
