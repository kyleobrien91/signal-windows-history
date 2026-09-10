#!/usr/bin/env python3
"""cli - Command-line interface for Signal Windows History.

Provides grep, search, list, and export functionality for local
Signal Desktop history on Windows.
"""

from .grep import main

__all__ = [
    "main",
]