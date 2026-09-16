#!/usr/bin/env python3
"""player.server - Signal Desktop local video player server module.

Provides the core functions used by the player package:
- is_signal_running(): Check if Signal.exe is running
- kill_signal(): Terminate running Signal.exe processes
- main(): Entry point for the player CLI
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse
import webbrowser

from collections import OrderedDict

# Bounded global state for streaming lookup & attachment root
_LOOKUP_MAX = 1000
_media_lookup: OrderedDict = OrderedDict()
_lookup_lock = threading.Lock()


def _update_media_lookup(lookup_dict: dict):
    """Updates bounded media lookup LRU map."""
    with _lookup_lock:
        for k, v in lookup_dict.items():
            if k in _media_lookup:
                _media_lookup.move_to_end(k)
            _media_lookup[k] = v
            while len(_media_lookup) > _LOOKUP_MAX:
                _media_lookup.popitem(last=False)


def _get_media_lookup_entry(msg_id: str) -> Optional[Tuple[str, str, int, str]]:
    """Retrieves lookup entry from LRU map or queries database on demand."""
    with _lookup_lock:
        if msg_id in _media_lookup:
            _media_lookup.move_to_end(msg_id)
            return _media_lookup[msg_id]

    # On LRU miss, query database on demand
    try:
        from db.queries import _db_lock, _db_cur
        with _db_lock:
            if not _db_cur:
                return None
            _db_cur.execute("""
                SELECT ma.path, ma.localKey, ma.size, ma.contentType
                FROM message_attachments ma
                WHERE ma.path = ? OR ma.messageId = ?
                LIMIT 1;
            """, (msg_id, msg_id))
            row = _db_cur.fetchone()
            if row:
                path, local_key, size, content_type = row[0], row[1], row[2] or 0, row[3] or "video/mp4"
                entry = (path, local_key, size, content_type)
                with _lookup_lock:
                    _media_lookup[msg_id] = entry
                    while len(_media_lookup) > _LOOKUP_MAX:
                        _media_lookup.popitem(last=False)
                return entry
    except Exception:
        pass
    return None
_attach_root = ""
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def is_signal_running() -> bool:
    """Checks if Signal.exe is currently running on Windows."""
    try:
        from downloader.dispatcher import is_signal_running as _is_running
        return _is_running()
    except Exception:
        return False


def kill_signal() -> bool:
    """Terminates running Signal.exe processes safely by targeted process IDs. Returns True on successful verification."""
    try:
        from downloader.dispatcher import get_signal_pids, safely_stop_signal_processes
        pids, err = get_signal_pids()
        if err is not None:
            return False
        if not pids:
            return True
        return safely_stop_signal_processes(pids)
    except Exception as e:
        print(f"[Signal Player] Warning: failed to terminate Signal: {e}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Entry point for the Signal Desktop local video player."""
    parser = argparse.ArgumentParser(
        description="Signal Desktop local video player — streams on-the-fly, nothing written to disk."
    )
    parser.add_argument('--port',          type=int, default=7788)
    parser.add_argument('--no-browser',    action='store_true')
    parser.add_argument('--auto-close',    action='store_true', help="Automatically close Signal if running without prompting")
    parser.add_argument('--auto-download', '--sync', action='store_true', dest='auto_download', help="Automatically run background sync/download")
    parser.add_argument('--download-only', '--download-media-only', action='store_true', dest='download_only', help="Download outstanding media only without starting player")
    args = parser.parse_args()

    print("[Signal Player] Starting…")

    sig_running = is_signal_running()

    if args.download_only:
        choice = "download_only"
    elif args.auto_close:
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
            print("    [4] Download outstanding media only")
            print("-" * 60)
            try:
                choice = input("  Select option [1/2/3/4] (default: 2): ").strip() or "2"
            except (EOFError, KeyboardInterrupt):
                choice = "2"
            if choice == "4":
                choice = "download_only"
        else:
            print("  Signal Desktop Status: NOT RUNNING")
            print("=" * 60)
            print("  Please choose how you would like to proceed:")
            print("    [1] Start player immediately (browse currently available videos)")
            print("    [2] Start player + background sync (launch Signal in background with CDP to auto-download pending videos)")
            print("    [3] Download outstanding media only")
            print("-" * 60)
            try:
                choice = input("  Select option [1/2/3] (default: 2): ").strip() or "2"
            except (EOFError, KeyboardInterrupt):
                choice = "2"
            if choice == "3":
                choice = "download_only"

    if choice == "download_only":
        from downloader import run_managed_download, ItemResultStatus
        res = run_managed_download(show_progress=True)
        if res.status in (ItemResultStatus.SUCCESS, ItemResultStatus.SKIPPED):
            sys.exit(0)
        elif res.status == ItemResultStatus.CANCELLED:
            sys.exit(130)
        else:
            sys.exit(1)

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
        from crypto import get_signal_key
        key = get_signal_key()
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr)
        sys.exit(1)
    print("[Signal Player] [OK] Key extracted (server process only, never leaves RAM)")

    print("[Signal Player] Copying database snapshot…")
    try:
        from db import copy_db_snapshot
        db_path = copy_db_snapshot()
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[Signal Player] [OK] DB snapshot at {db_path}")

    try:
        from db import open_db, set_active_db
        signal_db_conn, signal_db_cur = open_db(db_path, key)
        set_active_db(signal_db_conn, signal_db_cur)
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr)
        sys.exit(1)
    print("[Signal Player] [OK] Database opened")

    global _attach_root
    _attach_root = os.path.join(os.environ.get("APPDATA", ""), "Signal", "attachments.noindex")
    print(f"[Signal Player] [OK] Attachments root: {_attach_root}")

    server = ThreadingHTTPServer(('127.0.0.1', args.port), _Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n[Signal Player] [OK] Listening on {url}")
    print("[Signal Player]   Press Ctrl+C to stop\n")
    print("  Security notes:")
    print("  * Decrypted video bytes live in RAM only (LRU, max 10 videos)")
    print("  * localKey values are never sent to the browser")
    print(f"  * Metadata (favourites/labels) saved to: signal_player_meta.json")
    print("  * Server bound to 127.0.0.1 — not reachable from network\n")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    # Launch background downloader thread if option 2 was selected
    if 'bg_download_mode' in dir() and bg_download_mode:
        def _bg_downloader_worker():
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                if script_dir not in sys.path:
                    sys.path.insert(0, script_dir)
                from downloader import run_managed_download, query_pending_video_groups, ItemResultStatus
                from metadata import _set_sync_status
                from db import reload_db

                pending_groups = query_pending_video_groups(db_path, key)
                total_pending = sum(g[2] for g in pending_groups)
                if total_pending == 0:
                    print("[Background Sync] All media is already downloaded!")
                    return

                _set_sync_status(True, pending=total_pending, initial=total_pending)
                print(f"[Background Sync] Started background download of {total_pending} pending videos across {len(pending_groups)} groups.")

                run_res = run_managed_download(db_path=db_path, key=key, cdp_port=9222, show_progress=False)

                if run_res and run_res.status in (ItemResultStatus.SUCCESS, ItemResultStatus.SKIPPED):
                    reload_db(key)
                    _set_sync_status(False, pending=0)
                    print("\n[Background Sync] [OK] Background media download completed and database refreshed!")
                else:
                    _set_sync_status(False)
                    err_msg = getattr(run_res, 'error_message', None) or 'incomplete download'
                    print(f"[Background Sync] Notice: Background media download finished with incomplete items or error: {err_msg}")
            except Exception as e:
                from metadata import _set_sync_status
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
            import db.queries as db_q
            with db_q._db_lock:
                if db_q._db_conn:
                    db_q._db_conn.close()
        except Exception:
            pass
        # Record session timestamp so new videos arriving next time are detected
        from metadata import _save_metadata
        from metadata.store import _meta_data
        with _meta_lock:
            _meta_data["last_session_timestamp"] = _session_start_ts
            _save_metadata()
        print("[Signal Player] Done.")


# ---------------------------------------------------------------------------
# HTTP Request Handler
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
            self._serve_static(os.path.join(WEB_DIR, 'index.html'), 'text/html; charset=utf-8')
        elif path == '/style.css':
            self._serve_static(os.path.join(WEB_DIR, 'style.css'), 'text/css; charset=utf-8')
        elif path == '/app.js':
            self._serve_static(os.path.join(WEB_DIR, 'app.js'), 'application/javascript; charset=utf-8')
        elif path == '/api/groups':
            t_q = time.time()
            from db import _query_groups
            groups = _query_groups()
            print(f"[API] _query_groups executed in {time.time()-t_q:.3f}s, returning {len(groups)} groups", flush=True)
            self._json(groups)
        elif path == '/api/media':
            group_arg = qs.get('group', [None])[0]
            limit_arg = qs.get('limit', ['50'])[0]
            cursor_arg = qs.get('cursor', [None])[0]
            view_arg = qs.get('view', [None])[0]
            label_arg = qs.get('label', [None])[0]
            search_arg = qs.get('search', [None])[0]
            print(f"[API] Handling /api/media for group={group_arg}, limit={limit_arg}, cursor={cursor_arg}", flush=True)
            self._serve_media(
                group_id=group_arg,
                limit=limit_arg,
                cursor=cursor_arg,
                view=view_arg,
                label=label_arg,
                search=search_arg,
            )
        elif path.startswith('/api/media/derivative/'):
            cache_key = unquote(path[len('/api/media/derivative/'):])
            self._serve_derivative(cache_key, qs)
        elif path == '/api/media/new_count':
            from db import _query_new_count
            count = _query_new_count()
            self._json({"count": count})
        elif path == '/api/labels':
            from metadata import _all_labels
            self._json(_all_labels())
        elif path == '/api/sync/status':
            from metadata import _get_sync_status
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

    def _serve_static(self, file_path: str, content_type: str):
        try:
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(404, f"Static asset not found: {e}")

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

    def _serve_media(
        self,
        group_id: str = 'all',
        limit: str = '50',
        cursor: str = None,
        view: str = None,
        label: str = None,
        search: str = None,
    ):
        if not group_id:
            group_id = 'all'
        t0 = time.time()
        print(f"[API] _serve_media starting query for group_id='{group_id}'...", flush=True)
        try:
            from db.queries import query_media_paged
            page_res, lookup = query_media_paged(
                group_id=group_id,
                limit=limit,
                cursor=cursor,
                view=view,
                label=label,
                search=search,
            )
            elapsed = time.time() - t0
            print(f"[API] _serve_media query returned {len(page_res.get('items', []))} items in {elapsed:.3f}s", flush=True)
            _update_media_lookup(lookup)
            self._json(page_res)
        except ValueError as e:
            self.send_error(400, f"Bad request: {e}")
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
                from metadata import _mark_seen
                _mark_seen(ids)
            self._json({"status": "ok", "marked": len(ids)})
        except Exception as e:
            self.send_error(500, str(e))

    def _update_meta(self, msg_id: str):
        """
        POST /api/meta/<messageId>
        Body: { "favourite": bool, "labels": [str] }
        Updates persistent metadata. localKey and content never involved.
        """
        try:
            body = self._read_body_json()
            from metadata import _set_meta
            entry = _set_meta(
                msg_id,
                favourite=body.get('favourite'),
                labels=body.get('labels'),
            )
            self._json(entry)
        except Exception as e:
            self.send_error(500, str(e))

    def _serve_derivative(self, cache_key: str, qs: dict):
        token_str = qs.get('token', [None])[0]
        if not token_str:
            self.send_error(400, "Missing media token parameter")
            return

        from crypto.cache import DerivedMediaCache, decode_media_token
        if not hasattr(_Handler, "_global_cache"):
            cache_dir = os.path.join(os.environ.get("APPDATA", ""), "Signal", "derived_cache")
            _Handler._global_cache = DerivedMediaCache(cache_dir=cache_dir)
        cache = _Handler._global_cache

        try:
            media_id, token_size = decode_media_token(cache._master_key, token_str)
        except ValueError as e:
            self.send_error(400, f"Invalid media token: {e}")
            return

        entry = _get_media_lookup_entry(media_id)
        if not entry:
            self.send_error(404, "Media not found")
            return

        rel_path, local_key, size, content_type = entry
        enc_path = os.path.join(_attach_root, rel_path)

        if not os.path.exists(enc_path):
            self.send_error(404, "Attachment missing on disk")
            return

        deriv_type = qs.get('type', ['poster'])[0]
        if deriv_type not in ('poster', 'preview'):
            self.send_error(400, "Invalid derivative type")
            return

        try:
            width = max(16, min(1920, int(qs.get('w', ['320'])[0])))
            height = max(16, min(1080, int(qs.get('h', ['180'])[0])))
            frames = max(1, min(20, int(qs.get('frames', ['5'])[0])))
            version = max(1, min(100, int(qs.get('v', ['1'])[0])))
            quality = max(1, min(100, int(qs.get('q', ['80'])[0])))
        except (ValueError, TypeError):
            self.send_error(400, "Invalid parameter type or range")
            return

        fmt = str(qs.get('fmt', ['webp'])[0]).lower()
        if fmt not in ('webp', 'jpeg', 'png'):
            self.send_error(400, "Unsupported image format")
            return

        params = {
            'width': width,
            'height': height,
            'format': fmt,
            'quality': quality,
            'size': size,
        }
        if deriv_type == 'preview':
            params['frames'] = frames

        expected_key = cache.derive_cache_key(media_id, deriv_type, version, params)
        if expected_key != cache_key:
            self.send_error(400, "Cache key mismatch for specified parameters")
            return

        def _generate():
            from crypto.attachment import decrypt_attachment
            if not os.path.exists(enc_path):
                raise FileNotFoundError(f"Attachment file missing: {rel_path}")
            with open(enc_path, "rb") as f:
                enc_data = f.read()
            video_bytes = decrypt_attachment(enc_data, local_key, size)

            from crypto.derivatives import generate_poster_bytes, generate_preview_sprite_bytes
            if deriv_type == 'poster':
                return generate_poster_bytes(video_bytes, params)
            else:
                return generate_preview_sprite_bytes(video_bytes, params)

        try:
            _, image_bytes = cache.get_or_generate(
                attachment_id=media_id,
                derivative_type=deriv_type,
                generator_version=version,
                params=params,
                generator_func=_generate,
            )

            mime_type = "image/webp" if fmt == "webp" else ("image/jpeg" if fmt == "jpeg" else "image/png")
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(image_bytes)))
            self.send_header("Cache-Control", "private, no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(image_bytes)
        except Exception as e:
            sys.stderr.write(f"[Derivative Error] Failed to serve derivative for {media_id}: {e}\n")
            self.send_error(500, f"Failed to generate derivative: {e}")

    def _stream(self, msg_id: str):
        entry = _get_media_lookup_entry(msg_id)
        if not entry:
            sys.stderr.write(f"[Media Stream Error] Media ID not found in lookup: {msg_id}\n")
            self.send_error(404, "Media not found")
            return

        rel_path, local_key, size, content_type = entry
        enc_path = os.path.join(_attach_root, rel_path)

        from player.media import serve_encrypted_media
        serve_encrypted_media(self, enc_path, local_key, size, content_type)


# Global state initialization (import-side effects)
import metadata
import metadata.store as metadata_store
import db
import crypto
import time

_META_PATH = metadata._META_PATH
_meta_lock = metadata._meta_lock
_session_start_ts = int(time.time() * 1000)
_cache = crypto._cache if hasattr(crypto, '_cache') else {}
_cache_lock = crypto._cache_lock if hasattr(crypto, '_cache_lock') else threading.Lock()