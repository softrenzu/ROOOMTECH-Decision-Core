from __future__ import annotations

import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.parallel_head_model import (
    ParallelHeadModelProvider,
    ParallelHeadModelSummary,
    ParallelHeadPredictRequest,
    ParallelHeadPredictResponse,
    TrainParallelHeadRequest,
)


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def install_parallel_head_model_api(app, enterprise_services) -> ParallelHeadModelProvider:
    provider = ParallelHeadModelProvider()
    router = APIRouter(prefix="/v1/project/parallel-models")

    def _auth(required_scope: str, consume_quota: bool):
        def dependency(
            x_rtdc_project_key: str | None = Header(default=None),
            x_rtdc_accelerator_key: str | None = Header(default=None),
            x_rtdc_admin_key: str | None = Header(default=None),
        ):
            if _enterprise_enforced():
                try:
                    return enterprise_services.store.authenticate(
                        x_rtdc_project_key or "",
                        required_scope=required_scope,
                        consume_quota=consume_quota,
                    )
                except PermissionError as exc:
                    raise HTTPException(status_code=401, detail=str(exc)) from exc
                except OverflowError as exc:
                    raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "86400"}) from exc
            expected = (
                os.getenv("RTDC_ACCELERATOR_API_KEY", "").strip()
                or os.getenv("RTDC_ADMIN_API_KEY", "").strip()
            )
            supplied = x_rtdc_accelerator_key or x_rtdc_admin_key
            if expected and (supplied is None or not hmac.compare_digest(supplied, expected)):
                raise HTTPException(status_code=401, detail="invalid or missing accelerator/admin key")
            return True
        return dependency

    model_auth = _auth("models", True)
    inference_auth = _auth("inference", True)

    def project_id(auth) -> str:
        return auth.project_id if auth is not True else "local"

    def audit(auth, method: str, path: str, status_code: int, started: float, request: Request) -> None:
        if not _enterprise_enforced() or auth is True:
            return
        enterprise_services.store.audit(
            project_id=auth.project_id,
            key_id=auth.key_id,
            method=method,
            path=path,
            status_code=status_code,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            request_id=request.headers.get("x-request-id") or "mh_" + uuid.uuid4().hex[:20],
        )

    @router.post("/train", response_model=ParallelHeadModelSummary)
    async def train_parallel_model(
        payload: TrainParallelHeadRequest,
        request: Request,
        auth=Depends(model_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = await __import__("asyncio").to_thread(provider.train, project_id(auth), payload)
            status_code = 200
            return result
        except (ValueError, RuntimeError) as exc:
            status_code = 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", "/v1/project/parallel-models/train", status_code, started, request)

    @router.get("", response_model=list[ParallelHeadModelSummary])
    async def list_parallel_models(
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(model_auth),
    ):
        return provider.list_models(project_id(auth), limit=limit)

    @router.get("/{model_id}", response_model=ParallelHeadModelSummary)
    async def get_parallel_model(model_id: str, auth=Depends(model_auth)):
        try:
            return provider.get_model(project_id(auth), model_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/{model_id}/predict", response_model=ParallelHeadPredictResponse)
    async def predict_parallel_model(
        model_id: str,
        payload: ParallelHeadPredictRequest,
        request: Request,
        auth=Depends(inference_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = await provider.predict_async(project_id(auth), model_id, payload)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            status_code = 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/parallel-models/{model_id}/predict", status_code, started, request)

    app.include_router(router)
    return provider
