import importlib
import os
import subprocess
import sys
import unittest


class TestPackageAPIsAndExports(unittest.TestCase):

    def test_cli_public_api(self):
        import cli
        self.assertEqual(cli.__all__, ["main"])
        self.assertTrue(callable(cli.main))

    def test_config_public_api(self):
        import config
        expected = [
            "APPDATA",
            "SIGNAL_DIR",
            "SQL_DIR",
            "LOCAL_STATE_PATH",
            "CONFIG_JSON_PATH",
            "ATTACHMENTS_DIR",
            "PLAYER_EXE_PATH",
            "HOME_DIR",
        ]
        self.assertEqual(config.__all__, expected)
        for name in expected:
            self.assertTrue(hasattr(config, name))

    def test_crypto_public_api(self):
        import crypto
        expected = [
            "dpapi_decrypt",
            "get_signal_key",
            "decrypt_attachment",
            "inspect_attachment",
            "stream_attachment_range",
        ]
        self.assertEqual(crypto.__all__, expected)
        for name in expected:
            self.assertTrue(callable(getattr(crypto, name)))

    def test_db_public_api(self):
        import db
        expected = [
            "copy_db_snapshot",
            "open_db",
            "set_active_db",
            "reload_db",
            "query_groups",
            "query_media",
            "query_new_count",
            "get_conversation_map",
        ]
        self.assertEqual(db.__all__, expected)
        for name in expected:
            self.assertTrue(callable(getattr(db, name)))
        # Verify internal global state is not in __all__
        self.assertNotIn("_db_conn", db.__all__)
        self.assertNotIn("_db_cur", db.__all__)

    def test_downloader_public_api(self):
        import downloader
        expected = [
            "get_cdp_target",
            "query_pending_video_groups",
            "run_headless_download",
            "DownloadResult",
            "GroupDownloadResult",
            "ItemResultStatus",
        ]
        self.assertEqual(downloader.__all__, expected)
        for name in expected:
            self.assertTrue(hasattr(downloader, name))

    def test_metadata_public_api(self):
        import metadata
        expected = [
            "load_metadata",
            "save_metadata",
            "get_meta",
            "set_meta",
            "mark_seen",
            "all_labels",
            "get_sync_status",
            "set_sync_status",
        ]
        self.assertEqual(metadata.__all__, expected)
        for name in expected:
            self.assertTrue(callable(getattr(metadata, name)))
        # Verify internal state is not in __all__
        self.assertNotIn("_meta_data", metadata.__all__)

    def test_pipeline_public_api(self):
        import pipeline
        self.assertEqual(pipeline.__all__, [])

    def test_player_public_api(self):
        import player
        expected = [
            "is_signal_running",
            "kill_signal",
            "main",
        ]
        self.assertEqual(player.__all__, expected)
        for name in expected:
            self.assertTrue(callable(getattr(player, name)))


class TestLegacyRootShims(unittest.TestCase):

    def test_signal_key_shim(self):
        import signal_key
        from crypto import get_signal_key
        self.assertIs(signal_key.get_signal_key, get_signal_key)

    def test_signal_grep_shim(self):
        import signal_grep
        import cli
        self.assertIs(signal_grep.main, cli.main)

    def test_signal_db_shim(self):
        import signal_db
        import db
        self.assertIs(signal_db.copy_db_snapshot, db.copy_db_snapshot)
        self.assertIs(signal_db.open_db, db.open_db)

    def test_signal_crypto_shim(self):
        import signal_crypto
        import crypto
        self.assertIs(signal_crypto.get_signal_key, crypto.get_signal_key)
        self.assertIs(signal_crypto.decrypt_attachment, crypto.decrypt_attachment)

    def test_signal_meta_shim(self):
        import signal_meta
        import metadata
        self.assertIs(signal_meta.get_meta, metadata.get_meta)

    def test_signal_player_shim(self):
        import signal_player
        import player
        self.assertIs(signal_player.main, player.main)

    def test_signal_headless_downloader_shim(self):
        import signal_headless_downloader
        import downloader
        self.assertTrue(callable(signal_headless_downloader.run_headless_download))
        self.assertIs(signal_headless_downloader.get_cdp_target, downloader.get_cdp_target)


class TestModuleExecutionFromExternalDir(unittest.TestCase):

    def test_cli_module_help_from_tmp(self):
        res = subprocess.run(
            [sys.executable, "-m", "cli", "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Grep and query local Signal Desktop history", res.stdout)

    def test_player_module_help_from_tmp(self):
        res = subprocess.run(
            [sys.executable, "-m", "player", "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Signal Desktop local video player", res.stdout)

    def test_signal_cli_entrypoint_from_tmp(self):
        res = subprocess.run(
            ["signal-cli", "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Grep and query local Signal Desktop history", res.stdout)

    def test_signal_player_entrypoint_from_tmp(self):
        res = subprocess.run(
            ["signal-player", "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Signal Desktop local video player", res.stdout)


if __name__ == "__main__":
    unittest.main()
