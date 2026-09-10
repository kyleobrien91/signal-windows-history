#!/usr/bin/env python3
"""
db/snapshot.py - Safe snapshot copying of Signal Desktop database.

Copies db.sqlite, db.sqlite-wal, and db.sqlite-shm to a timestamped
temporary directory with retry logic for file-lock handling.
"""

import os
import shutil
import time


def copy_db_snapshot() -> str:
    """
    Safely copies db.sqlite, db.sqlite-wal, and db.sqlite-shm to a
    timestamped temporary directory.

    Uses APPDATA for Signal database location and a timestamped subdirectory
    under TEMP to prevent Windows file-lock collisions.

    Retry logic: 5 attempts with 0.3s sleep between attempts.

    Returns:
        str: Path to the copied db.sqlite file.
    """
    appdata = os.environ.get("APPDATA", "")
    src = os.path.join(appdata, "Signal", "sql")
    dst_dir = os.path.join(os.environ.get("TEMP", "."), f"signal-player-work-{int(time.time()*1000)}")
    os.makedirs(dst_dir, exist_ok=True)

    db_src = os.path.join(src, "db.sqlite")
    wal_src = os.path.join(src, "db.sqlite-wal")
    shm_src = os.path.join(src, "db.sqlite-shm")
    db_dst = os.path.join(dst_dir, "db.sqlite")
    wal_dst = os.path.join(dst_dir, "db.sqlite-wal")
    shm_dst = os.path.join(dst_dir, "db.sqlite-shm")

    if not os.path.exists(db_src):
        raise FileNotFoundError(f"Signal DB not found: {db_src}")

    max_retries = 5
    for attempt in range(max_retries):
        try:
            if os.path.exists(shm_src):
                shutil.copy2(shm_src, shm_dst)
            if os.path.exists(wal_src):
                shutil.copy2(wal_src, wal_dst)
            shutil.copy2(db_src, db_dst)
            break
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(0.3)

    return db_dst