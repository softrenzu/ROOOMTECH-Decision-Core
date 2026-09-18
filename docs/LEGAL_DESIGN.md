# Legal design notes

This document records the product-separation rules used for ROOOMTECH Decision Core. It is an engineering record, not legal advice. These controls reduce avoidable risk but cannot guarantee that a third party will never make a claim.

See also `docs/INDEPENDENT_PRODUCT_DEVELOPMENT.md` for the operational development policy.

## Independent implementation rules

1. Do not copy or adapt proprietary source code, model weights, private APIs, prompts, UI assets, benchmark datasets, product copy or documentation from another vendor.
2. Do not use another vendor's service output to train, distill, imitate, calibrate or benchmark this product unless that vendor's written terms expressly permit the exact use.
3. Do not reverse engineer a third-party hosted service or attempt to infer proprietary architecture from black-box behavior.
4. Implement features from general concepts, public standards and ROOOMTECH's own requirements, tests and datasets.
5. Keep product naming, endpoint naming, package naming, domains, logos, UI layout and marketing copy independently created and distinct from third-party marks and trade dress.
6. Do not claim official compatibility, endorsement, succession, clone status, drop-in replacement or equivalence without written permission and verified evidence.
7. Only connect external APIs or models through interfaces that the deployer is authorized to use.
8. Local classifier training must use data that the operator has the right to process and use for training.
9. Keep provenance records for datasets, model weights, dependencies and benchmark inputs used in commercial releases.
10. Comparative benchmark publication requires a separate terms-of-service and methodology review before release.

## Local model design

The local classifier uses ROOOMTECH code for Unicode character n-gram feature hashing and a small linear neural classifier. It is trained only from operator-supplied examples. It does not require a proprietary third-party decision-model API, output stream, prompt set or model weights.

## Decision Studio, calibration and human review

The v0.9 Decision Studio is independently designed and branded. It uses ROOOMTECH-created layout, copy, schemas and endpoints rather than reproducing a third-party console or playground.

Calibration consumes operator-supplied held-out `confidence + correct` observations. It does not require third-party service outputs. Human review is privacy-conscious by default: raw input is not retained unless the operator explicitly enables storage; otherwise only a SHA-256 digest is retained for correlation.

Operators remain responsible for lawful processing, access controls, retention and any required notices or agreements when raw customer or employee data is stored.

## Commercial licensing model

- Natural-person private non-business use: personal-use source license.
- Any business, professional, organizational, institutional or production use: separate paid commercial license.
- Commercial license tokens are signed with Ed25519. Keep the private signing key outside this repository.

## Release checklist

Before a public commercial launch:

- run a trademark clearance search for the final product name in intended markets;
- have qualified counsel review product naming, commercial terms, comparison claims and the personal/commercial licenses;
- run dependency, model-weight and license scanning;
- verify privacy disclosures, retention rules and security controls for deployments that process personal data;
- keep independent-development records, original design artifacts and release notes;
- preserve dataset/model provenance and written permission for any third-party material;
- review external service terms before publishing comparative benchmarks or compatibility statements.
