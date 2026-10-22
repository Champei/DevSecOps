from __future__ import annotations

import os, shutil, uuid
from pathlib import Path
from typing import Any

import git
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="dev-squad-write-tools")

WORKSPACE_ROOT = Path(os.getenv("DEVSQUAD_WORKSPACE_ROOT", "/tmp/devsquad_workspaces"))
PROTECTED = {"main", "master"}
MAX_DIFF = 20_000
_ACTIVE: Path | None = None


def workspace() -> Path | None:
    if _ACTIVE is not None and _ACTIVE.is_dir():
        return _ACTIVE
    dirs = [p for p in WORKSPACE_ROOT.glob("ws_*") if p.is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime, default=None)


def safe_path(rel: str):
    repo = workspace()
    if repo is None:
        return None, "no repository cloned yet"
    repo = repo.resolve()
    p = (repo / rel).resolve()
    if not p.is_relative_to(repo):
        return None, "path escapes repository"
    if ".git" in p.relative_to(repo).parts:
        return None, "refusing to touch .git"
    return p, None


def branch_of(repo: git.Repo):
    try:
        return repo.active_branch.name
    except TypeError:
        return None


def guard(repo: git.Repo):
    b = branch_of(repo)
    if b is None:
        return None, {"ok": False, "error": "detached HEAD"}
    if b in PROTECTED:
        return None, {"ok": False,
                      "error": f"refusing to modify protected branch '{b}'; call git_create_branch first"}
    return b, None


def compiles(src: str, name: str) -> bool:
    try:
        compile(src, name, "exec")
        return True
    except (SyntaxError, ValueError):
        return False


@mcp.tool()
def git_clone(repo_url: str) -> dict[str, Any]:
    """Clone an HTTPS repository into a fresh workspace."""
    global _ACTIVE
    if not repo_url.startswith("https://"):
        return {"ok": False, "error": "only https:// URLs are allowed"}
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    dest = WORKSPACE_ROOT / f"ws_{uuid.uuid4().hex[:12]}"
    try:
        repo = git.Repo.clone_from(repo_url, dest)
    except git.GitCommandError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": str(exc).replace(repo_url, "<repo_url>")[:500]}
    _ACTIVE = dest
    return {"ok": True, "branch": branch_of(repo), "workspace": str(dest)}


@mcp.tool()
def git_create_branch(branch_name: str) -> dict[str, Any]:
    """Create and switch to a NEW branch. Refuses main/master."""
    if branch_name in PROTECTED:
        return {"ok": False, "error": f"refusing protected name '{branch_name}'"}
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    try:
        repo.git.check_ref_format("--branch", branch_name)
    except git.GitCommandError:
        return {"ok": False, "error": f"'{branch_name}' is not a valid branch name"}
    if branch_name in [h.name for h in repo.heads]:
        return {"ok": False, "error": f"branch '{branch_name}' already exists"}
    try:
        repo.git.checkout("-b", branch_name)
    except git.GitCommandError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "branch": branch_name}


@mcp.tool()
def git_commit(message: str) -> dict[str, Any]:
    """Stage everything and commit. Refuses main/master."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    branch, err = guard(repo)
    if err:
        return err
    if not repo.is_dirty(untracked_files=True):
        return {"ok": False, "error": "nothing to commit"}
    try:
        repo.git.add(A=True)
        actor = git.Actor(os.getenv("GIT_AUTHOR_NAME", "DevSquad Coder"),
                          os.getenv("GIT_AUTHOR_EMAIL", "devsquad@users.noreply.github.com"))
        c = repo.index.commit(message, author=actor, committer=actor)
    except git.GitCommandError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "branch": branch, "commit_sha": c.hexsha[:10]}


@mcp.tool()
def git_push() -> dict[str, Any]:
    """Push the current feature branch to origin. Refuses main/master."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    branch, err = guard(repo)
    if err:
        return err
    try:
        repo.git.push("-u", "origin", branch)
    except git.GitCommandError as exc:
        return {"ok": False, "error": str(exc)[:500]}
    return {"ok": True, "branch": branch}


