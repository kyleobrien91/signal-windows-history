#!/usr/bin/env python3
"""db/snapshot.py - Safe snapshotting of the Signal Desktop database.

The project previously copied db.sqlite, db.sqlite-wal, and db.sqlite-shm as
independent files. Because Signal uses SQLite/WAL semantics, that can create a
mixed snapshot whose files are not from one consistent transaction. Copying the
live DB via SQLite's backup API preserves a coherent point-in-time view while
still allowing callers to keep the same `copy_db_snapshot()` API.
"""

import os
import tempfile
import time
from pathlib import Path

from crypto import get_signal_key


def _snapshot_dir() -> str:
    """Create a fresh work directory for a snapshot copy."""
    temp_root = os.environ.get("TEMP", ".")
    return tempfile.mkdtemp(prefix="signal-player-work-", dir=temp_root)


def _is_busy_or_locked_error(exc: Exception, sqlcipher3_module) -> bool:
    """Check if exception represents a SQLite/SQLCipher BUSY or LOCKED transient error."""
    op_err_cls = getattr(sqlcipher3_module, "OperationalError", None)
    if op_err_cls is not None and isinstance(exc, op_err_cls):
        is_op = True
    elif type(exc).__name__ in ("OperationalError", "DatabaseError"):
        is_op = True
    else:
        is_op = False

    if not is_op:
        return False

    SQLITE_BUSY = 5
    SQLITE_LOCKED = 6

    err_code = getattr(exc, "sqlite_errorcode", None)
    if err_code is not None and err_code in (SQLITE_BUSY, SQLITE_LOCKED):
        return True

    ext_code = getattr(exc, "sqlite_extended_errorcode", None)
    if ext_code is not None and (ext_code & 0xFF) in (SQLITE_BUSY, SQLITE_LOCKED):
        return True

    msg = str(exc).lower()
    busy_locked_phrases = (
        "database is locked",
        "database is busy",
        "database table is locked",
        "lock protocol error",
        "a table in the database is locked",
    )
    return any(phrase in msg for phrase in busy_locked_phrases)


def _validate_snapshot(db_dst: str, key: str, sqlcipher3_module) -> None:
    """Validate destination database after backup completes."""
    conn = sqlcipher3_module.connect(db_dst)
    try:
        conn.execute(f"PRAGMA key = \"x'{key}'\";")
        conn.execute("PRAGMA cipher_compatibility = 4;")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM sqlite_master;")
        cur.fetchone()
    finally:
        conn.close()


def copy_db_snapshot() -> str:
    """Create a consistent SQLite snapshot of Signal's live SQLCipher DB.

    We open the live database in read-only mode and back it up into a new file.
    SQLite's backup API captures a single consistent database state, even when the
    main database and WAL are changing concurrently.
    """
    appdata = os.environ.get("APPDATA", "")
    src = os.path.join(appdata, "Signal", "sql")
    db_src = os.path.join(src, "db.sqlite")
    if not os.path.exists(db_src):
        raise FileNotFoundError(f"Signal DB not found: {db_src}")

    key = get_signal_key()
    dst_dir = _snapshot_dir()
    db_dst = os.path.join(dst_dir, "db.sqlite")

    import sqlcipher3

    max_retries = 5
    for attempt in range(max_retries):
        try:
            source_uri = f"{Path(db_src).resolve().as_uri()}?mode=ro"
            src_conn = sqlcipher3.connect(source_uri, uri=True)
            try:
                src_conn.execute(f"PRAGMA key = \"x'{key}'\";")
                src_conn.execute("PRAGMA cipher_compatibility = 4;")
                src_conn.execute("PRAGMA query_only = ON;")

                dst_conn = sqlcipher3.connect(db_dst)
                try:
                    dst_conn.execute(f"PRAGMA key = \"x'{key}'\";")
                    dst_conn.execute("PRAGMA cipher_compatibility = 4;")
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

            break
        except Exception as exc:
            if os.path.exists(db_dst):
                try:
                    os.remove(db_dst)
                except OSError:
                    pass

            if attempt < max_retries - 1 and _is_busy_or_locked_error(exc, sqlcipher3):
                time.sleep(0.3)
                continue

            raise

    # Post-backup validation outside retry loop using the same key
    try:
        _validate_snapshot(db_dst, key, sqlcipher3)
    except Exception:
        if os.path.exists(db_dst):
            try:
                os.remove(db_dst)
            except OSError:
                pass
        raise

    return db_dst