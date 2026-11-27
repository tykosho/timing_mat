"""Frugal, non-repeating trackside commentary engine for 2+ hour track sessions.

Designed for runners passing a timing mat/camera at speed: every spoken line targets
~2 to 3.2 seconds so the runner hears their name, lap, and punchline before they are
down the backstretch. Uses Fisher-Yates shuffle bags so no burn or cheer repeats until
the entire deck has been exhausted.
"""
from __future__ import annotations

import random
import threading
from typing import Any, Dict, List, Optional


def format_spoken_compact(seconds: Optional[float]) -> str:
    """Frugal spoken duration: '1:14' or '52 seconds' (saves ~2s over '1 minute 14 point 2 seconds')."""
    if seconds is None or seconds <= 0:
        return ""
    total = int(round(float(seconds)))
    if total < 60:
        return f"{total} seconds"
    mins, secs = divmod(total, 60)
    return f"{mins}:{secs:02d}"


def format_mmss_short(seconds: Optional[float]) -> str:
    if seconds is None or seconds <= 0:
        return "--:--"
    mins = int(seconds // 60)
    rem = seconds - mins * 60
    return f"{mins}:{rem:04.1f}"


# 72 short, witty, trackside-friendly burns (4-8 words each after name + lap).
# At 25% burn frequency across a 2-hour club session, this cycles <3 times total with zero back-to-back repeats.
BURN_QUIPS: List[str] = [
    "Blistering jog.",
    "Scenic route today?",
    "Gravity winning?",
    "Saving it for next week.",
    "Technically still running.",
    "Coffee break next?",
    "Blink twice if you're moving.",
    "Bold pace strategy.",
    "Check your watch, not your email.",
    "Sun's getting low.",
    "Speed-walking champion.",
    "Drafting off the wind?",
    "Easy does it—too easy.",
    "Tying your shoes mid-lap?",
    "Tourist pace unlocked.",
    "Strava's gonna hide that split.",
    "Was there traffic on lane one?",
    "Negative energy split.",
    "Zero speeding tickets today.",
    "Still faster than the couch.",
    "Pace: aggressively relaxed.",
    "Did you stop for snacks?",
    "Running on airplane mode.",
    "Cruise control stuck on low.",
    "Glacier pace, strong form.",
    "Warm-up lap number what?",
    "Left the turbo at home.",
    "Plotting a comeback—eventually.",
    "At least you look comfy.",
    "Clock's still ticking, friend.",
    "That lap had a commercial break.",
    "Low heart rate, high vibes.",
    "Conservation mode engaged.",
    "Did the track get longer?",
    "Jogging like rent is optional.",
    "No rush, we've got all day.",
    "Your shadow lapped you.",
    "Checking out the scenery?",
    "Slower than Monday morning.",
    "Zone zero training?",
    "Light breeze slowed you down?",
    "Definitely not a sprint.",
    "Tactical recovery lap, sure.",
    "Saving legs for tomorrow's nap.",
    "That was a brisk stroll.",
    "Almost broke a sweat.",
    "Smooth, steady, glacial.",
    "Respecting the speed limit.",
    "Did you wave to everyone?",
    "Comfortably off pace.",
    "Powered by pure stubbornness.",
    "Taking the long way round.",
    "Good news: you didn't trip.",
    "Bad news: the clock saw that.",
    "Low-mileage effort, high drama.",
    "Looking fresh—suspiciously fresh.",
    "That split needs a coffee.",
    "Holding back for lap fifty?",
    "Legs on battery saver.",
    "Aesthetic jog, zero urgency.",
    "You beat the sunset—barely.",
    "Nice form, where's the speed?",
    "Cruising in first gear.",
    "Are we racing or sightseeing?",
    "Definitely pacing for a marathon.",
    "Clock yawned on that one.",
    "Gentle reminder: legs go faster.",
    "Effortless—literally zero effort.",
    "That lap took a detour.",
    "Still upright, counts as a win.",
    "Pure endurance, zero hurry.",
    "Saving the kick for next month.",
]

# 20 punchy Personal Record (PR) callouts
PR_QUIPS: List[str] = [
    "New PR! Crushed it!",
    "Fastest lap yet!",
    "New best—on fire!",
    "Personal best! Keep rolling!",
    "New record! Flying!",
    "PR alert! Pure speed!",
    "Best split of the day!",
    "Blazing PR! Stay smooth!",
    "New top speed—let's go!",
    "Fastest one yet! Dialed in!",
    "PR! Nobody's catching that!",
    "Rocket lap—new best!",
    "New PR! Look at that cadence!",
    "Peak speed unlocked!",
    "Fresh PR! Strong finish!",
    "Fastest lap—making it look easy!",
    "New best! Locked in!",
    "PR! Dropping the hammer!",
    "Lightning split—new best!",
    "New PR! Big energy!",
]

# 24 punchy fast-split / encouraging callouts
FAST_QUIPS: List[str] = [
    "Flying! Keep that cadence!",
    "Strong negative split!",
    "Dialed in!",
    "Smooth stride, fast split!",
    "Moving well!",
    "Locked in—nice rhythm!",
    "Quick lap! Looking sharp!",
    "Picking it up!",
    "Great turnover!",
    "Strong pace—hold it!",
    "Cruising fast!",
    "Nice surge!",
    "Metronome splits!",
    "Looking effortless!",
    "Solid clip—keep pushing!",
    "Fast feet!",
    "Right on pace!",
    "Strong work!",
    "Clean lap, great speed!",
    "Closing strong!",
    "Silky smooth!",
    "Rolling right along!",
    "Awesome rhythm!",
    "Bringing the heat!",
]

# 16 punchy Duo / Pack quips (used when 2+ runners cross together)
PACK_HYPE_QUIPS: List[str] = [
    "Photo finish!",
    "Side by side—strong pack!",
    "Tight group—keep pushing!",
    "Drafting train rolling through!",
    "Neck and neck!",
    "Fast pack—great teamwork!",
    "Locked together—flying!",
    "Big pack energy!",
]

PACK_BURN_QUIPS: List[str] = [
    "Holding hands or drafting?",
    "Group coffee stroll?",
    "Synced strides, questionable speed.",
    "Nobody wanted to suffer alone.",
    "Traffic jam at the line.",
    "Book club on the track?",
    "Drafting at walking speed.",
    "Social hour, lap complete.",
]


class _ShuffleDeck:
    """Fisher-Yates non-repeating deck: exhausts every line before reshuffling."""

    def __init__(self, items: List[str], rng: Optional[random.Random] = None) -> None:
        self._items = list(items)
        self._rng = rng or random.Random()
        self._bag: List[str] = []
        self._last: Optional[str] = None

    def draw(self) -> str:
        if not self._bag:
            self._bag = list(self._items)
            self._rng.shuffle(self._bag)
            # Prevent immediate repeat across reshuffle boundary
            if len(self._bag) > 1 and self._bag[-1] == self._last:
                self._bag[0], self._bag[-1] = self._bag[-1], self._bag[0]
        choice = self._bag.pop()
        self._last = choice
        return choice


class CommentaryEngine:
    """Thread-safe frugal trackside commentary generator with non-repeating decks."""

    def __init__(self, burn_frequency: int = 25, seed: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        self._rng = random.Random(seed)
        self.burn_frequency = max(0, min(100, int(burn_frequency)))
        self._burns = _ShuffleDeck(BURN_QUIPS, self._rng)
        self._prs = _ShuffleDeck(PR_QUIPS, self._rng)
        self._fasts = _ShuffleDeck(FAST_QUIPS, self._rng)
        self._pack_hype = _ShuffleDeck(PACK_HYPE_QUIPS, self._rng)
        self._pack_burns = _ShuffleDeck(PACK_BURN_QUIPS, self._rng)

    def set_burn_frequency(self, value: int) -> int:
        with self._lock:
            self.burn_frequency = max(0, min(100, int(value)))
            return self.burn_frequency

    def counts(self) -> Dict[str, int]:
        return {
            "burns": len(BURN_QUIPS),
            "prs": len(PR_QUIPS),
            "fast": len(FAST_QUIPS),
            "pack_hype": len(PACK_HYPE_QUIPS),
            "pack_burns": len(PACK_BURN_QUIPS),
            "total": (
                len(BURN_QUIPS)
                + len(PR_QUIPS)
                + len(FAST_QUIPS)
                + len(PACK_HYPE_QUIPS)
                + len(PACK_BURN_QUIPS)
            ),
        }

    def for_lap(
        self,
        name: str,
        lap_number: int,
        lap_duration_seconds: float,
        prev_best_seconds: Optional[float] = None,
        prev_avg_seconds: Optional[float] = None,
        burn_frequency_override: Optional[int] = None,
        force_category: Optional[str] = None,
        goal_speech: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return frugal spoken line + optional display quip for a completed lap."""
        with self._lock:
            burn_pct = (
                self.burn_frequency
                if burn_frequency_override is None
                else max(0, min(100, int(burn_frequency_override)))
            )
            short_name = (name or "Runner").strip().split()[0]
            spoken_time = format_spoken_compact(lap_duration_seconds)
            mmss = format_mmss_short(lap_duration_seconds)
            goal_part = f" {goal_speech}" if goal_speech else ""

            if force_category == "pr":
                is_pr, trigger_burn, is_fast = True, False, False
            elif force_category == "burn":
                is_pr, trigger_burn, is_fast = False, True, False
            elif force_category == "fast":
                is_pr, trigger_burn, is_fast = False, False, True
            elif force_category == "standard":
                is_pr, trigger_burn, is_fast = False, False, False
            else:
                is_pr = lap_number >= 2 and prev_best_seconds is not None and lap_duration_seconds < prev_best_seconds
                is_fast = lap_number >= 2 and prev_avg_seconds is not None and lap_duration_seconds < prev_avg_seconds
                is_slow = lap_number >= 2 and prev_avg_seconds is not None and lap_duration_seconds > prev_avg_seconds * 1.12
                trigger_burn = (burn_pct > 0 and is_slow) or (
                    burn_pct > 0 and self._rng.random() * 100.0 < burn_pct
                )

            if is_pr:
                quip = self._prs.draw()
                speech = f"{short_name}, lap {lap_number}, {spoken_time}.{goal_part} {quip}"
                return {
                    "category": "pr",
                    "quip": quip,
                    "speech": speech,
                    "display_time": mmss,
                }

            if trigger_burn:
                quip = self._burns.draw()
                speech = f"{short_name}, lap {lap_number}.{goal_part} {quip}"
                return {
                    "category": "burn",
                    "quip": quip,
                    "speech": speech,
                    "display_time": mmss,
                }

            if is_fast:
                quip = self._fasts.draw()
                speech = f"{short_name}, lap {lap_number}, {spoken_time}.{goal_part} {quip}"
                return {
                    "category": "fast",
                    "quip": quip,
                    "speech": speech,
                    "display_time": mmss,
                }

            return {
                "category": "standard",
                "quip": None,
                "speech": f"{short_name}, lap {lap_number}, {spoken_time}.{goal_part}",
                "display_time": mmss,
            }

    def sample_roast(self, name: str = "Coach") -> Dict[str, Any]:
        with self._lock:
            quip = self._burns.draw()
            short_name = (name or "Coach").strip().split()[0]
            return {
                "category": "burn",
                "quip": quip,
                "speech": f"{short_name}, lap 4. {quip}",
            }

    def for_pack(self, names: List[str], burn: bool = False) -> Dict[str, Any]:
        with self._lock:
            short_names = [(n or "Runner").strip().split()[0] for n in names]
            quip = self._pack_burns.draw() if burn else self._pack_hype.draw()
            if len(short_names) == 2:
                roster = f"{short_names[0]} and {short_names[1]}"
            else:
                roster = ", ".join(short_names[:-1]) + f", and {short_names[-1]}"
            return {
                "category": "burn" if burn else "fast",
                "quip": quip,
                "speech": f"{roster}! {quip}",
            }
