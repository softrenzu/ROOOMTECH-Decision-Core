from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import random
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.models import LocalModelSummary, LocalPrediction, TrainModelRequest, TrainModelResponse


PERSONALIZATION = b"RTDCv2"


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def hashed_char_features(text: str, feature_dim: int = 4096, ngram_min: int = 1, ngram_max: int = 3) -> list[float]:
    """Language-agnostic Unicode character n-gram hashing.

    Japanese does not require a tokenizer or dictionary. The same feature extractor works
    for Japanese, English, Chinese, Korean and mixed-language text.
    """
    normalized = "^" + normalize_text(text) + "$"
    vector = [0.0] * feature_dim
    for n in range(ngram_min, ngram_max + 1):
        if len(normalized) < n:
            continue
        for i in range(len(normalized) - n + 1):
            gram = normalized[i : i + n].encode("utf-8")
            digest = hashlib.blake2b(gram, digest_size=8, person=PERSONALIZATION).digest()
            index = int.from_bytes(digest[:4], "little") % feature_dim
            sign = 1.0 if (digest[4] & 1) else -1.0
            vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0:
        vector = [value / norm for value in vector]
    return vector


class LocalClassifierProvider:
    name = "local_classifier"

    def __init__(self):
        self.model_dir = Path(os.getenv("RTDC_LOCAL_MODEL_DIR", "data/models"))
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.default_device = os.getenv("RTDC_DEVICE", "auto").strip().lower() or "auto"
        self._cache: dict[tuple[str, str], tuple[dict, object]] = {}

    @property
    def torch_available(self) -> bool:
        return importlib.util.find_spec("torch") is not None

    def _torch(self):
        if not self.torch_available:
            raise RuntimeError("local ML support is not installed; install with: pip install -e '.[ml]'")
        import torch
        return torch

    def resolve_device(self, requested: str | None = None) -> str:
        torch = self._torch()
        requested = (requested or self.default_device or "auto").lower()
        if requested == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA was requested but no CUDA device is available")
            return "cuda"
        if requested == "cpu":
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    def runtime_info(self) -> dict:
        info = {
            "torch_installed": self.torch_available,
            "cuda_available": False,
            "device": "unavailable",
            "device_name": None,
            "local_model_dir": str(self.model_dir),
        }
        if not self.torch_available:
            return info
        torch = self._torch()
        cuda = bool(torch.cuda.is_available())
        info["cuda_available"] = cuda
        info["device"] = "cuda" if cuda else "cpu"
        if cuda:
            info["device_name"] = torch.cuda.get_device_name(0)
        return info

    def _paths(self, model_id: str) -> tuple[Path, Path]:
        if not model_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in model_id):
            raise ValueError("invalid model_id")
        return self.model_dir / f"{model_id}.json", self.model_dir / f"{model_id}.pt"

    def _read_metadata(self, model_id: str) -> dict:
        metadata_path, _ = self._paths(model_id)
        if not metadata_path.exists():
            raise FileNotFoundError(f"local model not found: {model_id}")
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def list_models(self) -> list[LocalModelSummary]:
        models: list[LocalModelSummary] = []
        for path in sorted(self.model_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                models.append(LocalModelSummary(**data))
            except Exception:
                continue
        return models

    def get_model(self, model_id: str) -> LocalModelSummary:
        return LocalModelSummary(**self._read_metadata(model_id))

    def _split_indices(self, labels: list[str], validation_split: float) -> tuple[list[int], list[int]]:
        by_label: dict[str, list[int]] = defaultdict(list)
        for index, label in enumerate(labels):
            by_label[label].append(index)
        rng = random.Random(42)
        train: list[int] = []
        validation: list[int] = []
        for indexes in by_label.values():
            rng.shuffle(indexes)
            if validation_split > 0 and len(indexes) >= 3:
                n_validation = max(1, round(len(indexes) * validation_split))
                n_validation = min(n_validation, len(indexes) - 2)
            else:
                n_validation = 0
            validation.extend(indexes[:n_validation])
            train.extend(indexes[n_validation:])
        rng.shuffle(train)
        rng.shuffle(validation)
        return train, validation

    def train(self, request: TrainModelRequest) -> TrainModelResponse:
        torch = self._torch()
        device = self.resolve_device(request.device)
        torch.manual_seed(42)
        if device == "cuda":
            torch.cuda.manual_seed_all(42)

        label_order = list(dict.fromkeys(example.label for example in request.examples))
        label_to_index = {label: index for index, label in enumerate(label_order)}
        texts = [example.text for example in request.examples]
        text_labels = [example.label for example in request.examples]
        train_indices, validation_indices = self._split_indices(text_labels, request.validation_split)

        features = torch.tensor(
            [
                hashed_char_features(text, request.feature_dim, request.ngram_min, request.ngram_max)
                for text in texts
            ],
            dtype=torch.float32,
        )
        targets = torch.tensor([label_to_index[label] for label in text_labels], dtype=torch.long)

        model = torch.nn.Linear(request.feature_dim, len(label_order)).to(device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=request.learning_rate, weight_decay=1e-4)
        criterion = torch.nn.CrossEntropyLoss()

        train_x = features[train_indices]
        train_y = targets[train_indices]
        model.train()
        for _ in range(request.epochs):
            permutation = torch.randperm(len(train_indices))
            for start in range(0, len(train_indices), request.batch_size):
                batch_indexes = permutation[start : start + request.batch_size]
                batch_x = train_x[batch_indexes].to(device)
                batch_y = train_y[batch_indexes].to(device)
                optimizer.zero_grad(set_to_none=True)
                loss = criterion(model(batch_x), batch_y)
                loss.backward()
                optimizer.step()

        model.eval()
        with torch.inference_mode():
            train_logits = model(train_x.to(device))
            train_accuracy = float((train_logits.argmax(dim=1).cpu() == train_y).float().mean().item())

            temperature = 1.0
            validation_accuracy = None
            if validation_indices:
                val_x = features[validation_indices].to(device)
                val_y = targets[validation_indices].to(device)
                val_logits = model(val_x)
                candidates = [0.5 + i * 0.05 for i in range(51)]
                best_nll = None
                for candidate in candidates:
                    nll = float(criterion(val_logits / candidate, val_y).item())
                    if best_nll is None or nll < best_nll:
                        best_nll = nll
                        temperature = candidate
                validation_accuracy = float((val_logits.argmax(dim=1) == val_y).float().mean().item())

        model_id = "mdl_" + uuid.uuid4().hex[:20]
        metadata_path, weights_path = self._paths(model_id)
        state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
        torch.save(state, weights_path)
        metadata = {
            "model_id": model_id,
            "decision_id": request.decision_id,
            "model_name": request.model_name,
            "labels": label_order,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "examples": len(request.examples),
            "feature_dim": request.feature_dim,
            "ngram_min": request.ngram_min,
            "ngram_max": request.ngram_max,
            "temperature": round(float(temperature), 4),
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        self._cache.pop((model_id, "cpu"), None)
        self._cache.pop((model_id, "cuda"), None)

        return TrainModelResponse(
            model_id=model_id,
            decision_id=request.decision_id,
            labels=label_order,
            examples=len(request.examples),
            device=device,
            feature_dim=request.feature_dim,
            ngram_range=(request.ngram_min, request.ngram_max),
            temperature=round(float(temperature), 4),
            train_accuracy=round(train_accuracy, 6),
            validation_accuracy=None if validation_accuracy is None else round(validation_accuracy, 6),
            raw_text_persisted=False,
        )

    def _load(self, model_id: str, device: str):
        torch = self._torch()
        cache_key = (model_id, device)
        if cache_key in self._cache:
            return self._cache[cache_key]
        metadata = self._read_metadata(model_id)
        _, weights_path = self._paths(model_id)
        if not weights_path.exists():
            raise FileNotFoundError(f"model weights not found: {model_id}")
        model = torch.nn.Linear(int(metadata["feature_dim"]), len(metadata["labels"]))
        try:
            state = torch.load(weights_path, map_location="cpu", weights_only=True)
        except TypeError:
            state = torch.load(weights_path, map_location="cpu")
        model.load_state_dict(state)
        model.to(device)
        model.eval()
        self._cache[cache_key] = (metadata, model)
        return metadata, model

    def predict_many(self, model_id: str, texts: Iterable[str], requested_device: str | None = None) -> tuple[str, list[LocalPrediction]]:
        torch = self._torch()
        device = self.resolve_device(requested_device)
        metadata, model = self._load(model_id, device)
        text_list = list(texts)
        if not text_list:
            return device, []
        matrix = torch.tensor(
            [
                hashed_char_features(
                    text,
                    int(metadata["feature_dim"]),
                    int(metadata["ngram_min"]),
                    int(metadata["ngram_max"]),
                )
                for text in text_list
            ],
            dtype=torch.float32,
            device=device,
        )
        temperature = max(float(metadata.get("temperature", 1.0)), 0.05)
        with torch.inference_mode():
            probabilities = torch.softmax(model(matrix) / temperature, dim=1).cpu().tolist()
        labels = list(metadata["labels"])
        predictions: list[LocalPrediction] = []
        for row in probabilities:
            scores = {label: round(float(row[index]), 6) for index, label in enumerate(labels)}
            selected = max(scores, key=scores.get)
            predictions.append(LocalPrediction(selected=selected, confidence=scores[selected], scores=scores))
        return device, predictions

    def evaluate_one(self, model_id: str, text: str, allowed_choices: list[str]) -> dict:
        _, predictions = self.predict_many(model_id, [text])
        prediction = predictions[0]
        raw = {choice: float(prediction.scores.get(choice, 0.0)) for choice in allowed_choices}
        total = sum(raw.values())
        if total <= 0:
            raise ValueError("trained model labels do not overlap with decision choices")
        scores = {choice: value / total for choice, value in raw.items()}
        return {
            "scores": scores,
            "evidence": [],
            "reason_codes": ["LOCAL_HASHED_CHAR_NGRAM", "NO_RAW_TEXT_STORED"],
        }
