from __future__ import annotations

import asyncio
import hmac
import json
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app import __version__
from app.advanced_models import (
    MapReduceJobStatus,
    MapReduceJobSubmission,
    MapReduceRequest,
    MapReduceResponse,
    RealtimeBatchRequest,
    RealtimeEvent,
    SchemaExtractionRequest,
    SchemaExtractionResponse,
)
from app.benchmark import BenchmarkRunner
from app.distributed import RedisDistributedMapReduce
from app.engine import DecisionEngine
from app.fastpath import FastPathEngine, FastPathOverloaded
from app.license import LicenseError, verify_runtime_license
from app.mapreduce import MapReduceEngine
from app.models import BatchRequest, BatchResponse, BenchmarkRequest, BenchmarkResponse, DecisionRequest, DecisionResponse, DecisionSpec, LocalModelSummary, LocalPredictRequest, LocalPredictResponse, MultimodalDecisionResponse, TrainModelRequest, TrainModelResponse
from app.multimodal import MediaFile, MultimodalDecisionEngine
from app.operation_models import DetectionRequest, DetectionResponse, FeatureExtractionRequest, FeatureExtractionResponse, RankRequest, RankResponse, RouteRequest, RouteResponse, ScoreRequest, ScoreResponse, VerificationRequest, VerificationResponse
from app.operations import OperationalDecisionEngine
from app.performance import PerformanceBenchmarker
from app.performance_models import (
    FastDecisionRequest,
    FastDecisionResponse,
    FastProfileCreate,
    FastProfileSummary,
    MapReduceLoadBenchmarkRequest,
    MapReduceLoadBenchmarkResponse,
    RealtimeLoadBenchmarkRequest,
    RealtimeLoadBenchmarkResponse,
)
from app.realtime import RealtimeDispatcher
from app.schema_extraction import SchemaExtractor

app = FastAPI(
    title="ROOOMTECH Decision Core",
    version=__version__,
    description="Independent multimodal decision API with low-latency profiles, schema extraction, Map/Reduce and realtime streaming.",
)
engine = DecisionEngine()
benchmark_runner = BenchmarkRunner(engine.local)
multimodal_engine = MultimodalDecisionEngine(engine)
operations_engine = OperationalDecisionEngine(engine)
schema_extractor = SchemaExtractor(engine.model)
mapreduce_engine = MapReduceEngine(engine, operations_engine, schema_extractor)
distributed_mapreduce = RedisDistributedMapReduce(mapreduce_engine)
fast_path = FastPathEngine(engine, operations_engine, schema_extractor)
performance_benchmarker = PerformanceBenchmarker(fast_path, mapreduce_engine)
realtime_dispatcher = RealtimeDispatcher(engine, operations_engine, schema_extractor, fast_path=fast_path)


