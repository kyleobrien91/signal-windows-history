#!/usr/bin/env python3
"""signal_grep.py - Legacy compatibility shim for Signal Windows History CLI.

Delegates execution to the canonical cli package.
This shim contains no business logic.
"""

from cli import (
    dump_thread,
    export_media,
    list_chats,
    list_media,
    main,
    open_db,
    run_custom_sql,
    search_messages,
)

if __name__ == "__main__":
    main()
