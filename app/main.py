from __future__ import annotations

import asyncio
import hmac
import json
import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from app import __version__
from app.benchmark import BenchmarkRunner
from app.engine import DecisionEngine
from app.license import LicenseError, verify_runtime_license
from app.models import (
    BatchRequest,
    BatchResponse,
    BenchmarkRequest,
    BenchmarkResponse,
    DecisionRequest,
    DecisionResponse,
    DecisionSpec,
    LocalModelSummary,
    LocalPredictRequest,
    LocalPredictResponse,
    MultimodalDecisionResponse,
    TrainModelRequest,
    TrainModelResponse,
)
from app.multimodal import MediaFile, MultimodalDecisionEngine
from app.operation_models import (
    DetectionRequest,
    DetectionResponse,
    FeatureExtractionRequest,
    FeatureExtractionResponse,
    RankRequest,
    RankResponse,
    RouteRequest,
    RouteResponse,
    ScoreRequest,
    ScoreResponse,
    VerificationRequest,
    VerificationResponse,
)
from app.operations import OperationalDecisionEngine

app = FastAPI(
    title="ROOOMTECH Decision Core",
    version=__version__,
    description="Independent multimodal structured-decision API with reusable decision operations.",
)
engine = DecisionEngine()
benchmark_runner = BenchmarkRunner(engine.local)
multimodal_engine = MultimodalDecisionEngine(engine)
operations_engine = OperationalDecisionEngine(engine)


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
        "multimodal": {
            "image": multimodal_engine.vision.configured,
            "pdf": True,
            "audio": True,
        },
        "operations": ["classification", "detection", "routing", "scoring", "verification", "ranking", "search", "feature_extraction"],
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


@app.post("/v1/ops/detect", response_model=DetectionResponse)
async def operation_detect(request: DetectionRequest):
    try:
        return await operations_engine.detect(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"detection error: {exc}") from exc


@app.post("/v1/ops/route", response_model=RouteResponse)
async def operation_route(request: RouteRequest):
    try:
        return await operations_engine.route(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"routing error: {exc}") from exc


@app.post("/v1/ops/score", response_model=ScoreResponse)
async def operation_score(request: ScoreRequest):
    try:
        return await operations_engine.score(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"scoring error: {exc}") from exc


@app.post("/v1/ops/verify", response_model=VerificationResponse)
async def operation_verify(request: VerificationRequest):
    try:
        return await operations_engine.verify(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"verification error: {exc}") from exc


@app.post("/v1/ops/rank", response_model=RankResponse)
async def operation_rank(request: RankRequest):
    try:
        return await operations_engine.rank(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ranking error: {exc}") from exc


@app.post("/v1/ops/search", response_model=RankResponse)
async def operation_search(request: RankRequest):
    return await operation_rank(request)


@app.post("/v1/ops/features", response_model=FeatureExtractionResponse)
async def operation_features(request: FeatureExtractionRequest):
    try:
        return await operations_engine.extract_features(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"feature extraction error: {exc}") from exc


@app.post("/v1/multimodal/decide", response_model=MultimodalDecisionResponse)
async def decide_multimodal(
    decisions_json: str = Form(...),
    text: str = Form(default=""),
    provider: str = Form(default="auto"),
    image_weight: float = Form(default=0.55),
    files: list[UploadFile] | None = File(default=None),
):
    try:
        if provider not in {"auto", "rules", "local_classifier", "openai_compatible"}:
            raise ValueError("invalid provider")
        if not 0.0 <= image_weight <= 1.0:
            raise ValueError("image_weight must be between 0 and 1")
        raw_specs = json.loads(decisions_json)
        if not isinstance(raw_specs, list):
            raise ValueError("decisions_json must be a JSON array")
        decisions = [DecisionSpec.model_validate(item) for item in raw_specs]
        if not decisions or len(decisions) > 30:
            raise ValueError("decisions must contain 1 to 30 items")

        max_file = int(os.getenv("RTDC_MAX_FILE_MB", "20")) * 1024 * 1024
        max_total = int(os.getenv("RTDC_MAX_TOTAL_UPLOAD_MB", "50")) * 1024 * 1024
        media_files: list[MediaFile] = []
        total = 0
        for upload in files or []:
            data = await upload.read(max_file + 1)
            if len(data) > max_file:
                raise ValueError(f"file too large: {upload.filename}")
            total += len(data)
            if total > max_total:
                raise ValueError("total upload size exceeds configured limit")
            media_files.append(MediaFile(
                filename=upload.filename or "upload.bin",
                content_type=upload.content_type or "application/octet-stream",
                data=data,
            ))

        return await multimodal_engine.decide(text, decisions, provider, media_files, image_weight)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"multimodal decision error: {exc}") from exc


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


@app.post("/v1/benchmarks/local", response_model=BenchmarkResponse, dependencies=[Depends(require_admin)])
async def benchmark_local(request: BenchmarkRequest):
    try:
        return await asyncio.to_thread(benchmark_runner.run_local, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"benchmark error: {exc}") from exc
