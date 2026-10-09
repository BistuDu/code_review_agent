"""冻结输入准备。所有目标读取完成后才启动模型，不改索引或 checkout。"""

from __future__ import annotations

import difflib
from dataclasses import replace
from pathlib import Path

from ..config import Settings
from ..contracts import FileSnapshot, InputMode, ReviewInput, stable_digest
from .diff import parse_hunks
from .git import Change, GitRepository
from .scan_files import enumerate_scan_paths
from .selection import select_files


def safe_relative_path(path: str) -> str:
    requested = Path(path)
    if not path or requested.is_absolute() or ".." in requested.parts or "\x00" in path:
        raise ValueError(f"Path outside repository: {path!r}")
    return requested.as_posix()


def _text(data: bytes) -> tuple[str, bool]:
    if b"\0" in data:
        return "", True
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


def _read_workspace(root: Path, path: str, limit: int) -> FileSnapshot:
    requested = root / safe_relative_path(path)
    resolved = requested.resolve()
    if not resolved.is_relative_to(root):
        return FileSnapshot(path, "", binary=True)
    if not requested.exists():
        return FileSnapshot(path, "", deleted=True)
    if not resolved.is_file():
        return FileSnapshot(path, "", binary=True)
    size = resolved.stat().st_size
    if size > limit:
        return FileSnapshot(path, "", byte_size=size)
    try:
        content, binary = _text(resolved.read_bytes())
        return FileSnapshot(path, content, binary=binary, byte_size=size)
    except OSError:
        return FileSnapshot(path, "", binary=True)


def _read_tree(repo: GitRepository, sha: str, limit: int) -> dict[str, FileSnapshot]:
    result: dict[str, FileSnapshot] = {}
    tree = repo.tree(sha)
    blobs = repo.blobs([blob for _, blob in tree.values()], limit)
    links: dict[str, str] = {}
    for path, (mode, blob) in tree.items():
        safe_relative_path(path)
        data, size = blobs[blob]
        if size > limit:
            result[path] = FileSnapshot(path, "", byte_size=size)
            continue
        if mode == "120000":
            links[path] = data.decode(errors="replace")
            continue
        content, binary = _text(data)
        result[path] = FileSnapshot(path, content, binary=binary, byte_size=size)

    def resolve_link(path: str, visited: set[str]) -> FileSnapshot:
        import posixpath
        from dataclasses import replace

        if path in visited or path not in links:
            return result.get(path, FileSnapshot(path, "", binary=True))
        target = links[path]
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if target.startswith("/") or joined == ".." or joined.startswith("../"):
            return FileSnapshot(path, "", binary=True)
        source = resolve_link(joined, visited | {path})
        return replace(source, path=path)

    for path in links:
        result[path] = resolve_link(path, set())
    return result


def _status(repo: GitRepository) -> tuple[str, ...]:
    entries = iter(repo.run("status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0"))
    values: list[str] = []
    for entry in entries:
        if not entry:
            continue
        text = entry.decode()
        state = text[:2]
        if "U" in state or state in {"AA", "DD"}:
            raise ValueError("Resolve unmerged workspace files before review")
        values.append(text)
        if "R" in state or "C" in state:
            values.append(next(entries).decode())
    return tuple(values)


def _untracked_diff(file: FileSnapshot) -> str:
    return "".join(
        difflib.unified_diff(
            [],
            file.content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile="b/" + file.path,
        )
    )


