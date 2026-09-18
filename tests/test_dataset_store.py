from app.dataset_models import DatasetCreate, DatasetExampleInput
from app.dataset_store import DatasetStore


def _dataset_request():
    return DatasetCreate(
        name="support-ja",
        decision_id="support_route",
        source_type="synthetic",
        provenance="Independently authored synthetic support examples for RTDC tests.",
        rights_attested=True,
        contains_personal_data=False,
        retention_days=30,
    )


def test_dataset_requires_rights_attestation():
    try:
        DatasetCreate(
            name="x",
            decision_id="route",
            source_type="operator_owned",
            provenance="owned test data",
            rights_attested=False,
        )
    except Exception as exc:
        assert "rights_attested" in str(exc)
    else:
        raise AssertionError("rights attestation must be required")


def test_dataset_splits_dedupe_and_fingerprint():
    store = DatasetStore(":memory:")
    try:
        created = store.create(_dataset_request())
        assert created.examples == 0
        examples = [
            DatasetExampleInput(text="ログインできません", label="account", split="train"),
            DatasetExampleInput(text="請求書をください", label="billing", split="train"),
            DatasetExampleInput(text="支払いについて", label="billing", split="validation"),
        ]
        added, duplicates = store.add_examples(created.id, examples)
        assert (added, duplicates) == (3, 0)
        added, duplicates = store.add_examples(
            created.id,
            [DatasetExampleInput(text="ログインできません", label="account", split="test")],
        )
        assert (added, duplicates) == (0, 1)
        summary = store.get(created.id)
        assert summary.train_examples == 2
        assert summary.validation_examples == 1
        assert summary.test_examples == 0
        assert summary.labels == {"account": 1, "billing": 2}
        assert len(summary.fingerprint_sha256) == 64
    finally:
        store.close()


def test_dataset_records_model_lineage():
    store = DatasetStore(":memory:")
    try:
        dataset = store.create(_dataset_request())
        store.add_examples(
            dataset.id,
            [DatasetExampleInput(text="a", label="one"), DatasetExampleInput(text="b", label="two")],
        )
        summary = store.get(dataset.id)
        record = store.record_model(
            dataset.id,
            "mdl_test",
            2,
            summary.fingerprint_sha256,
            {"model_id": "mdl_test"},
            {"accuracy": 0.9},
        )
        assert record.model_id == "mdl_test"
        rows = store.list_models(dataset.id)
        assert rows[0].dataset_fingerprint_sha256 == summary.fingerprint_sha256
        assert rows[0].evaluation == {"accuracy": 0.9}
    finally:
        store.close()
