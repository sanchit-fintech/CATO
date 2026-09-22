"""FastAPI interface for the Cato runtime."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.cato import VERSION, Cato
from core.config import ConfigurationError, Settings
from core.task_service import TaskService
from core.tasks import TERMINAL_STATUSES

CATO_API_VERSION = VERSION


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = None


class ApprovalRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


class TaskSubmission(BaseModel):
    request: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = None
    priority: int = Field(default=0, ge=-1, le=1)


class ScheduleSubmission(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    request: str = Field(min_length=1, max_length=20_000)
    run_at: datetime
    interval_seconds: int | None = Field(default=None, ge=60)


def _result(result: object) -> dict[str, object]:
    return {key: value for key, value in vars(result).items() if value is not None}


def create_app(cato: Cato | None = None) -> FastAPI:
    runtime = cato
    task_service = TaskService(runtime) if runtime is not None else None
    auth_token = (
        runtime.settings.api_token
        if runtime is not None
        else Settings.load(require_api_key=False).api_token
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if task_service is not None:
            task_service.start()
        yield
        if task_service is not None:
            task_service.stop()

    app = FastAPI(title="Cato", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if auth_token:
            authorization = request.headers.get("Authorization", "")
            if not hmac.compare_digest(authorization, f"Bearer {auth_token}"):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "authentication_required",
                            "message": "A valid local API token is required.",
                        }
                    },
                )
        return await call_next(request)

    def service() -> TaskService:
        nonlocal runtime, task_service
        runtime = runtime or Cato()
        task_service = task_service or TaskService(runtime)
        return task_service

    @app.exception_handler(Exception)
    async def safe_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "The request could not be completed.",
                }
            },
        )

    @app.get("/health")
    def health() -> dict[str, object]:
        if runtime is not None:
            configured = True
            provider = type(runtime.provider).__name__
        else:
            settings = Settings.load(require_api_key=False)
            configured = bool(settings.gemini_api_key)
            provider = "gemini"
        return {
            "status": "ok",
            "service": "cato",
            "version": CATO_API_VERSION,
            "ready": configured,
            "provider": provider,
            "provider_configured": configured,
        }

    @app.post("/chat")
    def chat(request: ChatRequest):
        nonlocal runtime
        if runtime is None:
            try:
                runtime = Cato()
            except ConfigurationError:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": "provider_not_configured",
                            "message": (
                                "Cato is running, but GEMINI_API_KEY is not configured."
                            ),
                        }
                    },
                )
        result = runtime.run(request.message, session_id=request.session_id)
        return _result(result)

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, request: ApprovalRequest) -> dict[str, object]:
        nonlocal runtime
        runtime = runtime or Cato()
        return _result(runtime.approve(approval_id, request.session_id))

    @app.post("/approvals/{approval_id}/deny")
    def deny(approval_id: str, request: ApprovalRequest) -> dict[str, object]:
        nonlocal runtime
        runtime = runtime or Cato()
        return _result(runtime.deny(approval_id, request.session_id))

    @app.delete("/sessions/{session_id}")
    def clear_session(session_id: str) -> dict[str, object]:
        nonlocal runtime
        runtime = runtime or Cato()
        return {"cleared": runtime.memory.clear(session_id)}

    @app.get("/tasks")
    def list_tasks(
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, object]]:
        if runtime is None:
            return []
        try:
            tasks = runtime.tasks.list(
                session_id=session_id,
                status=status,
                limit=limit,
                include_archived=include_archived,
            )
        except ValueError:
            return []
        return [task.to_dict() for task in tasks]

    @app.post("/tasks", status_code=202)
    def submit_task(request: TaskSubmission, http_request: Request):
        key = http_request.headers.get("Idempotency-Key")
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
        payload_hash = hashlib.sha256(
            json.dumps(request.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        if key_hash and runtime is not None:
            existing = runtime.tasks.idempotency(key_hash)
            if existing:
                previous_hash, task_id = existing
                if not hmac.compare_digest(previous_hash, payload_hash):
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": {
                                "code": "idempotency_conflict",
                                "message": "Key was used for a different request.",
                            }
                        },
                    )
                task = runtime.tasks.get(task_id)
                assert task is not None
                return {
                    "task_id": task.id,
                    "request_id": task.request_id,
                    "session_id": task.session_id,
                    "status": task.status,
                }
        try:
            task = service().submit(
                request.request,
                session_id=request.session_id,
                priority=request.priority,
            )
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "task_refused", "message": str(error)}},
            )
        if key_hash:
            service().tasks.register_idempotency(key_hash, payload_hash, task.id)
        return {
            "task_id": task.id,
            "request_id": task.request_id,
            "session_id": task.session_id,
            "status": task.status,
        }

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        if runtime is None or (task := runtime.tasks.get(task_id)) is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {"code": "task_not_found", "message": "Task not found."}
                },
            )
        return task.to_dict()

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        if runtime is None or not runtime.tasks.cancel(task_id):
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "task_not_cancellable",
                        "message": "Task cannot be cancelled.",
                    }
                },
            )
        task = runtime.tasks.get(task_id)
        return {"task_id": task_id, "status": task.status if task else "cancelled"}

    @app.post("/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        if runtime is None or not runtime.tasks.retry(task_id):
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "task_not_retryable",
                        "message": "Task cannot be retried.",
                    }
                },
            )
        service().start()
        return {"task_id": task_id, "status": "queued"}

    @app.post("/tasks/{task_id}/archive")
    def archive_task(task_id: str):
        if runtime is None or not runtime.tasks.archive(task_id):
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "task_not_archivable"}},
            )
        return {"task_id": task_id, "status": "archived"}

    @app.get("/tasks/{task_id}/events")
    def task_events(
        task_id: str, after: int = 0, limit: int = 200
    ) -> list[dict[str, object]]:
        if runtime is None or runtime.tasks.get(task_id) is None:
            return []
        return [
            event.to_dict()
            for event in runtime.tasks.events(task_id, after=after, limit=limit)
        ]

    @app.get("/tasks/{task_id}/events/stream")
    def stream_task_events(task_id: str, request: Request):
        if runtime is None or runtime.tasks.get(task_id) is None:
            return JSONResponse(status_code=404, content={"error": "task_not_found"})
        try:
            after = max(0, int(request.headers.get("Last-Event-ID", "0")))
        except ValueError:
            after = 0

        def generate():
            cursor, idle_cycles = after, 0
            while idle_cycles < 600:
                events = runtime.tasks.events(task_id, after=cursor, limit=200)
                for event in events:
                    cursor = event.id
                    yield (
                        f"id: {event.id}\n"
                        f"event: {event.type}\n"
                        f"data: {json.dumps(event.payload, default=str)}\n\n"
                    )
                task = runtime.tasks.get(task_id)
                if task is None or (task.status in TERMINAL_STATUSES and not events):
                    break
                if not events:
                    idle_cycles += 1
                    if idle_cycles % 100 == 0:
                        yield ": heartbeat\n\n"
                    time.sleep(0.05)
                else:
                    idle_cycles = 0

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/tools")
    def list_tools() -> list[dict[str, object]]:
        return runtime.tools.schemas() if runtime is not None else []

    @app.post("/schedules", status_code=201)
    def create_schedule(request: ScheduleSubmission):
        task_worker = service()
        try:
            job = task_worker.tasks.create_schedule(
                request.name,
                request.request,
                request.run_at,
                interval_seconds=request.interval_seconds,
            )
        except (ValueError, RuntimeError) as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "invalid_schedule", "message": str(error)}},
            )
        task_worker.start()
        return job.to_dict()

    @app.get("/schedules")
    def list_schedules() -> list[dict[str, object]]:
        if runtime is None:
            return []
        return [job.to_dict() for job in runtime.tasks.schedules()]

    @app.get("/readiness")
    def readiness() -> dict[str, object]:
        settings = Settings.load(require_api_key=False)
        configured = runtime is not None or bool(settings.gemini_api_key)
        return {
            "status": "ready" if configured else "not_ready",
            "provider_configured": configured,
            "memory": "available",
            "tools_registered": len(runtime.tools.list_tools()) if runtime else 0,
        }

    @app.get("/status")
    def platform_status() -> dict[str, object]:
        if runtime is None:
            return {"server": "running", "runtime": "not_initialized"}
        tasks = runtime.tasks.list(limit=500, include_archived=True)
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        return {
            "server": "running",
            "version": CATO_API_VERSION,
            "tasks": counts,
            "workers": runtime.tasks.workers(),
            "schedules": len(runtime.tasks.schedules(enabled=True)),
            "auth_enabled": bool(runtime.settings.api_token),
        }

    return app