def prepare_input(
    repo_path: Path,
    settings: Settings,
    *,
    mode: str = "review",
    from_ref: str | None = None,
    to_ref: str | None = None,
    commit: str | None = None,
    scan_path: str | None = None,
    range_strategy: str = "merge-base",
) -> ReviewInput:
    if range_strategy not in {"merge-base", "direct"}:
        raise ValueError("Invalid revision range strategy")
    if commit and (from_ref or to_ref):
        raise ValueError("commit cannot be combined with a revision range")
    if mode == "scan" and (commit or from_ref or to_ref):
        raise ValueError("Scan does not accept revision arguments")
    if bool(from_ref) != bool(to_ref):
        raise ValueError("from and to must both be provided")
    if mode not in {"review", "scan"}:
        raise ValueError("Invalid input mode")
    if mode == "scan":
        return prepare_scan(repo_path, settings, scan_path)
    repo = GitRepository(repo_path)
    workspace_state: tuple[str, ...] = ()
    requested_base: str | None = None
    if commit:
        input_mode = InputMode.COMMIT
        head = repo.resolve(commit)
        parents = repo.parents(head)
        base = parents[0] if parents else None
    elif from_ref and to_ref:
        input_mode = InputMode.RANGE
        requested_base, head = repo.resolve(from_ref), repo.resolve(to_ref)
        base = (
            requested_base if range_strategy == "direct" else repo.merge_base(requested_base, head)
        )
    else:
        input_mode = InputMode.WORKSPACE
        base, head = repo.resolve("HEAD"), None
        workspace_state = _status(repo)
    old_files = _read_tree(repo, base, settings.max_file_bytes) if base else {}
    changes = list(repo.changes(base, head))
    if head:
        files = _read_tree(repo, head, settings.max_file_bytes)
    else:
        paths = {
            entry.decode()
            for entry in repo.run("ls-files", "-c", "-o", "--exclude-standard", "-z").split(b"\0")
            if entry
        }
        files = {
            path: _read_workspace(repo.root, path, settings.max_file_bytes)
            for path in sorted(paths)
        }
        untracked = [
            entry.decode()
            for entry in repo.run("ls-files", "-o", "--exclude-standard", "-z").split(b"\0")
            if entry
        ]
        changes.extend(Change("?", path) for path in untracked)
    changed: list[FileSnapshot] = []
    for change in changes:
        file = files.get(change.path, FileSnapshot(change.path, "", deleted=True))
        old = old_files.get(change.old_path or change.path)
        diff = _untracked_diff(file) if change.status == "?" else repo.diff(base, head, change)
        updated = replace(
            file,
            old_content=old.content if old else "",
            old_path=change.old_path,
            diff=diff,
            hunks=parse_hunks(diff),
        )
        changed.append(updated)
        files[change.path] = updated
    selection = select_files(tuple(changed), settings)
    if head is None and workspace_state != _status(repo):
        raise ValueError("Workspace changed while freezing input; retry the review")
    if head is None:
        for path, file in files.items():
            current = _read_workspace(repo.root, path, settings.max_file_bytes)
            if (current.content_hash, current.byte_size, current.deleted, current.binary) != (
                file.content_hash,
                file.byte_size,
                file.deleted,
                file.binary,
            ):
                raise ValueError("Workspace content changed while freezing input; retry the review")
    ordered = tuple(files[path] for path in sorted(files))
    identity = stable_digest(
        {
            "repo": str(repo.root),
            "mode": input_mode.value,
            "base": base,
            "head": head,
            "from": requested_base,
            "files": [
                [file.path, file.content_hash, file.byte_size, file.binary, file.deleted]
                for file in ordered
            ],
            "state": list(workspace_state),
            **({"range_strategy": "direct"} if range_strategy == "direct" else {}),
        }
    )
    return ReviewInput(
        str(repo.root),
        input_mode,
        base,
        head,
        ordered,
        selection,
        identity,
        workspace_state,
        requested_base,
        range_strategy,
    )


def prepare_scan(repo_path: Path, settings: Settings, scan_path: str | None = None) -> ReviewInput:
    root = repo_path.resolve(strict=True)
    if root.is_file():
        scan_path, root = root.name, root.parent
    requested = root / safe_relative_path(scan_path or ".")
    if not requested.resolve().is_relative_to(root):
        raise ValueError("Scan path outside repository")
    if not requested.exists():
        raise ValueError("Scan path does not exist")
    all_paths = enumerate_scan_paths(root)
    files = tuple(_read_workspace(root, path, settings.max_file_bytes) for path in all_paths)
    selected = tuple(file for file in files if (root / file.path).is_relative_to(requested))
    selection = select_files(selected, settings)
    identity = stable_digest(
        {
            "repo": str(root),
            "mode": "scan",
            "scope": scan_path,
            "files": [
                [file.path, file.content_hash, file.byte_size, file.binary] for file in files
            ],
        }
    )
    return ReviewInput(str(root), InputMode.SCAN, None, None, files, selection, identity)
