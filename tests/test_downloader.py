import asyncio
import io
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import downloader.dispatcher as dispatcher


class TestDownloader(unittest.TestCase):

    @patch("downloader.dispatcher.sqlcipher3")
    def test_query_pending_video_groups_success_empty(self, mock_sqlcipher):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_sqlcipher.connect.return_value = mock_conn

        res = dispatcher.query_pending_video_groups("dummy_db.sqlite", "dummy_key")

        self.assertEqual(res, [])
        mock_conn.close.assert_called_once()

    @patch("downloader.dispatcher.sqlcipher3")
    def test_query_pending_video_groups_failure_propagates(self, mock_sqlcipher):
        mock_sqlcipher.connect.side_effect = RuntimeError("Database error")

        with self.assertRaises(RuntimeError) as ctx:
            dispatcher.query_pending_video_groups("dummy_db.sqlite", "dummy_key")

        self.assertIn("Database error", str(ctx.exception))

    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("downloader.dispatcher.get_cdp_target")
    def test_run_headless_download_initial_query_failure(self, mock_get_cdp, mock_query):
        mock_get_cdp.return_value = {"webSocketDebuggerUrl": "ws://localhost:9222/page/1"}
        mock_query.side_effect = RuntimeError("Initial query crashed")

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()

        with patch("sys.stderr", stderr_buf), patch("sys.stdout", stdout_buf):
            result = dispatcher.run_headless_download("dummy_db.sqlite", "dummy_key")

        self.assertFalse(result)
        self.assertIn("[Headless Downloader] Error: Failed to query pending videos: Initial query crashed", stderr_buf.getvalue())
        self.assertNotIn("Everything is downloaded!", stdout_buf.getvalue())
        self.assertNotIn("downloaded successfully", stdout_buf.getvalue())

    @patch("downloader.dispatcher._evaluate_cdp")
    @patch("downloader.dispatcher.websockets.connect")
    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("downloader.dispatcher.shutil.rmtree")
    @patch("db.copy_db_snapshot")
    def test_polling_failure_preserves_progress(self, mock_copy, mock_rmtree, mock_query, mock_ws_connect, mock_eval):
        mock_eval.return_value = True
        mock_ws = AsyncMock()
        mock_ws_connect.return_value.__aenter__.return_value = mock_ws

        mock_copy.return_value = "/tmp/snap/db.sqlite"

        # Sequence of polls:
        # 1. First poll: RuntimeError (query fails)
        # 2. Second poll: Query succeeds returning 1 remaining pending item
        # 3. Third poll: Query succeeds returning 0 remaining pending items (complete)
        mock_query.side_effect = [
            RuntimeError("Transient poll error"),
            [("c1", "Group 1", 1)],
            []
        ]

        groups = [("c1", "Group 1", 2)]  # Initial pending = 2

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()

        with patch("sys.stderr", stderr_buf), patch("sys.stdout", stdout_buf):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(
                    dispatcher._trigger_group_download("ws://localhost:9222", groups, "db.sqlite", "key", poll_interval=0.001)
                )
            finally:
                loop.close()

        self.assertTrue(res)
        stderr_val = stderr_buf.getvalue()
        stdout_val = stdout_buf.getvalue()

        self.assertIn("Warning: Polling query failed: Transient poll error", stderr_val)
        self.assertIn("All 2 videos have downloaded successfully!", stdout_val)

    @patch("downloader.dispatcher._evaluate_cdp")
    @patch("downloader.dispatcher.websockets.connect")
    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("db.copy_db_snapshot")
    def test_successful_transition_to_zero_completes(self, mock_copy, mock_query, mock_ws_connect, mock_eval):
        mock_eval.return_value = True
        mock_ws = AsyncMock()
        mock_ws_connect.return_value.__aenter__.return_value = mock_ws

        mock_copy.return_value = "/tmp/snap/db.sqlite"
        mock_query.return_value = []

        groups = [("c1", "Group 1", 1)]

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(
                    dispatcher._trigger_group_download("ws://localhost:9222", groups, "db.sqlite", "key", poll_interval=0.001)
                )
            finally:
                loop.close()

        self.assertTrue(res)
        self.assertIn("All 1 videos have downloaded successfully!", stdout_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
