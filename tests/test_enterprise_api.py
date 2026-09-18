from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.enterprise_api import install_enterprise_api


class FakeLocalModels:
    def get_model(self, model_id: str):
        return SimpleNamespace(model_id=model_id, decision_id="route")


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("RTDC_ADMIN_API_KEY", "admin-test")
    monkeypatch.setenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "true")
    monkeypatch.setenv("RTDC_ENTERPRISE_DB", str(tmp_path / "enterprise.sqlite3"))
    monkeypatch.setenv("RTDC_ENTERPRISE_KEY_PEPPER", "pepper-test")
    app = FastAPI()

    @app.get("/v1/echo")
    async def echo():
        return {"ok": True}

    @app.get("/v1/guardrails/demo")
    async def guardrail_demo():
        return {"ok": True}

    install_enterprise_api(app, FakeLocalModels())
    return app


def _admin_headers():
    return {"X-RTDC-Admin-Key": "admin-test"}


def test_project_key_middleware_quota_and_audit(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        project = client.post(
            "/v1/admin/projects",
            headers=_admin_headers(),
            json={"name": "Tenant A", "request_quota_per_day": 1},
        )
        assert project.status_code == 200
        project_id = project.json()["id"]
        issued = client.post(
            f"/v1/admin/projects/{project_id}/keys",
            headers=_admin_headers(),
            json={"name": "prod", "scopes": ["inference"]},
        )
        assert issued.status_code == 200
        token = issued.json()["key"]

        assert client.get("/v1/echo").status_code == 401
        allowed = client.get("/v1/echo", headers={"X-RTDC-Project-Key": token})
        assert allowed.status_code == 200
        assert allowed.headers["X-RTDC-Project-ID"] == project_id
        assert allowed.headers["X-RTDC-Quota-Remaining"] == "0"
        assert client.get("/v1/echo", headers={"X-RTDC-Project-Key": token}).status_code == 429

        audit = client.get(
            f"/v1/admin/audit?project_id={project_id}", headers=_admin_headers()
        )
        assert audit.status_code == 200
        assert len(audit.json()) == 1
        assert audit.json()[0]["path"] == "/v1/echo"


def test_scope_is_enforced(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        project_id = client.post(
            "/v1/admin/projects",
            headers=_admin_headers(),
            json={"name": "Tenant B", "request_quota_per_day": 10},
        ).json()["id"]
        inference_key = client.post(
            f"/v1/admin/projects/{project_id}/keys",
            headers=_admin_headers(),
            json={"name": "inference", "scopes": ["inference"]},
        ).json()["key"]
        guardrail_key = client.post(
            f"/v1/admin/projects/{project_id}/keys",
            headers=_admin_headers(),
            json={"name": "guardrails", "scopes": ["guardrails"]},
        ).json()["key"]

        denied = client.get(
            "/v1/guardrails/demo", headers={"X-RTDC-Project-Key": inference_key}
        )
        assert denied.status_code == 401
        allowed = client.get(
            "/v1/guardrails/demo", headers={"X-RTDC-Project-Key": guardrail_key}
        )
        assert allowed.status_code == 200
