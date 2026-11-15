import time
import random
import threading
import logging
from typing import Callable, List, Dict, Any, Optional
from core.models import TagRead
from core import clock
from hardware.base import BaseRFIDReader

logger = logging.getLogger("MockReader")

RUNNER_COLORS = ["#34d399", "#38bdf8", "#fbbf24", "#f472b6", "#a78bfa"]


class MockReader(BaseRFIDReader):
    """
    Simulates runners circling a 400m track and crossing an RFID mat using scaled time.
    Generates realistic lap variations (steady, surges, slow/fatigue laps, and rest stops)
    and tracks each runner's live position on the track for UI visualization.
    """

    def __init__(self, on_tag_read: Callable[[TagRead], None], speed_multiplier: float = 1.0):
        super().__init__(on_tag_read)
        self.speed_multiplier = max(0.1, min(float(speed_multiplier), 50.0))
        self.paused: bool = False
        self.sim_time: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Simulated runners with realistic base lap times (in simulated seconds)
        self.simulated_runners: List[Dict[str, Any]] = [
            {
                "bib": 101,
                "name": "Alex Chen",
                "epc": "E28011700000020101010001",
                "color": RUNNER_COLORS[0],
                "base_pace": 58.0,
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
        """Picks a realistic next lap (steady, fast surge, slow recovery, or a rest stop)."""
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
        """Assigns a fast, slow, or steady lap pace with mild fatigue drift."""
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
