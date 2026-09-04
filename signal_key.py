#!/usr/bin/env python3
"""
signal_key.py - Windows version of Signal Desktop SQLCipher key extractor.
Analogous to the macOS signal_key.py script in the original article.
"""

import base64
import ctypes
from ctypes import wintypes
import json
import os
import sys

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    print("Error: 'cryptography' library is required. Install via: pip install cryptography", file=sys.stderr)
    sys.exit(1)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte))
    ]


def dpapi_decrypt(encrypted_bytes: bytes) -> bytes:
    p_data_in = DATA_BLOB(
        len(encrypted_bytes),
        ctypes.cast(ctypes.create_string_buffer(encrypted_bytes), ctypes.POINTER(ctypes.c_byte))
    )
    p_data_out = DATA_BLOB()
    ret = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(p_data_in), None, None, None, None, 0, ctypes.byref(p_data_out)
    )
    if not ret:
        raise ctypes.WinError()
    decrypted = ctypes.string_at(p_data_out.pbData, p_data_out.cbData)
    ctypes.windll.kernel32.LocalFree(p_data_out.pbData)
    return decrypted


def main():
    appdata = os.environ.get("APPDATA")
    signal_dir = os.path.join(appdata, "Signal")
    local_state_path = os.path.join(signal_dir, "Local State")
    config_path = os.path.join(signal_dir, "config.json")

    # Step 1: Unwrap master key via Windows DPAPI
    with open(local_state_path, "r", encoding="utf-8") as f:
        local_state = json.load(f)

    raw_encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
    assert raw_encrypted_key[:5] == b"DPAPI", "Header must be DPAPI"
    master_key = dpapi_decrypt(raw_encrypted_key[5:])

    # Step 2: Unwrap SQLCipher database key via AES-256-GCM
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if "encryptedKey" in config:
        enc_bytes = bytes.fromhex(config["encryptedKey"])
        assert enc_bytes[:3] == b"v10", "Payload must start with v10"
        nonce = enc_bytes[3:15]
        ciphertext_and_tag = enc_bytes[15:]
        aesgcm = AESGCM(master_key)
        db_key = aesgcm.decrypt(nonce, ciphertext_and_tag, None).decode("utf-8")
    else:
        db_key = config["key"]

    print(f"Decrypted SQLCipher Key: {db_key}")
    print(f"Formatted for PRAGMA:    x'{db_key}'")


if __name__ == "__main__":
    main()
