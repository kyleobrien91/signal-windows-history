#!/usr/bin/env python3
"""crypto - Windows Signal cryptographic operations.

Consolidated DPAPI, key extraction, and attachment decryption modules.
Single source of truth - no duplicate definitions across the codebase.
"""

from .dpapi import dpapi_decrypt
from .key import get_signal_key, _get_cached, _cache, _cache_lock, _CACHE_MAX
from .attachment import decrypt_attachment

__all__ = [
    "dpapi_decrypt",
    "get_signal_key",
    "decrypt_attachment",
    "_get_cached",
    "_cache",
    "_cache_lock",
    "_CACHE_MAX",
]