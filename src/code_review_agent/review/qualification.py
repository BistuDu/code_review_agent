"""最终评论和扫描摘要共用资格，防止过滤后的问题重新进入摘要。"""

from ..contracts import ReviewResult


def eligible_ids(result: ReviewResult) -> set[str]:
    collected = {item.candidate_id for item in result.candidates}
    rejected = {item.candidate_id for item in result.reflections if item.decision == "reject"}
    return collected - rejected - set(result.suppressed)
