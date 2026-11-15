"""Reader health for the admin page (issue #48).

Counts every tag read the app receives (any reader), and combines that with the reader's
own connection details into one status: "ok" (green), "quiet" (amber: connected but the
reader has sent nothing for a while) or "disconnected" (red).
"""
import threading
import time
from typing import Any, Callable, Dict, Optional

# A serial reader that has sent no frame at all for this long is shown as "quiet". In an
# empty field that can be normal; the watchdog (#5) re-arms it every 30 s anyway.
QUIET_AFTER_SECONDS = 30.0


def _ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


class ReaderHealth:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()  # reads arrive on reader threads
        self.total_reads = 0
        self.last_read_at: Optional[float] = None
        self._sample_at = clock()
        self._sample_total = 0
        self.reads_per_second = 0.0

    def note_read(self):
        """Called for every tag read, on the reader's thread."""
        with self._lock:
            self.total_reads += 1
            self.last_read_at = self._clock()

    def snapshot(self, reader: Dict[str, Any]) -> Dict[str, Any]:
        """Combines the reader's health() with read counts. Updates reads/second when sampled."""
        now = self._clock()
        with self._lock:
            total, last = self.total_reads, self.last_read_at
        elapsed = now - self._sample_at
        if elapsed >= 0.5:
            self.reads_per_second = (total - self._sample_total) / elapsed
            self._sample_at, self._sample_total = now, total

        h = dict(reader)
        h["total_reads"] = total
        h["reads_per_second"] = round(self.reads_per_second, 1)
        h["seconds_since_read"] = round(now - last, 1) if last is not None else None

        if not h.get("connected"):
            h["state"] = "disconnected"
        elif h.get("kind") == "serial" and (h.get("seconds_since_frame") is None
                                            or h["seconds_since_frame"] > QUIET_AFTER_SECONDS):
            h["state"] = "quiet"
        else:
            h["state"] = "ok"
        h["summary"] = self._summary(h)
        return h

    @staticmethod
    def _summary(h: Dict[str, Any]) -> str:
        name = "Simulator" if h.get("kind") == "simulator" else f"Reader on {h.get('port', '?')}"
        if h["state"] == "disconnected":
            if not h.get("running"):
                return f"{name}: not running"
            return f"{name}: NOT CONNECTED" + (f" ({h['last_error']})" if h.get("last_error") else "")

        parts = [f"{h['reads_per_second']:g} reads/s", f"last tag {_ago(h['seconds_since_read'])}"]
        if h.get("rearms"):
            parts.append(f"re-armed {h['rearms']}×")
        if h.get("reconnects"):
            parts.append(f"{h['reconnects']} reconnect{'s' if h['reconnects'] != 1 else ''}")
        if h.get("paused"):
            parts.append("paused")
        if h["state"] == "quiet":
            silent = h.get("seconds_since_frame")
            lead = f"{name}: connected, but nothing heard " + (f"for {silent:.0f}s" if silent is not None else "yet")
            return " · ".join([lead] + parts)
        return " · ".join([f"{name}: reading"] + parts)
