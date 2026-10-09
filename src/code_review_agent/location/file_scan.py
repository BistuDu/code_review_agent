"""Go 兼容的逐行归一化；忽略空行但保留真实文件坐标。"""

from __future__ import annotations

from ..contracts import FileSnapshot, Location, Side


def normalize_line(line: str) -> str:
    return line.strip().removeprefix("+").removeprefix("-").strip()


def indexed_matches(lines: list[tuple[int, str]], snippet: str) -> tuple[tuple[int, int], ...]:
    wanted = [value for line in snippet.split("\n") if (value := normalize_line(line))]
    if not wanted:
        return ()
    width = len(wanted)
    return tuple(
        (lines[index][0], lines[index + width - 1][0])
        for index in range(len(lines) - width + 1)
        if [text for _, text in lines[index : index + width]] == wanted
    )


def matching_offsets(content: str, snippet: str) -> tuple[tuple[int, int], ...]:
    lines = [
        (number, value)
        for number, line in enumerate(content.split("\n"), 1)
        if (value := normalize_line(line))
    ]
    return indexed_matches(lines, snippet)


def file_matches(file: FileSnapshot, snippet: str, side: Side) -> tuple[Location, ...]:
    content = file.old_content if side == Side.LEFT else file.content
    return tuple(
        Location(file.path, side, start, end, snippet)
        for start, end in matching_offsets(content, snippet)
    )
