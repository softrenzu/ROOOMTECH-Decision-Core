# Parallel Probability-Head Models

ROOOMTECH Decision Core v0.23 adds an optional RTDC-owned local model class for workloads where one state should produce many typed decisions without repeatedly encoding the same input or generating prose.

This is an independent implementation of general multi-task classification, shared representations, proper scoring rules, temperature scaling, and parallel output heads. It is not an implementation of a third-party sampler or proprietary reinforcement-learning algorithm.

## Architecture

```text
operator state
    |
Unicode char n-gram hash (computed once)
    |
shared linear encoder -> GELU -> LayerNorm
    |---------------------|---------------------|
 boolean head        categorical head       scalar-level head
 probabilities        probabilities          probabilities + expected value
```

All configured heads are evaluated from one shared encoded representation in one network forward pass. The model does not autoregressively generate a natural-language answer.

## Training objective

Each observed head uses cross-entropy plus an optional Brier-score penalty:

```text
head_loss = cross_entropy + brier_weight * brier_score
```

Both terms are standard proper/probabilistic classification objectives. After training, each head receives independent held-out temperature scaling selected by validation negative log likelihood. The saved validation report includes accuracy, Brier score, ECE, temperature, and held-out sample count for every head.

This is called calibration-aware training in RTDC documentation. We do not call it RLCD and do not claim it reproduces any proprietary training method.

## API

```text
POST /v1/project/parallel-models/train
GET  /v1/project/parallel-models
GET  /v1/project/parallel-models/{model_id}
POST /v1/project/parallel-models/{model_id}/predict
```

Training/list/model inspection use the enterprise `models` scope. Prediction uses the `inference` scope. Model metadata contains the owning project and another project receives not-found rather than the owner identity.

Install the optional ML runtime with:

```text
pip install -e '.[ml]'
```

The default model directory is `data/parallel-models` and may be changed with `RTDC_PARALLEL_MODEL_DIR`.

## Decision Accelerator integration

`POST /v1/project/accelerate` accepts an optional `parallel_model_id`. Judgment IDs that also exist as heads in that model are resolved from one shared forward pass first. Other judgments can still use an individual RTDC local classifier or deterministic rules. Only judgments that remain below their confidence/margin/entropy policy are eligible for the bounded external-LLM batch.

This gives the production path:

```text
shared local probability heads
          |
          +-- confident -> use directly
          |
          +-- uncertain -> bounded external model fallback
                              |
                              +-- still uncertain -> review/abstain
```

The response reports how many judgments used the shared model, how many used individual local models or rules, how many were locally resolved, and how many were escalated externally.

## What this improves

Compared with running an LLM for every tiny decision, this path can avoid free-form generation and external token billing for locally resolved judgments. Compared with independent local classifiers, it avoids re-encoding the same state once per head when the judgments have been trained together.

Actual latency, cost reduction, calibration, and accuracy depend on hardware and data. The existence of parallel heads is not evidence of frontier-model intelligence; measure every deployment on operator-owned held-out outcomes.

## Data and privacy

Training raw text is used during training but is not written into the RTDC model metadata or weight files by this provider. The operator remains responsible for the lawful use of training data and for protecting any training request while it is in memory or in transit.

Do not use another vendor's hosted outputs to train, imitate, distill, tune, calibrate, or benchmark this model unless a separate documented agreement expressly permits that exact use.
