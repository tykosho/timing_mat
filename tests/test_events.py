import asyncio
import csv
import io
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from core import exports
from core.db import Database, EventStateError, RegistrationError
from core.lap_engine import LapEngine
from core.models import TagRead
from core.registration_import import parse_registration_csv

EPC_A = "E28011700000020101010001"
EPC_B = "E28011700000020101010002"


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "race.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()


class TestEventLifecycle(DbTestCase):
    def test_starts_with_one_event_in_setup(self):
        events = self.db.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "setup")
        self.assertTrue(events[0]["is_current"])

    def test_start_pause_end_reopen(self):
        self.assertEqual(self.db.start_event(at=1000.0)["status"], "running")
        self.assertEqual(self.db.pause_event()["status"], "paused")
        resumed = self.db.start_event(at=2000.0)
        self.assertEqual(resumed["start_time"], 1000.0, "resuming must keep the original gun time")

        ended = self.db.end_event(at=3000.0)
        self.assertEqual(ended["status"], "ended")
        self.assertEqual(ended["end_time"], 3000.0)

        with self.assertRaises(EventStateError):
            self.db.start_event()
        with self.assertRaises(EventStateError):
            self.db.pause_event()

        reopened = self.db.reopen_event()
        self.assertEqual(reopened["status"], "paused")
        self.assertIsNone(reopened["end_time"])

    def test_cannot_end_an_event_that_never_started(self):
        with self.assertRaises(EventStateError):
            self.db.end_event()

    def test_end_closes_open_manual_breaks(self):
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.start_event(at=1000.0)
        self.db.start_manual_break(1)
        self.db.end_event(at=1000.0 + 120)
        self.assertIsNone(self.db.get_open_break(1))
        self.assertEqual(len(self.db.get_breaks_for_runner(1)), 1)

    def test_leaderboard_marks_runners_finished_after_end(self):
        now = time.time()   # status uses the wall clock (rest threshold), so use real timestamps
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.start_event(at=now - 60)
        self.db.set_participant_start(1, now - 50)
        self.assertEqual(self.db.get_leaderboard()[0]["status"], "active")
        self.db.end_event()
        self.assertEqual(self.db.get_leaderboard()[0]["status"], "finished")

    def test_reset_clears_results_but_keeps_registrations(self):
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.start_event(at=1000.0)
        self.db.set_participant_start(1, 1001.0)
        self.db.record_lap(1, 1, 1070.0, 69.0)
        self.db.insert_raw_reads([(self.db.current_event_id, EPC_A, 1070.0, -50.0)])
        self.db.reset_race_data()
        self.assertEqual(self.db.get_event()["status"], "setup")
        self.assertEqual(self.db.get_total_laps_count(), 0)
        self.assertEqual(self.db.count_raw_reads(), 0)
        self.assertEqual(self.db.get_participant_by_bib(1)["epc"], EPC_A)
        self.assertIsNone(self.db.get_participant_by_bib(1)["start_time"])

    def test_backup_is_a_usable_copy(self):
        self.db.add_participant(1, "Pat", EPC_A)
        path = self.db.backup("test")
        self.assertTrue(os.path.exists(path))
        copy = Database(path)
        self.assertEqual(copy.get_participant_by_bib(1)["name"], "Pat")


class TestMultipleEvents(DbTestCase):
    def test_events_are_isolated(self):
        first = self.db.current_event_id
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.record_lap(1, 1, 1070.0, 69.0)

        second = self.db.create_event("Spring Relay")
        self.db.set_current_event(second)

        self.assertEqual(self.db.get_all_participants(), [])
        self.assertEqual(self.db.get_total_laps_count(), 0)
        # The same bib and the same physical tag can be reused in a different event
        self.db.add_participant(1, "Sam", EPC_A)
        self.assertEqual(self.db.get_participant_by_epc(EPC_A)["name"], "Sam")
        self.assertEqual(self.db.get_participant_by_epc(EPC_A, event_id=first)["name"], "Pat")
        self.assertEqual(self.db.get_total_laps_count(event_id=first), 1)

    def test_cannot_switch_away_from_a_running_event(self):
        other = self.db.create_event("Other")
        self.db.start_event()
        with self.assertRaises(EventStateError):
            self.db.set_current_event(other)
        self.db.pause_event()
        self.db.set_current_event(other)
        self.assertEqual(self.db.current_event_id, other)

    def test_new_event_inherits_rules(self):
        self.db.update_event_settings(min_lap_seconds=30, max_lap_rest_seconds=900, track_length_m=250)
        new_id = self.db.create_event("Indoor")
        ev = self.db.get_event(new_id)
        self.assertEqual((ev["min_lap_seconds"], ev["max_lap_rest_seconds"], ev["track_length_m"]), (30, 900, 250))

    def test_current_event_survives_restart(self):
        other = self.db.create_event("Other")
        self.db.set_current_event(other)
        self.assertEqual(Database(self.db_path).current_event_id, other)


