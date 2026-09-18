import pytest

from app.enterprise_models import ProjectCreate, ProjectKeyCreate, ProjectUpdate
from app.enterprise_store import EnterpriseStore


def test_project_keys_are_scoped_hashed_and_revocable(monkeypatch):
    monkeypatch.setenv("RTDC_ENTERPRISE_KEY_PEPPER", "test-pepper")
    store = EnterpriseStore(":memory:")
    try:
        project = store.create_project(ProjectCreate(name="Acme", request_quota_per_day=2))
        issued = store.issue_key(
            project.id,
            ProjectKeyCreate(name="prod", scopes=["inference", "guardrails"]),
        )
        assert issued.key.startswith("rtdc_pk_")
        rows = store.list_keys(project.id)
        assert rows[0].prefix == issued.prefix
        assert not hasattr(rows[0], "key")

        auth = store.authenticate(issued.key, required_scope="inference")
        assert auth.project_id == project.id
        with pytest.raises(PermissionError):
            store.authenticate(issued.key, required_scope="datasets", consume_quota=False)

        assert store.revoke_key(project.id, issued.id) is True
        with pytest.raises(PermissionError):
            store.authenticate(issued.key, consume_quota=False)
    finally:
        store.close()


def test_daily_quota_is_enforced():
    store = EnterpriseStore(":memory:")
    try:
        project = store.create_project(ProjectCreate(name="Quota", request_quota_per_day=2))
        issued = store.issue_key(project.id, ProjectKeyCreate(name="key", scopes=["inference"]))
        store.authenticate(issued.key, required_scope="inference")
        second = store.authenticate(issued.key, required_scope="inference")
        assert second.requests_today == 2
        with pytest.raises(OverflowError):
            store.authenticate(issued.key, required_scope="inference")
    finally:
        store.close()


def test_audit_does_not_store_request_body():
    store = EnterpriseStore(":memory:")
    try:
        project = store.create_project(ProjectCreate(name="Audit"))
        event = store.audit(
            project_id=project.id,
            key_id=None,
            method="POST",
            path="/v1/decide",
            status_code=200,
            latency_ms=1.2345,
            request_id="req-1",
        )
        assert event.path == "/v1/decide"
        rows = store.list_audit(project.id)
        assert rows[0].request_id == "req-1"
        assert "body" not in rows[0].model_fields
    finally:
        store.close()


def test_model_promotion_and_rollback_history():
    store = EnterpriseStore(":memory:")
    try:
        project = store.create_project(ProjectCreate(name="Models"))
        first = store.promote_model(project.id, "route", "production", "mdl_a", None, "first")
        second = store.promote_model(project.id, "route", "production", "mdl_b", None, "second")
        assert first.version == 1
        assert second.version == 2
        rolled, old = store.rollback_model(project.id, "route", "production", None)
        assert old == "mdl_b"
        assert rolled.model_id == "mdl_a"
        assert rolled.version == 3
    finally:
        store.close()


def test_project_can_be_disabled():
    store = EnterpriseStore(":memory:")
    try:
        project = store.create_project(ProjectCreate(name="Disabled"))
        issued = store.issue_key(project.id, ProjectKeyCreate(name="key", scopes=["inference"]))
        store.update_project(project.id, ProjectUpdate(enabled=False))
        with pytest.raises(PermissionError):
            store.authenticate(issued.key, consume_quota=False)
    finally:
        store.close()
