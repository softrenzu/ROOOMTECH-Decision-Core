from __future__ import annotations

import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.dynamic_candidates import (
    DynamicCandidateEngine,
    DynamicCandidateRequest,
    DynamicCandidateResponse,
)
from app.operations import OperationalDecisionEngine


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def install_dynamic_candidate_api(app, enterprise_services, operations: OperationalDecisionEngine):
    engine = DynamicCandidateEngine(operations)
    router = APIRouter(prefix="/v1/project/candidates")

    def require_access(
        x_rtdc_project_key: str | None = Header(default=None),
        x_rtdc_candidate_key: str | None = Header(default=None),
        x_rtdc_admin_key: str | None = Header(default=None),
    ):
        if _enterprise_enforced():
            try:
                return enterprise_services.store.authenticate(
                    x_rtdc_project_key or "",
                    required_scope="candidates",
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

        expected = os.getenv("RTDC_CANDIDATE_API_KEY", "").strip() or os.getenv("RTDC_ADMIN_API_KEY", "").strip()
        supplied = x_rtdc_candidate_key or x_rtdc_admin_key
        if expected and (supplied is None or not hmac.compare_digest(supplied, expected)):
            raise HTTPException(status_code=401, detail="invalid or missing candidate/admin key")
        return True

    @router.post("/select", response_model=DynamicCandidateResponse)
    async def select_candidates(
        payload: DynamicCandidateRequest,
        request: Request,
        auth=Depends(require_access),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = await engine.select(payload)
            status_code = 200
            return result
        except ValueError as exc:
            status_code = 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            status_code = 503
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            if _enterprise_enforced() and auth is not True:
                enterprise_services.store.audit(
                    project_id=auth.project_id,
                    key_id=auth.key_id,
                    method="POST",
                    path="/v1/project/candidates/select",
                    status_code=status_code,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    request_id=request.headers.get("x-request-id") or "cand_" + uuid.uuid4().hex[:20],
                )

    app.include_router(router)
    return engine
