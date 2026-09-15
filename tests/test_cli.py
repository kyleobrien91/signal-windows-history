import io
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

import cli


class TestCLIPackage(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
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

        self.cur.execute("INSERT INTO conversations (id, name, type) VALUES ('c1', 'Alice', 'direct');")
        self.cur.execute("INSERT INTO messages (id, conversationId, sent_at, body, type) VALUES ('m1', 'c1', 1000000000000, 'hello world', 'incoming');")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_list_chats(self):
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cli.list_chats(self.cur)

        output = stdout_buf.getvalue()
        self.assertIn("Alice", output)
        self.assertIn("c1", output)

    def test_search_messages(self):
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cli.search_messages(self.cur, "world")

        output = stdout_buf.getvalue()
        self.assertIn("hello world", output)
        self.assertIn("Alice", output)

    def test_dump_thread(self):
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cli.dump_thread(self.cur, "c1")

        output = stdout_buf.getvalue()
        self.assertIn("hello world", output)

    def test_run_custom_sql(self):
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cli.run_custom_sql(self.cur, "SELECT count(*) FROM messages;")

        output = stdout_buf.getvalue()
        self.assertIn("1", output)

    def test_list_media_empty(self):
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cli.list_media(self.cur)

        output = stdout_buf.getvalue()
        self.assertIn("Total attachments found: 0", output)

    def test_export_media_no_attachments_stub(self):
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cli.export_media(self.cur, "/tmp/dummy_export")

        output = stdout_buf.getvalue()
        self.assertIn("No attachments found to export.", output)

    @patch("cli.open_db")
    @patch("cli.copy_db_snapshot")
    @patch("cli.get_signal_key")
    def test_main_dispatch_list_chats(self, mock_key, mock_snap, mock_open):
        mock_key.return_value = "dummy_key"
        mock_snap.return_value = "dummy_db_path"
        mock_open.return_value = (self.conn, self.cur)

        test_args = ["cli", "--list-chats"]
        stdout_buf = io.StringIO()

        with patch.object(sys, "argv", test_args), patch("sys.stdout", stdout_buf):
            cli.main()

        output = stdout_buf.getvalue()
        self.assertIn("Alice", output)

    @patch("cli.get_signal_key")
    def test_main_key_error_exits(self, mock_key):
        mock_key.side_effect = RuntimeError("Key error")

        test_args = ["cli", "--list-chats"]
        stderr_buf = io.StringIO()

        with patch.object(sys, "argv", test_args), patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Error extracting Signal key", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
