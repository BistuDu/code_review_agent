"""调用完整生产评审，统一位置未知与单次发现身份。"""

from __future__ import annotations

from ...application.review_service import review
from ...config import Settings
from ...contracts import JsonObject, stable_digest
from ...output.render import output_json
from ...project_paths import ProjectPaths
from ...runtime.model_factory import create_model
from ...runtime.stages import ModelFactory
from ...sessions.serialization import json_object, objects
from ..task import ReviewTask, comparison_settings


async def run_project(
    paths: ProjectPaths,
    settings: Settings,
    task: ReviewTask,
    job_id: str,
    factory: ModelFactory = create_model,
) -> JsonObject:
    result = await review(
        paths,
        comparison_settings(settings, task.path, task.source, task.target),
        task.path,
        from_ref=task.source,
        to_ref=task.target,
        range_strategy="direct",
        model_factory=factory,
    )
    value = output_json(
        result,
        settings.public_json(),
        (settings.reviewer.api_key, settings.reflection.api_key, settings.judge.api_key),
    )
    comments = []
    for index, row in enumerate(objects(value["comments"])):
        row["candidate_id"] = stable_digest([job_id, index])
        if type(row.get("start_line")) is not int or int(str(row["start_line"])) < 1:
            row["start_line"] = row["end_line"] = row["side"] = None
        comments.append(row)
    value["comments"] = json_object({"rows": comments})["rows"]
    return value
