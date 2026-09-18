from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException

from app.guardrail_models import (
    GuardrailRequest,
    GuardrailResponse,
    RagVerificationRequest,
    RagVerificationResponse,
    ToolGateRequest,
)
from app.guardrails import GuardrailEngine


def _require_guardrail(x_rtdc_guardrail_key: str | None = Header(default=None)) -> bool:
    expected = (
        os.getenv("RTDC_GUARDRAIL_API_KEY", "").strip()
        or os.getenv("RTDC_REALTIME_API_KEY", "").strip()
        or os.getenv("RTDC_ADMIN_API_KEY", "").strip()
    )
    if expected and (x_rtdc_guardrail_key is None or not hmac.compare_digest(x_rtdc_guardrail_key, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Guardrail-Key")
    return True


def install_guardrail_api(app, operations_engine) -> GuardrailEngine:
    engine = GuardrailEngine(operations_engine)
    router = APIRouter(prefix="/v1/guardrails", dependencies=[Depends(_require_guardrail)])

    @router.post("/evaluate", response_model=GuardrailResponse)
    async def evaluate_guardrails(request: GuardrailRequest):
        try:
            return await engine.evaluate(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/tool-call", response_model=GuardrailResponse)
    async def gate_tool_call(request: ToolGateRequest):
        try:
            return await engine.gate_tool(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/rag", response_model=RagVerificationResponse)
    async def verify_rag(request: RagVerificationRequest):
        try:
            return engine.verify_rag(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    app.include_router(router)
    return engine
