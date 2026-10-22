try:
    import pydantic._internal._typing_extra as _pyd_typing
    if not hasattr(_pyd_typing, "eval_type_backport"):
        _pyd_typing.eval_type_backport = _pyd_typing.eval_type_lenient
except Exception:
    pass

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import git
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="dev-squad-tools")

WORKSPACE_ROOT = Path(os.getenv("DEVSQUAD_WORKSPACE_ROOT", Path(tempfile.gettempdir()) / "devsquad_workspaces"))
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
PROTECTED_BRANCHES = {"main", "master"}
_ACTIVE_WORKSPACE: Path | None = None


def workspace() -> Path | None:
    global _ACTIVE_WORKSPACE
    if _ACTIVE_WORKSPACE is not None and _ACTIVE_WORKSPACE.is_dir():
        return _ACTIVE_WORKSPACE
    dirs = [p for p in WORKSPACE_ROOT.glob("ws_*") if p.is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime, default=None)


def branch_of(repo: git.Repo) -> str | None:
    try:
        return repo.active_branch.name
    except (TypeError, ValueError):
        return None


def commit_dict(c: git.Commit) -> dict[str, Any]:
    return {
        "author": c.author.name if c.author else "Unknown",
        "date": datetime.fromtimestamp(c.committed_date, tz=timezone.utc).isoformat(),
        "message": c.message.strip().splitlines()[0] if c.message else "",
    }


def run_command(args: list[str], cwd: Path, timeout: int = 120) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
            env={
                **os.environ,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        return {
            "return_code": result.returncode,
            "stdout": result.stdout[-100_000:],
            "stderr": result.stderr[-50_000:],
        }
    except subprocess.TimeoutExpired:
        return {"return_code": -1, "stdout": "", "stderr": f"Command timed out after {timeout} seconds"}
    except FileNotFoundError as exc:
        return {"return_code": -1, "stdout": "", "stderr": f"Command not found: {exc}"}


def command_available(command: str) -> bool:
    return shutil.which(command) is not None


# ==========================================
# MCP Git & Workspace Tools
# ==========================================


@mcp.tool()
def git_clone(repo_url: str) -> dict[str, Any]:
    """Clone an HTTPS or local Git repository into a fresh workspace."""
    global _ACTIVE_WORKSPACE
    dest = Path(tempfile.mkdtemp(prefix="ws_", dir=WORKSPACE_ROOT))
    try:
        repo = git.Repo.clone_from(repo_url, dest, depth=50)
        _ACTIVE_WORKSPACE = dest
        branch = branch_of(repo) or "detached"
        return {"ok": True, "path": str(dest), "workspace": str(dest), "branch": branch}
    except git.GitCommandError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": str(exc)[:500]}


@mcp.tool()
def git_status() -> dict[str, Any]:
    """Get current git branch, last commit, and dirty status."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    branch = branch_of(repo)
    tracking = None
    try:
        tracking = repo.active_branch.tracking_branch()
    except Exception:
        pass
    return {
        "ok": True,
        "branch": branch,
        "is_dirty": repo.is_dirty(untracked_files=True),
        "last_commit": commit_dict(repo.head.commit) if repo.head.is_valid() else None,
        "last_pushed_commit": commit_dict(tracking.commit) if tracking else None,
    }


@mcp.tool()
def git_history(limit: int = 20) -> dict[str, Any]:
    """List recent commits from the repository."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    try:
        commits = [commit_dict(c) for c in repo.iter_commits(max_count=limit)]
        return {"ok": True, "commits": commits}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def git_diff() -> dict[str, Any]:
    """Return unified diff of all unstaged and staged changes in repository."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    result = run_command(["git", "diff", "HEAD"], cwd=path)
    if result["return_code"] != 0:
        result = run_command(["git", "diff"], cwd=path)
    return {"ok": True, "diff": result["stdout"], "raw_output": result["stdout"][:20000]}


@mcp.tool()
def git_changes() -> dict[str, Any]:
    """List changed and untracked files with diff."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    files = [line[3:].strip() for line in repo.git.status("--porcelain").splitlines() if line.strip()]
    try:
        diff = repo.git.diff("HEAD")
    except Exception:
        diff = repo.git.diff()
    return {"ok": True, "changed_files": files, "diff": diff[:25000]}


@mcp.tool()
def git_create_branch(branch_name: str) -> dict[str, Any]:
    """Create and switch to a new git branch. Refuses protected branches (main, master)."""
    if branch_name in PROTECTED_BRANCHES:
        return {"ok": False, "error": f"Refusing to branch to protected name '{branch_name}'"}
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    try:
        repo.git.check_ref_format("--branch", branch_name)
    except git.GitCommandError:
        return {"ok": False, "error": f"'{branch_name}' is not a valid branch name"}
    if branch_name in [h.name for h in repo.heads]:
        repo.git.checkout(branch_name)
        return {"ok": True, "branch": branch_name, "message": "Switched to existing branch"}
    try:
        repo.git.checkout("-b", branch_name)
    except git.GitCommandError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "branch": branch_name}


