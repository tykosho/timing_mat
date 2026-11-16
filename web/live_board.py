"""Live leaderboard pushes without flooding the Wi-Fi (issue #71).

Lap, start and resume messages used to carry the whole leaderboard (about 19 KB with 60
runners) to every screen, on every lap: 13 KB/s per phone, ~4 Mbit/s for 40 phones on the
Pi's hotspot. Now they carry only the runner's own updated row, which pages merge into their
copy (web/static/live_board.js), and the full board follows at most once per interval, to
catch what a row can't say (others' rest status, edits).
"""
import asyncio
import time
from typing import Awaitable, Callable, Optional

FULL_BOARD_INTERVAL = 10.0  # seconds between full leaderboard pushes while laps keep coming


class CoalescedPush:
    """Runs `push` at most once per `interval` after mark_dirty(), always after the last change."""

    def __init__(self, push: Callable[[], Awaitable[None]], interval: float = FULL_BOARD_INTERVAL,
                 clock: Callable[[], float] = time.monotonic):
        self._push = push
        self.interval = interval
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._last_push = float("-inf")
        self.pushes = 0

    def mark_dirty(self):
        """Call on the event loop whenever the standings changed."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    def pushed_now(self):
        """Someone just sent the full board anyway (an edit, a status change)."""
        self._last_push = self._clock()

    async def _run(self):
        wait = self._last_push + self.interval - self._clock()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_push = self._clock()
        self.pushes += 1
        await self._push()

    def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
