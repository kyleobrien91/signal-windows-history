#!/usr/bin/env python3
"""Attachment decryption for Signal media files.

Decrypts attachments using AES-256-CBC with HMAC-SHA256 verification,
matching the signal_grep.decrypt_attachment specification.
"""

import base64
import hashlib
import hmac
import os
from typing import Optional, Generator

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def inspect_attachment(enc_path: str, local_key_b64: str, declared_size: Optional[int] = None) -> int:
    """Performs Pass 1 HMAC-SHA256 verification and structure validation in O(1) memory.

    Args:
        enc_path: Path to the encrypted attachment file on disk.
        local_key_b64: Base64-encoded local key (64 bytes after decoding).
        declared_size: Optional declared size to validate against decrypted plaintext.

    Returns:
        int: Validated effective plaintext size.

    Raises:
        ValueError: If file structure is invalid, truncated, not block-aligned,
                    HMAC verification fails, padding is invalid, or declared_size is invalid.
    """
    raw_key = base64.b64decode(local_key_b64)
    aes_key = raw_key[:32]
    mac_key = raw_key[32:]

    file_size = os.path.getsize(enc_path)
    # File structure: 16-byte IV + N*16-byte ciphertext + 32-byte HMAC tag
    if file_size < 64 or (file_size - 48) % 16 != 0:
        raise ValueError("Invalid attachment file structure or alignment")

    ct_len = file_size - 48
    bytes_to_mac = file_size - 32

    # Step 1: O(1) memory streaming HMAC verification
    h = hmac.new(mac_key, digestmod=hashlib.sha256)
    with open(enc_path, "rb") as f:
        bytes_read = 0
        chunk_size = 65536
        while bytes_read < bytes_to_mac:
            to_read = min(chunk_size, bytes_to_mac - bytes_read)
            chunk = f.read(to_read)
            if len(chunk) < to_read:
                raise ValueError("Attachment file truncated during HMAC read")
            h.update(chunk)
            bytes_read += len(chunk)

        tag = f.read(32)
        if len(tag) < 32:
            raise ValueError("Attachment file truncated during tag read")

    if not hmac.compare_digest(h.digest(), tag):
        raise ValueError("HMAC verification failed for attachment.")

    # Step 2: Read and decrypt final block to check PKCS#7 padding
    with open(enc_path, "rb") as f:
        if ct_len == 16:
            f.seek(0)
            prev_block = f.read(16)
            last_ct = f.read(16)
        else:
            f.seek(16 + ct_len - 32)
            prev_block = f.read(16)
            last_ct = f.read(16)

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(prev_block))
    decryptor = cipher.decryptor()
    last_pt = decryptor.update(last_ct) + decryptor.finalize()

    if not last_pt:
        raise ValueError("Decryption produced empty block for final block")

    pad_len = last_pt[-1]
    if pad_len < 1 or pad_len > 16 or last_pt[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("Invalid PKCS#7 padding in attachment.")

    total_plaintext_len = ct_len - pad_len

    effective_size = total_plaintext_len
    if declared_size is not None:
        if not isinstance(declared_size, int) or declared_size < 0:
            raise ValueError("Invalid declared_size")
        if declared_size > total_plaintext_len:
            raise ValueError("declared_size exceeds decrypted plaintext length")
        effective_size = declared_size

    return effective_size


def stream_attachment_range(
    enc_path: str,
    local_key_b64: str,
    start: int,
    end: int,
    declared_size: Optional[int] = None,
) -> Generator[bytes, None, None]:
    """Performs Pass 2 range streaming decryption in O(1) memory.

    Yields chunks of decrypted plaintext bytes for inclusive offset interval [start, end].
    Assumes inspect_attachment has already been called to validate MAC and padding.

    Args:
        enc_path: Path to the encrypted attachment file on disk.
        local_key_b64: Base64-encoded local key.
        start: Inclusive start byte index in plaintext.
        end: Inclusive end byte index in plaintext.
        declared_size: Optional declared size.
    """
    if start > end:
        return

    length = end - start + 1
    if length <= 0:
        return

    raw_key = base64.b64decode(local_key_b64)
    aes_key = raw_key[:32]

    start_block = start // 16
    end_block = end // 16
    num_blocks = end_block - start_block + 1

    with open(enc_path, "rb") as f:
        f.seek(start_block * 16)
        iv = f.read(16)
        if len(iv) < 16:
            raise ValueError("Truncated attachment file when reading IV")

        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
        decryptor = cipher.decryptor()

        blocks_remaining = num_blocks
        bytes_emitted = 0
        discard_offset = start % 16

        chunk_blocks = 4096

        while blocks_remaining > 0:
            to_read_blocks = min(chunk_blocks, blocks_remaining)
            ct_chunk = f.read(to_read_blocks * 16)
            if len(ct_chunk) < to_read_blocks * 16:
                raise ValueError("Truncated attachment file during decryption stream")

            pt_chunk = decryptor.update(ct_chunk)
            blocks_remaining -= to_read_blocks

            if discard_offset > 0:
                if discard_offset >= len(pt_chunk):
                    discard_offset -= len(pt_chunk)
                    continue
                else:
                    pt_chunk = pt_chunk[discard_offset:]
                    discard_offset = 0

            needed = length - bytes_emitted
            if len(pt_chunk) > needed:
                pt_chunk = pt_chunk[:needed]

            bytes_emitted += len(pt_chunk)
            yield pt_chunk

            if bytes_emitted >= length:
                break


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