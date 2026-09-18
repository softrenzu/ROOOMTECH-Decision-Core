from __future__ import annotations

import asyncio
from fastapi import FastAPI, HTTPException
from app import __version__
from app.engine import DecisionEngine
from app.license import LicenseError, verify_runtime_license
from app.models import BatchRequest, BatchResponse, DecisionRequest, DecisionResponse

app = FastAPI(
    title="ROOOMTECH Decision Core",
    version=__version__,
    description="Independent structured-decision API with confidence and review gating.",
)
engine = DecisionEngine()


@app.on_event("startup")
async def startup_license_check():
    try:
        verify_runtime_license()
    except LicenseError as exc:
        raise RuntimeError(str(exc)) from exc


@app.get("/health")
async def health():
    return {"ok": True, "version": __version__}


@app.get("/v1/info")
async def info():
    try:
        license_info = verify_runtime_license()
    except LicenseError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "product": "ROOOMTECH Decision Core",
        "version": __version__,
        "model_provider_configured": engine.model.configured,
        "license": license_info,
        "persistence": "none-by-default",
    }


@app.post("/v1/decide", response_model=DecisionResponse)
async def decide(request: DecisionRequest):
    try:
        return await engine.decide(request)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"decision provider error: {exc}") from exc


@app.post("/v1/decide/batch", response_model=BatchResponse)
async def decide_batch(request: BatchRequest):
    # Concurrency is bounded by the request schema (100 items max); deployers can add gateway limits as needed.
    items = await asyncio.gather(*(engine.decide(item) for item in request.items))
    return BatchResponse(items=items)