@mcp.tool()
def git_changes() -> dict[str, Any]:
    """List changed files and a truncated diff."""
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    files = [l[3:].strip() for l in repo.git.status("--porcelain").splitlines() if l.strip()]
    try:
        diff = repo.git.diff("HEAD")
    except git.GitCommandError:
        diff = ""
    return {"ok": True, "changed_files": files, "diff": diff[:MAX_DIFF]}


@mcp.tool()
def apply_fixes(fixes: list[dict[str, Any]]) -> dict[str, Any]:
    """Single write tool: apply search/replace edits.

    Each fix has finding_id, file, search (EXACT text, must occur once;
    empty = create new file), replace, description. Python files are only
    written if they still compile. Refuses main/master.
    """
    path = workspace()
    if path is None:
        return {"ok": False, "error": "no repository cloned yet"}
    repo = git.Repo(path)
    branch, err = guard(repo)
    if err:
        return err

    if not isinstance(fixes, list):
        return {"ok": False, "error": "'fixes' must be a list"}
    if not fixes:
        return {"ok": False, "error": "nothing to do: supply fixes"}

    applied, failed, changed = [], [], []

    def info(f):
        return {"finding_id": str(f.get("finding_id") or ""),
                "file": str(f.get("file") or ""),
                "description": str(f.get("description") or "")}

    def fail(f, why):
        failed.append({**info(f), "reason": why})

    by_file: dict[str, list[dict]] = {}
    for f in fixes:
        rel = str(f.get("file") or "").strip() if isinstance(f, dict) else ""
        if not rel:
            fail(f if isinstance(f, dict) else {}, "missing 'file'")
            continue
        by_file.setdefault(rel, []).append(f)

    for rel, items in by_file.items():
        target, perr = safe_path(rel)
        if perr:
            for f in items:
                fail(f, perr)
            continue
        original, crlf = None, False
        if target.is_file():
            try:
                raw = target.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                for f in items:
                    fail(f, "file is not UTF-8 text")
                continue
            crlf = "\r\n" in raw
            original = raw.replace("\r\n", "\n")
        elif target.exists():
            for f in items:
                fail(f, "path is not a regular file")
            continue

        cur, ok_items = original, []
        for f in items:
            s = f.get("search") if isinstance(f.get("search"), str) else ""
            r = f.get("replace") if isinstance(f.get("replace"), str) else ""
            s, r = s.replace("\r\n", "\n"), r.replace("\r\n", "\n")
            if s == "":
                if cur is None:
                    cur = r
                    ok_items.append(f)
                else:
                    fail(f, "file exists; 'search' text is required")
                continue
            if cur is None:
                fail(f, "file does not exist")
                continue
            n = cur.count(s)
            if n == 0:
                fail(f, "search text not found (must match exactly)")
            elif n > 1:
                fail(f, f"search text matches {n} places; add more context")
            elif s == r:
                fail(f, "search and replace are identical")
            else:
                cur = cur.replace(s, r, 1)
                ok_items.append(f)

        if not ok_items or cur is None or cur == original:
            continue
        if rel.endswith(".py") and (original is None or compiles(original, rel)) and not compiles(cur, rel):
            for f in ok_items:
                fail(f, "edit rejected: would cause a Python syntax error")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((cur.replace("\n", "\r\n") if crlf else cur).encode("utf-8"))
        except OSError as exc:
            for f in ok_items:
                fail(f, f"could not write file: {exc}")
            continue
        applied.extend(info(f) for f in ok_items)
        changed.append(rel)

    return {"ok": True, "branch": branch, "applied_count": len(applied),
            "failed_count": len(failed), "applied": applied, "failed": failed,
            "files_changed": changed}


if __name__ == "__main__":
    mcp.run()