class TestLegacyMigration(unittest.TestCase):
    """A database created before events existed must come through intact."""

    def test_single_event_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "race.db")
            conn = sqlite3.connect(path)
            conn.executescript("""
            CREATE TABLE participants (bib_number INTEGER PRIMARY KEY, name TEXT NOT NULL, epc TEXT UNIQUE NOT NULL,
                notes TEXT DEFAULT '', created_at REAL NOT NULL, start_time REAL DEFAULT NULL, last_crossing_time REAL DEFAULT NULL);
            CREATE TABLE laps (id INTEGER PRIMARY KEY AUTOINCREMENT, bib_number INTEGER NOT NULL, lap_number INTEGER NOT NULL,
                crossing_time REAL NOT NULL, lap_duration_seconds REAL NOT NULL, peak_rssi REAL DEFAULT 0.0, manual_entry INTEGER DEFAULT 0);
            CREATE TABLE breaks (id INTEGER PRIMARY KEY AUTOINCREMENT, bib_number INTEGER NOT NULL, break_number INTEGER NOT NULL,
                start_time REAL NOT NULL, end_time REAL NOT NULL, duration_seconds REAL NOT NULL);
            CREATE TABLE event_state (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL, is_active INTEGER DEFAULT 0,
                start_time REAL, min_lap_seconds REAL DEFAULT 45.0, max_lap_rest_seconds REAL DEFAULT 600.0);
            INSERT INTO participants VALUES (7, 'Legacy Runner', 'E28011700000020101010009', 'old notes', 5.0, 1000.0, 1070.0);
            INSERT INTO laps (bib_number, lap_number, crossing_time, lap_duration_seconds) VALUES (7, 1, 1070.0, 70.0);
            INSERT INTO breaks (bib_number, break_number, start_time, end_time, duration_seconds) VALUES (7, 1, 1100.0, 1400.0, 300.0);
            INSERT INTO event_state VALUES (1, 'Club Night', 1, 990.0, 40.0, 500.0);
            """)
            conn.commit()
            conn.close()

            db = Database(path)

            event = db.get_event()
            self.assertEqual((event["name"], event["status"], event["start_time"]), ("Club Night", "running", 990.0))
            self.assertEqual((event["min_lap_seconds"], event["max_lap_rest_seconds"]), (40.0, 500.0))
            runner = db.get_participant_by_bib(7)
            self.assertEqual((runner["name"], runner["epc"], runner["notes"]), ("Legacy Runner", "E28011700000020101010009", "old notes"))
            self.assertEqual(len(db.get_laps_for_runner(7)), 1)
            self.assertEqual(db.get_breaks_for_runner(7)[0]["duration_seconds"], 300.0)
            self.assertEqual(len(db.list_events()), 1)
            # a safety copy of the old file was written before migrating
            self.assertTrue(os.listdir(os.path.join(tmp, "backups")))
            # and reopening is a no-op
            self.assertEqual(len(Database(path).get_all_participants()), 1)


