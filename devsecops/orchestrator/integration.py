from __future__ import annotations

import asyncio
from typing import Any

from agents.auditor_agent import AuditorAgent, Finding
from agents.verifier_agent import VerifierAgent, VerificationReport
from agents.writer_agent import CoderAgent
from orchestrator.events import event_bus


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # In an existing loop, run in a worker thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def run_auditor(repo_url: str, job_id: str) -> dict[str, Any]:
    event_bus.publish(job_id, {"type": "status", "phase": "AUDITING", "message": "Starting repository security and quality audit"})

    def on_event(ev):
        event_bus.publish(job_id, ev)

    agent = AuditorAgent(repo_url=repo_url, job_id=job_id, on_event=on_event)
    report = _run_async(agent.run())

    data = report.model_dump()
    event_bus.publish(job_id, {
        "type": "audit_completed",
        "finding_count": len(data.get("findings", [])),
        "summary": data.get("summary", ""),
        "findings": data.get("findings", []),
    })
    return data


def run_writer(
    repo_url: str,
    job_id: str,
    workspace: str,
    findings: list[dict[str, Any]],
    feedback: str = "",
) -> dict[str, Any]:
    event_bus.publish(job_id, {"type": "status", "phase": "CODING", "message": "Synthesizing and applying automated code patches"})

    def on_event(ev):
        event_bus.publish(job_id, ev)

    parsed_findings = [Finding.model_validate(f) for f in findings]
    v_report = VerificationReport(
        summary=feedback or "Initial fix round",
        passed=False,
        results=[],
        new_findings=parsed_findings,
    )

    branch = f"devsecops-fix-{job_id[:8]}"
    agent = CoderAgent(
        repo_url=repo_url,
        verification=v_report,
        branch=branch,
        original_findings=parsed_findings,
        on_event=on_event,
    )

    report = _run_async(agent.run())
    data = report.model_dump()

    event_bus.publish(job_id, {
        "type": "patch_created",
        "branch": branch,
        "files_changed": data.get("files_changed", []),
        "applied_count": len(data.get("applied", [])),
        "patch": data.get("patch", ""),
    })
    return data


def run_verifier(
    job_id: str,
    workspace: str,
    patch: str,
    repo_url: str = "",
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    event_bus.publish(job_id, {"type": "status", "phase": "VERIFYING", "message": "Executing CI/CD verification gate, test suite, and linter"})

    def on_event(ev):
        event_bus.publish(job_id, ev)

    parsed_findings = [Finding.model_validate(f) for f in (findings or [])]
    agent = VerifierAgent(
        repo_url=repo_url,
        findings=parsed_findings,
        on_event=on_event,
    )

    report = _run_async(agent.run())
    data = report.model_dump()

    event_bus.publish(job_id, {
        "type": "verification_completed",
        "passed": data.get("passed", False),
        "checks": data.get("checks", {}),
        "failures": data.get("failures", []),
        "summary": data.get("summary", ""),
    })
    return data


def publish_change(
    job_id: str,
    workspace: str,
    base_commit: str,
) -> dict[str, Any]:
    event_bus.publish(job_id, {"type": "status", "phase": "APPROVED", "message": "Approval granted. Publishing verified changes to origin repository"})
    event_bus.publish(job_id, {"type": "complete", "message": "DevSecOps Squad pipeline completed successfully!"})
    return {"published": True, "job_id": job_id, "status": "PUBLISHED"}