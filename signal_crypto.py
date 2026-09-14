#!/usr/bin/env python3
"""
signal_crypto.py - Signal DPAPI Key Extraction & In-Memory Decryption
=====================================================================
Extracts Signal Desktop's SQLCipher database key via Windows DPAPI,
and handles streaming attachment decryption (AES-CBC + HMAC-SHA256)
in RAM using an LRU cache.

Security guarantees:
  - Plaintext media is NEVER written to disk.
  - Decryption happens exclusively in-memory on demand.
  - In-memory cache is bounded and cleared on process termination.
"""

import base64
import collections
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
import sys
import threading

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    print("Error: 'cryptography' library required. pip install cryptography", file=sys.stderr)
    sys.exit(1)

from crypto.attachment import inspect_attachment, stream_attachment_range, decrypt_attachment


# ---------------------------------------------------------------------------
# Windows DPAPI + Signal Key Extraction
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_decrypt(enc: bytes) -> bytes:
    """Decrypts DPAPI-encrypted bytes using Windows crypt32.CryptUnprotectData."""
    blob_in = _DATA_BLOB(
        len(enc),
        ctypes.cast(ctypes.create_string_buffer(enc), ctypes.POINTER(ctypes.c_byte)),
    )
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError(ctypes.GetLastError())
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def get_signal_key() -> str:
    """
    Retrieves the 64-character hexadecimal SQLCipher key for Signal Desktop.
    Decrypts the master key via DPAPI from 'Local State', then decrypts
    'encryptedKey' from 'config.json' using AES-GCM.
    """
    appdata = os.environ.get("APPDATA", "")
    sig_dir = os.path.join(appdata, "Signal")

    with open(os.path.join(sig_dir, "Local State"), "r", encoding="utf-8") as f:
        ls = json.load(f)
    raw = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if not raw.startswith(b"DPAPI"):
        raise ValueError("Unexpected os_crypt.encrypted_key format (no DPAPI prefix)")
    master_key = _dpapi_decrypt(raw[5:])

    with open(os.path.join(sig_dir, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if "encryptedKey" in cfg:
        enc = bytes.fromhex(cfg["encryptedKey"])
        if not enc.startswith(b"v10"):
            raise ValueError("Unexpected encryptedKey format (no v10 prefix)")
        nonce, ct = enc[3:15], enc[15:]
        return AESGCM(master_key).decrypt(nonce, ct, None).decode("utf-8")
    if "key" in cfg:
        return cfg["key"]
    raise KeyError("No key found in Signal config.json")


# ---------------------------------------------------------------------------
# In-Memory Attachment Decryption — Plaintext NEVER written to disk
# ---------------------------------------------------------------------------

def _decrypt_blob(enc_path: str, local_key_b64: str, declared_size: int = None) -> bytes:
    """
    Decrypts an attachment encrypted with Signal's custom format:
    AES-256-CBC with HMAC-SHA256 verification.
    """
    raw_key = base64.b64decode(local_key_b64)
    aes_key = raw_key[:32]
    mac_key = raw_key[32:]

    with open(enc_path, "rb") as fh:
        enc_data = fh.read()

    iv   = enc_data[:16]
    body = enc_data[16:-32]
    tag  = enc_data[-32:]

    expected = hmac.new(mac_key, enc_data[:-32], hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("HMAC verification failed — file may be corrupt or tampered")

    cipher    = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded    = decryptor.update(body) + decryptor.finalize()
    pad_len   = padded[-1]
    plaintext = padded[:-pad_len]

    if declared_size and declared_size <= len(plaintext):
        plaintext = plaintext[:declared_size]
    return plaintext


# ---------------------------------------------------------------------------
# In-Memory LRU Cache — RAM only, cleared on shutdown
# ---------------------------------------------------------------------------

_CACHE_MAX = 10
_cache: collections.OrderedDict = collections.OrderedDict()
_cache_lock = threading.Lock()


def _get_cached(msg_id: str, enc_path: str, local_key: str, size: int) -> bytes:
    """Returns decrypted blob from RAM cache or decrypts on demand."""
    with _cache_lock:
        if msg_id in _cache:
            _cache.move_to_end(msg_id)
            return _cache[msg_id]

    data = _decrypt_blob(enc_path, local_key, size)

    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.popitem(last=False)
        _cache[msg_id] = data
    return data
