#!/usr/bin/env python3
"""signal_headless_downloader.py - Legacy compatibility shim for CDP media downloader.

Delegates to canonical downloader package module.
This shim contains no business logic.
"""

import sys
from typing import List, Tuple

try:
    import sqlcipher3
except ImportError:
    sqlcipher3 = None

from downloader.dispatcher import (
    _evaluate_cdp,
    _trigger_group_download,
    get_cdp_target,
)
from downloader.results import DownloadResult, GroupDownloadResult, ItemResultStatus


def query_pending_video_groups(db_path: str, key: str) -> List[Tuple[str, str, int]]:
    import downloader.dispatcher as dispatcher
    mod = sys.modules[__name__]
    sqlc = getattr(mod, "sqlcipher3", None)
    if sqlc is not None and sqlc is not dispatcher.sqlcipher3:
        conn = None
        try:
            conn = sqlc.connect(db_path)
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
    return dispatcher.query_pending_video_groups(db_path, key)


def run_headless_download(db_path: str, key: str, cdp_port: int = 9222, wait_seconds: int = 15) -> DownloadResult:
    import downloader.dispatcher as dispatcher
    mod = sys.modules[__name__]
    target_func = getattr(mod, "get_cdp_target", dispatcher.get_cdp_target)
    query_func = getattr(mod, "query_pending_video_groups", dispatcher.query_pending_video_groups)

    target = target_func(cdp_port)
    if not target:
        sys.stderr.write(f"\n[Headless Downloader] Could not connect to Signal on port {cdp_port}.\n")
        sys.stderr.write("  Make sure Signal was started with remote debugging enabled:\n")
        sys.stderr.write(r'  Start-Process "$env:LOCALAPPDATA\Programs\signal-desktop\Signal.exe" -ArgumentList "--remote-debugging-port=9222"' + "\n\n")
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            error_message=f"Could not connect to Signal on port {cdp_port}"
        )

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        sys.stderr.write("[Headless Downloader] Error: No webSocketDebuggerUrl returned by CDP endpoint.\n")
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            error_message="No webSocketDebuggerUrl returned by CDP endpoint"
        )

    try:
        pending_groups = query_func(db_path, key)
    except Exception as e:
        sys.stderr.write(f"[Headless Downloader] Error: Failed to query pending videos: {e}\n")
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

    import asyncio
    result = asyncio.run(_trigger_group_download(ws_url, pending_groups, db_path, key))
    return result


if __name__ == "__main__":
    import argparse
    from signal_player import copy_db_snapshot, get_signal_key

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
