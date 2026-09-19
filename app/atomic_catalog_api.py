from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status

from app.atomic_catalog import (
    AtomicCatalogActivation,
    AtomicCatalogActive,
    AtomicCatalogGeneration,
    AtomicCatalogServices,
)
from app.operations import OperationalDecisionEngine


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
    raise ValueError("could not infer generation format; use format=csv or format=jsonl")


def install_atomic_catalog_api(app, enterprise_services, operations: OperationalDecisionEngine) -> AtomicCatalogServices:
    services = AtomicCatalogServices(operations)
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
    build_auth = _auth(consume_quota=True)

    def project_id(auth) -> str:
        return auth.project_id if auth is not True else "local"

    def audit(auth, method: str, path: str, status_code_value: int, started: float, request: Request) -> None:
        if not _enterprise_enforced() or auth is True:
            return
        enterprise_services.store.audit(
            project_id=auth.project_id,
            key_id=auth.key_id,
            method=method,
            path=path,
            status_code=status_code_value,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            request_id=request.headers.get("x-request-id") or "atomic_" + uuid.uuid4().hex[:20],
        )

    @router.post(
        "/{catalog_id}/generations",
        response_model=AtomicCatalogGeneration,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_generation(
        catalog_id: str,
        request: Request,
        file: UploadFile = File(...),
        format: str = Form(default="auto"),
        batch_size: int = Form(default=5000, ge=100, le=25000),
        max_rows: int = Form(default=1_000_000, ge=1),
        feature_dim: int = Form(default=32768, ge=1024, le=262144),
        ngram_min: int = Form(default=2, ge=1, le=5),
        ngram_max: int = Form(default=4, ge=1, le=6),
        auto_activate: bool = Form(default=True),
        auth=Depends(build_auth),
    ):
        started = time.perf_counter()
        status_code_value = 500
        temp_path: Path | None = None
        source_uri: str | None = None
        try:
            if ngram_max < ngram_min:
                raise ValueError("ngram_max must be >= ngram_min")
            configured_max_rows = int(os.getenv("RTDC_ATOMIC_CATALOG_MAX_ROWS", "1000000"))
            if max_rows > configured_max_rows:
                raise HTTPException(status_code=413, detail=f"max_rows exceeds configured limit of {configured_max_rows}")
            source_format = _source_format(format, file.filename)
            max_bytes = int(os.getenv("RTDC_ATOMIC_SOURCE_MAX_BYTES", str(1024 * 1024 * 1024)))
            upload_dir = Path(os.getenv("RTDC_ATOMIC_UPLOAD_DIR", "data/atomic-upload"))
            upload_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".csv" if source_format == "csv" else ".jsonl"
            with tempfile.NamedTemporaryFile(delete=False, dir=upload_dir, suffix=suffix) as handle:
                temp_path = Path(handle.name)
                digest = hashlib.sha256()
                written = 0
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(status_code=413, detail="atomic catalog source exceeds configured byte limit")
                    digest.update(chunk)
                    handle.write(chunk)
            if written == 0:
                raise HTTPException(status_code=400, detail="atomic catalog source is empty")
            pid = project_id(auth)
            source_ref = services.storage.put_file(
                temp_path,
                f"atomic-sources/{pid}/{catalog_id}/{uuid.uuid4().hex}{suffix}",
                content_type="text/csv" if source_format == "csv" else "application/x-ndjson",
            )
            source_uri = source_ref.uri
            if source_ref.sha256 != digest.hexdigest():
                raise IOError("uploaded source digest changed while persisting")
            result = services.control.create_generation(
                project_id=pid,
                catalog_id=catalog_id,
                source_uri=source_ref.uri,
                source_sha256=source_ref.sha256,
                source_format=source_format,
                batch_size=batch_size,
                max_rows=max_rows,
                feature_dim=feature_dim,
                ngram_min=ngram_min,
                ngram_max=ngram_max,
                auto_activate=auto_activate,
            )
            status_code_value = 202
            return result
        except ValueError as exc:
            status_code_value = 400
            if source_uri:
                services.storage.delete(source_uri)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException as exc:
            status_code_value = exc.status_code
            if source_uri:
                services.storage.delete(source_uri)
            raise
        finally:
            await file.close()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/generations", status_code_value, started, request)

    @router.get("/{catalog_id}/generations", response_model=list[AtomicCatalogGeneration])
    async def list_generations(
        catalog_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(manage_auth),
    ):
        return services.control.list_generations(project_id(auth), catalog_id, limit=limit)

    @router.get("/{catalog_id}/generations/{generation_id}", response_model=AtomicCatalogGeneration)
    async def get_generation(catalog_id: str, generation_id: str, auth=Depends(manage_auth)):
        try:
            return services.control.get_generation(project_id(auth), catalog_id, generation_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/{catalog_id}/active-generation", response_model=AtomicCatalogActive | None)
    async def active_generation(catalog_id: str, auth=Depends(manage_auth)):
        return services.control.get_active(project_id(auth), catalog_id)

    @router.post("/{catalog_id}/generations/{generation_id}/activate", response_model=AtomicCatalogActivation)
    async def activate_generation(
        catalog_id: str,
        generation_id: str,
        request: Request,
        auth=Depends(manage_auth),
    ):
        started = time.perf_counter()
        status_code_value = 500
        try:
            result = services.activate(project_id(auth), catalog_id, generation_id)
            status_code_value = 200
            return result
        except FileNotFoundError as exc:
            status_code_value = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code_value = 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/generations/{generation_id}/activate", status_code_value, started, request)

    @router.post("/{catalog_id}/rollback", response_model=AtomicCatalogActivation)
    async def rollback_generation(catalog_id: str, request: Request, auth=Depends(manage_auth)):
        started = time.perf_counter()
        status_code_value = 500
        try:
            result = services.rollback(project_id(auth), catalog_id)
            status_code_value = 200
            return result
        except FileNotFoundError as exc:
            status_code_value = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code_value = 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            audit(auth, "POST", f"/v1/project/candidate-catalogs/{catalog_id}/rollback", status_code_value, started, request)

    app.include_router(router)

    @app.on_event("startup")
    async def _atomic_catalog_start():
        await services.worker.start()

    @app.on_event("shutdown")
    async def _atomic_catalog_stop():
        await services.close()

    return services
