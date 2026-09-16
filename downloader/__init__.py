#!/usr/bin/env python3
"""downloader - Headless CDP-based media downloader for Signal Desktop.

Connects to Signal via Chrome DevTools Protocol to trigger background
downloads of pending video attachments. Never writes decrypted media
to disk - Signal downloads encrypted blobs using its own secure transport.
"""

from .dispatcher import (
    RateEstimator,
    get_cdp_target,
    is_signal_running,
    query_pending_video_groups,
    run_headless_download,
    run_managed_download,
    start_managed_signal_cdp,
    stop_managed_signal_cdp,
)
from .results import DownloadResult, GroupDownloadResult, ItemResultStatus

__all__ = [
    "get_cdp_target",
    "query_pending_video_groups",
    "run_headless_download",
    "run_managed_download",
    "start_managed_signal_cdp",
    "stop_managed_signal_cdp",
    "is_signal_running",
    "RateEstimator",
    "DownloadResult",
    "GroupDownloadResult",
    "ItemResultStatus",
]
