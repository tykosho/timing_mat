"""Live leaderboard pushes (issue #71): one row per lap, the full board coalesced."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from web.live_board import CoalescedPush


class CoalescedPushTest(unittest.TestCase):
    def test_burst_of_changes_is_one_push_after_the_interval(self):
        async def scenario():
            pushes = []
            board = CoalescedPush(lambda: _record(pushes), interval=0.3)
            board.pushed_now()  # a full board just went out
            for _ in range(50):  # a pack of laps
                board.mark_dirty()
                await asyncio.sleep(0)
            await asyncio.sleep(0.1)
            self.assertEqual(pushes, [], "still inside the interval")
            while not pushes:
                await asyncio.sleep(0.01)
            board.mark_dirty()  # a change right after a push waits for the next interval
            await asyncio.sleep(0.1)
            self.assertEqual(len(pushes), 1)
            await asyncio.sleep(0.4)
            self.assertEqual(len(pushes), 2)

        asyncio.run(scenario())

    def test_first_change_after_a_quiet_spell_is_pushed_at_once(self):
        async def scenario():
            pushes = []
            board = CoalescedPush(lambda: _record(pushes), interval=10.0)
            board.mark_dirty()
            await asyncio.sleep(0.01)
            self.assertEqual(len(pushes), 1)

        asyncio.run(scenario())


async def _record(pushes):
    pushes.append(1)


class LapMessagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import fastapi  # noqa: F401
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "live_board_import.db"))
        os.environ.setdefault("MOCK_HARDWARE", "true")
        from core.db import Database
        from web import app as web_app
        cls.app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Database(os.path.join(cls.tmp.name, "live.db"))
        cls.patches = [mock.patch.object(web_app, "db", cls.db)]
        for p in cls.patches:
            p.start()
        for bib in (1, 2, 3):
            cls.db.add_participant(bib, f"Runner {bib}", f"E2000000000000000000000{bib}")
        cls.db.start_event(at=1000.0)
        for bib in (1, 2, 3):
            cls.db.set_participant_start(bib, 1000.0 + bib)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def test_lap_message_carries_only_the_runners_row(self):
        sent = []

        async def fake_broadcast(message, admin_only=False):
            sent.append(message)

        async def scenario():
            board = CoalescedPush(self.app.push_leaderboard, interval=0.1)
            with mock.patch.object(self.app, "broadcast_ws", fake_broadcast), \
                 mock.patch.object(self.app, "full_board", board), \
                 mock.patch.object(self.app, "active_connections", [object()]):
                await self.app.on_lap_completed({"bib_number": 2, "lap_number": 1, "id": 1})
                await self.app.on_runner_started({"bib_number": 3, "name": "Runner 3"})
                await asyncio.sleep(0.25)

        asyncio.run(scenario())
        lap, start = sent[0], sent[1]
        self.assertEqual(lap["type"], "lap_completed")
        self.assertNotIn("leaderboard", lap)
        self.assertEqual(lap["row"]["bib_number"], 2)
        self.assertEqual(start["row"]["bib_number"], 3)
        full = [m for m in sent if m["type"] == "leaderboard_update"]
        self.assertEqual(len(full), 1, "the full board follows once, not per message")
        self.assertEqual(len(full[0]["leaderboard"]), 3)

    def test_row_matches_the_full_board(self):
        full = {r["bib_number"]: r for r in self.db.get_leaderboard()}
        for bib in (1, 2, 3):
            self.assertEqual(self.db.get_leaderboard(bib_number=bib), [full[bib]])
        self.assertEqual(self.db.get_leaderboard(bib_number=99), [])

    def test_no_full_board_push_when_nobody_watches(self):
        async def fake_broadcast(message, admin_only=False):
            pass

        async def scenario():
            board = CoalescedPush(self.app.push_leaderboard, interval=0.0)
            with mock.patch.object(self.app, "broadcast_ws", fake_broadcast), \
                 mock.patch.object(self.app, "full_board", board), \
                 mock.patch.object(self.app, "active_connections", []):
                await self.app.on_lap_completed({"bib_number": 1, "lap_number": 1, "id": 1})
                await asyncio.sleep(0.05)
            return board.pushes

        self.assertEqual(asyncio.run(scenario()), 0)


if __name__ == "__main__":
    unittest.main()
