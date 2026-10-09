"""工具定义与阶段白名单，模型工具参数在此转换为业务调用。"""

from __future__ import annotations

import json
from typing import cast

import jsonschema

from ..contracts import JsonObject
from ..inputs.selection import RESOURCE_ROOT
from .context import ContextTools
from .findings import FindingCollector

CONTEXT_NAMES = {"file_read", "file_find", "file_read_diff", "code_search"}
PLAN_NAMES = {"file_find", "file_read_diff", "code_search"}


class ToolRegistry:
    def __init__(
        self,
        context: ContextTools,
        collector: FindingCollector | None = None,
        stage: str = "review",
    ) -> None:
        self.context, self.collector = context, collector
        if stage == "plan":
            allowed = PLAN_NAMES
        elif stage == "relocation":
            allowed = CONTEXT_NAMES
        elif stage == "review":
            allowed = CONTEXT_NAMES | {"code_comment", "task_done"}
        else:
            allowed = set()
        definitions = json.loads((RESOURCE_ROOT / "tool_schemas/tools.json").read_text())
        self.definitions: dict[str, JsonObject] = {}
        for entry in definitions:
            definition = cast(JsonObject, entry["definition"])
            name = cast(str, definition["name"])
            if name in allowed:
                self.definitions[name] = definition
        if "task_done" in self.definitions:
            parameters = cast(JsonObject, self.definitions["task_done"]["parameters"])
            properties = cast(JsonObject, parameters["properties"])
            properties["reviewed_paths"] = {
                "type": "array",
                "items": {"type": "string"},
                "description": "All files in this unit that you completed reviewing.",
            }
        if "code_comment" in self.definitions:
            parameters = cast(JsonObject, self.definitions["code_comment"]["parameters"])
            comments = cast(JsonObject, cast(JsonObject, parameters["properties"])["comments"])
            properties = cast(JsonObject, cast(JsonObject, comments["items"])["properties"])
            properties["side"] = {"type": "string", "enum": ["left", "right"], "default": "right"}

    def invoke(self, name: str, arguments: JsonObject) -> str:
        if name not in self.definitions:
            raise ValueError(f"Tool unavailable in this stage: {name}")
        jsonschema.validate(arguments, cast(JsonObject, self.definitions[name]["parameters"]))
        if name == "file_read":
            return self.context.file_read(
                cast(str, arguments["file_path"]),
                cast(int, arguments.get("start_line", 1)),
                cast(int | None, arguments.get("end_line")),
            )
        if name == "file_find":
            return self.context.file_find(
                cast(str, arguments["query_name"]),
                cast(bool, arguments.get("case_sensitive", False)),
            )
        if name == "file_read_diff":
            return self.context.file_read_diff(cast(list[str], arguments["path_array"]))
        if name == "code_search":
            return self.context.code_search(
                cast(str, arguments["search_text"]),
                cast(list[str] | None, arguments.get("file_patterns")),
                cast(bool, arguments.get("case_sensitive", False)),
                cast(bool, arguments.get("use_perl_regexp", False)),
            )
        if self.collector is None:
            raise ValueError("Finding collector is unavailable")
        if name == "code_comment":
            return self.collector.code_comment(cast(list[JsonObject], arguments["comments"]))
        return self.collector.task_done(
            cast(str, arguments.get("state", "DONE")),
            cast(list[str] | None, arguments.get("reviewed_paths")),
        )
