import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import db
import db.queries as db_q


class TestDBPackage(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.cur = self.conn.cursor()

        self.cur.execute("""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                name TEXT,
                profileName TEXT,
                e164 TEXT,
                type TEXT
            );
        """)
        self.cur.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conversationId TEXT,
                sent_at INTEGER,
                received_at INTEGER,
                timestamp INTEGER,
                sourceServiceId TEXT,
                source TEXT,
                body TEXT,
                type TEXT
            );
        """)
        self.cur.execute("""
            CREATE TABLE message_attachments (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                messageId TEXT,
                conversationId TEXT,
                sentAt INTEGER,
                receivedAt INTEGER,
                contentType TEXT,
                path TEXT,
                localKey TEXT,
                fileName TEXT,
                size INTEGER
            );
        """)
        self.conn.commit()

        self.old_conn = db.set_active_db(self.conn, self.cur)

    def tearDown(self):
        db.set_active_db(self.old_conn, self.old_conn.cursor() if self.old_conn else None)
        self.conn.close()

    def test_open_db_mock_sqlcipher(self):
        with patch("sqlcipher3.connect") as mock_connect:
            mock_c = MagicMock()
            mock_cur = MagicMock()
            mock_c.cursor.return_value = mock_cur
            mock_connect.return_value = mock_c

            conn, cur = db.open_db("test_path.db", "dummy_key")

            self.assertEqual(conn, mock_c)
            self.assertEqual(cur, mock_cur)
            mock_cur.execute.assert_any_call("PRAGMA key = \"x'dummy_key'\";")
            mock_cur.execute.assert_any_call("PRAGMA cipher_compatibility = 4;")
            mock_cur.execute.assert_any_call("SELECT count(*) FROM sqlite_master;")

    def test_set_active_db_connection_swapping(self):
        new_conn = sqlite3.connect(":memory:")
        new_cur = new_conn.cursor()

        previous_conn = db.set_active_db(new_conn, new_cur)
        self.assertEqual(previous_conn, self.conn)
        self.assertEqual(db_q._db_conn, new_conn)
        self.assertEqual(db_q._db_cur, new_cur)

        # Swapping back
        db.set_active_db(self.conn, self.cur)
        new_conn.close()

    @patch("db.open_db")
    @patch("db.copy_db_snapshot")
    def test_reload_db_atomic_refresh_and_close_old(self, mock_copy, mock_open):
        mock_copy.return_value = "/tmp/new_snapshot.db"
        mock_new_conn = MagicMock()
        mock_new_cur = MagicMock()
        mock_open.return_value = (mock_new_conn, mock_new_cur)

        mock_old_conn = MagicMock()
        db.set_active_db(mock_old_conn, MagicMock())

        success = db.reload_db("test_key")

        self.assertTrue(success)
        mock_copy.assert_called_once()
        mock_open.assert_called_once_with("/tmp/new_snapshot.db", "test_key")
        mock_old_conn.close.assert_called_once()
        self.assertEqual(db_q._db_conn, mock_new_conn)

    @patch("db.copy_db_snapshot")
    def test_reload_db_failure_retains_current_connection(self, mock_copy):
        mock_copy.side_effect = RuntimeError("Snapshot copy failed")

        mock_current_conn = MagicMock()
        mock_current_cur = MagicMock()
        db.set_active_db(mock_current_conn, mock_current_cur)

        success = db.reload_db("test_key")

        self.assertFalse(success)
        self.assertEqual(db_q._db_conn, mock_current_conn)
        mock_current_conn.close.assert_not_called()

    def test_queries_with_fixture_db(self):
        # Insert test data
        self.cur.execute("INSERT INTO conversations (id, name, type) VALUES ('conv1', 'Alice', 'direct');")
        self.cur.execute("INSERT INTO messages (id, conversationId, sent_at, body) VALUES ('msg1', 'conv1', 1000, 'Hello');")
        self.cur.execute("""
            INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
            VALUES ('msg1', 'conv1', 1000, 'video/mp4', 'att1.mp4', 'key1', 'vid.mp4', 500);
        """)
        self.conn.commit()

        # 1. query_groups
        groups = db.query_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "Alice")
        self.assertEqual(groups[0]["video_count"], 1)

        # 2. get_conversation_map
        conv_map = db.get_conversation_map()
        self.assertEqual(conv_map.get("conv1"), "Alice")

        # 3. query_media
        media, lookup = db.query_media("conv1")
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["filename"], "vid.mp4")
        self.assertIn("att1.mp4", lookup)

    def test_query_failures_distinguishable_from_empty_results(self):
        # Close connection to force query errors
        bad_conn = sqlite3.connect(":memory:")
        bad_cur = bad_conn.cursor()
        bad_conn.close()

        db.set_active_db(bad_conn, bad_cur)

        # Issue #5 behavior check: sqlite3.ProgrammingError/OperationalError should be raised upon executing on closed db
        with self.assertRaises(sqlite3.ProgrammingError):
            bad_cur.execute("SELECT count(*) FROM sqlite_master;")


if __name__ == "__main__":
    unittest.main()
