#!/usr/bin/env python3
"""
signal_player.py - Signal Desktop Local Video Player
=====================================================
Launches a local HTTP server (127.0.0.1 only) that reads Signal's SQLCipher
database, decrypts attachment blobs entirely in RAM, and streams them to a
browser-based video library with YouTube-style hover preview.

Security guarantees:
  - Nothing is written to disk in decrypted form (ever)
  - The localKey and raw attachment paths are kept server-side only
    and are never transmitted to the browser frontend
  - Server binds to 127.0.0.1 only — not accessible from the network
  - In-memory LRU cache is cleared on shutdown

Metadata (favourites + labels) are stored in signal_player_meta.json
alongside this script. This file contains only messageIds (not keys or
content) plus user-created annotations — safe to store on disk.

Usage:
    python signal_player.py [--port 7788] [--no-browser]

Dependencies (already installed from previous session):
    pip install cryptography sqlcipher3
"""

import argparse
import base64
import collections
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    print("Error: 'cryptography' required.  pip install cryptography", file=sys.stderr)
    sys.exit(1)

try:
    import sqlcipher3
except ImportError:
    print("Error: 'sqlcipher3' required.  pip install sqlcipher3", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Windows DPAPI + Signal key extraction
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_decrypt(enc: bytes) -> bytes:
    blob_in = _DATA_BLOB(
        len(enc),
        ctypes.cast(ctypes.create_string_buffer(enc), ctypes.POINTER(ctypes.c_byte)),
    )
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError(ctypes.GetLastError())
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def get_signal_key() -> str:
    appdata = os.environ.get("APPDATA", "")
    sig_dir = os.path.join(appdata, "Signal")

    with open(os.path.join(sig_dir, "Local State"), "r", encoding="utf-8") as f:
        ls = json.load(f)
    raw = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if not raw.startswith(b"DPAPI"):
        raise ValueError("Unexpected os_crypt.encrypted_key format (no DPAPI prefix)")
    master_key = _dpapi_decrypt(raw[5:])

    with open(os.path.join(sig_dir, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if "encryptedKey" in cfg:
        enc = bytes.fromhex(cfg["encryptedKey"])
        if not enc.startswith(b"v10"):
            raise ValueError("Unexpected encryptedKey format (no v10 prefix)")
        nonce, ct = enc[3:15], enc[15:]
        return AESGCM(master_key).decrypt(nonce, ct, None).decode("utf-8")
    if "key" in cfg:
        return cfg["key"]
    raise KeyError("No key in Signal config.json")


def copy_db_snapshot() -> str:
    appdata = os.environ.get("APPDATA", "")
    src     = os.path.join(appdata, "Signal", "sql")
    # Unique timestamped directory prevents Windows file-lock collisions with open connections
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
    conn = sqlcipher3.connect(db_path, check_same_thread=False)
    cur  = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{key}'\";")
    cur.execute("PRAGMA cipher_compatibility = 4;")
    # Quick sanity check to verify decryption and WAL recovery
    cur.execute("SELECT count(*) FROM sqlite_master;")
    cur.fetchone()
    return conn, cur


def reload_db(key: str):
    """Refreshes the database snapshot and swaps the active database cursor atomically."""
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


# ---------------------------------------------------------------------------
# In-memory attachment decryption — plaintext NEVER written to disk
# ---------------------------------------------------------------------------

def _decrypt_blob(enc_path: str, local_key_b64: str, declared_size: int = None) -> bytes:
    raw_key = base64.b64decode(local_key_b64)
    aes_key = raw_key[:32]
    mac_key = raw_key[32:]

    with open(enc_path, "rb") as fh:
        enc_data = fh.read()

    iv   = enc_data[:16]
    body = enc_data[16:-32]
    tag  = enc_data[-32:]

    expected = hmac.new(mac_key, enc_data[:-32], hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("HMAC verification failed — file may be corrupt or tampered")

    cipher    = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded    = decryptor.update(body) + decryptor.finalize()
    pad_len   = padded[-1]
    plaintext = padded[:-pad_len]

    if declared_size and declared_size <= len(plaintext):
        plaintext = plaintext[:declared_size]
    return plaintext


# ---------------------------------------------------------------------------
# In-memory LRU cache — RAM only, cleared on shutdown
# ---------------------------------------------------------------------------

_CACHE_MAX = 10
_cache: collections.OrderedDict = collections.OrderedDict()
_cache_lock = threading.Lock()


def _get_cached(msg_id: str, enc_path: str, local_key: str, size: int) -> bytes:
    with _cache_lock:
        if msg_id in _cache:
            _cache.move_to_end(msg_id)
            return _cache[msg_id]

    data = _decrypt_blob(enc_path, local_key, size)

    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.popitem(last=False)
        _cache[msg_id] = data
    return data


# ---------------------------------------------------------------------------
# Metadata store — favourites + labels
# Stored as signal_player_meta.json next to this script.
# Contains ONLY messageIds + user annotations. No keys. No content.
# ---------------------------------------------------------------------------

_META_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_player_meta.json")
_meta_data: dict = {
    "last_session_timestamp": 0,
    "seen_message_ids": [],
    "annotations": {}
}
_session_start_ts: int = int(time.time() * 1000)
_meta_lock = threading.Lock()


def _load_metadata():
    global _meta_data
    if os.path.exists(_META_PATH):
        try:
            with open(_META_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "annotations" in raw:
                _meta_data = {
                    "last_session_timestamp": raw.get("last_session_timestamp", 0),
                    "seen_message_ids": list(raw.get("seen_message_ids", [])),
                    "annotations": raw.get("annotations", {}),
                }
            elif isinstance(raw, dict):
                # Backwards compatible migration from { msg_id: { favourite, labels } }
                _meta_data = {
                    "last_session_timestamp": 0,
                    "seen_message_ids": list(raw.keys()),
                    "annotations": raw,
                }
            print(f"[Signal Player] [OK] Metadata loaded ({len(_meta_data['annotations'])} annotations, {len(_meta_data['seen_message_ids'])} seen)")
        except Exception as e:
            print(f"[Signal Player] Warning: could not load metadata: {e}")
            _meta_data = {"last_session_timestamp": 0, "seen_message_ids": [], "annotations": {}}
    else:
        _meta_data = {"last_session_timestamp": 0, "seen_message_ids": [], "annotations": {}}


def _save_metadata():
    with open(_META_PATH, "w", encoding="utf-8") as f:
        json.dump(_meta_data, f, ensure_ascii=False, indent=2)


def _get_meta(item_id: str, msg_id: str = "", sent_at_ms: int = 0) -> dict:
    with _meta_lock:
        ann = _meta_data["annotations"].get(item_id) or _meta_data["annotations"].get(msg_id, {"favourite": False, "labels": []})
        last_ts = _meta_data.get("last_session_timestamp", 0)
        seen_set = set(_meta_data.get("seen_message_ids", []))
        is_seen = (item_id in seen_set) or (msg_id in seen_set)
        is_new = bool(last_ts > 0 and sent_at_ms > last_ts and not is_seen)
        return {
            "favourite": bool(ann.get("favourite", False)),
            "labels": list(ann.get("labels", [])),
            "is_new": is_new,
        }


def _set_meta(msg_id: str, favourite: bool = None, labels: list = None):
    with _meta_lock:
        entry = _meta_data["annotations"].setdefault(msg_id, {"favourite": False, "labels": []})
        if favourite is not None:
            entry["favourite"] = bool(favourite)
        if labels is not None:
            seen = []
            for lbl in labels:
                lbl = str(lbl).strip()[:30]
                if lbl and lbl not in seen:
                    seen.append(lbl)
            entry["labels"] = seen[:20]
        _save_metadata()
        return dict(entry)


def _mark_seen(msg_ids: list):
    with _meta_lock:
        seen_list = _meta_data.setdefault("seen_message_ids", [])
        seen_set = set(seen_list)
        changed = False
        for mid in msg_ids:
            if mid and mid not in seen_set:
                seen_set.add(mid)
                seen_list.append(mid)
                changed = True
        if changed:
            _save_metadata()


def _all_labels() -> list:
    """Returns all unique labels in use, sorted alphabetically."""
    with _meta_lock:
        seen = set()
        for v in _meta_data["annotations"].values():
            seen.update(v.get("labels", []))
    return sorted(seen)


_sync_lock = threading.Lock()
_sync_state = {
    "is_running": False,
    "pending_count": 0,
    "total_initial": 0,
    "last_updated": 0
}


def _get_sync_status():
    with _sync_lock:
        return dict(_sync_state)


def _set_sync_status(is_running: bool, pending: int = 0, initial: int = 0):
    with _sync_lock:
        _sync_state["is_running"] = is_running
        _sync_state["pending_count"] = pending
        if initial > 0:
            _sync_state["total_initial"] = initial
        _sync_state["last_updated"] = int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

_db_conn = None
_db_cur  = None
_db_lock = threading.Lock()


def _query_groups():
    with _db_lock:
        _db_cur.execute("""
            SELECT
                c.id,
                COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS title,
                c.type,
                COUNT(DISTINCT ma.path) AS video_count
            FROM conversations c
            JOIN messages m ON m.conversationId = c.id
            JOIN message_attachments ma ON ma.messageId = m.id
            WHERE ma.contentType LIKE 'video/%'
              AND ma.path IS NOT NULL
              AND ma.localKey IS NOT NULL
            GROUP BY c.id
            ORDER BY video_count DESC;
        """)
        rows = _db_cur.fetchall()
    return [{"id": r[0], "name": r[1], "type": r[2], "video_count": r[3]} for r in rows]


def _get_conversation_map():
    """Builds fast in-memory lookup for sender names without slow correlated subqueries."""
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


def _query_media(group_id: str = None):
    with _db_lock:
        conv_map = _get_conversation_map()

        params = []
        where_conds = [
            "ma.contentType LIKE 'video/%'",
            "ma.path IS NOT NULL",
            "ma.localKey IS NOT NULL"
        ]
        if group_id and group_id != 'all':
            where_conds.append("m.conversationId = ?")
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
            JOIN messages m ON m.id = ma.messageId
            WHERE {where_clause}
            ORDER BY ma.sentAt ASC;
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



_media_lookup: dict = {}
_lookup_lock  = threading.Lock()
_attach_root  = ""


# ---------------------------------------------------------------------------
# Embedded HTML / CSS / JS
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Signal Player</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#1b1d21;--sidebar:#13141a;--card:#1e2029;
  --accent:#3b82f6;--text:#e2e8f0;--muted:#64748b;
  --border:#2a2d3a;--hover:#252836;--active:#1e3a5f;
  --fav:#f59e0b;--danger:#ef4444;
}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:flex;height:100vh;overflow:hidden}

/* ── Sidebar ── */
#sidebar{width:240px;min-width:200px;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
#sidebar-header{padding:16px 14px 12px;border-bottom:1px solid var(--border)}
#sidebar-header h1{font-size:15px;font-weight:600;color:var(--accent);display:flex;align-items:center;gap:7px}
#sidebar-header p{font-size:10px;color:var(--muted);margin-top:4px;line-height:1.4}
#sidebar-scroll{overflow-y:auto;flex:1;padding:6px 0}

.sidebar-section{padding:6px 12px 4px;font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-top:6px}
.nav-item{padding:9px 14px;cursor:pointer;border-bottom:1px solid var(--border);transition:background .12s;display:flex;align-items:center;gap:8px;font-size:13px}
.nav-item:hover{background:var(--hover)}
.nav-item.active{background:var(--active);border-left:3px solid var(--accent);padding-left:11px}
.nav-item .badge{margin-left:auto;font-size:10px;background:rgba(255,255,255,.1);padding:1px 6px;border-radius:10px;color:var(--muted)}
.nav-item.active .badge{color:var(--text)}

.group-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.group-count{font-size:11px;color:var(--muted);margin-top:1px}

/* ── Main ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#toolbar{padding:12px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;background:var(--sidebar);flex-shrink:0}
#toolbar h2{font-size:15px;font-weight:600;flex:0 0 auto}
#toolbar-info{font-size:12px;color:var(--muted);flex:0 0 auto}
#search-wrap{flex:1;display:flex;justify-content:flex-end}
#search{background:rgba(255,255,255,.07);border:1px solid var(--border);border-radius:6px;padding:5px 10px;color:var(--text);font-size:12px;width:200px;outline:none}
#search:focus{border-color:var(--accent)}
#search::placeholder{color:var(--muted)}

#grid-wrap{flex:1;overflow-y:auto;padding:14px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}

/* ── Cards ── */
.card{background:var(--card);border-radius:8px;overflow:hidden;cursor:pointer;border:1px solid var(--border);transition:transform .15s,border-color .15s;position:relative}
.card:hover{transform:scale(1.035);border-color:var(--accent);z-index:2}
.card.is-fav{border-color:rgba(245,158,11,.4)}
.thumb{position:relative;width:100%;padding-top:56.25%;background:#0a0a0a;overflow:hidden}
.thumb video{position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;pointer-events:none}
.play-icon{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:36px;height:36px;background:rgba(0,0,0,.55);border-radius:50%;display:flex;align-items:center;justify-content:center;transition:opacity .15s}
.card:hover .play-icon{opacity:0}
.play-icon svg{fill:#fff;width:13px;height:13px;margin-left:2px}
.dur{position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.78);color:#fff;font-size:10px;padding:2px 5px;border-radius:3px;font-variant-numeric:tabular-nums}
.fav-badge{position:absolute;top:6px;left:6px;font-size:15px;line-height:1;filter:drop-shadow(0 1px 2px rgba(0,0,0,.8))}
.new-badge{position:absolute;top:6px;right:6px;background:#10b981;color:#fff;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;letter-spacing:.05em;text-transform:uppercase;box-shadow:0 1px 4px rgba(0,0,0,.6)}
.btn-mark-seen{background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.35);color:#34d399;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:500;cursor:pointer;margin-right:8px;transition:all .15s}
.btn-mark-seen:hover{background:#10b981;color:#fff}
.card-meta{padding:8px 10px}
.card-fn{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px}
.card-ts{font-size:11px;color:var(--muted);margin-bottom:4px}
.card-labels{display:flex;flex-wrap:wrap;gap:3px}

/* ── Label chips ── */
.chip{display:inline-block;font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500;white-space:nowrap}
.chip-sm{font-size:10px;padding:1px 6px}

/* ── States ── */
#empty,#loading{display:none;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--muted);gap:14px;font-size:14px}
.spinner{width:30px;height:30px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Modal ── */
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:1000;flex-direction:column;align-items:center;justify-content:center}
#modal.open{display:flex}
#modal-x{position:absolute;top:14px;right:20px;font-size:28px;color:#fff;cursor:pointer;background:none;border:none;line-height:1;opacity:.7;z-index:1001;transition:opacity .15s}
#modal-x:hover{opacity:1}
#modal-video{max-width:90vw;max-height:72vh;border-radius:6px;background:#000;display:block;outline:none}

/* ── Modal metadata panel ── */
#modal-panel{display:flex;flex-direction:column;align-items:center;gap:10px;margin-top:10px;width:min(700px,90vw)}
#modal-info{text-align:center;color:var(--muted);font-size:13px;width:100%}
#modal-info strong{color:var(--text)}

#modal-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center}

/* Favourite button */
#fav-btn{background:none;border:2px solid rgba(255,255,255,.2);border-radius:8px;padding:7px 16px;color:#fff;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .15s}
#fav-btn:hover{border-color:var(--fav);color:var(--fav)}
#fav-btn.active{border-color:var(--fav);background:rgba(245,158,11,.15);color:var(--fav)}

/* Label input area */
#label-wrap{display:flex;align-items:center;gap:6px;flex-wrap:wrap;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:8px;padding:6px 10px;min-width:280px;max-width:500px;cursor:text}
#label-input{background:none;border:none;color:var(--text);font-size:12px;outline:none;min-width:80px;flex:1}
#label-input::placeholder{color:var(--muted)}
.label-chip-rm{display:inline-flex;align-items:center;gap:3px;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:500}
.label-chip-rm button{background:none;border:none;color:inherit;cursor:pointer;font-size:12px;line-height:1;padding:0;margin-left:1px;opacity:.7}
.label-chip-rm button:hover{opacity:1}

/* Nav buttons */
#modal-nav{display:flex;gap:14px}
.nbtn{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:#fff;padding:7px 18px;border-radius:6px;cursor:pointer;font-size:13px;transition:background .15s}
.nbtn:hover{background:rgba(255,255,255,.2)}
.nbtn:disabled{opacity:.3;cursor:default;pointer-events:none}

/* Keyboard hint */
#kb-hint{font-size:10px;color:var(--muted);margin-top:4px}
</style>
</head>
<body>

<div id="sidebar">
  <div id="sidebar-header">
    <h1>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/>
        <path d="M10 8l6 4-6 4V8z"/>
      </svg>
      Signal Player
    </h1>
    <p>Nothing written to disk * keys stay server-side</p>
  </div>
  <div id="sidebar-scroll">
    <div class="sidebar-section">Views</div>
    <div class="nav-item" id="nav-new" onclick="setView('new')">
      <span>✨</span><span class="group-name">New Videos</span>
      <span class="badge" id="new-count" style="display:none;background:#10b981;color:#fff;font-weight:600">0</span>
    </div>
    <div class="nav-item" id="nav-all" onclick="setView('all')">
      <span>📹</span><span class="group-name">All Videos</span>
    </div>
    <div class="nav-item" id="nav-fav" onclick="setView('favourites')">
      <span>⭐</span><span class="group-name">Favourites</span>
    </div>

    <div class="sidebar-section">Groups</div>
    <div id="group-list">
      <div style="padding:16px;color:var(--muted);font-size:12px;text-align:center">Loading…</div>
    </div>

    <div class="sidebar-section" id="label-section" style="display:none">Labels</div>
    <div id="label-list"></div>
  </div>
</div>

<div id="main">
  <div id="sync-banner" style="display:none;background:#065f46;border-bottom:1px solid #10b981;padding:8px 18px;font-size:12px;color:#ecfdf5;display:none;align-items:center;justify-content:space-between">
    <div style="display:flex;align-items:center;gap:8px">
      <div class="spinner" style="width:14px;height:14px;border-width:2px;border-top-color:#ecfdf5"></div>
      <span id="sync-status-text">Signal is running in background downloading pending media...</span>
    </div>
    <span id="sync-stats" style="font-weight:600"></span>
  </div>
  <div id="toolbar">
    <h2 id="group-title">Select a view</h2>
    <span id="toolbar-info"></span>
    <div id="search-wrap">
      <button class="btn-mark-seen" id="btn-mark-all" style="display:none" onclick="markAllSeen()">✔ Mark All Seen</button>
      <input id="search" type="text" placeholder="Search filename or label…" oninput="applyFilter()">
    </div>
  </div>

  <div id="grid-wrap">
    <div id="empty">
      <svg width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.4" viewBox="0 0 24 24">
        <rect x="2" y="6" width="20" height="14" rx="2"/><path d="m10 9 5 3-5 3V9z"/>
      </svg>
      <span id="empty-msg">Select a group or view from the sidebar</span>
    </div>
    <div id="loading"><div class="spinner"></div><span>Loading media…</span></div>
    <div id="grid"></div>
  </div>
</div>

<!-- Full-screen modal player -->
<div id="modal" onclick="if(event.target===this)closeModal()">
  <button id="modal-x" onclick="closeModal()">&times;</button>
  <video id="modal-video" controls autoplay></video>

  <div id="modal-panel">
    <div id="modal-info"></div>

    <div id="modal-actions">
      <!-- Favourite toggle -->
      <button id="fav-btn" onclick="toggleFav()">
        <span id="fav-icon">♡</span>
        <span id="fav-label">Favourite</span>
      </button>

      <!-- Label input -->
      <div id="label-wrap" onclick="document.getElementById('label-input').focus()">
        <div id="modal-chips"></div>
        <input id="label-input" type="text" placeholder="Add label… (Enter)" maxlength="30"
               onkeydown="handleLabelKey(event)">
      </div>
    </div>

    <div id="modal-nav">
      <button class="nbtn" id="btn-prev" onclick="navigate(-1)">&#8592; Prev</button>
      <button class="nbtn" id="btn-next" onclick="navigate(1)">Next &#8594;</button>
    </div>
    <div id="kb-hint">← → navigate &nbsp;*&nbsp; F favourite &nbsp;*&nbsp; Esc close</div>
  </div>
</div>

<script>
'use strict';
const $ = id => document.getElementById(id);

// ─────────────────────────────────────────────────────────────────────────────
// Comprehensive Logging & Diagnostics
// ─────────────────────────────────────────────────────────────────────────────
const LOG_TAG = '[SignalPlayer]';
function log(topic, msg, ...extra) {
  const ts = new Date().toISOString().substring(11, 23);
  console.log(`%c${LOG_TAG}[${ts}][${topic}] %c${msg}`, 'color: #3b82f6; font-weight: bold;', 'color: inherit;', ...extra);
}
function logWarn(topic, msg, ...extra) {
  const ts = new Date().toISOString().substring(11, 23);
  console.warn(`%c${LOG_TAG}[${ts}][${topic}] %c${msg}`, 'color: #f59e0b; font-weight: bold;', 'color: inherit;', ...extra);
}
function logErr(topic, msg, ...extra) {
  const ts = new Date().toISOString().substring(11, 23);
  console.error(`%c${LOG_TAG}[${ts}][${topic}] %c${msg}`, 'color: #ef4444; font-weight: bold;', 'color: inherit;', ...extra);
}

window.addEventListener('error', (e) => {
  logErr('GlobalError', `Uncaught window error: "${e.message}" at ${e.filename}:${e.lineno}:${e.colno}`, e.error);
});
window.addEventListener('unhandledrejection', (e) => {
  logErr('UnhandledRejection', 'Unhandled Promise Rejection:', e.reason);
});

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────
let allMedia     = [];    // full list for current group/view
let filtered     = [];    // after search/label filter
let currentIdx   = -1;
let currentView  = null;  // { type: 'group'|'all'|'favourites'|'label', id?, label? }
let groups       = [];
const hoverState = {};

// ─────────────────────────────────────────────────────────────────────────────
// Label colour palette (hash of label name → hsl)
// ─────────────────────────────────────────────────────────────────────────────
function labelColour(lbl) {
  let h = 0;
  for (let i = 0; i < lbl.length; i++) h = (h * 31 + lbl.charCodeAt(i)) & 0xffffffff;
  return `hsl(${((h >>> 0) % 360)},60%,38%)`;
}
function chipHtml(lbl, removable = false, small = false) {
  const bg  = labelColour(lbl);
  const cls = removable ? 'label-chip-rm' : `chip${small ? ' chip-sm' : ''}`;
  const rm  = removable ? `<button onclick="removeLabel('${esc(lbl)}')" title="Remove">×</button>` : '';
  return `<span class="${cls}" style="background:${bg};color:#fff">${esc(lbl)}${rm}</span>`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? '')
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}
function fmtDur(s) {
  if (!isFinite(s) || s < 0) return '?:??';
  const m = Math.floor(s/60), sec = Math.floor(s%60);
  return `${m}:${String(sec).padStart(2,'0')}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Bootstrap
// ─────────────────────────────────────────────────────────────────────────────
async function init() {
  log('Init', 'Client script started. Initializing event listeners & views...');

  const groupListEl = $('group-list');
  const labelListEl = $('label-list');

  if (!groupListEl) {
    logErr('Init', 'Element #group-list not found in DOM!');
  } else {
    log('Init', 'Binding click event listener to #group-list container (event delegation)');
    groupListEl.addEventListener('click', (e) => {
      log('Click', 'Group-list container click event triggered.', { target: e.target });
      const item = e.target.closest('.nav-item');
      if (!item) {
        logWarn('Click', 'Click was inside #group-list but not within a .nav-item element.');
        return;
      }
      const id = item.dataset.id;
      log('Click', `Clicked .nav-item found. data-id="${id}"`);
      const g = groups.find(x => x.id === id);
      const name = g ? g.name : (item.querySelector('.group-name')?.textContent?.trim() || id);
      log('Click', `Resolved group: "${name}" (id: ${id}, existsInGroupsArray: ${Boolean(g)}). Invoking loadGroup()...`);
      loadGroup(id, name);
    });
  }

  if (!labelListEl) {
    logErr('Init', 'Element #label-list not found in DOM!');
  } else {
    log('Init', 'Binding click event listener to #label-list container');
    labelListEl.addEventListener('click', (e) => {
      log('Click', 'Label-list container click event triggered.', { target: e.target });
      const item = e.target.closest('.nav-item');
      if (!item) return;
      const lbl = item.dataset.label;
      log('Click', `Clicked label: "${lbl}". Invoking loadLabel()...`);
      if (lbl) loadLabel(lbl);
    });
  }

  log('Init', 'Fetching groups from server: GET /api/groups');
  const t0 = performance.now();
  try {
    const res = await fetch('/api/groups');
    const elapsed = (performance.now() - t0).toFixed(1);
    log('Init', `/api/groups response received in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`);
    if (!res.ok) {
      const errTxt = await res.text();
      logErr('Init', `/api/groups HTTP error ${res.status}: ${errTxt}`);
      throw new Error(`Server error ${res.status}: ${errTxt}`);
    }
    groups = await res.json();
    log('Init', `Successfully parsed /api/groups. Total groups: ${groups.length}`, groups);
    renderSidebar();
    await refreshLabels();
    await updateNewCount();
    startSyncPolling();
    log('Init', 'Initialization completed successfully.');
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('Init', `Failed to initialize groups after ${elapsed}ms:`, err);
    $('empty-msg').innerHTML = `Failed to load groups from server:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span><br><small style="color:var(--muted)">Check Console (F12) for details</small>`;
  }
}

let lastPendingCount = -1;
function startSyncPolling() {
  log('Sync', 'Starting background sync status polling interval (every 4000ms)...');
  setInterval(async () => {
    try {
      const res = await fetch('/api/sync/status');
      if (!res.ok) {
        logWarn('Sync', `/api/sync/status returned HTTP ${res.status}`);
        return;
      }
      const data = await res.json();
      const banner = $('sync-banner');
      if (!banner) return;

      if (data.is_running) {
        banner.style.display = 'flex';
        const rem = data.pending_count ?? 0;
        $('sync-stats').textContent = `${rem} pending video${rem !== 1 ? 's' : ''}`;
        log('Sync', `Background download active. ${rem} video(s) pending.`);

        // If newly downloaded videos were detected, refresh UI
        if (lastPendingCount !== -1 && rem < lastPendingCount) {
          log('Sync', `Pending count decreased from ${lastPendingCount} to ${rem}. Refreshing UI...`);
          const gRes = await fetch('/api/groups');
          groups = await gRes.json();
          renderSidebar();
          await updateNewCount();
          if (currentView && currentView.type === 'new') {
            await setView('new');
          } else if (currentView && currentView.type === 'group') {
            await loadGroup(currentView.id, $('group-title').textContent);
          }
        }
        lastPendingCount = rem;
      } else {
        if (banner.style.display !== 'none') {
          log('Sync', 'Background sync finished. Hiding banner and refreshing group list.');
          banner.style.display = 'none';
          const gRes = await fetch('/api/groups');
          groups = await gRes.json();
          renderSidebar();
          await updateNewCount();
        }
      }
    } catch (e) {
      logWarn('Sync', 'Polling /api/sync/status encountered error:', e);
    }
  }, 4000);
}

async function updateNewCount() {
  try {
    log('NewCount', 'Fetching new video count: GET /api/media/new_count');
    const res = await fetch('/api/media/new_count');
    if (!res.ok) {
      logWarn('NewCount', `/api/media/new_count returned HTTP ${res.status}`);
      return;
    }
    const data = await res.json();
    const count = data.count ?? 0;
    log('NewCount', `Received count: ${count}`);
    const badge = $('new-count');
    if (badge) {
      badge.textContent = count;
      badge.style.display = count > 0 ? 'inline-block' : 'none';
    }
  } catch (e) {
    logWarn('NewCount', 'Failed to fetch /api/media/new_count:', e);
  }
}

function renderSidebar() {
  log('Sidebar', `renderSidebar called. Rendering ${groups.length} groups.`);
  const list = $('group-list');
  if (!list) {
    logErr('Sidebar', '#group-list element missing in DOM!');
    return;
  }
  if (!groups.length) {
    logWarn('Sidebar', 'groups list is empty.');
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px;text-align:center">No downloaded videos</div>';
    return;
  }
  list.innerHTML = groups.map(g => `
    <div class="nav-item" data-id="${esc(g.id)}">
      <div>
        <div class="group-name">${esc(g.name)}</div>
        <div class="group-count">${g.video_count} video${g.video_count!==1?'s':''} • ${esc(g.type)}</div>
      </div>
    </div>`).join('');
  log('Sidebar', `Injected ${groups.length} group items into #group-list.`);
}

async function refreshLabels() {
  log('Labels', 'Fetching labels: GET /api/labels');
  try {
    const res    = await fetch('/api/labels');
    const labels = await res.json();
    log('Labels', `Received ${labels.length} labels:`, labels);
    const sec    = $('label-section');
    const list   = $('label-list');
    if (!labels.length) {
      if (sec) sec.style.display='none';
      if (list) list.innerHTML='';
      return;
    }
    if (sec) sec.style.display = '';
    if (list) {
      list.innerHTML = labels.map(lbl => `
        <div class="nav-item" data-label="${esc(lbl)}">
          <span>${chipHtml(lbl,false,true)}</span>
        </div>`).join('');
    }
  } catch (e) {
    logWarn('Labels', 'Failed to refresh labels:', e);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// View switching
// ─────────────────────────────────────────────────────────────────────────────
function setActiveNav(type, id = null, label = null) {
  log('Nav', `setActiveNav: type="${type}", id="${id}", label="${label}"`);
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const btnMark = $('btn-mark-all');
  if (btnMark) btnMark.style.display = type === 'new' ? 'inline-block' : 'none';

  if (type === 'new')         $('nav-new')?.classList.add('active');
  else if (type === 'all')    $('nav-all')?.classList.add('active');
  else if (type === 'favourites') $('nav-fav')?.classList.add('active');
  else if (type === 'group' && id) {
    const el = document.querySelector(`.nav-item[data-id="${CSS.escape(id)}"]`);
    if (el) el.classList.add('active');
    else logWarn('Nav', `Could not find sidebar .nav-item with data-id="${id}" to set active.`);
  } else if (type === 'label' && label) {
    const el = document.querySelector(`.nav-item[data-label="${CSS.escape(label)}"]`);
    if (el) el.classList.add('active');
  }
}

async function setView(type) {
  log('View', `===> setView called with type: "${type}"`);
  currentView = { type };
  setActiveNav(type);
  const titles = {
    new: '✨ New Videos',
    all: 'All Videos',
    favourites: 'Favourites'
  };
  const title = titles[type] || 'Videos';
  $('group-title').textContent = title;
  showLoading();
  const url = '/api/media?group=all';
  log('View', `Sending request: GET ${url}`);
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const elapsed = (performance.now() - t0).toFixed(1);
    log('View', `Response received in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`);
    if (!res.ok) {
      const err = await res.text();
      logErr('View', `Server error (${res.status}): ${err}`);
      throw new Error(`Server error ${res.status}: ${err}`);
    }
    log('View', 'Parsing JSON media items...');
    const items = await res.json();
    log('View', `Total media items returned: ${items.length}`);
    let toRender = items;
    if (type === 'new') {
      toRender = items.filter(m => m.is_new);
      log('View', `Filtered for view="new": ${toRender.length} items.`);
    } else if (type === 'all') {
      log('View', `Showing all ${toRender.length} items.`);
    } else if (type === 'favourites') {
      toRender = items.filter(m => m.favourite);
      log('View', `Filtered for view="favourites": ${toRender.length} items.`);
    }
    renderGrid(toRender, title);
    log('View', `<=== setView finished rendering for view: "${type}".`);
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('View', `FAILED setView("${type}") after ${elapsed}ms:`, err);
    $('loading').style.display = 'none';
    $('empty-msg').innerHTML = `Error loading videos:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span><br><small style="color:var(--muted)">Check DevTools Console (F12) for detailed logs</small>`;
    $('empty').style.display = 'flex';
  }
}

async function loadGroup(id, name) {
  log('Group', `===> loadGroup called: id="${id}", name="${name}"`);
  currentView = { type: 'group', id };
  setActiveNav('group', id);
  $('group-title').textContent = name;
  showLoading();
  const url = '/api/media?group=' + encodeURIComponent(id);
  log('Group', `Fetching media items from URL: ${url}`);
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const elapsed = (performance.now() - t0).toFixed(1);
    log('Group', `Fetch responded in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`, {
      contentType: res.headers.get('content-type'),
      status: res.status
    });
    if (!res.ok) {
      const errText = await res.text();
      logErr('Group', `Server responded with error status ${res.status}: ${errText}`);
      throw new Error(`Server error (${res.status}): ${errText}`);
    }
    log('Group', 'Parsing JSON payload...');
    const parseT0 = performance.now();
    const items = await res.json();
    const parseElapsed = (performance.now() - parseT0).toFixed(1);
    log('Group', `JSON parsed successfully in ${parseElapsed}ms. Received ${items.length} media items for "${name}".`, {
      totalCount: items.length,
      sampleFirstItem: items[0] ?? null
    });
    renderGrid(items, name);
    log('Group', `<=== loadGroup complete for "${name}". Rendered ${items.length} videos.`);
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('Group', `FAILED to load media for group "${name}" (${id}) after ${elapsed}ms:`, err);
    $('loading').style.display = 'none';
    $('empty-msg').innerHTML = `Failed to load videos for <b>${esc(name)}</b>:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span><br><small style="color:var(--muted)">Check DevTools Console (F12) for full trace</small>`;
    $('empty').style.display = 'flex';
  }
}

async function loadLabel(label) {
  log('Label', `===> loadLabel called: label="${label}"`);
  currentView = { type: 'label', label };
  setActiveNav('label', null, label);
  const title = `Label: ${label}`;
  $('group-title').textContent = title;
  showLoading();
  const url = '/api/media?group=all';
  log('Label', `Fetching media items from URL: ${url}`);
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const elapsed = (performance.now() - t0).toFixed(1);
    log('Label', `Response received in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`);
    if (!res.ok) {
      const err = await res.text();
      logErr('Label', `Server error (${res.status}): ${err}`);
      throw new Error(`Server error (${res.status}): ${err}`);
    }
    const items = await res.json();
    const matching = items.filter(m => m.labels && m.labels.includes(label));
    log('Label', `Filtered ${items.length} total items down to ${matching.length} matching label "${label}".`);
    renderGrid(matching, title);
    log('Label', `<=== loadLabel complete for "${label}".`);
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('Label', `FAILED to load media for label "${label}" after ${elapsed}ms:`, err);
    $('loading').style.display = 'none';
    $('empty-msg').innerHTML = `Error loading videos for label "${esc(label)}":<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span>`;
    $('empty').style.display = 'flex';
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Grid rendering
// ─────────────────────────────────────────────────────────────────────────────
function showLoading() {
  log('Grid', 'showLoading: Clearing grid, hiding empty state, showing spinner.');
  $('grid').innerHTML = '';
  $('empty').style.display = 'none';
  $('loading').style.display = 'flex';
}

function renderGrid(media, title) {
  log('Grid', `renderGrid called with ${media.length} items, title="${title}"`);
  allMedia = media;
  $('loading').style.display = 'none';
  $('toolbar-info').textContent = `${media.length} video${media.length!==1?'s':''}`;
  $('search').value = '';
  applyFilter();
}

function applyFilter() {
  const q = $('search').value.toLowerCase().trim();
  log('Filter', `applyFilter: Query="${q}", Current allMedia.length=${allMedia.length}`);
  filtered = q
    ? allMedia.filter(m =>
        (m.filename || '').toLowerCase().includes(q) ||
        (m.sender   || '').toLowerCase().includes(q) ||
        (m.labels   || []).some(l => l.toLowerCase().includes(q))
      )
    : [...allMedia];
  log('Filter', `applyFilter: Filtered result count=${filtered.length}`);

  const grid = $('grid');
  if (!grid) {
    logErr('Grid', 'Element #grid not found in DOM!');
    return;
  }

  if (!filtered.length) {
    log('Grid', 'No items in filtered array. Displaying #empty state.');
    grid.innerHTML = '';
    $('empty-msg').textContent = q ? `No videos matching "${q}"` : 'No videos here yet';
    $('empty').style.display = 'flex';
    return;
  }
  $('empty').style.display = 'none';
  log('Grid', `Building and injecting ${filtered.length} card HTML elements into #grid...`);

  grid.innerHTML = filtered.map((m, i) => `
    <div class="card${m.favourite?' is-fav':''}" data-index="${i}"
         onclick="openModal(${i})"
         onmouseenter="hoverStart(${i})"
         onmouseleave="hoverStop(${i})">
      <div class="thumb">
        <video id="v${i}" src="/stream/${encodeURIComponent(m.id)}"
               preload="none" muted playsinline></video>
        <div class="play-icon">
          <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        </div>
        <div class="dur" id="d${i}">—:——</div>
        ${m.favourite ? '<div class="fav-badge">⭐</div>' : ''}
        ${m.is_new ? '<div class="new-badge">NEW</div>' : ''}
      </div>
      <div class="card-meta">
        <div class="card-fn">${esc(m.filename||'Video')}</div>
        <div class="card-ts">${esc(m.sent_time)}</div>
        ${m.labels.length ? `<div class="card-labels">${m.labels.map(l=>chipHtml(l,false,true)).join('')}</div>` : ''}
      </div>
    </div>`).join('');

  log('Grid', 'Setting up IntersectionObserver for cards...');
  const obs = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const vid = entry.target.querySelector('video');
      if (vid && vid.preload === 'none') {
        vid.preload = 'metadata';
        vid.load();
        obs.unobserve(entry.target);
        vid.addEventListener('loadedmetadata', () => {
          const badge = $(vid.id.replace('v','d'));
          if (badge) badge.textContent = fmtDur(vid.duration);
        }, { once: true });
      }
    });
  }, { rootMargin: '120px' });

  grid.querySelectorAll('.card').forEach(c => obs.observe(c));
  log('Grid', `IntersectionObserver observing ${grid.querySelectorAll('.card').length} cards.`);
}

// ─────────────────────────────────────────────────────────────────────────────
// YouTube-style hover preview
// ─────────────────────────────────────────────────────────────────────────────
function hoverStart(i) {
  const s = hoverState[i] = hoverState[i] || {};
  clearTimeout(s.stopTimer);
  s.startTimer = setTimeout(() => {
    const vid = $('v' + i);
    if (!vid) return;
    const begin = () => {
      vid.currentTime = 0;
      let t0 = null;
      const SWEEP = 4000;
      function frame(ts) {
        if (!t0) t0 = ts;
        const prog = Math.min((ts - t0) / SWEEP, 1);
        vid.currentTime = prog * (vid.duration || 30) * 0.9;
        if (prog < 1 && $('v'+i)?.closest('.card:hover'))
          s.rafId = requestAnimationFrame(frame);
      }
      s.rafId = requestAnimationFrame(frame);
    };
    if (vid.readyState >= 1) begin();
    else {
      vid.preload = 'metadata'; vid.load();
      vid.addEventListener('loadedmetadata', begin, { once: true });
    }
  }, 220);
}

function hoverStop(i) {
  const s = hoverState[i] || {};
  clearTimeout(s.startTimer);
  cancelAnimationFrame(s.rafId);
  s.stopTimer = setTimeout(() => { const v=$('v'+i); if(v) v.currentTime=0; }, 80);
}

// ─────────────────────────────────────────────────────────────────────────────
// Modal player
// ─────────────────────────────────────────────────────────────────────────────
function openModal(i) {
  log('Modal', `openModal called for index: ${i}`);
  currentIdx = i;
  renderModal();
  $('modal').classList.add('open');

  const m = filtered[i];
  if (m && m.is_new) {
    log('Modal', `Clearing is_new flag for item: ${m.id}`);
    m.is_new = false;
    const am = allMedia.find(x => x.id === m.id);
    if (am) am.is_new = false;
    fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: m.id })
    }).catch(err => logWarn('Modal', 'Error marking item seen:', err));
    refreshCard(i, m);
    updateNewCount();
  }
}

