"""普通仓库与共享 checkout 的行为测试，不访问 GitHub。"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from code_review_agent.contracts import stable_digest
from code_review_agent.evaluation.dataset import PullRequest
from code_review_agent.evaluation.repositories import RepositoryPool
from code_review_agent.project_paths import ProjectPaths


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class LocalPool(RepositoryPool):
    def __init__(self, paths: ProjectPaths, remote: Path, **kwargs):
        super().__init__(paths, **kwargs)
        self.remote = remote

    def _clone_url(self, pr):
        return str(self.remote)


@pytest.fixture
def fixture(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q", "-b", "main")
    git(remote, "config", "user.name", "Test")
    git(remote, "config", "user.email", "test@example.invalid")
    git(remote, "config", "commit.gpgsign", "false")
    (remote / "app.py").write_text("value = 1\n")
    (remote / ".gitignore").write_text("ignored\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "source")
    source = git(remote, "rev-parse", "HEAD")
    (remote / "app.py").write_text("value = 2\n")
    git(remote, "commit", "-qam", "target")
    target = git(remote, "rev-parse", "HEAD")
    root = tmp_path / "project"
    root.mkdir()
    pool = LocalPool(ProjectPaths(root), remote)
    pr = PullRequest(
        "https://github.com/test/repo/pull/1", "test/repo", "Python", source, target, ()
    )
    return pool, pr, remote


def test_clone_and_reuse(fixture):
    pool, pr, remote = fixture
    path = pool.prepare(pr)
    assert path.name == "test__repo"
    assert (path / ".git").is_dir()
    assert git(path, "rev-parse", "--is-shallow-repository") == "false"
    assert (path / "app.py").read_text() == "value = 2\n"
    assert git(path, "show", f"{pr.base_sha}:app.py") == "value = 1"
    (remote / "app.py").write_text("value = 3\n")
    git(remote, "commit", "-qam", "next")
    next_pr = PullRequest(
        "https://github.com/test/repo/pull/2",
        pr.repo,
        pr.language,
        pr.head_sha,
        git(remote, "rev-parse", "HEAD"),
        (),
    )
    assert pool.prepare(next_pr) == path
    assert (path / "app.py").read_text() == "value = 3\n"
    assert not (pool.paths.root / ".cache/benchmark-workspaces").exists()


def test_reuses_legacy_objects_without_duplicate_clone(fixture):
    pool, pr, _ = fixture
    path = pool.root / stable_digest(pr.repo)[:24]
    subprocess.check_call(["git", "clone", "-q", pool._clone_url(pr), str(path)])
    path.with_name(path.name + ".owner").write_text(pr.repo)
    for filename in ("app.py", ".gitignore"):
        (path / filename).unlink()
    assert pool.prepare(pr) == path
    assert (path / "app.py").read_text() == "value = 2\n"
    assert not (pool.root / "test__repo").exists()


@pytest.mark.parametrize("kind", ["unowned", "origin", "git_pointer", "symlink"])
def test_rejects_unowned_and_external_copies_before_mutation(fixture, kind):
    pool, pr, remote = fixture
    path = pool.prepare(pr)
    (path / "app.py").write_text("DO NOT DELETE")
    if kind == "unowned":
        path.with_name(path.name + ".owner").unlink()
    elif kind == "origin":
        git(path, "remote", "set-url", "origin", "https://example.invalid/other.git")
    elif kind == "git_pointer":
        (path / ".git").rename(path / "original_git")
        (path / ".git").write_text(f"gitdir: {remote / '.git'}\n")
    else:
        destination = path.with_name("outside")
        path.rename(destination)
        path.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError):
        pool.prepare(pr)
    assert (path / "app.py").read_text() == "DO NOT DELETE"


async def test_checkout_cleanup_after_failure_and_switch(fixture):
    pool, pr, _ = fixture
    path = pool.prepare(pr)
    with pytest.raises(RuntimeError, match="review failed"):
        async with pool.review_checkout(pr) as prepared:
            assert prepared.actual_head == pr.head_sha
            (path / "app.py").write_text("changed")
            (path / "untracked").write_text("temporary")
            (path / "ignored").write_text("temporary")
            raise RuntimeError("review failed")
    assert prepared.cleanup_status == "clean"
    assert (path / "app.py").read_text() == "value = 2\n"
    assert not (path / "untracked").exists()
    assert not (path / "ignored").exists()
    previous = PullRequest(pr.pr_url, pr.repo, pr.language, pr.head_sha, pr.base_sha, ())
    async with pool.review_checkout(previous):
        assert git(path, "rev-parse", "HEAD") == pr.base_sha
        assert (path / "app.py").read_text() == "value = 1\n"


async def test_shared_checkout_locked_while_event_loop_keeps_running(fixture):
    pool, pr, _ = fixture
    path = pool.prepare(pr)
    second = LocalPool(pool.paths, pool.remote, lock_timeout=0.15)
    async with pool.review_checkout(pr):
        task = asyncio.create_task(second.review_checkout(pr).__aenter__())
        await asyncio.sleep(0.05)
        assert not task.done()
        with pytest.raises(ValueError, match="lock wait"):
            await task
        assert git(path, "rev-parse", "HEAD") == pr.head_sha
    async with second.review_checkout(pr):
        assert git(path, "rev-parse", "HEAD") == pr.head_sha


async def test_cleanup_after_cancel(fixture):
    pool, pr, _ = fixture
    path = pool.prepare(pr)
    started = asyncio.Event()

    async def attempt():
        async with pool.review_checkout(pr):
            (path / "untracked").write_text("temporary")
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(attempt())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (path / "untracked").exists()
    async with pool.review_checkout(pr):
        pass


async def test_live_orphan_process_prevents_checkout(fixture):
    pool, pr, _ = fixture
    pool.prepare(pr)
    process = subprocess.Popen(["sleep", "10"], start_new_session=True)
    try:
        pool.register_process(pr, process.pid)
        with pytest.raises(ValueError, match="unreaped"):
            async with pool.review_checkout(pr):
                pytest.fail("Live process cannot lose its checkout")
    finally:
        os.killpg(process.pid, 15)
        process.wait()
    async with pool.review_checkout(pr):
        pass
    assert not pool._sidecar(pr, ".lease.json").exists()


def test_direct_comparison_does_not_require_common_ancestor(fixture):
    from code_review_agent.config import Settings
    from code_review_agent.inputs.snapshots import prepare_input

    pool, pr, remote = fixture
    git(remote, "checkout", "--orphan", "unrelated")
    git(remote, "rm", "-rf", ".")
    (remote / "app.py").write_text("value = 100\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "unrelated root")
    selected = PullRequest(
        pr.pr_url, pr.repo, pr.language, pr.base_sha, git(remote, "rev-parse", "HEAD"), ()
    )
    path = pool.prepare(selected)
    frozen = prepare_input(
        path,
        Settings(),
        from_ref=selected.base_sha,
        to_ref=selected.head_sha,
        range_strategy="direct",
    )
    assert frozen.base_sha == selected.base_sha
    assert frozen.head_sha == selected.head_sha
    assert frozen.range_strategy == "direct"
    assert frozen.targets[0].old_content == "value = 1\n"
    with pytest.raises(ValueError):
        prepare_input(path, Settings(), from_ref=selected.base_sha, to_ref=selected.head_sha)


async def test_cleanup_error_preserves_result_and_next_attempt_rechecks(fixture, monkeypatch):
    pool, pr, _ = fixture
    pool.prepare(pr)
    original = pool._clean
    async with pool.review_checkout(pr) as prepared:
        monkeypatch.setattr(
            pool, "_clean", lambda *_: (_ for _ in ()).throw(ValueError("cleanup unavailable"))
        )
    assert prepared.cleanup_status == "failed"
    assert prepared.warnings
    with pytest.raises(ValueError, match="cleanup unavailable"):
        async with pool.review_checkout(pr):
            pytest.fail("Dirty worktree must not be reused")
    monkeypatch.setattr(pool, "_clean", original)
    async with pool.review_checkout(pr) as recovered:
        assert recovered.actual_head == pr.head_sha


def test_clone_arguments_and_failed_clone_not_published(fixture, monkeypatch):
    pool, pr, _ = fixture
    calls = []
    original = pool._git

    def capture(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(pool, "_git", capture)
    pool.prepare(pr)
    clone = next(args for args in calls if args[0] == "clone")
    assert len(clone) == 3
    assert not any("--global" in args for args in calls)
    assert any(args[-3:] == ("fetch", "--all", "--tags") for args in calls)
    other = PullRequest(pr.pr_url, "test/failed", pr.language, pr.base_sha, pr.head_sha, ())

    def fail(*args, **kwargs):
        if args[0] == "clone":
            Path(args[-1]).mkdir()
            raise ValueError("clone interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(pool, "_git", fail)
    with pytest.raises(ValueError, match="clone interrupted"):
        pool.prepare(other)
    assert not (pool.root / "test__failed").exists()
    assert not list(pool.root.glob(".clone-*"))


def test_fetch_warning_with_complete_versions_and_unknown_sha(fixture, monkeypatch):
    pool, pr, _ = fixture
    pool.prepare(pr)
    original = pool._git

    def network_down(*args, **kwargs):
        if "fetch" in args:
            raise ValueError("network down")
        return original(*args, **kwargs)

    monkeypatch.setattr(pool, "_git", network_down)
    with pool._lock(pr):
        prepared = pool._prepare_locked(pr, update=True)
    assert prepared.warnings and prepared.actual_head == pr.head_sha
    unknown = PullRequest(pr.pr_url, pr.repo, pr.language, pr.base_sha, "f" * 40, ())
    with pytest.raises(ValueError, match="network down"):
        pool.prepare(unknown)
    assert git(prepared.path, "rev-parse", "HEAD") == pr.head_sha


def test_missing_blob_is_fetched_explicitly(fixture):
    pool, pr, _ = fixture
    path = pool.prepare(pr)
    blob = git(path, "rev-parse", f"{pr.base_sha}:app.py")
    obj = path / ".git/objects" / blob[:2] / blob[2:]
    assert obj.is_file()
    obj.unlink()
    assert pool.prepare(pr) == path
    assert git(path, "show", f"{pr.base_sha}:app.py") == "value = 1"
