import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_reflection import runner_with_response

from code_review_agent.application.benchmark_service import BenchmarkService
from code_review_agent.config import Settings
from code_review_agent.contracts import Side, UsageRecord
from code_review_agent.evaluation.dataset import (
    Reference,
    audit_rows,
    load_local_samples,
    sample_repositories,
    select_run_scope,
)
from code_review_agent.evaluation.judge import Judgment, SemanticJudge
from code_review_agent.evaluation.matching import Generated, match_comments, maximum_matching
from code_review_agent.evaluation.metrics import (
    Counts,
    aggregate,
    cost_metrics,
)
from code_review_agent.evaluation.statistics import paired_bootstrap
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.runtime.stages import StageRunner
from code_review_agent.sessions.store import SessionStore


def row(repo, language, label=1, start=1, end=1):
    return {
        "project_main_language": language,
        "pr_url": f"https://github.com/{repo}/pull/1",
        "pr_source_commit": "a" * 40,
        "pr_target_commit": "b" * 40,
        "note": "division by zero",
        "path": "a.py",
        "side": "right",
        "category": "Code Defect",
        "label": label,
        "from_line": start,
        "to_line": end,
    }


def test_dataset_schema_label_audit_and_stratified_reproducibility():
    rows = [
        row(f"org/{language}{i}", language, i % 2)
        for language in ("Python", "Go")
        for i in range(3)
    ]
    rows.append(row("org/Python0", "Python", 7, 9, 2))
    prs, audit = audit_rows(rows)
    selected = sample_repositories(prs, repo_count=2)
    assert selected == sample_repositories(prs, repo_count=2)
    assert not set(selected["selected_repos"]) & set(selected["development_repos"])
    assert audit["counts"]["label_None"] == 1 and len(audit["issues"]) == 1
    assert prs[0].base_sha == "a" * 40 and prs[0].head_sha == "b" * 40
    assert any(ref.label == 0 for pr in prs for ref in pr.references)
    with pytest.raises(ValueError, match="schema"):
        audit_rows([{"label": 1}])


def test_maximum_matching_order_and_competing_candidates():
    edges = {"r1": {"g1", "g2"}, "r2": {"g1"}}
    assert len(maximum_matching(edges)) == 2
    assert maximum_matching(edges) == maximum_matching(dict(reversed(list(edges.items()))))


@pytest.mark.asyncio
async def test_matching_unknown_judge_is_not_no_match(tmp_path: Path):
    class Judge:
        async def compare(self, first, second, version):
            return Judgment("undecided" if first == "unknown" else "match", "test")

    refs = [
        Reference("r1", "a.py", Side.RIGHT, 1, 2, "known", "bug", 1),
        Reference("r2", "a.py", Side.RIGHT, 1, 2, "unknown", "bug", 1),
        Reference("negative", "a.py", Side.RIGHT, 1, 2, "known", "bug", 0),
    ]
    gens = [
        Generated("g1", "a.py", Side.RIGHT, 1, 1, "x"),
        Generated("g2", "a.py", Side.RIGHT, 2, 2, "x"),
    ]
    score = await match_comments(refs, gens, Judge(), "version")
    assert score["matched"] == 1 and score["matched_upper"] == 2
    assert score["references"] == 2 and score["undecided"] == 2
    missing = await match_comments(
        refs, [Generated("g", "a.py", Side.RIGHT, None, None, "x")], Judge(), "version"
    )
    assert missing["matched"] == 0


