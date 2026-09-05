#!/usr/bin/env python3
"""
signal_db.py - Signal SQLCipher Database Snapshotting & Queries
===============================================================
Manages safe snapshotting of the active Signal Desktop database (including WAL/SHM),
connection initialization via SQLCipher, atomic reconnection, and high-performance
queries for conversations and attachments.
"""

import os
import shutil
import sys
import threading
import time
from typing import Dict, List, Tuple

try:
    import sqlcipher3
except ImportError:
    print("Error: 'sqlcipher3' library required. pip install sqlcipher3", file=sys.stderr)
    sys.exit(1)

from signal_meta import _get_meta, _meta_data, _meta_lock

_db_conn = None
_db_cur  = None
_db_lock = threading.Lock()


def copy_db_snapshot() -> str:
    """
    Safely copies db.sqlite, db.sqlite-wal, and db.sqlite-shm to a temporary directory.
    Uses timestamped subdirectories to prevent Windows file-lock collisions.
    """
    appdata = os.environ.get("APPDATA", "")
    src     = os.path.join(appdata, "Signal", "sql")
    dst_dir = os.path.join(os.environ.get("TEMP", "."), f"signal-player-work-{int(time.time()*1000)}")
    os.makedirs(dst_dir, exist_ok=True)

    db_src  = os.path.join(src, "db.sqlite")
    wal_src = os.path.join(src, "db.sqlite-wal")
    shm_src = os.path.join(src, "db.sqlite-shm")
    db_dst  = os.path.join(dst_dir, "db.sqlite")
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


def open_db(db_path: str, key: str):
    """Opens a SQLCipher connection, sets keys, and verifies decryptability."""
    conn = sqlcipher3.connect(db_path, check_same_thread=False)
    cur  = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{key}'\";")
    cur.execute("PRAGMA cipher_compatibility = 4;")
    # Sanity check to ensure decryption succeeded
    cur.execute("SELECT count(*) FROM sqlite_master;")
    cur.fetchone()
    return conn, cur


def reload_db(key: str) -> bool:
    """Refreshes the database snapshot and atomically updates the active connection."""
    global _db_conn, _db_cur
    try:
        new_path = copy_db_snapshot()
        new_conn, new_cur = open_db(new_path, key)
        with _db_lock:
            old_conn = _db_conn
            _db_conn = new_conn
            _db_cur = new_cur
        if old_conn:
            try:
                old_conn.close()
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"[Signal Player] Warning: reload_db failed (keeping existing connection): {e}")
        return False


def _query_groups() -> List[dict]:
    """
    Returns groups/conversations that contain downloaded videos.
    Uses indexed ma.conversationId = c.id for fast performance.
    """
    with _db_lock:
        _db_cur.execute("""
            SELECT
                c.id,
                COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS title,
                c.type,
                COUNT(DISTINCT ma.path) AS video_count
            FROM conversations c
            JOIN message_attachments ma ON ma.conversationId = c.id
            WHERE ma.contentType LIKE 'video/%'
              AND ma.path IS NOT NULL
              AND ma.localKey IS NOT NULL
            GROUP BY c.id
            ORDER BY video_count DESC;
        """)
        rows = _db_cur.fetchall()
    return [{"id": r[0], "name": r[1], "type": r[2], "video_count": r[3]} for r in rows]


def _get_conversation_map() -> Dict[str, str]:
    """Builds an in-memory mapping from conversation IDs and phone numbers to names."""
    conv_map = {}
    try:
        _db_cur.execute("SELECT id, COALESCE(name, profileName, e164, 'Unknown') FROM conversations WHERE id IS NOT NULL;")
        for cid, name in _db_cur.fetchall():
            conv_map[cid] = name
        _db_cur.execute("SELECT e164, COALESCE(name, profileName, e164, 'Unknown') FROM conversations WHERE e164 IS NOT NULL AND e164 != '';")
        for e164, name in _db_cur.fetchall():
            conv_map[e164] = name
    except Exception:
        pass
    return conv_map


def _query_new_count() -> int:
    """Returns count of newly received video attachments since last session."""
    with _meta_lock:
        last_ts = _meta_data.get("last_session_timestamp", 0)
        seen_set = set(_meta_data.get("seen_message_ids", []))
    if last_ts <= 0:
        return 0
    try:
        with _db_lock:
            _db_cur.execute("""
                SELECT ma.path, ma.messageId
                FROM message_attachments ma
                WHERE ma.contentType LIKE 'video/%'
                  AND ma.path IS NOT NULL
                  AND ma.localKey IS NOT NULL
                  AND ma.sentAt > ?;
            """, (last_ts,))
            rows = _db_cur.fetchall()
        count = 0
        for path, msg_id in rows:
            if path not in seen_set and msg_id not in seen_set:
                count += 1
        return count
    except Exception:
        return 0


def _query_media(group_id: str = None) -> Tuple[List[dict], dict]:
    """
    Queries video attachments for a specific group or all groups.
    Returns: (media_list, server_lookup_dict)
    """
    with _db_lock:
        conv_map = _get_conversation_map()

        params = []
        where_conds = [
            "ma.contentType LIKE 'video/%'",
            "ma.path IS NOT NULL",
            "ma.localKey IS NOT NULL"
        ]
        if group_id and group_id != 'all':
            where_conds.append("ma.conversationId = ?")
            params.append(group_id)

        where_clause = " AND ".join(where_conds)

        query = f"""
            SELECT
                ma.path AS item_id,
                ma.messageId,
                DATETIME(ma.sentAt / 1000, 'unixepoch', 'localtime') AS sent_time,
                m.sourceServiceId,
                m.source,
                ma.contentType,
                ma.size,
                ma.fileName,
                ma.path,
                ma.localKey,
                COALESCE(ma.sentAt, 0) AS sent_at_ms
            FROM message_attachments ma
            LEFT JOIN messages m ON m.id = ma.messageId
            WHERE {where_clause}
            ORDER BY ma.sentAt DESC;
        """
        _db_cur.execute(query, params)
        rows = _db_cur.fetchall()

    media = []
    server_lookup = {}
    for r in rows:
        item_id = r[0]
        msg_id = r[1]
        sent_time = r[2] or ""
        src_service_id = r[3]
        src_source = r[4]
        content_type = r[5] or "video/mp4"
        size = r[6] or 0
        filename = r[7] or ""
        path = r[8]
        local_key = r[9]
        sent_at_ms = int(r[10]) if r[10] else 0

        sender = conv_map.get(src_service_id) or conv_map.get(src_source) or 'Unknown'
        meta = _get_meta(item_id, msg_id=msg_id, sent_at_ms=sent_at_ms)

        media.append({
            "id":           item_id,
            "message_id":   msg_id,
            "sent_time":    sent_time,
            "sender":       sender,
            "content_type": content_type,
            "size":         size,
            "filename":     filename,
            "favourite":    meta["favourite"],
            "labels":       meta["labels"],
            "is_new":       meta["is_new"],
        })
        server_lookup[item_id] = (path, local_key, size, content_type)

    return media, server_lookup
