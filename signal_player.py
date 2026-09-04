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
    dst_dir = os.path.join(os.environ.get("TEMP", "."), "signal-player-work")
    os.makedirs(dst_dir, exist_ok=True)
    db_src  = os.path.join(src, "db.sqlite")
    wal_src = os.path.join(src, "db.sqlite-wal")
    db_dst  = os.path.join(dst_dir, "db.sqlite")
    if not os.path.exists(db_src):
        raise FileNotFoundError(f"Signal DB not found: {db_src}")
    shutil.copy2(db_src, db_dst)
    if os.path.exists(wal_src):
        shutil.copy2(wal_src, os.path.join(dst_dir, "db.sqlite-wal"))
    return db_dst


def open_db(db_path: str, key: str):
    conn = sqlcipher3.connect(db_path, check_same_thread=False)
    cur  = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{key}'\";")
    cur.execute("PRAGMA cipher_compatibility = 4;")
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
        print(f"[Signal Player] Warning: reload_db failed: {e}")
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


def _get_meta(msg_id: str, sent_at_ms: int = 0) -> dict:
    with _meta_lock:
        ann = _meta_data["annotations"].get(msg_id, {"favourite": False, "labels": []})
        last_ts = _meta_data.get("last_session_timestamp", 0)
        seen_set = set(_meta_data.get("seen_message_ids", []))
        is_new = bool(last_ts > 0 and sent_at_ms > last_ts and msg_id not in seen_set)
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
                COUNT(ma.messageId) AS video_count
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


def _query_media(group_id: str):
    with _db_lock:
        _db_cur.execute("""
            SELECT
                ma.messageId,
                DATETIME(ma.sentAt / 1000, 'unixepoch', 'localtime') AS sent_time,
                COALESCE(src.name, src.profileName, src.e164, 'Unknown') AS sender,
                ma.contentType,
                ma.size,
                ma.fileName,
                ma.path,
                ma.localKey,
                COALESCE(ma.sentAt, 0) AS sent_at_ms
            FROM message_attachments ma
            JOIN messages m ON m.id = ma.messageId
            JOIN conversations c ON c.id = m.conversationId
            LEFT JOIN conversations src
                ON src.id = m.sourceServiceId OR src.e164 = m.source
            WHERE c.id = ?
              AND ma.contentType LIKE 'video/%'
              AND ma.path IS NOT NULL
              AND ma.localKey IS NOT NULL
            ORDER BY ma.sentAt ASC;
        """, (group_id,))
        rows = _db_cur.fetchall()

    media = []
    for r in rows:
        sent_at_ms = int(r[8]) if r[8] else 0
        meta = _get_meta(r[0], sent_at_ms=sent_at_ms)
        media.append({
            "id":           r[0],
            "sent_time":    r[1] or "",
            "sender":       r[2] or "",
            "content_type": r[3] or "video/mp4",
            "size":         r[4] or 0,
            "filename":     r[5] or "",
            "favourite":    meta["favourite"],
            "labels":       meta["labels"],
            "is_new":       meta["is_new"],
        })

    server_lookup = {
        r[0]: (r[6], r[7], r[4] or 0, r[3] or "video/mp4")
        for r in rows
    }
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
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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
  const res = await fetch('/api/groups');
  groups    = await res.json();
  renderSidebar();
  await refreshLabels();
  await updateNewCount();
  startSyncPolling();
}

let lastPendingCount = -1;
function startSyncPolling() {
  setInterval(async () => {
    try {
      const res = await fetch('/api/sync/status');
      if (!res.ok) return;
      const data = await res.json();
      const banner = $('sync-banner');
      if (!banner) return;

      if (data.is_running) {
        banner.style.display = 'flex';
        const rem = data.pending_count ?? 0;
        $('sync-stats').textContent = `${rem} pending video${rem !== 1 ? 's' : ''}`;

        // If newly downloaded videos were detected, refresh UI
        if (lastPendingCount !== -1 && rem < lastPendingCount) {
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
          banner.style.display = 'none';
          const gRes = await fetch('/api/groups');
          groups = await gRes.json();
          renderSidebar();
          await updateNewCount();
        }
      }
    } catch (e) {}
  }, 4000);
}

async function updateNewCount() {
  let count = 0;
  for (const g of groups) {
    const res = await fetch('/api/media?group=' + encodeURIComponent(g.id));
    const items = await res.json();
    count += items.filter(m => m.is_new).length;
  }
  const badge = $('new-count');
  if (badge) {
    badge.textContent = count;
    badge.style.display = count > 0 ? 'inline-block' : 'none';
  }
}

