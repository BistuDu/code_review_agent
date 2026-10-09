"""Hunk 行映射。删除行只进入旧侧，新增行只进入新侧。"""

from __future__ import annotations

from ..contracts import FileSnapshot, Location, Side
from .file_scan import indexed_matches, normalize_line


def hunk_matches(file: FileSnapshot, snippet: str, side: Side) -> tuple[Location, ...]:
    matches: list[Location] = []
    for hunk in file.hunks:
        visible = [
            (line.text, line.old_line if side == Side.LEFT else line.new_line)
            for line in hunk.lines
        ]
        lines = [(number, normalize_line(text)) for text, number in visible if number is not None]
        for start, end in indexed_matches(lines, snippet):
            matches.append(Location(file.path, side, start, end, snippet))
    return tuple(dict.fromkeys(matches))
