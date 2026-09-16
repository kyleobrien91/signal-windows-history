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
import subprocess
import sys
import time
import urllib.request
from typing import List, Optional, Tuple

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

try:
    from config import PLAYER_EXE_PATH
except ImportError:
    PLAYER_EXE_PATH = os.path.expandvars(r"%LOCALAPPDATA%\Programs\signal-desktop\Signal.exe")


def get_signal_pids() -> List[int]:
    """Returns list of process IDs for running Signal.exe processes on Windows."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq Signal.exe", "/FO", "CSV", "/NH"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        pids = []
        for line in out.strip().splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "signal.exe":
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        return pids
    except Exception:
        return []


def is_signal_running() -> bool:
    """Checks if Signal.exe is currently running on Windows."""
    return len(get_signal_pids()) > 0


def safely_stop_signal_processes(pids: Optional[List[int]] = None, timeout: float = 5.0) -> bool:
    """Terminates specific Signal process PIDs safely and verifies they have exited."""
    if pids is None:
        pids = get_signal_pids()
    if not pids:
        return True

    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
        except Exception as e:
            print(f"[Downloader] Warning: failed to terminate Signal PID {pid}: {e}", file=sys.stderr)

    t0 = time.time()
    while time.time() - t0 < timeout:
        remaining = [p for p in pids if p in get_signal_pids()]
        if not remaining:
            return True
        time.sleep(0.3)

    return len(get_signal_pids()) == 0


def verify_cdp_port_owner(port: int, expected_pid: int) -> bool:
    """Verifies via netstat that the given CDP port is being listened to by expected_pid. Fails closed on error."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        port_str = f":{port}"
        for line in out.splitlines():
            if port_str in line and "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    try:
                        pid = int(parts[-1])
                        if pid == expected_pid:
                            return True
                    except ValueError:
                        pass
        return False
    except Exception:
        # Fail closed on inability to verify ownership
        return False


def get_signal_exe_path() -> str:
    """Returns absolute path to Signal.exe or fallback name."""
    if os.path.exists(PLAYER_EXE_PATH):
        return PLAYER_EXE_PATH
    return "Signal.exe"