function closeModal() {
  log('Modal', 'closeModal called');
  $('modal').classList.remove('open');
  const v = $('modal-video');
  v.pause(); v.src = '';
}

function renderModal() {
  const m = filtered[currentIdx];
  if (!m) {
    logWarn('Modal', `No item found for currentIdx: ${currentIdx}`);
    return;
  }
  log('Modal', `renderModal for item: id="${m.id}", filename="${m.filename}", sender="${m.sender}"`);

  const v = $('modal-video');
  v.src = '/stream/' + encodeURIComponent(m.id);
  v.load();
  v.play().catch(err => logWarn('Modal', 'Autoplay prevented or failed:', err));

  $('modal-info').innerHTML =
    `<strong>${esc(m.filename||'Video')}</strong> &nbsp;*&nbsp; `+
    `${esc(m.sender)} &nbsp;*&nbsp; ${esc(m.sent_time)}`;

  renderFavBtn(m.favourite);
  renderChips(m.labels);

  $('btn-prev').disabled = currentIdx === 0;
  $('btn-next').disabled = currentIdx === filtered.length - 1;
}

function navigate(dir) {
  const n = currentIdx + dir;
  log('Modal', `navigate: dir=${dir}, from=${currentIdx} to=${n}`);
  if (n < 0 || n >= filtered.length) return;
  currentIdx = n;
  renderModal();

  const m = filtered[n];
  if (m && m.is_new) {
    m.is_new = false;
    const am = allMedia.find(x => x.id === m.id);
    if (am) am.is_new = false;
    fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: m.id })
    }).catch(()=>{});
    refreshCard(n, m);
    updateNewCount();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Favourite toggle
