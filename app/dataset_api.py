from __future__ import annotations

import asyncio
import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query

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
from app.dataset_store import DatasetStore
from app.models import BenchmarkExample, BenchmarkRequest, TrainModelRequest, TrainingExample


def _require_dataset_admin(x_rtdc_studio_key: str | None = Header(default=None)) -> bool:
    expected = os.getenv("RTDC_STUDIO_API_KEY", "").strip() or os.getenv("RTDC_ADMIN_API_KEY", "").strip()
    if expected and (x_rtdc_studio_key is None or not hmac.compare_digest(x_rtdc_studio_key, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Studio-Key")
    return True


class DatasetServices:
    def __init__(self, engine, benchmark_runner, reviews):
        self.engine = engine
        self.benchmark_runner = benchmark_runner
        self.reviews = reviews
        self.datasets = DatasetStore()

    async def train(self, dataset_id: str, request: DatasetTrainRequest) -> DatasetTrainResponse:
        summary = self.datasets.get(dataset_id)
        if not summary.allow_model_training:
            raise ValueError("dataset governance does not permit model training")
        train_rows = self.datasets.list_examples(dataset_id, split="train", limit=50_000)
        if len(train_rows) < 6:
            raise ValueError("dataset requires at least 6 training examples")
        train_request = TrainModelRequest(
            decision_id=summary.decision_id,
            model_name=request.model_name or f"{summary.name}-{summary.fingerprint_sha256[:8]}",
            examples=[TrainingExample(text=row.text, label=row.label) for row in train_rows],
            feature_dim=request.feature_dim,
            ngram_min=request.ngram_min,
            ngram_max=request.ngram_max,
            epochs=request.epochs,
            learning_rate=request.learning_rate,
            batch_size=request.batch_size,
            validation_split=request.internal_validation_split,
            device=request.device,
        )
        training = await asyncio.to_thread(self.engine.local.train, train_request)

        evaluation = None
        if request.evaluate_on != "none":
            eval_rows = self.datasets.list_examples(dataset_id, split=request.evaluate_on, limit=50_000)
            if eval_rows:
                bench = BenchmarkRequest(
                    model_id=training.model_id,
                    examples=[BenchmarkExample(text=row.text, label=row.label) for row in eval_rows],
                    device=request.device,
                    warmup_runs=2,
                    repeat_runs=request.benchmark_repeat_runs,
                    batch_size=request.benchmark_batch_size,
                    confidence_threshold=request.confidence_threshold,
                    max_errors=25,
                )
                evaluation = await asyncio.to_thread(self.benchmark_runner.run_local, bench)

        lineage = self.datasets.record_model(
            dataset_id=dataset_id,
            model_id=training.model_id,
            train_examples=len(train_rows),
            fingerprint=summary.fingerprint_sha256,
            training=training.model_dump(mode="json"),
            evaluation=None if evaluation is None else evaluation.model_dump(mode="json"),
        )
        return DatasetTrainResponse(
            dataset_id=dataset_id,
            dataset_fingerprint_sha256=summary.fingerprint_sha256,
            training=training,
            evaluation=evaluation,
            lineage_id=lineage.id,
        )


def install_dataset_api(app, engine, benchmark_runner, reviews) -> DatasetServices:
    services = DatasetServices(engine, benchmark_runner, reviews)
    router = APIRouter(dependencies=[Depends(_require_dataset_admin)])

    @router.post("/v1/datasets", response_model=DatasetSummary)
    async def create_dataset(request: DatasetCreate):
        try:
            return services.datasets.create(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/v1/datasets", response_model=list[DatasetSummary])
    async def list_datasets(decision_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=1000)):
        return services.datasets.list(decision_id=decision_id, limit=limit)

    @router.get("/v1/datasets/{dataset_id}", response_model=DatasetSummary)
    async def get_dataset(dataset_id: str):
        try:
            return services.datasets.get(dataset_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/v1/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str):
        if not services.datasets.delete(dataset_id):
            raise HTTPException(status_code=404, detail=f"dataset not found: {dataset_id}")
        return {"deleted": True, "dataset_id": dataset_id}

    @router.post("/v1/datasets/{dataset_id}/examples")
    async def add_dataset_examples(dataset_id: str, request: DatasetExamplesAdd):
        try:
            added, duplicates = services.datasets.add_examples(dataset_id, request.examples)
            return {"added": added, "skipped_duplicates": duplicates, "dataset": services.datasets.get(dataset_id)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/v1/datasets/{dataset_id}/examples", response_model=list[DatasetExample])
    async def list_dataset_examples(
        dataset_id: str,
        split: str | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=50_000),
    ):
        try:
            return services.datasets.list_examples(dataset_id, split=split, limit=limit)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/v1/datasets/{dataset_id}/import-reviews", response_model=DatasetImportResult)
    async def import_resolved_reviews(dataset_id: str, request: DatasetImportReviewsRequest):
        try:
            summary = services.datasets.get(dataset_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        rows = services.reviews.list(status="resolved", limit=request.limit)
        matching = [row for row in rows if row.decision_id == summary.decision_id]
        examples: list[DatasetExampleInput] = []
        review_ids: list[str | None] = []
        missing_raw = 0
        for row in matching:
            if not row.input_text or not row.resolved_label:
                missing_raw += 1
                continue
            examples.append(
                DatasetExampleInput(
                    text=row.input_text,
                    label=row.resolved_label,
                    split=request.split,
                    source_ref=f"human_review:{row.id}",
                )
            )
            review_ids.append(row.id)
        if examples:
            added, duplicates = services.datasets.add_examples(dataset_id, examples, review_ids=review_ids)
        else:
            added, duplicates = 0, 0
        return DatasetImportResult(
            considered=len(matching),
            added=added,
            skipped_duplicates=duplicates,
            skipped_without_raw_input=missing_raw,
        )

    @router.post("/v1/datasets/{dataset_id}/train", response_model=DatasetTrainResponse)
    async def train_dataset(dataset_id: str, request: DatasetTrainRequest):
        try:
            return await services.train(dataset_id, request)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/v1/datasets/{dataset_id}/models", response_model=list[DatasetModelRecord])
    async def list_dataset_models(dataset_id: str, limit: int = Query(default=100, ge=1, le=1000)):
        try:
            return services.datasets.list_models(dataset_id, limit=limit)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/v1/active-learning/candidates", response_model=list[ActiveLearningCandidate])
    async def active_learning_candidates(
        decision_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        rows = services.reviews.list(status="pending", limit=1000)
        if decision_id:
            rows = [row for row in rows if row.decision_id == decision_id]
        rows.sort(key=lambda row: (1.0 if row.confidence is None else row.confidence, row.created_at))
        return [
            ActiveLearningCandidate(
                review_id=row.id,
                decision_id=row.decision_id,
                created_at=row.created_at,
                confidence=row.confidence,
                uncertainty=round(1.0 - (row.confidence if row.confidence is not None else 0.0), 6),
                suggested_label=row.suggested_label,
                input_text=row.input_text,
                input_sha256=row.input_sha256,
                external_ref=row.external_ref,
            )
            for row in rows[:limit]
        ]

    @router.post("/v1/datasets/purge-expired")
    async def purge_expired_datasets():
        return {"deleted": services.datasets.purge_expired()}

    app.include_router(router)

    @app.on_event("shutdown")
    async def _dataset_shutdown():
        services.datasets.close()

    return services
