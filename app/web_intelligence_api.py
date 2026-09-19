from __future__ import annotations

import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.web_intelligence import WebsiteAuditEngine, WebsiteAuditRequest, WebsiteAuditResponse


def _enabled() -> bool:
    return os.getenv("RTDC_WEB_INTELLIGENCE_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def install_web_intelligence_api(app, enterprise_services) -> WebsiteAuditEngine:
    engine = WebsiteAuditEngine()
    # Project-prefixed on purpose: enterprise middleware treats /v1/project/* as
    # management/project APIs, so this module can apply its dedicated `web` scope
    # without also requiring the generic inference scope.
    router = APIRouter(prefix="/v1/project/web")

    def require_web_access(
        x_rtdc_project_key: str | None = Header(default=None),
        x_rtdc_admin_key: str | None = Header(default=None),
    ):
        if not _enabled():
            raise HTTPException(
                status_code=503,
                detail="website intelligence is disabled; set RTDC_WEB_INTELLIGENCE_ENABLED=true",
            )

        if _enterprise_enforced():
            try:
                return enterprise_services.store.authenticate(
                    x_rtdc_project_key or "",
                    required_scope="web",
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

        # Server-side fetching should never become an unauthenticated public proxy.
        expected = os.getenv("RTDC_ADMIN_API_KEY", "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="RTDC_ADMIN_API_KEY must be configured before enabling website intelligence outside enterprise mode",
            )
        if x_rtdc_admin_key is None or not hmac.compare_digest(x_rtdc_admin_key, expected):
            raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Admin-Key")
        return True

    @router.post("/audit", response_model=WebsiteAuditResponse)
    async def audit_website(
        payload: WebsiteAuditRequest,
        request: Request,
        auth=Depends(require_web_access),
    ):
        started = time.perf_counter()
        status_code = 500
        request_id = request.headers.get("x-request-id") or "web_" + uuid.uuid4().hex[:20]
        try:
            result = await engine.audit(payload)
            status_code = 200
            return result
        except ValueError as exc:
            status_code = 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            status_code = 502
            raise HTTPException(
                status_code=502,
                detail=f"website audit failed: {type(exc).__name__}: {exc}",
            ) from exc
        finally:
            if _enterprise_enforced() and auth is not True:
                enterprise_services.store.audit(
                    project_id=auth.project_id,
                    key_id=auth.key_id,
                    method="POST",
                    path="/v1/project/web/audit",
                    status_code=status_code,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    request_id=request_id,
                )

    app.include_router(router)
    return engine
