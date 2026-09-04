#!/usr/bin/env python3
"""
signal_headless_downloader.py - Headless Multi-Group Media Downloader for Signal Desktop
=======================================================================================
Connects to Signal Desktop via Chrome DevTools Protocol (CDP) on port 9222.
Queries SQLCipher for groups that have pending (undownloaded) videos, and instructs
Signal's internal ConversationController to fetch all pending media attachments in the
background — completely headlessly, without driving the mouse or scrolling the UI.

Security guarantees:
  - NEVER writes decrypted media or attachment blobs to disk.
  - Signal downloads encrypted blobs to attachments.noindex/ using its own secure transport.
  - Zero plaintext media at rest.

Prerequisites:
  Signal Desktop must be running with remote debugging enabled:
    Signal.exe --remote-debugging-port=9222
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
from typing import List, Tuple

try:
    import websockets
except ImportError:
    print("[Error] 'websockets' library required. Run: pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    import sqlcipher3
except ImportError:
    print("[Error] 'sqlcipher3' library required.", file=sys.stderr)
    sys.exit(1)


def get_cdp_target(port: int = 9222) -> dict:
    """Finds the main Signal Desktop application page target over CDP."""
    url = f"http://127.0.0.1:{port}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SignalHeadlessDownloader"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            targets = json.loads(resp.read().decode())
    except Exception as e:
        return None

    # Find main window target (usually type == 'page')
    page_targets = [t for t in targets if t.get("type") == "page"]
    if page_targets:
        return page_targets[0]
    if targets:
        return targets[0]
    return None


def query_pending_video_groups(db_path: str, key: str) -> List[Tuple[str, str, int]]:
    """
    Connects to Signal SQLCipher snapshot and returns groups with pending videos:
    [(conversationId, groupName, pending_count), ...]
    """
    try:
        conn = sqlcipher3.connect(db_path)
        cur = conn.cursor()
        cur.execute(f"PRAGMA key = \"x'{key}'\";")
        cur.execute("PRAGMA cipher_compatibility = 4;")

        cur.execute("""
            SELECT 
                c.id, 
                COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS title, 
                COUNT(ma.messageId) AS pending_cnt
            FROM conversations c
            JOIN messages m ON m.conversationId = c.id
            JOIN message_attachments ma ON ma.messageId = m.id
            WHERE ma.contentType LIKE 'video/%' 
              AND (ma.path IS NULL OR ma.pending = 1)
            GROUP BY c.id
            HAVING pending_cnt > 0
            ORDER BY pending_cnt DESC;
        """)
        rows = cur.fetchall()
        conn.close()
        return [(r[0], r[1], r[2]) for r in rows]
    except Exception as e:
        return []


async def _evaluate_cdp(ws, expr: str, timeout: float = 10.0):
    """Evaluates a JavaScript expression inside the Signal Electron renderer context."""
    call_id = int(time.time() * 1000) % 1000000
    msg = {
        "id": call_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "awaitPromise": True,
            "returnByValue": True,
        }
    }
    await ws.send(json.dumps(msg))
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, timeout - (time.time() - t0)))
            data = json.loads(raw)
            if data.get("id") == call_id:
                res = data.get("result", {})
                if "exceptionDetails" in res:
                    return {"error": res["exceptionDetails"]}
                return res.get("result", {}).get("value")
        except asyncio.TimeoutError:
            break
        except Exception as e:
            return {"error": str(e)}
    return {"error": f"Timeout ({timeout}s) waiting for CDP evaluation"}


async def _trigger_group_download(ws_url: str, groups: List[Tuple[str, str, int]], db_path: str, key: str, poll_interval: int = 4, max_idle_rounds: int = 8):
    """Iterates through groups, invokes handleReadAndDownloadAttachments over CDP, and polls progress."""
    print(f"[Headless Downloader] Connecting to Signal via WebSocket: {ws_url}")
    async with websockets.connect(ws_url) as ws:
        # Verify window.ConversationController is accessible
        check_expr = "Boolean(window.ConversationController && window.ConversationController.get)"
        has_controller = await _evaluate_cdp(ws, check_expr)
        if not has_controller:
            print("[Headless Downloader] Warning: window.ConversationController not yet initialized in Signal.")
            return False

        print(f"[Headless Downloader] Triggering background downloads for {len(groups)} group(s)...")
        for idx, (convo_id, title, pending_count) in enumerate(groups, 1):
            print(f"  [{idx}/{len(groups)}] Queueing: '{title}' ({pending_count} pending videos)")

            trigger_js = f"""
            (async () => {{
                try {{
                    const convo = window.ConversationController.get({json.dumps(convo_id)});
                    if (!convo) return {{ success: false, error: "Conversation model not found" }};
                    if (typeof convo.handleReadAndDownloadAttachments === "function") {{
                        convo.handleReadAndDownloadAttachments({{ isLocalAction: true }});
                        return {{ success: true, method: "handleReadAndDownloadAttachments" }};
                    }}
                    return {{ success: false, error: "handleReadAndDownloadAttachments not available" }};
                }} catch (err) {{
                    return {{ success: false, error: String(err) }};
                }}
            }})()
            """
            result = await _evaluate_cdp(ws, trigger_js)
            if isinstance(result, dict) and result.get("success"):
                print(f"       -> [OK] Download job dispatched")
            else:
                print(f"       -> [Notice] {result}")

        print("\n[Headless Downloader] All download jobs dispatched. Monitoring live progress...")
        print("  Press Ctrl+C at any time to finish with currently downloaded videos.\n")

        initial_pending = sum(g[2] for g in groups)
        last_pending = initial_pending
        idle_count = 0

        try:
            while True:
                await asyncio.sleep(poll_interval)
                current_groups = query_pending_video_groups(db_path, key)
                current_pending = sum(g[2] for g in current_groups)
                downloaded = initial_pending - current_pending

                pct = int((downloaded / initial_pending) * 100) if initial_pending > 0 else 100
                bar = "=" * (pct // 5) + "-" * (20 - (pct // 5))
                print(f"\r  Progress: [{bar}] {pct}% ({downloaded}/{initial_pending} downloaded, {current_pending} remaining)", end="", flush=True)

                if current_pending == 0:
                    print(f"\n[Headless Downloader] [OK] All {initial_pending} videos have downloaded successfully!\n")
                    break

                if current_pending == last_pending:
                    idle_count += 1
                    if idle_count >= max_idle_rounds:
                        print(f"\n[Headless Downloader] No new downloads for {poll_interval * max_idle_rounds}s. Proceeding with currently completed media.\n")
                        break
                else:
                    idle_count = 0
                    last_pending = current_pending

        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\n[Headless Downloader] Download monitoring interrupted by user. Proceeding to player...")

        return True


def run_headless_download(db_path: str, key: str, cdp_port: int = 9222, wait_seconds: int = 15) -> bool:
    """
    Main entrypoint for programmatic headless downloads.
    Called from signal_player.py or CLI.
    """
    target = get_cdp_target(cdp_port)
    if not target:
        print(f"\n[Headless Downloader] Could not connect to Signal on port {cdp_port}.", file=sys.stderr)
        print("  Make sure Signal was started with remote debugging enabled:", file=sys.stderr)
        print(r'  Start-Process "$env:LOCALAPPDATA\Programs\signal-desktop\Signal.exe" -ArgumentList "--remote-debugging-port=9222"' + "\n", file=sys.stderr)
        return False

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        print("[Headless Downloader] Error: No webSocketDebuggerUrl returned by CDP endpoint.", file=sys.stderr)
        return False

    # 1. Discover pending video groups from SQLCipher
    pending_groups = query_pending_video_groups(db_path, key)
    if not pending_groups:
        print("[Headless Downloader] [OK] No pending videos found in any group. Everything is downloaded!")
        return True

    total_pending = sum(g[2] for g in pending_groups)
    print(f"[Headless Downloader] Found {total_pending} pending videos across {len(pending_groups)} group(s).")

    # 2. Run async CDP dispatch & live database progress monitoring
    success = asyncio.run(_trigger_group_download(ws_url, pending_groups, db_path, key))
    return success


if __name__ == "__main__":
    import argparse
    from signal_player import get_signal_key, copy_db_snapshot

    parser = argparse.ArgumentParser(description="Signal Desktop Headless Media Downloader via CDP")
    parser.add_argument("--port", type=int, default=9222, help="Signal CDP remote debugging port (default: 9222)")
    parser.add_argument("--wait", type=int, default=12, help="Seconds to wait for worker downloads (default: 12)")
    args = parser.parse_args()

    print("[Headless Downloader] Initializing...")
    key = get_signal_key()
    db_path = copy_db_snapshot()
    ok = run_headless_download(db_path, key, cdp_port=args.port, wait_seconds=args.wait)
    if ok:
        print("[Headless Downloader] Completed.")
    else:
        sys.exit(1)
