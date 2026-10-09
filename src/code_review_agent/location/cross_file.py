"""跨文件匹配独立于三级定位消融，仅完整系统启用。"""

from __future__ import annotations

from ..contracts import FileSnapshot, Location, Side
from .file_scan import file_matches
from .hunk import hunk_matches


def cross_file_matches(
    files: tuple[FileSnapshot, ...], path: str, snippet: str
) -> tuple[Location, ...]:
    hits: list[Location] = []
    for file in files:
        if path in {file.path, file.old_path}:
            continue
        matches = (
            hunk_matches(file, snippet, Side.RIGHT)
            or hunk_matches(file, snippet, Side.LEFT)
            or file_matches(file, snippet, Side.RIGHT)
        )
        if matches:
            hits.append(matches[0])
        if len(hits) > 1:
            break
    return tuple(hits)