@mcp.tool()
def git_commit(message: str) -> dict[str, Any]:
    """Stage all changes and commit. Refuses commits directly on protected branches."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    branch = branch_of(repo)
    if branch in PROTECTED_BRANCHES:
        return {"ok": False, "error": f"Refusing to commit directly on protected branch '{branch}'"}
    if not repo.is_dirty(untracked_files=True):
        return {"ok": False, "error": "Nothing to commit (working tree clean)"}
    try:
        repo.git.add(A=True)
        actor = git.Actor(
            os.getenv("GIT_AUTHOR_NAME", "DevSecOps Squad"),
            os.getenv("GIT_AUTHOR_EMAIL", "devsecops@automated-squad.local"),
        )
        c = repo.index.commit(message, author=actor, committer=actor)
        return {"ok": True, "branch": branch, "commit_sha": c.hexsha[:10]}
    except git.GitCommandError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def git_push() -> dict[str, Any]:
    """Push current feature branch to origin. Refuses main/master."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    branch = branch_of(repo)
    if branch in PROTECTED_BRANCHES:
        return {"ok": False, "error": f"Refusing to push protected branch '{branch}'"}
    try:
        repo.git.push("-u", "origin", branch)
        return {"ok": True, "branch": branch}
    except git.GitCommandError as exc:
        return {"ok": False, "error": str(exc)[:500]}


# ==========================================
# File System Tools
# ==========================================

MAX_FILE_SIZE = 200_000
MAX_FILES = 5_000
IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", ".next", "target", ".idea", ".vscode"
}


def resolve_file(relative_path: str):
    repo = workspace()
    if repo is None:
        return None, "no repository cloned yet"
    repo = repo.resolve()
    requested = (repo / relative_path).resolve()
    if not requested.is_relative_to(repo):
        return None, "path escapes repository"
    return requested, None


@mcp.tool()
def list_files() -> dict[str, Any]:
    """List relative paths of all non-ignored files in the repository."""
    repo = workspace()
    if repo is None:
        return {"ok": False, "error": "no repository cloned yet"}

    files = []
    for root, dirs, filenames in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for filename in filenames:
            rel = (Path(root) / filename).relative_to(repo).as_posix()
            files.append(rel)
            if len(files) >= MAX_FILES:
                return {"ok": True, "files": files, "truncated": True}
    return {"ok": True, "files": files, "truncated": False}


@mcp.tool()
def read_file(path: str) -> dict[str, Any]:
    """Read full text content of a file within the cloned repository."""
    file_path, error = resolve_file(path)
    if error or not file_path:
        return {"ok": False, "path": path, "error": error or "file not found"}
    if not file_path.is_file():
        return {"ok": False, "path": path, "error": "file does not exist"}

    size = file_path.stat().st_size
    if size > MAX_FILE_SIZE:
        return {"ok": False, "path": path, "error": "file too large to read", "size": size}

    return {
        "ok": True,
        "path": path,
        "size": size,
        "content": file_path.read_text(encoding="utf-8", errors="replace"),
    }


@mcp.tool()
def write_file(path: str, content: str) -> dict[str, Any]:
    """Write text content to a file in the workspace."""
    file_path, error = resolve_file(path)
    if error or not file_path:
        return {"ok": False, "path": path, "error": error}
    if ".git" in file_path.parts:
        return {"ok": False, "path": path, "error": "refusing to modify .git directory"}

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size": len(content)}


