# Signal Windows History Decryption & Querying

Toolkit and documentation for decrypting and searching local Signal Desktop SQLite databases on Windows, adapting the macOS workflow described in [techvomit.net/grepping-your-own-signal-history](https://techvomit.net/grepping-your-own-signal-history/).

## Quick Links
- **[Full Step-by-Step Tutorial (TUTORIAL.md)](file:///w:/home/user/development/homelab/signal-windows-history/TUTORIAL.md)**: Detailed breakdown of the Windows DPAPI + AES-256-GCM architecture, gotchas, and queries.
- **[`signal_grep.py`](file:///w:/home/user/development/homelab/signal-windows-history/signal_grep.py)**: All-in-one CLI utility to unwrap the key, safely copy the DB, grep messages, and export media.
- **[`scroll_all_media.js`](file:///w:/home/user/development/homelab/signal-windows-history/scroll_all_media.js)**: Highly efficient DevTools script for Signal's "All Media" view (`Ctrl+Shift+M`) to sweep through 100% media grids.
- **[`auto_download_media.js`](file:///w:/home/user/development/homelab/signal-windows-history/auto_download_media.js)**: Calibrated DevTools console script to auto-scroll the main group timeline.

- **[`extract_dom_structure.js`](file:///w:/home/user/development/homelab/signal-windows-history/extract_dom_structure.js)**: Diagnostic DevTools script to inspect live Signal DOM containers.
- **[`signal_key.py`](file:///w:/home/user/development/homelab/signal-windows-history/signal_key.py)**: Minimal script to output only the 64-character raw SQLCipher key.


## Requirements
```powershell
pip install cryptography sqlcipher3
```

## Quick Start
```powershell
# 1. Print the raw SQLCipher decryption key
python signal_key.py

# 2. List all conversations and message counts
python signal_grep.py --list-chats

# 3. Search messages across all chats
python signal_grep.py --search "keyword"

# 4. Dump conversation transcript
python signal_grep.py --thread "ContactName"

# 5. List all downloaded media
python signal_grep.py --list-media

# 6. Export and decrypt all media to a folder
python signal_grep.py --export-media --output-dir "C:\Users\user\Desktop\SignalMedia"

```
