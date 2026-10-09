"""Claude PreToolUse 守卫：只允许冻结源码和有限的只读 Git 查询。"""

from __future__ import annotations

import fcntl
import json
import shlex
import sys
from pathlib import Path
from typing import Any


def permitted(
    root: Path,
    paths: set[str],
    tool: str,
    arguments: dict[str, Any],
    versions: set[str],
    version_paths: dict[str, list[str]] | None = None,
    max_file_bytes: int = 2097152,
) -> bool:
    def within(raw: str, *, file: bool = False) -> bool:
        candidate = Path(raw)
        if ".." in candidate.parts:
            return False
        candidate = candidate if candidate.is_absolute() else root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root) or ".git" in resolved.relative_to(root).parts:
            return False
        if not candidate.absolute().is_relative_to(root):
            return False
        relative = candidate.absolute().relative_to(root).as_posix()
        if file:
            if (
                relative not in paths
                or not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size > max_file_bytes
            ):
                return False
            return b"\x00" not in candidate.read_bytes()
        return resolved.is_dir() or relative in paths

    if tool == "StructuredOutput":
        return True
    if tool == "Read":
        return isinstance(arguments.get("file_path"), str) and within(
            arguments["file_path"], file=True
        )
    if tool in {"Grep", "Glob"}:
        raw = arguments.get("path", str(root))
        return (
            (not Path(str(arguments.get("pattern", ""))).is_absolute() if tool == "Glob" else True)
            and not Path(str(arguments.get("glob", ""))).is_absolute()
            and (
                isinstance(raw, str)
                and within(raw)
                and not any(
                    ".." in str(arguments.get(key, "")).split("/")
                    or ".git" in str(arguments.get(key, ""))
                    for key in ("glob", "pattern")
                )
            )
        )
    if tool != "Bash" or not isinstance(arguments.get("command"), str):
        return False
    command = arguments["command"]
    if any(character in command for character in "\n;&|<>`$"):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if words == ["git", "ls-files"]:
        return True
    if len(words) == 4 and words[:2] == ["git", "diff"]:
        return words[2] in versions and words[3] in versions
    if len(words) == 3 and words[:2] == ["git", "show"]:
        sha, sep, path = words[2].partition(":")
        return bool(
            sep
            and sha in versions
            and path in (version_paths.get(sha, []) if version_paths else paths)
        )
    if words and words[0] == "rg":
        # 简单形式 rg [-n|-i|-F] PATTERN [tracked-file]；拒绝其它选项和 shell。
        values = words[1:]
        while values and values[0] in {"-n", "-i", "-F", "--line-number", "--fixed-strings"}:
            values = values[1:]
        return bool(
            values
            and not values[0].startswith("-")
            and len(values) <= 2
            and (len(values) == 1 or within(values[1], file=True))
        )
    return False


def main() -> None:
    try:
        config = json.loads(Path(sys.argv[1]).read_text())
        value = json.load(sys.stdin)
        root = Path(config["root"]).resolve()
        cwd = Path(value.get("cwd", str(root))).resolve()
        if value.get("hook_event_name") == "SessionStart":
            Path(config["audit"]).write_text(
                json.dumps({"initialized": True, "calls": 0, "denied": 0})
            )
            print("{}")
            return
        allow = cwd == root and permitted(
            root,
            set(config["paths"]),
            value.get("tool_name", ""),
            value.get("tool_input", {}),
            set(config["versions"]),
            config.get("version_paths"),
            config.get("max_file_bytes", 2097152),
        )
        # 仅记录守卫覆盖和拒绝数，不保存逐工具事件或源码内容。
        marker = Path(config["audit"])
        with marker.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            data = stream.read()
            audit = json.loads(data) if data else {"calls": 0, "denied": 0}
            audit["calls"] += 1
            audit["denied"] += int(not allow)
            stream.seek(0)
            stream.truncate()
            json.dump(audit, stream)
            stream.flush()
        updated = value.get("tool_input", {})
        if allow and value.get("tool_name") == "Bash":
            words = shlex.split(updated["command"])
            if words[:2] in (["git", "diff"], ["git", "show"]):
                updated = {
                    **updated,
                    "command": shlex.join(
                        [
                            "git",
                            "--no-pager",
                            "-c",
                            "core.hooksPath=/dev/null",
                            words[1],
                            "--no-ext-diff",
                            "--no-textconv",
                            *words[2:],
                        ]
                    ),
                }
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow" if allow else "deny",
                        "permissionDecisionReason": "Frozen local review access only",
                        **({"updatedInput": updated} if allow else {}),
                    }
                }
            )
        )
    except Exception:
        # hook 协议异常时用 blocking exit code，不能无输出地放行。
        print("Review tool guard failed closed", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
