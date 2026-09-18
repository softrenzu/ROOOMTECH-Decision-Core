from __future__ import annotations

import asyncio
import hmac
import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from app import __version__
from app.engine import DecisionEngine
from app.license import LicenseError, verify_runtime_license
from app.models import (
    BatchRequest,
    BatchResponse,
    DecisionRequest,
    DecisionResponse,
    LocalModelSummary,
    LocalPredictRequest,
    LocalPredictResponse,
    TrainModelRequest,
    TrainModelResponse,
)

app = FastAPI(
    title="ROOOMTECH Decision Core",
    version=__version__,
    description="Independent structured-decision API with local multilingual classifiers, GPU inference and optional LLM fallback.",
)
engine = DecisionEngine()


def require_admin(x_rtdc_admin_key: str | None = Header(default=None)):
    expected = os.getenv("RTDC_ADMIN_API_KEY", "")
    if expected and (x_rtdc_admin_key is None or not hmac.compare_digest(x_rtdc_admin_key, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Admin-Key")
    return True


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
        "local_ml": engine.local.runtime_info(),
        "license": license_info,
        "raw_input_persistence": "none-by-default",
    }


@app.get("/v1/accelerator")
async def accelerator():
    return engine.local.runtime_info()


@app.post("/v1/decide", response_model=DecisionResponse)
async def decide(request: DecisionRequest):
    try:
        return await engine.decide(request)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"decision provider error: {exc}") from exc


@app.post("/v1/decide/batch", response_model=BatchResponse)
async def decide_batch(request: BatchRequest):
    try:
        items = await asyncio.gather(*(engine.decide(item) for item in request.items))
        return BatchResponse(items=items)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"batch decision provider error: {exc}") from exc


@app.post("/v1/models/train", response_model=TrainModelResponse, dependencies=[Depends(require_admin)])
async def train_model(request: TrainModelRequest):
    try:
        return await asyncio.to_thread(engine.local.train, request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"training error: {exc}") from exc


@app.get("/v1/models", response_model=list[LocalModelSummary], dependencies=[Depends(require_admin)])
async def list_models():
    return engine.local.list_models()


@app.get("/v1/models/{model_id}", response_model=LocalModelSummary, dependencies=[Depends(require_admin)])
async def get_model(model_id: str):
    try:
        return engine.local.get_model(model_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/models/{model_id}/predict", response_model=LocalPredictResponse)
async def predict_local_model(model_id: str, request: LocalPredictRequest):
    try:
        device, predictions = await asyncio.to_thread(engine.local.predict_many, model_id, request.inputs, request.device)
        return LocalPredictResponse(model_id=model_id, device=device, predictions=predictions)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"prediction error: {exc}") from exc
