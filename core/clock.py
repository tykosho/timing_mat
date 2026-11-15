"""The race clock: wall-clock-style timestamps that never jump (issue #2).

A Raspberry Pi has no battery-backed clock. It boots with the last saved time, and if it
reaches the internet mid-race (someone's hotspot), NTP can move the system clock by
minutes or hours. Every lap time is a difference between two timestamps, so a jump in
time.time() gives negative or hours-long laps.

RaceClock.now() looks like time.time() (seconds since the epoch, so times of day still
display) but advances with time.monotonic(), which never jumps. It's anchored to the
system clock once, and only re-anchored when no event is running, so differences between
timestamps taken during a race are always real elapsed time.
"""
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("RaceClock")

# Re-anchor (between races) or warn (during one) when the system clock is this far off.
DRIFT_TOLERANCE = 2.0


class RaceClock:
    def __init__(self, wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic):
        self._wall = wall
        self._mono = mono
        self._warned_during_race = False
        self._anchor(wall())

    def _anchor(self, wall_now: float):
        self._anchor_wall = wall_now
        self._anchor_mono = self._mono()

    def now(self) -> float:
        return self._anchor_wall + (self._mono() - self._anchor_mono)

    def drift(self) -> float:
        """System clock minus race clock, in seconds (positive: the system clock jumped ahead)."""
        return self._wall() - self.now()

    def ensure_not_before(self, timestamp: Optional[float]) -> bool:
        """At startup: never hand out times earlier than ones already stored.

        Covers a Pi that rebooted and came back with an older saved time. Returns True if
        the clock had to be moved forward.
        """
        if timestamp is None or self.now() >= timestamp:
            return False
        behind = timestamp - self.now()
        self._anchor(timestamp + 1.0)
        logger.warning(f"System clock is {behind:.0f}s behind the latest stored timestamp (no RTC?). "
                       f"Race clock moved forward to continue from it. Lap times stay correct; "
                       f"times of day may be off until the clock is fixed.")
        return True

    def maintain(self, race_in_progress: bool, latest_timestamp: Optional[float] = None) -> Optional[str]:
        """Called periodically. Returns "resynced", "warned" or None.

        Between races, follow the system clock so times of day are right (but never go back
        before `latest_timestamp`). During a race, keep counting and warn once.
        """
        drift = self.drift()
        if abs(drift) < DRIFT_TOLERANCE:
            self._warned_during_race = False
            return None
        if race_in_progress:
            if not self._warned_during_race:
                logger.warning(f"System clock changed by {drift:+.0f}s during the race; ignoring it so "
                               f"lap times stay correct. The race clock re-syncs after the event.")
                self._warned_during_race = True
                return "warned"
            return None
        target = self._wall()
        if latest_timestamp is not None and target < latest_timestamp:
            target = max(self.now(), latest_timestamp)
        if abs(target - self.now()) < DRIFT_TOLERANCE:
            return None
        logger.info(f"Race clock re-synced to the system clock ({target - self.now():+.0f}s).")
        self._anchor(target)
        self._warned_during_race = False
        return "resynced"


clock = RaceClock()


def now() -> float:
    """The race clock's current time. Use this instead of time.time() for anything timed."""
    return clock.now()
