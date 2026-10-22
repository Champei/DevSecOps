import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator


class JobEventManager:
    def __init__(self):
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    def publish(self, job_id: str, event_data: dict[str, Any]):
        payload = {
            "job_id": job_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event_data,
        }
        if job_id not in self._history:
            self._history[job_id] = []
        self._history[job_id].append(payload)

        # Notify active subscriber queues
        if job_id in self._queues:
            for q in self._queues[job_id]:
                try:
                    q.put_nowait(payload)
                except Exception:
                    pass

    async def subscribe(self, job_id: str) -> AsyncGenerator[str, None]:
        q: asyncio.Queue = asyncio.Queue()
        if job_id not in self._queues:
            self._queues[job_id] = []
        self._queues[job_id].append(q)

        try:
            # Yield historical events first
            for past_event in self._history.get(job_id, []):
                yield f"data: {json.dumps(past_event)}\n\n"

            while True:
                # Wait for next event
                event = await q.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("complete", "failed"):
                    break
        finally:
            if job_id in self._queues and q in self._queues[job_id]:
                self._queues[job_id].remove(q)

    def get_history(self, job_id: str) -> list[dict[str, Any]]:
        return self._history.get(job_id, [])


# Global event manager instance
event_bus = JobEventManager()
