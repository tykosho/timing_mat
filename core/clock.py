"""The race clock: wall-clock-style timestamps that never jump (issue #2).

A Raspberry Pi has no battery-backed clock. It boots with the last saved time, and if it
reaches the internet mid-race (someone's hotspot), NTP can move the system clock by
minutes or hours. Every lap time is a difference between two timestamps, so a jump in
time.time() gives negative or hours-long laps.

RaceClock.now() looks like time.time() (seconds since the epoch, so times of day still
display) but advances with time.monotonic(), which never jumps. It's anchored to the
system clock once, and only re-anchored when no event is running, so differences between
timestamps taken during a race are always real elapsed time.

A restart during a race must carry on that timeline rather than anchor to the system clock
again, which would add every correction since the race started to the laps in progress
(issue #128). So a snapshot() is stored during a race, and resume() continues from it:
  - same boot: the kernel's monotonic clock kept counting, so it's exact;
  - after a reboot: nobody knows how long the Pi was off. The best estimate is how far the
    system clock moved, if plausible, and at least the time since power-on. Laps spanning
    it are flagged for review.
"""
import logging
import math
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("RaceClock")

# Re-anchor (between races) or warn (during one) when the system clock is this far off.
DRIFT_TOLERANCE = 2.0

# The system clock moving further than this across a reboot is a clock correction, not time off.
MAX_DOWNTIME_SECONDS = 6 * 3600.0

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def read_boot_id() -> Optional[str]:
    """Changes on every boot and is the host's inside a container. None where there isn't one."""
    try:
        with open(BOOT_ID_PATH) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _valid_snapshot(value: Any) -> Optional[Dict[str, Any]]:
    """A stored snapshot, or None if it's missing or damaged."""
    if not isinstance(value, dict):
        return None
    for key in ("race", "wall", "mono"):
        number = value.get(key)
        if not isinstance(number, (int, float)) or not math.isfinite(number):
            return None
    return value


class RaceClock:
    def __init__(self, wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic):
        self._wall = wall
        self._mono = mono
        self._warned_during_race = False
        self._anchor(wall())

    def _anchor(self, wall_now: float):
        # One assignment: the reader thread calls now() and mustn't see half of a new anchor
        self._anchor_at = (wall_now, self._mono())

    def now(self) -> float:
        wall, mono = self._anchor_at
        return wall + (self._mono() - mono)

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

    def monotonic(self) -> float:
        return self._mono()

    def snapshot(self) -> Dict[str, Any]:
        """What resume() needs to carry on this timeline in another process."""
        return {"race": self.now(), "wall": self._wall(), "mono": self._mono(), "boot": read_boot_id()}

    def resume(self, saved: Any, latest_timestamp: Optional[float] = None) -> bool:
        """At startup during a race: continues the race's timeline (issue #128).

        Returns True if that was exact (same boot), False if the time the app was down had to
        be estimated.
        """
        saved = _valid_snapshot(saved)
        wall, mono, boot = self._wall(), self._mono(), read_boot_id()
        same_boot = bool(saved and boot and saved.get("boot") == boot and mono >= saved["mono"])
        if same_boot:
            target = saved["race"] + (mono - saved["mono"])
        else:
            if saved:
                base, moved = saved["race"], wall - saved["wall"]
            elif latest_timestamp is not None:  # stored by a version that didn't save the clock
                base, moved = latest_timestamp, wall - latest_timestamp
            else:
                base, moved = wall, 0.0
            down = moved if 0.0 <= moved <= MAX_DOWNTIME_SECONDS else 0.0
            # After a reboot the Pi has been down at least as long as it has now been up. Only
            # then: without a known earlier boot, uptime can be hours longer than the restart.
            if saved and boot and saved.get("boot") and saved["boot"] != boot:
                down = max(down, mono)
            target = base + down
        if latest_timestamp is not None and target <= latest_timestamp:
            target = latest_timestamp + 1.0
        self._anchor(target)
        if same_boot:
            logger.info("Race clock resumed exactly after an app restart.")
        else:
            logger.warning(f"Race clock resumed after a restart with the time down estimated "
                           f"(system clock is {wall - target:+.0f}s from race time). "
                           f"Laps that span the restart are flagged for review.")
        return same_boot

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
