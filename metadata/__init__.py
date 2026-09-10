#!/usr/bin/env python3
"""metadata - User metadata store for Signal videos.

Tracks favourites, custom labels, and seen status for videos.
Persisted locally to signal_player_meta.json alongside this package.
"""

from .store import (_load_metadata, _save_metadata, _get_meta, _set_meta,
                    _mark_seen, _all_labels, _META_PATH)

__all__ = [
    "_load_metadata",
    "_save_metadata",
    "_get_meta",
    "_set_meta",
    "_mark_seen",
    "_all_labels",
    "_META_PATH",
]