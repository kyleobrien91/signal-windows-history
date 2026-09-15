#!/usr/bin/env python3
"""metadata - User metadata store for Signal videos.

Tracks favourites, custom labels, and seen status for videos.
Persisted locally to signal_player_meta.json.
"""

from .store import (
    all_labels,
    get_meta,
    get_sync_status,
    load_metadata,
    mark_seen,
    save_metadata,
    set_meta,
    set_sync_status,
    _all_labels,
    _get_meta,
    _get_sync_status,
    _load_metadata,
    _mark_seen,
    _META_PATH,
    _meta_data,
    _meta_lock,
    _save_metadata,
    _set_meta,
    _set_sync_status,
)

__all__ = [
    "load_metadata",
    "save_metadata",
    "get_meta",
    "set_meta",
    "mark_seen",
    "all_labels",
    "get_sync_status",
    "set_sync_status",
]
