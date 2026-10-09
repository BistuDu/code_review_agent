"""模型只重新提取代码片段，行号仍由文本匹配计算。"""

from __future__ import annotations

from ..contracts import FindingCandidate, ReviewInput
from ..inputs.selection import RESOURCE_ROOT
from ..runtime.stages import StageRunner


async def relocate(
    candidate: FindingCandidate, review_input: ReviewInput, runner: StageRunner
) -> str | None:
    files = [file for file in review_input.targets if candidate.path in {file.path, file.old_path}]
    if not files:
        return None
    file = files[0]
    system = (RESOURCE_ROOT / "prompts/re_location_task_system.md").read_text()
    template = (RESOURCE_ROOT / "prompts/re_location_task_user.md").read_text()
    user = (
        template.replace("{diff}", file.diff or file.content)
        .replace("{existing_code}", candidate.existing_code)
        .replace("{suggestion_content}", candidate.content)
    )
    result = await runner.run("location.relocation", system, user)
    if not result.completed:
        return None
    opening = result.text.find("```")
    if opening < 0:
        return None
    start = result.text.find("\n", opening + 3)
    if start < 0:
        return None
    end = result.text.find("```", start + 1)
    if end < 0:
        return None
    return result.text[start + 1 : end].strip() or None
