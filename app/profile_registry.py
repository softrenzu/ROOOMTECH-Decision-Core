from __future__ import annotations

import json
import os
from typing import Any

from app.performance_models import FastProfileCreate, FastProfileSummary


class SharedFastProfileRegistry:
    """Optional Redis-backed registry for sharing fast-profile definitions across workers.

    The compiled profile remains process-local. Redis stores only the validated profile
    definition and summary; another worker lazily compiles it on first use. This keeps
    the realtime hot path local after the first request handled by each process.
    """

    def __init__(self):
        self.url = os.getenv("RTDC_REDIS_URL", "").strip()
        enabled = os.getenv("RTDC_FAST_PROFILE_REDIS_ENABLED", "false").strip().lower()
        self.enabled = enabled in {"1", "true", "yes", "on"}
        self.key = os.getenv("RTDC_FAST_PROFILE_REDIS_KEY", "rtdc:fast:profiles").strip() or "rtdc:fast:profiles"

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.url)

    def _redis(self):
        if not self.enabled:
            raise RuntimeError("RTDC_FAST_PROFILE_REDIS_ENABLED is not enabled")
        if not self.url:
            raise RuntimeError("RTDC_REDIS_URL is not configured")
        try:
            import redis.asyncio as redis
        except ImportError as exc:
            raise RuntimeError("install the 'distributed' extra to use shared fast profiles") from exc
        return redis.from_url(self.url, decode_responses=True)

    @staticmethod
    def _payload(spec: FastProfileCreate, summary: FastProfileSummary) -> str:
        return json.dumps(
            {
                "spec": spec.model_dump(mode="json"),
                "summary": summary.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode(raw: str) -> tuple[FastProfileCreate, FastProfileSummary]:
        payload: dict[str, Any] = json.loads(raw)
        return FastProfileCreate.model_validate(payload["spec"]), FastProfileSummary.model_validate(payload["summary"])

    async def save(self, spec: FastProfileCreate, summary: FastProfileSummary) -> None:
        if not self.configured:
            return
        client = self._redis()
        try:
            await client.hset(self.key, summary.profile_id, self._payload(spec, summary))
        finally:
            await client.aclose()

    async def get(self, profile_id: str) -> tuple[FastProfileCreate, FastProfileSummary] | None:
        if not self.configured:
            return None
        client = self._redis()
        try:
            raw = await client.hget(self.key, profile_id)
        finally:
            await client.aclose()
        if not raw:
            return None
        return self._decode(raw)

    async def list(self) -> list[tuple[FastProfileCreate, FastProfileSummary]]:
        if not self.configured:
            return []
        client = self._redis()
        try:
            rows = await client.hvals(self.key)
        finally:
            await client.aclose()
        values: list[tuple[FastProfileCreate, FastProfileSummary]] = []
        for raw in rows:
            try:
                values.append(self._decode(raw))
            except Exception:
                continue
        return values

    async def delete(self, profile_id: str) -> bool:
        if not self.configured:
            return False
        client = self._redis()
        try:
            return bool(await client.hdel(self.key, profile_id))
        finally:
            await client.aclose()
