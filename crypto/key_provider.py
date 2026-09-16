#!/usr/bin/env python3
"""
crypto/key_provider.py - Master key provider abstraction for cache encryption.

Uses Windows DPAPI on Windows platforms, and secure file-backed or explicit test key
providers on non-Windows platforms.
"""

import os
import secrets
import sys


def get_cache_master_key(key_dir: str, test_key: bytes = None) -> bytes:
    """Retrieves or generates a 32-byte master key for AES-GCM cache encryption.

    Args:
        key_dir: Directory where the encrypted/stored master key resides.
        test_key: Optional explicit 32-byte master key for testing.

    Returns:
        32 bytes master key.
    """
    if test_key is not None:
        if not isinstance(test_key, bytes) or len(test_key) != 32:
            raise ValueError("test_key must be exactly 32 bytes")
        return test_key

    os.makedirs(key_dir, exist_ok=True)
    key_file = os.path.join(key_dir, "cache_master.key")

    if sys.platform == "win32":
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
        # Non-Windows environment (e.g. Linux CI / testing)
        if os.path.exists(key_file):
            try:
                with open(key_file, "rb") as f:
                    raw_key = f.read()
                if len(raw_key) == 32:
                    return raw_key
            except Exception:
                pass

        raw_key = secrets.token_bytes(32)
        with open(key_file, "wb") as f:
            f.write(raw_key)
        return raw_key