def require_admin(x_rtdc_admin_key: str | None = Header(default=None)):
    expected = os.getenv("RTDC_ADMIN_API_KEY", "")
    if expected and (x_rtdc_admin_key is None or not hmac.compare_digest(x_rtdc_admin_key, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Admin-Key")
    return True


def require_realtime(x_rtdc_api_key: str | None = Header(default=None)):
    expected = os.getenv("RTDC_REALTIME_API_KEY", "")
    if expected and (x_rtdc_api_key is None or not hmac.compare_digest(x_rtdc_api_key, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-API-Key")
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
        "multimodal": {"image": multimodal_engine.vision.configured, "pdf": True, "audio": True},
        "operations": [
            "classification", "detection", "routing", "scoring", "verification",
            "ranking", "search", "feature_extraction", "structured_extraction",
            "mapreduce", "realtime_streaming", "realtime_fast_path", "performance_benchmarking",
        ],
        "fast_path": {
            "profile_count": len(fast_path.list_profiles()),
            "default_target_ms": 150,
            "network_free_providers": ["rules", "local_classifier", "local_ngram", "heuristic"],
            "scheduler": fast_path.runtime_info(),
        },
        "distributed_mapreduce": distributed_mapreduce.configured,
        "license": license_info,
        "raw_input_persistence": "none-by-default; distributed Redis jobs persist payloads temporarily when explicitly enabled",
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
        return BatchResponse(items=await asyncio.gather(*(engine.decide(item) for item in request.items)))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"batch decision provider error: {exc}") from exc


@app.post("/v1/extract", response_model=SchemaExtractionResponse)
async def structured_extract(request: SchemaExtractionRequest):
    try:
        return await schema_extractor.extract(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/v1/mapreduce/run", response_model=MapReduceResponse, dependencies=[Depends(require_admin)])
async def mapreduce_run(request: MapReduceRequest):
    try:
        return await mapreduce_engine.run(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"map/reduce error: {exc}") from exc


@app.post("/v1/mapreduce/stream", dependencies=[Depends(require_admin)])
async def mapreduce_stream(request: MapReduceRequest):
    async def generate():
        async for event in mapreduce_engine.stream(request):
            yield json.dumps(event, ensure_ascii=False) + "\n"
    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.post("/v1/mapreduce/jobs", response_model=MapReduceJobSubmission, dependencies=[Depends(require_admin)])
async def mapreduce_submit(request: MapReduceRequest):
    try:
        return await distributed_mapreduce.submit(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/v1/mapreduce/jobs/{job_id}", response_model=MapReduceJobStatus, dependencies=[Depends(require_admin)])
async def mapreduce_status(job_id: str):
    try:
        return await distributed_mapreduce.status(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/v1/realtime/profiles", response_model=FastProfileSummary, dependencies=[Depends(require_admin)])
async def create_fast_profile(request: FastProfileCreate):
    try:
        return await fast_path.create_profile(request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/realtime/profiles", response_model=list[FastProfileSummary], dependencies=[Depends(require_admin)])
async def list_fast_profiles():
    return fast_path.list_profiles()


@app.delete("/v1/realtime/profiles/{profile_id}", dependencies=[Depends(require_admin)])
async def delete_fast_profile(profile_id: str):
    deleted = await fast_path.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"fast profile not found: {profile_id}")
    return {"deleted": True, "profile_id": profile_id}


@app.post("/v1/realtime/fast", response_model=FastDecisionResponse, dependencies=[Depends(require_realtime)])
async def realtime_fast(request: FastDecisionRequest):
    try:
        return await fast_path.execute(request)
    except FastPathOverloaded as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "1"}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/realtime/stream", dependencies=[Depends(require_realtime)])
async def realtime_stream(request: RealtimeBatchRequest):
    async def generate():
        async for result in realtime_dispatcher.stream_batch(request):
            yield result.model_dump_json() + "\n"
    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.websocket("/v1/realtime/ws")
async def realtime_ws(websocket: WebSocket):
    expected = os.getenv("RTDC_REALTIME_API_KEY", "")
    supplied = websocket.headers.get("x-rtdc-api-key", "")
    if expected and not hmac.compare_digest(expected, supplied):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    max_bytes = int(os.getenv("RTDC_WS_MAX_MESSAGE_BYTES", "1048576"))
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > max_bytes:
                await websocket.close(code=1009)
                return
            try:
                event = RealtimeEvent.model_validate_json(raw)
                result = await realtime_dispatcher.dispatch(event)
                await websocket.send_text(result.model_dump_json())
            except Exception as exc:
                await websocket.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    except WebSocketDisconnect:
        return


@app.post("/v1/ops/detect", response_model=DetectionResponse)
async def operation_detect(request: DetectionRequest):
    return await operations_engine.detect(request)


@app.post("/v1/ops/route", response_model=RouteResponse)
async def operation_route(request: RouteRequest):
    return await operations_engine.route(request)


@app.post("/v1/ops/score", response_model=ScoreResponse)
async def operation_score(request: ScoreRequest):
    return await operations_engine.score(request)


@app.post("/v1/ops/verify", response_model=VerificationResponse)
async def operation_verify(request: VerificationRequest):
    return await operations_engine.verify(request)


@app.post("/v1/ops/rank", response_model=RankResponse)
async def operation_rank(request: RankRequest):
    return await operations_engine.rank(request)


@app.post("/v1/ops/search", response_model=RankResponse)
async def operation_search(request: RankRequest):
    return await operations_engine.rank(request)


@app.post("/v1/ops/features", response_model=FeatureExtractionResponse)
async def operation_features(request: FeatureExtractionRequest):
    return await operations_engine.extract_features(request)


@app.post("/v1/multimodal/decide", response_model=MultimodalDecisionResponse)
async def decide_multimodal(decisions_json: str = Form(...), text: str = Form(default=""), provider: str = Form(default="auto"), image_weight: float = Form(default=0.55), files: list[UploadFile] | None = File(default=None)):
    try:
        raw_specs = json.loads(decisions_json)
        decisions = [DecisionSpec.model_validate(item) for item in raw_specs]
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
            media_files.append(MediaFile(filename=upload.filename or "upload.bin", content_type=upload.content_type or "application/octet-stream", data=data))
        return await multimodal_engine.decide(text, decisions, provider, media_files, image_weight)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.post("/v1/benchmarks/local", response_model=BenchmarkResponse, dependencies=[Depends(require_admin)])
async def benchmark_local(request: BenchmarkRequest):
    try:
        return await asyncio.to_thread(benchmark_runner.run_local, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/benchmarks/realtime-fast", response_model=RealtimeLoadBenchmarkResponse, dependencies=[Depends(require_admin)])
async def benchmark_realtime_fast(request: RealtimeLoadBenchmarkRequest):
    try:
        return await performance_benchmarker.benchmark_realtime(request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"realtime benchmark error: {exc}") from exc


@app.post("/v1/benchmarks/mapreduce-load", response_model=MapReduceLoadBenchmarkResponse, dependencies=[Depends(require_admin)])
async def benchmark_mapreduce_load(request: MapReduceLoadBenchmarkRequest):
    try:
        return await performance_benchmarker.benchmark_mapreduce(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"map/reduce benchmark error: {exc}") from exc
