from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.ml.local_classifier import hashed_char_features


ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
HeadKind = Literal["boolean", "categorical", "scalar"]


class ParallelHeadDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    kind: HeadKind = "categorical"
    labels: list[str] = Field(min_length=2, max_length=50)
    values: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_head(self):
        if len(self.labels) != len(set(self.labels)):
            raise ValueError("head labels must be unique")
        if self.kind == "boolean" and len(self.labels) != 2:
            raise ValueError("boolean head requires exactly two labels")
        if self.kind == "scalar":
            missing = set(self.labels) - set(self.values)
            if missing:
                raise ValueError(f"scalar head values missing labels: {sorted(missing)}")
        elif self.values:
            unknown = set(self.values) - set(self.labels)
            if unknown:
                raise ValueError(f"head values contain unknown labels: {sorted(unknown)}")
        return self


class ParallelHeadExample(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    targets: dict[str, str] = Field(min_length=1, max_length=64)


class TrainParallelHeadRequest(BaseModel):
    model_name: str = Field(min_length=1, max_length=160)
    heads: list[ParallelHeadDefinition] = Field(min_length=1, max_length=64)
    examples: list[ParallelHeadExample] = Field(min_length=20, max_length=50_000)
    feature_dim: int = Field(default=4096, ge=256, le=32768)
    hidden_dim: int = Field(default=256, ge=16, le=2048)
    ngram_min: int = Field(default=1, ge=1, le=5)
    ngram_max: int = Field(default=3, ge=1, le=6)
    epochs: int = Field(default=8, ge=1, le=100)
    batch_size: int = Field(default=128, ge=4, le=2048)
    learning_rate: float = Field(default=0.002, gt=0.0, le=0.1)
    validation_split: float = Field(default=0.2, ge=0.05, le=0.4)
    brier_weight: float = Field(default=0.15, ge=0.0, le=2.0)
    device: Literal["auto", "cpu", "cuda"] = "auto"

    @model_validator(mode="after")
    def validate_request(self):
        if self.ngram_max < self.ngram_min:
            raise ValueError("ngram_max must be >= ngram_min")
        head_ids = [head.id for head in self.heads]
        if len(head_ids) != len(set(head_ids)):
            raise ValueError("head ids must be unique")
        by_id = {head.id: head for head in self.heads}
        observations: dict[tuple[str, str], int] = {}
        for example in self.examples:
            unknown_heads = set(example.targets) - set(by_id)
            if unknown_heads:
                raise ValueError(f"example contains unknown heads: {sorted(unknown_heads)}")
            for head_id, label in example.targets.items():
                if label not in by_id[head_id].labels:
                    raise ValueError(f"invalid label {label!r} for head {head_id!r}")
                observations[(head_id, label)] = observations.get((head_id, label), 0) + 1
        for head in self.heads:
            for label in head.labels:
                if observations.get((head.id, label), 0) < 2:
                    raise ValueError(
                        f"head {head.id!r} label {label!r} requires at least two training observations"
                    )
        return self


class ParallelHeadMetric(BaseModel):
    accuracy: float | None = None
    brier: float | None = None
    ece: float | None = None
    temperature: float = 1.0
    validation_examples: int = 0


class ParallelHeadModelSummary(BaseModel):
    model_id: str
    project_id: str
    model_name: str
    heads: list[ParallelHeadDefinition]
    examples: int
    feature_dim: int
    hidden_dim: int
    ngram_min: int
    ngram_max: int
    brier_weight: float
    created_at: str
    validation: dict[str, ParallelHeadMetric] = Field(default_factory=dict)
    raw_text_persisted: bool = False
    architecture: str = "shared_hashed_ngram_encoder_parallel_probability_heads_v1"


class ParallelHeadPrediction(BaseModel):
    id: str
    kind: HeadKind
    selected: str
    confidence: float
    probabilities: dict[str, float]
    expected_value: float | None = None
    temperature: float


class ParallelHeadPredictRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    heads: list[str] | None = Field(default=None, max_length=64)
    device: Literal["auto", "cpu", "cuda"] = "auto"


class ParallelHeadPredictResponse(BaseModel):
    model_id: str
    project_id: str
    device: str
    predictions: list[ParallelHeadPrediction]
    input_encoded_once: bool = True
    output_generated_as_text: bool = False


def _ece(probabilities: list[list[float]], targets: list[int], bins: int = 10) -> float | None:
    if not probabilities:
        return None
    buckets = [{"n": 0, "conf": 0.0, "correct": 0.0} for _ in range(bins)]
    for probs, target in zip(probabilities, targets):
        confidence = max(probs)
        selected = max(range(len(probs)), key=lambda index: probs[index])
        bucket = min(bins - 1, int(confidence * bins))
        row = buckets[bucket]
        row["n"] += 1
        row["conf"] += confidence
        row["correct"] += float(selected == target)
    total = len(targets)
    value = 0.0
    for row in buckets:
        if not row["n"]:
            continue
        confidence = row["conf"] / row["n"]
        accuracy = row["correct"] / row["n"]
        value += (row["n"] / total) * abs(accuracy - confidence)
    return float(value)


class ParallelHeadModelProvider:
    """Independent shared-encoder multi-task classifier with calibrated heads.

    It uses generic multi-task classification and proper-scoring objectives. It is
    not an implementation of any third-party sampler or proprietary RL algorithm.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("RTDC_PARALLEL_MODEL_DIR", "data/parallel-models"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.default_device = os.getenv("RTDC_DEVICE", "auto").strip().lower() or "auto"
        self._cache: dict[tuple[str, str], tuple[dict[str, Any], Any]] = {}

    @property
    def torch_available(self) -> bool:
        return importlib.util.find_spec("torch") is not None

    def _torch(self):
        if not self.torch_available:
            raise RuntimeError("parallel-head ML support requires: pip install -e '.[ml]'")
        import torch
        return torch

    def resolve_device(self, requested: str | None = None) -> str:
        torch = self._torch()
        value = (requested or self.default_device or "auto").lower()
        if value == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA was requested but no CUDA device is available")
            return "cuda"
        if value == "cpu":
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    @staticmethod
    def _safe_id(value: str) -> str:
        if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in value):
            raise ValueError("invalid model id")
        return value

    def _paths(self, model_id: str) -> tuple[Path, Path]:
        safe = self._safe_id(model_id)
        return self.root / f"{safe}.json", self.root / f"{safe}.pt"

    @staticmethod
    def _build_network(torch, feature_dim: int, hidden_dim: int, heads: list[dict[str, Any]]):
        nn = torch.nn

        class ParallelHeadNet(nn.Module):
            def __init__(self):
                super().__init__()
                self.encoder = nn.Sequential(
                    nn.Linear(feature_dim, hidden_dim),
                    nn.GELU(),
                    nn.LayerNorm(hidden_dim),
                )
                self.heads = nn.ModuleDict(
                    {head["id"]: nn.Linear(hidden_dim, len(head["labels"])) for head in heads}
                )

            def forward(self, x):
                shared = self.encoder(x)
                return {head_id: layer(shared) for head_id, layer in self.heads.items()}

        return ParallelHeadNet()

    def _metadata(self, model_id: str) -> dict[str, Any]:
        metadata_path, _ = self._paths(model_id)
        if not metadata_path.exists():
            raise FileNotFoundError("parallel-head model not found")
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def get_model(self, project_id: str, model_id: str) -> ParallelHeadModelSummary:
        data = self._metadata(model_id)
        if data.get("project_id") != project_id:
            raise FileNotFoundError("parallel-head model not found")
        return ParallelHeadModelSummary.model_validate(data)

    def list_models(self, project_id: str, limit: int = 100) -> list[ParallelHeadModelSummary]:
        rows: list[ParallelHeadModelSummary] = []
        for path in sorted(self.root.glob("mh_*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            if len(rows) >= limit:
                break
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("project_id") == project_id:
                    rows.append(ParallelHeadModelSummary.model_validate(data))
            except Exception:
                continue
        return rows

    def _features(self, texts: list[str], request_or_meta: Any):
        torch = self._torch()
        return torch.tensor(
            [
                hashed_char_features(
                    text,
                    int(request_or_meta.feature_dim if hasattr(request_or_meta, "feature_dim") else request_or_meta["feature_dim"]),
                    int(request_or_meta.ngram_min if hasattr(request_or_meta, "ngram_min") else request_or_meta["ngram_min"]),
                    int(request_or_meta.ngram_max if hasattr(request_or_meta, "ngram_max") else request_or_meta["ngram_max"]),
                )
                for text in texts
            ],
            dtype=torch.float32,
        )

    def train(self, project_id: str, request: TrainParallelHeadRequest) -> ParallelHeadModelSummary:
        torch = self._torch()
        device = self.resolve_device(request.device)
        torch.manual_seed(42)
        if device == "cuda":
            torch.cuda.manual_seed_all(42)
        rng = random.Random(42)
        indexes = list(range(len(request.examples)))
        rng.shuffle(indexes)
        val_count = max(1, int(round(len(indexes) * request.validation_split)))
        val_indices = indexes[:val_count]
        train_indices = indexes[val_count:]
        if not train_indices:
            raise ValueError("training split is empty")

        heads_json = [head.model_dump() for head in request.heads]
        head_by_id = {head.id: head for head in request.heads}
        label_indexes = {head.id: {label: i for i, label in enumerate(head.labels)} for head in request.heads}
        model = self._build_network(torch, request.feature_dim, request.hidden_dim, heads_json).to(device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=request.learning_rate, weight_decay=1e-4)

        model.train()
        for _ in range(request.epochs):
            order = train_indices[:]
            rng.shuffle(order)
            for start in range(0, len(order), request.batch_size):
                batch_ids = order[start : start + request.batch_size]
                examples = [request.examples[index] for index in batch_ids]
                matrix = self._features([example.text for example in examples], request).to(device)
                outputs = model(matrix)
                losses = []
                for head in request.heads:
                    selected_rows = [i for i, example in enumerate(examples) if head.id in example.targets]
                    if not selected_rows:
                        continue
                    logits = outputs[head.id][selected_rows]
                    targets = torch.tensor(
                        [label_indexes[head.id][examples[i].targets[head.id]] for i in selected_rows],
                        dtype=torch.long,
                        device=device,
                    )
                    ce = torch.nn.functional.cross_entropy(logits, targets)
                    probs = torch.softmax(logits, dim=1)
                    one_hot = torch.nn.functional.one_hot(targets, num_classes=len(head.labels)).float()
                    brier = torch.mean(torch.sum((probs - one_hot) ** 2, dim=1))
                    losses.append(ce + request.brier_weight * brier)
                if not losses:
                    continue
                loss = torch.stack(losses).mean()
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                optimizer.step()

        validation: dict[str, ParallelHeadMetric] = {}
        model.eval()
        logits_by_head: dict[str, list[Any]] = {head.id: [] for head in request.heads}
        targets_by_head: dict[str, list[int]] = {head.id: [] for head in request.heads}
        with torch.inference_mode():
            for start in range(0, len(val_indices), request.batch_size):
                batch_ids = val_indices[start : start + request.batch_size]
                examples = [request.examples[index] for index in batch_ids]
                matrix = self._features([example.text for example in examples], request).to(device)
                outputs = model(matrix)
                for head in request.heads:
                    for row_index, example in enumerate(examples):
                        label = example.targets.get(head.id)
                        if label is None:
                            continue
                        logits_by_head[head.id].append(outputs[head.id][row_index].detach().cpu())
                        targets_by_head[head.id].append(label_indexes[head.id][label])

        temperatures: dict[str, float] = {}
        for head in request.heads:
            logits_rows = logits_by_head[head.id]
            targets = targets_by_head[head.id]
            if not logits_rows:
                validation[head.id] = ParallelHeadMetric(validation_examples=0)
                temperatures[head.id] = 1.0
                continue
            logits = torch.stack(logits_rows)
            target_tensor = torch.tensor(targets, dtype=torch.long)
            best_temperature = 1.0
            best_nll = None
            for step in range(51):
                temperature = 0.5 + step * 0.05
                nll = float(torch.nn.functional.cross_entropy(logits / temperature, target_tensor).item())
                if best_nll is None or nll < best_nll:
                    best_nll = nll
                    best_temperature = temperature
            temperatures[head.id] = best_temperature
            probs_tensor = torch.softmax(logits / best_temperature, dim=1)
            probs = probs_tensor.tolist()
            selected = probs_tensor.argmax(dim=1)
            accuracy = float((selected == target_tensor).float().mean().item())
            one_hot = torch.nn.functional.one_hot(target_tensor, num_classes=len(head.labels)).float()
            brier = float(torch.mean(torch.sum((probs_tensor - one_hot) ** 2, dim=1)).item())
            validation[head.id] = ParallelHeadMetric(
                accuracy=round(accuracy, 6),
                brier=round(brier, 6),
                ece=None if _ece(probs, targets) is None else round(float(_ece(probs, targets)), 6),
                temperature=round(best_temperature, 4),
                validation_examples=len(targets),
            )

        model_id = "mh_" + uuid.uuid4().hex[:20]
        metadata_path, weights_path = self._paths(model_id)
        state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
        torch.save(state, weights_path)
        metadata = ParallelHeadModelSummary(
            model_id=model_id,
            project_id=project_id,
            model_name=request.model_name,
            heads=request.heads,
            examples=len(request.examples),
            feature_dim=request.feature_dim,
            hidden_dim=request.hidden_dim,
            ngram_min=request.ngram_min,
            ngram_max=request.ngram_max,
            brier_weight=request.brier_weight,
            created_at=datetime.now(timezone.utc).isoformat(),
            validation=validation,
            raw_text_persisted=False,
        )
        metadata_path.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
        self._cache.pop((model_id, "cpu"), None)
        self._cache.pop((model_id, "cuda"), None)
        return metadata

    def _load(self, project_id: str, model_id: str, device: str):
        key = (model_id, device)
        if key in self._cache:
            metadata, model = self._cache[key]
            if metadata.get("project_id") != project_id:
                raise FileNotFoundError("parallel-head model not found")
            return metadata, model
        torch = self._torch()
        metadata = self._metadata(model_id)
        if metadata.get("project_id") != project_id:
            raise FileNotFoundError("parallel-head model not found")
        _, weights_path = self._paths(model_id)
        if not weights_path.exists():
            raise FileNotFoundError("parallel-head model weights not found")
        model = self._build_network(
            torch,
            int(metadata["feature_dim"]),
            int(metadata["hidden_dim"]),
            metadata["heads"],
        )
        try:
            state = torch.load(weights_path, map_location="cpu", weights_only=True)
        except TypeError:
            state = torch.load(weights_path, map_location="cpu")
        model.load_state_dict(state)
        model.to(device)
        model.eval()
        self._cache[key] = (metadata, model)
        return metadata, model

    def predict(self, project_id: str, model_id: str, request: ParallelHeadPredictRequest) -> ParallelHeadPredictResponse:
        torch = self._torch()
        device = self.resolve_device(request.device)
        metadata, model = self._load(project_id, model_id, device)
        heads = [ParallelHeadDefinition.model_validate(row) for row in metadata["heads"]]
        by_id = {head.id: head for head in heads}
        selected_heads = request.heads or [head.id for head in heads]
        unknown = set(selected_heads) - set(by_id)
        if unknown:
            raise ValueError(f"unknown parallel model heads: {sorted(unknown)}")
        matrix = self._features([request.text], metadata).to(device)
        with torch.inference_mode():
            outputs = model(matrix)
        predictions: list[ParallelHeadPrediction] = []
        for head_id in selected_heads:
            head = by_id[head_id]
            metric = (metadata.get("validation") or {}).get(head_id, {})
            temperature = max(0.05, float(metric.get("temperature", 1.0)))
            probs_row = torch.softmax(outputs[head_id][0] / temperature, dim=0).detach().cpu().tolist()
            probabilities = {label: round(float(probs_row[i]), 6) for i, label in enumerate(head.labels)}
            selected = max(probabilities, key=probabilities.get)
            expected = None
            if head.kind == "scalar":
                expected = sum(float(head.values[label]) * probabilities[label] for label in head.labels)
            predictions.append(
                ParallelHeadPrediction(
                    id=head_id,
                    kind=head.kind,
                    selected=selected,
                    confidence=probabilities[selected],
                    probabilities=probabilities,
                    expected_value=None if expected is None else round(expected, 6),
                    temperature=temperature,
                )
            )
        return ParallelHeadPredictResponse(
            model_id=model_id,
            project_id=project_id,
            device=device,
            predictions=predictions,
        )

    async def predict_async(self, project_id: str, model_id: str, request: ParallelHeadPredictRequest):
        return await asyncio.to_thread(self.predict, project_id, model_id, request)
