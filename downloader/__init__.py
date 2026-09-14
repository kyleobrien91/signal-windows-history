#!/usr/bin/env python3
"""downloader - Headless CDP-based media downloader for Signal Desktop.

Connects to Signal via Chrome DevTools Protocol to trigger background
downloads of pending video attachments. Never writes decrypted media
to disk - Signal downloads encrypted blobs using its own secure transport.
"""

from .dispatcher import (get_cdp_target, query_pending_video_groups,
                         run_headless_download)
from .results import DownloadResult, GroupDownloadResult, ItemResultStatus

__all__ = [
    "get_cdp_target",
    "query_pending_video_groups",
    "run_headless_download",
    "DownloadResult",
    "GroupDownloadResult",
    "ItemResultStatus",
]
