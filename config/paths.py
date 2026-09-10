#!/usr/bin/env python3
"""config.paths - Signal Desktop path constants for Windows."""

import os

APPDATA = os.environ.get("APPDATA", "")
"""APPDATA environment variable root."""

SIGNAL_DIR = os.path.join(APPDATA, "Signal") if APPDATA else ""
"""Path to %APPDATA%\\Signal"""

LOCAL_STATE_PATH = os.path.join(SIGNAL_DIR, "Local State")
"""Path to %APPDATA%\\Signal\\Local State"""

CONFIG_JSON_PATH = os.path.join(SIGNAL_DIR, "config.json")
"""Path to %APPDATA%\\Signal\\config.json"""

SQL_DIR = os.path.join(SIGNAL_DIR, "sql")
"""Path to %APPDATA%\\Signal\\sql"""

ATTACHMENTS_DIR = os.path.join(SIGNAL_DIR, "attachments.noindex")
"""Path to %APPDATA%\\Signal\\attachments.noindex"""

PLAYER_EXE_PATH = os.path.expandvars(
    r"%LOCALAPPDATA%\Programs\signal-desktop\Signal.exe"
)
"""Path to Signal Desktop executable via %LOCALAPPDATA%"""

HOME_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()
"""Base directory for the signal_windows_history package"""