def test_hand_calculated_metrics_failed_zero_unknown_and_cost():
    values = aggregate({"repo-a": Counts(2, 4, 5, 2, 1), "repo-b": Counts(0, 0, 1, 1, 0)})
    assert values["micro"]["precision"] == 0.5
    assert values["micro"]["recall"] == pytest.approx(1 / 3)
    assert values["micro"]["f1"] == pytest.approx(0.4)
    assert values["micro"]["completion_rate"] == pytest.approx(1 / 3)
    assert Counts(0, 0, 2).metrics()["precision"] is None
    assert Counts().metrics()["recall"] is None
    cost = cost_metrics(
        [UsageRecord("review", 100, 20, 50), UsageRecord("judge", 10, 5)], [1, 9, 3], 2
    )
    assert cost["review_tokens"] == 120 and cost["tokens_per_hit"] == 60
    assert cost["pr_median_seconds"] == 3 and cost["pr_p95_seconds"] == 9
    incomplete = cost_metrics([UsageRecord("review", None, None)], [], 0)
    assert incomplete["review_tokens"] is None and incomplete["tokens_per_hit"] is None


def test_paired_bootstrap_is_reproducible_and_repo_clustered():
    baseline = {"a": Counts(1, 2, 2), "b": Counts(0, 2, 2)}
    full = {"a": Counts(2, 2, 2), "b": Counts(1, 2, 2)}
    value = paired_bootstrap(baseline, full)
    assert value == paired_bootstrap(baseline, full)
    assert value["bootstrap_samples"] == 2000 and value["percentage_point_change"] == 50
    assert value["difference_ci95"] == [0.5, 0.5]


@pytest.mark.asyncio
async def test_judge_is_strict_cached_and_mock_cannot_be_formal(tmp_path: Path):
    store = SessionStore(ProjectPaths(tmp_path), "bench", namespace="benchmarks")
    with pytest.raises(ValueError, match="judge"):
        SemanticJudge(StageRunner(Settings()), store)
    runner = runner_with_response('{"decision":"match","reason":"same concern"}', [])
    runner.settings = replace(runner.settings, judge=runner.settings.reviewer)
    judge = SemanticJudge(runner, store)
    first = await judge.compare("division zero", "除零", "sha")
    second = await judge.compare("division zero", "除零", "sha")
    assert first.decision == "match" and second.cache_hit
    assert len(runner.usage) == 1
    broken = runner_with_response("yes similar", [])
    broken.settings = replace(broken.settings, judge=broken.settings.reviewer)
    undecided = await SemanticJudge(broken, store).compare("new", "texts", "sha")
    assert undecided.decision == "undecided"


def test_offline_prepare_and_explicit_selection(tmp_path: Path):
    rows = [row(f"org/Python{i}", "Python") for i in range(3)]
    (tmp_path / "mirror.json").write_text(json.dumps(rows))
    paths = ProjectPaths(tmp_path)
    service = BenchmarkService(paths, Settings(), "offline")
    payload = service.prepare(repo_count=1, mirror="mirror.json")
    assert payload["audit"]["flow_test_only"]
    assert service.prepare(repo_count=1, mirror="mirror.json") == payload
    assert len(service.selected_prs()) == 1
    assert len(service.selected_prs("org/Python0")) == 1


def test_legacy_cached_endpoints_rebuilt_only_from_original_fields(tmp_path):
    from code_review_agent.evaluation.dataset import canonical_dataset, encode_pr

    records = [row("org/one", "Python")]
    prs, audit = audit_rows(records)
    encoded = encode_pr(prs[0])
    encoded["base_sha"], encoded["head_sha"] = encoded["head_sha"], encoded["base_sha"]
    previous = {
        "schema_version": 1,
        "revision": "fixed",
        "prs": [encoded],
        "selection": sample_repositories(prs, repo_count=1),
        "audit": audit,
    }
    fixed = canonical_dataset(previous)
    assert fixed["prs"][0]["base_sha"] == records[0]["pr_source_commit"]
    assert fixed["prs"][0]["head_sha"] == records[0]["pr_target_commit"]
    assert previous["prs"][0]["base_sha"] == records[0]["pr_target_commit"]
    assert canonical_dataset(fixed) == fixed


