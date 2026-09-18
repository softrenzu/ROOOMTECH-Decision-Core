# Governed datasets and active learning

ROOOMTECH Decision Core v0.10 adds an operator-controlled dataset registry for training and evaluating local decision models. It is designed to make data provenance and model lineage explicit rather than silently collecting production inputs.

## Independent-development boundary

The dataset workflow is intended for data the operator is authorized to use. Dataset creation requires an affirmative rights attestation and a provenance statement.

Allowed source categories are intentionally generic: operator-owned data, consented data, licensed data, independently created synthetic data, internal business records, and public-domain data.

Do not use outputs from another proprietary decision/model service as training, distillation, imitation, calibration, or benchmark data unless the applicable written terms expressly authorize that exact use. Do not import private APIs, prompts, internal schemas, copied benchmark sets, or copied vendor UI data into a governed dataset.

These engineering controls reduce avoidable risk but are not legal advice.

## Dataset lifecycle

Create a dataset:

```http
POST /v1/datasets
```

Example:

```json
{
  "name": "support-ja-2026q3",
  "decision_id": "support_route",
  "source_type": "internal_business_records",
  "provenance": "Support tickets collected by the operator under its customer-support process; labels reviewed internally.",
  "purpose": "support routing model training and evaluation",
  "rights_attested": true,
  "contains_personal_data": true,
  "allow_model_training": true,
  "retention_days": 365
}
```

Add examples using explicit `train`, `validation`, or `test` splits:

```http
POST /v1/datasets/{dataset_id}/examples
```

The store hashes every example and rejects duplicate text within the same dataset, even if a duplicate is submitted to a different split. This helps reduce accidental train/test leakage.

`GET /v1/datasets/{dataset_id}` returns a deterministic dataset fingerprint derived from example hashes, labels, and splits. A model lineage record stores the fingerprint used for training so a model can be traced back to the exact logical dataset state.

## Human-review loop

Low-confidence governed decisions can enter the Human Review Queue. Resolved review items can be imported into a governed dataset:

```http
POST /v1/datasets/{dataset_id}/import-reviews
```

Only resolved items matching the dataset's `decision_id` are considered. Only review items whose raw input was explicitly retained can become training examples. When a review item stored only a SHA-256 reference, there is deliberately no raw text to train on.

Use:

```http
GET /v1/active-learning/candidates
```

to retrieve pending review items ordered by uncertainty. This supports human labeling without automatically treating model output as ground truth.

## Train and evaluate

```http
POST /v1/datasets/{dataset_id}/train
```

Training uses only the explicit `train` split. The `validation` or `test` split can be selected for post-training evaluation. Held-out evaluation reports the existing RTDC benchmark metrics, including accuracy, macro F1, confidence, ECE, coverage, selective accuracy, confusion matrix, and timing.

Every training run creates a lineage record available at:

```http
GET /v1/datasets/{dataset_id}/models
```

The record includes model ID, training metadata, evaluation metrics, training-example count, and dataset fingerprint.

## Privacy and deployment

Dataset examples are intentionally stored because they are training/evaluation data. This differs from the review queue's privacy default, where raw input is not stored unless explicitly enabled.

The bundled SQLite dataset registry is suitable for local, evaluation, and single-node deployments. Before enterprise or multi-node use, move governed datasets to approved managed storage with encryption, RBAC, backups, audit logging, retention/deletion controls, and any data-residency controls required by the operator.

Personal data should be minimized or pseudonymized before training where practical. Operators remain responsible for their legal basis, notices, consent/contract obligations, retention schedule, access controls, and cross-border data handling.
