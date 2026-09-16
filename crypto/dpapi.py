#!/usr/bin/env python3
"""
crypto/dpapi.py - Windows DPAPI decryption utilities.

Provides the DATA_BLOB ctypes Structure and dpapi_decrypt() function
used across the Signal Windows history tools to decrypt DPAPI-protected
blobs (e.g., Signal's os_crypt.encrypted_key).
"""

import ctypes
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def dpapi_decrypt(encrypted_bytes: bytes) -> bytes:
    """Decrypts a DPAPI-protected blob using the current Windows user session.

    Args:
        encrypted_bytes: The DPAPI-encrypted data (without the "DPAPI" prefix).

    Returns:
        The decrypted plaintext bytes.

    Raises:
        ctypes.WinError: If decryption fails, with the Windows error code.
    """
    p_data_in = DATA_BLOB(
        len(encrypted_bytes),
        ctypes.cast(
            ctypes.create_string_buffer(encrypted_bytes),
            ctypes.POINTER(ctypes.c_byte),
        ),
    )
    p_data_out = DATA_BLOB()

    ret = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(p_data_in),
        None,  # description
        None,  # optional entropy
        None,  # reserved
        None,  # prompt struct
        0,     # flags
        ctypes.byref(p_data_out),
    )
    if not ret:
        error_code = ctypes.GetLastError()
        raise ctypes.WinError(error_code)

    decrypted = ctypes.string_at(p_data_out.pbData, p_data_out.cbData)
    ctypes.windll.kernel32.LocalFree(p_data_out.pbData)
    return decrypted