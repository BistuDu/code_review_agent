"""冻结快照上的只读上下文工具，不对用户仓库执行任何修改命令。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..contracts import FileSnapshot, ReviewInput
from ..inputs.selection import matches
from ..inputs.snapshots import safe_relative_path


class ContextTools:
    def __init__(self, review_input: ReviewInput, result_chars: int = 50000) -> None:
        self.input = review_input
        self.files = {file.path: file for file in review_input.files}
        self.result_chars = result_chars
        self.read_paths: set[str] = set()

    def file(self, path: str) -> FileSnapshot:
        normalized = safe_relative_path(path)
        if normalized not in self.files:
            raise ValueError("File does not exist in the frozen input")
        file = self.files[normalized]
        if file.binary or file.deleted or (file.byte_size and not file.content):
            raise ValueError("File is binary, unreadable, deleted or exceeds snapshot size limit")
        self.read_paths.add(normalized)
        return file

    def limited(self, content: str) -> str:
        if len(content) <= self.result_chars:
            return content
        return content[: self.result_chars] + "\nIS_TRUNCATED: true (tool result character limit)"

    def file_read(self, file_path: str, start_line: int = 1, end_line: int | None = None) -> str:
        file = self.file(file_path)
        lines = file.content.splitlines()
        if type(start_line) is not int or start_line < 1:
            raise ValueError("start_line must be positive")
        requested_end = len(lines) if end_line is None else end_line
        if type(requested_end) is not int or requested_end < start_line:
            if not lines and start_line == 1:
                return (
                    f"File: {file.path} (Total lines: 0)\nIS_TRUNCATED: false\nLINE_RANGE: empty\n"
                )
            raise ValueError("Invalid end_line")
        end = min(requested_end, start_line + 499, len(lines))
        truncated = end < min(requested_end, len(lines))
        content = "".join(
            f"{number}|{line}\n"
            for number, line in enumerate(lines[start_line - 1 : end], start_line)
        )
        result = (
            f"File: {file.path} (Total lines: {len(lines)})\n"
            f"IS_TRUNCATED: {str(truncated).lower()}\n"
            f"LINE_RANGE: {start_line}-{end}\n{content}"
        )
        if truncated:
            result += "\nNote: Results truncated to 500 lines. Please narrow your line range.\n"
        return self.limited(result)

    def file_read_diff(self, path_array: list[str]) -> str:
        results: list[str] = []
        for path in path_array:
            file = self.file(path)
            results.append(
                f"==== FILE: {file.path} ====\n{file.diff or 'No diff in this input (scan or context-only file).'}"
            )
        return self.limited("\n\n".join(results))

    def file_find(self, query_name: str, case_sensitive: bool = False) -> str:
        query = query_name if case_sensitive else query_name.lower()
        results: list[str] = []
        if not query:
            return "No files found"
        for path, file in sorted(self.files.items()):
            target = path if "/" in query or "\\" in query else Path(path).name
            if not case_sensitive:
                target = target.lower()
            if query.replace("\\", "/") in target and not file.binary and not file.deleted:
                results.append(path)
        suffix = "\nIS_TRUNCATED: true" if len(results) > 100 else ""
        return self.limited("\n".join(results[:100]) + suffix)

    def _paths(self, patterns: list[str] | None) -> tuple[str, ...]:
        include: list[str] = []
        exclude: list[str] = []
        for pattern in patterns or []:
            destination = exclude if pattern.startswith((":(exclude)", ":!", ":^")) else include
            if pattern.startswith(":(exclude)"):
                pattern = pattern[len(":(exclude)") :]
            elif pattern.startswith((":!", ":^")):
                pattern = pattern[2:]
            elif pattern.startswith(":(glob)"):
                pattern = pattern[len(":(glob)") :]
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("Search pattern escapes repository")
            destination.append(pattern)

        def accepted(path: str, pattern: str) -> bool:
            return (
                matches(path, pattern)
                or path.startswith(pattern.rstrip("/") + "/")
                or ("/" not in pattern and matches(Path(path).name, pattern))
            )

        return tuple(
            path
            for path in sorted(self.files)
            if (not include or any(accepted(path, pattern) for pattern in include))
            and not any(accepted(path, pattern) for pattern in exclude)
        )

    def code_search(
        self,
        search_text: str,
        file_patterns: list[str] | None = None,
        case_sensitive: bool = False,
        use_perl_regexp: bool = False,
    ) -> str:
        if not search_text:
            return "No matches"
        results: list[str] = []
        for path in self._paths(file_patterns):
            file = self.files[path]
            if file.binary or file.deleted:
                continue
            if use_perl_regexp:
                arguments = [
                    "rg",
                    "--pcre2",
                    "--line-number",
                    "--no-heading",
                    "--no-config",
                    "--color",
                    "never",
                ]
                if not case_sensitive:
                    arguments.append("--ignore-case")
                result = subprocess.run(
                    [*arguments, "-e", search_text, "--"],
                    input=file.content,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode not in {0, 1}:
                    raise ValueError("Invalid or unsupported Perl regular expression")
                matched = [line.replace(":", "|", 1) for line in result.stdout.splitlines()]
            else:
                query = search_text if case_sensitive else search_text.lower()
                matched = [
                    f"{index}|{line}"
                    for index, line in enumerate(file.content.splitlines(), 1)
                    if query in (line if case_sensitive else line.lower())
                ]
            if matched:
                self.read_paths.add(path)
                results.extend(f"File: {path}\n{line}" for line in matched)
            if len(results) > 100:
                return self.limited("\n".join(results[:100]) + "\nIS_TRUNCATED: true")
        return self.limited("\n".join(results) or "No matches")
