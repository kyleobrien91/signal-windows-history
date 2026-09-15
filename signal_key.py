#!/usr/bin/env python3
"""signal_key.py - Legacy compatibility shim for Signal key extraction.

Delegates to canonical crypto package module.
This shim contains no business logic.
"""

from crypto import get_signal_key


def main():
    key = get_signal_key()
    print(f"Decrypted SQLCipher Key: {key}")
    print(f"Formatted for PRAGMA:    x'{key}'")


if __name__ == "__main__":
    main()