def start_managed_signal_cdp(cdp_port: int = 9222, timeout: float = 15.0) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """
    Launches a dedicated Signal.exe process with CDP remote debugging enabled.
    Verifies CDP endpoint ownership before returning.
    """
    if get_cdp_target(cdp_port) is not None:
        return None, f"Port {cdp_port} is already in use by an unmanaged CDP endpoint"

    sig_exe = get_signal_exe_path()
    try:
        proc = subprocess.Popen(
            [sig_exe, f"--remote-debugging-port={cdp_port}"],
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
    except Exception as e:
        return None, f"Failed to launch Signal process: {e}"

    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return None, f"Signal process terminated unexpectedly (exit code {proc.returncode})"
        target = get_cdp_target(cdp_port)
        if target and target.get("webSocketDebuggerUrl"):
            if verify_cdp_port_owner(cdp_port, proc.pid):
                return proc, None
        time.sleep(0.5)

    stop_managed_signal_cdp(proc, relaunch_normal=False, cdp_port=cdp_port)
    return None, f"Timed out waiting for verified CDP endpoint on port {cdp_port}"


def stop_managed_signal_cdp(proc: Optional[subprocess.Popen], relaunch_normal: bool = True, cdp_port: int = 9222):
    """
    Terminates the specific Signal process instance launched for CDP (using exact process handle).
    Verifies that the CDP endpoint is no longer reachable before optionally relaunching Signal.
    """
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
        except Exception as e:
            print(f"[Downloader] Warning: Error terminating CDP process {proc.pid}: {e}", file=sys.stderr)

    t0 = time.time()
    while time.time() - t0 < 3.0:
        if get_cdp_target(cdp_port) is None:
            break
        time.sleep(0.2)

    if relaunch_normal:
        try:
            sig_exe = get_signal_exe_path()
            subprocess.Popen(
                [sig_exe],
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
        except Exception as e:
            print(f"[Downloader] Warning: Could not relaunch normal Signal session: {e}", file=sys.stderr)


class RateEstimator:
    """Calculates smoothed download rate and ETA based on rolling window samples of database pending counts."""
    def __init__(self, window_seconds: float = 15.0):
        self.window_seconds = window_seconds
        self.samples: List[Tuple[float, int]] = []

    def add_sample(self, timestamp: float, pending_count: int):
        self.samples.append((timestamp, pending_count))
        cutoff = timestamp - self.window_seconds
        self.samples = [s for s in self.samples if s[0] >= cutoff]

    def get_rate_and_eta(self, initial_pending: int, current_pending: int) -> Tuple[float, Optional[float], str]:
        downloaded = initial_pending - current_pending
        if downloaded <= 0 or len(self.samples) < 2:
            return 0.0, None, "ETA calculating..."

        t_first, p_first = self.samples[0]
        t_last, p_last = self.samples[-1]

        dt = t_last - t_first
        dp = p_first - p_last

        if dt > 0 and dp > 0:
            rate = dp / dt
        else:
            rate = 0.0

        if rate > 0 and current_pending > 0:
            eta_sec = current_pending / rate
            mins = int(eta_sec) // 60
            secs = int(eta_sec) % 60
            if mins >= 60:
                hrs = mins // 60
                mins = mins % 60
                eta_str = f"ETA {hrs:02d}:{mins:02d}:{secs:02d}"
            else:
                eta_str = f"ETA {mins:02d}:{secs:02d}"
            return rate, eta_sec, eta_str
        elif current_pending == 0:
            return rate, 0.0, "ETA 00:00"
        else:
            return 0.0, None, "ETA calculating..."


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


async def _trigger_group_download(
    ws_url: str,
    groups: List[Tuple[str, str, int]],
    db_path: str,
    key: str,
    poll_interval: float = 2.0,
    max_idle_rounds: int = 8,
    show_progress: bool = False
) -> DownloadResult:
    """Iterates through groups, invokes handleReadAndDownloadAttachments over CDP, and polls progress."""
    if not show_progress:
        print(f"[Headless Downloader] Connecting to Signal via WebSocket: {ws_url}")
    group_results: List[GroupDownloadResult] = []
    initial_pending = sum(g[2] for g in groups)
    t_start = time.time()

    async with websockets.connect(ws_url) as ws:
        # Verify window.ConversationController is accessible
        check_expr = "Boolean(window.ConversationController && window.ConversationController.get)"
        has_controller = await _evaluate_cdp(ws, check_expr)
        if not has_controller:
            if not show_progress:
                print("[Headless Downloader] Warning: window.ConversationController not yet initialized in Signal.")
            return DownloadResult(
                success=False,
                status=ItemResultStatus.FAILED,
                initial_pending=initial_pending,
                pending_remaining=initial_pending,
                error_message="window.ConversationController not yet initialized in Signal."
            )

        if not show_progress:
            print(f"[Headless Downloader] Triggering background downloads for {len(groups)} group(s)...")

        for idx, (convo_id, title, pending_count) in enumerate(groups, 1):
            if not show_progress:
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
                if not show_progress:
                    print(f"       -> [OK] Download job dispatched")
                group_results.append(GroupDownloadResult(
                    conversation_id=convo_id,
                    title=title,
                    pending_count=pending_count,
                    status=ItemResultStatus.SUCCESS
                ))
            else:
                err_text = result.get("error") if isinstance(result, dict) else str(result)
                if not show_progress:
                    print(f"       -> [Notice] {result}")
                group_results.append(GroupDownloadResult(
                    conversation_id=convo_id,
                    title=title,
                    pending_count=pending_count,
                    status=ItemResultStatus.FAILED,
                    error=err_text
                ))

        if not show_progress:
            print("\n[Headless Downloader] All download jobs dispatched. Monitoring live progress...")
            print("  Press Ctrl+C at any time to finish with currently downloaded videos.\n")

        last_pending = initial_pending
        current_pending = initial_pending
        idle_count = 0
        dispatch_failures = [g for g in group_results if g.status == ItemResultStatus.FAILED]

        rate_estimator = RateEstimator(window_seconds=15.0)
        rate_estimator.add_sample(time.time(), current_pending)

        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        last_convo_title = None
        last_rate = 0.0
        last_eta_sec = None

        try:
            while True:
                await asyncio.sleep(poll_interval)
                now = time.time()

                poll_path = None
                try:
                    try:
                        from db import copy_db_snapshot
                        poll_path = copy_db_snapshot()
                    except Exception as snap_err:
                        if not show_progress:
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

                rate_estimator.add_sample(now, current_pending)
                rate, eta_sec, eta_str = rate_estimator.get_rate_and_eta(initial_pending, current_pending)
                last_rate = rate
                last_eta_sec = eta_sec

                pct = int((downloaded / initial_pending) * 100) if initial_pending > 0 else 100
                bar = "=" * (pct // 5) + "-" * (20 - (pct // 5))

                if show_progress:
                    current_convo = current_groups[0] if current_groups else None
                    if current_convo and current_convo[1] != last_convo_title:
                        if is_tty:
                            print()
                        print(f"Completed: {downloaded}/{initial_pending}")
                        print(f"Current conversation: {current_convo[1]} ({current_convo[2]} remaining)\n")
                        last_convo_title = current_convo[1]

                    rate_part = f"  |  {rate:.1f} videos/s" if rate > 0 else ""
                    prog_line = f"[{bar}] {pct}%  {downloaded}/{initial_pending} complete{rate_part}  |  {eta_str}"

                    if is_tty:
                        print(f"\r{prog_line}", end="", flush=True)
                    else:
                        print(prog_line, flush=True)
                else:
                    print(f"\r  Progress: [{bar}] {pct}% ({downloaded}/{initial_pending} downloaded, {current_pending} remaining)", end="", flush=True)

                if current_pending == 0:
                    elapsed = time.time() - t_start
                    if show_progress:
                        if is_tty:
                            print()
                        print("\n[====================] 100%  " + f"{initial_pending}/{initial_pending} complete  |  ETA 00:00\n")
                        print("Download complete.")
                        print("Signal media remains in Signal's normal encrypted attachment storage.\n")
                    else:
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
                        rate=rate,
                        eta_seconds=0.0,
                        elapsed_seconds=elapsed,
                        error_message=err_msg
                    )

                if current_pending == last_pending:
                    idle_count += 1
                    if idle_count >= max_idle_rounds:
                        elapsed = time.time() - t_start
                        if show_progress:
                            if is_tty:
                                print()
                            print(f"\nDownload incomplete (timed out after {int(elapsed)}s with {current_pending} pending remaining).\n")
                        else:
                            print(f"\n[Headless Downloader] No new downloads for {poll_interval * max_idle_rounds}s. Proceeding with currently completed media.\n")
                        return DownloadResult(
                            success=False,
                            status=ItemResultStatus.TIMEOUT,
                            groups=group_results,
                            initial_pending=initial_pending,
                            pending_remaining=current_pending,
                            downloaded_count=initial_pending - current_pending,
                            rate=last_rate,
                            eta_seconds=last_eta_sec,
                            elapsed_seconds=elapsed,
                            error_message=f"Download timed out with {current_pending} pending video(s) remaining"
                        )
                else:
                    idle_count = 0
                    last_pending = current_pending

        except (asyncio.CancelledError, KeyboardInterrupt):
            elapsed = time.time() - t_start
            if show_progress:
                if is_tty:
                    print()
                print("\nDownload cancelled by user.\n")
            else:
                print("\n[Headless Downloader] Download monitoring interrupted by user. Proceeding to player...")
            return DownloadResult(
                success=False,
                status=ItemResultStatus.CANCELLED,
                groups=group_results,
                initial_pending=initial_pending,
                pending_remaining=current_pending,
                downloaded_count=initial_pending - current_pending,
                rate=last_rate,
                eta_seconds=last_eta_sec,
                elapsed_seconds=elapsed,
                error_message="Download monitoring interrupted by user"
            )


def run_headless_download(
    db_path: str,
    key: str,
    cdp_port: int = 9222,
    wait_seconds: int = 15,
    managed_proc: Optional[subprocess.Popen] = None,
    show_progress: bool = False
) -> DownloadResult:
    """
    Main entrypoint for programmatic headless downloads.
    Called from signal_player.py, CLI, or run_managed_download.
    """
    target = get_cdp_target(cdp_port)
    if not target:
        if not show_progress:
            print(f"\n[Headless Downloader] Could not connect to Signal on port {cdp_port}.", file=sys.stderr)
            print("  Make sure Signal was started with remote debugging enabled:", file=sys.stderr)
            print(r'  Start-Process "$env:LOCALAPPDATA\Programs\signal-desktop\Signal.exe" -ArgumentList "--remote-debugging-port=9222"' + "\n", file=sys.stderr)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            tracked_pid=managed_proc.pid if managed_proc else None,
            error_message=f"Could not connect to Signal on port {cdp_port}"
        )

    if managed_proc and not verify_cdp_port_owner(cdp_port, managed_proc.pid):
        if not show_progress:
            print(f"[Headless Downloader] Security error: CDP endpoint on port {cdp_port} does not belong to expected PID {managed_proc.pid}.", file=sys.stderr)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            tracked_pid=managed_proc.pid,
            error_message=f"CDP endpoint ownership re-verification failed for PID {managed_proc.pid}"
        )

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        if not show_progress:
            print("[Headless Downloader] Error: No webSocketDebuggerUrl returned by CDP endpoint.", file=sys.stderr)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            tracked_pid=managed_proc.pid if managed_proc else None,
            error_message="No webSocketDebuggerUrl returned by CDP endpoint"
        )

    # 1. Discover pending video groups from SQLCipher
    try:
        pending_groups = query_pending_video_groups(db_path, key)
    except Exception as e:
        if not show_progress:
            print(f"[Headless Downloader] Error: Failed to query pending videos: {e}", file=sys.stderr)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            tracked_pid=managed_proc.pid if managed_proc else None,
            error_message=f"Failed to query pending videos: {e}",
            exception=e
        )

    if not pending_groups:
        if show_progress:
            print("Outstanding media: 0")
            print("All media is already downloaded.\n")
        else:
            print("[Headless Downloader] [OK] No pending videos found in any group. Everything is downloaded!")
        return DownloadResult(
            success=True,
            status=ItemResultStatus.SKIPPED,
            initial_pending=0,
            pending_remaining=0,
            downloaded_count=0,
            tracked_pid=managed_proc.pid if managed_proc else None
        )

    total_pending = sum(g[2] for g in pending_groups)
    if show_progress:
        print(f"Outstanding media: {total_pending} videos across {len(pending_groups)} conversation(s)\n")
        print("Downloading outstanding media...")
    else:
        print(f"[Headless Downloader] Found {total_pending} pending videos across {len(pending_groups)} group(s).")

    # 2. Run async CDP dispatch & live database progress monitoring
    result = asyncio.run(_trigger_group_download(ws_url, pending_groups, db_path, key, show_progress=show_progress))
    if managed_proc:
        result.tracked_pid = managed_proc.pid
    return result


