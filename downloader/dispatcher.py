#!/usr/bin/env python3
"""downloader.dispatcher - CDP-based Signal Desktop media download dispatcher.

Connects to Signal Desktop via Chrome DevTools Protocol (CDP) on port 9222.
Queries SQLCipher for groups with pending (undownloaded) videos, and instructs
Signal's internal ConversationController to fetch all pending media attachments
in the background — completely headlessly, without driving the mouse or scrolling
the UI.

Security guarantees:
  - NEVER writes decrypted media or attachment blobs to disk.
  - Signal downloads encrypted blobs to attachments.noindex/ using its own secure
    transport.
  - Zero plaintext media at rest.

Prerequisites:
  Signal Desktop must be running with remote debugging enabled:
    Signal.exe --remote-debugging-port=9222
"""

import asyncio
import json
import os
import shutil
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

try:
    from downloader.results import DownloadResult, GroupDownloadResult, ItemResultStatus
except ImportError:
    from results import DownloadResult, GroupDownloadResult, ItemResultStatus


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
    conn = None
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
            JOIN message_attachments ma ON ma.conversationId = c.id
            WHERE ma.contentType LIKE 'video/%' 
              AND (ma.path IS NULL OR ma.pending = 1)
            GROUP BY c.id
            HAVING pending_cnt > 0
            ORDER BY pending_cnt DESC;
        """)
        rows = cur.fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
    finally:
        if conn is not None:
            conn.close()


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


async def _trigger_group_download(ws_url: str, groups: List[Tuple[str, str, int]], db_path: str, key: str, poll_interval: int = 4, max_idle_rounds: int = 8) -> DownloadResult:
    """Iterates through groups, invokes handleReadAndDownloadAttachments over CDP, and polls progress."""
    print(f"[Headless Downloader] Connecting to Signal via WebSocket: {ws_url}")
    group_results: List[GroupDownloadResult] = []
    initial_pending = sum(g[2] for g in groups)

    async with websockets.connect(ws_url) as ws:
        # Verify window.ConversationController is accessible
        check_expr = "Boolean(window.ConversationController && window.ConversationController.get)"
        has_controller = await _evaluate_cdp(ws, check_expr)
        if not has_controller:
            print("[Headless Downloader] Warning: window.ConversationController not yet initialized in Signal.")
            return DownloadResult(
                success=False,
                status=ItemResultStatus.FAILED,
                initial_pending=initial_pending,
                pending_remaining=initial_pending,
                error_message="window.ConversationController not yet initialized in Signal."
            )

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
            is_ok = (result is True) or (isinstance(result, dict) and bool(result.get("success")))
            if is_ok:
                print(f"       -> [OK] Download job dispatched")
                group_results.append(GroupDownloadResult(
                    conversation_id=convo_id,
                    title=title,
                    pending_count=pending_count,
                    status=ItemResultStatus.SUCCESS
                ))
            else:
                err_text = result.get("error") if isinstance(result, dict) else str(result)
                print(f"       -> [Notice] {result}")
                group_results.append(GroupDownloadResult(
                    conversation_id=convo_id,
                    title=title,
                    pending_count=pending_count,
                    status=ItemResultStatus.FAILED,
                    error=err_text
                ))

        print("\n[Headless Downloader] All download jobs dispatched. Monitoring live progress...")
        print("  Press Ctrl+C at any time to finish with currently downloaded videos.\n")

        last_pending = initial_pending
        current_pending = initial_pending
        idle_count = 0

        dispatch_failures = [g for g in group_results if g.status == ItemResultStatus.FAILED]

        try:
            while True:
                await asyncio.sleep(poll_interval)
                # Query fresh database snapshot to observe Signal Desktop's live download progress
                poll_path = None
                try:
                    try:
                        from db import copy_db_snapshot
                        poll_path = copy_db_snapshot()
                    except Exception as snap_err:
                        print(f"\n[Headless Downloader] Warning: Snapshot creation failed ({snap_err}), falling back to direct db_path", file=sys.stderr)

                    if poll_path:
                        try:
                            current_groups = query_pending_video_groups(poll_path, key)
                        finally:
                            try:
                                shutil.rmtree(os.path.dirname(poll_path), ignore_errors=True)
                            except Exception:
                                pass
                    else:
                        current_groups = query_pending_video_groups(db_path, key)
                except Exception as query_err:
                    print(f"\n[Headless Downloader] Warning: Polling query failed: {query_err}", file=sys.stderr)
                    continue

                current_pending = sum(g[2] for g in current_groups)
                downloaded = initial_pending - current_pending

                pct = int((downloaded / initial_pending) * 100) if initial_pending > 0 else 100
                bar = "=" * (pct // 5) + "-" * (20 - (pct // 5))
                print(f"\r  Progress: [{bar}] {pct}% ({downloaded}/{initial_pending} downloaded, {current_pending} remaining)", end="", flush=True)

                if current_pending == 0:
                    print(f"\n[Headless Downloader] [OK] All {initial_pending} videos have downloaded successfully!\n")
                    overall_success = (len(dispatch_failures) == 0)
                    overall_status = ItemResultStatus.SUCCESS if overall_success else ItemResultStatus.FAILED
                    err_msg = None if overall_success else f"{len(dispatch_failures)} group dispatch job(s) failed"
                    return DownloadResult(
                        success=overall_success,
                        status=overall_status,
                        groups=group_results,
                        initial_pending=initial_pending,
                        pending_remaining=0,
                        downloaded_count=initial_pending,
                        error_message=err_msg
                    )

                if current_pending == last_pending:
                    idle_count += 1
                    if idle_count >= max_idle_rounds:
                        print(f"\n[Headless Downloader] No new downloads for {poll_interval * max_idle_rounds}s. Proceeding with currently completed media.\n")
                        return DownloadResult(
                            success=False,
                            status=ItemResultStatus.TIMEOUT,
                            groups=group_results,
                            initial_pending=initial_pending,
                            pending_remaining=current_pending,
                            downloaded_count=initial_pending - current_pending,
                            error_message=f"Download timed out with {current_pending} pending video(s) remaining"
                        )
                else:
                    idle_count = 0
                    last_pending = current_pending

        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\n[Headless Downloader] Download monitoring interrupted by user. Proceeding to player...")
            return DownloadResult(
                success=False,
                status=ItemResultStatus.CANCELLED,
                groups=group_results,
                initial_pending=initial_pending,
                pending_remaining=current_pending,
                downloaded_count=initial_pending - current_pending,
                error_message="Download monitoring interrupted by user"
            )


def run_headless_download(db_path: str, key: str, cdp_port: int = 9222, wait_seconds: int = 15) -> DownloadResult:
    """
    Main entrypoint for programmatic headless downloads.
    Called from signal_player.py or CLI.
    """
    target = get_cdp_target(cdp_port)
    if not target:
        print(f"\n[Headless Downloader] Could not connect to Signal on port {cdp_port}.", file=sys.stderr)
        print("  Make sure Signal was started with remote debugging enabled:", file=sys.stderr)
        print(r'  Start-Process "$env:LOCALAPPDATA\Programs\signal-desktop\Signal.exe" -ArgumentList "--remote-debugging-port=9222"' + "\n", file=sys.stderr)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            error_message=f"Could not connect to Signal on port {cdp_port}"
        )

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        print("[Headless Downloader] Error: No webSocketDebuggerUrl returned by CDP endpoint.", file=sys.stderr)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            error_message="No webSocketDebuggerUrl returned by CDP endpoint"
        )

    # 1. Discover pending video groups from SQLCipher
    try:
        pending_groups = query_pending_video_groups(db_path, key)
    except Exception as e:
        print(f"[Headless Downloader] Error: Failed to query pending videos: {e}", file=sys.stderr)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            error_message=f"Failed to query pending videos: {e}",
            exception=e
        )

    if not pending_groups:
        print("[Headless Downloader] [OK] No pending videos found in any group. Everything is downloaded!")
        return DownloadResult(
            success=True,
            status=ItemResultStatus.SKIPPED,
            initial_pending=0,
            pending_remaining=0,
            downloaded_count=0
        )

    total_pending = sum(g[2] for g in pending_groups)
    print(f"[Headless Downloader] Found {total_pending} pending videos across {len(pending_groups)} group(s).")

    # 2. Run async CDP dispatch & live database progress monitoring
    result = asyncio.run(_trigger_group_download(ws_url, pending_groups, db_path, key))
    return result


if __name__ == "__main__":
    import argparse
    from crypto import get_signal_key
    from db import copy_db_snapshot

    parser = argparse.ArgumentParser(description="Signal Desktop Headless Media Downloader via CDP")
    parser.add_argument("--port", type=int, default=9222, help="Signal CDP remote debugging port (default: 9222)")
    parser.add_argument("--wait", type=int, default=12, help="Seconds to wait for worker downloads (default: 12)")
    args = parser.parse_args()

    print("[Headless Downloader] Initializing...")
    key = get_signal_key()
    db_path = copy_db_snapshot()
    res = run_headless_download(db_path, key, cdp_port=args.port, wait_seconds=args.wait)
    if res:
        print("[Headless Downloader] Completed.")
        sys.exit(0)
    else:
        print(f"[Headless Downloader] Operation failed or incomplete: {getattr(res, 'error_message', 'non-zero pending items')}", file=sys.stderr)
        sys.exit(1)
