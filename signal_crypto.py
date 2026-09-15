#!/usr/bin/env python3
"""signal_crypto.py - Legacy compatibility shim for Signal crypto operations.

Delegates to canonical crypto package module.
This shim contains no business logic.
"""

import os
import sys

from crypto import (
    decrypt_attachment,
    dpapi_decrypt,
    get_signal_key,
    inspect_attachment,
    stream_attachment_range,
)

# Retained private legacy compatibility accessors for existing callers/tests
_DATA_BLOB = sys.modules["crypto.dpapi"].DATA_BLOB
_CACHE_MAX = sys.modules["crypto.key"]._CACHE_MAX
_cache = sys.modules["crypto.key"]._cache
_cache_lock = sys.modules["crypto.key"]._cache_lock
_get_cached = sys.modules["crypto.key"]._get_cached


def _decrypt_blob(enc_path: str, local_key_b64: str, declared_size: int = None) -> bytes:
    """Legacy compatibility helper function for blob decryption by path.

    Retained strictly for backwards compatibility with legacy callers.
    """
    if not os.path.exists(enc_path):
        file_id = os.path.basename(enc_path) or "unknown_attachment"
        raise FileNotFoundError(f"Attachment file missing: {file_id}")
    with open(enc_path, "rb") as fh:
        enc_data = fh.read()
    return decrypt_attachment(enc_data, local_key_b64, declared_size)


__all__ = [
    "get_signal_key",
    "dpapi_decrypt",
    "decrypt_attachment",
    "inspect_attachment",
    "stream_attachment_range",
]
