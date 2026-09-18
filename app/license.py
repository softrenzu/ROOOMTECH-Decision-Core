from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PRODUCT = "ROOOMTECH Decision Core"


class LicenseError(RuntimeError):
    pass


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_runtime_license() -> dict:
    mode = os.getenv("ROOOM_LICENSE_MODE", "personal").strip().lower()
    if mode == "personal":
        return {"mode": "personal", "licensee": None}
    if mode != "commercial":
        raise LicenseError("ROOOM_LICENSE_MODE must be personal or commercial")

    public_key_b64 = os.getenv("ROOOM_LICENSE_PUBLIC_KEY_B64", "").strip()
    token = os.getenv("ROOOM_LICENSE_TOKEN", "").strip()
    if not public_key_b64 or not token:
        raise LicenseError("commercial mode requires a signed commercial license token")

    try:
        payload_b64, signature_b64 = token.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        public_key.verify(signature, payload_bytes)
        payload = json.loads(payload_bytes)
    except Exception as exc:
        raise LicenseError("invalid commercial license token") from exc

    if payload.get("product") != PRODUCT:
        raise LicenseError("license token is for a different product")
    expires_at = payload.get("expires_at")
    if expires_at:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry < datetime.now(timezone.utc):
            raise LicenseError("commercial license has expired")
    return {"mode": "commercial", "licensee": payload.get("licensee"), "features": payload.get("features", [])}
