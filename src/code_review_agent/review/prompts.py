"""集中构造冻结版本、评审规则和阶段任务的提示词。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast

from jinja2 import Environment, StrictUndefined

from ..config import Settings
from ..contracts import FindingCandidate, InputMode, JsonObject, ReviewInput, ReviewUnit
from ..inputs.selection import RESOURCE_ROOT
from ..runtime.stages import parse_json_response, render_prompt
from .rules import RuleResolver


def scan_prompt(task: str, values: dict[str, str]) -> tuple[str, str]:
    template = json.loads((RESOURCE_ROOT / "prompts/scan_template.json").read_text())
    messages = template[task]["messages"]
    environment = Environment(undefined=StrictUndefined, autoescape=False)
    return (
        str(messages[0]["content"]),
        environment.from_string(messages[1]["content"]).render(**values),
    )


def format_scan_plan(raw: str) -> str:
    """把 Scan 预规划转换为关注点列表，异常格式保留模型原文。"""
    text = re.sub(r"\A```[^\n]*\n|\n```\s*\Z", "", raw.strip()).strip()
    if not text:
        return "(no pre-scan plan; review the entire file as usual)"
    try:
        value = parse_json_response(text)
        if not isinstance(value, dict):
            return text
        summary = value.get("summary", "")
        checkpoints = value.get("checkpoints", [])
        if not isinstance(summary, str) or not isinstance(checkpoints, list):
            return text
        parts = [f"**Summary**: {summary}\n"] if summary else []
        if checkpoints:
            parts.append("**Focus areas (give these extra attention; not exhaustive):**")
        for number, checkpoint in enumerate(checkpoints, 1):
            if not isinstance(checkpoint, dict):
                return text
            focus, lines, why = (checkpoint.get(key, "") for key in ("focus", "lines", "why"))
            if not all(isinstance(item, str) for item in (focus, lines, why)):
                return text
            parts.append(
                f"{number}. `{focus}`"
                + (f" (lines {lines})" if lines else "")
                + (f" — {why}" if why else "")
            )
        return "\n".join(parts).rstrip() or "(no pre-scan plan; review the entire file as usual)"
    except ValueError:
        return text


def group_system_rule(unit: ReviewUnit, rules: RuleResolver) -> str:
    """相同正文共享规则块；不同规则保留对应文件，路径排序稳定提示词。"""
    blocks: dict[str, list[str]] = {}
    for path in sorted(unit.paths):
        content = rules.resolve(path).content
        if content:
            blocks.setdefault(content, []).append(path)
    if not blocks:
        return ""
    if len(blocks) == 1:
        return next(iter(blocks))
    return "\n".join(
        f'<rules for="{", ".join(paths)}">\n{content}\n</rules>'
        for content, paths in blocks.items()
    )


def prompt_values(
    review_input: ReviewInput,
    unit: ReviewUnit,
    settings: Settings,
    rules: RuleResolver,
    plan: str = "",
    confirmed: tuple[FindingCandidate, ...] = (),
) -> dict[str, str]:
    files = {file.path: file for file in review_input.files}
    first = files[unit.paths[0]]
    return {
        "change_files": "\n".join(
            file.path for file in review_input.targets if file.path not in unit.paths
        ),
        "diffs": "\n".join(
            f"<file path={json.dumps(path)}>{files[path].diff}</file>" for path in unit.paths
        ),
        "current_system_date_time": datetime.now(UTC).isoformat(),
        "requirement_background": settings.background,
        "system_rule": group_system_rule(unit, rules),
        "plan_guidance": format_scan_plan(plan) if review_input.mode == InputMode.SCAN else plan,
        "confirmed_comments": confirmed_findings_block(confirmed),
        "current_file_path": first.path,
        "file_content": first.content,
    }


def confirmed_findings_block(confirmed: tuple[FindingCandidate, ...]) -> str:
    """跨轮传递紧凑发现摘要，明确要求不要重复已发现的问题。"""
    if not confirmed:
        return ""
    lines = [
        (RESOURCE_ROOT / "prompts/confirmed_findings.md").read_text().strip(),
        "<confirmed_findings>",
    ]
    for number, item in enumerate(confirmed, 1):
        lines.append(f"{number}. {item.path}")
        for label, text, limit in (("code", item.existing_code, 200), ("issue", item.content, 300)):
            if not text:
                continue
            flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
            lines.append(f"   {label}: {flat[:limit] + '...' if len(flat) > limit else flat}")
    lines.append("</confirmed_findings>")
    return "\n".join(lines)


def review_prompt(review_input: ReviewInput, values: dict[str, str]) -> tuple[str, str]:
    if review_input.mode == InputMode.SCAN:
        system, user = scan_prompt("MAIN_TASK", values)
    else:
        system = (RESOURCE_ROOT / "prompts/main_task_system.md").read_text()
        user = render_prompt("main_task_user.md", values)
        for key, heading in (
            ("plan_guidance", "Review Plan"),
            ("confirmed_comments", "Previously Confirmed Findings"),
        ):
            if not values[key]:
                user = re.sub(r"(?m)^### " + heading + r"[^\n]*\n\s*\n", "", user)
    system += "\nCall task_done with reviewed_paths naming every file you actually finished. A partial pass is not a completed file."
    return system, user


def candidate_json(candidate: FindingCandidate) -> JsonObject:
    return cast(JsonObject, json.loads(json.dumps(asdict(candidate))))
