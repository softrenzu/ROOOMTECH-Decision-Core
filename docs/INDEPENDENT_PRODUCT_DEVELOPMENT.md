# Independent product development policy

ROOOMTECH Decision Core is intended to be developed as an independent product. This policy is an engineering control for reducing intellectual-property, contractual, branding and unfair-competition risk. It is not legal advice and cannot guarantee that a third party will never make a claim.

## Product-separation rules

1. Build from ROOOMTECH requirements, general software/ML concepts, public standards and operator-owned datasets.
2. Do not copy another vendor's source code, model weights, prompts, internal schemas, SDK implementation, non-public APIs, screenshots, UI layout, visual assets, marketing copy, documentation text or proprietary benchmark datasets.
3. Do not train, distill, imitate, calibrate or tune ROOOMTECH models from another decision service's outputs unless explicit written terms allow that exact use.
4. Do not reverse engineer a hosted model or infer non-public implementation details through systematic probing.
5. Do not publish comparative benchmark claims based on a third-party service unless the service terms permit benchmarking and the test methodology is lawful, reproducible and accurately described.
6. Keep names, logos, package identifiers, endpoint naming, UI copy and visual design independently created. Do not use a third-party mark in the product name, repository name, logo, domain or product headline.
7. Do not claim clone, successor, drop-in compatibility, official alternative, endorsement, affiliation or equivalent performance without a separate documented basis and legal review.
8. Record the origin of datasets, model weights and external dependencies used in releases. Training data must be data the operator has the right to process for that purpose.
9. Prefer functionality described in generic terms such as classification, routing, calibration, review queues, verification, ranking and structured extraction rather than copying another vendor's product vocabulary or API surface.
10. Before commercial launch, run trademark clearance, dependency/license review, privacy review and counsel review for the intended jurisdictions and customer terms.

## Decision Studio separation

The ROOOMTECH Decision Studio is intentionally implemented as its own interface and API surface. It uses ROOOMTECH naming, layout, copy and data models. It is not intended to reproduce another vendor's playground, console or dashboard.

The studio's calibration system operates on operator-supplied held-out outcomes consisting of confidence/correctness pairs. It does not require another vendor's predictions or labels.

The human-review workflow is an independently designed operational feature. Raw inputs are not retained by default. When the operator enables raw-input retention, the operator is responsible for having an appropriate legal basis, retention policy and access controls.

## Development evidence

For releases intended for commercial use, retain:

- commit history and issue history showing independent feature requirements;
- original test fixtures and synthetic datasets;
- dependency and model-license records;
- dataset provenance and operator attestations;
- benchmark methodology and hardware details;
- product screenshots showing independently created interface design;
- counsel/trademark review records where applicable.

These records help demonstrate independent development, but they do not eliminate litigation risk.
