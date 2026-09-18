from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dataset_store import DatasetStore
from app.enterprise_api import install_enterprise_api
from app.enterprise_models import ProjectCreate, ProjectKeyCreate
from app.models import LocalPrediction
from app.review_store import ReviewStore
from app.tenant_api import install_tenant_resource_api
from app.tenant_resources import TenantResourceRegistry


class FakeLocalModels:
    def get_model(self, model_id: str):
        return SimpleNamespace(model_id=model_id, decision_id="route")

    def predict_many(self, model_id: str, inputs: list[str], device: str = "auto"):
        return "cpu", [
            LocalPrediction(
                selected="account",
                confidence=0.91,
                scores={"account": 0.91, "billing": 0.09},
            )
            for _ in inputs
        ]


def test_registry_hides_resources_owned_by_other_projects(monkeypatch, tmp_path):
    db = tmp_path / "enterprise.sqlite3"
    monkeypatch.setenv("RTDC_ENTERPRISE_DB", str(db))
    monkeypatch.setenv("RTDC_ENTERPRISE_KEY_PEPPER", "tenant-test-pepper")

    from app.enterprise_store import EnterpriseStore

    enterprise = EnterpriseStore(str(db))
    registry = TenantResourceRegistry(str(db))
    try:
        project_a = enterprise.create_project(ProjectCreate(name="A"))
        project_b = enterprise.create_project(ProjectCreate(name="B"))
        registry.register_resource(project_a.id, "dataset", "ds_shared_name")

        registry.assert_owner(project_a.id, "dataset", "ds_shared_name")
        assert registry.list_ids(project_a.id, "dataset") == ["ds_shared_name"]
        assert registry.list_ids(project_b.id, "dataset") == []

        with pytest.raises(FileNotFoundError):
            registry.assert_owner(project_b.id, "dataset", "ds_shared_name")
        with pytest.raises(PermissionError):
            registry.register_resource(project_b.id, "dataset", "ds_shared_name")
    finally:
        registry.close()
        enterprise.close()


def _tenant_app(monkeypatch, tmp_path):
    monkeypatch.setenv("RTDC_ADMIN_API_KEY", "admin-test")
    monkeypatch.setenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "true")
    monkeypatch.setenv("RTDC_ENTERPRISE_DB", str(tmp_path / "enterprise.sqlite3"))
    monkeypatch.setenv("RTDC_ENTERPRISE_KEY_PEPPER", "tenant-test-pepper")

    app = FastAPI()
    local = FakeLocalModels()
    enterprise = install_enterprise_api(app, local)
    dataset_store = DatasetStore(":memory:")
    review_store = ReviewStore(":memory:")
    datasets = SimpleNamespace(datasets=dataset_store)
    tenant = install_tenant_resource_api(
        app, enterprise, datasets, review_store, local
    )
    return app, enterprise, tenant, dataset_store, review_store


def _project_key(enterprise, name: str):
    project = enterprise.store.create_project(
        ProjectCreate(name=name, request_quota_per_day=100)
    )
    issued = enterprise.store.issue_key(
        project.id,
        ProjectKeyCreate(
            name="all",
            scopes=["inference", "realtime", "datasets", "reviews", "models"],
        ),
    )
    return project, issued.key


def test_project_apis_block_cross_tenant_dataset_review_and_model(monkeypatch, tmp_path):
    app, enterprise, tenant, dataset_store, review_store = _tenant_app(monkeypatch, tmp_path)
    project_a, key_a = _project_key(enterprise, "Tenant A")
    project_b, key_b = _project_key(enterprise, "Tenant B")
    headers_a = {"X-RTDC-Project-Key": key_a}
    headers_b = {"X-RTDC-Project-Key": key_b}

    try:
        with TestClient(app) as client:
            dataset = client.post(
                "/v1/project/datasets",
                headers=headers_a,
                json={
                    "name": "A data",
                    "decision_id": "route",
                    "source_type": "operator_owned",
                    "provenance": "created and owned by tenant A",
                    "rights_attested": True,
                },
            )
            assert dataset.status_code == 200
            dataset_id = dataset.json()["id"]
            assert client.get(
                f"/v1/project/datasets/{dataset_id}", headers=headers_a
            ).status_code == 200
            assert client.get(
                f"/v1/project/datasets/{dataset_id}", headers=headers_b
            ).status_code == 404

            review = client.post(
                "/v1/project/reviews",
                headers=headers_a,
                json={
                    "decision_id": "route",
                    "input_text": "tenant A secret text",
                    "store_input": True,
                },
            )
            assert review.status_code == 200
            review_id = review.json()["id"]
            assert client.get(
                f"/v1/project/reviews/{review_id}", headers=headers_a
            ).status_code == 200
            assert client.get(
                f"/v1/project/reviews/{review_id}", headers=headers_b
            ).status_code == 404

            tenant.registry.register_resource(project_a.id, "model", "mdl_a")
            own_prediction = client.post(
                "/v1/project/models/mdl_a/predict",
                headers=headers_a,
                json={"inputs": ["ログインできません"], "device": "cpu"},
            )
            assert own_prediction.status_code == 200
            assert own_prediction.json()["predictions"][0]["selected"] == "account"

            cross_prediction = client.post(
                "/v1/project/models/mdl_a/predict",
                headers=headers_b,
                json={"inputs": ["ログインできません"], "device": "cpu"},
            )
            assert cross_prediction.status_code == 404
    finally:
        dataset_store.close()
        review_store.close()
