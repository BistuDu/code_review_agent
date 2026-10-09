"""评测仓库缓存：获取版本、checkout 与全使用期锁。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from filelock import FileLock, Timeout

from ..contracts import JsonObject, stable_digest
from ..inputs.git import GitRepository
from ..project_paths import ProjectPaths
from ..sessions.store import redact
from .dataset import PullRequest

T = TypeVar("T")


@dataclass
class PreparedRepository:
    path: Path
    repo: str
    source: str
    target: str
    actual_head: str
    preparation_seconds: float
    lock_wait_seconds: float = 0.0
    cleanup_seconds: float = 0.0
    cleanup_status: str = "pending"
    warnings: list[str] = field(default_factory=list)

    def metadata(self) -> JsonObject:
        return {
            "repo": self.repo,
            "path": str(self.path),
            "source": self.source,
            "target": self.target,
            "actual_head": self.actual_head,
            "preparation_seconds": self.preparation_seconds,
            "lock_wait_seconds": self.lock_wait_seconds,
            "cleanup_seconds": self.cleanup_seconds,
            "cleanup_status": self.cleanup_status,
            "warnings": list(self.warnings),
        }


async def _settle(task: asyncio.Task[T]) -> T:
    """取消时仍等待正在改写工作区的线程结束，不能提前释放仓库锁。"""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class RepositoryPool:
    def __init__(self, paths: ProjectPaths, *, lock_timeout: float = 1800) -> None:
        self.paths = paths
        self.root = paths.directory(".cache/benchmark-repositories")
        self.lock_timeout = lock_timeout

    @staticmethod
    def _git(*arguments: str, timeout: int = 600) -> bytes:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *arguments],
            check=False,
            timeout=timeout,
            capture_output=True,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "1",
            },
        )
        if result.returncode:
            raise ValueError(
                "Git preparation failed: " + result.stderr.decode(errors="replace").strip()
            )
        return result.stdout

    def _clone_url(self, pr: PullRequest) -> str:
        return f"https://github.com/{pr.repo}.git"

    def _sidecar(self, pr: PullRequest, suffix: str) -> Path:
        path = self.root / (stable_digest(pr.repo)[:24] + suffix)
        if path.is_symlink():
            raise ValueError("Managed metadata symlink escape")
        return self.paths.writable(path)

    def _lock(self, pr: PullRequest) -> FileLock:
        return FileLock(str(self._sidecar(pr, ".lock")), thread_local=False)

    def _destination(self, pr: PullRequest) -> Path:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", pr.repo) or any(
            part in {".", ".."} for part in pr.repo.split("/")
        ):
            raise ValueError("Invalid managed repository identity")
        named = self.root / pr.repo.replace("/", "__")
        legacy = self.root / stable_digest(pr.repo)[:24]
        for candidate in (named, legacy):
            if candidate.is_symlink():
                raise ValueError("Managed repository symlink escape")
            if candidate.exists():
                marker = candidate.with_name(candidate.name + ".owner")
                if marker.is_symlink() or not marker.is_file() or marker.read_text() != pr.repo:
                    raise ValueError("Refusing to mutate an unowned repository")
        return self.paths.writable(named if named.exists() or not legacy.exists() else legacy)

    def _validate(self, pr: PullRequest, destination: Path) -> GitRepository:
        if destination.parent != self.root or destination.is_symlink():
            raise ValueError("Repository outside managed cache")
        marker = destination.with_name(destination.name + ".owner")
        if marker.is_symlink() or not marker.is_file() or marker.read_text() != pr.repo:
            raise ValueError("Refusing to mutate an unowned repository")
        git_dir = destination / ".git"
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise ValueError("Managed copy must have an independent .git directory")
        repository = GitRepository(destination)
        common = Path(repository.run("rev-parse", "--git-common-dir").decode().strip())
        common = (common if common.is_absolute() else destination / common).resolve()
        if repository.root != destination or common != git_dir.resolve():
            raise ValueError("Managed copy references an external Git directory")
        origin = repository.run("remote", "get-url", "origin").decode().strip()
        if origin != self._clone_url(pr):
            raise ValueError("Managed repository origin identity changed")
        self._check_process(pr)
        return repository

    def _check_process(self, pr: PullRequest) -> None:
        lease = self._sidecar(pr, ".lease.json")
        if not lease.exists():
            return
        data = json.loads(lease.read_text())
        pid = data.get("pid")
        if type(pid) is not int or pid <= 0:
            raise ValueError("Invalid repository process lease")
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            lease.unlink()
        else:
            raise ValueError("Repository still in use by an unreaped review process")

    def register_process(self, pr: PullRequest, pid: int, job_id: str | None = None) -> None:
        """adapter 必须先持 review_checkout 锁，再登记 start_new_session 子进程。"""
        path = self._sidecar(pr, ".lease.json")
        if path.exists():
            self._check_process(pr)
        path.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "repo": pr.repo,
                    "job_id": job_id,
                    "source": pr.base_sha,
                    "target": pr.head_sha,
                }
            )
        )

    def release_process(self, pr: PullRequest) -> None:
        self._check_process(pr)

    def _clone(self, pr: PullRequest, destination: Path) -> None:
        temporary = self.paths.writable(self.root / (".clone-" + uuid.uuid4().hex))
        try:
            self._git("clone", self._clone_url(pr), str(temporary), timeout=3600)
            # 发布前没有 owner 的目标不能被当作可用副本。
            temporary.rename(destination)
            destination.with_name(destination.name + ".owner").write_text(pr.repo)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _ensure_versions(self, repository: GitRepository, pr: PullRequest) -> None:
        for sha in (pr.base_sha, pr.head_sha):
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("Expected full dataset commit hash")
            try:
                repository.resolve(sha)
            except ValueError:
                self._git("-C", str(repository.root), "fetch", "origin", sha, timeout=1800)
            repository.resolve(sha)
        identities = sorted(
            {
                blob
                for sha in (pr.base_sha, pr.head_sha)
                for _, blob in repository.tree(sha).values()
            }
        )
        if not identities:
            return
        missing = self._missing_blobs(repository, identities)
        for offset in range(0, len(missing), 256):
            self._git(
                "-C",
                str(repository.root),
                "fetch",
                "--no-write-fetch-head",
                "origin",
                *missing[offset : offset + 256],
                timeout=1800,
            )
        if self._missing_blobs(repository, identities):
            raise ValueError("Frozen source objects remain missing")

    @staticmethod
    def _missing_blobs(repository: GitRepository, identities: list[str]) -> list[str]:
        if not identities:
            return []
        check = repository.run(
            "cat-file", "--batch-check", input_data=("\n".join(identities) + "\n").encode()
        )
        missing = []
        for line in check.decode().splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == "missing":
                missing.append(fields[0])
            elif len(fields) != 3 or fields[1] != "blob":
                raise ValueError("Unexpected source object metadata")
        return missing

    def _clean(self, pr: PullRequest, destination: Path) -> None:
        repository = self._validate(pr, destination)
        try:
            repository.resolve("HEAD")
        except ValueError:
            # 旧 init/fetch 缓存可能还没有 HEAD，端点已在 prepare 中核验。
            self._git("-C", str(destination), "reset", "--hard", pr.head_sha)
        else:
            self._git("-C", str(destination), "reset", "--hard")
        self._git("-C", str(destination), "clean", "-fdx")
        if self._git(
            "-C", str(destination), "status", "--porcelain", "--untracked-files=all"
        ).strip():
            raise ValueError("Managed worktree remains dirty after cleanup")

    def _prepare_locked(self, pr: PullRequest, *, update: bool) -> PreparedRepository:
        started = time.monotonic()
        destination = self._destination(pr)
        warnings: list[str] = []
        if not destination.exists():
            self._check_process(pr)
            self._clone(pr, destination)
        repository = self._validate(pr, destination)
        if update:
            try:
                # 旧 init/fetch 缓存可能没有普通分支 refspec，仅改受管副本。
                self._git(
                    "-C",
                    str(destination),
                    "config",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                )
                if repository.run("rev-parse", "--is-shallow-repository").strip() == b"true":
                    self._git(
                        "-C", str(destination), "fetch", "--unshallow", "origin", timeout=1800
                    )
                self._git("-C", str(destination), "fetch", "--all", "--tags", timeout=1800)
            except (ValueError, subprocess.TimeoutExpired) as error:
                warnings.append(f"Repository update warning: {redact(str(error))}")
        self._ensure_versions(repository, pr)
        self._clean(pr, destination)
        self._git("-C", str(destination), "checkout", "-f", pr.head_sha)
        if repository.resolve("HEAD") != pr.head_sha:
            raise ValueError("Checked out HEAD differs from dataset TARGET")
        if repository.run("status", "--porcelain", "--untracked-files=all").strip():
            raise ValueError("TARGET checkout is dirty")
        return PreparedRepository(
            destination,
            pr.repo,
            pr.base_sha,
            pr.head_sha,
            pr.head_sha,
            time.monotonic() - started,
            warnings=warnings,
        )

    def prepare_metadata(self, pr: PullRequest) -> PreparedRepository:
        """兼容同步准备调用；任务真正使用时仍须进入 review_checkout。"""
        with self._lock(pr).acquire(timeout=self.lock_timeout):
            return self._prepare_locked(pr, update=True)

    def prepare(self, pr: PullRequest) -> Path:
        return self.prepare_metadata(pr).path

    @asynccontextmanager
    async def review_checkout(self, pr: PullRequest) -> AsyncIterator[PreparedRepository]:
        lock = self._lock(pr)
        waiting = time.monotonic()
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() - waiting >= self.lock_timeout:
                    raise ValueError("Repository lock wait timed out") from None
                await asyncio.sleep(0.05)
        prepared: PreparedRepository | None = None
        try:
            prepared = await _settle(
                asyncio.create_task(asyncio.to_thread(self._prepare_locked, pr, update=False))
            )
            prepared.lock_wait_seconds = time.monotonic() - waiting - prepared.preparation_seconds
            yield prepared
        finally:
            try:
                if prepared:
                    started = time.monotonic()
                    try:
                        await _settle(
                            asyncio.create_task(asyncio.to_thread(self._clean, pr, prepared.path))
                        )
                        prepared.cleanup_status = "clean"
                    except (ValueError, OSError) as error:
                        prepared.cleanup_status = "failed"
                        prepared.warnings.append(f"Cleanup failed: {type(error).__name__}")
                    finally:
                        prepared.cleanup_seconds = time.monotonic() - started
            finally:
                lock.release()
