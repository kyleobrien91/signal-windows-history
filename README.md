# Signal Windows History Decryption & Querying

Toolkit and documentation for decrypting and searching local Signal Desktop SQLite databases on Windows, adapting the macOS workflow described in [techvomit.net/grepping-your-own-signal-history](https://techvomit.net/grepping-your-own-signal-history/).

## Quick Links
- **[Full Step-by-Step Tutorial (TUTORIAL.md)](TUTORIAL.md)**: Detailed breakdown of the Windows DPAPI + AES-256-GCM architecture, gotchas, and queries.
- **`signal-cli` / `python -m cli`**: Canonical CLI utility to unwrap the key, safely copy the DB, grep messages, and export media.
- **`signal-player` / `python -m player`**: Local video player server and browser interface.
- **[`scroll_all_media.js`](scroll_all_media.js)**: Highly efficient DevTools script for Signal's "All Media" view (`Ctrl+Shift+M`) to sweep through 100% media grids.
- **[`auto_download_media.js`](auto_download_media.js)**: Calibrated DevTools console script to auto-scroll the main group timeline.
- **[`extract_dom_structure.js`](extract_dom_structure.js)**: Diagnostic DevTools script to inspect live Signal DOM containers.

## Installation
Install as an editable package or standard package using `pip`:
```powershell
pip install -e .
```

*Note on legacy root scripts:* Legacy root scripts (`signal_grep.py`, `signal_player.py`, `signal_key.py`, `signal_db.py`, `signal_crypto.py`, `signal_meta.py`, `signal_headless_downloader.py`) remain available as thin compatibility shims that delegate 100% to the canonical package modules.

## Quick Start
```powershell
# 1. Print the raw SQLCipher decryption key
signal-cli --show-key
# Or: python -m cli --show-key

# 2. List all conversations and message counts
signal-cli --list-chats

# 3. Search messages across all chats
signal-cli --search "keyword"

# 4. Dump conversation transcript
signal-cli --thread "ContactName"

# 5. List all downloaded media
signal-cli --list-media

# 6. Export and decrypt all media to a folder
signal-cli --export-media --output-dir "C:\Users\user\Desktop\SignalMedia"

# 7. Launch local video player
signal-player --port 7788
# Or: python -m player --port 7788
```
