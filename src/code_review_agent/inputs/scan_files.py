"""Git 文件枚举与非 Git 目录的根 .gitignore 筛选。"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from wcmatch import glob

from .git import GitRepository

EXCLUDED_DIRS = (
    ".idea",
    ".vscode",
    ".svn",
    ".git",
    "vendor",
    "node_modules",
    "target",
    ".happypack",
    ".cachefile",
    "_packages",
    "rpm",
    "pkgs",
)


def load_ignore_patterns(root: Path) -> tuple[str, ...]:
    try:
        return tuple(
            line
            for raw in (root / ".gitignore").read_text().splitlines()
            if (line := raw.strip()) and not line.startswith("#")
        )
    except (OSError, UnicodeError):
        return ()


def ignore_match(path: str, pattern: str) -> bool:
    directory_only = pattern.endswith("/")
    body = pattern.removesuffix("/") if directory_only else pattern
    anchored = body.startswith("/")
    body = body.removeprefix("/")
    if not body:
        return False
    flags = glob.GLOBSTAR | glob.DOTGLOB
    if directory_only:
        components = path.split("/")[:-1]
        return any(
            glob.globmatch(
                "/".join(components[: index + 1]) if anchored or "/" in body else component,
                body,
                flags=flags,
            )
            for index, component in enumerate(components)
        )
    if "**" in body:
        return bool(glob.globmatch(path, body, flags=flags))
    if "/" not in body:
        return bool(
            glob.globmatch(path if anchored else PurePosixPath(path).name, body, flags=flags)
        )
    return bool(glob.globmatch(path, body, flags=flags)) or (
        not anchored and path.endswith("/" + body)
    )


def excluded_path(path: str, patterns: tuple[str, ...]) -> bool:
    if any(path == directory or path.startswith(directory + "/") for directory in EXCLUDED_DIRS):
        return True
    excluded = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        body = pattern.removeprefix("!") if negated else pattern
        if not body or (negated and body.endswith("/")):
            continue
        if ignore_match(path, body):
            excluded = not negated
    return excluded


def enumerate_scan_paths(root: Path) -> tuple[str, ...]:
    patterns = load_ignore_patterns(root)
    try:
        repo = GitRepository(root)
    except ValueError:
        files: list[str] = []
        for directory, folders, names in os.walk(root, followlinks=False):
            folders[:] = sorted(
                name
                for name in folders
                if not (Path(directory) / name).is_symlink()
                and not excluded_path(
                    (Path(directory) / name).relative_to(root).as_posix(), patterns
                )
            )
            files.extend(
                (Path(directory) / name).relative_to(root).as_posix() for name in sorted(names)
            )
    else:
        prefix = root.relative_to(repo.root).as_posix()
        raw = repo.run("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", prefix)
        files = [entry.decode() for entry in raw.split(b"\0") if entry]
        if prefix != ".":
            files = [path.removeprefix(prefix + "/") for path in files]
    return tuple(
        path
        for path in sorted(set(files))
        if not excluded_path(path, patterns)
        and not (root / path).is_symlink()
        and (root / path).is_file()
    )