@mcp.tool()
def delete_file(path: str) -> dict[str, Any]:
    """Delete a file in the repository."""
    file_path, error = resolve_file(path)
    if error or not file_path:
        return {"ok": False, "path": path, "error": error}
    if file_path.is_file():
        file_path.unlink()
        return {"ok": True, "path": path, "deleted": True}
    return {"ok": False, "path": path, "error": "file not found"}


@mcp.tool()
def apply_fixes(fixes: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply atomic search/replace patch edits to repository files."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}

    applied, failed, changed = [], [], []

    for item in fixes:
        rel = str(item.get("file") or "").strip()
        fid = str(item.get("finding_id") or "")
        search_txt = item.get("search", "")
        replace_txt = item.get("replace", "")

        target_path, err = resolve_file(rel)
        if err or not target_path:
            failed.append({"finding_id": fid, "file": rel, "reason": err or "invalid path"})
            continue

        if not target_path.exists() and search_txt == "":
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(replace_txt, encoding="utf-8")
            applied.append({"finding_id": fid, "file": rel, "action": "created"})
            changed.append(rel)
            continue

        if not target_path.is_file():
            failed.append({"finding_id": fid, "file": rel, "reason": "target is not a file"})
            continue

        original = target_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        search_normalized = search_txt.replace("\r\n", "\n")
        replace_normalized = replace_txt.replace("\r\n", "\n")

        if search_normalized not in original:
            failed.append({"finding_id": fid, "file": rel, "reason": "search string not found in file"})
            continue

        if original.count(search_normalized) > 1:
            failed.append({"finding_id": fid, "file": rel, "reason": "search string matches multiple locations"})
            continue

        updated = original.replace(search_normalized, replace_normalized, 1)

        # Syntax check for Python files
        if rel.endswith(".py"):
            try:
                compile(updated, rel, "exec")
            except SyntaxError as syn_err:
                failed.append({"finding_id": fid, "file": rel, "reason": f"Syntax error in replacement: {syn_err}"})
                continue

        target_path.write_text(updated, encoding="utf-8")
        applied.append({"finding_id": fid, "file": rel, "action": "patched"})
        changed.append(rel)

    return {
        "ok": True,
        "applied_count": len(applied),
        "failed_count": len(failed),
        "applied": applied,
        "failed": failed,
        "files_changed": list(set(changed)),
    }


# ==========================================
# AST Parsing Tools
# ==========================================

EXTENSION_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".java": "java", ".go": "go", ".rs": "rust"
}


@mcp.tool()
def parse_source_file(path: str) -> dict[str, Any]:
    """Parse a source file and extract AST structures (functions, classes, imports, calls)."""
    file_path, error = resolve_file(path)
    if error or not file_path:
        return {"ok": False, "path": path, "error": error}

    language = EXTENSION_LANGUAGE.get(file_path.suffix)
    if not language:
        return {"ok": False, "path": path, "error": f"unsupported language {file_path.suffix}"}

    source_text = file_path.read_text(encoding="utf-8", errors="replace")

    # Native Python AST parsing
    if language == "python":
        try:
            tree = ast.parse(source_text, filename=path)
            functions, classes, imports, calls = [], [], [], []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append({"name": node.name, "line": node.lineno})
                elif isinstance(node, ast.ClassDef):
                    classes.append({"name": node.name, "line": node.lineno})
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append({"statement": f"import {alias.name}", "line": node.lineno})
                elif isinstance(node, ast.ImportFrom):
                    imports.append({"statement": f"from {node.module or ''} import ...", "line": node.lineno})
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None) or "call"
                    calls.append({"name": str(name), "line": node.lineno})
            return {
                "ok": True,
                "path": path,
                "language": "python",
                "functions": functions[:100],
                "classes": classes[:50],
                "imports": imports[:50],
                "calls": calls[:150],
            }
        except SyntaxError as e:
            return {"ok": False, "path": path, "error": f"Python syntax error at line {e.lineno}: {e.msg}"}

    # Lightweight regex AST extractor for JS/TS/Go/Java/Rust
    funcs = re.findall(r"(?:function\s+([a-zA-Z0-9_]+)|def\s+([a-zA-Z0-9_]+)|func\s+([a-zA-Z0-9_]+))", source_text)
    classes = re.findall(r"(?:class|interface|struct)\s+([a-zA-Z0-9_]+)", source_text)
    imports = re.findall(r"(?:import|from|require)\s+[\"']?([a-zA-Z0-9_\-\./]+)[\"']?", source_text)

    found_funcs = [{"name": next(filter(None, f)), "line": 1} for f in funcs if any(f)]
    return {
        "ok": True,
        "path": path,
        "language": language,
        "functions": found_funcs[:50],
        "classes": [{"name": c, "line": 1} for c in classes[:30]],
        "imports": [{"statement": imp, "line": 1} for imp in imports[:30]],
        "calls": [],
    }


