from __future__ import annotations

import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.candidate_catalog import (
    CandidateCatalogCreate,
    CandidateCatalogDeleteItemsRequest,
    CandidateCatalogEngine,
    CandidateCatalogMutationResponse,
    CandidateCatalogSearchRequest,
    CandidateCatalogSearchResponse,
    CandidateCatalogStore,
    CandidateCatalogSummary,
    CandidateCatalogUpsertRequest,
)
from app.candidate_catalog_ops import (
    CandidateCatalogIntegrityReport,
    CandidateCatalogRebuildResponse,
    CandidateCatalogSnapshotCreate,
    CandidateCatalogSnapshotManager,
    CandidateCatalogSnapshotSummary,
)
from app.operations import OperationalDecisionEngine


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def install_candidate_catalog_api(app, enterprise_services, operations: OperationalDecisionEngine):
    store = CandidateCatalogStore()
    engine = CandidateCatalogEngine(store, operations)
    snapshots = CandidateCatalogSnapshotManager(store)
    router = APIRouter(prefix="/v1/project/candidate-catalogs")

    def _auth(*, consume_quota: bool = False):
        def dependency(
            x_rtdc_project_key: str | None = Header(default=None),
            x_rtdc_candidate_key: str | None = Header(default=None),
            x_rtdc_admin_key: str | None = Header(default=None),
        ):
            if _enterprise_enforced():
                try:
                    return enterprise_services.store.authenticate(
                        x_rtdc_project_key or "",
                        required_scope="candidates",
                        consume_quota=consume_quota,
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

        return dependency

    manage_auth = _auth(consume_quota=False)
    search_auth = _auth(consume_quota=True)

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
            request_id=request.headers.get("x-request-id") or "catalog_" + uuid.uuid4().hex[:20],
        )

    @router.post("", response_model=CandidateCatalogSummary)
    async def create_catalog(payload: CandidateCatalogCreate, request: Request, auth=Depends(manage_auth)):
        started = time.perf_counter()
        status_code = 500
        try:
            result = store.create_catalog(project_id(auth), payload)
            status_code = 200
            return result
        except FileExistsError as exc:
            status_code = 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", "/v1/project/candidate-catalogs", status_code, started, request)

    @router.get("", response_model=list[CandidateCatalogSummary])
    async def list_catalogs(
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(manage_auth),
    ):
        return store.list_catalogs(project_id(auth), limit=limit)

    @router.get("/{catalog_id}", response_model=CandidateCatalogSummary)
    async def get_catalog(catalog_id: str, auth=Depends(manage_auth)):
        try:
            return store.get_catalog(project_id(auth), catalog_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/{catalog_id}")
    async def delete_catalog(catalog_id: str, request: Request, auth=Depends(manage_auth)):
        started = time.perf_counter()
        status_code = 500
        try:
            store.delete_catalog(project_id(auth), catalog_id)
            status_code = 200
            return {"deleted": True, "catalog_id": catalog_id}
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            audit(auth, "DELETE", f"/v1/project/candidate-catalogs/{catalog_id}", status_code, started, request)

    @router.put("/{catalog_id}/items", response_model=CandidateCatalogMutationResponse)
    async def upsert_items(
        catalog_id: str,
        payload: CandidateCatalogUpsertRequest,
        request: Request,
        auth=Depends(manage_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = store.upsert_items(project_id(auth), catalog_id, payload)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            audit(auth, "PUT", f"/v1/project/candidate-catalogs/{catalog_id}/items", status_code, started, request)

    @router.post("/{catalog_id}/items/delete", response_model=CandidateCatalogMutationResponse)
    async def delete_items(
        catalog_id: str,
        payload: CandidateCatalogDeleteItemsRequest,
        request: Request,
        auth=Depends(manage_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = store.delete_items(project_id(auth), catalog_id, payload)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/items/delete", status_code, started, request)

    @router.post("/{catalog_id}/search", response_model=CandidateCatalogSearchResponse)
    async def search_catalog(
        catalog_id: str,
        payload: CandidateCatalogSearchRequest,
        request: Request,
        auth=Depends(search_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = await engine.search(project_id(auth), catalog_id, payload)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            status_code = 503
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/search", status_code, started, request)

    @router.post("/{catalog_id}/snapshots", response_model=CandidateCatalogSnapshotSummary)
    async def create_snapshot(
        catalog_id: str,
        payload: CandidateCatalogSnapshotCreate,
        request: Request,
        auth=Depends(manage_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = snapshots.create_snapshot(project_id(auth), catalog_id, payload)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/snapshots", status_code, started, request)

    @router.get("/{catalog_id}/snapshots", response_model=list[CandidateCatalogSnapshotSummary])
    async def list_snapshots(
        catalog_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(manage_auth),
    ):
        try:
            return snapshots.list_snapshots(project_id(auth), catalog_id, limit=limit)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/{catalog_id}/snapshots/{snapshot_id}")
    async def delete_snapshot(
        catalog_id: str,
        snapshot_id: str,
        request: Request,
        auth=Depends(manage_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            snapshots.delete_snapshot(project_id(auth), catalog_id, snapshot_id)
            status_code = 200
            return {"deleted": True, "catalog_id": catalog_id, "snapshot_id": snapshot_id}
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            audit(auth, "DELETE", f"/v1/project/candidate-catalogs/{catalog_id}/snapshots/{snapshot_id}", status_code, started, request)

    @router.post("/{catalog_id}/snapshots/{snapshot_id}/restore", response_model=CandidateCatalogRebuildResponse)
    async def restore_snapshot(
        catalog_id: str,
        snapshot_id: str,
        request: Request,
        auth=Depends(manage_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = snapshots.restore_snapshot(project_id(auth), catalog_id, snapshot_id)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/snapshots/{snapshot_id}/restore", status_code, started, request)

    @router.get("/{catalog_id}/integrity", response_model=CandidateCatalogIntegrityReport)
    async def catalog_integrity(catalog_id: str, auth=Depends(manage_auth)):
        try:
            return snapshots.integrity(project_id(auth), catalog_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/{catalog_id}/rebuild", response_model=CandidateCatalogRebuildResponse)
    async def rebuild_catalog_index(
        catalog_id: str,
        request: Request,
        auth=Depends(manage_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        try:
            result = snapshots.rebuild_index(project_id(auth), catalog_id)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/rebuild", status_code, started, request)

    app.include_router(router)

    @app.on_event("shutdown")
    async def _candidate_catalog_close():
        store.close()

    return engine
