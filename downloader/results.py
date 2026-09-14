#!/usr/bin/env python3
"""downloader.results - Canonical result models for Signal media downloader operations."""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ItemResultStatus(Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class GroupDownloadResult:
    conversation_id: str
    title: str
    pending_count: int
    status: ItemResultStatus
    error: Optional[str] = None
    exception: Optional[Exception] = None


@dataclass
class DownloadResult:
    success: bool
    status: ItemResultStatus
    groups: List[GroupDownloadResult] = field(default_factory=list)
    initial_pending: int = 0
    pending_remaining: int = 0
    downloaded_count: int = 0
    error_message: Optional[str] = None
    exception: Optional[Exception] = None

    def __bool__(self) -> bool:
        return self.success
