from typing import Any, Literal, TypedDict


class DevSecOpsState(TypedDict, total=False):
    job_id: str
    repo_url: str
    workspace: str
    base_commit: str

    audit_report: dict[str, Any]
    patch: str
    verification: dict[str, Any]
    feedback: str

    retry_count: int
    max_retries: int
    approved: bool | None
    status: Literal[
        "PENDING",
        "AUDITING",
        "CODING",
        "VERIFYING",
        "AWAITING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "FAILED",
    ]

    events: list[dict[str, Any]]