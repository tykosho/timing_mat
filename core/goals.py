"""Single per-participant goal (time, distance, or lap count) and remaining-laps countdown."""
import math
import re
from typing import Any, Dict, Optional, Tuple

VALID_GOAL_TYPES = ("laps", "distance", "time")


def parse_time_seconds(raw: Any) -> float:
    """Parse seconds, MM:SS, or HH:MM:SS into positive float seconds."""
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val <= 0:
            raise ValueError("Time goal must be greater than 0.")
        return val
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Time goal value is required (e.g. 30:00).")
    if re.fullmatch(r"\d+(\.\d+)?", text):
        val = float(text)
        if val <= 0:
            raise ValueError("Time goal must be greater than 0.")
        return val
    parts = text.split(":")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        mins, sec = int(parts[0]), int(parts[1])
        if sec >= 60:
            raise ValueError("Seconds must be 00–59 in MM:SS format.")
        total = float(mins * 60 + sec)
        if total <= 0:
            raise ValueError("Time goal must be greater than 0.")
        return total
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        hrs, mins, sec = int(parts[0]), int(parts[1]), int(parts[2])
        if mins >= 60 or sec >= 60:
            raise ValueError("Minutes and seconds must be 00–59 in HH:MM:SS format.")
        total = float(hrs * 3600 + mins * 60 + sec)
        if total <= 0:
            raise ValueError("Time goal must be greater than 0.")
        return total
    raise ValueError("Invalid time goal. Use MM:SS (e.g. 30:00), HH:MM:SS, or seconds.")


def normalize_goal(goal_type: Optional[str], goal_value: Any) -> Tuple[Optional[str], Optional[float]]:
    """Validate and normalize a participant's single goal (laps, distance in meters, or time in seconds)."""
    gt = (goal_type or "").strip().lower()
    if not gt or gt == "none":
        return None, None
    if gt not in VALID_GOAL_TYPES:
        raise ValueError(f"Goal type must be one of: {', '.join(VALID_GOAL_TYPES)}")
    if goal_value is None or str(goal_value).strip() == "":
        raise ValueError(f"A positive value is required for a {gt} goal.")

    if gt == "time":
        val = parse_time_seconds(goal_value)
        return "time", round(val, 2)

    try:
        val = float(goal_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Goal value must be a number (got '{goal_value}').") from exc
    if not math.isfinite(val) or val <= 0:
        raise ValueError("Goal value must be greater than 0.")
    if gt == "laps":
        val = float(max(1, int(round(val))))
    return gt, round(val, 2)


def format_goal_label(goal_type: str, goal_value: float) -> str:
    """Human-readable label for a participant's goal."""
    if goal_type == "laps":
        n = int(round(goal_value))
        return f"{n} {'lap' if n == 1 else 'laps'}"
    if goal_type == "distance":
        if goal_value >= 1000.0:
            km = goal_value / 1000.0
            km_str = f"{km:.0f}" if abs(km - round(km)) < 0.01 else f"{km:.2f}".rstrip("0").rstrip(".")
            return f"{km_str} km"
        return f"{int(round(goal_value))} m"
    if goal_type == "time":
        s = int(round(goal_value))
        hrs, rem = divmod(s, 3600)
        mins, sec = divmod(rem, 60)
        if hrs > 0:
            return f"{hrs}:{mins:02d}:{sec:02d}"
        return f"{mins}:{sec:02d}"
    return ""


def compute_goal_progress(
    goal_type: Optional[str],
    goal_value: Optional[float],
    total_laps: int,
    total_time_seconds: float,
    track_length_m: float = 400.0,
    last_lap_seconds: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Compute remaining laps (based on standard 400m or configured track length) for a runner's goal."""
    if not goal_type or goal_value is None or goal_value <= 0:
        return None
    gt = goal_type.strip().lower()
    if gt not in VALID_GOAL_TYPES:
        return None

    track_m = max(1.0, float(track_length_m or 400.0))
    laps_done = max(0, int(total_laps or 0))
    elapsed = max(0.0, float(total_time_seconds or 0.0))
    label = format_goal_label(gt, float(goal_value))

    if gt in ("laps", "distance"):
        target_laps = (
            max(1, int(round(goal_value)))
            if gt == "laps"
            else max(1, math.ceil(float(goal_value) / track_m))
        )
        remaining_laps = max(0, target_laps - laps_done)
        completed = remaining_laps == 0
        just_completed = completed and laps_done == target_laps
        remaining_seconds = None
        if completed:
            badge_text = "Goal complete!"
            speech_text = "Goal complete!" if just_completed else None
        elif remaining_laps == 1:
            badge_text = "1 lap to go"
            speech_text = "1 lap to go!"
        else:
            badge_text = f"{remaining_laps} laps to go"
            speech_text = f"{remaining_laps} laps to go!"
        return {
            "goal_type": gt,
            "goal_value": float(goal_value),
            "label": label,
            "target_laps": target_laps,
            "remaining_laps": remaining_laps,
            "remaining_seconds": remaining_seconds,
            "completed": completed,
            "just_completed": just_completed,
            "badge_text": badge_text,
            "speech_text": speech_text,
        }

    # Time goal: estimate remaining laps from average lap pace so far
    remaining_seconds = max(0.0, float(goal_value) - elapsed)
    completed = remaining_seconds <= 0.5
    prev_elapsed = max(0.0, elapsed - float(last_lap_seconds or 0.0))
    just_completed = completed and (prev_elapsed < float(goal_value) - 0.5)
    if completed:
        remaining_laps = 0
        target_laps = laps_done
        badge_text = "Time goal complete!"
        speech_text = "Goal complete!" if just_completed else None
    else:
        avg_lap = (elapsed / laps_done) if laps_done > 0 else float(last_lap_seconds or 120.0)
        remaining_laps = max(1, math.ceil(remaining_seconds / max(10.0, avg_lap)))
        target_laps = laps_done + remaining_laps
        if remaining_laps == 1:
            badge_text = "~1 lap to go"
            speech_text = "1 lap to go!"
        else:
            badge_text = f"~{remaining_laps} laps to go"
            speech_text = f"{remaining_laps} laps to go!"
    return {
        "goal_type": "time",
        "goal_value": float(goal_value),
        "label": label,
        "target_laps": target_laps,
        "remaining_laps": remaining_laps,
        "remaining_seconds": round(remaining_seconds, 1),
        "completed": completed,
        "just_completed": just_completed,
        "badge_text": badge_text,
        "speech_text": speech_text,
    }
