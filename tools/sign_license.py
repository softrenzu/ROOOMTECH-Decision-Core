"""Vendor-side helper for signing commercial license tokens.

Keep the private key outside the repository. Example env:
ROOOM_LICENSE_PRIVATE_KEY_B64=<raw 32-byte Ed25519 private key, base64>
"""

import argparse
import base64
import json
import os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--licensee", required=True)
    parser.add_argument("--expires-at", default=None, help="ISO 8601, e.g. 2027-09-18T00:00:00Z")
    args = parser.parse_args()

    key_b64 = os.environ["ROOOM_LICENSE_PRIVATE_KEY_B64"]
    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_b64))
    payload = {
        "product": "ROOOMTECH Decision Core",
        "licensee": args.licensee,
        "expires_at": args.expires_at,
        "features": ["commercial"],
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = private_key.sign(payload_bytes)
    print(f"{b64url(payload_bytes)}.{b64url(signature)}")


if __name__ == "__main__":
    main()
