import pytest

from app.engine import DecisionEngine
from app.fastpath import FastPathEngine
from app.operations import OperationalDecisionEngine
from app.performance_models import FastDecisionRequest, FastProfileCreate
from app.schema_extraction import SchemaExtractor


class FakeSharedRegistry:
    configured = True

    def __init__(self, store):
        self.store = store

    async def save(self, spec, summary):
        self.store[summary.profile_id] = (spec, summary)

    async def get(self, profile_id):
        return self.store.get(profile_id)

    async def list(self):
        return list(self.store.values())

    async def delete(self, profile_id):
        return self.store.pop(profile_id, None) is not None


def make_fast_path():
    decision = DecisionEngine()
    operations = OperationalDecisionEngine(decision)
    extractor = SchemaExtractor(decision.model)
    return FastPathEngine(decision, operations, extractor)


@pytest.mark.asyncio
async def test_second_worker_lazily_compiles_shared_profile():
    store = {}
    worker_a = make_fast_path()
    worker_b = make_fast_path()
    worker_a.registry = FakeSharedRegistry(store)
    worker_b.registry = FakeSharedRegistry(store)

    spec = FastProfileCreate(
        profile_id="shared-route",
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
    await worker_a.create_profile(spec)
    assert "shared-route" in store
    assert worker_b.list_profiles() == []

    result = await worker_b.execute(
        FastDecisionRequest(profile_id="shared-route", input="ログインできません")
    )
    assert result.ok is True
    assert result.data["route"] == "account"
    assert worker_b.runtime_info()["shared_profile_loads"] == 1
    assert len(worker_b.list_profiles()) == 1

    shared = await worker_b.list_profiles_shared()
    assert [item.profile_id for item in shared] == ["shared-route"]


@pytest.mark.asyncio
async def test_shared_profile_delete_removes_registry_entry():
    store = {}
    worker = make_fast_path()
    worker.registry = FakeSharedRegistry(store)
    await worker.create_profile(
        FastProfileCreate(
            profile_id="delete-me",
            kind="detect",
            request={
                "property": "refund request",
                "provider": "rules",
                "keywords": ["返金"],
            },
        )
    )
    assert await worker.delete_profile("delete-me") is True
    assert store == {}
