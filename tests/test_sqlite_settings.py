"""SQLite durability settings and migrations (issue #15)."""
import os
import sqlite3
import tempfile
import time
import unittest

from core.db import Database


class SqliteSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "race.db")
        self.db = Database(self.path)

    def test_wal_mode_is_persistent(self):
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode;").fetchone()[0], "wal")

    def test_connections_use_synchronous_normal(self):
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous;").fetchone()[0], 1)  # 1 = NORMAL

    def test_connect_no_longer_alters_the_schema(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE laps DROP COLUMN flag_reason;")
        self.db.get_event_state()  # any connect(); used to re-add the column on every call
        with sqlite3.connect(self.path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(laps);")]
        self.assertNotIn("flag_reason", cols)
        # ...and opening the database migrates it, once
        Database(self.path)
        with sqlite3.connect(self.path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(laps);")]
        self.assertIn("flag_reason", cols)

    def test_backup_includes_data_still_in_the_wal(self):
        self.db.add_participant(101, "Alex", "E28011700000020101010001")
        self.db.start_event(at=1000.0)
        self.db.record_lap(101, 1, 1070.0, 70.0)
        self.assertTrue(os.path.exists(self.path + "-wal"))
        copy = Database(self.db.backup("test"))
        self.assertEqual(len(copy.get_laps_for_runner(101)), 1)

    def test_leaderboard_stays_fast_with_a_full_field(self):
        """Issue target: 100 runners x 30 laps well under 50 ms on a Pi. Generous bound for CI."""
        self.db.start_event(at=1000.0)
        with self.db.connect() as conn:
            eid = self.db.current_event_id
            for bib in range(1, 101):
                conn.execute("""INSERT INTO participants (event_id, bib_number, name, email, epc, notes, created_at, start_time, last_crossing_time)
                                VALUES (?, ?, ?, '', ?, '', 0, 1000, 3100);""", (eid, bib, f"R{bib}", f"E{bib:023d}"))
                conn.executemany("""INSERT INTO laps (event_id, bib_number, lap_number, crossing_time, lap_duration_seconds)
                                    VALUES (?, ?, ?, ?, 70.0);""", [(eid, bib, n, 1000 + n * 70) for n in range(1, 31)])
        start = time.perf_counter()
        board = self.db.get_leaderboard()
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.assertEqual(len(board), 100)
        self.assertEqual(board[0]["total_laps"], 30)
        self.assertLess(elapsed_ms, 250)


if __name__ == "__main__":
    unittest.main()
