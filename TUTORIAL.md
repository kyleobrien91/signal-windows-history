# Grepping Your Own Signal History on Windows

## Quick Start — Signal Player (Video Library)

Browse, favourite, and label your Signal videos in a local browser UI.
Nothing is decrypted to disk — all streaming happens in RAM.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
signal-player --port 7788
# Or: python -m player --port 7788
# Opens http://127.0.0.1:7788 automatically
```

| Feature | How |
|---|---|
| Browse videos | Click a group in the left sidebar |
| Hover preview | Hover a card — video scrubs like YouTube |
| Play full video | Click any card |
| Navigate | `←` / `→` arrow keys inside the modal |
| Favourite | Click ♡ or press **F** in the modal |
| Add label | Type in the label box + **Enter** |
| Remove label | Click **×** on a chip |
| Filter by label / favourites | Left sidebar |
| Search | Filename, sender, or label text |

Metadata (favourites + labels) saved to `signal_player_meta.json` — messageIds and annotations only, no keys, no content.

---

A comprehensive, step-by-step guide to unwrapping your local Signal Desktop SQLCipher encryption key, safely copying the database, and querying or grepping your message history on Windows.


> **Attribution & Context:**  
> This guide adapts the macOS workflow detailed in Jayson Grace's article, [*Grepping Your Own Signal History on macOS*](https://techvomit.net/grepping-your-own-signal-history/), substituting macOS-specific tooling (Keychain, PBKDF2/AES-128-CBC) with Windows-specific architecture (DPAPI, Chromium OSCrypt AES-256-GCM, and NTFS concurrency considerations).

---

## 1. Architectural Differences: macOS vs. Windows

On both operating systems, Signal Desktop stores messages inside an SQLite database encrypted with **SQLCipher 4**. However, how Signal stores and protects the 64-character hex decryption key differs fundamentally across platforms:

| Component | macOS (from article) | Windows (this guide) |
| :--- | :--- | :--- |
| **Data Directory** | `~/Library/Application Support/Signal` | `%APPDATA%\Signal` (`C:\Users\<User>\AppData\Roaming\Signal`) |
| **Database Path** | `.../Signal/sql/db.sqlite` | `.../Signal/sql/db.sqlite` |
| **Secret Storage** | macOS Login Keychain (`Signal Safe Storage`) | Windows DPAPI (`CryptProtectData` / `CryptUnprotectData`) |
| **Key Hierarchy** | Single-stage unwrapping: Keychain password $\to$ PBKDF2 (`saltysalt`, 1003 iter) $\to$ AES-128-CBC unwraps `encryptedKey`. | Two-stage Chromium OSCrypt: `Local State` DPAPI blob $\to$ AES-256 Master Key $\to$ AES-256-GCM unwraps `encryptedKey`. |
| **Database Tooling** | `brew install sqlcipher` | `pip install sqlcipher3` (pre-built wheels) or DB Browser for SQLite |

### How Key Unwrapping Works on Windows

```
┌────────────────────────────────────────────────────────┐
│ %APPDATA%\Signal\Local State                           │
│ -> "os_crypt": { "encrypted_key": "DPAPI..." }         │
└──────────────────────────┬─────────────────────────────┘
                           │ Strip "DPAPI" header (5 bytes)
                           ▼
┌────────────────────────────────────────────────────────┐
│ Windows DPAPI (CryptUnprotectData via ctypes)          │
│ -> Decrypted 32-byte AES Master Key                    │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│ %APPDATA%\Signal\config.json                           │
│ -> "encryptedKey": "763130..." (hex for ASCII 'v10')   │
│    Bytes [0:3]   : 'v10' header                        │
│    Bytes [3:15]  : 12-byte Nonce / IV                  │
│    Bytes [15:-16]: Ciphertext                          │
│    Bytes [-16:]  : 16-byte GCM Auth Tag                │
└──────────────────────────┬─────────────────────────────┘
                           │ Decrypt with AES-256-GCM
                           ▼
