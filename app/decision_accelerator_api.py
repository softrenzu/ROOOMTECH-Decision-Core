from __future__ import annotations

import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.decision_accelerator import (
    DecisionAccelerator,
    DecisionAcceleratorRequest,
    DecisionAcceleratorResponse,
)
from app.engine import DecisionEngine
from app.tenant_context import reset_tenant_context, set_tenant_context


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def install_decision_accelerator_api(app, enterprise_services, engine: DecisionEngine):
    accelerator = DecisionAccelerator(engine)
    router = APIRouter(prefix="/v1/project")

    def require_access(
        x_rtdc_project_key: str | None = Header(default=None),
        x_rtdc_accelerator_key: str | None = Header(default=None),
        x_rtdc_admin_key: str | None = Header(default=None),
    ):
        if _enterprise_enforced():
            try:
                return enterprise_services.store.authenticate(
                    x_rtdc_project_key or "",
                    required_scope="inference",
                    consume_quota=True,
                )
            except PermissionError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except OverflowError as exc:
                raise HTTPException(
                    status_code=429,
                    detail=str(exc),
                    headers={"Retry-After": "86400"},
                ) from exc

        expected = (
            os.getenv("RTDC_ACCELERATOR_API_KEY", "").strip()
            or os.getenv("RTDC_REALTIME_API_KEY", "").strip()
            or os.getenv("RTDC_ADMIN_API_KEY", "").strip()
        )
        supplied = x_rtdc_accelerator_key or x_rtdc_admin_key
        if expected and (supplied is None or not hmac.compare_digest(supplied, expected)):
            raise HTTPException(status_code=401, detail="invalid or missing accelerator/admin key")
        return True

    @router.post("/accelerate", response_model=DecisionAcceleratorResponse)
    async def accelerate(
        payload: DecisionAcceleratorRequest,
        request: Request,
        auth=Depends(require_access),
    ):
        started = time.perf_counter()
        status_code = 500
        tenant_token = None
        try:
            if _enterprise_enforced() and auth is not True:
                registry = enterprise_services.resource_registry
                if registry is None:
                    raise HTTPException(status_code=503, detail="tenant resource registry is unavailable")
                for judgment in payload.judgments:
                    if judgment.model_id:
                        try:
                            registry.assert_owner(auth.project_id, "model", judgment.model_id)
                        except FileNotFoundError as exc:
                            raise HTTPException(status_code=404, detail=str(exc)) from exc
                tenant_token = set_tenant_context(auth.project_id, registry)
            result = await accelerator.evaluate(payload)
            status_code = 200
            return result
        except ValueError as exc:
            status_code = 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException as exc:
            status_code = exc.status_code
            raise
        except Exception as exc:
            status_code = 502
            raise HTTPException(status_code=502, detail=f"decision accelerator error: {exc}") from exc
        finally:
            if tenant_token is not None:
                reset_tenant_context(tenant_token)
            if _enterprise_enforced() and auth is not True:
                enterprise_services.store.audit(
                    project_id=auth.project_id,
                    key_id=auth.key_id,
                    method="POST",
                    path="/v1/project/accelerate",
                    status_code=status_code,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    request_id=request.headers.get("x-request-id") or "acc_" + uuid.uuid4().hex[:20],
                )

    app.include_router(router)
    return accelerator
