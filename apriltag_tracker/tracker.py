"""Session persistence, runner store, lap debounce state machine, and TTS script builder."""
from __future__ import annotations

import base64
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np


def format_lap_mmss(seconds: Optional[float]) -> str:
    """Format seconds as MM:SS.d (e.g., 01:14.3)."""
    if seconds is None:
        return "--:--.-"
    seconds = max(0.0, float(seconds))
    mins = int(seconds // 60)
    rem = seconds - mins * 60
    return f"{mins:02d}:{rem:04.1f}"


def format_spoken_duration(seconds: float) -> str:
    """Natural-sounding duration string for Web Speech API synthesis."""
    seconds = max(0.0, float(seconds))
    mins = int(seconds // 60)
    rem = round(seconds - mins * 60, 1)
    if rem >= 60.0:
        mins += 1
        rem = 0.0
    if mins == 0:
        return f"{rem:.1f} seconds"
    unit = "minute" if mins == 1 else "minutes"
    if rem == 0.0:
        return f"{mins} {unit}"
    return f"{mins} {unit} {rem:.1f} seconds"


FAST_TEMPLATES = [
    "New best lap for {name}! {spoken_lap}—keep that cadence!",
    "{name} is flying! Lap {laps} done in {spoken_lap}.",
]

BURN_TEMPLATES = [
    "{name}, lap {laps} in {spoken_lap}. Did you stop to tie your shoes or check your email?",
    "Lap {laps} complete for {name}. At this pace, the sun might set before lap ten.",
    "Bold strategy by {name}, saving all that energy for... next week.",
    "{name} crosses the line in {spoken_lap}. Technically, that still counts as running.",
]


def build_lap_commentary(
    name: str,
    laps: int,
    last_lap: float,
    is_pr: bool,
    is_fast: bool,
    is_slow: bool,
    burn_frequency: int,
    rng: Optional[random.Random] = None,
) -> Dict[str, str]:
    """Select commentary category and speech string for a completed lap."""
    rand = rng or random
    spoken = format_spoken_duration(last_lap)
    mmss = format_lap_mmss(last_lap)
    burn_pct = max(0, min(100, int(burn_frequency)))

    if is_pr:
        text = FAST_TEMPLATES[0].format(name=name, laps=laps, spoken_lap=spoken, last_lap=mmss)
        return {"category": "pr", "text": text}

    trigger_burn = (burn_pct > 0 and is_slow) or (burn_pct > 0 and rand.random() * 100.0 < burn_pct)
    if trigger_burn:
        tpl = rand.choice(BURN_TEMPLATES)
        return {
            "category": "burn",
            "text": tpl.format(name=name, laps=laps, spoken_lap=spoken, last_lap=mmss),
        }

    if is_fast:
        text = FAST_TEMPLATES[1].format(name=name, laps=laps, spoken_lap=spoken, last_lap=mmss)
        return {"category": "fast", "text": text}

    return {
        "category": "standard",
        "text": f"{name}, lap {laps} in {spoken}.",
    }


@dataclass
class RunnerRecord:
    tag_id: int
    name: str
    registered_at: float
    laps: int = 0
    lap_history: List[float] = field(default_factory=list)
    last_lap_timestamp: Optional[float] = None
    cooldown_until: float = 0.0
    lap_anchor_timestamp: Optional[float] = None
    avatar_version: int = 1
    last_lap_is_pr: bool = False
    last_lap_is_slow: bool = False

    @property
    def last_lap_time(self) -> Optional[float]:
        return self.lap_history[-1] if self.lap_history else None

    @property
    def best_lap_time(self) -> Optional[float]:
        return min(self.lap_history) if self.lap_history else None

    @property
    def avg_lap_time(self) -> Optional[float]:
        if not self.lap_history:
            return None
        return round(sum(self.lap_history) / len(self.lap_history), 2)

    @property
    def total_time(self) -> float:
        return round(sum(self.lap_history), 2)

    def cooldown_remaining(self, now: Optional[float] = None) -> float:
        ts = time.time() if now is None else now
        return max(0.0, round(self.cooldown_until - ts, 1))

    def to_dict(self, now: Optional[float] = None, face_exists: bool = True) -> Dict[str, Any]:
        ts = time.time() if now is None else now
        return {
            "tag_id": self.tag_id,
            "name": self.name,
            "registered_at": self.registered_at,
            "laps": self.laps,
            "lap_history": list(self.lap_history),
            "last_lap_time": self.last_lap_time,
            "last_lap_mmss": format_lap_mmss(self.last_lap_time),
            "best_lap_time": self.best_lap_time,
            "best_lap_mmss": format_lap_mmss(self.best_lap_time),
            "avg_lap_time": self.avg_lap_time,
            "avg_lap_mmss": format_lap_mmss(self.avg_lap_time),
            "total_time": self.total_time,
            "total_mmss": format_lap_mmss(self.total_time if self.laps > 0 else None),
            "last_lap_timestamp": self.last_lap_timestamp,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining": self.cooldown_remaining(ts),
            "lap_anchor_timestamp": self.lap_anchor_timestamp,
            "avatar_version": self.avatar_version,
            "avatar_url": f"/faces/{self.tag_id}.png?v={self.avatar_version}" if face_exists else None,
            "last_lap_is_pr": self.last_lap_is_pr,
            "last_lap_is_slow": self.last_lap_is_slow,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RunnerRecord":
        history = [float(x) for x in raw.get("lap_history", [])]
        return cls(
            tag_id=int(raw["tag_id"]),
            name=str(raw["name"]),
            registered_at=float(raw.get("registered_at", time.time())),
            laps=int(raw.get("laps", len(history))),
            lap_history=history,
            last_lap_timestamp=float(raw["last_lap_timestamp"])
            if raw.get("last_lap_timestamp") is not None
            else None,
            cooldown_until=float(raw.get("cooldown_until", 0.0)),
            lap_anchor_timestamp=float(raw["lap_anchor_timestamp"])
            if raw.get("lap_anchor_timestamp") is not None
            else None,
            avatar_version=int(raw.get("avatar_version", 1)),
            last_lap_is_pr=bool(raw.get("last_lap_is_pr", False)),
            last_lap_is_slow=bool(raw.get("last_lap_is_slow", False)),
        )


def make_placeholder_avatar_png(tag_id: int, label: str = "") -> bytes:
    """Create a clean 256x256 PNG portrait placeholder when no camera frame is supplied."""
    img = np.zeros((256, 256, 3), dtype=np.uint8)
    hue = (tag_id * 37) % 180
    hsv = np.full((256, 256, 3), (hue, 150, 75), dtype=np.uint8)
    img = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    cv2.circle(img, (128, 96), 42, (235, 240, 245), -1, lineType=cv2.LINE_AA)
    cv2.ellipse(img, (128, 224), (78, 66), 0, 180, 360, (235, 240, 245), -1, lineType=cv2.LINE_AA)
    badge = label.strip()[:2].upper() if label.strip() else f"#{tag_id}"
    cv2.putText(
        img,
        badge,
        (18, 42),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else b""


def decode_image_payload(preview_image_base64: Optional[str]) -> Optional[bytes]:
    """Decode a data URL or raw base64 image into validated PNG bytes."""
    if not preview_image_base64:
        return None
    raw_str = preview_image_base64.strip()
    if "," in raw_str and raw_str.startswith("data:"):
        raw_str = raw_str.split(",", 1)[1]
    try:
        decoded = base64.b64decode(raw_str, validate=False)
    except Exception:
        return None
    arr = np.frombuffer(decoded, dtype=np.uint8)
    if arr.size == 0:
        return None
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    resized = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
    ok, png_buf = cv2.imencode(".png", resized)
    return png_buf.tobytes() if ok else None


class SessionTracker:
    """Thread-safe session state, runner persistence, and lap-counting engine."""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        base_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parent / "data"
        self.data_dir = base_dir
        self.faces_dir = base_dir / "faces"
        self.session_file = base_dir / "session.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.faces_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self.status: str = "idle"  # "idle" | "running" | "paused"
        self.started_at: Optional[float] = None
        self.paused_at: Optional[float] = None
        self.elapsed_before_pause: float = 0.0

        # Tunable settings
        self.cooldown_seconds: float = 25.0
        self.finish_line_x: float = 0.5
        self.finish_zone_half_width: float = 0.14
        self.decision_margin_min: float = 42.0
        self.registration_margin_min: float = 48.0
        self.registration_confirm_frames: int = 3
        self.min_tag_area_ratio: float = 0.0012
        self.burn_frequency: int = 25
        self.debug_overlay: bool = True
        self.mirror: bool = False
        self.simulated_mode: bool = False
        self.camera_index: int = 0

        self.runners: Dict[int, RunnerRecord] = {}
        self.ignored_tags: set[int] = set()
        self.pending_registration: Dict[int, Dict[str, Any]] = {}
        self._prev_tag_x: Dict[int, tuple[float, float]] = {}

        self._load()

    # --- Persistence ---
    def _load(self) -> None:
        if not self.session_file.exists():
            return
        try:
            payload = json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception:
            return
        with self._lock:
            self.status = payload.get("status", "idle")
            if self.status not in ("idle", "running", "paused"):
                self.status = "idle"
            self.started_at = payload.get("started_at")
            self.paused_at = payload.get("paused_at")
            self.elapsed_before_pause = float(payload.get("elapsed_before_pause", 0.0))

            cfg = payload.get("config", {})
            self.cooldown_seconds = float(cfg.get("cooldown_seconds", 25.0))
            self.finish_line_x = float(cfg.get("finish_line_x", 0.5))
            self.burn_frequency = int(cfg.get("burn_frequency", 25))
            self.debug_overlay = bool(cfg.get("debug_overlay", True))
            self.mirror = bool(cfg.get("mirror", False))
            self.simulated_mode = bool(cfg.get("simulated_mode", False))
            self.camera_index = max(0, min(16, int(cfg.get("camera_index", 0))))

            self.ignored_tags = {int(t) for t in payload.get("ignored_tags", [])}
            self.runners = {}
            for item in payload.get("runners", []):
                rec = RunnerRecord.from_dict(item)
                self.runners[rec.tag_id] = rec

    def save(self) -> None:
        with self._lock:
            payload = {
                "status": self.status,
                "started_at": self.started_at,
                "paused_at": self.paused_at,
                "elapsed_before_pause": self.elapsed_before_pause,
                "config": {
                    "cooldown_seconds": self.cooldown_seconds,
                    "finish_line_x": self.finish_line_x,
                    "burn_frequency": self.burn_frequency,
                    "debug_overlay": self.debug_overlay,
                    "mirror": self.mirror,
                    "simulated_mode": self.simulated_mode,
                    "camera_index": self.camera_index,
                },
                "ignored_tags": sorted(self.ignored_tags),
                "runners": [
                    {
                        "tag_id": r.tag_id,
                        "name": r.name,
                        "registered_at": r.registered_at,
                        "laps": r.laps,
                        "lap_history": r.lap_history,
                        "last_lap_timestamp": r.last_lap_timestamp,
                        "cooldown_until": r.cooldown_until,
                        "lap_anchor_timestamp": r.lap_anchor_timestamp,
                        "avatar_version": r.avatar_version,
                        "last_lap_is_pr": r.last_lap_is_pr,
                        "last_lap_is_slow": r.last_lap_is_slow,
                    }
                    for r in self.runners.values()
                ],
            }
            tmp_path = self.session_file.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp_path, self.session_file)

    # --- Session clock & controls ---
    def session_elapsed(self, now: Optional[float] = None) -> float:
        ts = time.time() if now is None else now
        with self._lock:
            if self.status == "running" and self.started_at is not None:
                return max(0.0, self.elapsed_before_pause + (ts - self.started_at))
            return max(0.0, self.elapsed_before_pause)

    def start_session(self, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else now
        with self._lock:
            if self.status == "running":
                return
            if self.status == "paused" and self.paused_at is not None:
                pause_delta = max(0.0, ts - self.paused_at)
                for r in self.runners.values():
                    if r.lap_anchor_timestamp is not None:
                        r.lap_anchor_timestamp += pause_delta
            else:
                for r in self.runners.values():
                    if r.lap_anchor_timestamp is None:
                        r.lap_anchor_timestamp = ts
            self.status = "running"
            self.started_at = ts
            self.paused_at = None
            self.save()

    def pause_session(self, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else now
        with self._lock:
            if self.status != "running":
                return
            if self.started_at is not None:
                self.elapsed_before_pause += max(0.0, ts - self.started_at)
            self.status = "paused"
            self.started_at = None
            self.paused_at = ts
            self.save()

    def reset_session(self, clear_runners: bool = False) -> None:
        with self._lock:
            self.status = "idle"
            self.started_at = None
            self.paused_at = None
            self.elapsed_before_pause = 0.0
            self.pending_registration.clear()
            self._prev_tag_x.clear()
            if clear_runners:
                self.runners.clear()
                self.ignored_tags.clear()
                for png in self.faces_dir.glob("*.png"):
                    try:
                        png.unlink()
                    except OSError:
                        pass
            else:
                for r in self.runners.values():
                    r.laps = 0
                    r.lap_history.clear()
                    r.last_lap_timestamp = None
                    r.cooldown_until = 0.0
                    r.lap_anchor_timestamp = None
                    r.last_lap_is_pr = False
                    r.last_lap_is_slow = False
            self.save()

    def update_config(self, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            if "cooldown_seconds" in kwargs and kwargs["cooldown_seconds"] is not None:
                self.cooldown_seconds = max(5.0, min(120.0, float(kwargs["cooldown_seconds"])))
            if "finish_line_x" in kwargs and kwargs["finish_line_x"] is not None:
                self.finish_line_x = max(0.1, min(0.9, float(kwargs["finish_line_x"])))
            if "burn_frequency" in kwargs and kwargs["burn_frequency"] is not None:
                self.burn_frequency = max(0, min(100, int(kwargs["burn_frequency"])))
            if "debug_overlay" in kwargs and kwargs["debug_overlay"] is not None:
                self.debug_overlay = bool(kwargs["debug_overlay"])
            if "mirror" in kwargs and kwargs["mirror"] is not None:
                self.mirror = bool(kwargs["mirror"])
            if "simulated_mode" in kwargs and kwargs["simulated_mode"] is not None:
                self.simulated_mode = bool(kwargs["simulated_mode"])
            if "camera_index" in kwargs and kwargs["camera_index"] is not None:
                self.camera_index = max(0, min(16, int(kwargs["camera_index"])))
            self.save()
            return self.get_state()

    # --- Unknown tag & registration flow ---
    def is_registered(self, tag_id: int) -> bool:
        with self._lock:
            return tag_id in self.runners

    def should_prompt_unknown(self, tag_id: int) -> bool:
        with self._lock:
            return (
                0 <= tag_id < 30
                and tag_id not in self.runners
                and tag_id not in self.ignored_tags
                and tag_id not in self.pending_registration
            )

    def mark_pending_registration(
        self, tag_id: int, preview_image_base64: str, now: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        ts = time.time() if now is None else now
        with self._lock:
            if not self.should_prompt_unknown(tag_id):
                return None
            prompt = {
                "tag_id": int(tag_id),
                "preview_image_base64": preview_image_base64,
                "timestamp": ts,
            }
            self.pending_registration[int(tag_id)] = prompt
            return prompt

    def update_pending_preview(
        self, tag_id: int, preview_image_base64: str, now: Optional[float] = None
    ) -> Dict[str, Any]:
        ts = time.time() if now is None else now
        with self._lock:
            prompt = {
                "tag_id": int(tag_id),
                "preview_image_base64": preview_image_base64,
                "timestamp": ts,
            }
            if tag_id not in self.runners:
                self.pending_registration[int(tag_id)] = prompt
            return prompt

    def dismiss_pending(self, tag_id: int) -> None:
        with self._lock:
            self.pending_registration.pop(int(tag_id), None)

    def ignore_tag(self, tag_id: int) -> None:
        with self._lock:
            tid = int(tag_id)
            self.pending_registration.pop(tid, None)
            self.ignored_tags.add(tid)
            self.save()

    def unignore_tag(self, tag_id: int) -> None:
        with self._lock:
            self.ignored_tags.discard(int(tag_id))
            self.save()

    def face_path(self, tag_id: int) -> Path:
        return self.faces_dir / f"{int(tag_id)}.png"

    def register_runner(
        self,
        tag_id: int,
        name: str,
        preview_image_base64: Optional[str] = None,
        now: Optional[float] = None,
        apply_initial_cooldown: bool = True,
        source_tag_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        ts = time.time() if now is None else now
        tid = int(tag_id)
        if not (0 <= tid < 30):
            raise ValueError(f"tag_id must be 0..29, got {tid}")
        clean_name = name.strip() or f"Runner #{tid}"

        with self._lock:
            pending = self.pending_registration.pop(tid, None)
            if source_tag_id is not None and int(source_tag_id) != tid:
                src_pending = self.pending_registration.pop(int(source_tag_id), None)
                if pending is None:
                    pending = src_pending
            self.ignored_tags.discard(tid)

            img_b64 = preview_image_base64 or (pending["preview_image_base64"] if pending else None)
            png_bytes = decode_image_payload(img_b64)
            face_file = self.face_path(tid)

            existing = self.runners.get(tid)
            if existing is not None:
                existing.name = clean_name
                if png_bytes:
                    face_file.write_bytes(png_bytes)
                    existing.avatar_version += 1
                elif not face_file.exists():
                    face_file.write_bytes(make_placeholder_avatar_png(tid, clean_name))
                rec = existing
                is_new = False
            else:
                if not png_bytes:
                    png_bytes = make_placeholder_avatar_png(tid, clean_name)
                face_file.write_bytes(png_bytes)
                rec = RunnerRecord(
                    tag_id=tid,
                    name=clean_name,
                    registered_at=ts,
                    laps=0,
                    lap_history=[],
                    last_lap_timestamp=ts if apply_initial_cooldown else None,
                    cooldown_until=(ts + self.cooldown_seconds) if apply_initial_cooldown else 0.0,
                    lap_anchor_timestamp=ts,
                    avatar_version=1,
                )
                self.runners[tid] = rec
                is_new = True

            self.save()
            welcome_text = f"Welcome to the track, {rec.name}. Tag {rec.tag_id} locked in."
            return {
                "runner": rec.to_dict(ts, face_exists=face_file.exists()),
                "is_new": is_new,
                "speech_text": welcome_text,
            }

    def edit_runner(
        self,
        old_tag_id: int,
        name: str,
        new_tag_id: Optional[int] = None,
        preview_image_base64: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a runner's name/photo and optionally reassign or swap their tag ID."""
        ts = time.time() if now is None else now
        old_tid = int(old_tag_id)
        target_tid = int(new_tag_id) if new_tag_id is not None else old_tid
        if not (0 <= target_tid < 30):
            raise ValueError(f"new_tag_id must be 0..29, got {target_tid}")

        with self._lock:
            rec = self.runners.get(old_tid)
            if rec is None:
                return None

            clean_name = name.strip() or rec.name
            rec.name = clean_name
            png_bytes = decode_image_payload(preview_image_base64)

            if target_tid != old_tid:
                self.pending_registration.pop(target_tid, None)
                self.ignored_tags.discard(target_tid)
                old_face = self.face_path(old_tid)
                new_face = self.face_path(target_tid)

                other = self.runners.get(target_tid)
                if other is not None:
                    # Swap tags cleanly between the two runners (preserving each runner's laps & face)
                    tmp_face = self.faces_dir / f"_swap_{old_tid}_{target_tid}.png"
                    if old_face.exists() and new_face.exists():
                        os.replace(old_face, tmp_face)
                        os.replace(new_face, old_face)
                        os.replace(tmp_face, new_face)
                    elif old_face.exists():
                        os.replace(old_face, new_face)
                    elif new_face.exists():
                        os.replace(new_face, old_face)

                    other.tag_id = old_tid
                    other.avatar_version += 1
                    rec.tag_id = target_tid
                    rec.avatar_version += 1
                    self.runners[old_tid] = other
                    self.runners[target_tid] = rec
                else:
                    # Move runner to unused target_tid
                    if old_face.exists():
                        os.replace(old_face, new_face)
                    del self.runners[old_tid]
                    rec.tag_id = target_tid
                    rec.avatar_version += 1
                    self.runners[target_tid] = rec

                self._prev_tag_x.pop(old_tid, None)
                self._prev_tag_x.pop(target_tid, None)

            final_face = self.face_path(rec.tag_id)
            if png_bytes:
                final_face.write_bytes(png_bytes)
                rec.avatar_version += 1
            elif not final_face.exists():
                final_face.write_bytes(make_placeholder_avatar_png(rec.tag_id, rec.name))

            self.save()
            return rec.to_dict(ts, face_exists=final_face.exists())

    def delete_runner(self, tag_id: int) -> bool:
        """Delete an erroneously registered runner and remove their face thumbnail."""
        tid = int(tag_id)
        with self._lock:
            rec = self.runners.pop(tid, None)
            if rec is None:
                return False
            self._prev_tag_x.pop(tid, None)
            self.pending_registration.pop(tid, None)
            face_file = self.face_path(tid)
            if face_file.exists():
                try:
                    face_file.unlink()
                except OSError:
                    pass
            self.save()
            return True

    # --- Finish-line detection & lap counting ---
    def check_line_trigger(self, tag_id: int, norm_x: float, now: Optional[float] = None) -> bool:
        """Return True if tag center X is within the finish zone or straddled finish_line_x."""
        ts = time.time() if now is None else now
        with self._lock:
            prev = self._prev_tag_x.get(tag_id)
            self._prev_tag_x[tag_id] = (norm_x, ts)
            in_zone = abs(norm_x - self.finish_line_x) <= self.finish_zone_half_width
            if in_zone:
                return True
            if prev is not None:
                prev_x, prev_ts = prev
                if ts - prev_ts <= 1.8:
                    crossed = (prev_x < self.finish_line_x <= norm_x) or (
                        prev_x > self.finish_line_x >= norm_x
                    )
                    if crossed:
                        return True
            return False

    def record_crossing(
        self,
        tag_id: int,
        now: Optional[float] = None,
        force: bool = False,
        synthetic_split: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record a completed lap for a registered runner if outside cooldown."""
        ts = time.time() if now is None else now
        with self._lock:
            rec = self.runners.get(int(tag_id))
            if rec is None:
                return None
            if self.status == "paused" and not force:
                return None
            if self.status == "idle":
                self.start_session(ts)

            if not force:
                if ts < rec.cooldown_until:
                    return None
                if (
                    rec.last_lap_timestamp is not None
                    and (ts - rec.last_lap_timestamp) < self.cooldown_seconds
                ):
                    return None

            if synthetic_split is not None:
                split = max(0.1, round(float(synthetic_split), 2))
            elif rec.lap_anchor_timestamp is not None:
                raw_split = ts - rec.lap_anchor_timestamp
                split = max(0.5, round(raw_split, 2))
            else:
                split = round(max(1.0, self.session_elapsed(ts)), 2)

            prev_best = rec.best_lap_time
            prev_avg = rec.avg_lap_time
            new_lap_num = rec.laps + 1

            is_pr = new_lap_num >= 2 and (prev_best is not None and split < prev_best)
            is_fast = new_lap_num >= 2 and (prev_avg is not None and split < prev_avg)
            is_slow = new_lap_num >= 2 and (prev_avg is not None and split > prev_avg * 1.15)

            rec.lap_history.append(split)
            rec.laps = len(rec.lap_history)
            rec.last_lap_timestamp = ts
            rec.cooldown_until = ts + self.cooldown_seconds
            rec.lap_anchor_timestamp = ts
            rec.last_lap_is_pr = is_pr
            rec.last_lap_is_slow = is_slow

            commentary = build_lap_commentary(
                name=rec.name,
                laps=rec.laps,
                last_lap=split,
                is_pr=is_pr,
                is_fast=is_fast,
                is_slow=is_slow,
                burn_frequency=self.burn_frequency,
            )
            self.save()
            return {
                "runner": rec.to_dict(ts, face_exists=self.face_path(rec.tag_id).exists()),
                "lap_number": rec.laps,
                "lap_time": split,
                "lap_mmss": format_lap_mmss(split),
                "is_pr": is_pr,
                "is_fast": is_fast,
                "is_slow": is_slow,
                "speech_category": commentary["category"],
                "speech_text": commentary["text"],
                "timestamp": ts,
            }

    def adjust_laps(
        self, tag_id: int, delta: int, now: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Manual +1 / -1 lap correction per runner."""
        ts = time.time() if now is None else now
        with self._lock:
            rec = self.runners.get(int(tag_id))
            if rec is None:
                return None
            if delta > 0:
                for _ in range(delta):
                    if rec.lap_anchor_timestamp is not None and (ts - rec.lap_anchor_timestamp) >= 3.0:
                        est = round(ts - rec.lap_anchor_timestamp, 2)
                    elif rec.avg_lap_time is not None:
                        est = rec.avg_lap_time
                    else:
                        est = round(max(30.0, self.cooldown_seconds), 2)
                    rec.lap_history.append(est)
                    rec.laps = len(rec.lap_history)
                    rec.last_lap_timestamp = ts
                    rec.lap_anchor_timestamp = ts
                    rec.last_lap_is_pr = False
                    rec.last_lap_is_slow = False
            elif delta < 0:
                for _ in range(abs(delta)):
                    if rec.lap_history:
                        rec.lap_history.pop()
                rec.laps = len(rec.lap_history)
                rec.cooldown_until = 0.0
                if not rec.lap_history:
                    rec.last_lap_timestamp = None
                rec.last_lap_is_pr = False
                rec.last_lap_is_slow = False
            self.save()
            return rec.to_dict(ts, face_exists=self.face_path(rec.tag_id).exists())

    def leaderboard(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Runners sorted by Laps (desc), then Total Time (asc)."""
        ts = time.time() if now is None else now
        with self._lock:
            sorted_records = sorted(
                self.runners.values(),
                key=lambda r: (-r.laps, r.total_time if r.laps > 0 else float("inf"), r.tag_id),
            )
            out = []
            for rank, rec in enumerate(sorted_records, start=1):
                item = rec.to_dict(ts, face_exists=self.face_path(rec.tag_id).exists())
                item["rank"] = rank
                out.append(item)
            return out

    def next_unregistered_tag_id(self) -> Optional[int]:
        with self._lock:
            for tid in range(30):
                if (
                    tid not in self.runners
                    and tid not in self.ignored_tags
                    and tid not in self.pending_registration
                ):
                    return tid
            return None

    def get_state(self, now: Optional[float] = None) -> Dict[str, Any]:
        ts = time.time() if now is None else now
        with self._lock:
            elapsed = self.session_elapsed(ts)
            return {
                "session": {
                    "status": self.status,
                    "elapsed_seconds": round(elapsed, 1),
                    "elapsed_mmss": format_lap_mmss(elapsed),
                },
                "config": {
                    "cooldown_seconds": self.cooldown_seconds,
                    "finish_line_x": self.finish_line_x,
                    "finish_zone_half_width": self.finish_zone_half_width,
                    "decision_margin_min": self.decision_margin_min,
                    "burn_frequency": self.burn_frequency,
                    "debug_overlay": self.debug_overlay,
                    "mirror": self.mirror,
                    "simulated_mode": self.simulated_mode,
                    "camera_index": self.camera_index,
                },
                "leaderboard": self.leaderboard(ts),
                "ignored_tags": sorted(self.ignored_tags),
                "pending_prompts": list(self.pending_registration.values()),
            }