def run_managed_download(
    db_path: Optional[str] = None,
    key: Optional[str] = None,
    cdp_port: int = 9222,
    show_progress: bool = True
) -> DownloadResult:
    """
    Main entrypoint for fully managed download operations (e.g. --download-only).
    Handles process isolation, CDP startup, live progress, cleanup, and session restoration.
    """
    sig_was_running = is_signal_running()

    # 1. Extract key and create DB snapshot if not provided
    if not key:
        from crypto import get_signal_key
        try:
            key = get_signal_key()
        except Exception as e:
            print(f"[Error] Could not extract Signal key: {e}", file=sys.stderr)
            return DownloadResult(
                success=False,
                status=ItemResultStatus.FAILED,
                error_message=f"Could not extract Signal key: {e}",
                exception=e
            )

    # If Signal is running normally without CDP, stop it briefly for clean snapshot & CDP launch
    if sig_was_running:
        try:
            from player.server import kill_signal
            kill_signal()
        except Exception as e:
            print(f"[Warning] Failed to stop running Signal process: {e}", file=sys.stderr)

    if not db_path:
        from db import copy_db_snapshot
        try:
            db_path = copy_db_snapshot()
        except Exception as e:
            print(f"[Error] Could not create database snapshot: {e}", file=sys.stderr)
            return DownloadResult(
                success=False,
                status=ItemResultStatus.FAILED,
                error_message=f"Could not create database snapshot: {e}",
                exception=e
            )

    # 2. Check pending media before starting CDP if possible
    try:
        pending = query_pending_video_groups(db_path, key)
        if not pending:
            if show_progress:
                print("Outstanding media: 0")
                print("All media is already downloaded.\n")
            if sig_was_running:
                stop_managed_signal_cdp(None, relaunch_normal=True)
            return DownloadResult(
                success=True,
                status=ItemResultStatus.SKIPPED,
                initial_pending=0,
                pending_remaining=0,
                downloaded_count=0
            )
    except Exception:
        pass

    # 3. Launch dedicated temporary Signal instance with CDP debugging
    proc, err = start_managed_signal_cdp(cdp_port=cdp_port)
    if not proc:
        print(f"[Error] Failed to start Signal with remote debugging: {err}", file=sys.stderr)
        if sig_was_running:
            stop_managed_signal_cdp(None, relaunch_normal=True)
        return DownloadResult(
            success=False,
            status=ItemResultStatus.FAILED,
            error_message=f"CDP startup failed: {err}"
        )

    try:
        res = run_headless_download(
            db_path,
            key,
            cdp_port=cdp_port,
            managed_proc=proc,
            show_progress=show_progress
        )
        return res
    finally:
        # 4. Terminate temporary CDP process and restore normal Signal session
        stop_managed_signal_cdp(proc, relaunch_normal=True)


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
