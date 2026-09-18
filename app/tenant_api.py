from __future__ import annotations

import asyncio
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.dataset_models import (
    ActiveLearningCandidate,
    DatasetCreate,
    DatasetExample,
    DatasetExampleInput,
    DatasetExamplesAdd,
    DatasetImportResult,
    DatasetImportReviewsRequest,
    DatasetModelRecord,
    DatasetSummary,
    DatasetTrainRequest,
    DatasetTrainResponse,
)
from app.models import LocalModelSummary, LocalPredictRequest, LocalPredictResponse
from app.studio_models import ReviewCreate, ReviewItem, ReviewResolve
from app.tenant_context import reset_tenant_context, set_tenant_context
from app.tenant_resources import TenantResourceRegistry


class TenantResourceServices:
    def __init__(self, enterprise_services, dataset_services, reviews, local_models):
        self.enterprise = enterprise_services
        self.datasets = dataset_services
        self.reviews = reviews
        self.local = local_models
        self.registry = TenantResourceRegistry()
        self.enterprise.resource_registry = self.registry

    def authenticate(self, token: str, scope: str, *, consume_quota: bool = False):
        return self.enterprise.store.authenticate(
            token, required_scope=scope, consume_quota=consume_quota
        )


def install_tenant_resource_api(app, enterprise_services, dataset_services, reviews, local_models) -> TenantResourceServices:
    services = TenantResourceServices(enterprise_services, dataset_services, reviews, local_models)
    router = APIRouter(prefix="/v1/project")

    def _auth(scope: str, *, consume_quota: bool = False):
        def dependency(x_rtdc_project_key: str | None = Header(default=None)):
            try:
                return services.authenticate(
                    x_rtdc_project_key or "", scope, consume_quota=consume_quota
                )
            except PermissionError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except OverflowError as exc:
                raise HTTPException(
                    status_code=429,
                    detail=str(exc),
                    headers={"Retry-After": "86400"},
                ) from exc

        return dependency

    dataset_auth = _auth("datasets")
    review_auth = _auth("reviews")
    model_auth = _auth("models")
    model_predict_auth = _auth("models", consume_quota=True)

    def _owned(project_id: str, resource_type: str, resource_id: str) -> None:
        try:
            services.registry.assert_owner(project_id, resource_type, resource_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/datasets", response_model=DatasetSummary)
    async def create_project_dataset(payload: DatasetCreate, auth=Depends(dataset_auth)):
        created = services.datasets.datasets.create(payload)
        try:
            services.registry.register_resource(auth.project_id, "dataset", created.id)
        except Exception:
            services.datasets.datasets.delete(created.id)
            raise
        return created

    @router.get("/datasets", response_model=list[DatasetSummary])
    async def list_project_datasets(
        decision_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(dataset_auth),
    ):
        result: list[DatasetSummary] = []
        for dataset_id in services.registry.list_ids(auth.project_id, "dataset", limit=limit):
            try:
                item = services.datasets.datasets.get(dataset_id)
            except FileNotFoundError:
                continue
            if decision_id is None or item.decision_id == decision_id:
                result.append(item)
        return result[:limit]

    @router.get("/datasets/{dataset_id}", response_model=DatasetSummary)
    async def get_project_dataset(dataset_id: str, auth=Depends(dataset_auth)):
        _owned(auth.project_id, "dataset", dataset_id)
        try:
            return services.datasets.datasets.get(dataset_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/datasets/{dataset_id}")
    async def delete_project_dataset(dataset_id: str, auth=Depends(dataset_auth)):
        _owned(auth.project_id, "dataset", dataset_id)
        deleted = services.datasets.datasets.delete(dataset_id)
        services.registry.unregister_resource(auth.project_id, "dataset", dataset_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="dataset not found")
        return {"deleted": True, "dataset_id": dataset_id}

    @router.post("/datasets/{dataset_id}/examples")
    async def add_project_dataset_examples(
        dataset_id: str, payload: DatasetExamplesAdd, auth=Depends(dataset_auth)
    ):
        _owned(auth.project_id, "dataset", dataset_id)
        try:
            added, duplicates = services.datasets.datasets.add_examples(
                dataset_id, payload.examples
            )
            return {
                "added": added,
                "skipped_duplicates": duplicates,
                "dataset": services.datasets.datasets.get(dataset_id),
            }
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/datasets/{dataset_id}/examples", response_model=list[DatasetExample])
    async def list_project_dataset_examples(
        dataset_id: str,
        split: str | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=50_000),
        auth=Depends(dataset_auth),
    ):
        _owned(auth.project_id, "dataset", dataset_id)
        try:
            return services.datasets.datasets.list_examples(
                dataset_id, split=split, limit=limit
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/datasets/{dataset_id}/import-reviews", response_model=DatasetImportResult)
    async def import_project_reviews(
        dataset_id: str,
        payload: DatasetImportReviewsRequest,
        auth=Depends(dataset_auth),
    ):
        _owned(auth.project_id, "dataset", dataset_id)
        try:
            summary = services.datasets.datasets.get(dataset_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        examples: list[DatasetExampleInput] = []
        review_ids: list[str | None] = []
        considered = 0
        missing_raw = 0
        for review_id in services.registry.list_ids(
            auth.project_id, "review", limit=payload.limit
        ):
            try:
                row = services.reviews.get(review_id)
            except FileNotFoundError:
                continue
            if row.status != "resolved" or row.decision_id != summary.decision_id:
                continue
            considered += 1
            if not row.input_text or not row.resolved_label:
                missing_raw += 1
                continue
            examples.append(
                DatasetExampleInput(
                    text=row.input_text,
                    label=row.resolved_label,
                    split=payload.split,
                    source_ref=f"human_review:{row.id}",
                )
            )
            review_ids.append(row.id)

        if examples:
            added, duplicates = services.datasets.datasets.add_examples(
                dataset_id, examples, review_ids=review_ids
            )
        else:
            added, duplicates = 0, 0
        return DatasetImportResult(
            considered=considered,
            added=added,
            skipped_duplicates=duplicates,
            skipped_without_raw_input=missing_raw,
        )

    @router.post("/datasets/{dataset_id}/train", response_model=DatasetTrainResponse)
    async def train_project_dataset(
        dataset_id: str, payload: DatasetTrainRequest, auth=Depends(dataset_auth)
    ):
        _owned(auth.project_id, "dataset", dataset_id)
        try:
            result = await services.datasets.train(dataset_id, payload)
            services.registry.register_resource(
                auth.project_id, "model", result.training.model_id
            )
            return result
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/datasets/{dataset_id}/models", response_model=list[DatasetModelRecord])
    async def list_project_dataset_models(
        dataset_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(dataset_auth),
    ):
        _owned(auth.project_id, "dataset", dataset_id)
        rows = services.datasets.datasets.list_models(dataset_id, limit=limit)
        return [
            row
            for row in rows
            if services.registry.owner("model", row.model_id) == auth.project_id
        ]

    @router.get("/active-learning/candidates", response_model=list[ActiveLearningCandidate])
    async def project_active_learning_candidates(
        decision_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(review_auth),
    ):
        candidates: list[ActiveLearningCandidate] = []
        for review_id in services.registry.list_ids(
            auth.project_id, "review", limit=max(limit, 1000)
        ):
            try:
                row = services.reviews.get(review_id)
            except FileNotFoundError:
                continue
            if row.status != "pending":
                continue
            if decision_id and row.decision_id != decision_id:
                continue
            candidates.append(
                ActiveLearningCandidate(
                    review_id=row.id,
                    decision_id=row.decision_id,
                    created_at=row.created_at,
                    confidence=row.confidence,
                    uncertainty=round(
                        1.0 - (row.confidence if row.confidence is not None else 0.0), 6
                    ),
                    suggested_label=row.suggested_label,
                    input_text=row.input_text,
                    input_sha256=row.input_sha256,
                    external_ref=row.external_ref,
                )
            )
        candidates.sort(key=lambda row: (-row.uncertainty, row.created_at))
        return candidates[:limit]

    @router.post("/reviews", response_model=ReviewItem)
    async def create_project_review(payload: ReviewCreate, auth=Depends(review_auth)):
        created = services.reviews.create(payload)
        services.registry.register_resource(auth.project_id, "review", created.id)
        return created

    @router.get("/reviews", response_model=list[ReviewItem])
    async def list_project_reviews(
        status: Literal["pending", "resolved", "dismissed", "all"] = Query(default="pending"),
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(review_auth),
    ):
        rows: list[ReviewItem] = []
        for review_id in services.registry.list_ids(
            auth.project_id, "review", limit=max(limit, 1000)
        ):
            try:
                item = services.reviews.get(review_id)
            except FileNotFoundError:
                continue
            if status == "all" or item.status == status:
                rows.append(item)
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[:limit]

    @router.get("/reviews/export/training-examples")
    async def export_project_training_examples(
        limit: int = Query(default=5000, ge=1, le=50_000), auth=Depends(review_auth)
    ):
        examples: list[dict[str, str]] = []
        for review_id in services.registry.list_ids(
            auth.project_id, "review", limit=50_000
        ):
            try:
                item = services.reviews.get(review_id)
            except FileNotFoundError:
                continue
            if item.status == "resolved" and item.input_text and item.resolved_label:
                examples.append({"text": item.input_text, "label": item.resolved_label})
                if len(examples) >= limit:
                    break
        return {
            "examples": examples,
            "note": "Only this project's resolved reviews with explicitly retained raw input are exported.",
        }

    @router.get("/reviews/{review_id}", response_model=ReviewItem)
    async def get_project_review(review_id: str, auth=Depends(review_auth)):
        _owned(auth.project_id, "review", review_id)
        try:
            return services.reviews.get(review_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/reviews/{review_id}/resolve", response_model=ReviewItem)
    async def resolve_project_review(
        review_id: str, payload: ReviewResolve, auth=Depends(review_auth)
    ):
        _owned(auth.project_id, "review", review_id)
        try:
            return services.reviews.resolve(review_id, payload)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/models", response_model=list[LocalModelSummary])
    async def list_project_models(
        limit: int = Query(default=100, ge=1, le=1000), auth=Depends(model_auth)
    ):
        rows: list[LocalModelSummary] = []
        for model_id in services.registry.list_ids(auth.project_id, "model", limit=limit):
            try:
                rows.append(services.local.get_model(model_id))
            except FileNotFoundError:
                continue
        return rows[:limit]

    @router.get("/models/{model_id}", response_model=LocalModelSummary)
    async def get_project_model(model_id: str, auth=Depends(model_auth)):
        _owned(auth.project_id, "model", model_id)
        try:
            return services.local.get_model(model_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/models/{model_id}/predict", response_model=LocalPredictResponse)
    async def predict_project_model(
        model_id: str,
        payload: LocalPredictRequest,
        request: Request,
        auth=Depends(model_predict_auth),
    ):
        _owned(auth.project_id, "model", model_id)
        request_id = request.headers.get("x-request-id") or "req_" + uuid.uuid4().hex[:20]
        started = time.perf_counter()
        status_code = 500
        context_token = set_tenant_context(auth.project_id, services.registry)
        try:
            device, predictions = await asyncio.to_thread(
                services.local.predict_many, model_id, payload.inputs, payload.device
            )
            status_code = 200
            return LocalPredictResponse(
                model_id=model_id, device=device, predictions=predictions
            )
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            reset_tenant_context(context_token)
            services.enterprise.store.audit(
                project_id=auth.project_id,
                key_id=auth.key_id,
                method="POST",
                path=f"/v1/project/models/{model_id}/predict",
                status_code=status_code,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                request_id=request_id,
            )

    app.include_router(router)

    @app.on_event("shutdown")
    async def _tenant_resource_shutdown():
        services.registry.close()

    return services
