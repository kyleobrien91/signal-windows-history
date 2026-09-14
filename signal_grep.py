#!/usr/bin/env python3
"""
signal_grep.py - Extract Signal Desktop SQLCipher key and query/grep messages on Windows.
Substitutes macOS Keychain/safeStorage routines with Windows DPAPI + AES-256-GCM (Chromium OSCrypt).
"""

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import mimetypes
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    print("Error: 'cryptography' library is required. Install via: pip install cryptography", file=sys.stderr)
    sys.exit(1)


try:
    import sqlcipher3
except ImportError:
    sqlcipher3 = None


# ---------------------------------------------------------------------------
# Windows DPAPI structures & functions via ctypes
# ---------------------------------------------------------------------------
class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte))
    ]


def dpapi_decrypt(encrypted_bytes: bytes) -> bytes:
    """Decrypts a DPAPI-protected blob using the current Windows user session."""
    p_data_in = DATA_BLOB(
        len(encrypted_bytes),
        ctypes.cast(ctypes.create_string_buffer(encrypted_bytes), ctypes.POINTER(ctypes.c_byte))
    )
    p_data_out = DATA_BLOB()
    
    ret = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(p_data_in),
        None,  # description
        None,  # optional entropy
        None,  # reserved
        None,  # prompt struct
        0,     # flags
        ctypes.byref(p_data_out)
    )
    if not ret:
        error_code = ctypes.GetLastError()
        raise ctypes.WinError(error_code)
    
    decrypted = ctypes.string_at(p_data_out.pbData, p_data_out.cbData)
    ctypes.windll.kernel32.LocalFree(p_data_out.pbData)
    return decrypted


def get_signal_key() -> str:
    """
    Extracts and unwraps the 64-hex SQLCipher key from Signal Desktop on Windows:
    1. Read %APPDATA%\\Signal\\Local State -> decrypt os_crypt.encrypted_key via DPAPI -> AES master key
    2. Read %APPDATA%\\Signal\\config.json -> decrypt encryptedKey (v10 payload) via AES-256-GCM
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("%APPDATA% environment variable not found.")
    
    signal_dir = os.path.join(appdata, "Signal")
    local_state_path = os.path.join(signal_dir, "Local State")
    config_path = os.path.join(signal_dir, "config.json")

    if not os.path.exists(local_state_path):
        raise FileNotFoundError(f"Signal 'Local State' file not found at: {local_state_path}")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Signal 'config.json' file not found at: {config_path}")

    # 1. DPAPI master key from Local State
    with open(local_state_path, "r", encoding="utf-8") as f:
        local_state = json.load(f)
    
    raw_os_crypt = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
    if not raw_os_crypt.startswith(b"DPAPI"):
        raise ValueError("Invalid os_crypt.encrypted_key format (missing DPAPI header).")
    
    master_key = dpapi_decrypt(raw_os_crypt[5:])

    # 2. SQLCipher key from config.json
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if "encryptedKey" in config:
        enc_bytes = bytes.fromhex(config["encryptedKey"])
        if not enc_bytes.startswith(b"v10"):
            raise ValueError("Invalid encryptedKey format (missing v10 header).")
        
        nonce = enc_bytes[3:15]
        ciphertext_and_tag = enc_bytes[15:]
        aesgcm = AESGCM(master_key)
        decrypted_key = aesgcm.decrypt(nonce, ciphertext_and_tag, None)
        return decrypted_key.decode("utf-8")
    elif "key" in config:
        # Fallback for older versions storing plaintext key
        return config["key"]
    else:
        raise KeyError("Neither 'encryptedKey' nor 'key' found in Signal config.json")


def copy_db_to_work_dir() -> str:
    """Create a consistent SQLCipher snapshot suitable for read-only analysis."""
    appdata = os.environ.get("APPDATA")
    src_dir = os.path.join(appdata, "Signal", "sql")
    db_src = os.path.join(src_dir, "db.sqlite")
    if not os.path.exists(db_src):
        raise FileNotFoundError(f"Signal database not found at {db_src}")

    key = get_signal_key()
    work_dir = tempfile.mkdtemp(prefix="signal-player-work-", dir=os.environ.get("TEMP", "."))
    db_dst = os.path.join(work_dir, "db.sqlite")

    max_retries = 5
    for attempt in range(max_retries):
        try:
            source_uri = f"{Path(db_src).resolve().as_uri()}?mode=ro"
            src_conn = sqlcipher3.connect(source_uri, uri=True)
            try:
                src_conn.execute(f"PRAGMA key = \"x'{key}'\";")
                src_conn.execute("PRAGMA cipher_compatibility = 4;")
                src_conn.execute("PRAGMA query_only = ON;")

                dst_conn = sqlcipher3.connect(db_dst)
                try:
                    dst_conn.execute(f"PRAGMA key = \"x'{key}'\";")
                    dst_conn.execute("PRAGMA cipher_compatibility = 4;")
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
            return db_dst
        except Exception:
            if os.path.exists(db_dst):
                try:
                    os.remove(db_dst)
                except OSError:
                    pass
            if attempt == max_retries - 1:
                raise
            time.sleep(0.3)

    raise RuntimeError("Failed to create a consistent database snapshot")


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


def decrypt_attachment(enc_data: bytes, local_key_b64: str, size: int = None) -> bytes:
    """Decrypts a Signal attachment using its localKey (AES-256-CBC + HMAC-SHA256)."""
    raw_key = base64.b64decode(local_key_b64)
    aes_key = raw_key[:32]
    mac_key = raw_key[32:]

    iv = enc_data[:16]
    ct = enc_data[16:-32]
    tag = enc_data[-32:]

    # Authenticate
    computed_tag = hmac.new(mac_key, enc_data[:-32], hashlib.sha256).digest()
    if computed_tag != tag:
        raise ValueError("HMAC verification failed for attachment.")

    # Decrypt AES-256-CBC
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    pad = padded[-1]
    plaintext = padded[:-pad]

    if size is not None and size <= len(plaintext):
        plaintext = plaintext[:size]
    return plaintext


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
    attach_root = os.path.join(appdata, "Signal", "attachments.noindex")

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

            # Determine destination filename
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

            # Ensure no illegal characters in filename
            out_name = "".join(c for c in out_name if c.isalnum() or c in (".", "-", "_", " ")).strip()
            out_path = os.path.join(chat_dir, out_name)

            with open(out_path, "wb") as out_f:
                out_f.write(plaintext)

            # Set file timestamp to sentAt if available
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
        work_db = copy_db_to_work_dir()
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
            print("  python signal_grep.py --list-chats")
            print("  python signal_grep.py --search \"recipe\"")
            print("  python signal_grep.py --list-media")
            print("  python signal_grep.py --export-media --output-dir \"./media\"")
            print("  python signal_grep.py --thread \"pervy\"")
            print("  python signal_grep.py --sql \"SELECT count(*) FROM messages;\"")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

