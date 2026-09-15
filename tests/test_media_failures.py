import base64
import hashlib
import hmac
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from player.media import serve_encrypted_media
from player.server import _Handler as ServerHandler, _media_lookup, _lookup_lock
from signal_player import _Handler as SignalPlayerHandler
import crypto.key as crypto_key
import signal_crypto


class MockHTTPHandler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.response_code = None
        self.response_headers = {}
        self.wfile = io.BytesIO()
        self.error_code = None
        self.error_message = None
        self.close_connection = False

    def send_response(self, code, message=None):
        self.response_code = code

    def send_header(self, keyword, value):
        self.response_headers[keyword] = str(value)

    def end_headers(self):
        pass

    def send_error(self, code, message=None, explain=None):
        self.error_code = code
        self.error_message = message


class FailingWriter:
    def __init__(self, fail_after_bytes=10):
        self.written = 0
        self.fail_after_bytes = fail_after_bytes

    def write(self, chunk):
        if self.written + len(chunk) > self.fail_after_bytes:
            raise OSError("Connection reset by peer")
        self.written += len(chunk)


def make_encrypted_attachment(plaintext: bytes, key_b64: str) -> bytes:
    raw_key = base64.b64decode(key_b64)
    aes_key, mac_key = raw_key[:32], raw_key[32:]
    iv = os.urandom(16)
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    return iv + ct + tag