# ==========================================
# Code Linting, SAST, Secrets & Security Tools
# ==========================================


@mcp.tool()
def ruff_lint(target: str = ".") -> dict[str, Any]:
    """Run code linter and syntax checks across repository."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}

    findings = []
    # If ruff is installed, run it
    if command_available("ruff"):
        res = run_command(["ruff", "check", target, "--output-format=json"], cwd=path)
        try:
            items = json.loads(res["stdout"]) if res["stdout"] else []
            for item in items:
                findings.append({
                    "file": item.get("filename", ""),
                    "line": item.get("location", {}).get("row", 1),
                    "code": item.get("code", "LINT"),
                    "message": item.get("message", ""),
                })
        except Exception:
            pass

    # Built-in fallback Python syntax & basic sanity audit
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = Path(root) / f
                try:
                    ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
                except SyntaxError as syn:
                    findings.append({
                        "file": str(p.relative_to(path)),
                        "line": syn.lineno or 1,
                        "code": "E999",
                        "message": f"SyntaxError: {syn.msg}",
                    })

    return {
        "ok": True,
        "finding_count": len(findings),
        "findings": findings[:100],
        "passed": len(findings) == 0,
    }


@mcp.tool()
def run_sast() -> dict[str, Any]:
    """Run static application security testing (SAST) for dangerous coding patterns."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}

    findings = []
    # If semgrep is available
    if command_available("semgrep"):
        res = run_command(["semgrep", "scan", "--json", "--quiet", "."], cwd=path)
        try:
            data = json.loads(res["stdout"] or "{}")
            for item in data.get("results", []):
                findings.append({
                    "rule": item.get("check_id"),
                    "file": item.get("path"),
                    "line": item.get("start", {}).get("line"),
                    "message": item.get("extra", {}).get("message"),
                    "severity": item.get("extra", {}).get("severity", "MEDIUM"),
                })
            return {"ok": True, "available": True, "findings": findings}
        except Exception:
            pass

    # High-precision built-in security patterns
    security_patterns = [
        (r"eval\(", "SEC-EVAL", "HIGH", "Use of eval() detected, allowing arbitrary code execution"),
        (r"exec\(", "SEC-EXEC", "HIGH", "Use of exec() detected, allowing arbitrary code execution"),
        (r"subprocess\.(?:Popen|run|call)\(.*shell\s*=\s*True", "SEC-SHELL-TRUE", "HIGH", "Subprocess with shell=True invites command injection"),
        (r"os\.system\(", "SEC-OS-SYSTEM", "MEDIUM", "os.system() used instead of safer subprocess with explicit arguments"),
        (r"yaml\.load\((?!.*SafeLoader)", "SEC-UNSAFE-YAML", "HIGH", "Unsafe yaml.load() allows deserialization exploits; use safe_load"),
        (r"pickle\.loads?\(", "SEC-PICKLE", "HIGH", "Unsafe deserialization via pickle allows remote code execution"),
        (r"verify\s*=\s*False", "SEC-TLS-DISABLED", "HIGH", "Disabled SSL/TLS certificate verification detected"),
    ]

    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for f in files:
            if f.endswith((".py", ".js", ".ts")):
                p = Path(root) / f
                content = p.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                for line_idx, line in enumerate(lines, start=1):
                    for pat, rule_id, sev, desc in security_patterns:
                        if re.search(pat, line):
                            findings.append({
                                "rule": rule_id,
                                "file": str(p.relative_to(path)).replace("\\", "/"),
                                "line": line_idx,
                                "message": desc,
                                "severity": sev,
                            })

    return {"ok": True, "available": True, "findings": findings}


