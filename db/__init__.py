#!/usr/bin/env python3
"""db - Signal SQLCipher database operations.

Snapshotting, connection management, and high-performance queries
for Signal Desktop database contents.
"""

import os
import shutil
import time
import threading
from typing import Dict, List, Tuple

from .snapshot import copy_db_snapshot
from .queries import (_db_conn, _db_cur, _db_lock, _query_groups, _query_media,
                    _query_new_count, _get_conversation_map)
import db.queries as queries


def open_db(db_path: str, key: str):
    """Opens a SQLCipher connection, sets keys, and verifies decryptability."""
    import sqlcipher3
    conn = sqlcipher3.connect(db_path, check_same_thread=False)
    cur  = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{key}'\";")
    cur.execute("PRAGMA cipher_compatibility = 4;")
    # Sanity check to ensure decryption succeeded
    cur.execute("SELECT count(*) FROM sqlite_master;")
    cur.fetchone()
    with queries._db_lock:
        queries._db_conn = conn
        queries._db_cur  = cur
    return conn, cur


def reload_db(key: str) -> bool:
    """Refreshes the database snapshot and atomically updates the active connection."""
    try:
        new_path = copy_db_snapshot()
        new_conn, new_cur = open_db(new_path, key)
        with queries._db_lock:
            old_conn = queries._db_conn
            queries._db_conn = new_conn
            queries._db_cur  = new_cur
        if old_conn:
            try:
                old_conn.close()
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"[Signal Player] Warning: reload_db failed (keeping existing connection): {e}")
        return False


__all__ = [
    "copy_db_snapshot",
    "open_db",
    "reload_db",
    "_query_groups",
    "_query_media",
    "_query_new_count",
    "_get_conversation_map",
    "_db_conn",
    "_db_cur",
    "_db_lock",
]