┌────────────────────────────────────────────────────────┐
│ 64-Character Hex SQLCipher Key                         │
│ -> PRAGMA key = "x'<64-char-hex>'";                    │
└──────────────────────────┬─────────────────────────────┘
```

---

## 2. Prerequisites

Open PowerShell or Command Prompt on Windows and install the package:

```powershell
pip install -e .
```

Dependencies installed automatically include `cryptography`, `sqlcipher3`, and `websockets`.

*(Optional GUI)* If you prefer a visual database viewer, install DB Browser for SQLite:
```powershell
winget install DBBrowserForSQLite.DBBrowserForSQLite
```

---

## 3. Step 1: Extract the SQLCipher Key

You can use the package CLI entry point or python module execution:

```powershell
signal-cli --show-key
# Or: python -m cli --show-key
```

Or write a minimal Python snippet using the `crypto` package:

```python
from crypto import get_signal_key

key = get_signal_key()
print(f"Decrypted Key: {key}")
print(f"PRAGMA Key:    x'{key}'")
```

---

## 4. Step 2: Safely Copy the Database (Critical Windows Gotchas)

Signal Desktop keeps the live database locked and open in WAL (Write-Ahead Logging) mode. When copying the database, observe two critical Windows-specific caveats:

> [!WARNING]
> **Gotcha 1 — Do NOT copy `db.sqlite-shm`:**  
> The `-shm` (shared-memory index) file contains active process locks and memory addresses. If copied while Signal is running, any attempt to open `db.sqlite` will fail immediately with `sqlite3.OperationalError: database is locked`. Only copy `db.sqlite` and `db.sqlite-wal`.

> [!IMPORTANT]
> **Gotcha 2 — Use local NTFS storage (e.g. `%TEMP%`):**  
> Do not copy the database to a network drive, samba share, or WSL mount path (such as `\\wsl.localhost\...` or mapped drive `W:\`). SQLite WAL requires native OS shared-memory locking semantics that fail across network/cross-filesystem boundaries.

In PowerShell:
```powershell
$Src = "$env:APPDATA\Signal\sql"
$Work = "$env:TEMP\signal-work"

New-Item -ItemType Directory -Force -Path $Work | Out-Null
Copy-Item "$Src\db.sqlite" "$Work\"
if (Test-Path "$Src\db.sqlite-wal") {
    Copy-Item "$Src\db.sqlite-wal" "$Work\"
}
```

---

## 5. Step 3: Open & Authenticate with SQLCipher

### Method A: Using Python (`sqlcipher3`)
```python
import sqlcipher3

db_path = r"C:\Users\<User>\AppData\Local\Temp\signal-work\db.sqlite"
key = "91330144b0bd90fb..." # Your 64-char key

conn = sqlcipher3.connect(db_path)
cur = conn.cursor()

# Authenticate
cur.execute(f"PRAGMA key = \"x'{key}'\";")
cur.execute("PRAGMA cipher_compatibility = 4;")

# Verify decryption
cur.execute("SELECT count(*) FROM sqlite_master;")
print("Tables count:", cur.fetchone()[0])
```

### Method B: Using DB Browser for SQLite (GUI)
1. Open **DB Browser (SQLCipher)**.
2. Click **Open Database** $\to$ Navigate to `%TEMP%\signal-work\db.sqlite`.
3. In the encryption prompt:
   - **Password:** Change dropdown from *Passphrase* to **Raw key**.
   - **Encryption:** `SQLCipher 4.x`.
   - **Key:** Enter `0x` followed by your 64-character hex key (e.g., `0x91330144...`).
4. Click **OK** to browse all tables and schema graphically.

---

## 6. Step 4: Querying and Grepping Message History

The Signal schema primarily revolves around two core tables:
- `conversations`: Holds metadata for 1-on-1 and group chats (`id`, `name`, `profileName`, `e164`, `type`).
- `messages`: Holds message payloads (`id`, `conversationId`, `body`, `sent_at`, `type`, `hasAttachments`).

### 1. Identify Conversations & Activity Ranking
```sql
SELECT 
    c.id,
    COALESCE(c.name, c.profileName, c.e164, 'Unnamed') AS title,
    COUNT(m.id) AS total_messages,
    DATETIME(MAX(m.sent_at)/1000, 'unixepoch', 'localtime') AS last_active
