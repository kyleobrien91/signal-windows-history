import os
import secrets
import tempfile
import threading
import time
import unittest

from crypto.cache import DerivedMediaCache
from crypto.key_provider import get_cache_master_key


class TestDerivedMediaCache(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = os.path.join(self.tmp_dir.name, "cache")
        self.test_master_key = secrets.token_bytes(32)
        self.cache = DerivedMediaCache(
            cache_dir=self.cache_dir,
            l1_max_items=5,
            l1_max_bytes=1000,    # 1000 bytes L1 limit
            l2_max_items=10,
            l2_max_bytes=5000,    # 5000 bytes L2 limit
            test_master_key=self.test_master_key,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cache_keys_are_cryptographically_opaque(self):
        att_id = "sensitive/path/to/signal_attachment_12345.mp4"
        key1 = self.cache.derive_cache_key(att_id, "poster", 1, {"w": 320, "h": 180})
        key2 = self.cache.derive_cache_key(att_id, "poster", 1, {"w": 320, "h": 180})
        key3 = self.cache.derive_cache_key(att_id, "poster", 2, {"w": 320, "h": 180})  # different version
        key4 = self.cache.derive_cache_key(att_id, "poster", 1, {"w": 640, "h": 360})  # different params

        # Opaque hex length check (SHA-256 = 64 hex chars)
        self.assertEqual(len(key1), 64)
        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, key3)
        self.assertNotEqual(key1, key4)

        # Ensure raw identifiers do NOT appear in the key
        self.assertNotIn("sensitive", key1)
        self.assertNotIn("attachment", key1)
        self.assertNotIn("12345", key1)

    def test_aes_gcm_encrypted_at_rest_and_opaque_filenames(self):
        att_id = "attachment_001"
        key = self.cache.derive_cache_key(att_id, "poster", 1, {})
        plaintext = b"GIF89a_fake_image_header_and_data_content_here"

        self.cache.put(key, att_id, "poster", 1, {}, plaintext)

        blob_path = os.path.join(self.cache.blobs_dir, f"{key}.bin")
        self.assertTrue(os.path.exists(blob_path))

        # Raw file content must NOT contain plaintext image header or data
        with open(blob_path, "rb") as f:
            encrypted_data = f.read()

        self.assertNotIn(b"GIF89a", encrypted_data)
        self.assertNotIn(b"fake_image", encrypted_data)

        # Retrieval decrypts correctly
        retrieved = self.cache.get(key)
        self.assertEqual(retrieved, plaintext)

    def test_tamper_rejection_and_cross_entry_aad_substitution(self):
        att1 = "att_001"
        att2 = "att_002"
        key1 = self.cache.derive_cache_key(att1, "poster", 1, {})
        key2 = self.cache.derive_cache_key(att2, "poster", 1, {})

        data1 = b"Data for attachment 1"
        data2 = b"Data for attachment 2"

        self.cache.put(key1, att1, "poster", 1, {}, data1)
        self.cache.put(key2, att2, "poster", 1, {}, data2)

        blob1_path = os.path.join(self.cache.blobs_dir, f"{key1}.bin")
        blob2_path = os.path.join(self.cache.blobs_dir, f"{key2}.bin")

        # 1. Test bit tampering in blob1
        with open(blob1_path, "rb") as f:
            raw_b1 = bytearray(f.read())
        raw_b1[-1] ^= 0xFF  # Corrupt last byte
        with open(blob1_path, "wb") as f:
            f.write(raw_b1)

        # Evict from L1 to force L2 disk read
        self.cache._l1_delete(key1)
        hit1 = self.cache.get(key1)
        self.assertIsNone(hit1)  # Tampered blob must result in cache miss

        # 2. Test cross-entry ciphertext substitution (copying blob2 file onto blob1 filename)
        with open(blob2_path, "rb") as f:
            raw_b2 = f.read()
        with open(blob1_path, "wb") as f:
            f.write(raw_b2)

        self.cache._l1_delete(key1)
        hit_substituted = self.cache.get(key1)
        self.assertIsNone(hit_substituted)  # AAD mismatch must reject substituted ciphertext

    def test_dual_bounded_l1_eviction(self):
        # L1 capacity in setup: 5 items, 1000 bytes
        items = []
        for i in range(10):
            att = f"att_{i}"
            k = self.cache.derive_cache_key(att, "poster", 1, {})
            # Each item is 300 bytes -> 4 items = 1200 bytes > 1000 byte limit
            data = bytes([i]) * 300
            self.cache.put(k, att, "poster", 1, {}, data)
            items.append((k, data))

        stats = self.cache.stats()
        # L1 item count should be <= 3 (since 3 * 300 = 900 <= 1000 bytes)
        self.assertLessEqual(stats["l1_items"], 3)
        self.assertLessEqual(stats["l1_bytes"], 1000)

    def test_oversized_item_handling(self):
        # Put item larger than L1 limit (1000 bytes) -> e.g. 1500 bytes
        att = "att_large"
        k = self.cache.derive_cache_key(att, "poster", 1, {})
        large_data = b"X" * 1500

        self.cache.put(k, att, "poster", 1, {}, large_data)

        # Should NOT be in L1 RAM cache
        self.assertIsNone(self.cache._l1_get(k))

        # Should still be retrievable from L2 Disk
        retrieved = self.cache.get(k)
        self.assertEqual(retrieved, large_data)

    def test_disposability_and_deletion_recovery(self):
        att = "att_disp"
        k = self.cache.derive_cache_key(att, "poster", 1, {})
        data = b"Disposable cache data"

        self.cache.put(k, att, "poster", 1, {}, data)
        self.assertEqual(self.cache.get(k), data)

        # Clear/Delete cache entirely
        self.cache.clear()

        # Cache miss
        self.assertIsNone(self.cache.get(k))

        # Application can re-put data without error
        self.cache.put(k, att, "poster", 1, {}, data)
        self.assertEqual(self.cache.get(k), data)

    def test_invalidation_by_attachment_id(self):
        att = "att_to_invalidate"
        k1 = self.cache.derive_cache_key(att, "poster", 1, {})
        k2 = self.cache.derive_cache_key(att, "preview", 1, {})

        self.cache.put(k1, att, "poster", 1, {}, b"Poster data")
        self.cache.put(k2, att, "preview", 1, {}, b"Preview data")

        self.assertIsNotNone(self.cache.get(k1))
        self.assertIsNotNone(self.cache.get(k2))

        self.cache.invalidate_attachment(att)

        self.assertIsNone(self.cache.get(k1))
        self.assertIsNone(self.cache.get(k2))

    def test_single_flight_concurrency_and_error_recovery(self):
        att = "att_concurrent"
        gen_count = 0
        gen_lock = threading.Lock()

        def slow_generator():
            nonlocal gen_count
            time.sleep(0.1)
            with gen_lock:
                gen_count += 1
            return b"Generated image result"

        threads = []
        results = [None] * 5

        def worker(idx):
            _, res = self.cache.get_or_generate(att, "poster", 1, {}, slow_generator)
            results[idx] = res

        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Check all 5 threads got the exact result
        for res in results:
            self.assertEqual(res, b"Generated image result")

        # Check generator was invoked EXACTLY ONCE
        self.assertEqual(gen_count, 1)

        # Test single-flight failure cleanup
        fail_key = self.cache.derive_cache_key("att_fail", "poster", 1, {})

        def failing_generator():
            raise RuntimeError("Extraction failed")

        with self.assertRaises(RuntimeError):
            self.cache.get_or_generate("att_fail", "poster", 1, {}, failing_generator)

        # In-flight lock state for fail_key must be cleared
        self.assertNotIn(fail_key, self.cache._in_flight)

    def test_sqlite_cache_index_uses_opaque_attachment_key(self):
        att_id = "sensitive/signal/attachment_999.mp4"
        key = self.cache.derive_cache_key(att_id, "poster", 1, {})
        self.cache.put(key, att_id, "poster", 1, {}, b"some_data")

        import sqlite3
        conn = sqlite3.connect(self.cache.db_path)
        cur = conn.cursor()
        cur.execute("SELECT cache_key, attachment_key FROM cache_entries WHERE cache_key = ?;", (key,))
        row = cur.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        stored_att_key = row[1]
        self.assertNotIn("sensitive", stored_att_key)
        self.assertNotIn("signal", stored_att_key)
        self.assertNotIn("999", stored_att_key)
        self.assertEqual(stored_att_key, self.cache.derive_attachment_key(att_id))

    def test_sqlite_db_corruption_recovery(self):
        att_id = "att_corrupt"
        key = self.cache.derive_cache_key(att_id, "poster", 1, {})
        self.cache.put(key, att_id, "poster", 1, {}, b"data_before_corruption")

        # Corrupt the index.db file by writing garbage
        with open(self.cache.db_path, "wb") as f:
            f.write(b"CORRUPT_SQLITE_GARBAGE_HEADER_DATA_12345")

        # Getting item with corrupt DB must recover by resetting DB and returning None (miss)
        self.cache._l1_delete(key)
        hit = self.cache.get(key)
        self.assertIsNone(hit)

        # Cache should still be operational for new writes
        self.cache.put(key, att_id, "poster", 1, {}, b"data_after_recovery")
        self.assertEqual(self.cache.get(key), b"data_after_recovery")

    def test_non_windows_key_provider_ephemeral_safety(self):
        # Non-Windows key provider must return a 32-byte key without writing plaintext file
        key_dir = tempfile.mkdtemp()
        master_key = get_cache_master_key(key_dir)
        self.assertEqual(len(master_key), 32)
        plaintext_key_file = os.path.join(key_dir, "cache_master.key")
        self.assertFalse(os.path.exists(plaintext_key_file))

    def test_media_token_aes_gcm_opacity(self):
        att_id = "sensitive/path/to/signal_media.mp4"
        token = self.cache.encode_media_token(att_id, 1024)

        # Token must NOT contain raw attachment path in plain text
        self.assertNotIn("sensitive", token)
        self.assertNotIn("signal_media", token)

        # Decoding token with correct master key succeeds
        decoded_id, decoded_sz = self.cache.decode_media_token(token)
        self.assertEqual(decoded_id, att_id)
        self.assertEqual(decoded_sz, 1024)

        # Decoding token with wrong master key fails
        wrong_key = secrets.token_bytes(32)
        with self.assertRaises(ValueError):
            self.cache.decode_media_token(token, master_key=wrong_key)

    def test_content_key_change_invalidates_cache(self):
        att_id = "att_content_change"
        params_v1 = {'width': 320, 'height': 180, 'format': 'webp', 'quality': 80, 'size': 1024, 'src_key': 'key_v1'}
        params_v2 = {'width': 320, 'height': 180, 'format': 'webp', 'quality': 80, 'size': 1024, 'src_key': 'key_v2'}

        key1 = self.cache.derive_cache_key(att_id, "poster", 1, params_v1)
        key2 = self.cache.derive_cache_key(att_id, "poster", 1, params_v2)

        # Changing src_key must result in different derived cache key
        self.assertNotEqual(key1, key2)

    def test_operational_error_does_not_reset_db(self):
        import sqlite3
        # OperationalError (e.g. database is locked) must NOT trigger _reset_db
        err = sqlite3.OperationalError("database is locked")
        self.assertFalse(self.cache._is_corrupt_error(err))

        corrupt_err = sqlite3.DatabaseError("database disk image is malformed")
        self.assertTrue(self.cache._is_corrupt_error(corrupt_err))

    def test_sqlite_integrity_check_failure_triggers_reset(self):
        import sqlite3
        from unittest.mock import patch, MagicMock

        att_id = "att_integrity_test"
        key = self.cache.derive_cache_key(att_id, "poster", 1, {})
        self.cache.put(key, att_id, "poster", 1, {}, b"integrity_data")

        # Mock connection returning integrity check failure
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("index cache_entries_idx is inconsistent",)
        mock_conn.cursor.return_value = mock_cur

        with patch("sqlite3.connect", return_value=mock_conn):
            with patch.object(self.cache, "_reset_db", wraps=self.cache._reset_db) as mock_reset:
                conn = self.cache._get_db_conn()
                mock_reset.assert_called_once()

    def test_sqlite_operational_error_locked_does_not_reset(self):
        import sqlite3
        from unittest.mock import patch

        def mock_connect(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch("sqlite3.connect", side_effect=mock_connect):
            with patch.object(self.cache, "_reset_db") as mock_reset:
                with self.assertRaises(sqlite3.OperationalError):
                    self.cache._get_db_conn()
                mock_reset.assert_not_called()

    def test_sqlite_cache_index_does_not_contain_raw_localkey(self):
        att_id = "att_secret_key_test"
        raw_key = "super_secret_signal_localkey_xyz987"
        params = {'width': 320, 'height': 180, 'src_key': raw_key}

        key = self.cache.derive_cache_key(att_id, "poster", 1, params)
        self.cache.put(key, att_id, "poster", 1, params, b"test_bytes")

        import sqlite3
        conn = sqlite3.connect(self.cache.db_path)
        cur = conn.cursor()
        cur.execute("SELECT parameters_json FROM cache_entries WHERE cache_key = ?;", (key,))
        row = cur.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        stored_json = row[0]

        # Raw localKey string MUST NOT appear in SQLite parameters_json
        self.assertNotIn(raw_key, stored_json)
        self.assertNotIn("super_secret", stored_json)
        self.assertIn("src_key_hash", stored_json)


if __name__ == "__main__":
    unittest.main()
