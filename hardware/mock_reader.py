import time
import random
import threading
import logging
from typing import Callable, List, Dict
from core.models import TagRead
from hardware.base import BaseRFIDReader

logger = logging.getLogger("MockReader")

class MockReader(BaseRFIDReader):
    """
    Simulates runners circling a 400m track and crossing an RFID mat.
    Emits realistic burst reads with RSSI curves.
    """
    def __init__(self, on_tag_read: Callable[[TagRead], None], speed_multiplier: float = 1.0):
        super().__init__(on_tag_read)
        self.speed_multiplier = speed_multiplier
        self._thread: threading.Thread = None
        self._stop_event = threading.Event()
        
        # Simulated runners with base lap times (in seconds)
        # With speed_multiplier = 4.0, a 60s lap happens in 15 seconds!
        self.simulated_runners: List[Dict] = [
            {"bib": 101, "epc": "E28011700000020101010001", "base_pace": 55.0, "next_crossing": 0.0},
            {"bib": 102, "epc": "E28011700000020101010002", "base_pace": 68.0, "next_crossing": 0.0},
            {"bib": 103, "epc": "E28011700000020101010003", "base_pace": 80.0, "next_crossing": 0.0},
            {"bib": 104, "epc": "E28011700000020101010004", "base_pace": 95.0, "next_crossing": 0.0},
            {"bib": 105, "epc": "E28011700000020101010005", "base_pace": 110.0, "next_crossing": 0.0},
        ]

    def set_speed_multiplier(self, multiplier: float):
        old_multiplier = self.speed_multiplier
        self.speed_multiplier = max(0.1, min(multiplier, 20.0))
        logger.info(f"Mock reader speed multiplier set to {self.speed_multiplier}x")

        # Immediately reschedule remaining wait time so speed change takes effect right away
        now = time.time()
        for runner in self.simulated_runners:
            remaining = runner.get("next_crossing", now) - now
            if remaining > 0 and old_multiplier > 0:
                new_remaining = remaining * (old_multiplier / self.speed_multiplier)
                runner["next_crossing"] = now + max(0.5, new_remaining)
            else:
                runner["next_crossing"] = now + random.uniform(0.5, 2.5)

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._stop_event.clear()
        
        # Stagger initial crossing times
        now = time.time()
        for r in self.simulated_runners:
            interval = (r["base_pace"] / self.speed_multiplier) + random.uniform(-2, 3)
            r["next_crossing"] = now + interval

        self._thread = threading.Thread(target=self._run, daemon=True, name="MockReaderThread")
        self._thread.start()
        logger.info("Mock RFID Reader started.")

    def stop(self):
        if not self.is_running:
            return
        self._stop_event.set()
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Mock RFID Reader stopped.")

    def _run(self):
        while not self._stop_event.is_set():
            now = time.time()
            for runner in self.simulated_runners:
                if now >= runner["next_crossing"]:
                    self._simulate_crossing_burst(runner["epc"])
                    # Schedule next lap
                    jitter = random.uniform(-3.0, 3.0)
                    pace = max(30.0, runner["base_pace"] + jitter)
                    runner["next_crossing"] = now + (pace / self.speed_multiplier)

            time.sleep(0.2)

    def _simulate_crossing_burst(self, epc: str):
        """Simulates 6 to 10 reads over 1.2 seconds with a bell-curve RSSI."""
        num_reads = random.randint(6, 10)
        start_time = time.time()
        
        for i in range(num_reads):
            if self._stop_event.is_set():
                break
            # Bell curve: starts low (-78 dBm), peaks (-42 dBm), ends low (-75 dBm)
            progress = i / max(1, (num_reads - 1))
            peak_factor = 1.0 - 4.0 * ((progress - 0.5) ** 2)  # 0 at edges, 1 at center
            rssi = -78.0 + (36.0 * max(0.0, peak_factor)) + random.uniform(-2.5, 2.5)

            read = TagRead(epc=epc, timestamp=time.time(), rssi=round(rssi, 1))
            self.on_tag_read(read)
            time.sleep(random.uniform(0.08, 0.15))