FROM conversations c
LEFT JOIN messages m ON m.conversationId = c.id
GROUP BY c.id
ORDER BY total_messages DESC;
```

### 2. Search Messages Across All Threads (Grep)
```sql
SELECT 
    DATETIME(m.sent_at/1000, 'unixepoch', 'localtime') AS timestamp,
    COALESCE(c.name, c.profileName, 'Unnamed') AS chat,
    CASE m.type WHEN 'outgoing' THEN 'ME' WHEN 'incoming' THEN 'THEM' ELSE m.type END AS sender,
    m.body
FROM messages m
JOIN conversations c ON c.id = m.conversationId
WHERE m.body LIKE '%meeting%'
ORDER BY m.sent_at ASC;
```

### 3. Dump a Chronological Chat Transcript
```sql
SELECT 
    DATETIME(sent_at/1000, 'unixepoch', 'localtime') AS timestamp,
    CASE type WHEN 'outgoing' THEN 'ME' WHEN 'incoming' THEN 'THEM' ELSE type END AS sender,
    body
FROM messages 
WHERE conversationId = 'f0b9ddf1-dccd-46f1-9be6-23028038cf7a' 
  AND body IS NOT NULL
ORDER BY sent_at ASC;
```

---

## 7. Turnkey Automation: `signal-cli` (`python -m cli`)

The CLI utility automates DPAPI extraction, database copying, and searching:

*(Legacy root script `signal_grep.py` remains supported as a thin compatibility shim).*

### Show Decrypted Key
```powershell
signal-cli --show-key
```

### List All Conversations
```powershell
signal-cli --list-chats
```
```
CONVERSATION ID                          | MSGS   | LAST ACTIVE         | NAME / TITLE
------------------------------------------------------------------------------------------
f0b9ddf1-dccd-46f1-9be6-23028038cf7a     | 5      | 2026-09-03 15:37:27 | Signal (private)
06cd7514-e4f8-4cae-908e-b69616214eab     | 3      | 2026-09-03 15:41:58 | Alice (private)
```

### Search Messages
```powershell
signal-cli --search "dinner"
```
```
--- Search results for 'dinner' (1 matches) ---
[2026-09-03 15:39:45] [Alice] THEM: Italian place for dinner sounds great!
```

### Filter Search by Contact
```powershell
signal-cli --search "dinner" --chat "Alice"
```

### Dump Full Conversation Transcript
```powershell
signal-cli --thread "Alice"
```

### Execute Arbitrary SQL
```powershell
signal-cli --sql "SELECT count(*) FROM messages WHERE hasAttachments = 1;"
```

---

## 8. Accessing Downloaded Media & Attachments

When you send or receive photos, videos, voice memos, documents, or stickers in Signal Desktop, the binary files are downloaded to:
```
%APPDATA%\Signal\attachments.noindex\
%APPDATA%\Signal\stickers.noindex\
```

### Why You Cannot Directly Open Media Files
If you browse into `%APPDATA%\Signal\attachments.noindex\`, you will see sharded folders (e.g., `1e\`, `35\`, `c4\`) containing files named with 64-character hex hashes without file extensions.

Attempting to rename these files to `.jpg` or `.mp4` will fail because **Signal Desktop encrypts every attachment at rest individually** with a separate per-attachment cryptographic key.

### Attachment Cryptographic Architecture

Each attachment corresponds to a row in the `message_attachments` table of the SQLCipher database:
- `path`: The relative path inside `attachments.noindex` (e.g. `35\35a4acb...`).
- `contentType`: MIME type (e.g. `image/jpeg`, `video/mp4`, `audio/aac`).
- `size`: The exact original byte length of the file.
- `fileName`: Original filename (if set).
- `localKey`: An 88-character base64-encoded string (64 bytes raw).

```
┌────────────────────────────────────────────────────────┐
│ localKey (64 bytes decoded)                            │
│ ├─ Bytes [0:32] : AES-256 Decryption Key               │
│ └─ Bytes [32:64]: HMAC-SHA256 Authentication Key       │
└────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────┐
│ Encrypted File on Disk (attachments.noindex\<path>)    │
│ ├─ Bytes [0:16]      : 16-byte AES-CBC Initialization  │
│ │                      Vector (IV)                     │
│ ├─ Bytes [16:-32]    : AES-256-CBC Ciphertext          │
│ └─ Bytes [-32:]      : 32-byte HMAC-SHA256 Auth Tag    │
└────────────────────────────────────────────────────────┘
```

**Decryption Procedure:**
1. Compute `HMAC-SHA256(mac_key, data[:-32])` and verify it matches the trailing 32 bytes.
2. Decrypt `data[16:-32]` using **AES-256-CBC** with `IV = data[:16]`.
3. Strip standard PKCS7 padding.
4. Truncate plaintext to `plaintext[:size]` (Signal applies padding blocks for traffic analysis resistance; the exact original size is recorded in the `size` column).

---

#### Method A: Using `auto_download_media.js` (Calibrated DevTools Auto-Downloader)

Signal's timeline messages are rendered inside `main.module-timeline__messages__container` with lazy-loaded attachment wrappers (`.module-message__attachment-container`).

We provide [`auto_download_media.js`](file:///w:/home/user/development/homelab/signal-windows-history/auto_download_media.js) which:
1. Targets `main.module-timeline__messages__container`.
2. Automatically sweeps from bottom to top (handling pagination of older messages) and back down.
3. Automatically brings every `.module-message__attachment-container` into focus.
4. Programmatically clicks any explicit download buttons that require manual confirmation.
5. Displays a live floating HUD directly inside the Signal UI showing scroll progress, media items seen, and a Stop button.

**Usage:**
1. Open Signal Desktop with DevTools:
   ```powershell
   Start-Process "$env:LOCALAPPDATA\Programs\signal-desktop\Signal.exe" -ArgumentList "--enable-dev-tools"
   ```
2. Navigate to your group chat.
3. Press **`Ctrl + Shift + I`** $\to$ go to the **Console** tab $\to$ paste the contents of [`auto_download_media.js`](file:///w:/home/user/development/homelab/signal-windows-history/auto_download_media.js) and hit **Enter**.

#### Method B: Using `signal-cli` (Automated)

1. **List all downloaded media with metadata:**
   ```powershell
   signal-cli --list-media
   ```
   **Output:**
   ```text
   TIMESTAMP           | CHAT               | TYPE             | SIZE      | FILENAME / PATH
   ------------------------------------------------------------------------------------------
   2026-09-03 15:37:27 | Signal             | image/jpeg       | 110,052 B | bb\bbaf2c0b2e6f...
   2026-09-03 15:41:58 | Alice              | image/jpeg       | 386,155 B | signal-2026-09-03-154158.jpeg
   ```

2. **Export and decrypt all media to an output folder:**
   ```powershell
   signal-cli --export-media --output-dir "C:\Users\<User>\Desktop\SignalMedia"
   ```
   This automatically:
   - Verifies HMAC authenticity for every file.
   - Decrypts the binary payload to its original format.
   - Organizes exported files into subfolders by contact or group name.
   - Restores original filenames (or generates `<timestamp>_<msgId>.<ext>` from MIME type).
   - Restores the original timestamp (`mtime`) on the exported files.

3. **Filter export by specific contact or thread:**
   ```powershell
   signal-cli --export-media --chat "Alice" --output-dir "./alice_media"
   ```

#### Method C: Standalone Python Decryption Function

If you are writing your own script, you can decrypt any Signal attachment using the `crypto` package:

```python
from crypto import decrypt_attachment

with open("path/to/encrypted/file", "rb") as f:
    enc_bytes = f.read()

plaintext = decrypt_attachment(enc_bytes, local_key_b64, original_size)
```