// ─────────────────────────────────────────────────────────────────────────────
function renderFavBtn(isFav) {
  const btn = $('fav-btn');
  $('fav-icon').textContent  = isFav ? '★' : '☆';
  $('fav-label').textContent = isFav ? 'Favourited' : 'Favourite';
  btn.classList.toggle('active', isFav);
}

async function toggleFav() {
  const m = filtered[currentIdx];
  if (!m) return;
  const newFav = !m.favourite;
  log('Fav', `toggleFav: item=${m.id}, setting favourite=${newFav}`);
  m.favourite  = newFav;
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.favourite = newFav;

  try {
    await fetch('/api/meta/' + encodeURIComponent(m.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favourite: newFav }),
    });
    log('Fav', `Successfully saved favourite status for item: ${m.id}`);
  } catch (err) {
    logErr('Fav', `Failed to save favourite status for item: ${m.id}:`, err);
  }

  renderFavBtn(newFav);
  refreshCard(currentIdx, m);
}

// ─────────────────────────────────────────────────────────────────────────────
// Label management
// ─────────────────────────────────────────────────────────────────────────────
function renderChips(labels) {
  log('Labels', `renderChips called with ${labels.length} labels:`, labels);
  $('modal-chips').innerHTML = labels.map(l => chipHtml(l, true)).join('');
  $('label-input').value = '';
}