class TestRegistrations(DbTestCase):
    def test_tag_is_optional_and_unique_per_event(self):
        self.db.add_participant(1, "Pat")           # pre-registered, no tag yet
        self.db.add_participant(2, "Sam", EPC_A)
        self.assertIsNone(self.db.get_participant_by_bib(1)["epc"])

        with self.assertRaisesRegex(RegistrationError, "already assigned to #2 Sam"):
            self.db.set_participant_tag(1, EPC_A)
        self.db.set_participant_tag(1, EPC_B.lower())  # normalised to upper case
        self.assertEqual(self.db.get_participant_by_bib(1)["epc"], EPC_B)

        self.db.set_participant_tag(1, None)
        self.assertIsNone(self.db.get_participant_by_bib(1)["epc"])

    def test_rejects_duplicate_bib_and_bad_epc(self):
        self.db.add_participant(1, "Pat")
        with self.assertRaisesRegex(RegistrationError, "already registered"):
            self.db.add_participant(1, "Someone Else")
        with self.assertRaisesRegex(RegistrationError, "hex"):
            self.db.add_participant(2, "Sam", "NOT-HEX!")

    def test_reassigning_same_tag_to_same_runner_is_fine(self):
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.set_participant_tag(1, EPC_A)

    def test_demo_seed_only_fills_an_empty_event(self):
        self.db.add_participant(50, "Real Runner")
        self.db.seed_demo_runners()
        self.assertEqual([p["bib_number"] for p in self.db.get_all_participants()], [50])


class TestCsvImport(DbTestCase):
    def test_google_forms_style_headers(self):
        text = ("Timestamp,Email Address,First Name,Last Name,Goal / comments\n"
                "11/1/2026 9:00,pat@example.com,Pat,Runner,10 laps\n"
                "11/1/2026 9:05,sam@example.com,Sam,Sprinter,\n")
        rows, errors = parse_registration_csv(text)
        self.assertEqual(errors, [])
        self.assertEqual([(r["name"], r["email"], r["notes"]) for r in rows],
                         [("Pat Runner", "pat@example.com", "10 laps"), ("Sam Sprinter", "sam@example.com", "")])

    def test_import_assigns_bibs_and_skips_duplicates(self):
        self.db.add_participant(2, "Existing Person", email="existing@example.com")
        text = ("Full Name,Email,Bib\n"
                "Pat Runner,pat@example.com,\n"
                "Existing Person,EXISTING@example.com,\n"      # same email -> skipped
                "Sam Sprinter,sam@example.com,3\n"            # explicit bib
                "Taken Bib,taken@example.com,3\n"              # bib clash -> skipped
                "Alex,alex@example.com,\n")
        rows, errors = parse_registration_csv(text)
        result = self.db.bulk_add_registrations(rows)

        bibs = {p["name"]: p["bib_number"] for p in self.db.get_all_participants()}
        # 2 is taken by the existing runner and 3 is reserved by Sam, so auto numbers are 1 and 4
        self.assertEqual(bibs, {"Pat Runner": 1, "Existing Person": 2, "Sam Sprinter": 3, "Alex": 4})
        reasons = " | ".join(s["reason"] for s in result["skipped"])
        self.assertIn("same email", reasons)
        self.assertIn("bib #3 already taken", reasons)

    def test_import_with_known_tags_and_tag_clash(self):
        text = f"Name,Tag EPC\nPat,{EPC_A}\nSam,{EPC_A}\nAlex,zz\n"
        rows, errors = parse_registration_csv(text)
        self.assertEqual(len(errors), 1)                       # 'zz' isn't hex
        result = self.db.bulk_add_registrations(rows)
        self.assertEqual(len(result["imported"]), 1)
        self.assertIn("already assigned", result["skipped"][0]["reason"])

    def test_reimporting_the_same_file_adds_nothing(self):
        text = "Name,Email\nPat,pat@example.com\nSam,\n"
        rows, _ = parse_registration_csv(text)
        self.db.bulk_add_registrations(rows)
        again = self.db.bulk_add_registrations(rows)
        self.assertEqual(again["imported"], [])
        self.assertEqual(len(self.db.get_all_participants()), 2)

    def test_semicolon_delimiter_bom_and_blank_lines(self):
        rows, errors = parse_registration_csv("﻿Name;Email\n\nPat;pat@example.com\n")
        self.assertEqual((len(rows), errors), (1, []))

    def test_missing_name_column_is_a_clear_error(self):
        with self.assertRaisesRegex(RegistrationError, "name column"):
            parse_registration_csv("Email,Bib\npat@example.com,1\n")


