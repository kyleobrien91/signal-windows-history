#!/usr/bin/env python3
"""
crypto/key.py - Windows Signal Desktop SQLCipher key extractor.

Extracts the 64-hex-character SQLCipher key from Signal Desktop on Windows
by:
  1. Reading %APPDATA%\Signal\Local State
  2. DPAPI-decrypting os_crypt.encrypted_key
  3. Reading %APPDATA%\Signal\config.json
  4. AES-256-GCM decrypting the encryptedKey (v10 payload)
  5. Returning the 64-hex character key string
"""

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from crypto.dpapi import dpapi_decrypt


def get_signal_key() -> str:
    """
    Retrieves the 64-character hexadecimal SQLCipher key for Signal Desktop.

    Decrypts the master key via DPAPI from 'Local State', then decrypts
    'encryptedKey' from 'config.json' using AES-GCM.

    Returns:
        str: 64-hex-character SQLCipher key string.
    """
    appdata = os.environ.get("APPDATA", "")
    sig_dir = os.path.join(appdata, "Signal")

    # Step 1: Read Local State and DPAPI-decrypt master key
    with open(os.path.join(sig_dir, "Local State"), "r", encoding="utf-8") as f:
        ls = json.load(f)

    raw = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if not raw.startswith(b"DPAPI"):
        raise ValueError(
            "Unexpected os_crypt.encrypted_key format (no DPAPI prefix)"
        )
    master_key = dpapi_decrypt(raw[5:])

    # Step 2: Read config.json and AES-256-GCM decrypt encryptedKey
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