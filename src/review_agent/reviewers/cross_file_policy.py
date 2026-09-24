import re

from review_agent.models.diff import DiffHunk

CONFIG_CHANGE_SUFFIXES = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")


def needs_cross_file_review(hunk: DiffHunk) -> bool:
    path = hunk.file_path.lower()
    code = f"{hunk.added_code}\n{hunk.removed_code}"
    return (
        hunk.change_type in {"deleted", "renamed"}
        or path.endswith(CONFIG_CHANGE_SUFFIXES)
        or bool(re.search(r"(^|\n)\s*(def|class)\s+\w+.*:", code))
        or "import " in code
    )


def cross_file_reason(hunk: DiffHunk) -> str:
    if hunk.change_type in {"deleted", "renamed"}:
        return "deleted or renamed code needs reference and compatibility context"
    if needs_cross_file_review(hunk):
        return "signature, import, config, or public API shaped change"
    return "internal code change needs local function and related test context"


def any_needs_cross_file_review(hunks: list[DiffHunk]) -> bool:
    return any(needs_cross_file_review(hunk) for hunk in hunks)