function renderSidebar() {
  const list = $('group-list');
  if (!groups.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px;text-align:center">No downloaded videos</div>';
    return;
  }
  list.innerHTML = groups.map(g => `
    <div class="nav-item" data-id="${esc(g.id)}" onclick="loadGroup('${esc(g.id)}','${esc(g.name)}')">
      <div>
        <div class="group-name">${esc(g.name)}</div>
        <div class="group-count">${g.video_count} video${g.video_count!==1?'s':''} * ${esc(g.type)}</div>
      </div>
    </div>`).join('');
}

async function refreshLabels() {
  const res    = await fetch('/api/labels');
  const labels = await res.json();
  const sec    = $('label-section');
  const list   = $('label-list');
  if (!labels.length) { sec.style.display='none'; list.innerHTML=''; return; }
  sec.style.display = '';
  list.innerHTML = labels.map(lbl => `
    <div class="nav-item" data-label="${esc(lbl)}" onclick="loadLabel('${esc(lbl)}')">
      <span>${chipHtml(lbl,false,true)}</span>
    </div>`).join('');
}

// ─────────────────────────────────────────────────────────────────────────────
// View switching
// ─────────────────────────────────────────────────────────────────────────────
function setActiveNav(type, id = null, label = null) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  $('btn-mark-all').style.display = type === 'new' ? 'inline-block' : 'none';
  if (type === 'new')         $('nav-new')?.classList.add('active');
  else if (type === 'all')    $('nav-all')?.classList.add('active');
  else if (type === 'favourites') $('nav-fav')?.classList.add('active');
  else if (type === 'group' && id) {
    document.querySelector(`.nav-item[data-id="${CSS.escape(id)}"]`)?.classList.add('active');
  } else if (type === 'label' && label) {
    document.querySelector(`.nav-item[data-label="${CSS.escape(label)}"]`)?.classList.add('active');
  }
}

async function setView(type) {
  currentView = { type };
  setActiveNav(type);
  if (type === 'new') {
    $('group-title').textContent = '✨ New Videos';
    showLoading();
    const all = [];
    for (const g of groups) {
      const res   = await fetch('/api/media?group=' + encodeURIComponent(g.id));
      const items = await res.json();
      all.push(...items.filter(m => m.is_new));
    }
    renderGrid(all, '✨ New Videos');
  } else if (type === 'all') {
    $('group-title').textContent = 'All Videos';
    showLoading();
    const all = [];
    for (const g of groups) {
      const res   = await fetch('/api/media?group=' + encodeURIComponent(g.id));
      const items = await res.json();
      all.push(...items);
    }
    renderGrid(all, 'All Videos');
  } else if (type === 'favourites') {
    $('group-title').textContent = 'Favourites';
    showLoading();
    const all = [];
    for (const g of groups) {
      const res   = await fetch('/api/media?group=' + encodeURIComponent(g.id));
      const items = await res.json();
      all.push(...items.filter(m => m.favourite));
    }
    renderGrid(all, 'Favourites');
  }
}


async function loadGroup(id, name) {
  currentView = { type: 'group', id };
  setActiveNav('group', id);
  $('group-title').textContent = name;
  showLoading();
  const res   = await fetch('/api/media?group=' + encodeURIComponent(id));
  const items = await res.json();
  renderGrid(items, name);
}

async function loadLabel(label) {
  currentView = { type: 'label', label };
  setActiveNav('label', null, label);
  $('group-title').textContent = `Label: ${label}`;
  showLoading();
  const all = [];
  for (const g of groups) {
    const res   = await fetch('/api/media?group=' + encodeURIComponent(g.id));
    const items = await res.json();
    all.push(...items.filter(m => m.labels.includes(label)));
  }
  renderGrid(all, `Label: ${label}`);
}

// ─────────────────────────────────────────────────────────────────────────────
// Grid rendering
// ─────────────────────────────────────────────────────────────────────────────
function showLoading() {
  $('grid').innerHTML = '';
  $('empty').style.display = 'none';
  $('loading').style.display = 'flex';
}

function renderGrid(media, title) {
  allMedia = media;
  $('loading').style.display = 'none';
  $('toolbar-info').textContent = `${media.length} video${media.length!==1?'s':''}`;
  $('search').value = '';
  applyFilter();
}

function applyFilter() {
  const q = $('search').value.toLowerCase().trim();
  filtered = q
    ? allMedia.filter(m =>
        (m.filename || '').toLowerCase().includes(q) ||
        (m.sender   || '').toLowerCase().includes(q) ||
        (m.labels   || []).some(l => l.toLowerCase().includes(q))
      )
    : [...allMedia];

  const grid = $('grid');

  if (!filtered.length) {
    grid.innerHTML = '';
    $('empty-msg').textContent = q ? `No videos matching "${q}"` : 'No videos here yet';
    $('empty').style.display = 'flex';
    return;
  }
  $('empty').style.display = 'none';

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

  // Lazy metadata loading via IntersectionObserver
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
  currentIdx = i;
  renderModal();
  $('modal').classList.add('open');

  const m = filtered[i];
  if (m && m.is_new) {
    m.is_new = false;
    const am = allMedia.find(x => x.id === m.id);
    if (am) am.is_new = false;
    fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: m.id })
    }).catch(()=>{});
    refreshCard(i, m);
    updateNewCount();
  }
}

