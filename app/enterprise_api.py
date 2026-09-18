from __future__ import annotations

import asyncio
import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.enterprise_models import (
    AuditEvent,
    DeployedPredictRequest,
    DeployedPredictResponse,
    ModelDeploymentSummary,
    ModelPromotionRequest,
    ModelRollbackRequest,
    ModelRollbackResponse,
    ProjectCreate,
    ProjectKeyCreate,
    ProjectKeyIssued,
    ProjectKeySummary,
    ProjectSummary,
    ProjectUpdate,
)
from app.enterprise_store import EnterpriseStore


def _require_enterprise_admin(x_rtdc_admin_key: str | None = Header(default=None)) -> bool:
    expected = os.getenv("RTDC_ADMIN_API_KEY", "").strip()
    if expected and (x_rtdc_admin_key is None or not hmac.compare_digest(x_rtdc_admin_key, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Admin-Key")
    return True


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _scope_for_path(path: str) -> str:
    if path.startswith("/v1/guardrails"):
        return "guardrails"
    if path.startswith("/v1/realtime"):
        return "realtime"
    return "inference"


def _is_management_path(path: str) -> bool:
    prefixes = (
        "/v1/admin/",
        "/v1/project/",
        "/v1/info",
        "/v1/accelerator",
        "/v1/models",
        "/v1/datasets",
        "/v1/reviews",
        "/v1/active-learning",
        "/v1/evals",
        "/v1/benchmarks",
        "/v1/mapreduce",
        "/v1/realtime/profiles",
    )
    return path.startswith(prefixes)


class EnterpriseServices:
    def __init__(self, local_model_provider):
        self.store = EnterpriseStore()
        self.local = local_model_provider

    def validate_model_for_decision(self, model_id: str, decision_id: str) -> None:
        model = self.local.get_model(model_id)
        if model.decision_id != decision_id:
            raise ValueError(
                f"model decision_id mismatch: model={model.decision_id}, requested={decision_id}"
            )


def install_enterprise_api(app, local_model_provider) -> EnterpriseServices:
    services = EnterpriseServices(local_model_provider)
    admin = APIRouter(prefix="/v1/admin", dependencies=[Depends(_require_enterprise_admin)])

    @app.middleware("http")
    async def enterprise_project_auth_and_audit(request: Request, call_next):
        path = request.url.path
        if not _enterprise_enforced() or not path.startswith("/v1/") or _is_management_path(path):
            return await call_next(request)

        token = request.headers.get("x-rtdc-project-key", "")
        required_scope = _scope_for_path(path)
        try:
            auth = services.store.authenticate(token, required_scope=required_scope, consume_quota=True)
        except PermissionError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})
        except OverflowError as exc:
            return JSONResponse(status_code=429, content={"detail": str(exc)}, headers={"Retry-After": "86400"})

        request.state.rtdc_project = auth
        request_id = request.headers.get("x-request-id") or "req_" + uuid.uuid4().hex[:20]
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-RTDC-Project-ID"] = auth.project_id
            response.headers["X-Request-ID"] = request_id
            response.headers["X-RTDC-Quota-Remaining"] = str(max(0, auth.request_quota_per_day - auth.requests_today))
            return response
        finally:
            services.store.audit(
                project_id=auth.project_id,
                key_id=auth.key_id,
                method=request.method,
                path=path,
                status_code=status_code,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                request_id=request_id,
            )

    @admin.post("/projects", response_model=ProjectSummary)
    async def create_project(request: ProjectCreate):
        return services.store.create_project(request)

    @admin.get("/projects", response_model=list[ProjectSummary])
    async def list_projects(limit: int = Query(default=100, ge=1, le=1000)):
        return services.store.list_projects(limit=limit)

    @admin.get("/projects/{project_id}", response_model=ProjectSummary)
    async def get_project(project_id: str):
        try:
            return services.store.get_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @admin.patch("/projects/{project_id}", response_model=ProjectSummary)
    async def update_project(project_id: str, request: ProjectUpdate):
        try:
            return services.store.update_project(project_id, request)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @admin.post("/projects/{project_id}/keys", response_model=ProjectKeyIssued)
    async def issue_project_key(project_id: str, request: ProjectKeyCreate):
        try:
            return services.store.issue_key(project_id, request)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @admin.get("/projects/{project_id}/keys", response_model=list[ProjectKeySummary])
    async def list_project_keys(project_id: str, limit: int = Query(default=100, ge=1, le=1000)):
        try:
            return services.store.list_keys(project_id, limit=limit)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @admin.delete("/projects/{project_id}/keys/{key_id}")
    async def revoke_project_key(project_id: str, key_id: str):
        if not services.store.revoke_key(project_id, key_id):
            raise HTTPException(status_code=404, detail="active project key not found")
        return {"revoked": True, "project_id": project_id, "key_id": key_id}

    @admin.get("/audit", response_model=list[AuditEvent])
    async def list_audit(project_id: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=5000)):
        return services.store.list_audit(project_id=project_id, limit=limit)

    @admin.post("/projects/{project_id}/models/promote", response_model=ModelDeploymentSummary)
    async def promote_model(project_id: str, request: ModelPromotionRequest):
        try:
            services.validate_model_for_decision(request.model_id, request.decision_id)
            return services.store.promote_model(
                project_id,
                request.decision_id,
                request.environment,
                request.model_id,
                None,
                request.note,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @admin.get("/projects/{project_id}/models", response_model=list[ModelDeploymentSummary])
    async def list_model_deployments(project_id: str):
        try:
            return services.store.list_deployments(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @admin.post("/projects/{project_id}/models/rollback", response_model=ModelRollbackResponse)
    async def rollback_model(project_id: str, request: ModelRollbackRequest):
        try:
            deployment, old_model = services.store.rollback_model(project_id, request.decision_id, request.environment, None)
            return ModelRollbackResponse(deployment=deployment, rolled_back_from_model_id=old_model)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    app.include_router(admin)

    project = APIRouter(prefix="/v1/project")

    def project_auth(x_rtdc_project_key: str | None = Header(default=None)):
        try:
            return services.store.authenticate(x_rtdc_project_key or "", required_scope=None, consume_quota=False)
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def project_inference_auth(x_rtdc_project_key: str | None = Header(default=None)):
        try:
            return services.store.authenticate(x_rtdc_project_key or "", required_scope="inference", consume_quota=True)
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "86400"}) from exc

    @project.get("/deployments", response_model=list[ModelDeploymentSummary])
    async def project_deployments(auth=Depends(project_auth)):
        return services.store.list_deployments(auth.project_id)

    @project.post("/predict", response_model=DeployedPredictResponse)
    async def project_deployed_predict(payload: DeployedPredictRequest, request: Request, auth=Depends(project_inference_auth)):
        request_id = request.headers.get("x-request-id") or "req_" + uuid.uuid4().hex[:20]
        started = time.perf_counter()
        status_code = 500
        try:
            deployment = services.store.get_deployment(auth.project_id, payload.decision_id, payload.environment)
            services.validate_model_for_decision(deployment.model_id, payload.decision_id)
            device, predictions = await asyncio.to_thread(
                services.local.predict_many,
                deployment.model_id,
                [payload.input],
                payload.device,
            )
            status_code = 200
            return DeployedPredictResponse(
                project_id=auth.project_id,
                decision_id=payload.decision_id,
                environment=payload.environment,
                deployment_version=deployment.version,
                model_id=deployment.model_id,
                device=device,
                prediction=predictions[0],
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
                quota_remaining_today=max(0, auth.request_quota_per_day - auth.requests_today),
            )
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            status_code = 502
            raise HTTPException(status_code=502, detail=f"deployed model inference error: {exc}") from exc
        finally:
            services.store.audit(
                project_id=auth.project_id,
                key_id=auth.key_id,
                method="POST",
                path="/v1/project/predict",
                status_code=status_code,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                request_id=request_id,
            )

    app.include_router(project)

    @app.on_event("shutdown")
    async def _enterprise_shutdown():
        services.store.close()

    return services
