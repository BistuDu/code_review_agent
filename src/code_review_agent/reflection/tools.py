"""批量复核的结果工具声明，analysis 在 comment_ids 之前。"""

from ..contracts import JsonObject

FILTER_TOOLS: tuple[JsonObject, ...] = (
    {
        "type": "function",
        "function": {
            "name": "report_incorrect_comments",
            "description": "Report review comments that this diff proves to be factually wrong: either the code they target is absent from the diff, "
            "or one diff line literally contradicts their central claim. For every id listed you must be able to name that line. "
            "Do not use this for comments you merely find unconvincing, unverifiable, or low-value, nor for comments about memory safety, "
            "concurrency, linkage consistency, unused parameters, or behavioral changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "analysis": {
                        "type": "array",
                        "description": "Work through every candidate comment BEFORE deciding. One entry per candidate: its id, "
                        "whether its subject hits the protected-subject veto (Step 1) or the value veto (Step 2), "
                        "the exact diff line that refutes it if any, and your final call. "
                        "Only ids you conclude here as removable may appear in comment_ids.",
                        "items": {"type": "string"},
                    },
                    "comment_ids": {
                        "type": "array",
                        "description": 'IDs concluded removable in analysis, e.g. ["c-0", "c-2"]. Must not be empty.',
                        "items": {"type": "string"},
                    },
                },
                "required": ["analysis", "comment_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_all_comments",
            "description": "Keep every review comment. Call this whenever no comment clears the removal bar — including when comments look doubtful, "
            "cannot be verified from the diff alone, or seem minor. This is the expected outcome for most files.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
)
