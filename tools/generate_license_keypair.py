"""Generate an Ed25519 keypair for ROOOMTECH commercial license signing.

Run this on a trusted offline/admin machine. Never commit the private key.
"""

import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print("ROOOM_LICENSE_PRIVATE_KEY_B64=" + base64.b64encode(private_raw).decode())
    print("ROOOM_LICENSE_PUBLIC_KEY_B64=" + base64.b64encode(public_raw).decode())


if __name__ == "__main__":
    main()
