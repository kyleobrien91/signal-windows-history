#!/usr/bin/env python3
"""player - Signal Desktop local video player server.

Runs a local HTTP server (127.0.0.1 only) that reads Signal's SQLCipher
database, decrypts attachment blobs entirely in RAM, and streams them
to a browser-based video library with YouTube-style hover preview.
"""

from .server import (is_signal_running, kill_signal, main)

__all__ = [
    "is_signal_running",
    "kill_signal",
    "main",
]