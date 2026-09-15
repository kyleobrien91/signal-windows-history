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
import signal_crypto
from signal_crypto import (
    AESGCM,
    Cipher,
    algorithms,
    dpapi_decrypt,
    get_signal_key,
    modes,
    _CACHE_MAX,
    _cache,
    _cache_lock,
    _decrypt_blob,
    _get_cached,
)
import signal_db
from signal_db import (
    copy_db_snapshot,
    open_db,
    reload_db,
    _query_groups,
    _query_media,
    _query_new_count,
)
import signal_meta
from signal_meta import (
    _all_labels,
    _get_meta,
    _get_sync_status,
    _load_metadata,
    _mark_seen,
    _save_metadata,
    _set_meta,
    _set_sync_status,
)


class _SignalPlayerModule(sys.modules[__name__].__class__):
    @property
    def _META_PATH(self):
        import metadata.store as meta_s
        return meta_s._META_PATH

    @_META_PATH.setter
    def _META_PATH(self, val):
        import metadata.store as meta_s
        import metadata
        import signal_meta
        meta_s._META_PATH = val
        metadata._META_PATH = val
        signal_meta._META_PATH = val

    @property
    def _meta_data(self):
        import metadata.store as meta_s
        return meta_s._meta_data

    @_meta_data.setter
    def _meta_data(self, val):
        import metadata.store as meta_s
        import metadata
        import signal_meta
        meta_s._meta_data = val
        metadata._meta_data = val
        signal_meta._meta_data = val

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

_meta_lock = sys.modules["metadata.store"]._meta_lock
_db_lock = sys.modules["db.queries"]._db_lock
_session_start_ts = sys.modules["metadata.store"]._session_start_ts

if __name__ == "__main__":
    main()