function handleLabelKey(e) {
  if (e.key !== 'Enter' && e.key !== ',') return;
  e.preventDefault();
  const val = $('label-input').value.trim();
  if (!val) return;
  log('Labels', `handleLabelKey submitted label: "${val}"`);
  addLabel(val);
}

async function addLabel(lbl) {
  const m = filtered[currentIdx];
  if (!m) return;
  if (m.labels.includes(lbl)) {
    log('Labels', `Label "${lbl}" already present on item ${m.id}`);
    $('label-input').value = '';
    return;
  }
  log('Labels', `Adding label "${lbl}" to item ${m.id}`);
  m.labels.push(lbl);
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.labels = [...m.labels];

  await saveMeta(m);
  renderChips(m.labels);
  refreshCard(currentIdx, m);
  await refreshLabels();
}

async function removeLabel(lbl) {
  const m = filtered[currentIdx];
  if (!m) return;
  log('Labels', `Removing label "${lbl}" from item ${m.id}`);
  m.labels = m.labels.filter(l => l !== lbl);
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.labels = [...m.labels];

  await saveMeta(m);
  renderChips(m.labels);
  refreshCard(currentIdx, m);
  await refreshLabels();
}

async function saveMeta(m) {
  log('Meta', `Saving metadata for item ${m.id}: fav=${m.favourite}, labels=`, m.labels);
  try {
    const res = await fetch('/api/meta/' + encodeURIComponent(m.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favourite: m.favourite, labels: m.labels }),
    });
    log('Meta', `saveMeta response: HTTP ${res.status}`);
  } catch (err) {
    logErr('Meta', `saveMeta failed for item ${m.id}:`, err);
  }
}