function closeModal() {
  $('modal').classList.remove('open');
  const v = $('modal-video');
  v.pause(); v.src = '';
}

function renderModal() {
  const m = filtered[currentIdx];
  if (!m) return;

  const v = $('modal-video');
  v.src = '/stream/' + encodeURIComponent(m.id);
  v.load(); v.play().catch(()=>{});

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
  m.favourite  = newFav;
  // Also update in allMedia
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.favourite = newFav;

  await fetch('/api/meta/' + encodeURIComponent(m.id), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ favourite: newFav }),
  });

  renderFavBtn(newFav);
  // Refresh card in grid (star badge + border)
  refreshCard(currentIdx, m);
}

// ─────────────────────────────────────────────────────────────────────────────
// Label management
// ─────────────────────────────────────────────────────────────────────────────
function renderChips(labels) {
  $('modal-chips').innerHTML = labels.map(l => chipHtml(l, true)).join('');
  $('label-input').value = '';
}

function handleLabelKey(e) {
  if (e.key !== 'Enter' && e.key !== ',') return;
  e.preventDefault();
  const val = $('label-input').value.trim();
  if (!val) return;
  addLabel(val);
}

async function addLabel(lbl) {
  const m = filtered[currentIdx];
  if (!m) return;
  if (m.labels.includes(lbl)) { $('label-input').value=''; return; }
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
  m.labels = m.labels.filter(l => l !== lbl);
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.labels = [...m.labels];

  await saveMeta(m);
  renderChips(m.labels);
  refreshCard(currentIdx, m);
  await refreshLabels();
}

async function saveMeta(m) {
  await fetch('/api/meta/' + encodeURIComponent(m.id), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ favourite: m.favourite, labels: m.labels }),
  });
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
  if (!newItems.length) return;
  const ids = newItems.map(m => m.id);

  newItems.forEach(m => { m.is_new = false; });
  filtered.forEach(m => { if (ids.includes(m.id)) m.is_new = false; });

  await fetch('/api/meta/seen', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids })
  });

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
  if (e.key === 'Escape')     closeModal();
  if (e.key === 'ArrowRight') navigate(1);
  if (e.key === 'ArrowLeft')  navigate(-1);
  if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleFav(); }
});

init();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # Suppress routine access logs

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == '/':
            self._html()
        elif path == '/api/groups':
            self._json(_query_groups())
        elif path == '/api/media':
            self._serve_media(qs.get('group', [None])[0])
        elif path == '/api/labels':
            self._json(_all_labels())
        elif path == '/api/sync/status':
            self._json(_get_sync_status())
        elif path.startswith('/stream/'):
            self._stream(unquote(path[len('/stream/'):]))
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == '/api/meta/seen':
            self._handle_mark_seen()
        elif path.startswith('/api/meta/'):
            self._update_meta(unquote(path[len('/api/meta/'):]))
        else:
            self.send_error(404)

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
            self.send_error(400, "Missing ?group= parameter")
            return
        try:
            media, lookup = _query_media(group_id)
            with _lookup_lock:
                _media_lookup.update(lookup)
            self._json(media)
        except Exception as e:
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
    parser.add_argument('--auto-download', action='store_true', help="Automatically run headless download via CDP if Signal is running")
    args = parser.parse_args()

    print("[Signal Player] Starting…")

    _load_metadata()
    bg_download_mode = False

    # Process Lifecycle Check
    if is_signal_running():
        print("\n" + "=" * 60)
        print("  [!] Signal Desktop is currently running.")
        print("=" * 60)
        if args.auto_close:
            choice = "1"
        elif args.auto_download:
            choice = "2"
        else:
            print("  Please choose how you would like to proceed:")
            print("    [1] Close Signal now to copy the latest database & start player")
            print("    [2] Perform action on Signal: iterate over groups & auto-download pending videos")
            print("    [3] Proceed immediately (copy live database snapshot while Signal runs)")
            print("-" * 60)
            try:
                choice = input("  Select option [1/2/3] (default: 1): ").strip() or "1"
            except (EOFError, KeyboardInterrupt):
                choice = "1"

        if choice == "1":
            print("[Signal Player] Closing Signal Desktop...")
            kill_signal()
            print("[Signal Player] Signal closed.")
        elif choice == "2":
            print("\n[Signal Player] Fast startup with background media sync enabled:")
            print("  1. Closing Signal briefly to take a clean database snapshot...")
            kill_signal()
            print("  2. Database snapshot will be taken immediately so you can start viewing videos.")
            print("  3. Signal will be reopened in background with remote debugging to download pending media.")
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
