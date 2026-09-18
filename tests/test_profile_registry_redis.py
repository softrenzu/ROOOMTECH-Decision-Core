import os
import uuid

import pytest

from app.engine import DecisionEngine
from app.fastpath import FastPathEngine
from app.operations import OperationalDecisionEngine
from app.performance_models import FastDecisionRequest, FastProfileCreate
from app.schema_extraction import SchemaExtractor


pytestmark = pytest.mark.skipif(
    not os.getenv("RTDC_TEST_REDIS_URL"),
    reason="RTDC_TEST_REDIS_URL is not configured",
)


def make_fast_path():
    decision = DecisionEngine()
    operations = OperationalDecisionEngine(decision)
    extractor = SchemaExtractor(decision.model)
    return FastPathEngine(decision, operations, extractor)


@pytest.mark.asyncio
async def test_real_redis_shares_profile_between_engine_instances(monkeypatch):
    redis_url = os.environ["RTDC_TEST_REDIS_URL"]
    key = f"rtdc:test:fast:{uuid.uuid4().hex}"
    monkeypatch.setenv("RTDC_REDIS_URL", redis_url)
    monkeypatch.setenv("RTDC_FAST_PROFILE_REDIS_ENABLED", "true")
    monkeypatch.setenv("RTDC_FAST_PROFILE_REDIS_KEY", key)

    worker_a = make_fast_path()
    worker_b = make_fast_path()
    assert worker_a.registry.configured is True
    assert worker_b.registry.configured is True

    profile_id = "redis-shared-route"
    await worker_a.create_profile(
        FastProfileCreate(
            profile_id=profile_id,
            kind="route",
            target_ms=150,
            request={
                "provider": "rules",
                "min_confidence": 0.5,
                "routes": [
                    {"id": "billing", "keywords": ["請求"]},
                    {"id": "account", "keywords": ["ログイン"]},
                ],
            },
        )
    )

    assert worker_b.list_profiles() == []
    result = await worker_b.execute(
        FastDecisionRequest(profile_id=profile_id, input="ログインできません")
    )
    assert result.ok is True
    assert result.data["route"] == "account"
    assert worker_b.runtime_info()["shared_profile_loads"] == 1

    shared = await worker_b.list_profiles_shared()
    assert [item.profile_id for item in shared] == [profile_id]

    assert await worker_a.delete_profile(profile_id) is True
    assert await worker_a.registry.get(profile_id) is None
