#!/usr/bin/env python3
"""signal_player.py - Legacy compatibility shim for Signal local video player.

Delegates to canonical player.server package module.
This shim contains no business logic.
"""

import sys
from player.server import (
    WEB_DIR,
    _Handler,
    _attach_root,
    _lookup_lock,
    _media_lookup,
    is_signal_running,
    kill_signal,
    main,
)
from crypto import (
    decrypt_attachment,
    dpapi_decrypt,
    get_signal_key,
    inspect_attachment,
    stream_attachment_range,
)
from db import (
    copy_db_snapshot,
    get_conversation_map,
    open_db,
    query_groups,
    query_media,
    query_new_count,
    reload_db,
    set_active_db,
)
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


class _SignalPlayerModule(sys.modules[__name__].__class__):
    """Legacy compatibility module class providing properties for writable metadata and DB state.

    Retained strictly for backwards compatibility with legacy callers/tests that reassign state via signal_player.
    """

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

    @property
    def _db_conn(self):
        import db.queries as db_q
        return db_q._db_conn

    @_db_conn.setter
    def _db_conn(self, val):
        import db.queries as db_q
        db_q._db_conn = val

    @property
    def _db_cur(self):
        import db.queries as db_q
        return db_q._db_cur

    @_db_cur.setter
    def _db_cur(self, val):
        import db.queries as db_q
        db_q._db_cur = val


sys.modules[__name__].__class__ = _SignalPlayerModule

# Retained private compatibility accessors for existing legacy callers/tests
_meta_lock = sys.modules["metadata.store"]._meta_lock
_db_lock = sys.modules["db.queries"]._db_lock
_session_start_ts = sys.modules["metadata.store"]._session_start_ts

__all__ = [
    "is_signal_running",
    "kill_signal",
    "main",
]

if __name__ == "__main__":
    main()
