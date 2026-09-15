#!/usr/bin/env python3
"""signal_meta.py - Legacy compatibility shim for user metadata store.

Delegates to canonical metadata package.
This shim contains no business logic.
"""

import sys

from metadata import (
    all_labels,
    get_meta,
    get_sync_status,
    load_metadata,
    mark_seen,
    save_metadata,
    set_meta,
    set_sync_status,
)
from metadata.store import (
    _all_labels,
    _get_meta,
    _get_sync_status,
    _load_metadata,
    _mark_seen,
    _save_metadata,
    _set_meta,
    _set_sync_status,
    _sync_lock,
    _sync_state,
)


class _SignalMetaModule(sys.modules[__name__].__class__):
    @property
    def _META_PATH(self):
        import metadata.store as meta_s
        return meta_s._META_PATH

    @_META_PATH.setter
    def _META_PATH(self, val):
        import metadata.store as meta_s
        import metadata
        meta_s._META_PATH = val
        metadata._META_PATH = val

    @property
    def _meta_data(self):
        import metadata.store as meta_s
        return meta_s._meta_data

    @_meta_data.setter
    def _meta_data(self, val):
        import metadata.store as meta_s
        import metadata
        meta_s._meta_data = val
        metadata._meta_data = val


sys.modules[__name__].__class__ = _SignalMetaModule

_meta_lock = sys.modules["metadata.store"]._meta_lock
_session_start_ts = sys.modules["metadata.store"]._session_start_ts

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
