from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status

from app.candidate_bulk_import import (
    CandidateBulkImportFailure,
    CandidateBulkImportJob,
    CandidateBulkImportRetryResponse,
    CandidateBulkImportServices,
    CandidateBulkImportStore,
    CandidateBulkImportWorker,
)
from app.candidate_catalog import CandidateCatalogStore
from app.candidate_catalog_ops import CandidateCatalogSnapshotManager


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _source_format(requested: str, filename: str | None) -> Literal["csv", "jsonl"]:
    value = requested.strip().lower()
    if value in {"csv", "jsonl"}:
        return value  # type: ignore[return-value]
    if value != "auto":
        raise ValueError("format must be auto, csv or jsonl")
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in {".jsonl", ".ndjson"}:
        return "jsonl"
    raise ValueError("could not infer import format; use format=csv or format=jsonl")


def install_candidate_bulk_import_api(app, enterprise_services):
    jobs = CandidateBulkImportStore()
    catalog_store = CandidateCatalogStore()
    snapshots = CandidateCatalogSnapshotManager(catalog_store)
    worker = CandidateBulkImportWorker(jobs, catalog_store, snapshots)
    services = CandidateBulkImportServices(jobs, catalog_store, worker)
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
    submit_auth = _auth(consume_quota=True)

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
            request_id=request.headers.get("x-request-id") or "import_" + uuid.uuid4().hex[:20],
        )

    @router.post(
        "/{catalog_id}/imports",
        response_model=CandidateBulkImportJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_import(
        catalog_id: str,
        request: Request,
        file: UploadFile = File(...),
        format: str = Form(default="auto"),
        batch_size: int = Form(default=1000, ge=1, le=5000),
        max_rows: int = Form(default=1_000_000, ge=1),
        on_error: Literal["continue", "stop"] = Form(default="continue"),
        snapshot_before_import: bool = Form(default=True),
        auth=Depends(submit_auth),
    ):
        started = time.perf_counter()
        status_code_value = 500
        source_path = None
        try:
            pid = project_id(auth)
            catalog_store.get_catalog(pid, catalog_id)
            configured_max_rows = int(os.getenv("RTDC_CANDIDATE_IMPORT_MAX_ROWS", "1000000"))
            if max_rows > configured_max_rows:
                raise HTTPException(
                    status_code=413,
                    detail=f"max_rows exceeds configured limit of {configured_max_rows}",
                )
            source_format = _source_format(format, file.filename)
            max_bytes = int(os.getenv("RTDC_CANDIDATE_IMPORT_MAX_BYTES", str(1024 * 1024 * 1024)))
            source_path = jobs.allocate_source_path(Path(file.filename or "").suffix)
            digest = hashlib.sha256()
            written = 0
            with source_path.open("wb") as handle:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(status_code=413, detail="bulk import file exceeds configured byte limit")
                    digest.update(chunk)
                    handle.write(chunk)
            if written == 0:
                raise HTTPException(status_code=400, detail="bulk import file is empty")
            result = jobs.create_job(
                project_id=pid,
                catalog_id=catalog_id,
                source_path=str(source_path),
                source_format=source_format,
                original_filename=file.filename,
                source_sha256=digest.hexdigest(),
                source_bytes=written,
                batch_size=batch_size,
                max_rows=max_rows,
                on_error=on_error,
                snapshot_before_import=snapshot_before_import,
            )
            status_code_value = 202
            return result
        except FileNotFoundError as exc:
            status_code_value = 404
            if source_path is not None:
                source_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code_value = 400
            if source_path is not None:
                source_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException as exc:
            status_code_value = exc.status_code
            if source_path is not None:
                source_path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/imports", status_code_value, started, request)

    @router.get("/{catalog_id}/imports", response_model=list[CandidateBulkImportJob])
    async def list_imports(
        catalog_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(manage_auth),
    ):
        pid = project_id(auth)
        try:
            catalog_store.get_catalog(pid, catalog_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return jobs.list_jobs(pid, catalog_id, limit=limit)

    @router.get("/{catalog_id}/imports/{job_id}", response_model=CandidateBulkImportJob)
    async def get_import(catalog_id: str, job_id: str, auth=Depends(manage_auth)):
        try:
            result = jobs.get_job(project_id(auth), job_id)
            if result.catalog_id != catalog_id:
                raise FileNotFoundError("candidate import job not found")
            return result
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get(
        "/{catalog_id}/imports/{job_id}/failures",
        response_model=list[CandidateBulkImportFailure],
    )
    async def list_import_failures(
        catalog_id: str,
        job_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        auth=Depends(manage_auth),
    ):
        try:
            result = jobs.get_job(project_id(auth), job_id)
            if result.catalog_id != catalog_id:
                raise FileNotFoundError("candidate import job not found")
            return jobs.list_failures(project_id(auth), job_id, limit=limit, offset=offset)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _control(action: str, catalog_id: str, job_id: str, request: Request, auth):
        started = time.perf_counter()
        status_code_value = 500
        try:
            pid = project_id(auth)
            current = jobs.get_job(pid, job_id)
            if current.catalog_id != catalog_id:
                raise FileNotFoundError("candidate import job not found")
            fn = getattr(jobs, action)
            result = fn(pid, job_id)
            status_code_value = 200
            return result
        except FileNotFoundError as exc:
            status_code_value = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code_value = 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            audit(
                auth,
                "POST",
                f"/v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/{action}",
                status_code_value,
                started,
                request,
            )

    @router.post("/{catalog_id}/imports/{job_id}/pause", response_model=CandidateBulkImportJob)
    async def pause_import(catalog_id: str, job_id: str, request: Request, auth=Depends(manage_auth)):
        return await _control("pause", catalog_id, job_id, request, auth)

    @router.post("/{catalog_id}/imports/{job_id}/resume", response_model=CandidateBulkImportJob)
    async def resume_import(catalog_id: str, job_id: str, request: Request, auth=Depends(manage_auth)):
        return await _control("resume", catalog_id, job_id, request, auth)

    @router.post("/{catalog_id}/imports/{job_id}/cancel", response_model=CandidateBulkImportJob)
    async def cancel_import(catalog_id: str, job_id: str, request: Request, auth=Depends(manage_auth)):
        return await _control("cancel", catalog_id, job_id, request, auth)

    @router.post(
        "/{catalog_id}/imports/{job_id}/retry-failed",
        response_model=CandidateBulkImportRetryResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_failed(catalog_id: str, job_id: str, request: Request, auth=Depends(submit_auth)):
        started = time.perf_counter()
        status_code_value = 500
        try:
            pid = project_id(auth)
            current = jobs.get_job(pid, job_id)
            if current.catalog_id != catalog_id:
                raise FileNotFoundError("candidate import job not found")
            retry = jobs.create_retry_job(pid, job_id)
            status_code_value = 202
            return CandidateBulkImportRetryResponse(source_job_id=job_id, retry_job=retry)
        except FileNotFoundError as exc:
            status_code_value = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code_value = 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            audit(
                auth,
                "POST",
                f"/v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/retry-failed",
                status_code_value,
                started,
                request,
            )

    app.include_router(router)

    @app.on_event("startup")
    async def _candidate_bulk_import_start():
        await worker.start()

    @app.on_event("shutdown")
    async def _candidate_bulk_import_stop():
        await services.close()

    return services
