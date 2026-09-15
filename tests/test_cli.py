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

    def test_export_media_no_attachments(self):
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            result = cli.export_media(self.cur, "/tmp/dummy_export")

        output = stdout_buf.getvalue()
        self.assertTrue(result)
        self.assertIn("No attachments found to export.", output)

    def test_export_media_success(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            enc_dir = os.path.join(tmpdir, "enc")
            os.makedirs(enc_dir, exist_ok=True)
            enc_file = os.path.join(enc_dir, "enc.bin")
            with open(enc_file, "wb") as f:
                f.write(b"encrypted_data")

            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES ('msg00001', 'c1', 1000000000000, 'image/jpeg', 'enc.bin', 'key123', 'photo.jpg', 14);
            """)
            self.conn.commit()

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with patch.dict(os.environ, {"APPDATA": tmpdir}), \
                 patch("cli.os.path.join", side_effect=lambda *args: os.path.sep.join(args) if args[0] != tmpdir or len(args) != 3 or args[1] != "Signal" else os.path.join(tmpdir, "enc", args[2])), \
                 patch("cli.decrypt_attachment", return_value=b"decrypted_photo"), \
                 patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                # Put file directly in mock APPDATA/Signal/attachments.noindex path
                attach_root = os.path.join(tmpdir, "Signal", "attachments.noindex")
                os.makedirs(attach_root, exist_ok=True)
                with open(os.path.join(attach_root, "enc.bin"), "wb") as f:
                    f.write(b"encrypted_data")

                out_dir = os.path.join(tmpdir, "out")
                res = cli.export_media(self.cur, out_dir)

            self.assertTrue(res)
            self.assertEqual(stderr_buf.getvalue(), "")
            exported_file = os.path.join(out_dir, "Alice", "photo.jpg")
            self.assertTrue(os.path.exists(exported_file))
            with open(exported_file, "rb") as f:
                self.assertEqual(f.read(), b"decrypted_photo")

    def test_export_media_missing_file(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES ('msg_missing_123', 'c1', 1000000000000, 'image/jpeg', 'nonexistent.bin', 'key123', 'photo.jpg', 14);
            """)
            self.conn.commit()

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with patch.dict(os.environ, {"APPDATA": tmpdir}), \
                 patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                out_dir = os.path.join(tmpdir, "out")
                res = cli.export_media(self.cur, out_dir)

            self.assertFalse(res)
            self.assertIn("Error: Missing attachment file 'nonexistent.bin' for message 'msg_missing_123'", stderr_buf.getvalue())

    def test_export_media_decryption_failure(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            attach_root = os.path.join(tmpdir, "Signal", "attachments.noindex")
            os.makedirs(attach_root, exist_ok=True)
            with open(os.path.join(attach_root, "bad_enc.bin"), "wb") as f:
                f.write(b"corrupt")

            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES ('msg_bad_dec', 'c1', 1000000000000, 'image/jpeg', 'bad_enc.bin', 'key123', 'photo.jpg', 7);
            """)
            self.conn.commit()

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with patch.dict(os.environ, {"APPDATA": tmpdir}), \
                 patch("cli.decrypt_attachment", side_effect=ValueError("Bad key")), \
                 patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                out_dir = os.path.join(tmpdir, "out")
                res = cli.export_media(self.cur, out_dir)

            self.assertFalse(res)
            self.assertIn("Error decrypting attachment 'bad_enc.bin' for message 'msg_bad_dec': Bad key", stderr_buf.getvalue())

    def test_export_media_collision_resolution_and_content_unmodified(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            attach_root = os.path.join(tmpdir, "Signal", "attachments.noindex")
            os.makedirs(attach_root, exist_ok=True)

            with open(os.path.join(attach_root, "enc1.bin"), "wb") as f:
                f.write(b"enc1")
            with open(os.path.join(attach_root, "enc2.bin"), "wb") as f:
                f.write(b"enc2")

            mid1 = "msg_1234567890abcdef_1"
            mid2 = "msg_1234567890abcdef_2"

            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES (?, 'c1', 1000000000000, 'application/x-tar', 'enc1.bin', 'key1', 'archive.tar.gz', 4);
            """, (mid1,))
            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES (?, 'c1', 1000000000000, 'application/x-tar', 'enc2.bin', 'key2', 'archive.tar.gz', 4);
            """, (mid2,))
            self.conn.commit()

            out_dir = os.path.join(tmpdir, "out")
            chat_dir = os.path.join(out_dir, "Alice")
            os.makedirs(chat_dir, exist_ok=True)

            # Pre-create archive.tar.gz with existing content
            existing_path = os.path.join(chat_dir, "archive.tar.gz")
            with open(existing_path, "wb") as f:
                f.write(b"PRE_EXISTING_CONTENT")

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            def mock_decrypt(data, key, size):
                if data == b"enc1":
                    return b"NEW_DECRYPTED_1"
                return b"NEW_DECRYPTED_2"

            with patch.dict(os.environ, {"APPDATA": tmpdir}), \
                 patch("cli.decrypt_attachment", side_effect=mock_decrypt), \
                 patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                res = cli.export_media(self.cur, out_dir)

            self.assertTrue(res)
            # Verify pre-existing file content is completely UNCHANGED
            with open(existing_path, "rb") as f:
                self.assertEqual(f.read(), b"PRE_EXISTING_CONTENT")

            # First attachment (mid1) collides with archive.tar.gz -> archive_msg_1234.tar.gz
            suffixed_path_1 = os.path.join(chat_dir, f"archive_{mid1[:8]}.tar.gz")
            self.assertTrue(os.path.exists(suffixed_path_1))
            with open(suffixed_path_1, "rb") as f:
                self.assertEqual(f.read(), b"NEW_DECRYPTED_1")

            # Second attachment (mid2) collides with archive.tar.gz -> archive_msg_1234.tar.gz (if mid2 has same prefix)
            # mid2[:8] is also msg_1234, but archive_msg_1234.tar.gz will be occupied by mid1.
            # So mid2 will try candidate mid2[:16] -> archive_msg_1234567890ab.tar.gz
            suffixed_path_2 = os.path.join(chat_dir, f"archive_{mid2[:16]}.tar.gz")
            self.assertTrue(os.path.exists(suffixed_path_2))
            with open(suffixed_path_2, "rb") as f:
                self.assertEqual(f.read(), b"NEW_DECRYPTED_2")

    def test_export_media_output_dir_creation_failure(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES ('msg00001', 'c1', 1000000000000, 'image/jpeg', 'enc.bin', 'key123', 'photo.jpg', 14);
            """)
            self.conn.commit()

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with patch("cli.os.makedirs", side_effect=PermissionError("Permission denied")), \
                 patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                res = cli.export_media(self.cur, os.path.join(tmpdir, "invalid_out"))

            self.assertFalse(res)
            self.assertIn("Error creating output directory", stderr_buf.getvalue())

    def test_export_media_utime_failure(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            attach_root = os.path.join(tmpdir, "Signal", "attachments.noindex")
            os.makedirs(attach_root, exist_ok=True)
            with open(os.path.join(attach_root, "enc.bin"), "wb") as f:
                f.write(b"enc")

            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES ('msg_utime_fail', 'c1', 1000000000000, 'image/jpeg', 'enc.bin', 'key123', 'photo.jpg', 3);
            """)
            self.conn.commit()

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with patch.dict(os.environ, {"APPDATA": tmpdir}), \
                 patch("cli.decrypt_attachment", return_value=b"decrypted"), \
                 patch("os.utime", side_effect=OSError("utime failed")), \
                 patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                out_dir = os.path.join(tmpdir, "out")
                res = cli.export_media(self.cur, out_dir)

            self.assertFalse(res)
            self.assertIn("Error setting timestamp on attachment 'enc.bin' for message 'msg_utime_fail': utime failed", stderr_buf.getvalue())

    def test_export_media_candidate_exhaustion(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            attach_root = os.path.join(tmpdir, "Signal", "attachments.noindex")
            os.makedirs(attach_root, exist_ok=True)
            with open(os.path.join(attach_root, "enc.bin"), "wb") as f:
                f.write(b"enc")

            mid = "msg12345678901234567890"
            self.cur.execute("""
                INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size)
                VALUES (?, 'c1', 1000000000000, 'image/png', 'enc.bin', 'key1', 'test.png', 3);
            """, (mid,))
            self.conn.commit()

            out_dir = os.path.join(tmpdir, "out")
            chat_dir = os.path.join(out_dir, "Alice")
            os.makedirs(chat_dir, exist_ok=True)

            # Pre-create all candidates: test.png, test_msg12345.png, test_msg1234567890123.png, test_msg12345678901234567890.png
            candidates = ["test.png", f"test_{mid[:8]}.png", f"test_{mid[:16]}.png", f"test_{mid}.png"]
            for c in candidates:
                with open(os.path.join(chat_dir, c), "wb") as f:
                    f.write(b"OCCUPIED")

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with patch.dict(os.environ, {"APPDATA": tmpdir}), \
                 patch("cli.decrypt_attachment", return_value=b"decrypted"), \
                 patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                res = cli.export_media(self.cur, out_dir)

            self.assertFalse(res)
            self.assertIn("All collision resolution candidate filenames are occupied", stderr_buf.getvalue())
            # Ensure all occupied files are unchanged
            for c in candidates:
                with open(os.path.join(chat_dir, c), "rb") as f:
                    self.assertEqual(f.read(), b"OCCUPIED")

    @patch("cli.open_db")
    @patch("cli.copy_db_snapshot")
    @patch("cli.get_signal_key")
    @patch("cli.export_media")
    def test_main_export_media_success_and_failure(self, mock_export, mock_key, mock_snap, mock_open):
        mock_key.return_value = "dummy_key"
        mock_snap.return_value = "dummy_db_path"
        mock_open.return_value = (self.conn, self.cur)

        # Test success (export_media returns True) -> main returns 0
        mock_export.return_value = True
        test_args = ["cli", "--export-media"]
        with patch.object(sys, "argv", test_args):
            cli.main()

        # Test failure (export_media returns False) -> main calls sys.exit(1)
        mock_export.return_value = False
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)

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
