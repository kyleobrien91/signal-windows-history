import json
import os
import shutil
import tempfile
import unittest

import signal_player


class TestSignalPlayerMetadata(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_meta_path = signal_player._META_PATH
        signal_player._META_PATH = os.path.join(self.test_dir, "test_meta.json")

    def tearDown(self):
        signal_player._META_PATH = self.orig_meta_path
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_new_video_detection(self):
        signal_player._meta_data = {
            "last_session_timestamp": 1000,
            "seen_message_ids": ["msg_old_1"],
            "annotations": {}
        }
        signal_player._save_metadata()

        meta_old = signal_player._get_meta("msg_old_2", sent_at_ms=800)
        self.assertFalse(meta_old["is_new"])

        meta_new = signal_player._get_meta("msg_new_1", sent_at_ms=1200)
        self.assertTrue(meta_new["is_new"])

        signal_player._mark_seen(["msg_new_1"])
        meta_after_seen = signal_player._get_meta("msg_new_1", sent_at_ms=1200)
        self.assertFalse(meta_after_seen["is_new"])

    def test_legacy_format_migration(self):
        legacy_data = {
            "msg_legacy_1": {"favourite": True, "labels": ["fun", "meme"]},
            "msg_legacy_2": {"favourite": False, "labels": []}
        }
        with open(signal_player._META_PATH, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        signal_player._load_metadata()

        self.assertEqual(signal_player._meta_data["last_session_timestamp"], 0)
        self.assertIn("msg_legacy_1", signal_player._meta_data["seen_message_ids"])
        self.assertIn("msg_legacy_2", signal_player._meta_data["seen_message_ids"])
        self.assertTrue(signal_player._meta_data["annotations"]["msg_legacy_1"]["favourite"])
        self.assertEqual(signal_player._meta_data["annotations"]["msg_legacy_1"]["labels"], ["fun", "meme"])

    def test_mark_seen_idempotent(self):
        signal_player._meta_data = {
            "last_session_timestamp": 500,
            "seen_message_ids": [],
            "annotations": {}
        }
        signal_player._mark_seen(["msg_1", "msg_2"])
        signal_player._mark_seen(["msg_2", "msg_3"])

        self.assertEqual(signal_player._meta_data["seen_message_ids"], ["msg_1", "msg_2", "msg_3"])

        with open(signal_player._META_PATH, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["seen_message_ids"], ["msg_1", "msg_2", "msg_3"])


if __name__ == "__main__":
    unittest.main()
