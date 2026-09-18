from __future__ import annotations

import json
import os
import uuid
from contextlib import suppress
from typing import Any, Awaitable, Callable

from app.performance_models import FastProfileCreate, FastProfileSummary


class SharedFastProfileRegistry:
    """Optional Redis-backed registry for sharing fast-profile definitions across workers.

    Redis stores validated profile definitions and summaries. A lightweight Pub/Sub
    channel invalidates process-local compiled copies when another worker updates or
    deletes a profile, so the realtime hot path remains local between changes.
    """

    def __init__(self):
        self.url = os.getenv("RTDC_REDIS_URL", "").strip()
        enabled = os.getenv("RTDC_FAST_PROFILE_REDIS_ENABLED", "false").strip().lower()
        self.enabled = enabled in {"1", "true", "yes", "on"}
        self.key = os.getenv("RTDC_FAST_PROFILE_REDIS_KEY", "rtdc:fast:profiles").strip() or "rtdc:fast:profiles"
        self.channel = os.getenv("RTDC_FAST_PROFILE_REDIS_CHANNEL", f"{self.key}:events").strip() or f"{self.key}:events"
        self.instance_id = uuid.uuid4().hex

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

    def _event(self, action: str, profile_id: str) -> str:
        return json.dumps(
            {"action": action, "profile_id": profile_id, "source": self.instance_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def save(self, spec: FastProfileCreate, summary: FastProfileSummary) -> None:
        if not self.configured:
            return
        client = self._redis()
        try:
            pipe = client.pipeline(transaction=True)
            pipe.hset(self.key, summary.profile_id, self._payload(spec, summary))
            pipe.publish(self.channel, self._event("upsert", summary.profile_id))
            await pipe.execute()
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
            deleted = bool(await client.hdel(self.key, profile_id))
            if deleted:
                await client.publish(self.channel, self._event("delete", profile_id))
            return deleted
        finally:
            await client.aclose()

    async def listen(self, callback: Callable[[str, str], Awaitable[None]]) -> None:
        """Listen for profile changes made by other application processes."""
        if not self.configured:
            return
        client = self._redis()
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(self.channel)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = json.loads(message.get("data") or "{}")
                    if event.get("source") == self.instance_id:
                        continue
                    action = str(event.get("action") or "")
                    profile_id = str(event.get("profile_id") or "")
                    if action in {"upsert", "delete"} and profile_id:
                        await callback(action, profile_id)
                except Exception:
                    continue
        finally:
            with suppress(Exception):
                await pubsub.unsubscribe(self.channel)
            with suppress(Exception):
                await pubsub.aclose()
            with suppress(Exception):
                await client.aclose()
