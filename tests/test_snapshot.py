import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import db.snapshot as snapshot
import signal_db


class FakeOperationalError(Exception):
    def __init__(self, msg="database is locked", sqlite_errorcode=None, sqlite_extended_errorcode=None):
        super().__init__(msg)
        if sqlite_errorcode is not None:
            self.sqlite_errorcode = sqlite_errorcode
        if sqlite_extended_errorcode is not None:
            self.sqlite_extended_errorcode = sqlite_extended_errorcode


class FakeConnection:
    def __init__(self, path, *, is_source=False, fail_backup_exc=None, fail_validation=False):
        self.path = path
        self.is_source = is_source
        self.queries = []
        self.backup_target = None
        self.fail_backup_exc = fail_backup_exc
        self.fail_validation = fail_validation

    def execute(self, statement):
        self.queries.append(statement)
        if self.fail_validation and "SELECT count(*)" in statement:
            raise FakeOperationalError("file is not a database")

    def cursor(self):
        return self

    def fetchone(self):
        return (10,)

    def backup(self, target):
        self.backup_target = target
        type(self).backup_calls += 1
        if self.fail_backup_exc and type(self).backup_calls <= self.fail_backup_exc.get("fail_count", 1):
            raise self.fail_backup_exc["exc"]
        Path(target.path).write_bytes(b"backup-db")

    def close(self):
        pass

    backup_calls = 0


class FakeSqlCipherModule(types.ModuleType):
    OperationalError = FakeOperationalError

    def __init__(self, fail_backup_exc=None, fail_validation=False):
        super().__init__("sqlcipher3")
        self.connect_calls = []
        self.fail_backup_exc = fail_backup_exc
        self.fail_validation = fail_validation

    def connect(self, path, uri=False):
        # Validation connection is non-uri and opened for db_dst after backup
        is_val = not uri and len(self.connect_calls) >= 2
        conn = FakeConnection(
            path,
            is_source=uri,
            fail_backup_exc=self.fail_backup_exc,
            fail_validation=is_val and self.fail_validation
        )
        self.connect_calls.append((path, uri))
        return conn


class TestDatabaseSnapshot(unittest.TestCase):
    def setUp(self):
        self.appdata = Path("/tmp") / "Signal"
        self.signal_sql = self.appdata / "sql"
        self.signal_sql.mkdir(parents=True, exist_ok=True)
        (self.signal_sql / "db.sqlite").write_bytes(b"live-db")

        self.temp_dir = Path("/tmp")
        self.old_appdata = os.environ.get("APPDATA")
        self.old_temp = os.environ.get("TEMP")
        os.environ["APPDATA"] = str(self.appdata.parent)
        os.environ["TEMP"] = str(self.temp_dir)

        FakeConnection.backup_calls = 0

    def tearDown(self):
        if self.old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self.old_appdata

        if self.old_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = self.old_temp

        for path in sorted(self.temp_dir.glob("signal-player-work-*"), reverse=True):
            if path.is_dir():
                for child in sorted(path.iterdir(), reverse=True):
                    if child.is_file():
                        child.unlink()
                path.rmdir()

    @patch("db.snapshot.get_signal_key", return_value="a" * 64)
    def test_snapshot_uses_backup_api_and_retries_on_busy(self, _mock_key):
        fail_spec = {"fail_count": 1, "exc": FakeOperationalError("database is busy")}
        fake_module = FakeSqlCipherModule(fail_backup_exc=fail_spec)
        with patch.dict(sys.modules, {"sqlcipher3": fake_module}):
            result = snapshot.copy_db_snapshot()

        self.assertTrue(result.endswith("db.sqlite"))
        self.assertTrue(os.path.exists(result))
        self.assertEqual(FakeConnection.backup_calls, 2)

        source_call = fake_module.connect_calls[0]
        dest_call = fake_module.connect_calls[1]
        self.assertIn("mode=ro", source_call[0])
        self.assertTrue(source_call[1])
        self.assertFalse(dest_call[1])

        # Verify validation call occurred on destination connection using same key
        val_call = fake_module.connect_calls[-1]
        self.assertEqual(val_call[0], result)

    @patch("db.snapshot.get_signal_key", return_value="a" * 64)
    def test_busy_locked_exhausts_retries(self, _mock_key):
        fail_spec = {"fail_count": 10, "exc": FakeOperationalError("database is locked")}
        fake_module = FakeSqlCipherModule(fail_backup_exc=fail_spec)
        with patch.dict(sys.modules, {"sqlcipher3": fake_module}):
            with self.assertRaises(FakeOperationalError):
                snapshot.copy_db_snapshot()

        self.assertEqual(FakeConnection.backup_calls, 5)

    @patch("db.snapshot.get_signal_key", return_value="a" * 64)
    def test_non_retryable_database_error_propagates_immediately(self, _mock_key):
        fail_spec = {"fail_count": 10, "exc": FakeOperationalError("file is not a database")}
        fake_module = FakeSqlCipherModule(fail_backup_exc=fail_spec)
        with patch.dict(sys.modules, {"sqlcipher3": fake_module}):
            with self.assertRaises(FakeOperationalError):
                snapshot.copy_db_snapshot()

        # Should fail immediately on 1st attempt without retrying
        self.assertEqual(FakeConnection.backup_calls, 1)

    @patch("db.snapshot.get_signal_key", return_value="a" * 64)
    def test_non_sqlite_exception_propagates_immediately(self, _mock_key):
        fail_spec = {"fail_count": 10, "exc": ValueError("invalid key format")}
        fake_module = FakeSqlCipherModule(fail_backup_exc=fail_spec)
        with patch.dict(sys.modules, {"sqlcipher3": fake_module}):
            with self.assertRaises(ValueError):
                snapshot.copy_db_snapshot()

        self.assertEqual(FakeConnection.backup_calls, 1)

    @patch("db.snapshot.get_signal_key", return_value="a" * 64)
    def test_validation_failure_cleans_up_and_does_not_retry(self, _mock_key):
        fake_module = FakeSqlCipherModule(fail_validation=True)
        with patch.dict(sys.modules, {"sqlcipher3": fake_module}):
            with self.assertRaises(FakeOperationalError) as ctx:
                snapshot.copy_db_snapshot()

        self.assertIn("file is not a database", str(ctx.exception))

        # Backup succeeded once, validation failed, no retries were performed for validation failure
        self.assertEqual(FakeConnection.backup_calls, 1)

        # Check destination file was cleaned up
        val_dst_path = fake_module.connect_calls[1][0]
        self.assertFalse(os.path.exists(val_dst_path))

    @patch("db.snapshot.get_signal_key", return_value="a" * 64)
    def test_legacy_signal_db_delegates_to_canonical_snapshot(self, _mock_key):
        fake_module = FakeSqlCipherModule()
        with patch.dict(sys.modules, {"sqlcipher3": fake_module}):
            result = signal_db.copy_db_snapshot()

        self.assertTrue(result.endswith("db.sqlite"))
        self.assertTrue(os.path.exists(result))


if __name__ == "__main__":
    unittest.main()
