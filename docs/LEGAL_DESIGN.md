# Legal design notes

This document records the product-separation rules used for ROOOMTECH Decision Core. It is an engineering record, not legal advice.

## Independent implementation rules

1. Do not copy or adapt proprietary source code, model weights, private APIs, prompts, UI assets, benchmark datasets or documentation from another vendor.
2. Do not use another vendor's service output to train, distill, imitate or benchmark this product unless that vendor's written terms expressly permit it.
3. Do not reverse engineer a third-party hosted service or attempt to infer proprietary architecture from black-box behavior.
4. Implement features from general concepts and ROOOMTECH's own requirements, tests and datasets.
5. Keep product naming, package naming, domains and logos distinct from third-party marks.
6. Do not claim official compatibility, endorsement, succession or equivalence without written permission and verified evidence.
7. Only connect external APIs or models through interfaces that the deployer is authorized to use.

## Commercial licensing model

- Natural-person private non-business use: personal-use source license.
- Any business, professional, organizational, institutional or production use: separate paid commercial license.
- Commercial license tokens are signed with Ed25519. Keep the private signing key outside this repository.

## Release checklist

Before a public commercial launch:

- rename the GitHub repository to `ROOOMTECH-Decision-Core`;
- run a trademark clearance search for the final product name in intended markets;
- have Japanese counsel review the personal and commercial license terms;
- run dependency/license scanning;
- verify privacy disclosures for any deployment that processes personal data;
- keep independent-development records and release notes.
