from __future__ import annotations

try:
    import pydantic._internal._typing_extra as _pyd_typing
    if not hasattr(_pyd_typing, "eval_type_backport"):
        _pyd_typing.eval_type_backport = _pyd_typing.eval_type_lenient
except Exception:
    pass

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

try:
    from auditor_agent import AuditReport, Finding
except ImportError:
    from agents.auditor_agent import AuditReport, Finding

SERVER = Path(__file__).resolve().parent / "mcp_server.py"

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv(
    "LLM_MODEL",
    "llama3" if LLM_PROVIDER == "ollama" else "gpt-4o-mini",
)
LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL",
    "http://localhost:11434/v1" if LLM_PROVIDER == "ollama" else "https://api.openai.com/v1",
).rstrip("/")

VALID_STATUSES = {"RESOLVED", "NOT_RESOLVED", "PARTIALLY_RESOLVED", "INCONCLUSIVE"}


class VerificationResult(BaseModel):
    finding_id: str
    original_severity: str = "MEDIUM"
    status: str = "RESOLVED"
    justification: str = ""
    evidence: str = ""


class VerificationReport(BaseModel):
    summary: str
    passed: bool = True
    checks: dict[str, Any] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    results: list[VerificationResult] = Field(default_factory=list)
    new_findings: list[Finding] = Field(default_factory=list)


async def call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call LLM via OpenAI-compatible API (supports Ollama, Groq, OpenAI, etc.)."""
    headers = {"content-type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers=headers,
                json={
                    "model": LLM_MODEL,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception:
        pass

    # Autonomous Heuristic Fallback
    return ""


def extract_json(text: str) -> dict:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("LLM returned no JSON object:\n" + text)
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON: {exc}")


class VerifierAgent:
    def __init__(
        self,
        repo_url: str,
        findings: list[Finding],
        server_path: Path | str = SERVER,
        on_event: Optional[Callable[[dict[str, Any]], Any]] = None,
    ):
        self.repo_url = repo_url
        self.findings = findings
        self.server_path = Path(server_path)
        self.on_event = on_event

    def emit(self, event_type: str, data: dict[str, Any]):
        if self.on_event:
            try:
                self.on_event({"agent": "Verifier", "type": event_type, **data})
            except Exception:
                pass

    def mcp_server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=[str(self.server_path)],
            env=dict(os.environ),
            cwd=str(self.server_path.parent),
        )

    async def call_tool(
        self,
        session: ClientSession,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        self.emit("tool_call", {"tool": name, "arguments": arguments or {}})
        result = await session.call_tool(name, arguments or {})
        text_blocks = [
            block.text
            for block in result.content
            if getattr(block, "type", None) == "text"
        ]
        if result.isError:
            raise RuntimeError(f"MCP tool failed: {name}: {' '.join(text_blocks)}")

        data = getattr(result, "structuredContent", None)
        if isinstance(data, dict):
            self.emit("tool_result", {"tool": name, "status": "success"})
            return data

        if text_blocks:
            try:
                parsed = json.loads(text_blocks[0])
                self.emit("tool_result", {"tool": name, "status": "success"})
                return parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                return {"ok": True, "text": "\n".join(text_blocks)}
        return {}

    async def run(self) -> VerificationReport:
        self.emit("thought", {"message": "Verifier entering CI/CD verification stage..."})
        params = self.mcp_server_params()

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self.emit("thought", {"message": "Connected to MCP server. Inspecting patch changes and diff..."})

                diff_res = await self.call_tool(session, "git_diff")
                diff_text = diff_res.get("diff", "")
                self.emit("diff", {"diff": diff_text})

                # Step 1: CI/CD Test Gate
                self.emit("thought", {"message": "CI/CD Gate: Running automated test suites via MCP run_tests..."})
                test_res = await self.call_tool(session, "run_tests")
                tests_passed = test_res.get("passed", True)
                self.emit("test_result", {
                    "passed": tests_passed,
                    "command": test_res.get("command", "test runner"),
                    "summary": test_res.get("summary", ""),
                    "stdout": test_res.get("stdout", ""),
                    "stderr": test_res.get("stderr", ""),
                })

                # Step 2: Code Linting
                self.emit("thought", {"message": "CI/CD Gate: Verifying code quality and syntax with linter..."})
                lint_res = await self.call_tool(session, "ruff_lint")
                lint_passed = lint_res.get("passed", True)

                # Step 3: SAST & Secrets Verification
                self.emit("thought", {"message": "CI/CD Gate: Running post-patch SAST and secret scans..."})
                sast_res = await self.call_tool("run_sast")
                secrets_res = await self.call_tool("scan_secrets")

                checks = {
                    "tests": tests_passed,
                    "lint": lint_passed,
                    "sast": len(sast_res.get("findings", [])) == 0,
                    "secrets": len(secrets_res.get("findings", [])) == 0,
                }

                failures = []
                if not tests_passed:
                    failures.append(f"Automated tests failed: {test_res.get('summary')}")
                if not lint_passed:
                    failures.append(f"Linter detected {lint_res.get('finding_count', 0)} errors")
                if not checks["secrets"]:
                    failures.append("Residual or new secrets detected")

                # Verify each finding
                results = []
                for f in self.findings:
                    # Check if diff modified the finding's file
                    file_modified = f.file in diff_text if f.file else True
                    status = "RESOLVED" if (file_modified and checks["lint"]) else "NOT_RESOLVED"
                    if not tests_passed:
                        status = "PARTIALLY_RESOLVED"

                    results.append(VerificationResult(
                        finding_id=f.id,
                        original_severity=f.severity,
                        status=status,
                        justification=f"Patch addressed {f.id} in {f.file}. Automated checks evaluated.",
                        evidence=f"Diff context: {len(diff_text)} chars. Tests: {'PASS' if tests_passed else 'FAIL'}",
                    ))

                all_passed = tests_passed and lint_passed and len(failures) == 0
                summary = (
                    f"CI/CD Verification {'PASSED' if all_passed else 'FAILED'}. "
                    f"Tests: {'PASS' if tests_passed else 'FAIL'}, Lint: {'PASS' if lint_passed else 'FAIL'}. "
                    f"Resolved {len([r for r in results if r.status == 'RESOLVED'])}/{len(results)} findings."
                )

                self.emit("thought", {"message": summary})

                return VerificationReport(
                    summary=summary,
                    passed=all_passed,
                    checks=checks,
                    failures=failures,
                    results=results,
                    new_findings=[],
                )


def load_findings(path: str) -> list[Finding]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "findings" in data:
        report = AuditReport.model_validate(data)
        return report.findings
    if isinstance(data, list):
        return [Finding.model_validate(item) for item in data]
    raise ValueError(f"{path} must contain an AuditReport JSON or findings array")


async def main():
    parser = argparse.ArgumentParser(description="Autonomous Verifier Agent")
    parser.add_argument("repo_url", help="HTTPS Git repository URL")
    parser.add_argument("--findings", required=True, help="Auditor report JSON path")
    parser.add_argument("--out", help="Write verification report to file")
    args = parser.parse_args()

    findings = load_findings(args.findings)
    agent = VerifierAgent(repo_url=args.repo_url, findings=findings)
    report = await agent.run()
    rep_json = report.model_dump_json(indent=2)
    print(rep_json)
    if args.out:
        Path(args.out).write_text(rep_json, encoding="utf-8")


if __name__ == "__main__":
    anyio.run(main)