def local_samples(directory: Path) -> None:
    directory.mkdir()
    record = row("org/one", "Python")
    pr = {
        "githubPrUrl": record["pr_url"],
        "source_commit": record["pr_source_commit"],
        "target_commit": record["pr_target_commit"],
        "project_main_language": record["project_main_language"],
        "comments": [record],
    }
    for filename in ("positive_samples.json", "negative_samples.json"):
        (directory / filename).write_text(json.dumps([pr]))


def test_local_aacr_read_only_source_labels_and_resume_identity(tmp_path: Path, monkeypatch):
    import sys

    # 标注位于项目外，只读转换；准备不依赖 Hugging Face 或网络。
    source = tmp_path / "reference"
    local_samples(source)
    original = {file.name: file.read_bytes() for file in source.iterdir()}
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setitem(sys.modules, "datasets", None)
    service = BenchmarkService(ProjectPaths(project), Settings(), "local")
    data = service.prepare(repo_count=1, dataset_dir=str(source))
    assert data["revision"].startswith("local:")
    assert not data["audit"]["flow_test_only"]
    assert data["audit"]["source_kind"] == "aacr-local-json"
    assert data["audit"]["rows"] == 2 and data["audit"]["prs"] == 1
    pr = service.selected_prs()[0]
    assert pr.base_sha == "a" * 40 and pr.head_sha == "b" * 40
    assert [ref.label for ref in pr.references] == [1, 0]
    assert service.prepare(repo_count=1, dataset_dir=str(source)) == data
    assert original == {file.name: file.read_bytes() for file in source.iterdir()}
    file = source / "positive_samples.json"
    file.write_text(file.read_text() + "\n")
    with pytest.raises(ValueError, match="revision changed"):
        service.prepare(repo_count=1, dataset_dir=str(source))
    second = BenchmarkService(ProjectPaths(project), Settings(), "changed")
    assert second.prepare(repo_count=1, dataset_dir=str(source))["revision"] != data["revision"]


def test_local_aacr_rejects_missing_file_and_invalid_schema(tmp_path: Path):
    local_samples(tmp_path / "source")
    source = tmp_path / "source"
    (source / "negative_samples.json").unlink()
    with pytest.raises(FileNotFoundError):
        load_local_samples(source)
    (source / "negative_samples.json").write_text("{}")
    with pytest.raises(ValueError, match="list of PR"):
        load_local_samples(source)
    (source / "negative_samples.json").write_text('[{"comments": []}]')
    with pytest.raises(ValueError, match="Invalid AACR PR"):
        load_local_samples(source)


def test_benchmark_cli_defaults_to_project_dataset(tmp_path: Path, monkeypatch, capsys):
    import sys

    from code_review_agent.application.script_entry import benchmark

    project = tmp_path / "project"
    project.mkdir()
    captured = []

    def prepare(self, **kwargs):
        captured.append(kwargs)
        return {}

    async def prepare_repositories(self, **kwargs):
        return {"status": "prepared"}

    monkeypatch.setattr(BenchmarkService, "prepare", prepare)
    monkeypatch.setattr(BenchmarkService, "prepare_repositories", prepare_repositories)
    monkeypatch.setattr(sys, "argv", ["benchmark.py", "--stage", "prepare", "--format", "json"])
    assert benchmark(ProjectPaths(project)) == 0
    assert captured[0]["dataset_dir"] == str(project / "dataset")
    assert captured[0]["mirror"] is None
    assert json.loads(capsys.readouterr().out)["status"] == "prepared"


