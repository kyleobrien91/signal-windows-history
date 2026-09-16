#!/usr/bin/env python3
"""crypto - Windows Signal cryptographic operations.

Consolidated DPAPI, key extraction, and attachment decryption modules.
"""

from .attachment import decrypt_attachment, inspect_attachment, stream_attachment_range
from .cache import DerivedMediaCache
from .dpapi import dpapi_decrypt
from .key import _CACHE_MAX, _cache, _cache_lock, _get_cached, get_signal_key
from .key_provider import get_cache_master_key

__all__ = [
    "dpapi_decrypt",
    "get_signal_key",
    "decrypt_attachment",
    "inspect_attachment",
    "stream_attachment_range",
    "DerivedMediaCache",
    "get_cache_master_key",
]
