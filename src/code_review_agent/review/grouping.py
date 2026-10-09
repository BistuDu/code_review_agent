"""验证关联分组；模型只建议组织方式，不能改变目标文件集合。"""

from __future__ import annotations

from typing import cast

from ..config import Settings
from ..contracts import JsonValue, ReviewUnit, stable_digest
from ..inputs.selection import RESOURCE_ROOT
from ..runtime.stages import StageRunner, render_prompt


def validated_groups(value: JsonValue, paths: tuple[str, ...], maximum: int) -> list[ReviewUnit]:
    if not isinstance(value, list):
        raise ValueError("Grouping must be an array")
    remaining = set(paths)
    units: list[ReviewUnit] = []
    for group in value:
        if not isinstance(group, dict) or not isinstance(group.get("files"), list):
            raise ValueError("Invalid grouping entry")
        files = cast(list[JsonValue], group["files"])
        if not files or any(not isinstance(path, str) or path not in remaining for path in files):
            raise ValueError("Grouping contains duplicate or out-of-scope paths")
        if len(set(files)) != len(files):
            raise ValueError("Grouping repeats a file")
        ordered = sorted(str(path) for path in files)
        remaining.difference_update(ordered)
        for offset in range(0, len(ordered), maximum):
            batch = tuple(ordered[offset : offset + maximum])
            units.append(
                ReviewUnit(stable_digest(list(batch)), batch, str(group.get("label", "related")))
            )
    for path in sorted(remaining):
        units.append(ReviewUnit(stable_digest([path]), (path,), "omission fallback"))
    return units


async def group_files(
    paths: tuple[str, ...], settings: Settings, runner: StageRunner
) -> list[ReviewUnit]:
    if len(paths) < settings.group_min_files:
        return validated_groups([], paths, settings.group_max_files)
    try:
        value = await runner.json(
            "grouping",
            (RESOURCE_ROOT / "prompts/grouping_task_system.md").read_text(),
            render_prompt("grouping_task_user.md", {"file_list": "\n".join(paths)}),
        )
        return validated_groups(value, paths, settings.group_max_files)
    except Exception:
        # 分组服务失败不能令文件从计划中消失。
        return [
            ReviewUnit(stable_digest([path]), (path,), "grouping failure fallback")
            for path in sorted(paths)
        ]
