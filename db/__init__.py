#!/usr/bin/env python3
"""db - Signal SQLCipher database operations.

Snapshotting, connection management, and high-performance queries
for Signal Desktop database contents.
"""

from .queries import (
    get_conversation_map,
    query_groups,
    query_media,
    query_new_count,
    _db_lock,
    _get_conversation_map,
    _query_groups,
    _query_media,
    _query_new_count,
)
from .snapshot import copy_db_snapshot


def open_db(db_path: str, key: str):
    """Opens a SQLCipher connection, sets keys, and verifies decryptability."""
    import sqlcipher3
    conn = sqlcipher3.connect(db_path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{key}'\";")
    cur.execute("PRAGMA cipher_compatibility = 4;")
    cur.execute("SELECT count(*) FROM sqlite_master;")
    cur.fetchone()
    return conn, cur


def set_active_db(conn, cur):
    """Sets the active database connection and cursor for queries."""
    import db.queries as queries
    with queries._db_lock:
        old_conn = queries._db_conn
        queries._db_conn = conn
        queries._db_cur = cur
    return old_conn


def reload_db(key: str) -> bool:
    """Refreshes the database snapshot and atomically updates the active connection."""
    import db.queries as queries
    try:
        new_path = copy_db_snapshot()
        new_conn, new_cur = open_db(new_path, key)
        with queries._db_lock:
            old_conn = queries._db_conn
            queries._db_conn = new_conn
            queries._db_cur = new_cur
        if old_conn:
            try:
                old_conn.close()
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"[Signal Player] Warning: reload_db failed: {e}")
        return False


__all__ = [
    "copy_db_snapshot",
    "open_db",
    "set_active_db",
    "reload_db",
    "query_groups",
    "query_media",
    "query_new_count",
    "get_conversation_map",
]