class TestEngineWithEvents(DbTestCase):
    def make_engine(self):
        return LapEngine(db=self.db, crossing_window=0.5)

    def test_reads_ignored_until_event_runs_then_logged_raw(self):
        self.db.add_participant(1, "Pat", EPC_A)
        engine = self.make_engine()

        engine.refresh_event()                      # setup: nothing is logged
        engine.process_tag_read(TagRead(epc=EPC_A, timestamp=10.0, rssi=-50.0))
        engine.flush_raw_reads()
        self.assertEqual(self.db.count_raw_reads(), 0)

        self.db.start_event(at=100.0)
        engine.refresh_event()
        engine.process_tag_read(TagRead(epc=EPC_A, timestamp=110.0, rssi=-52.0))
        engine.process_tag_read(TagRead(epc="DEADBEEF", timestamp=110.1, rssi=-70.0))  # unassigned tags are kept too
        engine.flush_raw_reads()
        self.assertEqual([r["epc"] for r in self.db.get_raw_reads()], [EPC_A, "DEADBEEF"])

        self.db.end_event(at=200.0)
        engine.refresh_event()                      # ended: logging stops
        engine.process_tag_read(TagRead(epc=EPC_A, timestamp=210.0, rssi=-50.0))
        engine.flush_raw_reads()
        self.assertEqual(self.db.count_raw_reads(), 2)

    def test_crossing_before_end_still_counts_but_after_end_does_not(self):
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.update_event_settings(min_lap_seconds=45.0)
        self.db.start_event(at=1000.0)
        engine = self.make_engine()
        asyncio.run(engine._resolve_crossing(EPC_A, [(1100.0, -45.0)]))   # chip start
        self.db.end_event(at=1180.0)

        # Burst that finished at t=1170 but is only flushed after End was pressed at 1180
        asyncio.run(engine._resolve_crossing(EPC_A, [(1170.0, -45.0)]))
        self.assertEqual(self.db.get_total_laps_count(), 1)

        # A crossing after the End is ignored
        asyncio.run(engine._resolve_crossing(EPC_A, [(1250.0, -45.0)]))
        self.assertEqual(self.db.get_total_laps_count(), 1)

    def test_paused_event_does_not_time_laps(self):
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.start_event(at=1000.0)
        engine = self.make_engine()
        asyncio.run(engine._resolve_crossing(EPC_A, [(1100.0, -45.0)]))
        with mock.patch("core.clock.now", return_value=1150.0):
            self.db.pause_event()
        asyncio.run(engine._resolve_crossing(EPC_A, [(1200.0, -45.0)]))
        self.assertEqual(self.db.get_total_laps_count(), 0)


class TestExports(DbTestCase):
    def rows(self, text):
        return list(csv.reader(io.StringIO(text)))

    def test_results_and_laps_csv(self):
        self.db.update_event_settings(track_length_m=400)
        self.db.add_participant(1, "Pat", EPC_A)
        self.db.add_participant(2, "Sam", EPC_B)
        self.db.start_event(at=1000.0)
        self.db.set_participant_start(1, 1000.0)
        for n, t in enumerate((1070.0, 1139.0), start=1):
            self.db.record_lap(1, n, t, 69.0 if n == 2 else 70.0)
        self.db.end_event(at=1200.0)

        results = self.rows(exports.results_csv(self.db))
        header, first, second = results
        self.assertEqual(first[header.index("name")], "Pat")
        self.assertEqual(first[header.index("laps")], "2")
        self.assertEqual(first[header.index("distance_km")], "0.8")
        self.assertEqual(first[header.index("fastest_lap_seconds")], "69.0")
        self.assertEqual(second[header.index("laps")], "0")

        laps = self.rows(exports.laps_csv(self.db))
        self.assertEqual(len(laps), 3)   # header + 2

    def test_names_that_look_like_formulas_are_neutralised(self):
        self.db.add_participant(1, '=HYPERLINK("http://evil","x")', EPC_A)
        out = self.rows(exports.registrations_csv(self.db))
        self.assertTrue(out[1][1].startswith("'="))


if __name__ == "__main__":
    unittest.main()
