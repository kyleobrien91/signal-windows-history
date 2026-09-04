# Signal Group Video Extraction, Deduplication & Re-Encryption Pipeline

This architecture specification outlines the complete end-to-end programmatic pipeline to discover, download, deduplicate (via perceptual hashing), and securely re-encrypt all group videos from Signal Desktop on Windows.

---

## Architecture Flowchart

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. DISCOVERY (SQLCipher Query)                                         │
│    - Find all group IDs in `conversations`                             │
│    - Inventory videos: downloaded vs. pending (path IS NULL)          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. DOWNLOAD TRIGGERING (CDP / DevTools Automation)                    │
│    - Iterate through group IDs                                         │
│    - Switch view to All Media -> Videos                                │
│    - Fast downward scroller mounts video items & triggers jobs         │
│    - Poll DB until pending_videos == 0                                 │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. DECRYPT & STAGE (AES-256-CBC + HMAC-SHA256)                         │
│    - Read blobs from %APPDATA%\Signal\attachments.noindex\             │
│    - Decrypt using localKey into temporary staging directory           │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. PERCEPTUAL VIDEO HASHING (pHash & OpenCV)                           │
│    - Sample 8-12 uniformly distributed keyframes per video             │
│    - Compute 64-bit DCT perceptual hash per frame                      │
│    - Construct composite temporal fingerprint                          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 5. DEDUPLICATION (Hamming Distance Analysis)                           │
│    - Compare fingerprints: Hamming distance <= threshold              │
│    - Identify duplicates (re-encoded, re-compressed, or identical)     │
│    - Retain highest resolution/bitrate master copy                     │
│    - Delete redundant copies & record dedup_manifest.json              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 6. SECURE VAULT RE-ENCRYPTION (AES-256-GCM / Argon2id)                 │
│    - Encrypt master videos using a user passphrase                     │
│    - Write to external storage / backup location away from Signal      │
│    - Securely wipe temporary staging files                             │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Step-by-Step Implementation Details

### Step 1: Discover Undownloaded Videos via SQL
```sql
SELECT 
    c.id AS conversationId,
    c.name AS groupName,
    COUNT(ma.messageId) AS total_videos,
    SUM(CASE WHEN ma.path IS NOT NULL AND ma.localKey IS NOT NULL THEN 1 ELSE 0 END) AS downloaded_videos,
    SUM(CASE WHEN ma.path IS NULL OR ma.pending = 1 THEN 1 ELSE 0 END) AS pending_videos
FROM conversations c
JOIN messages m ON m.conversationId = c.id
JOIN message_attachments ma ON ma.messageId = m.id
WHERE c.type = 'group' AND ma.contentType LIKE 'video/%'
GROUP BY c.id
ORDER BY pending_videos DESC;
```

### Step 2: Perceptual Video Hashing Algorithm (Python)
Standard SHA-256 hashing fails for deduplication because different users forward videos that undergo minor transcoding, re-compression, or metadata changes. Perceptual hashing (pHash) solves this by comparing visual frame structures:

```python
import cv2
import imagehash
from PIL import Image

def generate_video_phash(video_path, num_samples=8):
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        return []

    frame_indices = [int(i * total_frames / (num_samples + 1)) for i in range(1, num_samples + 1)]
    hashes = []

    for idx in frame_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb_frame)
            hashes.append(imagehash.phash(pil_img))
    
    cap.release()
    return hashes

def are_videos_duplicate(hashes1, hashes2, max_diff_per_frame=4):
    if len(hashes1) != len(hashes2) or not hashes1:
        return False
    diffs = [h1 - h2 for h1, h2 in zip(hashes1, hashes2)]
    avg_diff = sum(diffs) / len(diffs)
    return avg_diff <= max_diff_per_frame
```

### Step 3: Vault Re-Encryption Away from Signal
Re-encrypting with AES-256-GCM and a user passphrase ensures the files are decoupled from Windows DPAPI and Signal's key hierarchy:

```python
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

def encrypt_to_vault(input_file, output_file, passphrase: str):
    salt = os.urandom(16)
    kdf = Argon2id(salt=salt, length=32, iterations=2, lanes=4, memory_cost=65536)
    key = kdf.derive(passphrase.encode())

    with open(input_file, "rb") as f:
        plaintext = f.read()

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    with open(output_file, "wb") as f:
        # File header: Magic (4B) + Salt (16B) + Nonce (12B) + Ciphertext + Tag
        f.write(b"VAULT1" + salt + nonce + ciphertext)
```
