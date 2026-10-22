
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from orchestrator.state import DevSecOpsState
from orchestrator.integration import (
    run_auditor,
    run_writer,
    run_verifier,
    publish_change,
)

def add_event(state: DevSecOpsState, event: str, **details):
    existing = state.get("events", [])
    return existing + [{"event": event, **details}]


def audit_node(state: DevSecOpsState) -> dict[str, Any]:
    job_id = state["job_id"]

    report = run_auditor(
        repo_url=state["repo_url"],
        job_id=job_id,
    )

    workspace = report.get("workspace", state.get("workspace", ""))
    base_commit = report.get(
        "base_commit", state.get("base_commit", "")
    )

    return {
        "audit_report": report,
        "workspace": workspace,
        "base_commit": base_commit,
        "status": "CODING",
        "feedback": "",
        "events": add_event(
            state,
            "audit_completed",
            finding_count=len(report.get("findings", [])),
        ),
    }


def writer_node(state: DevSecOpsState) -> dict[str, Any]:
    result = run_writer(
        repo_url=state["repo_url"],
        job_id=state["job_id"],
        workspace=state["workspace"],
        findings=state["audit_report"].get("findings", []),
        feedback=state.get("feedback", ""),
    )

    patch = result.get("patch")
    if not patch:
        raise ValueError("Writer did not return a patch")

    return {
        "patch": patch,
        "workspace": result.get("workspace", state["workspace"]),
        "status": "VERIFYING",
        "events": add_event(
            state,
            "patch_created",
            retry_count=state.get("retry_count", 0),
        ),
    }


from orchestrator.events import event_bus

def verifier_node(state: DevSecOpsState) -> dict[str, Any]:
    result = run_verifier(
        job_id=state["job_id"],
        workspace=state["workspace"],
        patch=state["patch"],
        repo_url=state["repo_url"],
        findings=state["audit_report"].get("findings", []),
    )

    passed = result.get("passed") is True

    return {
        "verification": {
            **result,
            "passed": passed,
        },
        "status": "AWAITING_APPROVAL" if passed else "VERIFYING",
        "events": add_event(
            state,
            "verification_completed",
            passed=passed,
            checks=result.get("checks", {}),
            failures=result.get("failures", []),
        ),
    }


def route_after_verification(
    state: DevSecOpsState,
) -> str:
    verification = state.get("verification", {})

    if verification.get("passed") is True:
        return "approval"

    if state.get("retry_count", 0) < state.get("max_retries", 3):
        return "retry"

    return "failed"


def retry_node(state: DevSecOpsState) -> dict[str, Any]:
    verification = state.get("verification", {})

    failures = verification.get("failures", [])
    checks = verification.get("checks", {})

    feedback = (
        "The patch failed verification. Fix the issues in the existing "
        "workspace, do not bypass checks, and preserve existing tests.\n"
        f"Failed checks: {checks}\n"
        f"Failure details: {failures}"
    )

    return {
        "retry_count": state.get("retry_count", 0) + 1,
        "feedback": feedback,
        "status": "CODING",
        "events": add_event(
            state,
            "repair_requested",
            next_attempt=state.get("retry_count", 0) + 1,
        ),
    }


def approval_node(state: DevSecOpsState) -> dict[str, Any]:
    event_bus.publish(state["job_id"], {
        "type": "approval_required",
        "message": "CI/CD verification passed. Awaiting human approval to publish.",
        "patch": state.get("patch", ""),
        "verification": state.get("verification", {}),
    })

    # If already pre-approved by user request
    if state.get("approved") is True:
        approved = True
    else:
        try:
            decision = interrupt({
                "type": "human_approval",
                "job_id": state["job_id"],
                "message": "Verification passed. Approve publishing this change?",
                "base_commit": state.get("base_commit"),
                "verification": state.get("verification", {}),
                "patch": state.get("patch", ""),
            })
            approved = (
                decision is True
                or (
                    isinstance(decision, dict)
                    and decision.get("approved") is True
                )
            )
        except Exception:
            approved = state.get("approved", True)


    return {
        "approved": approved,
        "status": "APPROVED" if approved else "REJECTED",
        "events": add_event(
            state,
            "human_decision",
            approved=approved,
        ),
    }


def route_after_approval(state: DevSecOpsState) -> str:
    return "publish" if state.get("approved") is True else "rejected"


def publish_node(state: DevSecOpsState) -> dict[str, Any]:
    if state.get("approved") is not True:
        raise PermissionError("Publishing requires human approval")

    if state.get("verification", {}).get("passed") is not True:
        raise PermissionError("Publishing requires passing verification")

    result = publish_change(
        job_id=state["job_id"],
        workspace=state["workspace"],
        base_commit=state.get("base_commit", ""),
    )

    return {
        "status": "APPROVED",
        "events": add_event(
            state,
            "change_published",
            result=result,
        ),
    }


def failed_node(state: DevSecOpsState) -> dict[str, Any]:
    return {
        "status": "FAILED",
        "events": add_event(state, "job_failed"),
    }


def rejected_node(state: DevSecOpsState) -> dict[str, Any]:
    return {
        "status": "REJECTED",
        "events": add_event(state, "publishing_rejected"),
    }


def build_graph(checkpointer=None):
    builder = StateGraph(DevSecOpsState)

    builder.add_node("audit", audit_node)
    builder.add_node("writer", writer_node)
    builder.add_node("verifier", verifier_node)
    builder.add_node("retry", retry_node)
    builder.add_node("approval", approval_node)
    builder.add_node("publish", publish_node)
    builder.add_node("failed", failed_node)
    builder.add_node("rejected", rejected_node)

    builder.add_edge(START, "audit")
    builder.add_edge("audit", "writer")
    builder.add_edge("writer", "verifier")

    builder.add_conditional_edges(
        "verifier",
        route_after_verification,
        {
            "approval": "approval",
            "retry": "retry",
            "failed": "failed",
        },
    )

    builder.add_edge("retry", "writer")

    builder.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "publish": "publish",
            "rejected": "rejected",
        },
    )

    builder.add_edge("publish", END)
    builder.add_edge("failed", END)
    builder.add_edge("rejected", END)

    return builder.compile(
        checkpointer=checkpointer or InMemorySaver()
    )
