"""FastAPI interface for the Cato runtime."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.cato import Cato


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = None


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
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat")
    def chat(request: ChatRequest) -> dict[str, object]:
        nonlocal runtime
        runtime = runtime or Cato()
        result = runtime.run(request.message, session_id=request.session_id)
        return {
            "response": result.response,
            "session_id": result.session_id,
            "status": result.status,
            "iterations": result.iterations,
        }

    @app.delete("/sessions/{session_id}")
    def clear_session(session_id: str) -> dict[str, object]:
        nonlocal runtime
        runtime = runtime or Cato()
        return {"cleared": runtime.memory.clear(session_id)}

    return app