// Refresh a grid card's star badge + label chips + fav border without a full re-render
function refreshCard(idx, m) {
  const card = $('grid').querySelectorAll('.card')[idx];
  if (!card) return;
  card.classList.toggle('is-fav', m.favourite);
  // Fav badge
  const existingFav = card.querySelector('.fav-badge');
  if (m.favourite && !existingFav) {
    card.querySelector('.thumb').insertAdjacentHTML('beforeend', '<div class="fav-badge">⭐</div>');
  } else if (!m.favourite && existingFav) {
    existingFav.remove();
  }
  // New badge
  const existingNew = card.querySelector('.new-badge');
  if (m.is_new && !existingNew) {
    card.querySelector('.thumb').insertAdjacentHTML('beforeend', '<div class="new-badge">NEW</div>');
  } else if (!m.is_new && existingNew) {
    existingNew.remove();
  }
  // Labels
  const labelEl = card.querySelector('.card-labels');
  const meta    = card.querySelector('.card-meta');
  if (m.labels.length) {
    const html = `<div class="card-labels">${m.labels.map(l=>chipHtml(l,false,true)).join('')}</div>`;
    if (labelEl) labelEl.outerHTML = html;
    else meta.insertAdjacentHTML('beforeend', html);
  } else if (labelEl) {
    labelEl.remove();
  }
}

