"""Tests for Trackside -> Cloud Run snapshot sync."""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from core.db import Database


class TestCloudSnapshotSync(unittest.TestCase):
    def test_snapshot_export_apply_and_authenticated_endpoint(self):
        import web.app as app_mod
        from web import auth

        src_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        dst_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        src_tmp.close()
        dst_tmp.close()
        orig_db = app_mod.db
        orig_auth = app_mod.admin_auth
        try:
            src_db = Database(src_tmp.name)
            src_db.add_participant(10, "Elena", "E28000000010", apriltag_id=3, goal_type="laps", goal_value=5)
            src_db.start_event()
            t0 = src_db.get_event_state()["start_time"]
            src_db.set_participant_start(10, t0)
            src_db.record_lap(10, 1, t0 + 85.0, 85.0, -42.0)
            snapshot = src_db.export_snapshot()

            dst_db = Database(dst_tmp.name)
            app_mod.db = dst_db
            app_mod.admin_auth = auth.AdminAuth("654321")
            client = TestClient(app_mod.app)

            # Reject missing or wrong token
            self.assertEqual(client.post("/api/remote/snapshot", json=snapshot).status_code, 401)
            self.assertEqual(
                client.post("/api/remote/snapshot", json=snapshot, headers={"X-Sync-Token": "000000"}).status_code,
                401,
            )

            # Accept valid token and verify remote leaderboard mirrors trackside
            r = client.post("/api/remote/snapshot", json=snapshot, headers={"X-Sync-Token": "654321"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["total_laps"], 1)

            board = client.get("/api/leaderboard").json()
            self.assertEqual(len(board), 1)
            self.assertEqual(board[0]["name"], "Elena")
            self.assertEqual(board[0]["total_laps"], 1)
            self.assertEqual(board[0]["goal"]["remaining_laps"], 4)
        finally:
            app_mod.db = orig_db
            app_mod.admin_auth = orig_auth
            os.unlink(src_tmp.name)
            os.unlink(dst_tmp.name)


if __name__ == "__main__":
    unittest.main()