def test_bundled_dataset_works_in_relocated_project(tmp_path: Path):
    import shutil
    import subprocess
    import sys

    from code_review_agent.project_paths import project_root

    original = project_root()
    relocated = tmp_path / "standalone"
    relocated.mkdir()
    shutil.copytree(
        original / "src", relocated / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(original / "dataset", relocated / "dataset")
    # 子进程不使用 editable import 或原项目路径，依赖可由已安装 Python 提供。
    script = """
import sys, json
from pathlib import Path
root = Path.cwd()
sys.dont_write_bytecode = True
sys.path.insert(0, str(root / 'src'))
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.config import Settings
from code_review_agent.application.benchmark_service import BenchmarkService
import code_review_agent.evaluation.dataset as dataset_module
assert Path(dataset_module.__file__).is_relative_to(root)
assert not (root.parent / 'aacr-bench-main').exists()
service = BenchmarkService(ProjectPaths(root), Settings(), 'standalone')
data = service.prepare()
assert data['audit']['source_path'] == str(root / 'dataset')
assert not data['audit']['flow_test_only']
print(json.dumps({'rows': data['audit']['rows'], 'repos': data['audit']['repos'],
 'prs': data['audit']['prs'], 'selected': data['selection']['selected_pr_count']}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=relocated,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"rows": 2145, "repos": 50, "prs": 200, "selected": 35}


def test_runtime_paths_do_not_create_unused_huggingface_cache(tmp_path: Path):
    import os

    ProjectPaths(tmp_path).configure_environment()
    assert (tmp_path / ".cache/tmp").is_dir()
    assert not (tmp_path / ".cache/huggingface").exists()
    for name in ("matplotlib", "pycache", "xdg"):
        assert not (tmp_path / ".cache" / name).exists()
    # 仍定向 SDK 的可选缓存，防止第三方库默认写到项目外。
    assert os.environ["HF_HOME"] == str(tmp_path / ".cache/huggingface")


def test_explicit_repository_scope_skips_sampling_and_freezes_limit(tmp_path: Path, monkeypatch):
    from code_review_agent.evaluation import dataset

    records = [row("org/one", "Python"), row("org/one", "Python"), row("org/two", "Go")]
    records[1]["pr_url"] = "https://github.com/org/one/pull/2"
    (tmp_path / "mirror.json").write_text(json.dumps(records))

    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit repository selection must not sample ten repositories")

    monkeypatch.setattr(dataset, "sample_repositories", forbidden)
    service = BenchmarkService(ProjectPaths(tmp_path), Settings(), "explicit")
    data = service.prepare(mirror="mirror.json", repo="org/one", limit=1)
    selection = data["selection"]
    assert selection["mode"] == "explicit_repository"
    assert selection["parameters"] == {"repo": "org/one", "limit": 1}
    assert selection["selected_repos"] == ["org/one"]
    assert selection["selected_repo_count"] == selection["selected_pr_count"] == 1
    assert selection["final_prs"] == [records[0]["pr_url"]]
    assert "seed" not in selection and "development_repos" not in selection
    assert service.prepare(mirror="mirror.json", repo="org/one", limit=1, repo_count=50) == data
    plan = service._plan(None, None, 1)
    assert plan["selected_prs"] == selection["final_prs"]
    assert plan["repo"] == "org/one" and plan["limit"] == 1
    with pytest.raises(ValueError, match="selection changed"):
        service.prepare(mirror="mirror.json", repo="org/one")
    all_service = BenchmarkService(ProjectPaths(tmp_path), Settings(), "all-explicit")
    assert (
        all_service.prepare(mirror="mirror.json", repo="org/one")["selection"]["selected_pr_count"]
        == 2
    )
    unknown = BenchmarkService(ProjectPaths(tmp_path), Settings(), "unknown")
    with pytest.raises(ValueError, match="No selected PRs"):
        unknown.prepare(mirror="mirror.json", repo="org/missing")
    assert not (unknown.store.root / "dataset.json").exists()


def test_sampled_scope_reports_actual_range_after_global_limit():
    records = [row("org/one", "Python"), row("org/two", "Go")]
    prs, _ = audit_rows(records)
    scope = select_run_scope(prs, repo_count=2, seed=42, limit=1)
    assert scope["mode"] == "sampled_repositories"
    assert scope["parameters"] == {"repo_count": 2, "seed": 42, "limit": 1}
    assert scope["selected_repo_count"] == scope["selected_pr_count"] == 1
    assert scope["selected_repos"] == [prs[0].repo]