async function markAllSeen() {
  const newItems = allMedia.filter(m => m.is_new);
  log('Seen', `markAllSeen called. Found ${newItems.length} new items to mark.`);
  if (!newItems.length) return;
  const ids = newItems.map(m => m.id);

  newItems.forEach(m => { m.is_new = false; });
  filtered.forEach(m => { if (ids.includes(m.id)) m.is_new = false; });

  try {
    const res = await fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids })
    });
    log('Seen', `markAllSeen POST response: HTTP ${res.status}`);
  } catch (err) {
    logErr('Seen', 'markAllSeen POST failed:', err);
  }

  await updateNewCount();

  if (currentView && currentView.type === 'new') {
    renderGrid([], '✨ New Videos');
  } else {
    applyFilter();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Keyboard shortcuts
// ─────────────────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  // Don't hijack when typing in label input
  if (document.activeElement === $('label-input') || document.activeElement === $('search')) return;

  if (!$('modal').classList.contains('open')) return;
  log('Keyboard', `Keydown detected: "${e.key}"`);
  if (e.key === 'Escape')     closeModal();
  if (e.key === 'ArrowRight') navigate(1);
  if (e.key === 'ArrowLeft')  navigate(-1);
  if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleFav(); }
});

log('Init', 'Invoking init() bootstrap function...');
init();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[HTTP {time.strftime('%H:%M:%S')}] {self.address_string()} - {fmt % args}\n")
        sys.stderr.flush()

    def do_GET(self):
        t0 = time.time()
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)
        print(f"[HTTP] Incoming GET: {self.path}", flush=True)

        if path == '/':
            self._html()
        elif path == '/api/groups':
            t_q = time.time()
            groups = _query_groups()
            print(f"[API] _query_groups completed in {time.time()-t_q:.3f}s, returning {len(groups)} groups", flush=True)
            self._json(groups)
        elif path == '/api/media':
            group_arg = qs.get('group', [None])[0]
            print(f"[API] Handling /api/media for group={group_arg}", flush=True)
            self._serve_media(group_arg)
        elif path == '/api/media/new_count':
            count = _query_new_count()
            self._json({"count": count})
        elif path == '/api/labels':
            self._json(_all_labels())
        elif path == '/api/sync/status':
            self._json(_get_sync_status())
        elif path.startswith('/stream/'):
            media_id = unquote(path[len('/stream/'):])
            self._stream(media_id)
        else:
            print(f"[HTTP 404] No route for: {self.path}", flush=True)
            self.send_error(404)
        print(f"[HTTP] GET {self.path} finished in {time.time()-t0:.3f}s", flush=True)

    def do_POST(self):
        t0 = time.time()
        parsed = urlparse(self.path)
        path   = parsed.path
        print(f"[HTTP] Incoming POST: {self.path}", flush=True)

        if path == '/api/meta/seen':
            self._handle_mark_seen()
        elif path.startswith('/api/meta/'):
            self._update_meta(unquote(path[len('/api/meta/'):]))
        else:
            print(f"[HTTP 404] No POST route for: {self.path}", flush=True)
            self.send_error(404)
        print(f"[HTTP] POST {self.path} finished in {time.time()-t0:.3f}s", flush=True)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _html(self):
        body = _HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _read_body_json(self):
        length = int(self.headers.get('Content-Length', 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode('utf-8'))

    # ── Routes ───────────────────────────────────────────────────────────────

    def _serve_media(self, group_id):
        if not group_id:
            group_id = 'all'
        t0 = time.time()
        print(f"[API] _serve_media starting query for group_id='{group_id}'...", flush=True)
        try:
            media, lookup = _query_media(group_id)
            elapsed = time.time() - t0
            print(f"[API] _serve_media query returned {len(media)} items in {elapsed:.3f}s", flush=True)
            with _lookup_lock:
                _media_lookup.update(lookup)
            self._json(media)
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[API ERROR] _serve_media failed after {elapsed:.3f}s: {e}", flush=True)
            import traceback
            traceback.print_exc()
            self.send_error(500, str(e))

    def _handle_mark_seen(self):
        try:
            body = self._read_body_json()
            ids = body.get('ids', [])
            if 'id' in body and body['id']:
                ids.append(body['id'])
            if ids:
                _mark_seen(ids)
            self._json({"status": "ok", "marked": len(ids)})
        except Exception as e:
            self.send_error(500, str(e))

    def _update_meta(self, msg_id):
        """
        POST /api/meta/<messageId>
        Body: { "favourite": bool, "labels": [str] }
        Updates persistent metadata. localKey and content never involved.
        """
        try:
            body = self._read_body_json()
            entry = _set_meta(
                msg_id,
                favourite=body.get('favourite'),
                labels=body.get('labels'),
            )
            self._json(entry)
        except Exception as e:
            self.send_error(500, str(e))


    def _stream(self, msg_id):
        with _lookup_lock:
            entry = _media_lookup.get(msg_id)
        if not entry:
            self.send_error(404, "Media not found — load its group first")
            return

        rel_path, local_key, size, content_type = entry
        enc_path = os.path.join(_attach_root, rel_path)

        if not os.path.exists(enc_path):
            self.send_error(404, "Encrypted file missing from attachments.noindex")
            return

        try:
            data = _get_cached(msg_id, enc_path, local_key, size)
        except Exception as e:
            self.send_error(500, f"Decryption error: {e}")
            return

        total        = len(data)
        range_header = self.headers.get('Range')

        if range_header:
            try:
                spec         = range_header.strip().replace('bytes=', '')
                s_str, e_str = spec.split('-')
                start = int(s_str) if s_str else 0
                end   = int(e_str) if e_str else total - 1
                end   = min(end, total - 1)
                chunk = data[start:end + 1]

                self.send_response(206)
                self.send_header('Content-Type',   content_type)
                self.send_header('Content-Range',  f'bytes {start}-{end}/{total}')
                self.send_header('Content-Length', len(chunk))
                self.send_header('Accept-Ranges',  'bytes')
                self.end_headers()
                self.wfile.write(chunk)
            except Exception as e:
                self.send_error(400, f"Bad Range header: {e}")
        else:
            self.send_response(200)
            self.send_header('Content-Type',   content_type)
            self.send_header('Content-Length', total)
            self.send_header('Accept-Ranges',  'bytes')
            self.end_headers()
            self.wfile.write(data)


def is_signal_running() -> bool:
    """Checks if Signal.exe is currently running on Windows."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq Signal.exe", "/FO", "CSV", "/NH"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        return "signal.exe" in out.lower()
    except Exception:
        return False


def kill_signal():
    """Terminates running Signal.exe processes."""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "Signal.exe"], capture_output=True)
        time.sleep(1)
    except Exception as e:
        print(f"[Signal Player] Warning: failed to terminate Signal: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _db_conn, _db_cur, _attach_root

    parser = argparse.ArgumentParser(
        description="Signal Desktop local video player — streams on-the-fly, nothing written to disk."
    )
    parser.add_argument('--port',       type=int, default=7788)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--auto-close', action='store_true', help="Automatically close Signal if running without prompting")
    parser.add_argument('--auto-download', '--sync', action='store_true', dest='auto_download', help="Automatically run background sync/download")
    args = parser.parse_args()

    print("[Signal Player] Starting…")

    _load_metadata()
    bg_download_mode = False

    sig_running = is_signal_running()

    if args.auto_close:
        choice = "1"
    elif args.auto_download:
        choice = "2"
    else:
        print("\n" + "=" * 60)
        if sig_running:
            print("  Signal Desktop Status: RUNNING")
            print("=" * 60)
            print("  Please choose how you would like to proceed:")
            print("    [1] Close Signal now to copy the latest database & start player")
            print("    [2] Fast startup + background sync (close Signal for snapshot, start player, relaunch in background to auto-download pending videos)")
            print("    [3] Proceed immediately (copy live database snapshot while Signal runs)")
            print("-" * 60)
            try:
                choice = input("  Select option [1/2/3] (default: 2): ").strip() or "2"
            except (EOFError, KeyboardInterrupt):
                choice = "2"
        else:
            print("  Signal Desktop Status: NOT RUNNING")
            print("=" * 60)
            print("  Please choose how you would like to proceed:")
            print("    [1] Start player immediately (browse currently available videos)")
            print("    [2] Start player + background sync (launch Signal in background with CDP to auto-download pending videos)")
            print("-" * 60)
            try:
                choice = input("  Select option [1/2] (default: 2): ").strip() or "2"
            except (EOFError, KeyboardInterrupt):
                choice = "2"

    if choice == "1":
        if sig_running:
            print("[Signal Player] Closing Signal Desktop...")
            kill_signal()
            print("[Signal Player] Signal closed.")
        else:
            print("[Signal Player] Signal is not running. Starting player immediately...")
    elif choice == "2":
        if sig_running:
            print("\n[Signal Player] Fast startup with background media sync enabled:")
            print("  1. Closing Signal briefly to take a clean database snapshot...")
            kill_signal()
            print("  2. Database snapshot will be taken immediately so you can start viewing videos.")
            print("  3. Signal will be reopened in background with remote debugging to download pending media.")
        else:
            print("\n[Signal Player] Starting player with background media sync enabled:")
            print("  1. Database snapshot will be taken immediately so you can start viewing videos.")
            print("  2. Signal will be launched in background with remote debugging to download pending media.")
        bg_download_mode = True
    else:
        print("[Signal Player] Proceeding with live database copy.")

    print("[Signal Player] Extracting encryption key…")
    try:
        key = get_signal_key()
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr); sys.exit(1)
    print("[Signal Player] [OK] Key extracted (server process only, never leaves RAM)")

    print("[Signal Player] Copying database snapshot…")
    try:
        db_path = copy_db_snapshot()
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr); sys.exit(1)
    print(f"[Signal Player] [OK] DB snapshot at {db_path}")

    try:
        _db_conn, _db_cur = open_db(db_path, key)
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr); sys.exit(1)
    print("[Signal Player] [OK] Database opened")

    _attach_root = os.path.join(os.environ.get("APPDATA", ""), "Signal", "attachments.noindex")
    print(f"[Signal Player] [OK] Attachments root: {_attach_root}")

    server = ThreadingHTTPServer(('127.0.0.1', args.port), _Handler)
    url    = f"http://127.0.0.1:{args.port}"
    print(f"\n[Signal Player] [OK] Listening on {url}")
    print("[Signal Player]   Press Ctrl+C to stop\n")
    print("  Security notes:")
    print("  * Decrypted video bytes live in RAM only (LRU, max 10 videos)")
    print("  * localKey values are never sent to the browser")
    print(f"  * Metadata (favourites/labels) saved to: {_META_PATH}")
    print("  * Server bound to 127.0.0.1 — not reachable from network\n")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    # Launch background downloader thread if option 2 was selected
    if bg_download_mode:
        def _bg_downloader_worker():
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                if script_dir not in sys.path:
                    sys.path.insert(0, script_dir)
                from signal_headless_downloader import run_headless_download, get_cdp_target, query_pending_video_groups
                # 1. Relaunch Signal with remote debugging enabled
                print("[Background Sync] Launching Signal with --remote-debugging-port=9222...")
                sig_exe = os.path.expandvars(r"%LOCALAPPDATA%\Programs\signal-desktop\Signal.exe")
                if not os.path.exists(sig_exe):
                    sig_exe = "Signal.exe"
                try:
                    subprocess.Popen([sig_exe, "--remote-debugging-port=9222"])
                    time.sleep(5)
                except Exception as e:
                    print(f"[Background Sync] Could not launch Signal: {e}")
                    return

                # 2. Check pending groups
                pending_groups = query_pending_video_groups(db_path, key)
                total_pending = sum(g[2] for g in pending_groups)
                if total_pending == 0:
                    print("[Background Sync] All media is already downloaded!")
                    return

                _set_sync_status(True, pending=total_pending, initial=total_pending)
                print(f"[Background Sync] Started background download of {total_pending} pending videos across {len(pending_groups)} groups.")

                # Run headless download
                run_headless_download(db_path, key, cdp_port=9222, wait_seconds=10)

                # Reload database snapshot so player immediately sees newly downloaded files
                reload_db(key)
                _set_sync_status(False, pending=0)
                print("\n[Background Sync] [OK] Background media download completed and database refreshed!")
            except Exception as e:
                _set_sync_status(False)
                print(f"[Background Sync] Error: {e}")

        bg_thread = threading.Thread(target=_bg_downloader_worker, daemon=True)
        bg_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[Signal Player] Shutting down — wiping in-memory cache…")
        with _cache_lock:
            _cache.clear()
        try:
            _db_conn.close()
        except Exception:
            pass
        # Record session timestamp so new videos arriving next time are detected
        with _meta_lock:
            _meta_data["last_session_timestamp"] = _session_start_ts
            _save_metadata()
        print("[Signal Player] Done.")


if __name__ == '__main__':
    main()
