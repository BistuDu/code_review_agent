"""解析单文件 unified diff；文件路径由 Git 的 NUL 分隔元数据提供。"""

from __future__ import annotations

import re

from ..contracts import DiffLine, Hunk

HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_hunks(diff: str) -> tuple[Hunk, ...]:
    hunks: list[Hunk] = []
    lines: list[DiffLine] = []
    old_start = new_start = old_line = new_line = 0
    active = False
    for text in diff.splitlines():
        header = HEADER.match(text)
        if header:
            if active:
                hunks.append(Hunk(old_start, new_start, tuple(lines)))
            old_start, new_start = map(int, header.groups())
            old_line, new_line = old_start, new_start
            lines = []
            active = True
        elif active and text and text[0] in {" ", "+", "-"}:
            kind = text[0]
            lines.append(
                DiffLine(
                    text[1:],
                    kind,
                    old_line if kind != "+" else None,
                    new_line if kind != "-" else None,
                )
            )
            old_line += kind != "+"
            new_line += kind != "-"
    if active:
        hunks.append(Hunk(old_start, new_start, tuple(lines)))
    return tuple(hunks)
