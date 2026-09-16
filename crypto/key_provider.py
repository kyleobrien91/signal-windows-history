#!/usr/bin/env python3
"""
crypto/key_provider.py - Master key provider abstraction for cache encryption.

Uses Windows DPAPI on Windows platforms, and explicit test keys or ephemeral in-memory
master keys on non-Windows platforms (failing closed for untrusted file storage).
"""

import os
import secrets
import sys

_EPHEMERAL_KEY: bytes = None


def get_cache_master_key(key_dir: str, test_key: bytes = None) -> bytes:
    """Retrieves or generates a 32-byte master key for AES-GCM cache encryption.

    Args:
        key_dir: Directory where the DPAPI-encrypted master key resides on Windows.
        test_key: Explicit 32-byte master key for testing / CI.

    Returns:
        32 bytes master key.
    """
    global _EPHEMERAL_KEY

    if test_key is not None:
        if not isinstance(test_key, bytes) or len(test_key) != 32:
            raise ValueError("test_key must be exactly 32 bytes")
        return test_key

    if sys.platform == "win32":
        os.makedirs(key_dir, exist_ok=True)
        key_file = os.path.join(key_dir, "cache_master.key")
        try:
            from crypto.dpapi import dpapi_decrypt, dpapi_encrypt
            if os.path.exists(key_file):
                try:
                    with open(key_file, "rb") as f:
                        enc_key = f.read()
                    raw = dpapi_decrypt(enc_key)
                    if len(raw) == 32:
                        return raw
                except Exception:
                    pass

            raw_key = secrets.token_bytes(32)
            enc_key = dpapi_encrypt(raw_key)
            with open(key_file, "wb") as f:
                f.write(enc_key)
            return raw_key
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Windows DPAPI cache master key: {e}")
    else:
        # Non-Windows environment (e.g. Linux CI / testing): use ephemeral in-memory key
        if _EPHEMERAL_KEY is None:
            _EPHEMERAL_KEY = secrets.token_bytes(32)
        return _EPHEMERAL_KEY
