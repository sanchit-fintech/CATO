"""FastAPI interface for the Cato runtime."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.cato import Cato
from core.config import ConfigurationError, Settings

CATO_API_VERSION = "0.9.1"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = None


class ApprovalRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


def _result(result: object) -> dict[str, object]:
    return {key: value for key, value in vars(result).items() if value is not None}


def create_app(cato: Cato | None = None) -> FastAPI:
    app = FastAPI(title="Cato", version="1.0.0")
    runtime = cato

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

    return app
