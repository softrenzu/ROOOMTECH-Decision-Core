from __future__ import annotations

import hashlib
import math
import time
import unicodedata
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SemanticItem(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticMatrixRequest(BaseModel):
    left: list[SemanticItem] = Field(min_length=1, max_length=5000)
    right: list[SemanticItem] = Field(min_length=1, max_length=5000)
    top_k_per_left: int = Field(default=5, ge=1, le=200)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    exclude_same_id: bool = True
    feature_dim: int = Field(default=16384, ge=1024, le=262144)
    ngram_min: int = Field(default=2, ge=1, le=5)
    ngram_max: int = Field(default=4, ge=1, le=6)
    max_logical_pairs: int = Field(default=2_000_000, ge=1, le=25_000_000)

    @model_validator(mode="after")
    def validate_request(self):
        if self.ngram_max < self.ngram_min:
            raise ValueError("ngram_max must be >= ngram_min")
        logical_pairs = len(self.left) * len(self.right)
        if logical_pairs > self.max_logical_pairs:
            raise ValueError(
                f"semantic matrix too large: {logical_pairs} logical pairs exceeds max_logical_pairs={self.max_logical_pairs}"
            )
        return self


class SemanticMatch(BaseModel):
    left_id: str
    right_id: str
    score: float
    rank: int
    right_metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticMatrixResponse(BaseModel):
    left_items: int
    right_items: int
    logical_pairs: int
    candidate_pairs_scored: int
    matches: list[SemanticMatch]
    latency_ms: float
    method: str = "local_sparse_unicode_char_ngram_cosine"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())


def _feature_id(value: str, feature_dim: int) -> int:
    digest = hashlib.blake2b(
        value.encode("utf-8", errors="ignore"),
        digest_size=8,
        person=b"RTDCMTRX",
    ).digest()
    return int.from_bytes(digest, "big") % feature_dim


def sparse_signature(
    text: str,
    feature_dim: int = 16384,
    ngram_min: int = 2,
    ngram_max: int = 4,
) -> dict[int, float]:
    normalized = _normalize(text)
    counts: dict[int, float] = defaultdict(float)
    if not normalized:
        return {}
    for n in range(ngram_min, ngram_max + 1):
        if len(normalized) < n:
            continue
        for index in range(len(normalized) - n + 1):
            gram = normalized[index : index + n]
            if gram.isspace():
                continue
            counts[_feature_id(gram, feature_dim)] += 1.0
    if not counts:
        return {}
    norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
    return {key: value / norm for key, value in counts.items()}


def sparse_cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    score = sum(value * right.get(key, 0.0) for key, value in left.items())
    return max(0.0, min(1.0, float(score)))


class SemanticMatrixEngine:
    """Many-to-many local semantic scoring without external model calls.

    An inverted feature index avoids scoring pairs that share no hashed character
    n-gram features. The method is intentionally generic and independently designed;
    it is suitable for candidate generation, internal-link discovery, record matching,
    retrieval prefiltering and other high-cardinality workflows.
    """

    def run(self, request: SemanticMatrixRequest) -> SemanticMatrixResponse:
        started = time.perf_counter()
        right_signatures = [
            sparse_signature(item.text, request.feature_dim, request.ngram_min, request.ngram_max)
            for item in request.right
        ]
        inverted: dict[int, set[int]] = defaultdict(set)
        for index, signature in enumerate(right_signatures):
            for feature_id in signature:
                inverted[feature_id].add(index)

        matches: list[SemanticMatch] = []
        scored = 0
        for left_item in request.left:
            signature = sparse_signature(
                left_item.text,
                request.feature_dim,
                request.ngram_min,
                request.ngram_max,
            )
            candidates: set[int] = set()
            for feature_id in signature:
                candidates.update(inverted.get(feature_id, ()))

            ranked: list[tuple[float, str, int]] = []
            for right_index in candidates:
                right_item = request.right[right_index]
                if request.exclude_same_id and left_item.id == right_item.id:
                    continue
                score = sparse_cosine(signature, right_signatures[right_index])
                scored += 1
                if score >= request.min_score:
                    ranked.append((score, right_item.id, right_index))

            ranked.sort(key=lambda row: (-row[0], row[1]))
            for rank, (score, _, right_index) in enumerate(
                ranked[: request.top_k_per_left], start=1
            ):
                right_item = request.right[right_index]
                matches.append(
                    SemanticMatch(
                        left_id=left_item.id,
                        right_id=right_item.id,
                        score=round(score, 6),
                        rank=rank,
                        right_metadata=right_item.metadata,
                    )
                )

        return SemanticMatrixResponse(
            left_items=len(request.left),
            right_items=len(request.right),
            logical_pairs=len(request.left) * len(request.right),
            candidate_pairs_scored=scored,
            matches=matches,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
