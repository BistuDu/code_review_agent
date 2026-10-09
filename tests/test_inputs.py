import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from code_review_agent.config import Settings
from code_review_agent.contracts import InputMode
from code_review_agent.inputs.diff import parse_hunks
from code_review_agent.inputs.scan import scan_units
from code_review_agent.inputs.snapshots import prepare_input


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True).strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "app.py").write_text("def f(x):\n    return x\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_workspace_combines_staged_unstaged_and_untracked(repository: Path) -> None:
    (repository / "app.py").write_text("def f(x):\n    return x + 1\n")
    git(repository, "add", "app.py")
    (repository / "app.py").write_text("def f(x):\n    return x + 2\n")
    (repository / "new space.py").write_text("new = 1\n")
    before = git(repository, "status", "--porcelain")
    staged = git(repository, "diff", "--cached")
    frozen = prepare_input(repository, Settings())
    assert frozen.mode == InputMode.WORKSPACE
    assert {file.path for file in frozen.targets} == {"app.py", "new space.py"}
    assert next(file for file in frozen.targets if file.path == "app.py").content.endswith(
        "x + 2\n"
    )
    (repository / "app.py").write_text("external mutation\n")
    assert "external mutation" not in frozen.targets[0].content
    assert git(repository, "diff", "--cached") == staged
    (repository / "app.py").write_text("def f(x):\n    return x + 2\n")
    assert git(repository, "status", "--porcelain") == before


def test_frozen_internal_links_and_context_capacity(repository: Path):
    (repository / "link.py").symlink_to("app.py")
    (repository / "outside.py").symlink_to("/outside/secret.py")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "links")
    frozen = prepare_input(repository, Settings(), commit="HEAD")
    files = {file.path: file for file in frozen.files}
    assert files["link.py"].content == files["app.py"].content
    assert files["outside.py"].binary
    from code_review_agent.config import ModelSettings

    small = replace(Settings(), reviewer=ModelSettings(context_tokens=1000, max_output_tokens=128))
    (repository / "large.py").write_text("value = 1\n" * 1000)
    scan = prepare_input(repository, small, mode="scan")
    assert (
        next(item.reason for item in scan.selection if item.path == "large.py")
        == "context_capacity"
    )


def test_final_cancellation_is_not_a_change(repository: Path) -> None:
    original = (repository / "app.py").read_text()
    (repository / "app.py").write_text("staged = 1\n")
    git(repository, "add", "app.py")
    (repository / "app.py").write_text(original)
    assert not prepare_input(repository, Settings()).targets


def test_range_freezes_target_and_merge_base(repository: Path) -> None:
    git(repository, "checkout", "-qb", "feature")
    (repository / "app.py").write_text("def f(x):\n    return 1 / x\n")
    git(repository, "commit", "-qam", "feature")
    head = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "-q", "main")
    frozen = prepare_input(repository, Settings(), from_ref="main", to_ref="feature")
    assert frozen.head_sha == head
    assert "return 1 / x" in frozen.targets[0].content
    assert "return x" in (repository / "app.py").read_text()
    assert frozen.targets[0].hunks[0].lines[-1].new_line == 2
    with pytest.raises(ValueError):
        prepare_input(repository, Settings(), commit="HEAD", from_ref="main", to_ref="feature")
    with pytest.raises(ValueError):
        prepare_input(repository, Settings(), commit="missing-ref")


def test_root_commit_rename_and_deleted_files(repository: Path) -> None:
    root = prepare_input(repository, Settings(), commit="HEAD")
    assert root.base_sha is None
    assert root.targets[0].hunks[0].lines[0].new_line == 1
    git(repository, "mv", "app.py", "renamed space.py")
    git(repository, "commit", "-qm", "rename")
    renamed = prepare_input(repository, Settings(), commit="HEAD")
    assert renamed.targets[0].old_path == "app.py"
    (repository / "renamed space.py").unlink()
    deleted = prepare_input(repository, Settings())
    assert not deleted.targets
    assert deleted.selection[0].reason == "deleted"


def test_scan_selection_boundaries_and_batches(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("a = 1\n")
    (source / "b.js").write_text("let b = 1;\n")
    (source / "large.py").write_text("x" * 100)
    (source / "binary.py").write_bytes(b"\x00\x01")
    (source / "a_test.py").write_text("test = 1\n")
    (tmp_path / "context.py").write_text("context = 1\n")
    outside = tmp_path.parent / "external.py"
    outside.write_text("secret = 1\n")
    (source / "escape.py").symlink_to(outside)
    frozen = prepare_input(
        tmp_path, replace(Settings(), max_file_bytes=50), mode="scan", scan_path="src"
    )
    assert {file.path for file in frozen.targets} == {"src/a.py", "src/b.js"}
    reasons = {item.path: item.reason for item in frozen.selection}
    assert reasons["src/large.py"] == "oversized"
    assert reasons["src/a_test.py"] == "default_exclude"
    assert reasons["src/binary.py"] == "binary_or_unreadable"
    assert "src/escape.py" not in reasons
    assert any(file.path == "context.py" for file in frozen.files)
    for strategy in ("none", "by-language", "by-directory"):
        units = scan_units(tuple(file.path for file in frozen.targets), strategy, 1)
        assert sum(len(unit.paths) for unit in units) == 2
        assert all(len(unit.paths) == 1 for unit in units)
    included = prepare_input(tmp_path, replace(Settings(), include=("**/*_test.py",)), mode="scan")
    assert [file.path for file in included.targets] == ["src/a_test.py"]
    with pytest.raises(ValueError):
        prepare_input(tmp_path, Settings(), mode="scan", scan_path="../external.py")


def test_commit_uses_first_parent_and_scan_works_in_git(repository: Path) -> None:
    git(repository, "checkout", "-qb", "feature")
    (repository / "feature.py").write_text("feature = True\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "feature")
    git(repository, "checkout", "-q", "main")
    first_parent = git(repository, "rev-parse", "HEAD")
    git(repository, "merge", "--no-ff", "-qm", "merge", "feature")
    reviewed = prepare_input(repository, Settings(), commit="HEAD")
    assert reviewed.base_sha == first_parent
    assert [file.path for file in reviewed.targets] == ["feature.py"]
    scanned = prepare_input(repository, Settings(), mode="scan", scan_path="app.py")
    assert [file.path for file in scanned.targets] == ["app.py"]
    assert not scanned.targets[0].hunks


def test_hunks_keep_old_new_sides_and_no_newline_marker() -> None:
    hunks = parse_hunks(
        "@@ -1,2 +1,2 @@\n-old\n+new\n same\n@@ -10 +10 @@\n-last\n+tail\n\\ No newline at end of file\n"
    )
    assert len(hunks) == 2
    assert (hunks[0].lines[0].old_line, hunks[0].lines[0].new_line) == (1, None)
    assert (hunks[0].lines[1].old_line, hunks[0].lines[1].new_line) == (None, 1)
    assert hunks[1].lines[-1].text == "tail"
    assert hunks[1].lines[-1].new_line == 10


def test_user_excludes_override_include(repository: Path) -> None:
    (repository / "app.py").write_text("new = 1\n")
    settings = replace(Settings(), include=("**/*.{py,js}",), exclude=("**/app.py",))
    reviewed = prepare_input(repository, settings)
    assert not reviewed.targets
    assert reviewed.selection[0].reason == "user_exclude"
