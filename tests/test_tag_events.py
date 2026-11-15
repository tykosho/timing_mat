"""Raw tag reads go only to admin pages, one combined message per tag (issue #12)."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("DB_PATH", os.path.join(_tmp.name, "tag_events_import.db"))
os.environ.setdefault("MOCK_HARDWARE", "true")
os.environ.setdefault("SEED_DEMO", "false")

try:
    from fastapi.testclient import TestClient
except ImportError:  # httpx missing
    TestClient = None

from core.db import Database  # noqa: E402
from web.auth import AdminAuth, COOKIE_NAME  # noqa: E402


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


@unittest.skipIf(TestClient is None, "fastapi TestClient unavailable")
class TagEventTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import app as web_app
        cls.app = web_app

    def setUp(self):
        self.public, self.admin = FakeWS(), FakeWS()
        for name, value in [("active_connections", [self.public, self.admin]),
                            ("admin_connections", [self.admin]),
                            ("pending_tags", {})]:
            patcher = mock.patch.object(self.app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def read(self, epc, rssi, t=1.0):
        self.app._note_tag({"epc": epc, "timestamp": t, "rssi": rssi})

    def flush(self):
        asyncio.run(self.app.flush_tag_events())

    def test_bursts_become_one_message_per_tag_with_strongest_rssi(self):
        for i, rssi in enumerate([-70, -55, -48, -60] * 5):   # 20 reads of one tag
            self.read("AAA", rssi, t=i * 0.01)
        self.read("BBB", -65)
        self.flush()
        msgs = {m["data"]["epc"]: m["data"]["rssi"] for m in self.admin.sent}
        self.assertEqual(msgs, {"AAA": -48, "BBB": -65})
        self.assertTrue(all(m["type"] == "tag_detected" for m in self.admin.sent))

    def test_public_screens_never_get_raw_reads(self):
        self.read("AAA", -50)
        self.flush()
        self.assertEqual(self.public.sent, [])
        self.assertEqual(len(self.admin.sent), 1)

    def test_flush_clears_pending_and_skips_when_nobody_listens(self):
        self.read("AAA", -50)
        self.flush()
        self.flush()                                  # nothing new
        self.assertEqual(len(self.admin.sent), 1)
        self.app.admin_connections.clear()
        self.read("BBB", -50)
        self.flush()
        self.assertEqual(self.app.pending_tags, {})

    def test_lap_broadcasts_still_reach_everyone(self):
        asyncio.run(self.app.broadcast_ws({"type": "leaderboard_update"}))
        self.assertEqual(len(self.public.sent), 1)
        self.assertEqual(len(self.admin.sent), 1)

    def test_dead_connection_is_dropped_from_both_lists(self):
        class Dead(FakeWS):
            async def send_json(self, message):
                raise RuntimeError("closed")
        dead = Dead()
        self.app.active_connections.append(dead)
        self.app.admin_connections.append(dead)
        asyncio.run(self.app.broadcast_ws({"type": "x"}, admin_only=True))
        self.assertNotIn(dead, self.app.admin_connections)
        self.assertNotIn(dead, self.app.active_connections)


@unittest.skipIf(TestClient is None, "fastapi TestClient unavailable")
class AdminChannelTest(unittest.TestCase):
    """Only a logged-in admin's ?channel=admin socket is put on the admin list."""

    @classmethod
    def setUpClass(cls):
        from web import app as web_app
        cls.app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = mock.patch.object(web_app, "db", Database(os.path.join(cls.tmp.name, "ws.db")))
        cls.db_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.auth = AdminAuth("482913")
        for name, value in [("admin_auth", self.auth), ("active_connections", []), ("admin_connections", [])]:
            patcher = mock.patch.object(self.app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def connected_as(self, path, cookie=None):
        client = TestClient(self.app.app)
        if cookie:
            client.cookies.set(COOKIE_NAME, cookie)
        with client.websocket_connect(path) as ws:
            self.assertEqual(ws.receive_json()["type"], "init")
            return len(self.app.active_connections), len(self.app.admin_connections)

    def test_public_socket(self):
        self.assertEqual(self.connected_as("/ws/live"), (1, 0))

    def test_admin_channel_without_login_is_public_only(self):
        self.assertEqual(self.connected_as("/ws/live?channel=admin"), (1, 0))
        self.assertEqual(self.connected_as("/ws/live?channel=admin", cookie="forged"), (1, 0))

    def test_admin_channel_with_login(self):
        self.assertEqual(self.connected_as("/ws/live?channel=admin", cookie=self.auth.session_token()), (1, 1))

    def test_admin_pages_ask_for_the_admin_channel(self):
        # crossings.html is admin-only too and needs unassigned-tag prompts (#52)
        for name in ("admin.html", "prepare.html", "crossings.html"):
            with open(os.path.join("web", "templates", name)) as f:
                self.assertIn("/ws/live?channel=admin", f.read(), name)
        for name in ("leaderboard.html", "replay.html"):
            with open(os.path.join("web", "templates", name)) as f:
                self.assertNotIn("channel=admin", f.read(), name)


if __name__ == "__main__":
    unittest.main()
