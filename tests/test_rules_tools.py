import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from code_review_agent.config import Settings
from code_review_agent.contracts import ReviewUnit
from code_review_agent.inputs.selection import RESOURCE_ROOT
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.review.prompts import prompt_values
from code_review_agent.review.rules import RuleResolver
from code_review_agent.tools.context import ContextTools
from code_review_agent.tools.findings import FindingCollector
from code_review_agent.tools.registry import ToolRegistry


def test_resources_are_independent_and_preserve_manifest() -> None:
    root = RESOURCE_ROOT.parents[2]
    manifest = json.loads((RESOURCE_ROOT / "resource_manifest.json").read_text())
    for item in manifest["resources"]:
        file = root / item["path"]
        assert file.is_file()
        assert hashlib.sha256(file.read_bytes()).hexdigest() == item["sha256"]
    configuration = json.loads((RESOURCE_ROOT / "rules/system_rules.json").read_text())
    for name in [configuration["default_rule"], *configuration["path_rule_map"].values()]:
        assert (RESOURCE_ROOT / "rules" / name).is_file()


def test_rule_priority_merge_sniff_and_filters(tmp_path: Path) -> None:
    (tmp_path / "view.m").write_text("#import <Foundation/Foundation.h>\n")
    (tmp_path / "app.py").write_text("app = 1\n")
    (tmp_path / ".opencodereview").mkdir()
    (tmp_path / ".opencodereview/rule.json").write_text(
        json.dumps(
            {
                "rules": [{"path": "**/*.py", "rule": "project policy", "merge_system_rule": True}],
                "exclude": ["**/view.m"],
            }
        )
    )
    (tmp_path / "custom.json").write_text(
        json.dumps(
            {
                "rules": [
                    {"path": "**/*.{py,m}", "rule": "first custom policy"},
                    {"path": "**/*.py", "rule": "second custom policy"},
                ]
            }
        )
    )
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    paths = ProjectPaths(tmp_path)
    resolver = RuleResolver(paths, frozen, replace(Settings(), rule_file="custom.json"))
    assert resolver.resolve("app.py").content == "first custom policy"
    assert resolver.resolve("view.m").source == "custom"
    project = RuleResolver(paths, frozen, Settings())
    assert "project policy" in project.resolve("app.py").content
    assert project.resolve("app.py").source == "project"
    assert "Objective-C" in project.system_rule("view.m").content
    filtered = project.apply_filters(frozen, Settings())
    assert "view.m" not in {file.path for file in filtered.targets}
    assert len(project.identity) == 64


@pytest.mark.parametrize(
    ("project_policy", "expected"),
    [
        ("shared policy", "shared policy"),
        (
            "Go policy",
            '<rules for="a.py, z.py">\nshared policy\n</rules>\n'
            '<rules for="b.go">\nGo policy\n</rules>',
        ),
    ],
)
def test_group_rules_share_body_and_map_files(
    tmp_path: Path, project_policy: str, expected: str
) -> None:
    for name in ("a.py", "b.go", "z.py"):
        (tmp_path / name).write_text("value = 1\n")
    (tmp_path / ".opencodereview").mkdir()
    (tmp_path / ".opencodereview/rule.json").write_text(
        json.dumps({"rules": [{"path": "**", "rule": project_policy}]})
    )
    (tmp_path / "custom.json").write_text(
        json.dumps({"rules": [{"path": "**/*.py", "rule": "shared policy"}]})
    )
    settings = replace(Settings(), rule_file="custom.json")
    frozen = prepare_input(tmp_path, settings, mode="scan")
    resolver = RuleResolver(ProjectPaths(tmp_path), frozen, settings)
    unit = ReviewUnit("mixed", ("z.py", "b.go", "a.py"))
    assert resolver.resolve("a.py").source == "custom"
    assert resolver.resolve("b.go").source == "project"
    assert prompt_values(frozen, unit, settings, resolver)["system_rule"] == expected
    reordered = replace(unit, paths=tuple(reversed(unit.paths)))
    assert prompt_values(frozen, reordered, settings, resolver)["system_rule"] == expected
    assert unit.paths == ("z.py", "b.go", "a.py")
    single = ReviewUnit("single", ("b.go",))
    assert prompt_values(frozen, single, settings, resolver)["system_rule"] == project_policy


def test_context_freezes_reads_search_modes_and_truncation(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def calculate(x):\n    return x * 2\n" * 300)
    (tmp_path / "other.py").write_text("Other = calculate(1)\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    tools = ContextTools(frozen)
    (tmp_path / "app.py").write_text("changed after freeze\n")
    full_read = tools.file_read("app.py")
    assert "IS_TRUNCATED: true" in full_read
    assert "LINE_RANGE: 1-500" in full_read
    numbered_lines = [line for line in full_read.splitlines() if line.partition("|")[0].isdigit()]
    assert len(numbered_lines) == 500
    assert numbered_lines[0] == "1|def calculate(x):"
    assert numbered_lines[-1] == "500|    return x * 2"
    assert "Note: Results truncated to 500 lines. Please narrow your line range." in full_read
    assert tools.file_read("app.py", 5, 6) == (
        "File: app.py (Total lines: 600)\nIS_TRUNCATED: false\nLINE_RANGE: 5-6\n"
        "5|def calculate(x):\n6|    return x * 2\n"
    )
    exact_limit = tools.file_read("app.py", 101, 600)
    assert "IS_TRUNCATED: false" in exact_limit
    assert "LINE_RANGE: 101-600" in exact_limit
    assert "101|def calculate(x):\n" in exact_limit
    assert exact_limit.endswith("600|    return x * 2\n")
    assert "IS_TRUNCATED: true" in tools.code_search("return")
    assert "other.py" in tools.file_find("OTHER")
    assert "No diff" in tools.file_read_diff(["other.py"])
    assert "other.py" not in tools.code_search("calculate", ["*.py", ":(exclude)other.py"])
    assert "1|Other" in tools.code_search(
        r"(?<=Other = )calculate", ["other.py"], use_perl_regexp=True
    )
    assert "No matches" == tools.code_search("")
    with pytest.raises(ValueError):
        tools.file_read("../outside.py")
    with pytest.raises(ValueError):
        tools.file_read("absent.py")


def test_comment_batch_is_atomic_and_coverage_is_per_file(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x = 0\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    collector = FindingCollector(frozen, ("a.py", "b.py"), "snapshot")
    comment = {
        "path": "a.py",
        "content": "bug",
        "existing_code": "x = 0",
        "severity": "high",
        "category": "bug",
    }
    with pytest.raises(ValueError, match="outside"):
        collector.code_comment([comment, {**comment, "path": "context.py"}])
    assert not collector.candidates
    collector.code_comment([comment, {**comment, "path": "b.py"}])
    assert len(collector.candidates) == 2
    collector.code_comment([comment, comment])
    assert len(collector.candidates) == 4
    assert len({item.candidate_id for item in collector.candidates}) == 4
    assert [item.path for item in collector.candidates] == ["a.py", "b.py", "a.py", "a.py"]
    collector.task_done("DONE")
    assert not collector.covered
    collector.task_done("DONE", ["a.py", "b.py"])
    assert collector.covered == {"a.py", "b.py"}
    plan = ToolRegistry(ContextTools(frozen), stage="plan")
    assert "code_comment" not in plan.definitions
    with pytest.raises(ValueError, match="unavailable"):
        plan.invoke("code_comment", {"comments": [comment]})
