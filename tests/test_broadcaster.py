"""A stalled WebSocket client can't hold up lap timing or the other screens (issue #84)."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("MOCK_HARDWARE", "true")
os.environ.setdefault("SEED_DEMO", "false")

from fastapi.testclient import TestClient  # noqa: E402

from core.db import Database  # noqa: E402
from core.lap_engine import LapEngine  # noqa: E402
from web.broadcaster import Broadcaster, serialize  # noqa: E402

EPC = "E28011700000020101010001"


class GoodWS:
    def __init__(self):
        self.sent = []
        self.texts = []

    async def send_text(self, text):
        self.texts.append(text)
        self.sent.append(json.loads(text))


class StalledWS:
    """Handshake done, then never reads again: every send waits forever."""

    def __init__(self):
        self.attempts = 0
        self.closed = False

    async def send_text(self, text):
        self.attempts += 1
        await asyncio.Event().wait()

    async def close(self, code=1000):
        self.closed = True


async def wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


class BroadcasterTest(unittest.TestCase):
    def test_serialized_once_and_same_text_as_send_json(self):
        good1, good2 = GoodWS(), GoodWS()
        message = {"type": "lap_completed", "lap": {"name": "Zoë", "lap_number": 3}}

        async def scenario():
            b = Broadcaster()
            with mock.patch("web.broadcaster.json.dumps", wraps=json.dumps) as dumps:
                b.publish(message, [good1, good2])
                self.assertEqual(dumps.call_count, 1)
            await b.drain()

        asyncio.run(scenario())
        self.assertEqual(good1.sent, [message])
        self.assertIs(good1.texts[0], good2.texts[0])
        # Starlette's send_json encoding
        self.assertEqual(serialize(message), json.dumps(message, separators=(",", ":"), ensure_ascii=False))

    def test_publish_never_waits_for_a_stalled_client(self):
        stalled, good = StalledWS(), GoodWS()
        dropped = []

        async def scenario():
            b = Broadcaster(on_drop=dropped.append, send_timeout=0.2)
            started = time.monotonic()
            for i in range(20):
                b.publish({"type": "x", "i": i}, [stalled, good])
            self.assertLess(time.monotonic() - started, 0.05)
            await wait_until(lambda: len(good.sent) == 20)
            await wait_until(lambda: dropped == [stalled])  # after send_timeout
            await asyncio.sleep(0.01)
            self.assertTrue(stalled.closed)
            b.publish({"type": "after"}, [good])           # the rest carry on
            await b.drain()

        asyncio.run(scenario())
        self.assertEqual([m["i"] for m in good.sent[:20]], list(range(20)))
        self.assertEqual(good.sent[-1], {"type": "after"})
        self.assertEqual(stalled.attempts, 1)

    def test_client_too_far_behind_is_dropped(self):
        stalled = StalledWS()
        dropped = []

        async def scenario():
            b = Broadcaster(on_drop=dropped.append, send_timeout=60.0, max_queued=3)
            for i in range(10):
                b.publish({"i": i}, [stalled])
            self.assertEqual(dropped, [stalled])
            await asyncio.sleep(0.01)

        asyncio.run(scenario())
        self.assertTrue(stalled.closed)

    def test_failed_send_forgets_the_client(self):
        class Dead(GoodWS):
            async def send_text(self, text):
                raise RuntimeError("closed")
        dead, dropped = Dead(), []

        async def scenario():
            b = Broadcaster(on_drop=dropped.append)
            b.publish({"type": "x"}, [dead])
            await b.drain()

        asyncio.run(scenario())
        self.assertEqual(dropped, [dead])


class StalledClientDuringLapsTest(unittest.TestCase):
    """End to end through the app's real callbacks: the lap engine resolves crossings while
    one connected phone has stopped reading."""
    SEND_TIMEOUT = 3.0

    @classmethod
    def setUpClass(cls):
        from web import app as web_app
        cls.app = web_app

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        for bib in range(1, 11):
            self.db.add_participant(bib, f"Runner {bib}", f"{EPC[:-2]}{bib:02d}")
        self.db.update_event_settings(min_lap_seconds=45.0)
        self.db.start_event(at=1000.0)
        self.stalled, self.good = StalledWS(), GoodWS()
        for name, value in [("db", self.db), ("active_connections", [self.stalled, self.good]),
                            ("admin_connections", [self.stalled, self.good]),
                            ("broadcaster", Broadcaster(on_drop=self.app._forget_ws, send_timeout=self.SEND_TIMEOUT))]:
            patcher = mock.patch.object(self.app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = LapEngine(db=self.db, on_lap_completed=self.app.on_lap_completed,
                                on_runner_started=self.app.on_runner_started,
                                on_runner_resumed=self.app.on_runner_resumed,
                                on_unassigned_tag=self.app.on_unassigned_tag)

    def test_laps_are_recorded_without_waiting_for_the_stalled_phone(self):
        """A crossing that waited on the stalled phone would take at least SEND_TIMEOUT (30 of them
        far longer), so the limit sits well below it rather than near the normal ~0.1 s (#110)."""
        async def scenario():
            started = time.monotonic()
            for lap in range(3):                       # start + 2 laps for 10 runners
                for bib in range(1, 11):
                    epc = f"{EPC[:-2]}{bib:02d}"
                    await self.engine._resolve_crossing(epc, [(1010.0 + lap * 60 + bib, -50.0)])
            await self.engine._resolve_crossing("DEADBEEF", [(1200.0, -60.0)])  # admin-only push
            elapsed = time.monotonic() - started
            self.assertIn(self.stalled, self.app.active_connections)  # its send hasn't timed out yet
            await wait_until(lambda: self.stalled not in self.app.active_connections, timeout=self.SEND_TIMEOUT + 2)
            await wait_until(lambda: self.stalled.closed)
            await self.app.broadcaster.drain()
            self.app.full_board.cancel()
            return elapsed

        elapsed = asyncio.run(scenario())
        self.assertLess(elapsed, self.SEND_TIMEOUT - 1, "lap recording waited on the network")
        self.assertEqual(sum(len(self.db.get_laps_for_runner(b)) for b in range(1, 11)), 20)
        types = [m["type"] for m in self.good.sent]
        self.assertEqual(types.count("runner_started"), 10)
        self.assertEqual(types.count("lap_completed"), 20)
        self.assertIn("unassigned_tag_detected", types)
        lap = next(m for m in self.good.sent if m["type"] == "lap_completed")
        self.assertIn("row", lap)                       # #71 delta update kept
        self.assertNotIn("leaderboard", lap)
        self.assertNotIn(self.stalled, self.app.admin_connections)
        self.assertTrue(self.stalled.closed)


class WebSocketEndpointTest(unittest.TestCase):
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
        for name in ("active_connections", "admin_connections"):
            patcher = mock.patch.object(self.app, name, [])
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_init_is_sent_and_socket_forgotten_on_close(self):
        with TestClient(self.app.app).websocket_connect("/ws/live") as ws:
            self.assertEqual(ws.receive_json()["type"], "init")
            self.assertEqual(len(self.app.active_connections), 1)
        self.assertEqual(self.app.active_connections, [])

    def test_socket_forgotten_on_any_error_not_just_disconnect(self):
        client = TestClient(self.app.app)
        try:
            with client.websocket_connect("/ws/live") as ws:
                ws.receive_json()
                self.assertEqual(len(self.app.active_connections), 1)
                ws.send_bytes(b"\x00")   # receive_text() raises KeyError, not WebSocketDisconnect
                ws.receive_json()
        except Exception:
            pass
        self.assertEqual(self.app.active_connections, [])


if __name__ == "__main__":
    unittest.main()
