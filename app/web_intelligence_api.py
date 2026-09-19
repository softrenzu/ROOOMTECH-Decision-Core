from __future__ import annotations

import hmac
import os

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
    router = APIRouter(prefix="/v1/web")

    def require_web_access(
        request: Request,
        x_rtdc_admin_key: str | None = Header(default=None),
    ):
        if not _enabled():
            raise HTTPException(
                status_code=503,
                detail="website intelligence is disabled; set RTDC_WEB_INTELLIGENCE_ENABLED=true",
            )

        # In enterprise mode the global project middleware authenticates the request,
        # enforces the dedicated web scope, consumes quota and writes metadata-only audit.
        if _enterprise_enforced():
            auth = getattr(request.state, "rtdc_project", None)
            if auth is None:
                raise HTTPException(status_code=401, detail="project authentication required")
            return auth

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
        _=Depends(require_web_access),
    ):
        try:
            return await engine.audit(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"website audit failed: {type(exc).__name__}: {exc}",
            ) from exc

    app.include_router(router)
    return engine
