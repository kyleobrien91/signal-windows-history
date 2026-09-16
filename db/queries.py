#!/usr/bin/env python3
"""
db/queries.py - SQL queries for Signal database media and conversation data.

Provides functions to query video attachments, conversation groups,
conversation mappings, and new message counts since last session.
"""

from typing import Dict, List, Optional, Tuple
import threading

_db_conn = None
_db_cur = None
_db_lock = threading.Lock()


def _query_groups() -> List[dict]:
    """Returns groups/conversations that contain downloaded videos."""
    with _db_lock:
        if not _db_cur:
            return []
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
    if not _db_cur:
        return conv_map
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
    from metadata.store import _meta_data, _meta_lock
    with _meta_lock:
        last_ts = _meta_data.get("last_session_timestamp", 0)
        seen_set = set(_meta_data.get("seen_message_ids", []))
    if last_ts <= 0:
        return 0
    try:
        with _db_lock:
            if not _db_cur:
                return 0
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


def _query_media(group_id: Optional[str] = None) -> Tuple[List[dict], dict]:
    """
    Queries video attachments for a specific group or all groups.
    Returns: (media_list, server_lookup_dict)
    """
    from metadata.store import _get_meta
    with _db_lock:
        if not _db_cur:
            return [], {}
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

        ts_expr = "COALESCE(NULLIF(ma.sentAt, 0), NULLIF(m.sent_at, 0), NULLIF(m.timestamp, 0), NULLIF(ma.receivedAt, 0), NULLIF(m.received_at, 0), 0)"

        query = f"""
            SELECT
                ma.path AS item_id,
                ma.messageId,
                DATETIME({ts_expr} / 1000, 'unixepoch', 'localtime') AS sent_time,
                m.sourceServiceId,
                m.source,
                ma.contentType,
                ma.size,
                ma.fileName,
                ma.path,
                ma.localKey,
                {ts_expr} AS sent_at_ms
            FROM message_attachments ma
            LEFT JOIN messages m ON m.id = ma.messageId
            WHERE {where_clause}
            ORDER BY {ts_expr} DESC, ma.rowid DESC;
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
            "sent_at":      sent_at_ms,
            "sender":       sender,
            "content_type": content_type,
            "size":         size,
            "filename":     filename,
            "favourite":    meta["favourite"],
            "labels":       meta["labels"],
            "is_new":       meta["is_new"],
        })
        server_lookup[item_id] = (path, local_key, size, content_type)

    media.sort(key=lambda m: m.get("sent_at", 0), reverse=True)

    return media, server_lookup


# Clean public API aliases
query_groups = _query_groups
query_media = _query_media
query_new_count = _query_new_count
get_conversation_map = _get_conversation_map
