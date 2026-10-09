"""内置过滤资源与显式覆盖，逐文件保留唯一筛选结论。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from wcmatch import glob

from ..config import Settings
from ..contracts import FileSelection, FileSnapshot

RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources"


def matches(path: str, pattern: str) -> bool:
    return bool(
        glob.globmatch(
            path, pattern, flags=glob.GLOBSTAR | glob.BRACE | glob.IGNORECASE | glob.DOTGLOB
        )
    )


def select_files(files: tuple[FileSnapshot, ...], settings: Settings) -> tuple[FileSelection, ...]:
    types = json.loads((RESOURCE_ROOT / "filters/supported_file_types.json").read_text())
    excludes = cast(
        list[str], json.loads((RESOURCE_ROOT / "filters/default_exclude_patterns.json").read_text())
    )
    extensions = {extension.lower() for extension in types}
    selections: list[FileSelection] = []
    for file in files:
        reason = "included"
        explicit = any(matches(file.path, pattern) for pattern in settings.include)
        if file.binary:
            reason = "binary_or_unreadable"
        elif file.deleted:
            reason = "deleted"
        elif max(file.byte_size, len(file.content.encode())) > settings.max_file_bytes:
            reason = "oversized"
        elif (
            len((file.diff or file.content).encode())
            > max(
                0,
                settings.reviewer.context_tokens
                - settings.reviewer.max_output_tokens
                - min(4096, max(128, settings.reviewer.context_tokens // 8)),
            )
            * 3
        ):
            # 文件本身过大时明确跳过，不靠压缩初始源码隐瞒容量不足。
            reason = "context_capacity"
        elif any(matches(file.path, pattern) for pattern in settings.exclude):
            reason = "user_exclude"
        elif settings.include and not explicit:
            reason = "not_included"
        elif not explicit and any(matches(file.path, pattern) for pattern in excludes):
            reason = "default_exclude"
        elif (
            not explicit
            and (Path(file.path).suffix.lower() or Path(file.path).name.lower()) not in extensions
        ):
            reason = "unsupported_type"
        selections.append(FileSelection(file.path, reason == "included", reason))
    return tuple(selections)
