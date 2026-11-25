"""Keys shaped like the ones a browser's PushSubscription.toJSON() sends:
p256dh a base64url uncompressed P-256 public point, auth 16 random bytes,
both unpadded. The subscribe door refuses anything else."""

import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def browser_keys() -> dict:
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return {"p256dh": _b64u(point), "auth": _b64u(os.urandom(16))}
