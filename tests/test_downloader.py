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


class TestRateEstimatorAndProgress(unittest.TestCase):

    def test_rate_estimator_insufficient_samples(self):
        estimator = dispatcher.RateEstimator()
        estimator.add_sample(100.0, 50)
        rate, eta_sec, eta_str = estimator.get_rate_and_eta(50, 50)
        self.assertEqual(rate, 0.0)
        self.assertIsNone(eta_sec)
        self.assertEqual(eta_str, "ETA calculating...")

    def test_rate_estimator_valid_samples(self):
        estimator = dispatcher.RateEstimator(window_seconds=15.0)
        estimator.add_sample(100.0, 50)
        estimator.add_sample(105.0, 40)
        rate, eta_sec, eta_str = estimator.get_rate_and_eta(50, 40)
        self.assertEqual(rate, 2.0)
        self.assertEqual(eta_sec, 20.0)
        self.assertEqual(eta_str, "ETA 00:20")

    def test_rate_estimator_completion(self):
        estimator = dispatcher.RateEstimator()
        estimator.add_sample(100.0, 50)
        estimator.add_sample(110.0, 0)
        rate, eta_sec, eta_str = estimator.get_rate_and_eta(50, 0)
        self.assertEqual(eta_sec, 0.0)
        self.assertEqual(eta_str, "ETA 00:00")

    def test_rate_estimator_stalled_window_returns_zero_rate_and_calculating_eta(self):
        estimator = dispatcher.RateEstimator(window_seconds=15.0)
        estimator.add_sample(100.0, 50)
        estimator.add_sample(105.0, 40)
        # Stalled for 20 seconds, old samples drop out of 15s window
        estimator.add_sample(125.0, 40)
        rate, eta_sec, eta_str = estimator.get_rate_and_eta(50, 40)
        self.assertEqual(rate, 0.0)
        self.assertIsNone(eta_sec)
        self.assertEqual(eta_str, "ETA calculating...")


class TestManagedDownloadLifecycle(unittest.TestCase):

    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("downloader.dispatcher.is_signal_running")
    def test_no_pending_media_succeeds_cleanly(self, mock_is_running, mock_query):
        mock_is_running.return_value = False
        mock_query.return_value = []

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            res = dispatcher.run_managed_download(db_path="dummy_db", key="dummy_key")

        self.assertTrue(res)
        self.assertEqual(res.status, ItemResultStatus.SKIPPED)
        self.assertIn("Outstanding media: 0", stdout_buf.getvalue())
        self.assertIn("All media is already downloaded.", stdout_buf.getvalue())

    @patch("downloader.dispatcher.get_cdp_target")
    def test_start_managed_signal_cdp_fails_if_unmanaged_port_in_use(self, mock_get_cdp):
        mock_get_cdp.return_value = {"webSocketDebuggerUrl": "ws://localhost:9222/page/1"}
        proc, err = dispatcher.start_managed_signal_cdp(cdp_port=9222)
        self.assertIsNone(proc)
        self.assertIn("already in use by an unmanaged CDP endpoint", err)

    @patch("downloader.dispatcher.get_cdp_target")
    @patch("subprocess.Popen")
    def test_stop_managed_signal_cdp_verifies_port_cleanup(self, mock_popen, mock_get_cdp):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        # Target exists initially, then becomes None after cleanup
        mock_get_cdp.side_effect = [{"webSocketDebuggerUrl": "ws://localhost:9222"}, None]

        dispatcher.stop_managed_signal_cdp(mock_proc, relaunch_normal=False, cdp_port=9222)
        mock_proc.terminate.assert_called_once()

    @patch("downloader.dispatcher.start_managed_signal_cdp")
    @patch("downloader.dispatcher.is_signal_running")
    @patch("downloader.dispatcher.query_pending_video_groups")
    def test_cdp_startup_failure_reports_failed_status(self, mock_query, mock_is_running, mock_start_cdp):
        mock_is_running.return_value = False
        mock_query.return_value = [("c1", "Group 1", 5)]
        mock_start_cdp.return_value = (None, "Port 9222 bound")

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            res = dispatcher.run_managed_download(db_path="dummy_db", key="dummy_key")

        self.assertFalse(res)
        self.assertEqual(res.status, ItemResultStatus.FAILED)
        self.assertIn("CDP startup failed", res.error_message)

    @patch("downloader.dispatcher.stop_managed_signal_cdp")
    @patch("downloader.dispatcher.run_headless_download")
    @patch("downloader.dispatcher.start_managed_signal_cdp")
    @patch("downloader.dispatcher.query_pending_video_groups")
    @patch("downloader.dispatcher.is_signal_running")
    def test_cdp_cleanup_and_session_restore_on_completion(self, mock_is_running, mock_query, mock_start_cdp, mock_run_hl, mock_stop_cdp):
        mock_is_running.return_value = True
        mock_query.return_value = [("c1", "Group 1", 3)]
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_start_cdp.return_value = (mock_proc, None)

        mock_run_hl.return_value = DownloadResult(
            success=True,
            status=ItemResultStatus.SUCCESS,
            initial_pending=3,
            pending_remaining=0,
            downloaded_count=3,
            tracked_pid=12345
        )

        res = dispatcher.run_managed_download(db_path="dummy_db", key="dummy_key")

        self.assertTrue(res)
        self.assertEqual(res.tracked_pid, 12345)
        mock_stop_cdp.assert_called_once_with(mock_proc, relaunch_normal=True)


