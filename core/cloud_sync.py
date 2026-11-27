"""Lightweight Trackside -> Cloud Run snapshot sync (stdlib only, non-blocking).

When `CLOUD_SYNC_URL` is configured on the trackside MacBook/Pi, `CloudSyncClient`
pushes atomic race snapshots (event, participants, laps, breaks) to the remote
Cloud Run instance over HTTPS so spectators anywhere can follow live.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("CloudSync")


class CloudSyncClient:
    """Background coalescing pusher that posts snapshots to `<url>/api/remote/snapshot`."""

    def __init__(
        self,
        cloud_url: Optional[str],
        token: Optional[str],
        snapshot_fn: Callable[[], Dict[str, Any]],
        min_interval: float = 1.0,
        heartbeat_interval: float = 15.0,
    ) -> None:
        self.cloud_url = (cloud_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self._snapshot_fn = snapshot_fn
        self.min_interval = max(0.25, float(min_interval))
        self.heartbeat_interval = max(5.0, float(heartbeat_interval))
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_push_ok: Optional[bool] = None
        self.last_push_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.push_count: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.cloud_url and self.token)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="CloudSyncClient", daemon=True)
        self._thread.start()
        self.mark_dirty()
        logger.info(f"Cloud sync enabled -> {self.cloud_url}")

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def mark_dirty(self) -> None:
        if self.enabled:
            self._dirty.set()

    def push_once(self) -> bool:
        if not self.enabled:
            return False
        payload = json.dumps(self._snapshot_fn()).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cloud_url}/api/remote/snapshot",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Sync-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                ok = 200 <= resp.status < 300
                self.last_push_ok = ok
                self.last_push_at = time.time()
                self.last_error = None if ok else f"HTTP {resp.status}"
                if ok:
                    self.push_count += 1
                return ok
        except Exception as exc:
            self.last_push_ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"Cloud sync push failed: {self.last_error}")
            return False

    def _loop(self) -> None:
        last_sent = 0.0
        while not self._stop.is_set():
            triggered = self._dirty.wait(timeout=self.heartbeat_interval)
            if self._stop.is_set():
                break
            self._dirty.clear()
            now = time.monotonic()
            wait = self.min_interval - (now - last_sent)
            if triggered and wait > 0:
                if self._stop.wait(timeout=wait):
                    break
            self.push_once()
            last_sent = time.monotonic()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "cloud_url": self.cloud_url or None,
            "last_push_ok": self.last_push_ok,
            "last_push_at": self.last_push_at,
            "last_error": self.last_error,
            "push_count": self.push_count,
        }
