"""Envelope encryption for aggregator secrets: tokens are never stored
plaintext once real data connects.

Two layers of Fernet (AES128-CBC + HMAC, versioned, ttl-capable):
  * MASTER key — env `OIKONOME_MASTER_KEY` (urlsafe-base64 32 bytes; generate
    with `python -m oikonome.db.crypto`). Compose passes it from .env; hosted
    moves it to a secret manager. Rotation: wrap new tenant keys with the
    new master, then re-wrap the existing rows.
  * per-TENANT data key — random, stored in `tenant_keys.wrapped_key`
    encrypted by the master. All of a tenant's secrets are under its own
    key, so a single-tenant compromise or export never exposes another's.

Ciphertexts are tagged `enc:v1:<token>`; `decrypt` passes through untagged
values so pre-encryption rows keep working during migration (and dev setups
without a master key keep functioning, loudly warned).
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

PREFIX = "enc:v1:"


def master_key() -> bytes | None:
    k = os.environ.get("OIKONOME_MASTER_KEY")
    return k.encode() if k else None


# ---- control plane -------------------------------------------------
# `users.totp_secret` lives on the control plane, where there is no ambient
# tenant (and no tenant_keys row to unwrap), so the tenant envelope above
# cannot apply. Control-plane secrets encrypt directly under the master key
# with their own tag. Same dev-mode story: no master key → plaintext
# passthrough; decrypt passes untagged values through so pre-encryption rows
# keep verifying (the migrate sweep re-writes them when a key is present).

CP_PREFIX = "enc:cp1:"


def encrypt_cp(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    mk = master_key()
    if not mk:
        return plaintext
    return CP_PREFIX + Fernet(mk).encrypt(plaintext.encode()).decode()


def decrypt_cp(stored: str | None) -> str | None:
    if stored is None or not stored.startswith(CP_PREFIX):
        return stored
    mk = master_key()
    if not mk:
        raise RuntimeError(
            "encrypted secret present but OIKONOME_MASTER_KEY is not set")
    return Fernet(mk).decrypt(stored[len(CP_PREFIX):].encode()).decode()


def _tenant_fernet(conn) -> Fernet | None:
    """The current tenant's data key (created on first use). None when no
    master key is configured (dev mode — secrets stored plaintext, warned)."""
    mk = master_key()
    if not mk:
        return None
    wrapper = Fernet(mk)
    row = conn.execute("SELECT wrapped_key FROM tenant_keys").fetchone()
    if row is None:
        data_key = Fernet.generate_key()
        conn.execute("INSERT INTO tenant_keys (wrapped_key) VALUES (%s) "
                     "ON CONFLICT (tenant_id) DO NOTHING",
                     (wrapper.encrypt(data_key).decode(),))
        row = conn.execute("SELECT wrapped_key FROM tenant_keys").fetchone()
    return Fernet(wrapper.decrypt(row["wrapped_key"].encode()))


def encrypt(conn, plaintext: str | None) -> str | None:
    """Encrypt a secret under the current tenant's key. Plaintext
    passthrough (with no tag) when no master key is configured."""
    if plaintext is None:
        return None
    f = _tenant_fernet(conn)
    if f is None:
        return plaintext
    return PREFIX + f.encrypt(plaintext.encode()).decode()


def encryptor(conn):
    """One key unwrap, many encrypts. `encrypt` fetches and unwraps the
    tenant key per call, which is fine for a single secret but turns a
    loop (e.g. a multi-backend settings save holding the settings row
    lock) into one DB round trip per item. Returns a callable with
    `encrypt`'s exact contract, with the key resolved once up front."""
    f = _tenant_fernet(conn)

    def _enc(plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        if f is None:
            return plaintext
        return PREFIX + f.encrypt(plaintext.encode()).decode()
    return _enc


def decrypt(conn, stored: str | None) -> str | None:
    """Decrypt a stored secret; untagged values pass through (legacy /
    dev-mode rows)."""
    if stored is None or not stored.startswith(PREFIX):
        return stored
    f = _tenant_fernet(conn)
    if f is None:
        raise RuntimeError(
            "encrypted secret present but OIKONOME_MASTER_KEY is not set")
    return f.decrypt(stored[len(PREFIX):].encode()).decode()


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
