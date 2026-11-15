import os
import math
import time
import random
import threading
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Callable, List, Dict, Any, Optional
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


class MockReader(BaseRFIDReader):
    """
    Simulates runners circling a 400m track and crossing an RFID mat using scaled time.
    Generates realistic lap variations (steady, surges, slow/fatigue laps, and rest stops)
    and tracks each runner's live position on the track for UI visualization.
    """

    def __init__(
        self,
        on_tag_read: Callable[[TagRead], None],
        speed_multiplier: float = 1.0,
        gpx_path: Optional[str] = DEFAULT_SAMPLE_GPX,
    ):
        super().__init__(on_tag_read)
        self.speed_multiplier = max(0.1, min(float(speed_multiplier), 50.0))
        self.paused: bool = False
        self.sim_time: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

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
            # Occasional water/stretch stop (20-75s) or longer break (80-150s)
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
        logger.info(f"Mock reader {'paused' if self.paused else 'resumed'}")

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "is_running": self.is_running,
                "paused": self.paused,
                "speed": self.speed_multiplier,
                "sim_time": round(self.sim_time, 1),
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

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._stop_event.clear()

        with self._lock:
            # Stagger initial track positions so crossings start happening right away
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

    def _run(self):
        tick_sec = 0.1
        while not self._stop_event.is_set():
            epcs_crossed: List[str] = []
            with self._lock:
                if not self.paused:
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
                                epcs_crossed.append(runner["epc"])
                                runner["laps_completed"] += 1
                                self._roll_next_lap(runner, allow_rest=True)

            for epc in epcs_crossed:
                threading.Thread(
                    target=self._simulate_crossing_burst,
                    args=(epc,),
                    daemon=True,
                ).start()

            time.sleep(tick_sec)

    def _simulate_crossing_burst(self, epc: str):
        """Simulates 6 to 10 reads with a bell-curve RSSI, scaled so high sim speeds stay crisp."""
        num_reads = random.randint(6, 10)
        speed = max(1.0, self.speed_multiplier)
        for i in range(num_reads):
            if self._stop_event.is_set():
                break
            # Bell curve: starts low (-78 dBm), peaks (-42 dBm), ends low (-75 dBm)
            progress = i / max(1, (num_reads - 1))
            peak_factor = 1.0 - 4.0 * ((progress - 0.5) ** 2)
            rssi = -78.0 + (36.0 * max(0.0, peak_factor)) + random.uniform(-2.5, 2.5)

            read = TagRead(epc=epc, timestamp=clock.now(), rssi=round(rssi, 1))
            self.on_tag_read(read)
            time.sleep(max(0.008, random.uniform(0.08, 0.15) / speed))
