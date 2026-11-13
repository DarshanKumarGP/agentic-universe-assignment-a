"""PII protection at rest.

* Phone numbers are matched through a keyed hash (HMAC-SHA256) so events can still be linked to
  leads without storing the number. A keyed hash (not a plain hash) matters: the Indian mobile
  space is ~4 billion numbers, so an unkeyed SHA-256 could be reversed by brute force.
* All other PII fields are stored encrypted (Fernet: AES-128-CBC + HMAC-SHA256).
* Keys are derived from one secret that lives OUTSIDE the database (env var or a 0600 key file).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .config import SECRET_ENV


class Vault:
    def __init__(self, secret: bytes):
        self._hash_key = hmac.new(secret, b"ledger/phone-hash/v1", hashlib.sha256).digest()
        enc_key = hmac.new(secret, b"ledger/pii-encryption/v1", hashlib.sha256).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_key))

    def phone_hash(self, canonical_phone: str) -> str:
        return hmac.new(self._hash_key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()

    def encrypt(self, obj: Any) -> bytes:
        return self._fernet.encrypt(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    def decrypt(self, blob: bytes) -> Any:
        return json.loads(self._fernet.decrypt(blob))

    def fingerprint(self) -> str:
        """Non-reversible key check, stored in the DB to detect a wrong/missing key early."""
        return hmac.new(self._hash_key, b"fingerprint", hashlib.sha256).hexdigest()[:16]


def load_vault(db_path: str) -> Vault:
    env = os.environ.get(SECRET_ENV)
    if env:
        return Vault(env.encode("utf-8"))
    key_path = Path(str(db_path) + ".key")
    if not key_path.exists():
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
        except FileExistsError:  # another process created it first
            pass
    return Vault(key_path.read_text().strip().encode("utf-8"))
