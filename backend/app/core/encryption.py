"""AES-256-GCM symmetric encryption for at-rest API key storage.

The platform NEVER stores plaintext exchange API credentials.
Each ciphertext is bound to a fresh 96-bit nonce; tampering produces
an InvalidTag and the credential is treated as destroyed.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


def _master_key() -> bytes:
    raw = base64.urlsafe_b64decode(settings.ENCRYPTION_KEY.encode())
    if len(raw) != 32:
        raise RuntimeError("ENCRYPTION_KEY must decode to 32 bytes")
    return raw


@dataclass(frozen=True)
class EncryptedBlob:
    """Ciphertext + nonce, both base64url-encoded."""

    ciphertext: str
    nonce: str


def encrypt_secret(plaintext: str, *, associated_data: bytes | None = None) -> EncryptedBlob:
    """Encrypt a single secret string with AES-256-GCM.

    `associated_data` is authenticated but not encrypted. Pass e.g. the user_id
    to bind a ciphertext to its owner — tampering invalidates decryption.
    """
    aesgcm = AESGCM(_master_key())
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data)
    return EncryptedBlob(
        ciphertext=base64.urlsafe_b64encode(ct).decode(),
        nonce=base64.urlsafe_b64encode(nonce).decode(),
    )


def decrypt_secret(blob: EncryptedBlob, *, associated_data: bytes | None = None) -> str:
    aesgcm = AESGCM(_master_key())
    ct = base64.urlsafe_b64decode(blob.ciphertext.encode())
    nonce = base64.urlsafe_b64decode(blob.nonce.encode())
    return aesgcm.decrypt(nonce, ct, associated_data).decode("utf-8")


def pack(blob: EncryptedBlob) -> str:
    """Combined `nonce.ciphertext` string for single-column storage."""
    return f"{blob.nonce}.{blob.ciphertext}"


def unpack(packed: str) -> EncryptedBlob:
    nonce, _, ct = packed.partition(".")
    if not ct:
        raise ValueError("malformed packed blob")
    return EncryptedBlob(ciphertext=ct, nonce=nonce)


def encrypt_packed(plaintext: str, *, associated_data: bytes | None = None) -> str:
    return pack(encrypt_secret(plaintext, associated_data=associated_data))


def decrypt_packed(packed: str, *, associated_data: bytes | None = None) -> str:
    return decrypt_secret(unpack(packed), associated_data=associated_data)
