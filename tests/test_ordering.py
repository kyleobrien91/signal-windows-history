import sqlite3
import unittest
from unittest.mock import patch

import db
import db.queries as db_q


class TestVideoOrdering(unittest.TestCase):

    def setUp(self):
        # Create an in-memory SQLite database mimicking Signal's SQLCipher schema
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

        # Insert test conversation
        self.cur.execute("INSERT INTO conversations (id, name, type) VALUES ('conv1', 'Test Group', 'group');")

        # Insert messages with varying timestamps
        # msg1: oldest (ts: 1000000000000)
        # msg2: middle (ts: 1000000050000)
        # msg3: newest (ts: 1000000100000)
        # msg4: sentAt is 0, fallback to message sent_at (ts: 1000000200000 - newest of all)
        self.cur.execute("""
            INSERT INTO messages (id, conversationId, sent_at, timestamp) VALUES
            ('msg1', 'conv1', 1000000000000, 1000000000000),
            ('msg2', 'conv1', 1000000050000, 1000000050000),
            ('msg3', 'conv1', 1000000100000, 1000000100000),
            ('msg4', 'conv1', 1000000200000, 1000000200000);
        """)

        self.cur.execute("""
            INSERT INTO message_attachments (messageId, conversationId, sentAt, contentType, path, localKey, fileName, size) VALUES
            ('msg1', 'conv1', 1000000000000, 'video/mp4', 'path/to/vid1.mp4', 'key1', 'vid1.mp4', 1024),
            ('msg2', 'conv1', 1000000050000, 'video/mp4', 'path/to/vid2.mp4', 'key2', 'vid2.mp4', 2048),
            ('msg3', 'conv1', 1000000100000, 'video/mp4', 'path/to/vid3.mp4', 'key3', 'vid3.mp4', 3072),
            ('msg4', 'conv1', 0,             'video/mp4', 'path/to/vid4.mp4', 'key4', 'vid4.mp4', 4096);
        """)
        self.conn.commit()

        self.old_conn = db.set_active_db(self.conn, self.cur)

    def tearDown(self):
        db.set_active_db(self.old_conn, self.old_conn.cursor() if self.old_conn else None)
        self.conn.close()

    def test_default_ordering_newest_to_oldest(self):
        """Verify that query_media returns videos ordered newest to oldest by default."""
        media, lookup = db.query_media('conv1')
        self.assertEqual(len(media), 4)

        # Expected order: vid4 (200000), vid3 (100000), vid2 (50000), vid1 (0)
        filenames = [m['filename'] for m in media]
        self.assertEqual(filenames, ['vid4.mp4', 'vid3.mp4', 'vid2.mp4', 'vid1.mp4'])

        # Check sent_at values are descending
        timestamps = [m['sent_at'] for m in media]
        self.assertEqual(timestamps, [1000000200000, 1000000100000, 1000000050000, 1000000000000])
        self.assertTrue(all(timestamps[i] >= timestamps[i+1] for i in range(len(timestamps)-1)))

    def test_all_groups_ordering_newest_to_oldest(self):
        """Verify that query_media('all') also orders newest to oldest."""
        media, _ = db.query_media('all')
        self.assertEqual(len(media), 4)
        timestamps = [m['sent_at'] for m in media]
        self.assertEqual(timestamps, [1000000200000, 1000000100000, 1000000050000, 1000000000000])


if __name__ == '__main__':
    unittest.main()
