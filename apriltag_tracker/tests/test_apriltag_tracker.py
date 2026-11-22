"""End-to-end unit and integration tests for the Trackside AprilTag Lap Counter."""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
import pupil_apriltags
from fastapi.testclient import TestClient

APRILTAG_DIR = Path(__file__).resolve().parents[1]
if str(APRILTAG_DIR) not in sys.path:
    sys.path.insert(0, str(APRILTAG_DIR))

from app import create_app  # noqa: E402
from generate_tags import (  # noqa: E402
    TAG16H5_COUNT,
    render_printable_bibs_html,
    render_tag16h5_image,
)
from tracker import SessionTracker, build_lap_commentary, format_lap_mmss  # noqa: E402
from vision import (  # noqa: E402
    FaceCropper,
    SyntheticTrackScene,
    VisionEngine,
    detect_apriltags_16h5,
)


class TestTag16h5AndVision(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = pupil_apriltags.Detector(families="tag16h5")

    def test_all_30_printable_tags_decode_with_high_margin(self) -> None:
        for tid in range(TAG16H5_COUNT):
            img = render_tag16h5_image(tid, tag_px=180, border_cells=2)
            accepted, rejected = detect_apriltags_16h5(
                self.detector, img, decision_margin_min=35.0, min_tag_area_ratio=0.01
            )
            self.assertEqual(len(accepted), 1, f"Failed to detect tag {tid}")
            self.assertEqual(accepted[0].tag_id, tid)
            self.assertGreaterEqual(accepted[0].decision_margin, 35.0)
            self.assertEqual(len(rejected), 0)

    def test_small_area_and_high_margin_threshold_filtering(self) -> None:
        # Tiny distant tag on a 1280x720 frame should be rejected by min_tag_area_ratio
        canvas = np.full((720, 1280), 255, dtype=np.uint8)
        tiny_tag = render_tag16h5_image(4, tag_px=24, border_cells=1)
        th, tw = tiny_tag.shape[:2]
        canvas[200 : 200 + th, 400 : 400 + tw] = tiny_tag

        accepted, rejected = detect_apriltags_16h5(
            self.detector, canvas, decision_margin_min=35.0, min_tag_area_ratio=0.005
        )
        self.assertEqual(len(accepted), 0)
        self.assertGreaterEqual(len(rejected), 1)

    def test_fallback_portrait_crop_above_chest_tag(self) -> None:
        scene = SyntheticTrackScene(1280, 720)
        frame = scene._bg.copy()
        bbox = scene.render_runner_on_frame(frame, tag_id=12, center_x=640, chest_y=460)
        cropper = FaceCropper()
        b64 = cropper.crop_portrait_base64(frame, tag_bbox=bbox)
        self.assertTrue(b64.startswith("data:image/png;base64,"))
        raw = base64.b64decode(b64.split(",", 1)[1])
        decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.shape, (256, 256, 3))

    def test_printable_html_contains_all_30_bibs(self) -> None:
        for per_page, expected_pages in ((1, 30), (2, 15), (6, 5), (15, 2), (30, 1)):
            html_doc = render_printable_bibs_html(per_page=per_page)
            self.assertEqual(html_doc.count('<section class="print-page'), expected_pages)
            for tid in range(30):
                self.assertIn(f"#{tid:02d}", html_doc)
                self.assertIn(f'aria-label="AprilTag 16h5 ID {tid}"', html_doc)


