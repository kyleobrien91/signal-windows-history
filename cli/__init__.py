#!/usr/bin/env python3
"""cli - Command-line interface for Signal Windows History.

Provides grep, search, list, and export functionality for local
Signal Desktop history on Windows.
"""

import argparse
import base64
import hashlib
import hmac
import mimetypes
import os
import sys

try:
    import sqlcipher3
except ImportError:
    sqlcipher3 = None

from crypto import get_signal_key, decrypt_attachment
from db import copy_db_snapshot


def open_db(db_path: str, key: str):
    """Opens the SQLCipher database with the 64-hex key."""
    if sqlcipher3 is None:
        print("Error: 'sqlcipher3' is required to query the database. Install via: pip install sqlcipher3", file=sys.stderr)
        sys.exit(1)

    conn = sqlcipher3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{key}'\";")
    cur.execute("PRAGMA cipher_compatibility = 4;")
    return conn, cur


def list_chats(cur):
    """Lists conversations ordered by message volume and activity."""
    cur.execute("""
        SELECT
            c.id,
            COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS title,
            c.type,
            COUNT(m.id) AS msg_count,
            DATETIME(MAX(m.sent_at)/1000, 'unixepoch', 'localtime') AS last_active
        FROM conversations c
        LEFT JOIN messages m ON m.conversationId = c.id
        GROUP BY c.id
        ORDER BY msg_count DESC, last_active DESC;
    """)
    rows = cur.fetchall()
    print(f"\n{'CONVERSATION ID':<40} | {'MSGS':<6} | {'LAST ACTIVE':<19} | {'NAME / TITLE'}")
    print("-" * 90)
    for r in rows:
        cid, name, ctype, count, last = r
        last_str = last if last else "Never"
        print(f"{cid:<40} | {count:<6} | {last_str:<19} | {name} ({ctype})")


def search_messages(cur, term: str, chat_filter: str = None):
    """Searches messages by body text."""
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
    params = [f"%{term}%"]
    if chat_filter:
        query += " AND (m.conversationId = ? OR c.name LIKE ? OR c.profileName LIKE ?)"
        params.extend([chat_filter, f"%{chat_filter}%", f"%{chat_filter}%"])
    query += " ORDER BY m.sent_at ASC"

    cur.execute(query, params)
    rows = cur.fetchall()
    print(f"\n--- Search results for '{term}' ({len(rows)} matches) ---")
    for r in rows:
        ts, chat, who, body, cid = r
        clean_body = body.replace("\n", " ") if body else ""
        print(f"[{ts}] [{chat}] {who}: {clean_body}")


def dump_thread(cur, chat_identifier: str, limit: int = 100):
    """Dumps recent messages from a specific conversation."""
    query = """
        SELECT
            DATETIME(m.sent_at/1000, 'unixepoch', 'localtime') AS ts,
            CASE m.type WHEN 'outgoing' THEN 'ME' WHEN 'incoming' THEN 'THEM' ELSE m.type END AS who,
            m.body
        FROM messages m
        JOIN conversations c ON c.id = m.conversationId
        WHERE (c.id = ? OR c.name LIKE ? OR c.profileName LIKE ?) AND m.body IS NOT NULL
        ORDER BY m.sent_at ASC
        LIMIT ?
    """
    cur.execute(query, (chat_identifier, f"%{chat_identifier}%", f"%{chat_identifier}%", limit))
    rows = cur.fetchall()
    print(f"\n--- Transcript for '{chat_identifier}' ({len(rows)} messages) ---")
    for r in rows:
        ts, who, body = r
        print(f"[{ts}] {who}: {body}")


def run_custom_sql(cur, sql: str):
    """Runs a raw SQL query and outputs results."""
    cur.execute(sql)
    rows = cur.fetchall()
    if cur.description:
        cols = [d[0] for d in cur.description]
        print(" | ".join(cols))
        print("-" * (sum(len(c) for c in cols) + 3 * len(cols)))
    for r in rows:
        print(" | ".join(str(item) for item in r))


def list_media(cur, chat_filter: str = None):
    """Lists all attachments with metadata."""
    query = """
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
    """
    params = []
    if chat_filter:
        query += " AND (m.conversationId = ? OR c.name LIKE ? OR c.profileName LIKE ?)"
        params.extend([chat_filter, f"%{chat_filter}%", f"%{chat_filter}%"])
    query += " ORDER BY m.sentAt ASC;"

    cur.execute(query, params)
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


