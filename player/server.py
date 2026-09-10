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
from urllib.parse import parse_qs, unquote, urlparse
import webbrowser

# Global state for streaming lookup & attachment root
_media_lookup: dict = {}
_lookup_lock = threading.Lock()
_attach_root = ""
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

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
    """Entry point for the Signal Desktop local video player."""
    parser = argparse.ArgumentParser(
        description="Signal Desktop local video player — streams on-the-fly, nothing written to disk."
    )
    parser.add_argument('--port',          type=int, default=7788)
    parser.add_argument('--no-browser',    action='store_true')
    parser.add_argument('--auto-close',    action='store_true', help="Automatically close Signal if running without prompting")
    parser.add_argument('--auto-download', '--sync', action='store_true', dest='auto_download', help="Automatically run background sync/download")
    args = parser.parse_args()

    print("[Signal Player] Starting…")

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
        from db import open_db
        signal_db_conn, signal_db_cur = open_db(db_path, key)
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
                from downloader.dispatcher import run_headless_download, query_pending_video_groups

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

                from metadata import _set_sync_status
                from db import reload_db

                pending_groups = query_pending_video_groups(db_path, key)
                total_pending = sum(g[2] for g in pending_groups)
                if total_pending == 0:
                    print("[Background Sync] All media is already downloaded!")
                    return

                _set_sync_status(True, pending=total_pending, initial=total_pending)
                print(f"[Background Sync] Started background download of {total_pending} pending videos across {len(pending_groups)} groups.")

                run_headless_download(db_path, key, cdp_port=9222, wait_seconds=10)

                reload_db(key)
                _set_sync_status(False, pending=0)
                print("\n[Background Sync] [OK] Background media download completed and database refreshed!")
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
            if signal_db_conn:
                signal_db_conn.close()
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
            print(f"[API] Handling /api/media for group={group_arg}", flush=True)
            self._serve_media(group_arg)
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

    def _serve_media(self, group_id: str):
        if not group_id:
            group_id = 'all'
        t0 = time.time()
        print(f"[API] _serve_media starting query for group_id='{group_id}'...", flush=True)
        try:
            from db import _query_media
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

    def _stream(self, msg_id: str):
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
            from crypto import _get_cached
            data = _get_cached(msg_id, enc_path, local_key, size)
        except Exception as e:
            self.send_error(500, f"Decryption error: {e}")
            return

        total = len(data)
        range_header = self.headers.get('Range')

        if range_header:
            try:
                spec = range_header.strip().replace('bytes=', '')
                s_str, e_str = spec.split('-')
                start = int(s_str) if s_str else 0
                end = int(e_str) if e_str else total - 1
                end = min(end, total - 1)
                chunk = data[start:end + 1]

                self.send_response(206)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
                self.send_header('Content-Length', len(chunk))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                self.wfile.write(chunk)
            except Exception as e:
                self.send_error(400, f"Bad Range header: {e}")
        else:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', total)
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            self.wfile.write(data)


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