from __future__ import annotations

import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.web_change_management import (
    AuditRunSummary,
    ConnectorTestResult,
    LinkChangeProposal,
    ScheduleCreate,
    ScheduleSummary,
    StageAuditRequest,
    StagedAuditResponse,
    WebChangeService,
    WordPressConnectorCreate,
    WordPressConnectorSummary,
)
from app.web_intelligence import WebsiteAuditEngine, WebsiteAuditRequest, WebsiteAuditResponse
from app.web_review_ui import render_web_review_html


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
    changes = WebChangeService(engine)
    # Project-prefixed on purpose: enterprise middleware treats /v1/project/* as
    # management/project APIs, so this module applies its dedicated `web` scope.
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

        # Server-side fetching and write connectors must never become an
        # unauthenticated public proxy/control surface.
        expected = os.getenv("RTDC_ADMIN_API_KEY", "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="RTDC_ADMIN_API_KEY must be configured before enabling website intelligence outside enterprise mode",
            )
        if x_rtdc_admin_key is None or not hmac.compare_digest(x_rtdc_admin_key, expected):
            raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Admin-Key")
        return True

    def project_id_for(auth) -> str:
        return auth.project_id if auth is not True else "local"

    def audit_meta(auth, method: str, path: str, status_code: int, started: float, request: Request) -> None:
        if not _enterprise_enforced() or auth is True:
            return
        enterprise_services.store.audit(
            project_id=auth.project_id,
            key_id=auth.key_id,
            method=method,
            path=path,
            status_code=status_code,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            request_id=request.headers.get("x-request-id") or "web_" + uuid.uuid4().hex[:20],
        )

    @app.get("/web-review", response_class=HTMLResponse)
    async def web_review_ui():
        # The shell contains no tenant data. All data/API calls remain protected.
        return HTMLResponse(render_web_review_html())

    @router.post("/audit", response_model=WebsiteAuditResponse)
    async def audit_website(
        payload: WebsiteAuditRequest,
        request: Request,
        auth=Depends(require_web_access),
    ):
        started = time.perf_counter()
        status_code = 500
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
            audit_meta(auth, "POST", "/v1/project/web/audit", status_code, started, request)

    @router.post("/connectors/wordpress", response_model=WordPressConnectorSummary)
    async def create_wordpress_connector(
        payload: WordPressConnectorCreate,
        auth=Depends(require_web_access),
    ):
        try:
            return await changes.create_connector(project_id_for(auth), payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/connectors", response_model=list[WordPressConnectorSummary])
    async def list_connectors(auth=Depends(require_web_access)):
        return changes.store.list_connectors(project_id_for(auth))

    @router.post("/connectors/{connector_id}/test", response_model=ConnectorTestResult)
    async def test_connector(connector_id: str, auth=Depends(require_web_access)):
        try:
            return await changes.test_connector(project_id_for(auth), connector_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/audits/stage", response_model=StagedAuditResponse)
    async def stage_audit(
        payload: StageAuditRequest,
        request: Request,
        auth=Depends(require_web_access),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = await changes.stage_audit(project_id_for(auth), payload)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            status_code = 403
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            status_code = 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            status_code = 502
            raise HTTPException(status_code=502, detail=f"staged website audit failed: {type(exc).__name__}: {exc}") from exc
        finally:
            audit_meta(auth, "POST", "/v1/project/web/audits/stage", status_code, started, request)

    @router.get("/audit-runs", response_model=list[AuditRunSummary])
    async def list_audit_runs(
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(require_web_access),
    ):
        return changes.store.list_runs(project_id_for(auth), limit=limit)

    @router.get("/proposals", response_model=list[LinkChangeProposal])
    async def list_proposals(
        status: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
        auth=Depends(require_web_access),
    ):
        if status is not None and status not in {"pending", "approved", "rejected", "applied", "stale", "failed"}:
            raise HTTPException(status_code=400, detail="invalid proposal status")
        return changes.store.list_proposals(project_id_for(auth), status=status, limit=limit)

    @router.get("/proposals/{proposal_id}", response_model=LinkChangeProposal)
    async def get_proposal(proposal_id: str, auth=Depends(require_web_access)):
        try:
            return changes.store.get_proposal(project_id_for(auth), proposal_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/approve", response_model=LinkChangeProposal)
    async def approve_proposal(proposal_id: str, auth=Depends(require_web_access)):
        try:
            return changes.approve(project_id_for(auth), proposal_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/reject", response_model=LinkChangeProposal)
    async def reject_proposal(proposal_id: str, auth=Depends(require_web_access)):
        try:
            return changes.reject(project_id_for(auth), proposal_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/apply", response_model=LinkChangeProposal)
    async def apply_proposal(
        proposal_id: str,
        request: Request,
        auth=Depends(require_web_access),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = await changes.apply(project_id_for(auth), proposal_id)
            status_code = 200 if result.status == "applied" else 409
            if status_code != 200:
                raise HTTPException(status_code=409, detail=result.last_error or f"proposal became {result.status}")
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code = 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            audit_meta(auth, "POST", f"/v1/project/web/proposals/{proposal_id}/apply", status_code, started, request)

    @router.post("/proposals/{proposal_id}/approve-apply", response_model=LinkChangeProposal)
    async def approve_and_apply_proposal(
        proposal_id: str,
        request: Request,
        auth=Depends(require_web_access),
    ):
        # This is the explicit one-click action used after the caller has reviewed
        # the proposal/preview. Scheduled audits never call this endpoint.
        started = time.perf_counter()
        status_code = 500
        try:
            result = await changes.approve_and_apply(project_id_for(auth), proposal_id)
            status_code = 200 if result.status == "applied" else 409
            if status_code != 200:
                raise HTTPException(status_code=409, detail=result.last_error or f"proposal became {result.status}")
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code = 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            audit_meta(auth, "POST", f"/v1/project/web/proposals/{proposal_id}/approve-apply", status_code, started, request)

    @router.post("/schedules", response_model=ScheduleSummary)
    async def create_schedule(payload: ScheduleCreate, auth=Depends(require_web_access)):
        project_id = project_id_for(auth)
        try:
            changes.store.get_connector(project_id, payload.connector_id)
            return changes.store.create_schedule(project_id, payload)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/schedules", response_model=list[ScheduleSummary])
    async def list_schedules(auth=Depends(require_web_access)):
        return changes.store.list_schedules(project_id_for(auth))

    @router.delete("/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, auth=Depends(require_web_access)):
        if not changes.store.delete_schedule(project_id_for(auth), schedule_id):
            raise HTTPException(status_code=404, detail="schedule not found")
        return {"deleted": True, "schedule_id": schedule_id}

    app.include_router(router)

    @app.on_event("startup")
    async def _web_scheduler_start():
        await changes.start_scheduler()

    @app.on_event("shutdown")
    async def _web_scheduler_stop():
        await changes.stop_scheduler()
        changes.store.close()

    # Keep the existing return type/contract for main.py while making the change
    # workflow reachable for diagnostics/tests without adding another global.
    engine.change_service = changes
    return engine
