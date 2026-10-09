"""Git 只读命令。引用先冻结为完整 SHA，后续文件读取使用冻结值。"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Change:
    status: str
    path: str
    old_path: str | None = None


class GitRepository:
    def __init__(self, path: Path) -> None:
        self.root = path.resolve(strict=True)
        top = self.run("rev-parse", "--show-toplevel").decode().strip()
        self.root = Path(top).resolve(strict=True)

    def run(self, *arguments: str, input_data: bytes | None = None) -> bytes:
        environment = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "LC_ALL": "C",
        }
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(self.root), *arguments],
            capture_output=True,
            check=False,
            env=environment,
            timeout=60,
            input=input_data,
        )
        if result.returncode:
            # 不输出完整环境、请求凭证或额外命令上下文。
            raise ValueError(
                f"Git operation failed: {result.stderr.decode(errors='replace').strip()}"
            )
        return result.stdout

    def resolve(self, ref: str) -> str:
        sha = (
            self.run("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
            .decode()
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", sha):
            raise ValueError("Expected a full commit identity")
        return sha

    def parents(self, sha: str) -> tuple[str, ...]:
        return tuple(self.run("rev-list", "--parents", "-n", "1", sha).decode().split()[1:])

    def merge_base(self, base: str, head: str) -> str:
        return self.run("merge-base", base, head).decode().strip()

    def tree(self, sha: str) -> dict[str, tuple[str, str]]:
        files: dict[str, tuple[str, str]] = {}
        for entry in self.run("ls-tree", "-r", "-z", sha).split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, blob = metadata.decode().split()
            if kind == "blob":
                files[path.decode()] = (mode, blob)
        return files

    def blob(self, identity: str) -> bytes:
        return self.run("cat-file", "blob", identity)

    def blobs(self, identities: list[str], limit: int) -> dict[str, tuple[bytes, int]]:
        ordered = sorted(set(identities))
        if not ordered:
            return {}
        metadata = self.run(
            "cat-file", "--batch-check", input_data=("\n".join(ordered) + "\n").encode()
        )
        sizes: dict[str, int] = {}
        for line in metadata.decode().splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[1] != "blob":
                raise ValueError(
                    "Frozen Git blob missing; prepare a complete repository copy first"
                )
            sizes[fields[0]] = int(fields[2])
        readable = [identity for identity in ordered if sizes[identity] <= limit]
        result = {identity: (b"", size) for identity, size in sizes.items() if size > limit}
        for offset in range(0, len(readable), 32):
            batch = readable[offset : offset + 32]
            data = self.run("cat-file", "--batch", input_data=("\n".join(batch) + "\n").encode())
            cursor = 0
            for identity in batch:
                header_end = data.index(b"\n", cursor)
                size = sizes[identity]
                begin = header_end + 1
                result[identity] = (data[begin : begin + size], size)
                cursor = begin + size + 1
        return result

    def changes(self, base: str | None, head: str | None) -> tuple[Change, ...]:
        revisions = [ref for ref in (base, head) if ref is not None]
        arguments = ["diff", "--no-ext-diff", "--no-textconv", "-M", "--name-status", "-z"]
        if base is None:
            arguments = ["diff-tree", "--root", "--no-commit-id", "-r", "-M", "--name-status", "-z"]
        entries = iter(self.run(*arguments, *revisions, "--").split(b"\0"))
        changes: list[Change] = []
        for status in entries:
            if not status:
                continue
            first = next(entries).decode()
            name = status.decode()
            if name.startswith(("R", "C")):
                changes.append(Change(name, next(entries).decode(), first))
            else:
                changes.append(Change(name, first))
        return tuple(changes)

    def diff(self, base: str | None, head: str | None, change: Change) -> str:
        files = [change.old_path, change.path] if change.old_path else [change.path]
        revisions = [ref for ref in (base, head) if ref is not None]
        arguments = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M"]
        if base is None:
            arguments = ["show", "--format=", "--no-ext-diff", "--no-textconv", "--no-color"]
        output = self.run(*arguments, *revisions, "--", *files)
        return output.decode(errors="replace")
