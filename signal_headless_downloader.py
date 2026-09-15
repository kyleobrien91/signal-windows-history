#!/usr/bin/env python3
"""signal_headless_downloader.py - Legacy compatibility shim for CDP media downloader.

Delegates to canonical downloader package module.
This shim contains no business logic.
"""

import sys
import downloader.dispatcher as dispatcher
from downloader.dispatcher import (
    get_cdp_target,
    query_pending_video_groups,
    run_headless_download,
)
from downloader.results import DownloadResult, GroupDownloadResult, ItemResultStatus


class _HeadlessDownloaderModule(sys.modules[__name__].__class__):
    """Legacy compatibility module class providing properties for dispatcher attributes.

    Retained strictly for backwards compatibility with legacy tests that mock/reassign downloader attributes on signal_headless_downloader.
    """

    @property
    def sqlcipher3(self):
        return dispatcher.sqlcipher3

    @sqlcipher3.setter
    def sqlcipher3(self, val):
        dispatcher.sqlcipher3 = val

    @sqlcipher3.deleter
    def sqlcipher3(self):
        if hasattr(dispatcher, "sqlcipher3"):
            delattr(dispatcher, "sqlcipher3")

    @property
    def get_cdp_target(self):
        return dispatcher.get_cdp_target

    @get_cdp_target.setter
    def get_cdp_target(self, val):
        dispatcher.get_cdp_target = val

    @get_cdp_target.deleter
    def get_cdp_target(self):
        if hasattr(dispatcher, "get_cdp_target"):
            delattr(dispatcher, "get_cdp_target")

    @property
    def query_pending_video_groups(self):
        return dispatcher.query_pending_video_groups

    @query_pending_video_groups.setter
    def query_pending_video_groups(self, val):
        dispatcher.query_pending_video_groups = val

    @query_pending_video_groups.deleter
    def query_pending_video_groups(self):
        if hasattr(dispatcher, "query_pending_video_groups"):
            delattr(dispatcher, "query_pending_video_groups")


sys.modules[__name__].__class__ = _HeadlessDownloaderModule

__all__ = [
    "get_cdp_target",
    "query_pending_video_groups",
    "run_headless_download",
    "DownloadResult",
    "GroupDownloadResult",
    "ItemResultStatus",
]

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
