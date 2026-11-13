"""PII protection at rest.

* **Matching** (events -> leads by phone): a keyed HMAC-SHA256 of the canonical phone. It is
  deterministic, so equality joins work, but unlike a plain hash it cannot be brute-forced
  from the ~4 billion Indian mobile numbers without the key.
* **Storage** (names, the phone itself, any other ``pii: true`` field): Fernet (AES-128-CBC +
  HMAC) with a separate key, so the application can still show or dial the value, while a
  database dump alone reveals nothing.
* Two keys, two purposes: rotating one never forces re-deriving the other.
"""
from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import Fernet


class PiiProtector:
    def __init__(self, hmac_key: bytes, fernet_key: bytes) -> None:
        if len(hmac_key) < 16:
            raise ValueError("LEDGER_HMAC_KEY must be at least 16 bytes")
        self._hmac_key = hmac_key
        self._fernet = Fernet(fernet_key)

    def phone_hash(self, canonical_phone: str) -> bytes:
        """32-byte deterministic pseudonym of a canonical ``+91XXXXXXXXXX`` number."""
        return hmac.new(self._hmac_key, b"phone:" + canonical_phone.encode(), hashlib.sha256).digest()

    @staticmethod
    def phone_last4(canonical_phone: str) -> str:
        return canonical_phone[-4:]

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
