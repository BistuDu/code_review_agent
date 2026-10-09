"""AACR 数据审计、PR 聚合及仓库隔离抽样。标签仅在评测层使用。"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from ..contracts import JsonObject, JsonValue, Side, stable_digest
from ..inputs.snapshots import safe_relative_path
from ..project_paths import ProjectPaths
from ..sessions.serialization import json_object, objects
from ..sessions.store import SessionStore

DATASET_NAME = "Alibaba-Aone/aacr-bench"


def load_local_samples(directory: str | Path) -> tuple[list[JsonObject], JsonObject]:
    """只读 AACR 原始正/负样本目录，以文件标签和内容指纹建立独立数据身份。"""
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("AACR dataset path must be a directory")
    rows: list[JsonObject] = []
    files: list[JsonValue] = []
    for filename, label in (("positive_samples.json", 1), ("negative_samples.json", 0)):
        file = root / filename
        content = file.read_bytes()
        records = json.loads(content)
        if not isinstance(records, list) or any(not isinstance(pr, dict) for pr in records):
            raise ValueError(f"AACR {filename} must contain a list of PR objects")
        for ordinal, pr in enumerate(records):
            fields = ("githubPrUrl", "source_commit", "target_commit", "project_main_language")
            comments = pr.get("comments")
            if any(not isinstance(pr.get(key), str) for key in fields) or not isinstance(
                comments, list
            ):
                raise ValueError(f"Invalid AACR PR in {filename} at entry {ordinal}")
            for comment in comments:
                if not isinstance(comment, dict):
                    raise ValueError(f"Invalid AACR comment in {filename} at entry {ordinal}")
                rows.append(
                    json_object(
                        {
                            **comment,
                            "project_main_language": pr["project_main_language"],
                            "pr_url": pr["githubPrUrl"],
                            "pr_source_commit": pr["source_commit"],
                            "pr_target_commit": pr["target_commit"],
                            "label": label,
                        }
                    )
                )
        files.append(
            {
                "path": str(file),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )
    if not rows:
        raise ValueError("AACR local dataset contains no comments")
    revision = "local:" + stable_digest(
        [[Path(str(file["path"])).name, file["sha256"]] for file in objects(files)]
    )
    return rows, {
        "revision": revision,
        "raw_files": files,
        "source_kind": "aacr-local-json",
        "source_path": str(root),
    }


@dataclass(frozen=True)
class Reference:
    reference_id: str
    path: str
    side: Side
    start_line: int | None
    end_line: int | None
    content: str
    category: str
    label: int | None
    eligibility: str = "valid"
    raw: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class PullRequest:
    pr_url: str
    repo: str
    language: str
    base_sha: str
    head_sha: str
    references: tuple[Reference, ...]


def audit_rows(rows: list[JsonObject]) -> tuple[list[PullRequest], JsonObject]:
    groups: dict[str, list[Reference]] = defaultdict(list)
    metadata: dict[str, tuple[str, str, str, str]] = {}
    counts: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    problems: list[JsonValue] = []
    for ordinal, row in enumerate(rows):
        required = (
            "project_main_language",
            "pr_url",
            "pr_source_commit",
            "pr_target_commit",
            "note",
            "path",
            "side",
            "category",
            "label",
            "from_line",
            "to_line",
        )
        if any(key not in row for key in required):
            raise ValueError(f"AACR schema mismatch at row {ordinal}")
        for key in (
            "project_main_language",
            "pr_url",
            "pr_source_commit",
            "pr_target_commit",
            "note",
            "path",
            "category",
        ):
            if not isinstance(row[key], str):
                raise ValueError(f"AACR {key} must be a string at row {ordinal}")
        url, language = str(row["pr_url"]), str(row["project_main_language"])
        match = re.fullmatch(r"https://github.com/([\w.-]+/[\w.-]+)/pull/(\d+)", url)
        if not match:
            raise ValueError(f"Unsupported PR URL: {url}")
        # AACR 的 target 是被评审的选定版本，source 是起始版本；不是 GitHub 分支名语义。
        base, head = str(row["pr_source_commit"]), str(row["pr_target_commit"])
        if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, base)):
            raise ValueError("Dataset must contain full commit hashes")
        item_metadata = match.group(1), language, base, head
        if url in metadata and metadata[url] != item_metadata:
            raise ValueError("Conflicting language/version metadata within PR")
        metadata[url] = item_metadata
        label_raw = row["label"]
        label = label_raw if type(label_raw) is int and label_raw in {0, 1} else None
        counts[f"label_{label}"] += 1
        side_raw = row["side"]
        eligibility = "valid"
        side = Side.LEFT if side_raw == "left" else Side.RIGHT
        if side_raw not in {"left", "right"}:
            eligibility = "invalid_side"
        start, end = row["from_line"], row["to_line"]
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            eligibility = "invalid_interval"
            start = end = None
        path = safe_relative_path(str(row["path"]))
        content = str(row["note"])
        if not content.strip():
            eligibility = "empty_comment"
        signature = stable_digest(
            [url, path, str(side_raw), row["from_line"], row["to_line"], content, label]
        )
        if seen[signature]:
            counts["exact_duplicate_extra_rows"] += 1
        seen[signature] += 1
        identifier = stable_digest([signature, seen[signature]])
        reference = Reference(
            identifier,
            path,
            side,
            start,
            end,
            content,
            str(row["category"]),
            label,
            eligibility,
            dict(row),
        )
        groups[url].append(reference)
        categories[reference.category] += 1
        counts[f"side_{side_raw}"] += 1
        if eligibility != "valid" or label is None:
            problems.append(
                {
                    "row": ordinal,
                    "reference_id": identifier,
                    "eligibility": eligibility,
                    "label": label,
                }
            )
    prs = [PullRequest(url, *metadata[url], tuple(groups[url])) for url in sorted(groups)]
    audit: JsonObject = {
        "rows": len(rows),
        "repos": len({pr.repo for pr in prs}),
        "prs": len(prs),
        "counts": dict(counts),
        "categories": dict(categories),
        "issues": problems,
        "languages": dict(Counter(pr.language for pr in prs)),
        "raw_rows_digest": stable_digest(cast(list[JsonValue], rows)),
    }
    return prs, audit


def sample_repositories(
    prs: list[PullRequest], *, seed: int = 42, repo_count: int = 10
) -> JsonObject:
    by_language: dict[str, set[str]] = defaultdict(set)
    for pr in prs:
        by_language[pr.language].add(pr.repo)
    all_repos = sorted({pr.repo for pr in prs})
    if not 1 <= repo_count <= len(all_repos):
        raise ValueError("repo_count must be within the available repository count")
    rng = random.Random(seed)
    stratified = [rng.choice(sorted(by_language[language])) for language in sorted(by_language)]
    if repo_count == len(stratified):
        selected = stratified  # 保留现有 seed=42 正式清单。
    elif repo_count < len(stratified):
        selected = rng.sample(stratified, repo_count)
    else:
        selected = stratified + rng.sample(
            sorted(set(all_repos) - set(stratified)), repo_count - len(stratified)
        )
    development = [
        rng.choice(sorted(by_language[language] - set(selected)))
        for language in sorted(by_language)
        if by_language[language] - set(selected)
    ]
    final_prs = [pr.pr_url for pr in prs if pr.repo in selected]
    dev_prs = [next(pr.pr_url for pr in prs if pr.repo == repo) for repo in development]
    final_rows = sum(len(pr.references) for pr in prs if pr.repo in selected)
    return json_object(
        {
            "seed": seed,
            "selected_repos": sorted(selected),
            "development_repos": sorted(development),
            "final_prs": final_prs,
            "development_prs": dev_prs,
            "repo_fraction": len(selected) / len({pr.repo for pr in prs}),
            "pr_fraction": len(final_prs) / len(prs),
            "comment_fraction": final_rows / sum(len(pr.references) for pr in prs),
            "selected_pr_count": len(final_prs),
            "selected_comment_count": final_rows,
        }
    )


def encode_pr(pr: PullRequest) -> JsonObject:
    return json_object(asdict(pr))


def select_run_scope(
    prs: list[PullRequest],
    *,
    repo: str | None = None,
    limit: int | None = None,
    seed: int = 42,
    repo_count: int = 10,
) -> JsonObject:
    """只保存本次实际入选范围；明确指定仓库时不执行默认抽样。"""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    ordered = sorted(prs, key=lambda pr: pr.pr_url)
    if repo:
        selected = [pr for pr in ordered if pr.repo == repo]
        parameters: JsonObject = {"repo": repo, "limit": limit}
        mode = "explicit_repository"
    else:
        sample = sample_repositories(ordered, seed=seed, repo_count=repo_count)
        sampled_repositories = set(cast(list[str], sample["selected_repos"]))
        selected = [pr for pr in ordered if pr.repo in sampled_repositories]
        parameters = {"repo_count": repo_count, "seed": seed, "limit": limit}
        mode = "sampled_repositories"
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError(f"No selected PRs for repository: {repo}" if repo else "No selected PRs")
    rows = sum(len(pr.references) for pr in selected)
    repositories = sorted({pr.repo for pr in selected})
    return json_object(
        {
            "mode": mode,
            "parameters": parameters,
            "selected_repos": repositories,
            "final_prs": [pr.pr_url for pr in selected],
            "selected_repo_count": len(repositories),
            "selected_pr_count": len(selected),
            "selected_comment_count": rows,
            "repo_fraction": len(repositories) / len({pr.repo for pr in prs}),
            "pr_fraction": len(selected) / len(prs),
            "comment_fraction": rows / sum(len(pr.references) for pr in prs),
        }
    )


def decode_pr(value: JsonObject) -> PullRequest:
    references: list[Reference] = []
    for reference in objects(value["references"]):
        plain = dict(reference)
        plain["side"] = Side(str(plain["side"]))
        references.append(Reference(**cast(dict[str, object], plain)))  # type: ignore[arg-type]
    return PullRequest(
        str(value["pr_url"]),
        str(value["repo"]),
        str(value["language"]),
        str(value["base_sha"]),
        str(value["head_sha"]),
        tuple(references),
    )


def prepare_dataset(
    paths: ProjectPaths,
    store: SessionStore,
    *,
    seed: int = 42,
    repo_count: int = 10,
    mirror: str | None = None,
    dataset_dir: str | None = None,
    repo: str | None = None,
    limit: int | None = None,
) -> JsonObject:
    paths.configure_environment()
    provenance: JsonObject = {}
    if mirror and dataset_dir:
        raise ValueError("Choose either dataset_dir or a flow-test mirror")
    if mirror:
        rows = objects(cast(JsonValue, json.loads(paths.writable(mirror).read_text())))
        resolved = "offline:" + stable_digest(cast(list[JsonValue], rows))
    else:
        rows, provenance = load_local_samples(dataset_dir or paths.root / "dataset")
        resolved = str(provenance["revision"])
    prs, audit = audit_rows(rows)
    selection = select_run_scope(prs, repo=repo, limit=limit, seed=seed, repo_count=repo_count)
    audit.update(
        {
            "dataset": DATASET_NAME,
            "revision": resolved,
            "raw_files": [],
            "flow_test_only": mirror is not None,
            **provenance,
        }
    )
    payload: JsonObject = {
        "schema_version": 1,
        "dataset": DATASET_NAME,
        "revision": resolved,
        "audit": audit,
        "selection": selection,
        "prs": [encode_pr(pr) for pr in prs],
    }
    payload = canonical_dataset(payload)
    store.write("dataset.json", payload)
    return payload


def canonical_dataset(value: JsonObject) -> JsonObject:
    """旧清单只复用原始行，重新审计；历史 base/head 不能作为版本真值。"""
    raw_rows = []
    for pr in objects(value["prs"]):
        for reference in objects(pr["references"]):
            raw = reference.get("raw")
            if not isinstance(raw, dict):
                raise ValueError("Cached dataset lacks original AACR fields; prepare it again")
            raw_rows.append(raw)
    prs, audit = audit_rows(raw_rows)
    previous = value.get("audit")
    if isinstance(previous, dict):
        for key in (
            "dataset",
            "revision",
            "raw_files",
            "flow_test_only",
            "source_kind",
            "source_path",
        ):
            if key in previous:
                audit[key] = previous[key]
    selection = value["selection"]
    result = {
        **value,
        "schema_version": 2,
        "endpoint_mapping": "pr_source_commit-to-pr_target_commit",
        "audit": audit,
        "prs": [encode_pr(pr) for pr in prs],
        "selection": selection,
    }
    return json_object(result)
