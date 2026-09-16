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


import base64
import hashlib
import hmac
import json
import secrets

_CURSOR_SECRET = secrets.token_bytes(32)


def encode_cursor(sent_at_ms: int, rowid: int) -> str:
    """Encodes (sent_at_ms, rowid) into an opaque, HMAC-authenticated Base64 string."""
    payload = json.dumps({"ts": int(sent_at_ms), "rowid": int(rowid)}, separators=(',', ':')).encode('utf-8')
    sig = hmac.new(_CURSOR_SECRET, payload, hashlib.sha256).digest()
    p_b64 = base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
    s_b64 = base64.urlsafe_b64encode(sig).decode('ascii').rstrip('=')
    return f"{p_b64}.{s_b64}"


def decode_cursor(cursor_str: str) -> Tuple[int, int]:
    """Decodes and validates an opaque cursor string into (sent_at_ms, rowid).

    Raises:
        ValueError: If the cursor is malformed or HMAC verification fails.
    """
    if not cursor_str or "." not in cursor_str:
        raise ValueError("Empty or malformed cursor string")
    try:
        p_b64, s_b64 = cursor_str.split(".", 1)
        p_b64_padded = p_b64 + "=" * ((4 - len(p_b64) % 4) % 4)
        s_b64_padded = s_b64 + "=" * ((4 - len(s_b64) % 4) % 4)
        payload = base64.urlsafe_b64decode(p_b64_padded.encode('ascii'))
        sig = base64.urlsafe_b64decode(s_b64_padded.encode('ascii'))
        expected_sig = hmac.new(_CURSOR_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            raise ValueError("Cursor HMAC signature verification failed")
        data = json.loads(payload.decode('utf-8'))
        return int(data["ts"]), int(data["rowid"])
    except Exception as e:
        raise ValueError(f"Invalid cursor: {e}")


def _query_media_paged(
    group_id: Optional[str] = 'all',
    limit: int = 50,
    cursor: Optional[str] = None,
    view: Optional[str] = None,
    label: Optional[str] = None,
    search: Optional[str] = None,
) -> Tuple[dict, dict]:
    """
    Queries video attachments with deterministic keyset/cursor pagination.
    Returns: (page_dict, server_lookup_dict)
      page_dict: {"items": [...], "has_more": bool, "next_cursor": str or None}
    """
    from metadata.store import _get_meta

    # Enforce limit bounds (1 <= limit <= 100, default 50)
    try:
        limit = max(1, min(100, int(limit)))
    except (ValueError, TypeError):
        limit = 50

    cursor_ts = None
    cursor_rowid = None
    if cursor:
        cursor_ts, cursor_rowid = decode_cursor(cursor)

    with _db_lock:
        if not _db_cur:
            return {"items": [], "has_more": False, "next_cursor": None}, {}
        conv_map = _get_conversation_map()

        where_conds = [
            "ma.contentType LIKE 'video/%'",
            "ma.path IS NOT NULL",
            "ma.localKey IS NOT NULL"
        ]
        params = []

        if group_id and group_id != 'all':
            where_conds.append("ma.conversationId = ?")
            params.append(group_id)

        ts_expr = "COALESCE(NULLIF(ma.sentAt, 0), NULLIF(m.sent_at, 0), NULLIF(m.timestamp, 0), NULLIF(ma.receivedAt, 0), NULLIF(m.received_at, 0), 0)"

        filtered_media = []
        server_lookup = {}
        last_processed_item = None
        has_more = False

        search_q = (search or "").lower().strip()

        # Chunked SQL keyset loop to ensure SQL queries always execute with explicit LIMIT
        chunk_ts = cursor_ts
        chunk_rowid = cursor_rowid
        has_filters = bool(view or search_q or label)
        chunk_size = (limit + 1) if not has_filters else min(200, limit * 4)

        while len(filtered_media) < limit + 1:
            loop_where = list(where_conds)
            loop_params = list(params)

            if chunk_ts is not None and chunk_rowid is not None:
                loop_where.append(f"({ts_expr} < ? OR ({ts_expr} = ? AND ma.rowid < ?))")
                loop_params.extend([chunk_ts, chunk_ts, chunk_rowid])

            loop_clause = " AND ".join(loop_where)

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
                    {ts_expr} AS sent_at_ms,
                    ma.rowid AS attachment_rowid
                FROM message_attachments ma
                LEFT JOIN messages m ON m.id = ma.messageId
                WHERE {loop_clause}
                ORDER BY {ts_expr} DESC, ma.rowid DESC
                LIMIT ?;
            """
            loop_params.append(chunk_size)

            _db_cur.execute(query, loop_params)
            rows = _db_cur.fetchall()

            if not rows:
                break

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
                rowid = int(r[11]) if r[11] else 0

                chunk_ts = sent_at_ms
                chunk_rowid = rowid

                sender = conv_map.get(src_service_id) or conv_map.get(src_source) or 'Unknown'
                meta = _get_meta(item_id, msg_id=msg_id, sent_at_ms=sent_at_ms)

                # Apply metadata/search filters
                if view == 'new' and not meta["is_new"]:
                    continue
                if view == 'favourites' and not meta["favourite"]:
                    continue
                if view == 'label' and label and label not in meta["labels"]:
                    continue
                if search_q:
                    matches_fn = search_q in filename.lower()
                    matches_sender = search_q in sender.lower()
                    matches_label = any(search_q in l.lower() for l in meta["labels"])
                    if not (matches_fn or matches_sender or matches_label):
                        continue

                # Generate derivative URLs
                from urllib.parse import quote
                from crypto.cache import DerivedMediaCache
                if not hasattr(_query_media_paged, "_global_cache"):
                    import os
                    cache_dir = os.path.join(os.environ.get("APPDATA", ""), "Signal", "derived_cache")
                    _query_media_paged._global_cache = DerivedMediaCache(cache_dir=cache_dir)
                cache = _query_media_paged._global_cache

                poster_params = {'width': 320, 'height': 180, 'format': 'webp', 'quality': 80}
                poster_key = cache.derive_cache_key(item_id, 'poster', 1, poster_params)
                poster_url = f"/api/media/derivative/{poster_key}?id={quote(item_id)}&type=poster&w=320&h=180&v=1&fmt=webp&q=80"

                preview_params = {'width': 320, 'height': 180, 'frames': 5, 'format': 'webp', 'quality': 80}
                preview_key = cache.derive_cache_key(item_id, 'preview', 1, preview_params)
                preview_url = f"/api/media/derivative/{preview_key}?id={quote(item_id)}&type=preview&w=320&h=180&frames=5&v=1&fmt=webp&q=80"

                item = {
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
                    "poster_url":   poster_url,
                    "preview_url":  preview_url,
                    "_rowid":       rowid,
                }

                if len(filtered_media) < limit:
                    filtered_media.append(item)
                    server_lookup[item_id] = (path, local_key, size, content_type)
                    last_processed_item = item
                else:
                    has_more = True
                    break

            if len(rows) < chunk_size and not has_more:
                break

    next_cursor = None
    if has_more and last_processed_item:
        next_cursor = encode_cursor(last_processed_item["sent_at"], last_processed_item["_rowid"])

    # Clean internal `_rowid` from output items
    for m in filtered_media:
        m.pop("_rowid", None)

    page_res = {
        "items": filtered_media,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }

    return page_res, server_lookup


def _query_media(group_id: Optional[str] = None) -> Tuple[List[dict], dict]:
    """Backward compatibility wrapper returning unpaged list."""
    page_res, server_lookup = _query_media_paged(group_id=group_id, limit=1000000)
    return page_res["items"], server_lookup


# Clean public API aliases
query_groups = _query_groups
query_media = _query_media
query_media_paged = _query_media_paged
query_new_count = _query_new_count
get_conversation_map = _get_conversation_map
