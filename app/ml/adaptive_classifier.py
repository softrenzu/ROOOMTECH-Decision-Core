from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Iterable

from app.models import LocalPrediction
from .local_classifier import PERSONALIZATION, LocalClassifierProvider as _BaseLocalClassifierProvider, normalize_text


def fast_hashed_char_features(text: str, feature_dim: int = 4096, ngram_min: int = 1, ngram_max: int = 3) -> list[float]:
    """Equivalent hashed character features with sparse normalization work.

    Realtime inputs usually touch only a small fraction of the feature vector. Keeping
    the accumulator sparse avoids two full-vector scans before materializing the dense
    tensor expected by the current linear classifier.
    """
    normalized = "^" + normalize_text(text) + "$"
    sparse: dict[int, float] = {}
    for n in range(ngram_min, ngram_max + 1):
        if len(normalized) < n:
            continue
        for i in range(len(normalized) - n + 1):
            gram = normalized[i : i + n].encode("utf-8")
            digest = hashlib.blake2b(gram, digest_size=8, person=PERSONALIZATION).digest()
            index = int.from_bytes(digest[:4], "little") % feature_dim
            sign = 1.0 if (digest[4] & 1) else -1.0
            sparse[index] = sparse.get(index, 0.0) + sign

    vector = [0.0] * feature_dim
    norm = math.sqrt(sum(value * value for value in sparse.values()))
    if norm > 0:
        inverse = 1.0 / norm
        for index, value in sparse.items():
            vector[index] = value * inverse
    return vector


class AdaptiveLocalClassifierProvider(_BaseLocalClassifierProvider):
    """Choose the low-latency scheduling strategy for the active accelerator.

    CUDA requests use the base provider's micro-batching and bounded GPU queue.
    CPU requests skip the batching collection window and run through a bounded
    thread gate, which avoids adding batch-wait latency to this lightweight model.
    """

    def __init__(self):
        super().__init__()
        self._cpu_direct_calls = 0
        self._cpu_direct_errors = 0

    def runtime_info(self) -> dict:
        info = super().runtime_info()
        scheduling = info.setdefault("inference_batching", {})
        scheduling["strategy"] = "cuda_microbatch_cpu_direct"
        scheduling["cpu_direct_calls"] = self._cpu_direct_calls
        scheduling["cpu_direct_errors"] = self._cpu_direct_errors
        scheduling["cpu_feature_extractor"] = "sparse_hash_v1"
        return info

    def predict_many(self, model_id: str, texts: Iterable[str], requested_device: str | None = None):
        torch = self._torch()
        device = self.resolve_device(requested_device)
        metadata, model = self._load(model_id, device)
        text_list = list(texts)
        if not text_list:
            return device, []

        matrix = torch.tensor(
            [
                fast_hashed_char_features(
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

    async def evaluate_one_async(self, model_id: str, text: str, allowed_choices: list[str]) -> dict:
        device = self.resolve_device()
        if device == "cuda":
            return await super().evaluate_one_async(model_id, text, allowed_choices)

        try:
            async with self._cpu_gate:
                _, predictions = await asyncio.to_thread(
                    self.predict_many,
                    model_id,
                    [text],
                    device,
                )
            self._cpu_direct_calls += 1
            return self._prediction_for_choices(predictions[0], allowed_choices)
        except Exception:
            self._cpu_direct_errors += 1
            raise
