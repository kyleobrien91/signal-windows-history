import asyncio
import io
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import downloader.dispatcher as dispatcher
from downloader.results import DownloadResult, GroupDownloadResult, ItemResultStatus
import signal_headless_downloader


class TestDownloader(unittest.TestCase):

    def test_download_result_boolean_contract(self):
        res_ok = DownloadResult(success=True, status=ItemResultStatus.SUCCESS)
        res_fail = DownloadResult(success=False, status=ItemResultStatus.FAILED)
        res_skip = DownloadResult(success=True, status=ItemResultStatus.SKIPPED)

        self.assertTrue(bool(res_ok))
        self.assertFalse(bool(res_fail))
        self.assertTrue(bool(res_skip))

        # Existing callers using 'if run_headless_download(...):'
        branch_ok = False
        if res_ok:
            branch_ok = True
        self.assertTrue(branch_ok)

        branch_fail = True
        if res_fail:
            branch_fail = False
        self.assertTrue(branch_fail)

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

    @patch("signal_headless_downloader.sqlcipher3")
    def test_legacy_query_pending_video_groups_failure_propagates(self, mock_sqlcipher):
        mock_sqlcipher.connect.side_effect = RuntimeError("Legacy DB error")

        with self.assertRaises(RuntimeError) as ctx:
            signal_headless_downloader.query_pending_video_groups("dummy_db.sqlite", "dummy_key")

        self.assertIn("Legacy DB error", str(ctx.exception))

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
        self.assertEqual(result.status, ItemResultStatus.FAILED)
        self.assertIn("[Headless Downloader] Error: Failed to query pending videos: Initial query crashed", stderr_buf.getvalue())
        self.assertNotIn("Everything is downloaded!", stdout_buf.getvalue())
        self.assertNotIn("downloaded successfully", stdout_buf.getvalue())

    @patch("signal_headless_downloader.query_pending_video_groups")
    @patch("signal_headless_downloader.get_cdp_target")
    def test_legacy_run_headless_download_initial_query_failure(self, mock_get_cdp, mock_query):
        mock_get_cdp.return_value = {"webSocketDebuggerUrl": "ws://localhost:9222/page/1"}
        mock_query.side_effect = RuntimeError("Legacy initial query crashed")

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()

        with patch("sys.stderr", stderr_buf), patch("sys.stdout", stdout_buf):
            result = signal_headless_downloader.run_headless_download("dummy_db.sqlite", "dummy_key")

        self.assertFalse(result)
        self.assertEqual(result.status, ItemResultStatus.FAILED)
        self.assertIn("Legacy initial query crashed", stderr_buf.getvalue())

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

        mock_query.side_effect = [
            RuntimeError("Transient poll error"),
            [("c1", "Group 1", 1)],
            []
        ]

        groups = [("c1", "Group 1", 2)]

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
        self.assertEqual(res.status, ItemResultStatus.SUCCESS)
        self.assertIn("Warning: Polling query failed: Transient poll error", stderr_buf.getvalue())
        self.assertIn("All 2 videos have downloaded successfully!", stdout_buf.getvalue())

    @patch("downloader.dispatcher._evaluate_cdp")
    @patch("downloader.dispatcher.websockets.connect")
    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("db.copy_db_snapshot")
    def test_partial_dispatch_failure_continues_and_reports_failed_aggregate(self, mock_copy, mock_query, mock_ws_connect, mock_eval):
        # Group 1 fails dispatch, Group 2 succeeds dispatch
        mock_eval.side_effect = [
            True,  # Controller check
            {"error": "Conversation model not found"},  # Group 1 trigger
            {"success": True}  # Group 2 trigger
        ]
        mock_ws = AsyncMock()
        mock_ws_connect.return_value.__aenter__.return_value = mock_ws
        mock_copy.return_value = "/tmp/snap/db.sqlite"
        mock_query.return_value = []

        groups = [("c1", "Group 1", 1), ("c2", "Group 2", 1)]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            res = loop.run_until_complete(
                dispatcher._trigger_group_download("ws://localhost:9222", groups, "db.sqlite", "key", poll_interval=0.001)
            )
        finally:
            loop.close()

        self.assertFalse(res)
        self.assertEqual(res.status, ItemResultStatus.FAILED)
        self.assertEqual(len(res.groups), 2)
        self.assertEqual(res.groups[0].status, ItemResultStatus.FAILED)
        self.assertEqual(res.groups[1].status, ItemResultStatus.SUCCESS)

    @patch("downloader.dispatcher._evaluate_cdp")
    @patch("downloader.dispatcher.websockets.connect")
    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("db.copy_db_snapshot")
    def test_idle_timeout_reports_timeout_status(self, mock_copy, mock_query, mock_ws_connect, mock_eval):
        mock_eval.return_value = True
        mock_ws = AsyncMock()
        mock_ws_connect.return_value.__aenter__.return_value = mock_ws
        mock_copy.return_value = "/tmp/snap/db.sqlite"
        mock_query.return_value = [("c1", "Group 1", 2)]  # Stalled at 2 remaining

        groups = [("c1", "Group 1", 2)]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            res = loop.run_until_complete(
                dispatcher._trigger_group_download("ws://localhost:9222", groups, "db.sqlite", "key", poll_interval=0.0001, max_idle_rounds=2)
            )
        finally:
            loop.close()

        self.assertFalse(res)
        self.assertEqual(res.status, ItemResultStatus.TIMEOUT)
        self.assertEqual(res.pending_remaining, 2)

    @patch("downloader.dispatcher._evaluate_cdp")
    @patch("downloader.dispatcher.websockets.connect")
    def test_cancellation_reports_cancelled_status(self, mock_ws_connect, mock_eval):
        mock_eval.return_value = True
        mock_ws = AsyncMock()
        mock_ws_connect.return_value.__aenter__.return_value = mock_ws

        # Simulate KeyboardInterrupt during sleep
        async def interrupt_sleep(seconds):
            raise KeyboardInterrupt()

        groups = [("c1", "Group 1", 2)]

        with patch("asyncio.sleep", side_effect=interrupt_sleep):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(
                    dispatcher._trigger_group_download("ws://localhost:9222", groups, "db.sqlite", "key", poll_interval=0.001)
                )
            finally:
                loop.close()

        self.assertFalse(res)
        self.assertEqual(res.status, ItemResultStatus.CANCELLED)

    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("downloader.dispatcher.get_cdp_target")
    def test_no_pending_items_returns_skipped_success(self, mock_get_cdp, mock_query):
        mock_get_cdp.return_value = {"webSocketDebuggerUrl": "ws://localhost:9222/page/1"}
        mock_query.return_value = []

        res = dispatcher.run_headless_download("db.sqlite", "key")
        self.assertTrue(res)
        self.assertEqual(res.status, ItemResultStatus.SKIPPED)


if __name__ == "__main__":
    unittest.main()
