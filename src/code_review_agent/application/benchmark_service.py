"""本项目 / Claude Code：共同任务、受管 checkout、独立评审和可重算评分。"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import cast

from filelock import FileLock, Timeout

from ..config import Settings
from ..contracts import JsonObject, JsonValue, Side, stable_digest
from ..evaluation.config import ClaudeConfig, external_rules_identity, load_claude_config
from ..evaluation.dataset import (
    PullRequest,
    decode_pr,
    load_local_samples,
    prepare_dataset,
    select_run_scope,
)
from ..evaluation.judge import SemanticJudge
from ..evaluation.matching import Generated, match_comments
from ..evaluation.repositories import RepositoryPool
from ..evaluation.reviewers.claude import ClaudeReviewer
from ..evaluation.reviewers.project import run_project
from ..evaluation.task import build_task
from ..project_paths import ProjectPaths
from ..runtime.model_factory import create_model
from ..runtime.stages import ModelFactory, StageRunner
from ..sessions.serialization import json_object, objects, strings
from ..sessions.store import SessionStore


def implementation_identity(paths: ProjectPaths) -> str:
    files = sorted(paths.writable("src/code_review_agent").rglob("*.py")) + sorted(
        paths.writable("src/code_review_agent/resources").rglob("*")
    )
    return stable_digest(
        [
            [str(file.relative_to(paths.root)), hashlib.sha256(file.read_bytes()).hexdigest()]
            for file in files
            if file.is_file()
        ]
    )


class BenchmarkService:
    def __init__(
        self,
        paths: ProjectPaths,
        settings: Settings,
        run_id: str,
        model_factory: ModelFactory = create_model,
        *,
        repository_concurrency: int = 1,
        timeout: float | None = None,
        claude_config: ClaudeConfig | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        timeout = (settings.review_timeout_seconds or 1800) if timeout is None else timeout
        if repository_concurrency < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid benchmark concurrency or timeout")
        settings = replace(settings, review_timeout_seconds=timeout)
        self.paths, self.settings, self.model_factory = paths, settings, model_factory
        self.concurrency, self.timeout = repository_concurrency, timeout
        self.progress = progress
        self.claude_config = claude_config or load_claude_config(paths, settings)
        self.store = SessionStore(
            paths,
            run_id,
            (
                settings.reviewer.api_key,
                settings.reflection.api_key,
                settings.judge.api_key,
                self.claude_config.api_key,
            ),
        )
        self.runner = StageRunner(settings, model_factory=model_factory)

    def _notify(self, message: str) -> None:
        """进度只发给调用方，不写入结果或改变评审上下文。"""
        if self.progress:
            self.progress(message)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        lock = FileLock(str(self.store.root / ".run.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise ValueError("This benchmark run is already active") from None
        try:
            yield
        finally:
            lock.release()

    def prepare(
        self,
        *,
        seed: int = 42,
        repo_count: int = 10,
        mirror: str | None = None,
        dataset_dir: str | None = None,
        repo: str | None = None,
        limit: int | None = None,
    ) -> JsonObject:
        if mirror and dataset_dir:
            raise ValueError("Choose either dataset_dir or a flow-test mirror")
        revision: str | None = None
        if not mirror and dataset_dir is None:
            dataset_dir = str(self.paths.root / "dataset")
        if dataset_dir:
            _, source = load_local_samples(dataset_dir)
            revision = str(source["revision"])
        if (self.store.root / "dataset.json").exists():
            data = self.dataset()
            if data["revision"] != revision and not mirror:
                raise ValueError("Dataset revision changed; use a new run_id")
            expected = select_run_scope(
                [decode_pr(row) for row in objects(data["prs"])],
                repo=repo,
                limit=limit,
                seed=seed,
                repo_count=repo_count,
            )
            if data["selection"] != expected:
                raise ValueError("Dataset selection changed; use a new run_id")
            return data
        return prepare_dataset(
            self.paths,
            self.store,
            seed=seed,
            repo_count=repo_count,
            mirror=mirror,
            dataset_dir=dataset_dir,
            repo=repo,
            limit=limit,
        )

    def dataset(self) -> JsonObject:
        value = self.store.read("dataset.json")
        if value.get("endpoint_mapping") != "pr_source_commit-to-pr_target_commit":
            raise ValueError("Legacy dataset mapping cannot drive new review; use a new run_id")
        for pr in objects(value["prs"]):
            for reference in objects(pr["references"]):
                raw = cast(JsonObject, reference["raw"])
                if (
                    raw["pr_source_commit"] != pr["base_sha"]
                    or raw["pr_target_commit"] != pr["head_sha"]
                ):
                    raise ValueError("Dataset endpoint fields disagree with original AACR rows")
        return value

    def selected_prs(self, repo: str | None = None, limit: int | None = None) -> list[PullRequest]:
        prs = [decode_pr(value) for value in objects(self.dataset()["prs"])]
        if repo:
            prs = [pr for pr in prs if pr.repo == repo]
        else:
            selection = cast(JsonObject, self.dataset()["selection"])
            chosen = set(strings(selection["final_prs"]))
            prs = [pr for pr in prs if pr.pr_url in chosen]
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            prs = prs[:limit]
        if not prs:
            raise ValueError("No selected PRs")
        return prs

    def _plan(self, repo: str | None, limit: int | None, repetitions: int) -> JsonObject:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        data = self.dataset()
        selection = cast(JsonObject, data["selection"])
        parameters = cast(JsonObject, selection.get("parameters", {}))
        if not (self.store.root / "run.json").exists():
            # API 调用可以在数据审计后指定范围；发布计划前先固定真实 selection。
            repo = repo if repo is not None else cast(str | None, parameters.get("repo"))
            limit = limit if limit is not None else cast(int | None, parameters.get("limit"))
            data["selection"] = select_run_scope(
                [decode_pr(row) for row in objects(data["prs"])],
                repo=repo,
                limit=limit,
                seed=cast(int, parameters.get("seed", 42)),
                repo_count=cast(
                    int, parameters.get("repo_count", len(strings(selection["selected_repos"])))
                ),
            )
            if data["selection"] != selection:
                self.store.write("dataset.json", data)
        identity = {
            "implementation": implementation_identity(self.paths),
            "dataset": self.dataset()["revision"],
            "dataset_digest": stable_digest(self.dataset()),
            "external_rules_identity": external_rules_identity(self.paths, self.settings),
            "review_settings": {
                key: value for key, value in self.settings.public_json().items() if key != "judge"
            },
            "claude": ClaudeReviewer(
                self.paths, self.settings, config=self.claude_config
            ).identity(),
            "repository_concurrency": self.concurrency,
            "timeout": self.timeout,
        }
        if (self.store.root / "run.json").exists():
            plan = self.store.read("run.json")
            if plan.get("schema_version") != 2 or plan.get("identity") != json_object(identity):
                raise ValueError("Implementation/configuration changed; use a new run_id")
            if (
                repo is not None
                and repo != plan.get("repo")
                or limit is not None
                and limit != plan.get("limit")
                or repetitions != plan["repetitions"]
            ):
                raise ValueError("Sample/repetition settings changed; use a new run_id")
            return plan
        prs = self.selected_prs()
        jobs = []
        for repetition in range(repetitions):
            for ordinal, pr in enumerate(prs):
                order = (
                    ("project", "claude")
                    if (ordinal + repetition) % 2 == 0
                    else ("claude", "project")
                )
                for reviewer in order:
                    jobs.append(
                        {
                            "job_id": stable_digest(
                                [pr.pr_url, pr.base_sha, pr.head_sha, repetition, reviewer]
                            ),
                            "pr_url": pr.pr_url,
                            "repo": pr.repo,
                            "reviewer": reviewer,
                            "repetition": repetition,
                            "status": "pending",
                            "attempts": [],
                        }
                    )
        plan = json_object(
            {
                "schema_version": 2,
                "identity": identity,
                "repo": repo,
                "limit": limit,
                "repetitions": repetitions,
                "selected_prs": [pr.pr_url for pr in prs],
                "jobs": jobs,
            }
        )
        self.store.write("run.json", plan)
        return plan

    async def prepare_repositories(
        self, *, repo: str | None = None, limit: int | None = None, repetitions: int = 1
    ) -> JsonObject:
        with self.exclusive():
            return await self._prepare_repositories(repo=repo, limit=limit, repetitions=repetitions)

    async def _prepare_repositories(
        self, *, repo: str | None = None, limit: int | None = None, repetitions: int = 1
    ) -> JsonObject:
        plan = self._plan(repo, limit, repetitions)
        by_url = {
            pr.pr_url: pr for pr in (decode_pr(row) for row in objects(self.dataset()["prs"]))
        }
        pool = RepositoryPool(self.paths)
        repository_rows: list[JsonValue] = []
        selected = strings(plan["selected_prs"])
        for ordinal, url in enumerate(selected, 1):
            pr = by_url[url]
            self._notify(
                f"[prepare {ordinal}/{len(selected)}] {pr.repo} PR#{url.rsplit('/', 1)[-1]}："
                f"下载/核验源码，切换 TARGET {pr.head_sha[:12]}……"
            )
            try:
                prepared = await asyncio.to_thread(pool.prepare_metadata, pr)
                repository_rows.append(
                    {
                        "repo": pr.repo,
                        **prepared.metadata(),
                        "status": "prepared",
                    }
                )
                self._notify(
                    f"[prepare {ordinal}/{len(selected)}] 完成，{prepared.preparation_seconds:.1f}s"
                )
            except (ValueError, OSError) as error:
                repository_rows.append({"repo": pr.repo, "status": "failed", "error": str(error)})
                self._notify(f"[prepare {ordinal}/{len(selected)}] 失败：{type(error).__name__}")
        plan["repositories"] = repository_rows
        self.store.write("run.json", plan)
        return plan

    async def run_reviews(
        self,
        *,
        reviewer: str = "both",
        repetitions: int = 1,
        repo: str | None = None,
        limit: int | None = None,
    ) -> JsonObject:
        with self.exclusive():
            return await self._run_reviews(
                reviewer=reviewer, repetitions=repetitions, repo=repo, limit=limit
            )

    async def _run_reviews(
        self,
        *,
        reviewer: str = "both",
        repetitions: int = 1,
        repo: str | None = None,
        limit: int | None = None,
    ) -> JsonObject:
        if reviewer not in {"project", "claude", "both"}:
            raise ValueError("Invalid reviewer")
        plan = self._plan(repo, limit, repetitions)
        prs = {pr.pr_url: pr for pr in [decode_pr(row) for row in objects(self.dataset()["prs"])]}
        jobs = objects(plan["jobs"])
        plan["jobs"] = cast(list[JsonValue], jobs)
        grouped: dict[str, list[JsonObject]] = defaultdict(list)
        for job in jobs:
            grouped[str(job["repo"])].append(job)
        semaphore = asyncio.Semaphore(self.concurrency)
        pool = RepositoryPool(self.paths)
        claude = ClaudeReviewer(self.paths, self.settings, self.timeout, self.claude_config)

        async def group(items: list[JsonObject]) -> None:
            async with semaphore:
                for job in items:
                    if (
                        reviewer != "both"
                        and job["reviewer"] != reviewer
                        or job["status"] in {"completed", "no_files"}
                    ):
                        continue
                    pr = prs[str(job["pr_url"])]
                    job["status"] = "running"
                    self.store.write("run.json", plan)
                    prepared = None
                    output: JsonObject = {"status": "failed", "comments": [], "usage": []}
                    attempt_started = time.monotonic()
                    interrupted = False
                    description = f"{job['reviewer']} {pr.repo} PR#{pr.pr_url.rsplit('/', 1)[-1]}"
                    self._notify(f"[review] {description}：准备评审工作区……")
                    try:
                        async with pool.review_checkout(pr) as prepared:
                            task = build_task(
                                self.paths, self.settings, prepared.path, pr.base_sha, pr.head_sha
                            )
                            if job.get("task_identity") not in {None, task.identity}:
                                raise ValueError("Rules/task changed; use a new run_id")
                            job["task_identity"] = task.identity
                            job["task_metadata"] = {
                                "targets": task.payload["targets"],
                                "excluded": task.payload["excluded"],
                                "rules_identity": stable_digest(task.payload["rules"]),
                            }
                            review_started = time.monotonic()
                            self._notify(f"[review] {description}：开始 Agent 评审……")
                            try:
                                if job["reviewer"] == "project":
                                    output = await run_project(
                                        self.paths,
                                        self.settings,
                                        task,
                                        str(job["job_id"]),
                                        self.model_factory,
                                        deadline=review_started + self.timeout,
                                    )
                                else:
                                    output = await claude.run(task, str(job["job_id"]), pool, pr)
                            finally:
                                output.setdefault(
                                    "review_seconds", time.monotonic() - review_started
                                )
                            output["task_identity"] = task.identity
                            self.store.write(
                                f"reviews/{job['reviewer']}/{job['job_id']}.json", output
                            )
                    except asyncio.CancelledError:
                        interrupted = True
                        output["status"] = "interrupted"
                        output["error"] = "Review cancelled"
                    except Exception as error:
                        output["status"] = "failed"
                        output["error"] = f"{type(error).__name__}: {error}"
                    job["status"] = output["status"]
                    if prepared:
                        job["repository"] = prepared.metadata()
                        if prepared.cleanup_status != "clean":
                            job["cleanup_failed"] = True
                    attempt = json_object(
                        {
                            "status": job["status"],
                            "elapsed_seconds": time.monotonic() - attempt_started,
                            "review_seconds": output.get("review_seconds"),
                            "repository": prepared.metadata() if prepared else None,
                            "usage": output.get("usage", []),
                            "error": output.get("error"),
                        }
                    )
                    cast(list[JsonValue], job["attempts"]).append(attempt)
                    output["repository"] = prepared.metadata() if prepared else None
                    self.store.write(f"reviews/{job['reviewer']}/{job['job_id']}.json", output)
                    self.store.write("run.json", plan)
                    self._notify(
                        f"[review] {description}：{job['status']}，"
                        f"{len(objects(output.get('comments', [])))} 条评论，"
                        f"本次尝试 {time.monotonic() - attempt_started:.1f}s"
                    )
                    if interrupted:
                        raise asyncio.CancelledError
                    if prepared and prepared.cleanup_status != "clean":
                        raise ValueError("Repository cleanup failed; refusing further tasks")

        tasks = [asyncio.create_task(group(items)) for items in grouped.values()]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return plan

    async def score_reviews(self, *, mock: bool = False, k: int = 1) -> JsonObject:
        with self.exclusive():
            return await self._score_reviews(mock=mock, k=k)

    async def _score_reviews(self, *, mock: bool = False, k: int = 1) -> JsonObject:
        if k < 0:
            raise ValueError("k must be nonnegative")
        plan = self.store.read("run.json")
        prs = {pr.pr_url: pr for pr in [decode_pr(row) for row in objects(self.dataset()["prs"])]}
        runner = StageRunner(self.settings, model_factory=self.model_factory)
        judge = SemanticJudge(runner, self.store, mock=mock)
        identity = {
            "review_results": {
                str(job["job_id"]): stable_digest(
                    self.store.read(f"reviews/{job['reviewer']}/{job['job_id']}.json")
                )
                for job in objects(plan["jobs"])
                if (self.store.root / f"reviews/{job['reviewer']}/{job['job_id']}.json").exists()
            },
            "run_identity": plan["identity"],
            "jobs": plan["jobs"],
            "judge": self.settings.public_json()["judge"],
            "k": k,
            "mock": mock,
            "protocol": "maximum-cardinality-v2",
        }
        score_id = stable_digest(json_object(identity))[:24] + "-" + SessionStore.new_id()[:8]
        scores = []
        self._notify("[score] 开始独立 Judge 匹配与判分……")
        for job in objects(plan["jobs"]):
            pr = prs[str(job["pr_url"])]
            name = f"reviews/{job['reviewer']}/{job['job_id']}.json"
            value = self.store.read(name) if (self.store.root / name).exists() else None
            generated = (
                [
                    Generated(
                        str(row["candidate_id"]),
                        str(row["path"]),
                        Side(str(row["side"])) if row.get("side") else None,
                        cast(int | None, row["start_line"]),
                        cast(int | None, row["end_line"]),
                        str(row["content"]),
                    )
                    for row in objects(value["comments"])
                ]
                if value
                else []
            )
            complete = job["status"] in {"completed", "no_files"}
            self._notify(
                f"[score] {job['reviewer']} {pr.repo} PR#{pr.pr_url.rsplit('/', 1)[-1]}："
                f"{len(generated)} 条生成评论"
            )
            score = await match_comments(
                list(pr.references),
                generated if complete else [],
                judge,
                stable_digest([pr.base_sha, pr.head_sha]),
                k=k,
            )
            score["generated"] = len(generated)
            if not complete:
                score["matched"] = score["matched_upper"] = 0
            score.update(
                {
                    "job_id": job["job_id"],
                    "reviewer": job["reviewer"],
                    "repetition": job["repetition"],
                    "repo": pr.repo,
                    "pr_url": pr.pr_url,
                    "completed": complete,
                    "task_metadata": job.get("task_metadata"),
                    "generated_known": complete,
                    "flow_test_only": mock
                    or cast(JsonObject, self.dataset()["audit"])["flow_test_only"],
                }
            )
            scores.append(score)
            self._notify(
                f"[score] {job['reviewer']}：命中 {score['matched']}，未判定 {score['undecided']}"
            )
        result = json_object(
            {
                "score_id": score_id,
                "identity": identity,
                "scores": scores,
                "judge_usage": [asdict(row) for row in runner.usage],
                "flow_test_only": mock
                or cast(JsonObject, self.dataset()["audit"])["flow_test_only"],
            }
        )
        self.store.write(f"scores/{score_id}.json", result)
        plan["latest_score_id"] = score_id
        self.store.write("run.json", plan)
        return result

    def report_reviews(self, *, score_id: str | None = None) -> JsonObject:
        from ..evaluation.reporting import build_report

        plan = self.store.read("run.json")
        selected_score = score_id or str(plan.get("latest_score_id", ""))
        self._notify("[report] 生成评测报告……")
        report = build_report(self.store, plan, self.store.read(f"scores/{selected_score}.json"))
        self._notify(
            f"[report] 完成：{self.paths.root / 'reports/benchmarks' / self.store.session_id / (selected_score + '.md')}"
        )
        return report
