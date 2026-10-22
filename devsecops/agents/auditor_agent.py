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
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable, Optional

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

SERVER = Path(__file__).resolve().parent / "mcp_server.py"


class Finding(BaseModel):
    id: str
    file: str = ""
    line: Optional[int] = 1
    category: str = "SECURITY"
    severity: str = "MEDIUM"
    title: str = ""
    description: str = ""
    recommendation: str = ""


class AuditReport(BaseModel):
    repo_url: str = ""
    workspace: str = ""
    base_commit: str = ""
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)


class AuditorAgent:
    def __init__(
        self,
        repo_url: str = "",
        job_id: str = "",
        server_path: Path | str = SERVER,
        on_event: Optional[Callable[[dict[str, Any]], Any]] = None,
    ):
        self.repo_url = repo_url
        self.job_id = job_id
        self.server_path = Path(server_path)
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self.repo_path: Path | None = None
        self.base_commit: str = ""
        self.files: list[str] = []
        self.on_event = on_event

    def emit(self, event_type: str, data: dict[str, Any]):
        if self.on_event:
            try:
                self.on_event({"agent": "Auditor", "type": event_type, **data})
            except Exception:
                pass

    async def start(self):
        if self.session:
            return self
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(self.server_path)],
            env=dict(os.environ),
            cwd=str(self.server_path.parent),
        )
        self._stack = AsyncExitStack()
        try:
            r, w = await self._stack.enter_async_context(stdio_client(params))
            self.session = await self._stack.enter_async_context(ClientSession(r, w))
            await self.session.initialize()
            self.emit("thought", {"message": "Auditor connected to MCP tools server"})
        except Exception as e:
            if self._stack:
                await self._stack.aclose()
            self.session = self._stack = None
            raise RuntimeError(f"Auditor failed to start MCP server: {e}") from e
        return self

    async def close(self):
        stack = self._stack
        self.session = self._stack = None
        if stack:
            await stack.aclose()

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.session:
            raise RuntimeError("MCP session not connected")
        self.emit("tool_call", {"tool": name, "arguments": args or {}})
        res = await self.session.call_tool(name, args or {})
        texts = [b.text for b in res.content if getattr(b, "type", None) == "text"]
        if res.isError:
            raise RuntimeError(f"Tool {name} failed: {' '.join(texts)}")
        data = getattr(res, "structuredContent", None)
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        if isinstance(data, dict):
            self.emit("tool_result", {"tool": name, "status": "success"})
            return data
        if texts:
            try:
                parsed = json.loads(texts[0])
                self.emit("tool_result", {"tool": name, "status": "success"})
                return parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                return {"result": "\n".join(texts)}
        return {}

    async def run(self, repo_url: str | None = None) -> AuditReport:
        target_repo = repo_url or self.repo_url
        if not target_repo:
            raise ValueError("No repository URL provided to AuditorAgent")
        self.repo_url = target_repo

        await self.start()
        try:
            self.emit("thought", {"message": f"Cloning repository {target_repo} into sandbox environment..."})
            clone_res = await self.call_tool("git_clone", {"repo_url": target_repo})
            if not clone_res.get("ok"):
                raise RuntimeError(f"Clone failed: {clone_res.get('error')}")

            workspace_dir = clone_res.get("path") or clone_res.get("workspace")
            self.repo_path = Path(workspace_dir)

            self.emit("thought", {"message": f"Cloned into workspace: {workspace_dir}. Inspecting repository status and commits..."})
            status_res = await self.call_tool("git_status")
            history_res = await self.call_tool("git_history", {"limit": 10})

            last_c = status_res.get("last_commit") or {}
            self.base_commit = last_c.get("message", "HEAD")

            listing_res = await self.call_tool("list_files")
            self.files = listing_res.get("files", [])
            self.emit("thought", {"message": f"Discovered {len(self.files)} files across repository."})

            findings: list[Finding] = []
            finding_counter = 1

            # 1. Static Application Security Testing (SAST)
            self.emit("thought", {"message": "Running Static Application Security Testing (SAST) via MCP..."})
            sast_res = await self.call_tool("run_sast")
            for sf in sast_res.get("findings", []):
                findings.append(Finding(
                    id=f"SEC-{finding_counter:03d}",
                    file=sf.get("file", ""),
                    line=sf.get("line") or 1,
                    category="SAST",
                    severity=sf.get("severity", "HIGH"),
                    title=f"Security issue: {sf.get('rule', 'Code Flaw')}",
                    description=sf.get("message", "Potential security vulnerability identified."),
                    recommendation="Refactor to eliminate unsafe construct."
                ))
                finding_counter += 1

            # 2. Secret Detection
            self.emit("thought", {"message": "Scanning codebase for hardcoded secrets, API keys, and credentials..."})
            secrets_res = await self.call_tool("scan_secrets")
            for sec in secrets_res.get("findings", []):
                findings.append(Finding(
                    id=f"SEC-{finding_counter:03d}",
                    file=sec.get("file", ""),
                    line=sec.get("line") or 1,
                    category="SECRET",
                    severity="HIGH",
                    title=f"Exposed Secret: {sec.get('rule', 'Credential')}",
                    description=sec.get("description", "Exposed hardcoded secret found."),
                    recommendation="Move secret to environment variable or secrets manager."
                ))
                finding_counter += 1

            # 3. Code Linting & AST Syntax Checking
            self.emit("thought", {"message": "Performing AST syntax validation and code linting..."})
            lint_res = await self.call_tool("ruff_lint")
            for lf in lint_res.get("findings", []):
                findings.append(Finding(
                    id=f"LINT-{finding_counter:03d}",
                    file=lf.get("file", ""),
                    line=lf.get("line") or 1,
                    category="LINT",
                    severity="MEDIUM",
                    title=f"Code Quality: {lf.get('code', 'Lint Error')}",
                    description=lf.get("message", "Linting error or syntax defect."),
                    recommendation="Fix formatting or syntax according to standards."
                ))
                finding_counter += 1

            # 4. AST Structure Analysis on Key Source Files
            self.emit("thought", {"message": "Executing AST code structure parsing on source files..."})
            for f in self.files[:10]:
                if f.endswith((".py", ".js", ".ts", ".go", ".java")):
                    await self.call_tool("parse_source_file", {"path": f})

            # 5. Dependency Audit
            self.emit("thought", {"message": "Auditing dependency manifests for known CVEs..."})
            dep_res = await self.call_tool("dependency_audit")
            for dep in dep_res.get("findings", []):
                findings.append(Finding(
                    id=f"DEP-{finding_counter:03d}",
                    file="requirements.txt",
                    line=1,
                    category="DEPENDENCY",
                    severity=dep.get("severity", "MEDIUM"),
                    title=f"Dependency Risk: {dep.get('package', 'Package')}",
                    description=dep.get("issue", "Outdated or vulnerable dependency."),
                    recommendation="Pin dependency to patched and verified version."
                ))
                finding_counter += 1

            summary = (
                f"Audited {len(self.files)} files. Identified {len(findings)} security & quality findings "
                f"({len([f for f in findings if f.severity == 'HIGH'])} High, "
                f"{len([f for f in findings if f.severity == 'MEDIUM'])} Medium)."
            )
            self.emit("thought", {"message": summary})

            report = AuditReport(
                repo_url=target_repo,
                workspace=str(self.repo_path),
                base_commit=self.base_commit,
                summary=summary,
                findings=findings,
            )
            return report

        finally:
            await self.close()

    async def build_report(self, repo_url: str) -> str:
        rep = await self.run(repo_url)
        return rep.model_dump_json(indent=2)


async def main():
    parser = argparse.ArgumentParser(description="Autonomous Auditor Agent")
    parser.add_argument("repo_url", help="HTTPS or local Git repository URL")
    parser.add_argument("--out", help="Path to write report JSON")
    args = parser.parse_args()

    agent = AuditorAgent()
    report = await agent.run(args.repo_url)
    res_json = report.model_dump_json(indent=2)
    print(res_json)
    if args.out:
        Path(args.out).write_text(res_json, encoding="utf-8")


if __name__ == "__main__":
    anyio.run(main)