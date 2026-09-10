#!/usr/bin/env python3
"""cli - Command-line interface for Signal Windows History.

Provides grep, search, list, and export functionality for local
Signal Desktop history on Windows.
"""

import argparse
import sys

try:
    import sqlcipher3
except ImportError:
    sqlcipher3 = None


def main():
    """Main entry point for the Signal Windows History CLI."""
    parser = argparse.ArgumentParser(description="Grep and query local Signal Desktop history on Windows.")
    parser.add_argument("--show-key", action="store_true", help="Print the 64-hex SQLCipher key and exit")
    parser.add_argument("--list-chats", action="store_true", help="List all conversations and message counts")
    parser.add_argument("--search", "-s", type=str, help="Search messages containing the given text")
    parser.add_argument("--chat", "-c", type=str, help="Filter search/media to a specific chat ID or name")
    parser.add_argument("--thread", "-t", type=str, help="Dump transcript for a specific chat ID or name")
    parser.add_argument("--sql", type=str, help="Run an arbitrary SQL query")
    parser.add_argument("--list-media", action="store_true", help="List all media attachments with metadata")
    parser.add_argument("--export-media", action="store_true", help="Decrypt and export all downloaded media files")
    parser.add_argument("--output-dir", type=str, default="exported_media", help="Directory to save exported media (default: ./exported_media)")
    parser.add_argument("--db-copy-path", action="store_true", help="Print path to the safe working DB copy")

    args = parser.parse_args()

    # Step 1: Extract Key
    try:
        from crypto import get_signal_key
        key = get_signal_key()
    except Exception as e:
        print(f"Error extracting Signal key: {e}", file=sys.stderr)
        sys.exit(1)

    if args.show_key:
        print(f"SQLCipher Key:\nx'{key}'")
        return

    # Step 2: Copy Database safely
    try:
        from db import copy_db_snapshot
        work_db = copy_db_snapshot()
    except Exception as e:
        print(f"Error copying Signal database: {e}", file=sys.stderr)
        sys.exit(1)

    if args.db_copy_path:
        print(f"Working database copy at: {work_db}")
        if not (args.list_chats or args.search or args.thread or args.sql or args.list_media or args.export_media):
            return

    # Step 3: Connect
    if sqlcipher3 is None:
        print("Error: 'sqlcipher3' is required to query the database. Install via: pip install sqlcipher3", file=sys.stderr)
        sys.exit(1)

    conn = sqlcipher3.connect(work_db)
    cur = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{key}'\";")
    cur.execute("PRAGMA cipher_compatibility = 4;")

    try:
        if args.list_chats:
            cur.execute("""
                SELECT 
                    c.id,
                    COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS title,
                    c.type,
                    COUNT(m.id) AS msg_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversationId = c.id
                GROUP BY c.id
                ORDER BY msg_count DESC;
            """)
            rows = cur.fetchall()
            print(f"\n{'CONVERSATION ID':<40} | {'MSGS':<6} | {'LAST ACTIVE':<19} | {'NAME / TITLE'}")
            print("-" * 90)
            for r in rows:
                cid, name, ctype, count = r
                last_str = "Never"
                print(f"{cid:<40} | {count:<6} | {last_str:<19} | {name} ({ctype})")
        elif args.search:
            query = """
                SELECT 
                    DATETIME(m.sent_at/1000, 'unixepoch', 'localtime') AS ts,
                    COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS chat_name,
                    CASE m.type WHEN 'outgoing' THEN 'ME' WHEN 'incoming' THEN 'THEM' ELSE m.type END AS who,
                    m.body,
                    m.conversationId
                FROM messages m
                JOIN conversations c ON c.id = m.conversationId
                WHERE m.body LIKE ?
            """
            params = [f"%{args.search}%"]
            if args.chat:
                query += " AND (m.conversationId = ? OR c.name LIKE ? OR c.profileName LIKE ?)"
                params.extend([args.chat, f"%{args.chat}%", f"%{args.chat}%"])
            query += " ORDER BY m.sent_at ASC"

            cur.execute(query, params)
            rows = cur.fetchall()
            print(f"\n--- Search results for '{args.search}' ({len(rows)} matches) ---")
            for r in rows:
                ts, chat, who, body, cid = r
                clean_body = body.replace("\n", " ") if body else ""
                print(f"[{ts}] [{chat}] {who}: {clean_body}")
        elif args.thread:
            # Simplified thread dump
            cur.execute("""
                SELECT 
                    DATETIME(m.sent_at/1000, 'unixepoch', 'localtime') AS ts,
                    CASE m.type WHEN 'outgoing' THEN 'ME' WHEN 'incoming' THEN 'THEM' ELSE m.type END AS who,
                    m.body
                FROM messages m
                JOIN conversations c ON c.id = m.conversationId
                WHERE (c.id = ? OR c.name LIKE ? OR c.profileName LIKE ?) AND m.body IS NOT NULL
                ORDER BY m.sent_at ASC
                LIMIT ?
            """, (args.chat, f"%{args.chat}%", f"%{args.chat}%", 100))
            rows = cur.fetchall()
            print(f"\n--- Transcript for '{args.chat}' ({len(rows)} messages) ---")
            for r in rows:
                ts, who, body = r
                print(f"[{ts}] {who}: {body}")
        elif args.list_media:
            # Basic list media
            cur.execute("""
                SELECT 
                    m.messageId,
                    DATETIME(m.sentAt/1000, 'unixepoch', 'localtime') AS sent_time,
                    COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS chat_name,
                    m.contentType,
                    m.size,
                    m.fileName,
                    m.path
                FROM message_attachments m
                LEFT JOIN conversations c ON c.id = m.conversationId
                WHERE m.path IS NOT NULL AND m.localKey IS NOT NULL
            """)
            rows = cur.fetchall()
            print(f"\n{'TIMESTAMP':<19} | {'CHAT':<18} | {'TYPE':<16} | {'SIZE':<9} | {'FILENAME / PATH'}")
            print("-" * 90)
            for r in rows:
                mid, ts, chat, ctype, size, fname, path = r
                size_str = f"{size:,} B" if size else "Unknown"
                name_str = fname if fname else path
                ts_str = ts if ts else "Unknown"
                print(f"{ts_str:<19} | {chat:<18} | {ctype:<16} | {size_str:<9} | {name_str}")
            print(f"\nTotal attachments found: {len(rows)}")
        elif args.export_media:
            print("Export media functionality not yet implemented in CLI")
        else:
            # Default action: show key + quick help
            print(f"Decrypted SQLCipher Key: x'{key}'")
            print(f"Safe database copy: {work_db}")
            print("\nUsage examples:")
            print("  python -m cli --list-chats")
            print("  python -m cli --search \"recipe\"")
            print("  python -m cli --list-media")
            print("  python -m cli --sql \"SELECT count(*) FROM messages;\"")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
