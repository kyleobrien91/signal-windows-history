import sqlite3
import unittest
from unittest.mock import patch

import db
import db.queries as db_q


class TestKeysetPagination(unittest.TestCase):

    def setUp(self):
        # Create an in-memory SQLite database mimicking Signal's schema
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
                source TEXT
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

        self.cur.execute("INSERT INTO conversations (id, name, type) VALUES ('conv1', 'Group 1', 'group');")
        self.conn.commit()

        self.old_conn = db.set_active_db(self.conn, self.cur)

    def tearDown(self):
        db.set_active_db(self.old_conn, self.old_conn.cursor() if self.old_conn else None)
        self.conn.close()

    def _insert_attachment(self, msg_id, sent_at, filename, path=None, rowid=None):
        if path is None:
            path = f"path/{filename}"
        self.cur.execute(
            "INSERT INTO messages (id, conversationId, sent_at, timestamp) VALUES (?, 'conv1', ?, ?);",
            (msg_id, sent_at, sent_at)
        )
        if rowid is not None:
            self.cur.execute(
                "INSERT INTO message_attachments (rowid, messageId, conversationId, sentAt, contentType, path, localKey, fileName, size) "
                "VALUES (?, ?, 'conv1', ?, 'video/mp4', ?, 'key', ?, 1024);",
                (rowid, msg_id, sent_at, path, filename)
            )
        else:
            self.cur.execute(
                "INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size) "
                "VALUES (?, 'conv1', ?, 'video/mp4', ?, 'key', ?, 1024);",
                (msg_id, sent_at, path, filename)
            )
        self.conn.commit()

    def test_empty_dataset(self):
        page, lookup = db_q.query_media_paged(group_id='conv1', limit=10)
        self.assertEqual(page["items"], [])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(lookup, {})

    def test_fewer_than_one_page(self):
        self._insert_attachment("m1", 1000, "vid1.mp4")
        self._insert_attachment("m2", 2000, "vid2.mp4")

        page, lookup = db_q.query_media_paged(group_id='conv1', limit=10)
        self.assertEqual(len(page["items"]), 2)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual([i["filename"] for i in page["items"]], ["vid2.mp4", "vid1.mp4"])

    def test_multiple_pages_exact_boundaries(self):
        # Insert 5 attachments
        for i in range(1, 6):
            self._insert_attachment(f"m{i}", i * 1000, f"vid{i}.mp4", rowid=i)

        # Page 1 (limit 2) -> vid5, vid4
        page1, _ = db_q.query_media_paged(group_id='conv1', limit=2)
        self.assertEqual(len(page1["items"]), 2)
        self.assertTrue(page1["has_more"])
        self.assertIsNotNone(page1["next_cursor"])
        self.assertEqual([i["filename"] for i in page1["items"]], ["vid5.mp4", "vid4.mp4"])

        # Page 2 (limit 2) -> vid3, vid2
        page2, _ = db_q.query_media_paged(group_id='conv1', limit=2, cursor=page1["next_cursor"])
        self.assertEqual(len(page2["items"]), 2)
        self.assertTrue(page2["has_more"])
        self.assertIsNotNone(page2["next_cursor"])
        self.assertEqual([i["filename"] for i in page2["items"]], ["vid3.mp4", "vid2.mp4"])

        # Page 3 (limit 2) -> vid1
        page3, _ = db_q.query_media_paged(group_id='conv1', limit=2, cursor=page2["next_cursor"])
        self.assertEqual(len(page3["items"]), 1)
        self.assertFalse(page3["has_more"])
        self.assertIsNone(page3["next_cursor"])
        self.assertEqual([i["filename"] for i in page3["items"]], ["vid1.mp4"])

    def test_timestamp_ties_and_rowid_breaker(self):
        # Same sentAt timestamp 5000 for three items with explicit rowids 10, 20, 30
        self._insert_attachment("m1", 5000, "vid_r10.mp4", rowid=10)
        self._insert_attachment("m2", 5000, "vid_r20.mp4", rowid=20)
        self._insert_attachment("m3", 5000, "vid_r30.mp4", rowid=30)

        # Page 1 (limit 2) -> DESC order: rowid 30, rowid 20
        page1, _ = db_q.query_media_paged(group_id='conv1', limit=2)
        self.assertEqual([i["filename"] for i in page1["items"]], ["vid_r30.mp4", "vid_r20.mp4"])
        self.assertTrue(page1["has_more"])

        # Page 2 -> rowid 10
        page2, _ = db_q.query_media_paged(group_id='conv1', limit=2, cursor=page1["next_cursor"])
        self.assertEqual([i["filename"] for i in page2["items"]], ["vid_r10.mp4"])
        self.assertFalse(page2["has_more"])

    def test_newly_inserted_media_during_pagination(self):
        # Insert 3 items
        self._insert_attachment("m1", 1000, "old1.mp4", rowid=1)
        self._insert_attachment("m2", 2000, "old2.mp4", rowid=2)
        self._insert_attachment("m3", 3000, "old3.mp4", rowid=3)

        # Get Page 1 (limit 2) -> old3, old2
        page1, _ = db_q.query_media_paged(group_id='conv1', limit=2)
        self.assertEqual([i["filename"] for i in page1["items"]], ["old3.mp4", "old2.mp4"])

        # Now insert a NEW item with newer timestamp 4000 while user is paging
        self._insert_attachment("m_new", 4000, "new_arrival.mp4", rowid=4)

        # Fetch Page 2 using cursor from Page 1 -> must return old1 without duplicating or skipping
        page2, _ = db_q.query_media_paged(group_id='conv1', limit=2, cursor=page1["next_cursor"])
        self.assertEqual([i["filename"] for i in page2["items"]], ["old1.mp4"])
        self.assertFalse(page2["has_more"])

    def test_db_query_execution_contains_explicit_limit(self):
        # Insert 100 items
        for i in range(1, 101):
            self._insert_attachment(f"m{i}", i * 1000, f"vid{i}.mp4", rowid=i)

        executed_sql = []
        orig_cur = db_q._db_cur

        class MockCursorWrapper:
            def __init__(self, real_cur):
                self.real_cur = real_cur

            def execute(self, sql, params=()):
                executed_sql.append((sql, params))
                return self.real_cur.execute(sql, params)

            def fetchall(self):
                return self.real_cur.fetchall()

            def fetchone(self):
                return self.real_cur.fetchone()

        db_q._db_cur = MockCursorWrapper(self.cur)

        try:
            page, _ = db_q.query_media_paged(group_id='conv1', limit=10)
            self.assertEqual(len(page["items"]), 10)
            self.assertTrue(page["has_more"])

            # Find media query SQL
            media_queries = [s for s, p in executed_sql if "FROM message_attachments" in s]
            self.assertGreater(len(media_queries), 0)

            # Assert every executed media query contains explicit LIMIT clause
            for sql in media_queries:
                self.assertIn("LIMIT ?", sql)

            # Assert params for LIMIT parameter do not exceed chunk_size (<= 50)
            limit_params = [p[-1] for s, p in executed_sql if "LIMIT ?" in s]
            for lp in limit_params:
                self.assertLessEqual(lp, 50)
        finally:
            db_q._db_cur = orig_cur

    def test_malformed_cursor_rejection(self):
        with self.assertRaises(ValueError):
            db_q.decode_cursor("invalid_base64!!!")

        with self.assertRaises(ValueError):
            # Valid base64 but invalid HMAC signature
            tampered = "eyJ0cyI6MTAwMCwicm93aWQiOjF9LnRhbXBlcmVkX3NpZ25hdHVyZQ=="
            db_q.decode_cursor(tampered)


if __name__ == "__main__":
    unittest.main()
