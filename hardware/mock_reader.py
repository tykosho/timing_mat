import os
import math
import time
import random
import threading
import logging
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from typing import Callable, Deque, List, Dict, Any, Optional
from core.models import TagRead
from core import clock
from hardware.base import BaseRFIDReader

logger = logging.getLogger("MockReader")

RUNNER_COLORS = ["#34d399", "#38bdf8", "#fbbf24", "#f472b6", "#a78bfa"]
DEFAULT_SAMPLE_GPX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "samples",
    "Inaugural_Last_Annual_Take_Back_the_Track_.gpx",
)


def load_gpx_lap_durations(gpx_path: str) -> List[float]:
    """
    Extracts sequential lap durations (in seconds) from a track GPX file by
    unwrapping angular progress around the track center.
    """
    if not gpx_path or not os.path.exists(gpx_path):
        return []
    try:
        tree = ET.parse(gpx_path)
        root = tree.getroot()
        ns = {"gpx": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        pts = root.findall(".//gpx:trkpt", ns) if ns else root.findall(".//trkpt")

        coords = []
        for p in pts:
            t_el = p.find("gpx:time", ns) if ns else p.find("time")
            if t_el is None or not t_el.text:
                continue
            lat = float(p.attrib["lat"])
            lon = float(p.attrib["lon"])
            t = datetime.fromisoformat(t_el.text.replace("Z", "+00:00")).timestamp()
            coords.append((lat, lon, t))

        if len(coords) < 10:
            return []

        clat = sum(c[0] for c in coords) / len(coords)
        clon = sum(c[1] for c in coords) / len(coords)
        m_lat = 111320.0
        m_lon = 111320.0 * math.cos(math.radians(clat))

        cum = 0.0
        last_a = math.atan2((coords[0][0] - clat) * m_lat, (coords[0][1] - clon) * m_lon)
        lap_times = [coords[0][2]]
        for lat, lon, t in coords[1:]:
            a = math.atan2((lat - clat) * m_lat, (lon - clon) * m_lon)
            da = a - last_a
            if da > math.pi:
                da -= 2 * math.pi
            elif da < -math.pi:
                da += 2 * math.pi
            cum += da
            last_a = a
            if abs(cum) >= 2 * math.pi:
                lap_times.append(t)
                cum -= math.copysign(2 * math.pi, cum)

        durs = [round(lap_times[i] - lap_times[i - 1], 1) for i in range(1, len(lap_times)) if lap_times[i] > lap_times[i - 1]]
        return durs
    except Exception as exc:
        logger.warning(f"Could not parse GPX sample {gpx_path}: {exc}")
        return []

# Built-in presets mapping scenario names to relative weights during automatic simulation.
SIMULATOR_PRESETS: Dict[str, Dict[str, float]] = {
    "happy_path": {
        "normal": 1.0,
        "double_bounce": 0.0,
        "double_count": 0.0,
        "missed_read": 0.0,
        "weak_stray": 0.0,
        "loiter": 0.0,
        "pack_crossing": 0.0,
        "unknown_tag": 0.0,
    },
    "realistic": {
        "normal": 0.78,
        "double_bounce": 0.06,
        "double_count": 0.02,
        "missed_read": 0.04,
        "weak_stray": 0.03,
        "loiter": 0.02,
        "pack_crossing": 0.04,
        "unknown_tag": 0.01,
    },
    "glitchy": {
        "normal": 0.35,
        "double_bounce": 0.15,
        "double_count": 0.10,
        "missed_read": 0.12,
        "weak_stray": 0.08,
        "loiter": 0.08,
        "pack_crossing": 0.07,
        "unknown_tag": 0.05,
    },
    "manual": {
        "normal": 1.0,
        "double_bounce": 0.0,
        "double_count": 0.0,
        "missed_read": 0.0,
        "weak_stray": 0.0,
        "loiter": 0.0,
        "pack_crossing": 0.0,
        "unknown_tag": 0.0,
    },
}

SCENARIO_METADATA: Dict[str, Dict[str, Any]] = {
    "normal": {
        "label": "Happy Path Crossing",
        "is_error": False,
        "description": "Clean bell-curve burst (6-10 reads over ~1s, peak around -42 dBm).",
    },
    "double_bounce": {
        "label": "Double-Bounce (Debounce Test)",
        "is_error": True,
        "description": "Runner crosses mat, then steps back into antenna range ~4s later (should be rejected by debounce).",
    },
    "double_count": {
        "label": "Short-Lap Double Count",
        "is_error": True,
        "description": "Runner triggers a second crossing right after the debounce window (records an anomalously short lap).",
    },
    "missed_read": {
        "label": "Missed Read (Occluded Tag)",
        "is_error": True,
        "description": "Runner physically passes the mat but tag is blocked (0 reads sent; next lap duration will be ~2x).",
    },
    "weak_stray": {
        "label": "Weak Stray Walk-By",
        "is_error": True,
        "description": "Single isolated weak read (-85 dBm) from a runner warming up or walking nearby.",
    },
    "loiter": {
        "label": "Loitering on Mat",
        "is_error": True,
        "description": "Runner stands on the mat chatting for several seconds, emitting continuous reads without a gap.",
    },
    "pack_crossing": {
        "label": "Pack Crossing (Interleaved)",
        "is_error": False,
        "description": "Multiple runners cross within 150ms of each other with interleaved tag reads.",
    },
    "unknown_tag": {
        "label": "Unassigned / Stray Tag",
        "is_error": True,
        "description": "Unregistered RFID tag passes the antenna (tests sniffer and unassigned-tag handling).",
    },
    "mass_start": {
        "label": "Mass Start (All Runners)",
        "is_error": False,
        "description": "All runners cross the starting mat together in a tight pack.",
    },
}


class MockReader(BaseRFIDReader):
    """
    Simulates runners circling a 400m track and crossing an RFID mat using scaled time.
    Generates realistic lap variations (steady, surges, slow/fatigue laps, and rest stops),
    tracks each runner's live position on the track for UI visualization, and supports
    configurable hardware/runner glitches (double-bounces, short-lap double-counts,
    occluded missed reads, mat loitering, weak stray reads, pack crossings, and unknown tags).
    """

    def __init__(
        self,
        on_tag_read: Callable[[TagRead], None],
        speed_multiplier: float = 1.0,
        gpx_path: Optional[str] = DEFAULT_SAMPLE_GPX,
        preset: str = "happy_path",
    ):
        super().__init__(on_tag_read)
        self.speed_multiplier = max(0.1, min(float(speed_multiplier), 50.0))
        self.paused: bool = False
        self.sim_time: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Simulation mode & fault weights
        self.preset: str = preset if preset in SIMULATOR_PRESETS else "happy_path"
        self.auto_crossing_enabled: bool = self.preset != "manual"
        if self.preset == "manual":
            self.paused = True
        self.weights: Dict[str, float] = dict(SIMULATOR_PRESETS[self.preset])

        # Ground-truth log of simulated crossings and glitches (most recent first)
        self.event_log: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._event_seq: int = 0
        self.stats: Dict[str, int] = {
            "total_events": 0,
            "total_reads_emitted": 0,
            "normal": 0,
            "double_bounce": 0,
            "double_count": 0,
            "missed_read": 0,
            "weak_stray": 0,
            "loiter": 0,
            "pack_crossing": 0,
            "unknown_tag": 0,
            "mass_start": 0,
        }

        self.participant_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None
        self.event_state_provider: Optional[Callable[[], Dict[str, Any]]] = None

        self.gpx_laps: List[float] = load_gpx_lap_durations(gpx_path) if gpx_path else []
        self.gpx_median: float = (
            sorted(self.gpx_laps)[len(self.gpx_laps) // 2] if self.gpx_laps else 120.0
        )
        if self.gpx_laps:
            logger.info(f"Loaded {len(self.gpx_laps)} sample laps from GPX (median {self.gpx_median:.1f}s).")

        # Simulated runners with realistic base lap times (in simulated seconds)
        self.simulated_runners: List[Dict[str, Any]] = [
            {
                "bib": 101,
                "name": "Alex Chen",
                "epc": "E28011700000020101010001",
                "color": RUNNER_COLORS[0],
                "base_pace": 58.0,
                "pace_scale": 0.72,
                "gpx_index": 0,
                "pending_lap_sec": None,
                "progress": 0.0,
                "state": "running",
                "pace_mode": "steady",
                "current_lap_target_sec": 58.0,
                "lap_elapsed_sim_sec": 0.0,
                "rest_remaining_sim_sec": 0.0,
                "rest_total_sim_sec": 0.0,
                "laps_completed": 0,
            },
            {
                "bib": 102,
                "name": "Maya Patel",
                "epc": "E28011700000020101010002",
                "color": RUNNER_COLORS[1],
                "base_pace": 68.0,
                "pace_scale": 0.85,
                "gpx_index": 17,
                "pending_lap_sec": None,
                "progress": 0.0,
                "state": "running",
                "pace_mode": "steady",
                "current_lap_target_sec": 68.0,
                "lap_elapsed_sim_sec": 0.0,
                "rest_remaining_sim_sec": 0.0,
                "rest_total_sim_sec": 0.0,
                "laps_completed": 0,
            },
            {
                "bib": 103,
                "name": "Jordan Lee",
                "epc": "E28011700000020101010003",
                "color": RUNNER_COLORS[2],
                "base_pace": 82.0,
                "pace_scale": 1.00,
                "gpx_index": 35,
                "pending_lap_sec": None,
                "progress": 0.0,
                "state": "running",
                "pace_mode": "steady",
                "current_lap_target_sec": 82.0,
                "lap_elapsed_sim_sec": 0.0,
                "rest_remaining_sim_sec": 0.0,
                "rest_total_sim_sec": 0.0,
                "laps_completed": 0,
            },
            {
                "bib": 104,
                "name": "Sam Rivera",
                "epc": "E28011700000020101010004",
                "color": RUNNER_COLORS[3],
                "base_pace": 96.0,
                "pace_scale": 1.12,
                "gpx_index": 52,
                "pending_lap_sec": None,
                "progress": 0.0,
                "state": "running",
                "pace_mode": "steady",
                "current_lap_target_sec": 96.0,
                "lap_elapsed_sim_sec": 0.0,
                "rest_remaining_sim_sec": 0.0,
                "rest_total_sim_sec": 0.0,
                "laps_completed": 0,
            },
            {
                "bib": 105,
                "name": "Taylor Kim",
                "epc": "E28011700000020101010005",
                "color": RUNNER_COLORS[4],
                "base_pace": 112.0,
                "pace_scale": 1.25,
                "gpx_index": 70,
                "pending_lap_sec": None,
                "progress": 0.0,
                "state": "running",
                "pace_mode": "steady",
                "current_lap_target_sec": 112.0,
                "lap_elapsed_sim_sec": 0.0,
                "rest_remaining_sim_sec": 0.0,
                "rest_total_sim_sec": 0.0,
                "laps_completed": 0,
            },
        ]

    # ------------------------------------------------------------------
    # Track Pacing & State (Used by /crossings & /api/simulator/state)
    # ------------------------------------------------------------------
    def _roll_next_lap(self, runner: Dict[str, Any], allow_rest: bool = True):
        """Picks the next lap from the GPX sample (splitting long laps into a rest stop + lap) or synthetic fallback."""
        if self.gpx_laps:
            idx = runner["gpx_index"] % len(self.gpx_laps)
            raw_dur = self.gpx_laps[idx]
            runner["gpx_index"] = idx + 1
            scaled_dur = raw_dur * runner["pace_scale"]
            runner_median = self.gpx_median * runner["pace_scale"]

            if allow_rest and raw_dur > self.gpx_median * 1.35:
                rest_sec = max(20.0, scaled_dur - runner_median)
                runner["pending_lap_sec"] = max(46.0, runner_median)
                runner["state"] = "resting"
                runner["pace_mode"] = "resting"
                runner["rest_remaining_sim_sec"] = round(rest_sec, 1)
                runner["rest_total_sim_sec"] = round(rest_sec, 1)
                runner["lap_elapsed_sim_sec"] = 0.0
                runner["progress"] = 0.0
                return

            mode = "fast" if raw_dur < self.gpx_median * 0.90 else ("slow" if raw_dur > self.gpx_median * 1.10 else "steady")
            runner["state"] = "running"
            runner["pace_mode"] = mode
            runner["current_lap_target_sec"] = round(max(46.0, scaled_dur), 1)
            runner["lap_elapsed_sim_sec"] = 0.0
            runner["rest_remaining_sim_sec"] = 0.0
            runner["rest_total_sim_sec"] = 0.0
            runner["progress"] = 0.0
            return

        if allow_rest and runner["laps_completed"] >= 1 and random.random() < 0.20:
            rest_sec = random.uniform(80.0, 150.0) if random.random() < 0.25 else random.uniform(20.0, 65.0)
            runner["state"] = "resting"
            runner["pace_mode"] = "resting"
            runner["rest_remaining_sim_sec"] = round(rest_sec, 1)
            runner["rest_total_sim_sec"] = round(rest_sec, 1)
            runner["lap_elapsed_sim_sec"] = 0.0
            runner["progress"] = 0.0
            return

        self._assign_running_pace(runner)

    def _assign_running_pace(self, runner: Dict[str, Any]):
        """Assigns a fast, slow, or steady lap pace (using pending GPX lap if resuming from rest)."""
        if runner.get("pending_lap_sec") is not None:
            target = runner["pending_lap_sec"]
            runner["pending_lap_sec"] = None
            runner["state"] = "running"
            runner["pace_mode"] = "steady"
            runner["current_lap_target_sec"] = round(max(46.0, target), 1)
            runner["lap_elapsed_sim_sec"] = 0.0
            runner["rest_remaining_sim_sec"] = 0.0
            runner["rest_total_sim_sec"] = 0.0
            runner["progress"] = 0.0
            return

        if self.gpx_laps:
            self._roll_next_lap(runner, allow_rest=False)
            return

        fatigue_factor = 1.0 + min(0.15, runner["laps_completed"] * 0.012)
        roll = random.random()
        if roll < 0.22:
            mode = "fast"
            factor = random.uniform(0.78, 0.90)
        elif roll < 0.48:
            mode = "slow"
            factor = random.uniform(1.15, 1.42)
        else:
            mode = "steady"
            factor = random.uniform(0.94, 1.07)

        target = max(46.0, runner["base_pace"] * fatigue_factor * factor)
        runner["state"] = "running"
        runner["pace_mode"] = mode
        runner["current_lap_target_sec"] = round(target, 1)
        runner["lap_elapsed_sim_sec"] = 0.0
        runner["rest_remaining_sim_sec"] = 0.0
        runner["rest_total_sim_sec"] = 0.0
        runner["progress"] = 0.0

    def set_speed_multiplier(self, multiplier: float):
        with self._lock:
            self.speed_multiplier = max(0.1, min(float(multiplier), 50.0))
        logger.info(f"Mock reader speed multiplier set to {self.speed_multiplier}x")

    def set_paused(self, paused: bool):
        with self._lock:
            self.paused = bool(paused)
            self.auto_crossing_enabled = not self.paused
        logger.info(f"Mock reader {'paused' if self.paused else 'resumed'}")

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "is_running": self.is_running,
                "paused": self.paused,
                "speed": self.speed_multiplier,
                "sim_time": round(self.sim_time, 1),
                "preset": self.preset,
                "runners": [
                    {
                        "bib": r["bib"],
                        "name": r["name"],
                        "color": r["color"],
                        "progress": round(r["progress"], 4),
                        "state": r["state"],
                        "pace_mode": r["pace_mode"],
                        "current_lap_target_sec": r["current_lap_target_sec"],
                        "rest_remaining_sim_sec": round(r["rest_remaining_sim_sec"], 1),
                        "laps_completed": r["laps_completed"],
                    }
                    for r in self.simulated_runners
                ],
            }

    # ------------------------------------------------------------------
    # Preset & Fault Injection Controls
    # ------------------------------------------------------------------
    def set_preset(self, preset: str, custom_weights: Optional[Dict[str, float]] = None):
        if preset not in SIMULATOR_PRESETS and preset != "custom":
            raise ValueError(f"Unknown simulator preset: {preset}")
        with self._lock:
            self.preset = preset
            if preset == "manual":
                self.auto_crossing_enabled = False
                self.paused = True
                self.weights = dict(SIMULATOR_PRESETS["manual"])
            elif preset == "custom" and custom_weights:
                self.auto_crossing_enabled = True
                self.paused = False
                for k in self.weights:
                    if k in custom_weights:
                        self.weights[k] = max(0.0, float(custom_weights[k]))
            else:
                self.auto_crossing_enabled = True
                self.paused = False
                self.weights = dict(SIMULATOR_PRESETS[preset])
                if custom_weights:
                    for k, v in custom_weights.items():
                        if k in self.weights:
                            self.weights[k] = max(0.0, float(v))
        logger.info(f"Mock reader preset set to '{self.preset}' (auto={self.auto_crossing_enabled})")

    def sync_runners_from_db(self):
        """Syncs simulated_runners names/EPCs with registered participants that have an EPC assigned."""
        if not self.participant_provider:
            return
        try:
            participants = self.participant_provider()
        except Exception as e:
            logger.debug(f"Could not sync participants for simulator: {e}")
            return

        tagged = [p for p in participants if p.get("epc")]
        if not tagged:
            return

        with self._lock:
            existing_by_bib = {r["bib"]: r for r in self.simulated_runners}
            default_paces = [58.0, 68.0, 82.0, 96.0, 112.0, 62.0, 74.0, 88.0]
            updated: List[Dict[str, Any]] = []
            for idx, p in enumerate(tagged):
                bib = p["bib_number"]
                if bib in existing_by_bib:
                    r = existing_by_bib[bib]
                    r["epc"] = p["epc"]
                    r["name"] = p.get("name", f"Bib {bib}")
                    updated.append(r)
                else:
                    pace = default_paces[idx % len(default_paces)]
                    updated.append({
                        "bib": bib,
                        "name": p.get("name", f"Bib {bib}"),
                        "epc": p["epc"],
                        "color": RUNNER_COLORS[idx % len(RUNNER_COLORS)],
                        "base_pace": pace,
                        "progress": 0.0,
                        "state": "running",
                        "pace_mode": "steady",
                        "current_lap_target_sec": pace,
                        "lap_elapsed_sim_sec": 0.0,
                        "rest_remaining_sim_sec": 0.0,
                        "rest_total_sim_sec": 0.0,
                        "laps_completed": 0,
                    })
            self.simulated_runners = updated

    def get_runner(self, bib: Optional[int] = None) -> Optional[Dict[str, Any]]:
        self.sync_runners_from_db()
        with self._lock:
            if not self.simulated_runners:
                return None
            if bib is not None:
                for r in self.simulated_runners:
                    if r["bib"] == bib:
                        return r
                return None
            return random.choice(self.simulated_runners)

    def get_status(self) -> Dict[str, Any]:
        self.sync_runners_from_db()
        with self._lock:
            runners_view = []
            for r in self.simulated_runners:
                remaining_sim = max(0.0, r.get("current_lap_target_sec", 60.0) - r.get("lap_elapsed_sim_sec", 0.0))
                eta = remaining_sim / max(0.1, self.speed_multiplier)
                runners_view.append({
                    "bib": r["bib"],
                    "name": r.get("name", f"Bib {r['bib']}"),
                    "epc": r["epc"],
                    "base_pace": r["base_pace"],
                    "progress": round(r.get("progress", 0.0), 3),
                    "state": r.get("state", "running"),
                    "next_crossing_eta_seconds": round(eta, 1),
                })
            return {
                "is_running": self.is_running,
                "auto_crossing_enabled": self.auto_crossing_enabled,
                "paused": self.paused,
                "preset": self.preset,
                "speed_multiplier": self.speed_multiplier,
                "weights": dict(self.weights),
                "stats": dict(self.stats),
                "runners": runners_view,
                "scenarios": SCENARIO_METADATA,
                "recent_events": list(self.event_log)[:50],
            }

    def clear_log(self):
        with self._lock:
            self.event_log.clear()
            for k in self.stats:
                self.stats[k] = 0

    # ------------------------------------------------------------------
    # Reader Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._stop_event.clear()

        with self._lock:
            stagger_positions = [0.86, 0.72, 0.56, 0.38, 0.22]
            for r, pos in zip(self.simulated_runners, stagger_positions):
                self._assign_running_pace(r)
                r["progress"] = pos
                r["lap_elapsed_sim_sec"] = pos * r["current_lap_target_sec"]

        self._thread = threading.Thread(target=self._run, daemon=True, name="MockReaderThread")
        self._thread.start()
        logger.info(f"Mock RFID Reader started (speed={self.speed_multiplier}x).")

    def stop(self):
        if not self.is_running:
            return
        self._stop_event.set()
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Mock RFID Reader stopped.")

    def _choose_scenario(self) -> str:
        with self._lock:
            items = list(self.weights.items())
        scenarios = [k for k, w in items if w > 0]
        weights = [w for _, w in items if w > 0]
        if not scenarios:
            return "normal"
        return random.choices(scenarios, weights=weights, k=1)[0]

    def _run(self):
        tick_sec = 0.1
        while not self._stop_event.is_set():
            crossed_bibs: List[int] = []
            with self._lock:
                if not self.paused and self.auto_crossing_enabled:
                    sim_dt = tick_sec * self.speed_multiplier
                    self.sim_time += sim_dt
                    for runner in self.simulated_runners:
                        if runner["state"] == "resting":
                            runner["rest_remaining_sim_sec"] -= sim_dt
                            if runner["rest_remaining_sim_sec"] <= 0.0:
                                self._assign_running_pace(runner)
                        else:
                            runner["lap_elapsed_sim_sec"] += sim_dt
                            target = max(1.0, runner["current_lap_target_sec"])
                            runner["progress"] = min(1.0, runner["lap_elapsed_sim_sec"] / target)
                            if runner["lap_elapsed_sim_sec"] >= target:
                                crossed_bibs.append(runner["bib"])
                                runner["laps_completed"] += 1
                                self._roll_next_lap(runner, allow_rest=True)

            for bib in crossed_bibs:
                scenario = self._choose_scenario()
                threading.Thread(
                    target=self.run_scenario,
                    kwargs={"scenario": scenario, "bib": bib, "blocking": True},
                    daemon=True,
                ).start()

            time.sleep(tick_sec)

    def _get_effective_min_lap(self) -> float:
        min_lap_sec = 45.0
        if self.event_state_provider:
            try:
                state = self.event_state_provider()
                if state and state.get("min_lap_seconds"):
                    min_lap_sec = float(state["min_lap_seconds"])
            except Exception:
                pass
        return min_lap_sec / max(0.1, self.speed_multiplier)

    # ------------------------------------------------------------------
    # Message Generation Primitives & Scenario Execution
    # ------------------------------------------------------------------
    def _record_sim_event(
        self,
        scenario: str,
        bibs: List[int],
        epcs: List[str],
        reads_emitted: int,
        peak_rssi: Optional[float],
        description: str,
        timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        meta = SCENARIO_METADATA.get(scenario, {"label": scenario, "is_error": False})
        with self._lock:
            self._event_seq += 1
            entry = {
                "id": self._event_seq,
                "timestamp": timestamp if timestamp is not None else clock.now(),
                "scenario": scenario,
                "label": meta["label"],
                "is_error": meta["is_error"],
                "bibs": bibs,
                "epcs": epcs,
                "reads_emitted": reads_emitted,
                "peak_rssi": round(peak_rssi, 1) if peak_rssi is not None else None,
                "description": description,
            }
            self.event_log.appendleft(entry)
            self.stats["total_events"] = self.stats.get("total_events", 0) + 1
            self.stats["total_reads_emitted"] = self.stats.get("total_reads_emitted", 0) + reads_emitted
            if scenario in self.stats:
                self.stats[scenario] += 1
            return entry

    def generate_bell_curve_reads(
        self,
        epc: str,
        base_time: float,
        num_reads: int = 8,
        duration: float = 1.0,
        peak_rssi: float = -42.0,
        edge_rssi: float = -78.0,
        noise: float = 2.0,
    ) -> List[TagRead]:
        """Generates a list of TagRead objects with a bell-curve RSSI profile."""
        reads: List[TagRead] = []
        span = peak_rssi - edge_rssi
        for i in range(num_reads):
            progress = i / max(1, (num_reads - 1))
            t = base_time + progress * duration
            peak_factor = 1.0 - 4.0 * ((progress - 0.5) ** 2)
            rssi = edge_rssi + (span * max(0.0, peak_factor)) + random.uniform(-noise, noise)
            reads.append(TagRead(epc=epc, timestamp=t, rssi=round(rssi, 1)))
        return reads

    def _emit_reads(self, reads: List[TagRead], real_time_delay: Optional[float] = None):
        """Sends TagRead messages to on_tag_read callback."""
        for idx, read in enumerate(reads):
            if self._stop_event.is_set() and real_time_delay:
                break
            if real_time_delay and real_time_delay > 0:
                read = TagRead(epc=read.epc, timestamp=clock.now(), rssi=read.rssi)
            if self.on_tag_read:
                self.on_tag_read(read)
            if real_time_delay and real_time_delay > 0 and idx < len(reads) - 1:
                time.sleep(real_time_delay)

    def _simulate_crossing_burst(self, epc: str):
        """Simulates 6 to 10 reads with a bell-curve RSSI, scaled so high sim speeds stay crisp."""
        num_reads = random.randint(6, 10)
        speed = max(1.0, self.speed_multiplier)
        for i in range(num_reads):
            if self._stop_event.is_set():
                break
            progress = i / max(1, (num_reads - 1))
            peak_factor = 1.0 - 4.0 * ((progress - 0.5) ** 2)
            rssi = -78.0 + (36.0 * max(0.0, peak_factor)) + random.uniform(-2.5, 2.5)

            read = TagRead(epc=epc, timestamp=clock.now(), rssi=round(rssi, 1))
            self.on_tag_read(read)
            time.sleep(max(0.008, random.uniform(0.08, 0.15) / speed))

    def trigger_scenario_async(
        self,
        scenario: str,
        bib: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Triggers a scenario in a background thread so HTTP handlers return immediately."""
        runner = self.get_runner(bib) if scenario not in ("unknown_tag", "pack_crossing", "mass_start") else None
        if scenario not in ("unknown_tag", "pack_crossing", "mass_start") and not runner:
            raise ValueError(f"Runner with bib {bib} not found")

        t = threading.Thread(
            target=self.run_scenario,
            kwargs={"scenario": scenario, "bib": bib, "blocking": True, **kwargs},
            daemon=True,
            name=f"SimScenario-{scenario}",
        )
        t.start()
        return {
            "status": "scheduled",
            "scenario": scenario,
            "label": SCENARIO_METADATA.get(scenario, {}).get("label", scenario),
            "bib": runner["bib"] if runner else bib,
            "runner": runner.get("name") if runner else None,
        }

    def run_scenario(
        self,
        scenario: str,
        bib: Optional[int] = None,
        blocking: bool = True,
        base_time: Optional[float] = None,
        crossing_window: float = 2.5,
    ) -> Dict[str, Any]:
        """
        Executes a simulation scenario.
        - When `blocking=True` and `base_time=None` (live mode), sleeps realistically between reads.
        - When `blocking=False` or `base_time` is provided (deterministic/test mode), emits reads
          immediately with synthetic timestamps starting at `base_time`.
        """
        instant = (not blocking) or (base_time is not None)
        t0 = base_time if base_time is not None else clock.now()
        speed = max(1.0, self.speed_multiplier)
        step_delay = None if instant else max(0.008, 0.09 / speed)

        if scenario == "unknown_tag":
            epc = f"E280117099990000DEAD{random.randint(1000, 9999)}"
            reads = self.generate_bell_curve_reads(epc=epc, base_time=t0, num_reads=6, duration=0.6, peak_rssi=-46.0)
            self._emit_reads(reads, real_time_delay=step_delay)
            peak = max(r.rssi for r in reads)
            return self._record_sim_event(
                scenario="unknown_tag",
                bibs=[],
                epcs=[epc],
                reads_emitted=len(reads),
                peak_rssi=peak,
                description=f"Unassigned tag {epc[-8:]} crossed mat ({len(reads)} reads, peak {peak:.1f} dBm)",
                timestamp=t0,
            )

        if scenario in ("pack_crossing", "mass_start"):
            self.sync_runners_from_db()
            with self._lock:
                runners_copy = list(self.simulated_runners)
            if scenario == "mass_start":
                pack = runners_copy
            else:
                k = min(len(runners_copy), random.randint(2, min(4, max(2, len(runners_copy)))))
                pack = random.sample(runners_copy, k=k) if runners_copy else []

            all_reads: List[TagRead] = []
            for idx, r in enumerate(pack):
                offset = idx * 0.07 + random.uniform(0.0, 0.05)
                r_reads = self.generate_bell_curve_reads(
                    epc=r["epc"],
                    base_time=t0 + offset,
                    num_reads=7,
                    duration=0.8,
                    peak_rssi=-41.0 - random.uniform(0.0, 6.0),
                )
                all_reads.extend(r_reads)
            all_reads.sort(key=lambda x: x.timestamp)
            self._emit_reads(all_reads, real_time_delay=max(0.005, 0.025 / speed) if not instant else None)
            bibs = [r["bib"] for r in pack]
            epcs = [r["epc"] for r in pack]
            peak = max((r.rssi for r in all_reads), default=-42.0)
            label_prefix = "Mass start" if scenario == "mass_start" else "Pack crossing"
            return self._record_sim_event(
                scenario=scenario,
                bibs=bibs,
                epcs=epcs,
                reads_emitted=len(all_reads),
                peak_rssi=peak,
                description=f"{label_prefix}: Bibs {', '.join('#' + str(b) for b in bibs)} crossed together ({len(all_reads)} interleaved reads)",
                timestamp=t0,
            )

        runner = self.get_runner(bib)
        if not runner:
            raise ValueError(f"Runner not found for bib={bib}")
        r_bib = runner["bib"]
        r_name = runner.get("name", f"Bib {r_bib}")
        epc = runner["epc"]

        if scenario == "normal":
            num_reads = random.randint(6, 10)
            reads = self.generate_bell_curve_reads(epc=epc, base_time=t0, num_reads=num_reads, duration=0.9)
            self._emit_reads(reads, real_time_delay=step_delay)
            peak = max(r.rssi for r in reads)
            return self._record_sim_event(
                scenario="normal",
                bibs=[r_bib],
                epcs=[epc],
                reads_emitted=len(reads),
                peak_rssi=peak,
                description=f"#{r_bib} ({r_name}) clean crossing ({len(reads)} reads, peak {peak:.1f} dBm)",
                timestamp=t0,
            )

        if scenario == "missed_read":
            return self._record_sim_event(
                scenario="missed_read",
                bibs=[r_bib],
                epcs=[epc],
                reads_emitted=0,
                peak_rssi=None,
                description=f"#{r_bib} ({r_name}) crossed mat with occluded tag — 0 reads sent (next split will be ~2x)",
                timestamp=t0,
            )

        if scenario == "weak_stray":
            rssi = round(random.uniform(-88.0, -82.0), 1)
            reads = [TagRead(epc=epc, timestamp=t0, rssi=rssi)]
            self._emit_reads(reads, real_time_delay=None)
            return self._record_sim_event(
                scenario="weak_stray",
                bibs=[r_bib],
                epcs=[epc],
                reads_emitted=1,
                peak_rssi=rssi,
                description=f"#{r_bib} ({r_name}) stray single weak read ({rssi} dBm) from outer lane",
                timestamp=t0,
            )

        if scenario == "double_bounce":
            reads1 = self.generate_bell_curve_reads(epc=epc, base_time=t0, num_reads=7, duration=0.7, peak_rssi=-41.0)
            bounce_gap = crossing_window + 1.2
            t_bounce = t0 + 0.7 + bounce_gap
            reads2 = self.generate_bell_curve_reads(epc=epc, base_time=t_bounce, num_reads=5, duration=0.5, peak_rssi=-51.0)

            self._emit_reads(reads1, real_time_delay=step_delay)
            if not instant:
                time.sleep(max(0.1, bounce_gap / speed))
            self._emit_reads(reads2, real_time_delay=step_delay)

            total_reads = len(reads1) + len(reads2)
            peak = max(r.rssi for r in reads1 + reads2)
            return self._record_sim_event(
                scenario="double_bounce",
                bibs=[r_bib],
                epcs=[epc],
                reads_emitted=total_reads,
                peak_rssi=peak,
                description=f"#{r_bib} ({r_name}) crossed mat, then bounced back {bounce_gap:.1f}s later (2 bursts, {total_reads} reads)",
                timestamp=t0,
            )

        if scenario == "double_count":
            eff_min_lap = self._get_effective_min_lap()
            reads1 = self.generate_bell_curve_reads(epc=epc, base_time=t0, num_reads=7, duration=0.7, peak_rssi=-42.0)
            self._emit_reads(reads1, real_time_delay=step_delay)
            peak = max(r.rssi for r in reads1)

            if instant:
                t2 = t0 + eff_min_lap + 1.5
                reads2 = self.generate_bell_curve_reads(epc=epc, base_time=t2, num_reads=6, duration=0.6, peak_rssi=-44.0)
                self._emit_reads(reads2, real_time_delay=None)
                return self._record_sim_event(
                    scenario="double_count",
                    bibs=[r_bib],
                    epcs=[epc],
                    reads_emitted=len(reads1) + len(reads2),
                    peak_rssi=peak,
                    description=f"#{r_bib} ({r_name}) double-counted: 2 crossings {eff_min_lap + 1.5:.1f}s apart (just past debounce)",
                    timestamp=t0,
                )
            else:
                with self._lock:
                    target = max(1.0, runner.get("current_lap_target_sec", 60.0))
                    short_sim_sec = max(2.0, (eff_min_lap + 1.0) * self.speed_multiplier)
                    runner["lap_elapsed_sim_sec"] = max(0.0, target - short_sim_sec)
                    runner["progress"] = min(0.98, runner["lap_elapsed_sim_sec"] / target)
                return self._record_sim_event(
                    scenario="double_count",
                    bibs=[r_bib],
                    epcs=[epc],
                    reads_emitted=len(reads1),
                    peak_rssi=peak,
                    description=f"#{r_bib} ({r_name}) crossed now + scheduled short-lap re-crossing in {eff_min_lap + 1.0:.1f}s",
                    timestamp=t0,
                )

        if scenario == "loiter":
            num_reads = 20
            duration = 4.5
            reads: List[TagRead] = []
            for i in range(num_reads):
                t = t0 + (i * (duration / max(1, num_reads - 1)))
                rssi = round(-52.0 + random.uniform(-6.0, 6.0), 1)
                reads.append(TagRead(epc=epc, timestamp=t, rssi=rssi))
            self._emit_reads(reads, real_time_delay=max(0.01, 0.22 / speed) if not instant else None)
            peak = max(r.rssi for r in reads)
            return self._record_sim_event(
                scenario="loiter",
                bibs=[r_bib],
                epcs=[epc],
                reads_emitted=len(reads),
                peak_rssi=peak,
                description=f"#{r_bib} ({r_name}) loitered on mat for {duration:.1f}s ({len(reads)} continuous reads)",
                timestamp=t0,
            )

        raise ValueError(f"Unknown scenario: {scenario}")