def export_media(cur, output_dir: str, chat_filter: str = None):
    """Decrypts and exports all attachments to disk."""
    appdata = os.environ.get("APPDATA")
    attach_root = os.path.join(appdata, "Signal", "attachments.noindex") if appdata else ""

    query = """
        SELECT
            m.messageId,
            DATETIME(m.sentAt/1000, 'unixepoch', 'localtime') AS sent_time,
            m.sentAt,
            COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS chat_name,
            m.contentType,
            m.size,
            m.fileName,
            m.path,
            m.localKey
        FROM message_attachments m
        LEFT JOIN conversations c ON c.id = m.conversationId
        WHERE m.path IS NOT NULL AND m.localKey IS NOT NULL
    """
    params = []
    if chat_filter:
        query += " AND (m.conversationId = ? OR c.name LIKE ? OR c.profileName LIKE ?)"
        params.extend([chat_filter, f"%{chat_filter}%", f"%{chat_filter}%"])
    query += " ORDER BY m.sentAt ASC;"

    cur.execute(query, params)
    rows = cur.fetchall()

    if not rows:
        print("No attachments found to export.")
        return

    os.makedirs(output_dir, exist_ok=True)
    exported_count = 0
    errors = 0

    print(f"\nExporting {len(rows)} attachments to '{output_dir}'...")

    for r in rows:
        mid, ts, sent_at_ms, chat, ctype, size, fname, rel_path, local_key = r
        full_enc_path = os.path.join(attach_root, rel_path)

        if not os.path.exists(full_enc_path):
            print(f"  [MISSING] {rel_path} (referenced by msg {mid[:8]})")
            errors += 1
            continue

        try:
            with open(full_enc_path, "rb") as f:
                enc_data = f.read()

            plaintext = decrypt_attachment(enc_data, local_key, size)

            clean_chat = "".join(c for c in chat if c.isalnum() or c in (" ", "_", "-")).strip()
            if not clean_chat:
                clean_chat = "chat"
            chat_dir = os.path.join(output_dir, clean_chat)
            os.makedirs(chat_dir, exist_ok=True)

            if fname:
                out_name = fname
            else:
                ext = mimetypes.guess_extension(ctype) or ".bin"
                if ext == ".jpe":
                    ext = ".jpg"
                ts_clean = (ts or "unknown").replace(":", "-").replace(" ", "_")
                out_name = f"{ts_clean}_{mid[:8]}{ext}"

            out_name = "".join(c for c in out_name if c.isalnum() or c in (".", "-", "_", " ")).strip()
            out_path = os.path.join(chat_dir, out_name)

            with open(out_path, "wb") as out_f:
                out_f.write(plaintext)

            if sent_at_ms:
                epoch_sec = sent_at_ms / 1000.0
                os.utime(out_path, (epoch_sec, epoch_sec))

            print(f"  [DECRYPTED] {clean_chat}/{out_name} ({len(plaintext):,} bytes)")
            exported_count += 1
        except Exception as e:
            print(f"  [ERROR] {rel_path}: {e}")
            errors += 1

    print(f"\nDone! Exported: {exported_count}, Errors/Missing: {errors}")
    print(f"Output directory: {os.path.abspath(output_dir)}")


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
        key = get_signal_key()
    except Exception as e:
        print(f"Error extracting Signal key: {e}", file=sys.stderr)
        sys.exit(1)

    if args.show_key:
        print(f"SQLCipher Key:\nx'{key}'")
        return

    # Step 2: Copy Database safely
    try:
        work_db = copy_db_snapshot()
    except Exception as e:
        print(f"Error copying Signal database: {e}", file=sys.stderr)
        sys.exit(1)

    if args.db_copy_path:
        print(f"Working database copy at: {work_db}")
        if not (args.list_chats or args.search or args.thread or args.sql or args.list_media or args.export_media):
            return

    # Step 3: Connect
    conn, cur = open_db(work_db, key)

    try:
        if args.list_chats:
            list_chats(cur)
        elif args.search:
            search_messages(cur, args.search, args.chat)
        elif args.thread:
            dump_thread(cur, args.thread)
        elif args.list_media:
            list_media(cur, args.chat)
        elif args.export_media:
            export_media(cur, args.output_dir, args.chat)
        elif args.sql:
            run_custom_sql(cur, args.sql)
        else:
            # Default action: show key + quick help
            print(f"Decrypted SQLCipher Key: x'{key}'")
            print(f"Safe database copy: {work_db}")
            print("\nUsage examples:")
            print("  python -m cli --list-chats")
            print("  python -m cli --search \"recipe\"")
            print("  python -m cli --list-media")
            print("  python -m cli --export-media --output-dir \"./media\"")
            print("  python -m cli --thread \"pervy\"")
            print("  python -m cli --sql \"SELECT count(*) FROM messages;\"")
    finally:
        conn.close()


__all__ = ["main"]
