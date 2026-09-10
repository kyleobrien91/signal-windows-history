#!/usr/bin/env python3
"""Attachment decryption for Signal media files.

Decrypts attachments using AES-256-CBC with HMAC-SHA256 verification,
matching the signal_grep.decrypt_attachment specification.
"""

import base64
import hashlib
import hmac
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def decrypt_attachment(enc_data: bytes, local_key_b64: str, size: Optional[int] = None) -> bytes:
    """Decrypts a Signal attachment using its localKey (AES-256-CBC + HMAC-SHA256).

    Args:
        enc_data: The encrypted attachment data (bytes).
        local_key_b64: Base64-encoded local key (64 bytes after decoding).
        size: Optional declared size to truncate the plaintext to.

    Returns:
        The decrypted plaintext bytes.

    Raises:
        ValueError: If HMAC verification fails.
    """
    raw_key = base64.b64decode(local_key_b64)
    aes_key = raw_key[:32]
    mac_key = raw_key[32:]

    iv = enc_data[:16]
    ct = enc_data[16:-32]
    tag = enc_data[-32:]

    # Authenticate
    computed_tag = hmac.new(mac_key, enc_data[:-32], hashlib.sha256).digest()
    if computed_tag != tag:
        raise ValueError("HMAC verification failed for attachment.")

    # Decrypt AES-256-CBC
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    pad = padded[-1]
    plaintext = padded[:-pad]

    if size is not None and size <= len(plaintext):
        plaintext = plaintext[:size]
    return plaintext