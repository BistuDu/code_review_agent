"""规则优先级、内容嗅探和有效资源身份。外部项目规则只读取冻结输入。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from ..config import Settings
from ..contracts import JsonObject, JsonValue, ReviewInput, stable_digest
from ..inputs.selection import RESOURCE_ROOT, matches, select_files
from ..inputs.snapshots import safe_relative_path
from ..project_paths import ProjectPaths


@dataclass(frozen=True)
class RuleEntry:
    pattern: str
    content: str
    merge_system: bool


@dataclass(frozen=True)
class RuleLayer:
    source: str
    entries: tuple[RuleEntry, ...]
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedRule:
    content: str
    source: str
    pattern: str


class RuleResolver:
    def __init__(self, paths: ProjectPaths, review_input: ReviewInput, settings: Settings) -> None:
        self.files = {file.path: file for file in review_input.files}
        self.paths = paths
        self.layers: list[RuleLayer] = []
        self.warnings: list[str] = []
        for source, name in (
            ("custom", settings.rule_file),
            ("project", ".opencodereview/rule.json"),
            ("global", settings.global_rule_file),
        ):
            if not name:
                continue
            if source == "project":
                file = self.files.get(name)
                if file is None or file.binary:
                    continue
                raw = file.content
            else:
                raw = paths.writable(name).read_text()
            if source == "custom" and Path(name).suffix.lower() in {".md", ".txt", ".markdown"}:
                self.layers.append(RuleLayer(source, (RuleEntry("**", raw, False),)))
                continue
            value = cast(JsonObject, json.loads(raw))
            if not isinstance(value, dict):
                raise ValueError("Rule configuration must be an object")
            self.layers.append(self._layer(source, value))
        self.system = cast(
            JsonObject, json.loads((RESOURCE_ROOT / "rules/system_rules.json").read_text())
        )

    def _layer(self, source: str, value: JsonObject) -> RuleLayer:
        raw_entries = value.get("rules", [])
        if not isinstance(raw_entries, list):
            raise ValueError("rules must be an ordered list")
        entries: list[RuleEntry] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ValueError("Invalid rule entry")
            pattern, content, merge = (
                raw.get("path"),
                raw.get("rule", ""),
                raw.get("merge_system_rule", False),
            )
            if (
                not isinstance(pattern, str)
                or not isinstance(content, str)
                or type(merge) is not bool
            ):
                raise ValueError("Invalid rule fields")
            if (
                "\n" not in content
                and " " not in content
                and Path(content).suffix.lower() in {".md", ".txt", ".markdown"}
            ):
                try:
                    if source == "project":
                        reference = self.files[safe_relative_path(content)]
                        if reference.binary or reference.byte_size > 512 * 1024:
                            raise ValueError("Unreadable or oversized rule reference")
                        content = reference.content
                    else:
                        reference_path = self.paths.writable(content)
                        if reference_path.stat().st_size > 512 * 1024:
                            raise ValueError("Oversized rule reference")
                        content = reference_path.read_text()
                except (KeyError, OSError, ValueError):
                    self.warnings.append(f"Unreadable {source} rule reference: {content}")
                    content = ""
            entries.append(RuleEntry(pattern, content, merge))
        include, exclude = value.get("include", []), value.get("exclude", [])
        if not isinstance(include, list) or not isinstance(exclude, list):
            raise ValueError("Rule filters must be string lists")
        if not all(isinstance(pattern, str) for pattern in include + exclude):
            raise ValueError("Invalid rule filter")
        return RuleLayer(
            source, tuple(entries), tuple(cast(list[str], include)), tuple(cast(list[str], exclude))
        )

    def system_rule(self, path: str) -> ResolvedRule:
        mapping = cast(JsonObject, self.system["path_rule_map"])
        name, pattern = cast(str, self.system["default_rule"]), "default"
        for candidate, reference in mapping.items():
            if matches(path, candidate):
                name, pattern = cast(str, reference), candidate
                break
        if path.lower().endswith(".m"):
            file = self.files[path]
            first = next((line.strip() for line in file.content.splitlines() if line.strip()), "")
            if first.startswith(
                (
                    "#import",
                    "#include",
                    "#pragma",
                    "#if",
                    "#define",
                    "@import",
                    "@interface",
                    "@implementation",
                    "@class",
                    "@protocol",
                    "//",
                    "/*",
                )
            ):
                name = "objc.md"
        return ResolvedRule((RESOURCE_ROOT / "rules" / name).read_text(), "system", pattern)

    def resolve(self, path: str) -> ResolvedRule:
        for layer in self.layers:
            for entry in layer.entries:
                if (entry.content or entry.merge_system) and matches(path, entry.pattern):
                    content = entry.content
                    if entry.merge_system:
                        content = self.system_rule(path).content + "\n\n" + content
                    return ResolvedRule(content, layer.source, entry.pattern)
        return self.system_rule(path)

    def apply_filters(self, review_input: ReviewInput, settings: Settings) -> ReviewInput:
        include = settings.include + tuple(
            pattern for layer in self.layers for pattern in layer.include
        )
        exclude = settings.exclude + tuple(
            pattern for layer in self.layers for pattern in layer.exclude
        )
        candidate_paths = {item.path for item in review_input.selection}
        candidates = tuple(file for file in review_input.files if file.path in candidate_paths)
        selection = select_files(candidates, replace(settings, include=include, exclude=exclude))
        return replace(review_input, selection=selection)

    @property
    def identity(self) -> str:
        resources: list[JsonValue] = [
            [str(path.relative_to(RESOURCE_ROOT)), stable_digest(path.read_text())]
            for path in sorted(RESOURCE_ROOT.rglob("*"))
            if path.is_file()
        ]
        layers: list[JsonValue] = [
            [
                layer.source,
                [[entry.pattern, entry.content, entry.merge_system] for entry in layer.entries],
                list(layer.include),
                list(layer.exclude),
            ]
            for layer in self.layers
        ]
        return stable_digest({"resources": resources, "layers": layers})
