#!/usr/bin/env python3
"""
crypto/cache.py - Bounded, AES-GCM encrypted derived-media cache engine.

Provides dual-bounded L1 (in-memory) and L2 (encrypted disk) caching with
opaque HMAC keys, atomic writes, tamper detection, disposability, and
single-flight request deduplication under concurrency.
"""

import base64
from collections import OrderedDict
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from crypto.key_provider import get_cache_master_key


def encode_media_token(master_key: bytes, attachment_id: str, size: int = 0) -> str:
    """Encrypts attachment_id and size into an AES-GCM encrypted, opaque Base64 URL-safe token."""
    payload = json.dumps({"id": attachment_id, "sz": int(size)}, separators=(',', ':')).encode('utf-8')
    nonce = os.urandom(12)
    aesgcm = AESGCM(master_key)
    ciphertext = aesgcm.encrypt(nonce, payload, b"media_token")
    raw = nonce + ciphertext
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_media_token(master_key: bytes, token_str: str) -> Tuple[str, int]:
    """Decrypts and authenticates an opaque AES-GCM media token into (attachment_id, size)."""
    if not token_str:
        raise ValueError("Empty media token")
    try:
        padded_b64 = token_str + "=" * ((4 - len(token_str) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded_b64.encode('ascii'))
        if len(raw) < 28:
            raise ValueError("Media token payload too short")
        nonce = raw[:12]
        ciphertext = raw[12:]
        aesgcm = AESGCM(master_key)
        payload = aesgcm.decrypt(nonce, ciphertext, b"media_token")
        data = json.loads(payload.decode('utf-8'))
        return str(data["id"]), int(data.get("sz", 0))
    except Exception as e:
        raise ValueError(f"Invalid media token: {e}")


class SingleFlightEntry:

    def __init__(self):
        self.event = threading.Event()
        self.result: Optional[bytes] = None
        self.exception: Optional[Exception] = None


class DerivedMediaCache:
    """Bounded encrypted derived-media cache with L1 RAM and L2 disk storage."""

    def __init__(
        self,
        cache_dir: str,
        l1_max_items: int = 50,
        l1_max_bytes: int = 32 * 1024 * 1024,      # 32 MB
        l2_max_items: int = 1000,
        l2_max_bytes: int = 500 * 1024 * 1024,    # 500 MB
        test_master_key: Optional[bytes] = None,
    ):
        self.cache_dir = os.path.abspath(cache_dir)
        self.blobs_dir = os.path.join(self.cache_dir, "blobs")
        os.makedirs(self.blobs_dir, exist_ok=True)

        self.l1_max_items = l1_max_items
        self.l1_max_bytes = l1_max_bytes
        self.l2_max_items = l2_max_items
        self.l2_max_bytes = l2_max_bytes

        # Master key initialization
        self._master_key = get_cache_master_key(self.cache_dir, test_key=test_master_key)
        self._aesgcm = AESGCM(self._master_key)

        # L1 RAM cache state
        self._l1_cache: OrderedDict = OrderedDict()
        self._l1_bytes: int = 0
        self._l1_lock = threading.RLock()

        # L2 Database & Lock
        self.db_path = os.path.join(self.cache_dir, "index.db")
        self._db_lock = threading.RLock()
        self._init_db()
        self.reconcile_orphaned_blobs()

        # Single-flight request deduplication state
        self._single_flight_lock = threading.Lock()
        self._in_flight: Dict[str, "SingleFlightEntry"] = {}

    # ── Database Initialization & Connection ─────────────────────────

    def _reset_db(self):
        """Completely removes corrupt database and WAL files and re-initializes schema."""
        for ext in ("", "-wal", "-shm"):
            fpath = self.db_path + ext
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass

    def _ensure_schema(self, conn: sqlite3.Connection):
        """Ensures cache_entries schema and indexes exist."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_entries (
                cache_key TEXT PRIMARY KEY,
                attachment_key TEXT NOT NULL,
                derivative_type TEXT NOT NULL,
                generator_version INTEGER NOT NULL,
                parameters_json TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attachment ON cache_entries(attachment_key);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lru ON cache_entries(last_accessed_at);")

    def _is_corrupt_error(self, err: Exception) -> bool:
        """Returns True if exception indicates actual database image corruption."""
        msg = str(err).lower()
        return any(term in msg for term in ("corrupt", "malformed", "not a database", "file is not a database"))

    def _get_db_conn(self) -> sqlite3.Connection:
        """Returns SQLite connection to index DB, resetting database schema ONLY on actual corruption."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            cur = conn.cursor()
            cur.execute("PRAGMA quick_check;")
            res = cur.fetchone()
            if res and res[0] not in ("ok", "ok\n"):
                conn.close()
                raise sqlite3.DatabaseError(f"Integrity check failed: {res[0]}")
            self._ensure_schema(conn)
            return conn
        except sqlite3.DatabaseError as e:
            if self._is_corrupt_error(e):
                self._reset_db()
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                conn.execute("PRAGMA journal_mode = WAL;")
                self._ensure_schema(conn)
                return conn
            raise

    def _init_db(self):
        """Initializes SQLite cache index schema."""
        with self._db_lock:
            conn = self._get_db_conn()
            conn.close()

    # ── Key Derivation & Cryptography ─────────────────────────────────

    def encode_media_token(self, attachment_id: str, size: int = 0) -> str:
        """Encodes attachment_id into an opaque AES-GCM media token."""
        return encode_media_token(self._master_key, attachment_id, size)

    def decode_media_token(self, token_str: str, master_key: Optional[bytes] = None) -> Tuple[str, int]:
        """Decodes an opaque AES-GCM media token."""
        key = master_key if master_key is not None else self._master_key
        return decode_media_token(key, token_str)

    def derive_attachment_key(self, attachment_id: str) -> str:
        """Derives an opaque HMAC key for attachment indexing and invalidation."""
        h = hmac.new(self._master_key, f"att|{attachment_id}".encode('utf-8'), hashlib.sha256)
        return h.hexdigest()

    def derive_cache_key(
        self,
        attachment_id: str,
        derivative_type: str,
        generator_version: int,
        params: Optional[dict] = None,
    ) -> str:
        """Derives a cryptographically opaque HMAC cache key.

        Ensures raw attachment paths, filenames, or IDs never appear in cache keys, index DB, or filenames.
        """
        sorted_params = json.dumps(params or {}, sort_keys=True, separators=(',', ':'))
        canonical_str = f"{attachment_id}|{derivative_type}|{generator_version}|{sorted_params}"
        h = hmac.new(self._master_key, canonical_str.encode('utf-8'), hashlib.sha256)
        return h.hexdigest()

    def _encrypt_blob(self, cache_key: str, plaintext: bytes) -> bytes:
        """Encrypts plaintext bytes with AES-GCM, binding cache_key as AAD."""
        nonce = os.urandom(12)
        aad = cache_key.encode('utf-8')
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, aad)
        return nonce + ciphertext

    def _decrypt_blob(self, cache_key: str, encrypted_bytes: bytes) -> bytes:
        """Decrypts ciphertext bytes with AES-GCM, verifying nonce and AAD."""
        if len(encrypted_bytes) < 28: # 12 nonce + 16 tag
            raise ValueError("Encrypted blob too small")
        nonce = encrypted_bytes[:12]
        ciphertext = encrypted_bytes[12:]
        aad = cache_key.encode('utf-8')
        return self._aesgcm.decrypt(nonce, ciphertext, aad)

    # ── L1 RAM Cache Operations ──────────────────────────────────────

    def _l1_get(self, cache_key: str) -> Optional[bytes]:
        with self._l1_lock:
            if cache_key in self._l1_cache:
                self._l1_cache.move_to_end(cache_key)
                return self._l1_cache[cache_key]
            return None

    def _l1_put(self, cache_key: str, plaintext: bytes):
        item_len = len(plaintext)
        if item_len > self.l1_max_bytes:
            # Oversized entry skips L1 RAM cache completely
            return

        with self._l1_lock:
            if cache_key in self._l1_cache:
                old = self._l1_cache.pop(cache_key)
                self._l1_bytes -= len(old)

            while (
                self._l1_cache and (
                    len(self._l1_cache) >= self.l1_max_items or
                    self._l1_bytes + item_len > self.l1_max_bytes
                )
            ):
                _, evicted = self._l1_cache.popitem(last=False)
                self._l1_bytes -= len(evicted)

            self._l1_cache[cache_key] = plaintext
            self._l1_bytes += item_len

    def _l1_delete(self, cache_key: str):
        with self._l1_lock:
            if cache_key in self._l1_cache:
                old = self._l1_cache.pop(cache_key)
                self._l1_bytes -= len(old)

    # ── L2 Disk Cache & Eviction Operations ──────────────────────────

    def _enforce_l2_limits(self, conn: sqlite3.Connection):
        """Evicts oldest LRU entries if L2 item count or total bytes limit is exceeded."""
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM cache_entries;")
        row = cur.fetchone()
        count = row[0] if row else 0
        total_bytes = row[1] if row else 0

        while count > self.l2_max_items or total_bytes > self.l2_max_bytes:
            cur.execute("SELECT cache_key, size_bytes FROM cache_entries ORDER BY last_accessed_at ASC LIMIT 1;")
            victim = cur.fetchone()
            if not victim:
                break
            v_key, v_size = victim[0], victim[1]
            cur.execute("DELETE FROM cache_entries WHERE cache_key = ?;", (v_key,))

            blob_path = os.path.join(self.blobs_dir, f"{v_key}.bin")
            if os.path.exists(blob_path):
                try:
                    os.remove(blob_path)
                except Exception:
                    pass

            self._l1_delete(v_key)
            count -= 1
            total_bytes -= v_size

    # ── Public Cache API ──────────────────────────────────────────────

    def get(self, cache_key: str) -> Optional[bytes]:
        """Retrieves cached plaintext bytes for cache_key, checking L1 then L2."""
        # 1. Try L1 RAM cache
        l1_hit = self._l1_get(cache_key)
        if l1_hit is not None:
            return l1_hit

        # 2. Try L2 Disk cache
        blob_path = os.path.join(self.blobs_dir, f"{cache_key}.bin")
        if not os.path.exists(blob_path):
            return None

        now = time.time()
        with self._db_lock:
            try:
                conn = self._get_db_conn()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT attachment_key FROM cache_entries WHERE cache_key = ?;", (cache_key,))
                    row = cur.fetchone()
                    if not row:
                        # Orphaned blob file without DB entry
                        try:
                            os.remove(blob_path)
                        except Exception:
                            pass
                        return None

                    # Read encrypted blob
                    try:
                        with open(blob_path, "rb") as f:
                            encrypted_bytes = f.read()
                        plaintext = self._decrypt_blob(cache_key, encrypted_bytes)
                    except Exception:
                        # Blob file missing, tampered, or corrupt -> remove entry and return None (miss)
                        cur.execute("DELETE FROM cache_entries WHERE cache_key = ?;", (cache_key,))
                        conn.commit()
                        if os.path.exists(blob_path):
                            try:
                                os.remove(blob_path)
                            except Exception:
                                pass
                        return None

                    # Update LRU access timestamp
                    cur.execute("UPDATE cache_entries SET last_accessed_at = ? WHERE cache_key = ?;", (now, cache_key))
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as e:
                if self._is_corrupt_error(e):
                    self._reset_db()
                return None

        # Store in L1 RAM cache and return
        self._l1_put(cache_key, plaintext)
        return plaintext

    def put(
        self,
        cache_key: str,
        attachment_id: str,
        derivative_type: str,
        generator_version: int,
        params: dict,
        plaintext: bytes,
    ):
        """Encrypts and puts plaintext derivative bytes into L1 and L2 cache."""
        now = time.time()
        encrypted_bytes = self._encrypt_blob(cache_key, plaintext)
        enc_size = len(encrypted_bytes)

        if enc_size <= self.l2_max_bytes:
            # Atomic temp file write + atomic replace
            fd, tmp_path = tempfile.mkstemp(dir=self.blobs_dir, prefix="tmp_", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(encrypted_bytes)
                    f.flush()
                    os.fsync(f.fileno())

                final_path = os.path.join(self.blobs_dir, f"{cache_key}.bin")
                os.replace(tmp_path, final_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                raise

            # Insert/update in SQLite index using opaque attachment_key
            att_key = self.derive_attachment_key(attachment_id)
            params_json = json.dumps(params or {}, sort_keys=True)
            with self._db_lock:
                conn = self._get_db_conn()
                try:
                    with conn:
                        conn.execute("""
                            INSERT OR REPLACE INTO cache_entries
                            (cache_key, attachment_key, derivative_type, generator_version, parameters_json, size_bytes, created_at, last_accessed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                        """, (cache_key, att_key, derivative_type, generator_version, params_json, enc_size, now, now))
                        self._enforce_l2_limits(conn)
                finally:
                    conn.close()

        # Put into L1 RAM cache
        self._l1_put(cache_key, plaintext)

    def get_or_generate(
        self,
        attachment_id: str,
        derivative_type: str,
        generator_version: int,
        params: dict,
        generator_func: Callable[[], bytes],
    ) -> Tuple[str, bytes]:
        """Gets derivative from cache or deduplicates concurrent generation using explicit Result propagation.

        Returns (cache_key, plaintext_bytes).
        """
        cache_key = self.derive_cache_key(attachment_id, derivative_type, generator_version, params)

        # 1. Check existing cache hit
        hit = self.get(cache_key)
        if hit is not None:
            return cache_key, hit

        # 2. Concurrency single-flight deduplication with explicit Event/Future result
        is_leader = False
        entry = None

        with self._single_flight_lock:
            # Re-check cache under lock
            hit = self.get(cache_key)
            if hit is not None:
                return cache_key, hit

            if cache_key in self._in_flight:
                entry = self._in_flight[cache_key]
            else:
                entry = SingleFlightEntry()
                self._in_flight[cache_key] = entry
                is_leader = True

        if not is_leader:
            # Wait for leader thread to complete generation
            entry.event.wait()
            if entry.exception is not None:
                raise entry.exception
            if entry.result is not None:
                return cache_key, entry.result
            hit = self.get(cache_key)
            if hit is not None:
                return cache_key, hit
            raise RuntimeError("Generation finished without result")

        # Leader thread executes generation
        try:
            data = generator_func()
            self.put(cache_key, attachment_id, derivative_type, generator_version, params, data)
            entry.result = data
            return cache_key, data
        except Exception as err:
            entry.exception = err
            raise
        finally:
            with self._single_flight_lock:
                self._in_flight.pop(cache_key, None)
            entry.event.set()

    def delete(self, cache_key: str):
        """Deletes a specific cache entry from L1, L2, and disk."""
        self._l1_delete(cache_key)

        blob_path = os.path.join(self.blobs_dir, f"{cache_key}.bin")
        if os.path.exists(blob_path):
            try:
                os.remove(blob_path)
            except Exception:
                pass

        with self._db_lock:
            conn = self._get_db_conn()
            try:
                with conn:
                    conn.execute("DELETE FROM cache_entries WHERE cache_key = ?;", (cache_key,))
            finally:
                conn.close()

    def invalidate_attachment(self, attachment_id: str):
        """Invalidates all cached derivatives associated with attachment_id using opaque attachment_key."""
        att_key = self.derive_attachment_key(attachment_id)
        with self._db_lock:
            conn = self._get_db_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT cache_key FROM cache_entries WHERE attachment_key = ?;", (att_key,))
                keys = [r[0] for r in cur.fetchall()]

                for k in keys:
                    self._l1_delete(k)
                    blob_path = os.path.join(self.blobs_dir, f"{k}.bin")
                    if os.path.exists(blob_path):
                        try:
                            os.remove(blob_path)
                        except Exception:
                            pass

                with conn:
                    conn.execute("DELETE FROM cache_entries WHERE attachment_key = ?;", (att_key,))
            finally:
                conn.close()

    def reconcile_orphaned_blobs(self):
        """Reconciles disk blob files and SQLite index entries, removing orphans."""
        with self._db_lock:
            try:
                conn = self._get_db_conn()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT cache_key FROM cache_entries;")
                    indexed_keys = set(r[0] for r in cur.fetchall())

                    # 1. Purge orphaned blob files on disk with no DB index row
                    if os.path.exists(self.blobs_dir):
                        for fname in os.listdir(self.blobs_dir):
                            if fname.endswith(".bin"):
                                key = fname[:-4]
                                if key not in indexed_keys:
                                    try:
                                        os.remove(os.path.join(self.blobs_dir, fname))
                                    except Exception:
                                        pass

                    # 2. Purge stale index rows in DB with no blob file on disk
                    stale_keys = []
                    for key in indexed_keys:
                        blob_path = os.path.join(self.blobs_dir, f"{key}.bin")
                        if not os.path.exists(blob_path):
                            stale_keys.append(key)

                    if stale_keys:
                        with conn:
                            for sk in stale_keys:
                                conn.execute("DELETE FROM cache_entries WHERE cache_key = ?;", (sk,))
                finally:
                    conn.close()
            except sqlite3.Error as e:
                if self._is_corrupt_error(e):
                    self._reset_db()

    def clear(self):
        """Clears all L1 and L2 cache entries."""
        with self._l1_lock:
            self._l1_cache.clear()
            self._l1_bytes = 0

        # Remove all blob files
        if os.path.exists(self.blobs_dir):
            for fname in os.listdir(self.blobs_dir):
                fpath = os.path.join(self.blobs_dir, fname)
                try:
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                except Exception:
                    pass

        with self._db_lock:
            conn = self._get_db_conn()
            try:
                with conn:
                    conn.execute("DELETE FROM cache_entries;")
            finally:
                conn.close()

    def stats(self) -> dict:
        """Returns statistics on L1 and L2 cache usage."""
        with self._l1_lock:
            l1_items = len(self._l1_cache)
            l1_bytes = self._l1_bytes

        with self._db_lock:
            conn = self._get_db_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM cache_entries;")
                row = cur.fetchone()
                l2_items = row[0] if row else 0
                l2_bytes = row[1] if row else 0
            finally:
                conn.close()

        return {
            "l1_items": l1_items,
            "l1_bytes": l1_bytes,
            "l2_items": l2_items,
            "l2_bytes": l2_bytes,
        }
