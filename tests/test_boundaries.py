import ast
from dataclasses import replace
from pathlib import Path

import pytest
from test_inputs import git

from code_review_agent.config import Settings
from code_review_agent.contracts import stable_digest
from code_review_agent.evaluation.dataset import PullRequest
from code_review_agent.evaluation.repositories import RepositoryPool
from code_review_agent.inputs.git import GitRepository
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.project_paths import ProjectPaths, project_root


def test_managed_repository_ownership_and_symlink_boundary(tmp_path: Path):
    paths = ProjectPaths(tmp_path)
    pool = RepositoryPool(paths)
    pr = PullRequest(
        "https://github.com/org/repo/pull/1", "org/repo", "Python", "a" * 40, "b" * 40, ()
    )
    destination = pool.root / stable_digest(pr.repo)[:24]
    destination.mkdir()
    with pytest.raises(ValueError, match="unowned"):
        pool.prepare(pr)
    marker = destination.parent / (destination.name + ".owner")
    marker.write_text(pr.repo)
    git(destination, "init", "-q", "-b", "main")
    git(destination, "config", "user.name", "Test")
    git(destination, "config", "user.email", "test@example.invalid")
    git(destination, "config", "commit.gpgsign", "false")
    (destination / "a.py").write_text("return x\n")
    git(destination, "add", ".")
    git(destination, "commit", "-qm", "initial")
    sha = git(destination, "rev-parse", "HEAD")
    git(destination, "remote", "add", "origin", "https://github.com/org/repo.git")
    frozen_pr = replace(pr, base_sha=sha, head_sha=sha)
    before = git(destination, "status", "--porcelain")
    assert pool.prepare(frozen_pr) == destination
    assert git(destination, "status", "--porcelain") == before
    escaped = pool.root / stable_digest("org/escape")[:24]
    other = tmp_path / "user-repo"
    other.mkdir()
    escaped.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="escape"):
        pool.prepare(replace(pr, repo="org/escape"))


def test_production_imports_stay_independent_of_evaluation_and_reference_projects():
    root = project_root() / "src/code_review_agent"
    for file in root.rglob("*.py"):
        text = file.read_text()
        assert "open-code-review-main" not in text
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if file.parent.name not in {"application", "evaluation"}:
                    assert not (node.module or "").startswith("evaluation")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"run", "Popen", "check_output"}:
                    assert not any(
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value
                        for keyword in node.keywords
                    )


def test_partial_copy_materializes_both_sides_and_merge_base(tmp_path: Path, monkeypatch):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    git(source, "config", "uploadpack.allowFilter", "true")
    git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    (source / "a.py").write_text("return 1\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "common")
    common = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-qb", "feature")
    (source / "a.py").write_text("return 2\n")
    git(source, "commit", "-qam", "feature")
    head = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", "main")
    (source / "b.py").write_text("return 3\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "main")
    base = git(source, "rev-parse", "HEAD")
    pool = RepositoryPool(ProjectPaths(tmp_path))
    monkeypatch.setattr(pool, "_clone_url", lambda pr: source.as_uri())
    pr = PullRequest("https://github.com/org/repo/pull/1", "org/repo", "Python", base, head, ())
    destination = pool.root / stable_digest(pr.repo)[:24]
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            source.as_uri(),
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    (pool.root / (destination.name + ".owner")).write_text(pr.repo)
    repository = GitRepository(destination)
    blobs = sorted(
        {blob for sha in (base, head, common) for _, blob in repository.tree(sha).values()}
    )
    assert pool._missing_blobs(repository, blobs)
    assert pool.prepare(pr) == destination
    assert repository.merge_base(base, head) == common
    assert not pool._missing_blobs(repository, blobs)
    expected = prepare_input(source, Settings(), from_ref=base, to_ref=head)
    actual = prepare_input(destination, Settings(), from_ref=base, to_ref=head)
    assert actual.files == expected.files
    assert actual.selection == expected.selection
    assert actual.base_sha == expected.base_sha
    assert actual.head_sha == expected.head_sha


def test_shallow_history_deepens_before_freezing_input(tmp_path: Path, monkeypatch):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    git(source, "config", "uploadpack.allowFilter", "true")
    git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    (source / "a.py").write_text("return 1\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "common")
    common = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-qb", "feature")
    (source / "a.py").write_text("return 2\n")
    git(source, "commit", "-qam", "feature")
    head = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", "main")
    (source / "b.py").write_text("return 3\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "main")
    base = git(source, "rev-parse", "HEAD")
    pool = RepositoryPool(ProjectPaths(tmp_path))
    monkeypatch.setattr(pool, "_clone_url", lambda pr: source.as_uri())
    pr = PullRequest("https://github.com/org/repo/pull/1", "org/repo", "Python", base, head, ())
    destination = pool.root / stable_digest(pr.repo)[:24]
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--depth=1",
            "--filter=blob:none",
            "--no-checkout",
            source.as_uri(),
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    git(
        destination,
        "fetch",
        "--depth=1",
        "--filter=blob:none",
        "origin",
        f"{head}:refs/code-review/frozen/{head}",
    )
    (pool.root / (destination.name + ".owner")).write_text(pr.repo)
    repository = GitRepository(destination)
    with pytest.raises(ValueError):
        repository.merge_base(base, head)
    assert pool.prepare(pr) == destination
    assert repository.merge_base(base, head) == common
    expected = prepare_input(source, Settings(), from_ref=base, to_ref=head)
    actual = prepare_input(destination, Settings(), from_ref=base, to_ref=head)
    assert actual.files == expected.files
    assert actual.selection == expected.selection


def test_visible_merge_base_is_not_accepted_with_unverified_branch(tmp_path: Path, monkeypatch):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    git(source, "config", "uploadpack.allowFilter", "true")
    git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    (source / "a.py").write_text("return 0\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "root")
    older = git(source, "rev-parse", "HEAD")
    (source / "a.py").write_text("return 1\n")
    git(source, "commit", "-qam", "true common")
    common = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-qb", "side")
    for value in (2, 3):
        (source / "a.py").write_text(f"return {value}\n")
        git(source, "commit", "-qam", f"side {value}")
    boundary = git(source, "rev-parse", "HEAD")
    tree = git(source, "rev-parse", "HEAD^{tree}")
    head = git(source, "commit-tree", tree, "-p", older, "-p", boundary, "-m", "merge")
    git(source, "update-ref", "refs/heads/feature", head)
    git(source, "checkout", "-q", "main")
    (source / "a.py").write_text("return 4\n")
    git(source, "commit", "-qam", "main")
    base = git(source, "rev-parse", "HEAD")
    pool = RepositoryPool(ProjectPaths(tmp_path))
    monkeypatch.setattr(pool, "_clone_url", lambda pr: source.as_uri())
    pr = PullRequest("https://github.com/org/repo/pull/1", "org/repo", "Python", base, head, ())
    destination = pool.root / stable_digest(pr.repo)[:24]
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--no-checkout",
            source.as_uri(),
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    (pool.root / (destination.name + ".owner")).write_text(pr.repo)
    # 隐去 side 分支的父边，Git 仍能返回一个存在但过老的共同祖先。
    (destination / ".git/shallow").write_text(boundary + "\n")
    repository = GitRepository(destination)
    assert repository.merge_base(base, head) == older
    assert pool.prepare(pr) == destination
    assert repository.merge_base(base, head) == common
    expected = prepare_input(source, Settings(), from_ref=base, to_ref=head)
    actual = prepare_input(destination, Settings(), from_ref=base, to_ref=head)
    assert actual.files == expected.files
