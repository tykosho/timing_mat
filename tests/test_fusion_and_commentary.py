"""Tests for unified RFID + AprilTag sensor fusion and frugal non-repeating commentary."""
import asyncio
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from core.commentary import (
    BURN_QUIPS,
    FAST_QUIPS,
    PACK_BURN_QUIPS,
    PACK_HYPE_QUIPS,
    PR_QUIPS,
    CommentaryEngine,
    format_spoken_compact,
)
from core.db import Database
from core.lap_engine import LapEngine
from core.models import TagRead, apriltag_to_epc, epc_to_apriltag


class TestCommentaryEngine(unittest.TestCase):
    def test_library_size_and_counts(self):
        engine = CommentaryEngine(burn_frequency=25, seed=42)
        counts = engine.counts()
        self.assertEqual(counts["burns"], len(BURN_QUIPS))
        self.assertGreaterEqual(counts["burns"], 60)
        self.assertEqual(counts["prs"], len(PR_QUIPS))
        self.assertEqual(counts["fast"], len(FAST_QUIPS))
        self.assertEqual(counts["pack_hype"], len(PACK_HYPE_QUIPS))
        self.assertEqual(counts["pack_burns"], len(PACK_BURN_QUIPS))
        self.assertEqual(
            counts["total"],
            len(BURN_QUIPS) + len(PR_QUIPS) + len(FAST_QUIPS) + len(PACK_HYPE_QUIPS) + len(PACK_BURN_QUIPS),
        )

    def test_burn_deck_does_not_repeat_until_exhausted(self):
        engine = CommentaryEngine(burn_frequency=100, seed=7)
        seen = []
        for i in range(len(BURN_QUIPS)):
            res = engine.for_lap(
                name="Alex",
                lap_number=i + 2,
                lap_duration_seconds=95.0,
                prev_best_seconds=80.0,
                prev_avg_seconds=85.0,
                force_category="burn",
            )
            self.assertEqual(res["category"], "burn")
            self.assertIsNotNone(res["quip"])
            seen.append(res["quip"])
        # All burns in one full cycle must be unique
        self.assertEqual(len(set(seen)), len(BURN_QUIPS))
        # Reshuffle boundary should not immediately repeat the last burn
        next_res = engine.for_lap(
            name="Alex",
            lap_number=99,
            lap_duration_seconds=95.0,
            force_category="burn",
        )
        self.assertNotEqual(next_res["quip"], seen[-1])

    def test_frugal_speech_length(self):
        # Every burn + PR + fast line should stay concise (< 80 chars total spoken)
        engine = CommentaryEngine(burn_frequency=100, seed=1)
        for cat in ("pr", "fast", "burn"):
            for _ in range(20):
                res = engine.for_lap(
                    name="Elena",
                    lap_number=3,
                    lap_duration_seconds=74.3,
                    force_category=cat,
                )
                self.assertLessEqual(len(res["speech"]), 90, f"Too wordy: {res['speech']}")
                self.assertIn("Elena", res["speech"])

    def test_format_spoken_compact(self):
        self.assertEqual(format_spoken_compact(52.4), "52 seconds")
        self.assertEqual(format_spoken_compact(60.0), "1:00")
        self.assertEqual(format_spoken_compact(74.2), "1:14")
        self.assertEqual(format_spoken_compact(65.0), "1:05")

    def test_standard_lap_has_no_display_quip(self):
        engine = CommentaryEngine(burn_frequency=0, seed=1)
        res = engine.for_lap(
            name="Sam",
            lap_number=2,
            lap_duration_seconds=90.0,
            prev_best_seconds=80.0,
            prev_avg_seconds=85.0,
        )
        self.assertEqual(res["category"], "standard")
        self.assertIsNone(res["quip"])
        self.assertIn("Sam", res["speech"])

    def test_pr_detected_automatically(self):
        engine = CommentaryEngine(burn_frequency=100, seed=1)
        res = engine.for_lap(
            name="Maya",
            lap_number=4,
            lap_duration_seconds=75.0,
            prev_best_seconds=78.5,
            prev_avg_seconds=82.0,
        )
        self.assertEqual(res["category"], "pr")
        self.assertIsNotNone(res["quip"])


class TestSensorFusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.db.start_event()
        self.db.update_event_settings(min_lap_seconds=10.0)
        self.started = []
        self.laps = []
        self.engine = LapEngine(
            db=self.db,
            crossing_window=1.0,
            on_lap_completed=lambda d: self.laps.append(d),
            on_runner_started=lambda d: self.started.append(d),
            auto_register_unassigned=True,
        )
        self.engine.refresh_event()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_apriltag_epc_roundtrip(self):
        epc = apriltag_to_epc(17)
        self.assertEqual(epc_to_apriltag(epc), 17)
        self.assertIsNone(epc_to_apriltag("E28011700000020101010001"))

    def test_fused_crossing_counts_once_and_tags_source_fused(self):
        rfid_epc = "E28011700000020101010099"
        self.db.add_participant(10, "Elena", rfid_epc, apriltag_id=5)

        t0 = self.db.get_event_state()["start_time"] + 1.0
        # Simultaneous RFID + AprilTag at start -> ONE start with source="fused"
        self.engine.process_tag_read(TagRead(epc=rfid_epc, timestamp=t0, rssi=-48.0, source="rfid"))
        self.engine.process_tag_read(
            TagRead(epc=apriltag_to_epc(5), timestamp=t0 + 0.15, rssi=-42.0, source="apriltag", apriltag_id=5)
        )
        asyncio.run(self.engine.flush_once(t0 + 2.0))

        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0]["bib_number"], 10)
        self.assertEqual(self.started[0]["source"], "fused")

        # Lap 1: simultaneous RFID + AprilTag -> ONE lap with source="fused"
        t1 = t0 + 30.0
        self.engine.process_tag_read(
            TagRead(epc=apriltag_to_epc(5), timestamp=t1, rssi=-42.0, source="apriltag", apriltag_id=5)
        )
        self.engine.process_tag_read(TagRead(epc=rfid_epc, timestamp=t1 + 0.2, rssi=-45.0, source="rfid"))
        asyncio.run(self.engine.flush_once(t1 + 2.0))

        self.assertEqual(len(self.laps), 1)
        self.assertEqual(self.laps[0]["lap_number"], 1)
        self.assertEqual(self.laps[0]["source"], "fused")

        # Lap 2: RFID misses, AprilTag catches it -> source="apriltag"
        t2 = t1 + 32.0
        self.engine.process_tag_read(
            TagRead(epc=apriltag_to_epc(5), timestamp=t2, rssi=-42.0, source="apriltag", apriltag_id=5)
        )
        asyncio.run(self.engine.flush_once(t2 + 2.0))

        self.assertEqual(len(self.laps), 2)
        self.assertEqual(self.laps[1]["lap_number"], 2)
        self.assertEqual(self.laps[1]["source"], "apriltag")

        # Lap 3: Camera misses, RFID catches it -> source="rfid"
        t3 = t2 + 31.0
        self.engine.process_tag_read(TagRead(epc=rfid_epc, timestamp=t3, rssi=-47.0, source="rfid"))
        asyncio.run(self.engine.flush_once(t3 + 2.0))

        self.assertEqual(len(self.laps), 3)
        self.assertEqual(self.laps[2]["lap_number"], 3)
        self.assertEqual(self.laps[2]["source"], "rfid")

    def test_apriltag_only_walkup_auto_registers(self):
        t0 = self.db.get_event_state()["start_time"] + 2.0
        self.engine.process_tag_read(
            TagRead(epc=apriltag_to_epc(12), timestamp=t0, rssi=-42.0, source="apriltag", apriltag_id=12)
        )
        asyncio.run(self.engine.flush_once(t0 + 2.0))

        runner = self.db.get_participant_by_apriltag(12)
        self.assertIsNotNone(runner)
        self.assertEqual(runner["apriltag_id"], 12)
        # Walk-up tags get a placeholder but are not timed until a name is entered (PR #135)
        self.assertEqual(len(self.started), 0)

    def test_apriltag_assignment_swaps_between_runners(self):
        self.db.add_participant(1, "Runner A", "E28000000001", apriltag_id=3)
        self.db.add_participant(2, "Runner B", "E28000000002", apriltag_id=4)
        self.db.set_participant_apriltag(2, 3, swap=True)
        a = self.db.get_participant_by_bib(1)
        b = self.db.get_participant_by_bib(2)
        self.assertEqual(a["apriltag_id"], 4)
        self.assertEqual(b["apriltag_id"], 3)


class TestUnifiedWebAPI(unittest.TestCase):
    def test_announcer_and_camera_endpoints(self):
        from web.app import app

        client = TestClient(app)
        r = client.get("/api/announcer/config")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("burn_frequency", body)
        self.assertGreaterEqual(body["counts"]["burns"], 60)

        r2 = client.post("/api/announcer/config", json={"burn_frequency": 40})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["burn_frequency"], 40)

        r3 = client.post("/api/announcer/test", json={"name": "Elena", "category": "burn"})
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["category"], "burn")
        self.assertIsNotNone(r3.json()["quip"])

        r4 = client.get("/api/cameras")
        self.assertEqual(r4.status_code, 200)
        self.assertIn("available", r4.json())
        self.assertIn("health", r4.json())


if __name__ == "__main__":
    unittest.main()
