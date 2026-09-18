import importlib
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.enterprise_models import ProjectCreate, ProjectKeyCreate


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("RTDC_ADMIN_API_KEY", "admin-ws-test")
    monkeypatch.setenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "true")
    monkeypatch.setenv("RTDC_ENTERPRISE_DB", str(tmp_path / "enterprise.sqlite3"))
    monkeypatch.setenv("RTDC_ENTERPRISE_KEY_PEPPER", "ws-test-pepper")
    monkeypatch.setenv("RTDC_REVIEW_DB", str(tmp_path / "reviews.sqlite3"))
    monkeypatch.setenv("RTDC_DATASET_DB", str(tmp_path / "datasets.sqlite3"))
    monkeypatch.setenv("RTDC_LOCAL_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("ROOOM_LICENSE_MODE", "personal")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_websocket_requires_realtime_project_scope_and_rechecks_revocation(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    project = main.enterprise_services.store.create_project(
        ProjectCreate(name="WS tenant", request_quota_per_day=10)
    )
    realtime_key = main.enterprise_services.store.issue_key(
        project.id,
        ProjectKeyCreate(name="ws", scopes=["realtime"]),
    )
    inference_key = main.enterprise_services.store.issue_key(
        project.id,
        ProjectKeyCreate(name="inference", scopes=["inference"]),
    )

    try:
        with TestClient(main.app) as client:
            with pytest.raises(WebSocketDisconnect) as missing:
                with client.websocket_connect("/v1/realtime/ws"):
                    pass
            assert missing.value.code == 4401

            with pytest.raises(WebSocketDisconnect) as wrong_scope:
                with client.websocket_connect(
                    "/v1/realtime/ws",
                    headers={"X-RTDC-Project-Key": inference_key.key},
                ):
                    pass
            assert wrong_scope.value.code == 4401

            with client.websocket_connect(
                "/v1/realtime/ws",
                headers={"X-RTDC-Project-Key": realtime_key.key},
            ) as websocket:
                websocket.send_json({"request_id": "r1", "kind": "ping", "request": {}})
                first = websocket.receive_json()
                assert first["ok"] is True
                assert first["data"]["pong"] is True

                assert main.enterprise_services.store.revoke_key(
                    project.id, realtime_key.id
                ) is True
                websocket.send_json({"request_id": "r2", "kind": "ping", "request": {}})
                revoked = websocket.receive_json()
                assert revoked["ok"] is False

            audit = main.enterprise_services.store.list_audit(project.id, limit=20)
            assert any(row.method == "WS" and row.path == "/v1/realtime/ws" for row in audit)
    finally:
        sys.modules.pop("app.main", None)
