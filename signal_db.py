#!/usr/bin/env python3
"""signal_db.py - Legacy compatibility shim for Signal database operations.

Delegates to canonical db and metadata package modules.
This shim contains no business logic.
"""

import sys

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
from db.queries import (
    _get_conversation_map,
    _query_groups,
    _query_media,
    _query_new_count,
)


class _SignalDBModule(sys.modules[__name__].__class__):
    """Legacy compatibility module class providing properties for writable DB connection state.

    Retained strictly for backwards compatibility with legacy callers/tests that reassign
    signal_db._db_conn or signal_db._db_cur. Canonical callers should use set_active_db().
    """

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


sys.modules[__name__].__class__ = _SignalDBModule

_db_lock = sys.modules["db.queries"]._db_lock

__all__ = [
    "copy_db_snapshot",
    "open_db",
    "reload_db",
    "set_active_db",
    "query_groups",
    "query_media",
    "query_new_count",
    "get_conversation_map",
]
