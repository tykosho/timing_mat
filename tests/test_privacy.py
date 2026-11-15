"""Public pages and feeds don't expose tag IDs, notes or emails (issue #52)."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("DB_PATH", os.path.join(_tmp.name, "privacy_import.db"))
os.environ.setdefault("MOCK_HARDWARE", "true")
os.environ.setdefault("SEED_DEMO", "false")

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

from core.db import Database  # noqa: E402
from web.auth import AdminAuth  # noqa: E402

EPC = "E280117000000201DEADBEEF"
NOTES = "Asthma: inhaler in the blue bag"
EMAIL = "alex.runner@example.com"
SECRETS = (EPC, NOTES, EMAIL, '"epc"', '"notes"', '"email"')


@unittest.skipIf(TestClient is None, "fastapi TestClient unavailable")
class PrivacyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import app as web_app
        cls.app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(cls.tmp.name, "privacy.db"))
        db.add_participant(101, "Alex", EPC, NOTES, email=EMAIL)
        db.start_event(at=1000.0)
        db.set_participant_start(101, 1000.0)
        db.record_lap(101, 1, 1070.0, 70.0)
        cls.auth = AdminAuth("482913")
        cls.patches = [mock.patch.object(web_app, "db", db), mock.patch.object(web_app.lap_engine, "db", db),
                       mock.patch.object(web_app, "admin_auth", cls.auth)]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def assertNoSecrets(self, text, where):
        for secret in SECRETS:
            self.assertNotIn(secret, text, f"{where} leaks {secret}")

    def test_public_runner_page_hides_tag_and_notes(self):
        page = TestClient(self.app.app).get("/runner/101")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Alex", page.text)
        self.assertNoSecrets(page.text, "/runner/101")

    def test_admin_still_sees_them(self):
        client = TestClient(self.app.app)
        client.post("/login", data={"pin": "482913", "next": "/admin"})
        page = client.get("/runner/101").text
        self.assertIn(EPC, page)
        self.assertIn(NOTES, page)

    def test_public_api_and_websocket(self):
        client = TestClient(self.app.app)
        for path in ("/api/leaderboard", "/api/event", "/leaderboard"):
            res = client.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertNoSecrets(res.text, path)
        with client.websocket_connect("/ws/live") as ws:
            self.assertNoSecrets(json.dumps(ws.receive_json()), "WebSocket init")

    def test_unassigned_tag_prompt_goes_to_admins_only(self):
        class FakeWS:
            def __init__(self):
                self.sent = []

            async def send_json(self, message):
                self.sent.append(message)

        public, admin = FakeWS(), FakeWS()
        with mock.patch.object(self.app, "active_connections", [public, admin]), \
                mock.patch.object(self.app, "admin_connections", [admin]):
            asyncio.run(self.app.on_unassigned_tag({"epc": EPC, "rssi": -50.0, "timestamp": 1.0}))
        self.assertEqual(public.sent, [])
        self.assertEqual(admin.sent[0]["type"], "unassigned_tag_detected")


if __name__ == "__main__":
    unittest.main()
