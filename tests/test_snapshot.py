import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import db.snapshot as snapshot


class FakeConnection:
    def __init__(self, path, *, is_source=False):
        self.path = path
        self.is_source = is_source
        self.queries = []
        self.backup_target = None

    def execute(self, statement):
        self.queries.append(statement)

    def backup(self, target):
        self.backup_target = target
        type(self).backup_calls += 1
        if type(self).backup_calls == 1:
            raise RuntimeError("database is busy")
        Path(target.path).write_bytes(b"backup-db")

    def close(self):
        pass

    backup_calls = 0


class FakeSqlCipherModule(types.ModuleType):
    def __init__(self):
        super().__init__("sqlcipher3")
        self.connect_calls = []

    def connect(self, path, uri=False):
        conn = FakeConnection(path, is_source=uri)
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
        fake_module = FakeSqlCipherModule()
        with patch.dict(sys.modules, {"sqlcipher3": fake_module}):
            result = snapshot.copy_db_snapshot()

        self.assertTrue(result.endswith("db.sqlite"))
        self.assertTrue(os.path.exists(result))
        self.assertEqual(FakeConnection.backup_calls, 2)

        # The source connection is opened read-only and the backup API is used to
        # materialize a single coherent snapshot instead of copying WAL/SHM fragments.
        source_call = fake_module.connect_calls[0]
        dest_call = fake_module.connect_calls[1]
        self.assertIn("mode=ro", source_call[0])
        self.assertTrue(source_call[1])
        self.assertFalse(dest_call[1])
        self.assertIn("signal-player-work-", os.path.dirname(result))


if __name__ == "__main__":
    unittest.main()