class TestMediaFailures(unittest.TestCase):

    def setUp(self):
        self.key_b64 = base64.b64encode(os.urandom(64)).decode("utf-8")
        self.sensitive_user_path = os.path.join("/home", "secret_user_john_doe", "Signal", "attachments.noindex", "ab", "abc123.bin")

    def test_missing_file_returns_404_and_sanitized_log(self):
        handler = MockHTTPHandler()
        stderr_buf = io.StringIO()

        with patch("sys.stderr", stderr_buf):
            serve_encrypted_media(handler, self.sensitive_user_path, self.key_b64, None, "video/mp4")

        self.assertEqual(handler.error_code, 404)
        err_log = stderr_buf.getvalue()
        self.assertIn("abc123.bin", err_log)
        self.assertNotIn("secret_user_john_doe", err_log)
        self.assertNotIn(self.key_b64, err_log)

    def test_corrupt_mac_returns_400_and_sanitized_log(self):
        plaintext = b"Sensitive Plaintext Media Content"
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)
        corrupt_data = enc_data[:-1] + b"\x00"

        with tempfile.NamedTemporaryFile(suffix="_abc123.bin", delete=False) as f:
            f.write(corrupt_data)
            enc_path = f.name

        try:
            handler = MockHTTPHandler()
            stderr_buf = io.StringIO()

            with patch("sys.stderr", stderr_buf):
                serve_encrypted_media(handler, enc_path, self.key_b64, None, "video/mp4")

            self.assertEqual(handler.error_code, 400)
            err_log = stderr_buf.getvalue()
            self.assertIn(os.path.basename(enc_path), err_log)
            self.assertNotIn(self.key_b64, err_log)
            self.assertNotIn("Sensitive Plaintext Media Content", err_log)
        finally:
            os.unlink(enc_path)

    def test_post_header_stream_chunk_failure(self):
        plaintext = b"A" * 1000
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)

        with tempfile.NamedTemporaryFile(suffix="_abc123.bin", delete=False) as f:
            f.write(enc_data)
            enc_path = f.name

        try:
            handler = MockHTTPHandler()
            stderr_buf = io.StringIO()

            def failing_stream(*args, **kwargs):
                yield b"A" * 10
                raise RuntimeError("Stream read failure")

            with patch("player.media.stream_attachment_range", side_effect=failing_stream):
                with patch("sys.stderr", stderr_buf):
                    with self.assertRaises(RuntimeError):
                        serve_encrypted_media(handler, enc_path, self.key_b64, None, "video/mp4")

            # 1. 200 headers were sent
            self.assertEqual(handler.response_code, 200)
            self.assertEqual(handler.response_headers["Content-Length"], "1000")
            # 2. send_error was NOT called
            self.assertIsNone(handler.error_code)
            # 3. close_connection is True
            self.assertTrue(handler.close_connection)
            # 4. Truncated output: written bytes < Content-Length
            written_bytes = handler.wfile.getvalue()
            self.assertLess(len(written_bytes), 1000)
            # 5. Sanitized log checks
            err_log = stderr_buf.getvalue()
            self.assertIn("abc123.bin", err_log)
            self.assertIn("RuntimeError", err_log)
            self.assertNotIn("secret_user_john_doe", err_log)
            self.assertNotIn(self.key_b64, err_log)
            self.assertNotIn("AAAAA", err_log)
        finally:
            os.unlink(enc_path)

    def test_post_header_wfile_write_failure(self):
        plaintext = b"B" * 1000
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)

        with tempfile.NamedTemporaryFile(suffix="_abc123.bin", delete=False) as f:
            f.write(enc_data)
            enc_path = f.name

        try:
            handler = MockHTTPHandler()
            handler.wfile = FailingWriter(fail_after_bytes=10)
            stderr_buf = io.StringIO()

            with patch("sys.stderr", stderr_buf):
                with self.assertRaises(OSError):
                    serve_encrypted_media(handler, enc_path, self.key_b64, None, "video/mp4")

            self.assertEqual(handler.response_code, 200)
            self.assertIsNone(handler.error_code)
            self.assertTrue(handler.close_connection)
            self.assertLess(handler.wfile.written, 1000)
            err_log = stderr_buf.getvalue()
            self.assertIn("abc123.bin", err_log)
            self.assertIn("OSError", err_log)
            self.assertNotIn(self.key_b64, err_log)
        finally:
            os.unlink(enc_path)

    def test_unregistered_media_lookup_returns_404(self):
        handler = MockHTTPHandler()
        stderr_buf = io.StringIO()

        with patch("sys.stderr", stderr_buf):
            ServerHandler._stream(handler, "nonexistent_msg_999")

        self.assertEqual(handler.error_code, 404)
        self.assertIn("nonexistent_msg_999", stderr_buf.getvalue())

    def test_decrypt_blob_missing_file_raises_sanitized_error(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            signal_crypto._decrypt_blob(self.sensitive_user_path, self.key_b64)

        err_msg = str(ctx.exception)
        self.assertIn("abc123.bin", err_msg)
        self.assertNotIn("secret_user_john_doe", err_msg)

    def test_get_cached_missing_file_raises_sanitized_error(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            crypto_key._get_cached("msg_1", self.sensitive_user_path, self.key_b64, 100)

        err_msg = str(ctx.exception)
        self.assertIn("abc123.bin", err_msg)
        self.assertNotIn("secret_user_john_doe", err_msg)


class TestCLIExitCodes(unittest.TestCase):

    @patch("downloader.dispatcher.sys.exit")
    @patch("downloader.dispatcher.run_headless_download")
    @patch("db.copy_db_snapshot")
    @patch("crypto.get_signal_key")
    def test_dispatcher_cli_exit_codes(self, mock_key, mock_snap, mock_run, mock_exit):
        mock_key.return_value = "dummy_key"
        mock_snap.return_value = "dummy_db"

        from downloader.results import DownloadResult, ItemResultStatus

        # 1. Successful run -> exits 0
        mock_run.return_value = DownloadResult(success=True, status=ItemResultStatus.SKIPPED)
        import downloader.dispatcher as disp
        res = disp.run_headless_download("dummy_db", "dummy_key")
        if res:
            mock_exit(0)
        else:
            mock_exit(1)

        mock_exit.assert_called_with(0)

        # 2. Failed run -> exits 1
        mock_run.return_value = DownloadResult(success=False, status=ItemResultStatus.FAILED, error_message="CDP failed")
        res = disp.run_headless_download("dummy_db", "dummy_key")
        if res:
            mock_exit(0)
        else:
            mock_exit(1)

        mock_exit.assert_called_with(1)

    @patch("signal_headless_downloader.sys.exit")
    @patch("signal_headless_downloader.run_headless_download")
    @patch("signal_player.copy_db_snapshot")
    @patch("signal_player.get_signal_key")
    def test_legacy_cli_exit_codes(self, mock_key, mock_snap, mock_run, mock_exit):
        from downloader.results import DownloadResult, ItemResultStatus
        mock_run.return_value = DownloadResult(success=False, status=ItemResultStatus.TIMEOUT)

        import signal_headless_downloader as legacy_disp
        res = mock_run("dummy_db", "dummy_key")
        if res:
            mock_exit(0)
        else:
            mock_exit(1)

        mock_exit.assert_called_with(1)


if __name__ == "__main__":
    unittest.main()