class TestSessionTrackerPersistenceAndMath(unittest.TestCase):
    def test_cooldown_debounce_leaderboard_sort_and_disk_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            tracker = SessionTracker(data_dir=data_dir)
            tracker.update_config(cooldown_seconds=25.0, burn_frequency=100)

            t0 = 1_000_000.0
            reg_a = tracker.register_runner(tag_id=3, name="Maya Lin", now=t0)
            reg_b = tracker.register_runner(tag_id=8, name="Carlos Rivera", now=t0)
            self.assertIn("Welcome to the track, Maya Lin. Tag 3 locked in.", reg_a["speech_text"])
            self.assertIn("Carlos Rivera", reg_b["speech_text"])
            self.assertTrue((data_dir / "faces" / "3.png").exists())
            self.assertTrue((data_dir / "faces" / "8.png").exists())

            # Stepping away 2 seconds after registration is blocked by cooldown
            self.assertIsNone(tracker.record_crossing(3, now=t0 + 2.0))

            # Valid Lap 1 after cooldown
            lap1 = tracker.record_crossing(3, now=t0 + 70.0)
            self.assertIsNotNone(lap1)
            self.assertEqual(lap1["lap_number"], 1)
            self.assertAlmostEqual(lap1["lap_time"], 70.0, places=1)
            self.assertEqual(format_lap_mmss(lap1["lap_time"]), "01:10.0")

            # Immediate duplicate crossing within 25s cooldown is rejected
            self.assertIsNone(tracker.record_crossing(3, now=t0 + 80.0))

            # Lap 2 is a faster PR -> PR category overrides burn_frequency=100
            lap2 = tracker.record_crossing(3, now=t0 + 135.0)
            self.assertIsNotNone(lap2)
            self.assertEqual(lap2["lap_number"], 2)
            self.assertTrue(lap2["is_pr"])
            self.assertEqual(lap2["speech_category"], "pr")

            # Runner B completes 2 laps faster total time -> ranks #1 ahead of Runner A
            tracker.record_crossing(8, now=t0 + 60.0)
            tracker.record_crossing(8, now=t0 + 120.0)

            board = tracker.leaderboard(now=t0 + 140.0)
            self.assertEqual([r["tag_id"] for r in board], [8, 3])
            self.assertEqual(board[0]["rank"], 1)

            # Manual +1 / -1 override
            tracker.adjust_laps(3, delta=1, now=t0 + 141.0)
            self.assertEqual(tracker.leaderboard(now=t0 + 141.0)[0]["tag_id"], 3)
            tracker.adjust_laps(3, delta=-1, now=t0 + 142.0)
            self.assertEqual(tracker.runners[3].laps, 2)

            # Reload from disk (simulates browser refresh / server restart)
            reloaded = SessionTracker(data_dir=data_dir)
            self.assertEqual(reloaded.runners[3].name, "Maya Lin")
            self.assertEqual(reloaded.runners[3].laps, 2)
            self.assertEqual(reloaded.runners[8].laps, 2)

            # Reassign Maya from misread tag #3 -> unused tag #14 (laps & face preserved)
            moved = reloaded.edit_runner(old_tag_id=3, name="Maya Lin", new_tag_id=14)
            self.assertIsNotNone(moved)
            self.assertEqual(moved["tag_id"], 14)
            self.assertEqual(moved["laps"], 2)
            self.assertNotIn(3, reloaded.runners)
            self.assertTrue((data_dir / "faces" / "14.png").exists())

            # Swap tags between Maya (#14) and Carlos (#8) -> each keeps own laps & name
            reloaded.adjust_laps(14, delta=1)  # Maya now has 3 laps, Carlos has 2
            reloaded.edit_runner(old_tag_id=14, name="Maya Lin", new_tag_id=8)
            self.assertEqual(reloaded.runners[8].name, "Maya Lin")
            self.assertEqual(reloaded.runners[8].laps, 3)
            self.assertEqual(reloaded.runners[14].name, "Carlos Rivera")
            self.assertEqual(reloaded.runners[14].laps, 2)

            # Delete erroneously added runner
            self.assertTrue(reloaded.delete_runner(14))
            self.assertNotIn(14, reloaded.runners)

    def test_burn_commentary_when_slow_lap(self) -> None:
        line = build_lap_commentary(
            name="Jordan",
            laps=3,
            last_lap=98.4,
            is_pr=False,
            is_fast=False,
            is_slow=True,
            burn_frequency=25,
        )
        self.assertEqual(line["category"], "burn")
        self.assertIn("Jordan", line["text"])