@mcp.tool()
def scan_secrets() -> dict[str, Any]:
    """Scan the repository for leaked API keys, tokens, and hardcoded secrets."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}

    findings = []
    # If gitleaks is installed
    if command_available("gitleaks"):
        res = run_command(["gitleaks", "detect", "--source", ".", "--report-format", "json", "--no-banner"], cwd=path)
        try:
            data = json.loads(res["stdout"] or "[]")
            for item in data:
                findings.append({
                    "rule": item.get("RuleID", "SECRET"),
                    "file": item.get("File"),
                    "line": item.get("StartLine", 1),
                    "description": item.get("Description", "Secret detected"),
                })
            return {"ok": True, "findings": findings}
        except Exception:
            pass

    # Built-in secret regex rules
    secret_patterns = [
        (r"(?:api_key|apikey|secret_key|secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]", "GENERIC-SECRET", "Potential hardcoded secret or API key"),
        (r"ghp_[A-Za-z0-9_]{36}", "GITHUB-TOKEN", "GitHub personal access token"),
        (r"AKIA[0-9A-Z]{16}", "AWS-ACCESS-KEY", "AWS Access Key ID"),
        (r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", "PRIVATE-KEY", "Unencrypted private cryptographic key"),
    ]

    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for f in files:
            p = Path(root) / f
            content = p.read_text(encoding="utf-8", errors="replace")
            for line_idx, line in enumerate(content.splitlines(), start=1):
                for pat, rule_id, desc in secret_patterns:
                    if re.search(pat, line, re.IGNORECASE):
                        findings.append({
                            "rule": rule_id,
                            "file": str(p.relative_to(path)).replace("\\", "/"),
                            "line": line_idx,
                            "description": desc,
                        })

    return {"ok": True, "findings": findings}


@mcp.tool()
def dependency_audit() -> dict[str, Any]:
    """Audit project dependencies for known security vulnerabilities or outdated versions."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}

    findings = []
    req_file = path / "requirements.txt"
    if req_file.exists():
        content = req_file.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "==" not in line and ">=" not in line:
                findings.append({
                    "package": line,
                    "issue": "Unpinned dependency version poses supply chain risks",
                    "severity": "LOW",
                })
            elif "urllib3==1.26.5" in line or "requests==2.20.0" in line:
                findings.append({
                    "package": line,
                    "issue": "Known vulnerable package version",
                    "severity": "HIGH",
                })

    return {"ok": True, "findings": findings}


# ==========================================
# CI/CD Verification Gate - Test Runner Tool
# ==========================================


@mcp.tool()
def run_tests(command: str = "", timeout: int = 120) -> dict[str, Any]:
    """CI/CD Verification Gate: Execute automated tests in the repository and verify correctness."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}

    # Automatic test discovery
    test_cmd = command
    if not test_cmd:
        if (path / "pytest.ini").exists() or (path / "tests").exists() or any(path.glob("test_*.py")):
            if command_available("pytest"):
                test_cmd = "pytest -v"
            else:
                test_cmd = f"{sys.executable} -m unittest discover -v"
        elif (path / "package.json").exists():
            test_cmd = "npm test"
        else:
            test_cmd = f"{sys.executable} -m unittest discover -s . -p \"*test*.py\""

    cmd_parts = test_cmd.split()
    res = run_command(cmd_parts, cwd=path, timeout=timeout)
    passed = res["return_code"] == 0

    return {
        "ok": True,
        "command": test_cmd,
        "passed": passed,
        "exit_code": res["return_code"],
        "stdout": res["stdout"],
        "stderr": res["stderr"],
        "summary": "All tests passed successfully!" if passed else f"Tests failed with exit code {res['return_code']}",
    }


if __name__ == "__main__":
    mcp.run()