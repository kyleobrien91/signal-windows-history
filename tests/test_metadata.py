import json
import os
import shutil
import tempfile
import threading
import unittest

import metadata
import metadata.store as store


class TestMetadataPackage(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_meta_path = store._META_PATH
        store._META_PATH = os.path.join(self.test_dir, "test_meta.json")
        store._meta_data = {
            "last_session_timestamp": 0,
            "seen_message_ids": [],
            "annotations": {},
        }

    def tearDown(self):
        store._META_PATH = self.orig_meta_path
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_new_video_detection(self):
        store._meta_data = {
            "last_session_timestamp": 1000,
            "seen_message_ids": ["msg_old_1"],
            "annotations": {},
        }
        metadata.save_metadata()

        meta_old = metadata.get_meta("msg_old_2", sent_at_ms=800)
        self.assertFalse(meta_old["is_new"])

        meta_new = metadata.get_meta("msg_new_1", sent_at_ms=1200)
        self.assertTrue(meta_new["is_new"])

        metadata.mark_seen(["msg_new_1"])
        meta_after_seen = metadata.get_meta("msg_new_1", sent_at_ms=1200)
        self.assertFalse(meta_after_seen["is_new"])

    def test_legacy_format_migration(self):
        legacy_data = {
            "msg_legacy_1": {"favourite": True, "labels": ["fun", "meme"]},
            "msg_legacy_2": {"favourite": False, "labels": []},
        }
        with open(store._META_PATH, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        metadata.load_metadata()

        self.assertEqual(store._meta_data["last_session_timestamp"], 0)
        self.assertIn("msg_legacy_1", store._meta_data["seen_message_ids"])
        self.assertIn("msg_legacy_2", store._meta_data["seen_message_ids"])
        self.assertTrue(store._meta_data["annotations"]["msg_legacy_1"]["favourite"])
        self.assertEqual(store._meta_data["annotations"]["msg_legacy_1"]["labels"], ["fun", "meme"])

    def test_mark_seen_idempotent(self):
        store._meta_data = {
            "last_session_timestamp": 500,
            "seen_message_ids": [],
            "annotations": {},
        }
        metadata.mark_seen(["msg_1", "msg_2"])
        metadata.mark_seen(["msg_2", "msg_3"])

        self.assertEqual(store._meta_data["seen_message_ids"], ["msg_1", "msg_2", "msg_3"])

        with open(store._META_PATH, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["seen_message_ids"], ["msg_1", "msg_2", "msg_3"])

    def test_set_meta_and_all_labels(self):
        metadata.set_meta("msg_1", favourite=True, labels=["meme", "cool"])
        metadata.set_meta("msg_2", favourite=False, labels=["cool", "work"])

        labels = metadata.all_labels()
        self.assertEqual(labels, ["cool", "meme", "work"])

        meta1 = metadata.get_meta("msg_1")
        self.assertTrue(meta1["favourite"])
        self.assertEqual(meta1["labels"], ["meme", "cool"])

    def test_sync_status(self):
        metadata.set_sync_status(True, pending=5, initial=10)
        status = metadata.get_sync_status()
        self.assertTrue(status["is_running"])
        self.assertEqual(status["pending_count"], 5)
        self.assertEqual(status["total_initial"], 10)

        metadata.set_sync_status(False)
        status_after = metadata.get_sync_status()
        self.assertFalse(status_after["is_running"])

    def test_concurrent_lock_safety(self):
        errors = []

        def worker(idx):
            try:
                msg_id = f"msg_thread_{idx}"
                metadata.set_meta(msg_id, favourite=(idx % 2 == 0), labels=[f"label_{idx}"])
                metadata.mark_seen([msg_id])
                metadata.get_meta(msg_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)

    def test_meta_lock_reentrancy(self):
        """Explicit regression test verifying _meta_lock is re-entrant (RLock) and does not deadlock when re-acquired on the same thread."""
        with store._meta_lock:
            # Re-enter the lock by calling metadata methods that acquire _meta_lock internally
            metadata.set_meta("msg_reentrant_1", favourite=True, labels=["test_reentrant"])
            meta = metadata.get_meta("msg_reentrant_1")
            self.assertTrue(meta["favourite"])
            self.assertEqual(meta["labels"], ["test_reentrant"])
            metadata.save_metadata()


if __name__ == "__main__":
    unittest.main()
