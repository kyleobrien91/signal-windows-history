#!/usr/bin/env python3
"""
db/queries.py - SQL queries for Signal database media and conversation data.

Provides functions to query video attachments, conversation groups,
conversation mappings, and new message counts since last session.
"""

from typing import List, Optional, Tuple
import threading


def _query_groups() -> str:
    """SQL query to retrieve groups/conversations with downloaded videos."""
    return """
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
    """


def _get_conversation_map() -> Tuple[str, str]:
    """SQL queries to build conversation ID and phone number to name mapping."""
    return (
        "SELECT id, COALESCE(name, profileName, e164, 'Unknown') FROM conversations WHERE id IS NOT NULL;",
        "SELECT e164, COALESCE(name, profileName, e164, 'Unknown') FROM conversations WHERE e164 IS NOT NULL AND e164 != '';",
    )


def _query_new_count(last_session_timestamp: int, seen_message_ids: list) -> str:
    """SQL query to count new video attachments since last session."""
    return """
        SELECT ma.path, ma.messageId
        FROM message_attachments ma
        WHERE ma.contentType LIKE 'video/%'
          AND ma.path IS NOT NULL
          AND ma.localKey IS NOT NULL
          AND ma.sentAt > ?;
    """


def _query_media(group_id: Optional[str] = None) -> str:
    """SQL query to retrieve video attachments for a specific group or all groups."""
    ts_expr = "COALESCE(NULLIF(ma.sentAt, 0), NULLIF(m.sent_at, 0), NULLIF(m.timestamp, 0), NULLIF(ma.receivedAt, 0), NULLIF(m.received_at, 0), 0)"

    where_conds = [
        "ma.contentType LIKE 'video/%'",
        "ma.path IS NOT NULL",
        "ma.localKey IS NOT NULL",
    ]

    if group_id and group_id != "all":
        where_conds.append("ma.conversationId = ?")

    where_clause = " AND ".join(where_conds)

    return f"""
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


_db_lock_usage = """
    All query functions must use _db_lock with context manager:

    with _db_lock:
        _db_cur.execute(query, params)
        rows = _db_cur.fetchall()
"""