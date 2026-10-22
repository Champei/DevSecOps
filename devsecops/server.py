try:
    import pydantic._internal._typing_extra as _pyd_typing
    if not hasattr(_pyd_typing, "eval_type_backport"):
        _pyd_typing.eval_type_backport = _pyd_typing.eval_type_lenient
except Exception:
    pass

try:
    import starlette.routing
    _orig_router_init = starlette.routing.Router.__init__
    def _patched_router_init(self, routes=None, redirect_slashes=True, default=None, on_startup=None, on_shutdown=None, lifespan=None, middleware=None):
        return _orig_router_init(self, routes=routes, redirect_slashes=redirect_slashes, default=default, lifespan=lifespan, middleware=middleware)
    starlette.routing.Router.__init__ = _patched_router_init
except Exception:
    pass

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from orchestrator.events import event_bus
from orchestrator.workflow import build_graph

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Autonomous DevSecOps Squad with MCP",
    description="Multi-agent squad inspecting code, generating patches, and verifying CI/CD gates with live SSE streaming.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for job states and background runners
JOBS: dict[str, dict[str, Any]] = {}


class CreateJobRequest(BaseModel):
    repo_url: str
    auto_approve: bool = False
    provider: Optional[str] = "ollama"
    model: Optional[str] = "llama3"


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: Optional[str] = ""


def run_pipeline_worker(job_id: str, repo_url: str, auto_approve: bool):
    try:
        graph = build_graph()
        config = {"configurable": {"thread_id": job_id}}

        initial_state = {
            "job_id": job_id,
            "repo_url": repo_url,
            "workspace": "",
            "base_commit": "",
            "retry_count": 0,
            "max_retries": 3,
            "approved": True if auto_approve else None,
            "status": "PENDING",
            "events": [],
            "audit_report": {},
            "patch": "",
            "feedback": "",
            "verification": {},
        }

        event_bus.publish(job_id, {
            "type": "thought",
            "agent": "Coordinator",
            "message": f"Coordinator initiated DevSecOps Squad for repository: {repo_url}",
        })

        result = graph.invoke(initial_state, config=config)
        JOBS[job_id]["state"] = result
        JOBS[job_id]["status"] = result.get("status", "COMPLETED")

        event_bus.publish(job_id, {
            "type": "complete",
            "status": result.get("status", "COMPLETED"),
            "patch": result.get("patch", ""),
            "verification": result.get("verification", {}),
            "message": "DevSecOps Squad pipeline finished execution.",
        })
    except Exception as exc:
        JOBS[job_id]["status"] = "FAILED"
        JOBS[job_id]["error"] = str(exc)
        event_bus.publish(job_id, {
            "type": "failed",
            "error": str(exc),
            "message": f"DevSecOps Squad encountered an error: {exc}",
        })


@app.post("/api/jobs")
async def create_job(req: CreateJobRequest, background_tasks: BackgroundTasks):
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    JOBS[job_id] = {
        "job_id": job_id,
        "repo_url": req.repo_url,
        "status": "PENDING",
        "created_at": None,
    }

    background_tasks.add_task(run_pipeline_worker, job_id, req.repo_url, req.auto_approve)

    return {
        "ok": True,
        "job_id": job_id,
        "repo_url": req.repo_url,
        "stream_url": f"/api/jobs/{job_id}/stream",
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    history = event_bus.get_history(job_id)
    return {
        "job": JOBS[job_id],
        "events": history,
    }


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    """Server-Sent Events (SSE) endpoint streaming intermediate agent thoughts, tool calls, diffs, and test results."""
    return StreamingResponse(
        event_bus.subscribe(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: str, req: ApprovalRequest):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    event_bus.publish(job_id, {
        "type": "human_decision",
        "approved": req.approved,
        "feedback": req.feedback,
        "message": f"Human supervisor decision: {'APPROVED' if req.approved else 'REJECTED'}",
    })

    JOBS[job_id]["approved"] = req.approved
    return {"ok": True, "job_id": job_id, "approved": req.approved}


@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "mcp_server": "dev-squad-tools",
        "active_jobs": len(JOBS),
    }


# Static web UI files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Autonomous DevSecOps Squad API is running. Access /static/index.html"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