class TestPlayerStartupMenuAndCLI(unittest.TestCase):

    @patch("downloader.run_managed_download")
    @patch("player.server.is_signal_running")
    @patch("player.server.ThreadingHTTPServer")
    @patch("webbrowser.open")
    def test_cli_flag_download_only_runs_managed_download_and_exits(self, mock_browser, mock_http, mock_is_running, mock_run_managed):
        mock_is_running.return_value = False
        mock_run_managed.return_value = DownloadResult(success=True, status=ItemResultStatus.SUCCESS)

        test_args = ["player.server", "--download-only"]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as ctx:
                import player.server
                player.server.main()

            self.assertEqual(ctx.exception.code, 0)
            mock_run_managed.assert_called_once_with(show_progress=True)
            mock_http.assert_not_called()
            mock_browser.assert_not_called()

    @patch("downloader.run_managed_download")
    @patch("player.server.is_signal_running")
    @patch("player.server.ThreadingHTTPServer")
    @patch("webbrowser.open")
    def test_menu_running_option_4_runs_download_only(self, mock_browser, mock_http, mock_is_running, mock_run_managed):
        mock_is_running.return_value = True
        mock_run_managed.return_value = DownloadResult(success=True, status=ItemResultStatus.SUCCESS)

        test_args = ["player.server"]
        with patch.object(sys, "argv", test_args), patch("builtins.input", return_value="4"):
            with self.assertRaises(SystemExit) as ctx:
                import player.server
                player.server.main()

            self.assertEqual(ctx.exception.code, 0)
            mock_run_managed.assert_called_once_with(show_progress=True)
            mock_http.assert_not_called()
            mock_browser.assert_not_called()

    @patch("downloader.run_managed_download")
    @patch("player.server.is_signal_running")
    @patch("player.server.ThreadingHTTPServer")
    @patch("webbrowser.open")
    def test_menu_not_running_option_3_runs_download_only(self, mock_browser, mock_http, mock_is_running, mock_run_managed):
        mock_is_running.return_value = False
        mock_run_managed.return_value = DownloadResult(success=True, status=ItemResultStatus.SUCCESS)

        test_args = ["player.server"]
        with patch.object(sys, "argv", test_args), patch("builtins.input", return_value="3"):
            with self.assertRaises(SystemExit) as ctx:
                import player.server
                player.server.main()

            self.assertEqual(ctx.exception.code, 0)
            mock_run_managed.assert_called_once_with(show_progress=True)
            mock_http.assert_not_called()
            mock_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