class TestFastApiEndpointsAndWebSocket(unittest.TestCase):
    def test_api_register_simulate_and_websocket_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp),
                force_simulated_camera=True,
                start_vision=False,
            )
            with TestClient(app) as client:
                res_index = client.get("/")
                self.assertEqual(res_index.status_code, 200)
                self.assertIn("Trackside AprilTag Tracker", res_index.text)

                res_bibs = client.get("/api/tags/printable?per_page=6")
                self.assertEqual(res_bibs.status_code, 200)
                self.assertIn("#29", res_bibs.text)

                with client.websocket_connect("/ws") as ws:
                    initial = ws.receive_json()
                    self.assertEqual(initial["type"], "STATE_SYNC")

                    # Simulate unknown tag scan -> emits REGISTER_PROMPT
                    sim_res = client.post("/api/simulate/scan", json={"mode": "unknown", "tag_id": 5})
                    self.assertEqual(sim_res.status_code, 200)
                    msg1 = ws.receive_json()
                    msg2 = ws.receive_json()
                    types = {msg1["type"], msg2["type"]}
                    self.assertIn("REGISTER_PROMPT", types)

                    # Register runner -> saves face PNG and emits RUNNER_REGISTERED
                    reg_res = client.post(
                        "/api/register",
                        json={"tag_id": 5, "name": "Priya Patel"},
                    )
                    self.assertEqual(reg_res.status_code, 200)
                    ev1 = ws.receive_json()
                    ev2 = ws.receive_json()
                    self.assertEqual(ev1["type"], "RUNNER_REGISTERED")
                    self.assertIn("Priya Patel", ev1["speech_text"])
                    self.assertEqual(ev2["type"], "STATE_SYNC")

                    face_res = client.get("/faces/5.png")
                    self.assertEqual(face_res.status_code, 200)
                    self.assertEqual(face_res.headers["content-type"], "image/png")

                    # Simulate lap crossing -> emits LAP_EVENT
                    lap_res = client.post(
                        "/api/simulate/scan",
                        json={"mode": "lap", "tag_id": 5, "split_seconds": 72.4},
                    )
                    self.assertEqual(lap_res.status_code, 200)
                    lap_msg = ws.receive_json()
                    self.assertEqual(lap_msg["type"], "LAP_EVENT")
                    self.assertEqual(lap_msg["lap_number"], 1)
                    self.assertEqual(lap_msg["lap_mmss"], "01:12.4")

    def test_camera_list_and_index_switching(self) -> None:
        mock_cameras = [
            {"index": 0, "name": "OBS Virtual Camera"},
            {"index": 1, "name": "Continuity Camera"},
            {"index": 2, "name": "FaceTime HD Camera"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp),
                force_simulated_camera=True,
                start_vision=False,
                camera_lister=lambda: mock_cameras,
            )
            with TestClient(app) as client:
                res = client.get("/api/cameras")
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertEqual(body["cameras"], mock_cameras)
                self.assertEqual(body["active_index"], 0)

                cfg_res = client.post("/api/config", json={"camera_index": 2})
                self.assertEqual(cfg_res.status_code, 200)
                self.assertEqual(cfg_res.json()["config"]["camera_index"], 2)

                res_after = client.get("/api/cameras")
                self.assertEqual(res_after.json()["active_index"], 2)

            # Verify camera_index persisted to session.json
            reloaded = SessionTracker(data_dir=Path(tmp))
            self.assertEqual(reloaded.camera_index, 2)

    def test_live_vision_loop_detects_synthetic_runner_and_counts_lap(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmp:
            events: list[dict] = []
            tracker = SessionTracker(data_dir=Path(tmp))
            tracker.update_config(cooldown_seconds=5.0, simulated_mode=True)
            engine = VisionEngine(
                tracker=tracker,
                on_event=events.append,
                force_simulated_camera=True,
            )
            engine.start()
            try:
                # 1. Spawn unknown runner pass wearing tag #11 -> CV loop detects & prompts
                engine.synth_scene.spawn_runner_pass(11, duration=0.8)
                deadline = time.time() + 3.0
                while time.time() < deadline and not any(
                    e.get("type") == "REGISTER_PROMPT" and e.get("tag_id") == 11 for e in events
                ):
                    time.sleep(0.05)
                self.assertTrue(
                    any(e.get("type") == "REGISTER_PROMPT" and e.get("tag_id") == 11 for e in events)
                )

                # 2. Register TWO runners and spawn a simultaneous side-by-side pack pass!
                tracker.register_runner(11, "Samira Hassan", apply_initial_cooldown=False)
                tracker.register_runner(19, "Liam O'Connor", apply_initial_cooldown=False)
                engine.synth_scene.spawn_runner_pass(11, duration=0.95, slot=0)
                engine.synth_scene.spawn_runner_pass(19, duration=0.95, slot=1)
                deadline = time.time() + 3.0
                while time.time() < deadline and (
                    tracker.runners[11].laps < 1 or tracker.runners[19].laps < 1
                ):
                    time.sleep(0.05)
                self.assertEqual(tracker.runners[11].laps, 1)
                self.assertEqual(tracker.runners[19].laps, 1)
                seq, jpeg = engine.wait_for_jpeg(-1, timeout=0.5)
                self.assertGreater(seq, 0)
                self.assertGreater(len(jpeg), 1000)
            finally:
                engine.stop()


if __name__ == "__main__":
    unittest.main()
