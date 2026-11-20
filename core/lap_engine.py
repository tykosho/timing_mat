import asyncio
import logging
import time
from collections import deque
from typing import Deque, Dict, List, Tuple, Callable, Optional, Any
from core.db import Database
from core import clock
from core.models import TagRead

logger = logging.getLogger("LapEngine")

# Longest a single crossing's burst of reads may last before it's resolved anyway (issue #11).
# A runner passing the mat is in the antenna field for 1-2 s.
MAX_BURST_SECONDS = 8.0
# A first crossing between the minimum lap time and this many typical laps after the gun is
# flagged as a possible missed start (#72); before there are enough laps, within 10 minutes.
MISSED_START_LAPS = 2.5
MISSED_START_DEFAULT_WINDOW = 600.0
# flush_loop ticks every 0.2 s. No successful tick for this long means laps aren't being
# recorded: /api/event fails (Docker healthcheck) and the admin strip turns red (issue #83).
FLUSH_STALE_SECONDS = 10.0
FLUSH_INTERVAL = 0.2
# A crossing whose resolution failed (e.g. a transient DB error) is retried every tick for this
# long before it is given up on, loudly.
CROSSING_RETRY_SECONDS = 300.0
FLUSH_RETRY_DELAY = 1.0  # after a failed tick: don't hammer a failing disk 5 times a second

class LapEngine:
    def __init__(self, db: Database, 
                 crossing_window: float = 2.0, 
                 on_lap_completed: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_runner_started: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_runner_resumed: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_tag_detected: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_unassigned_tag: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_crossing_too_soon: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.db = db
        self.crossing_window = crossing_window
        self.on_lap_completed = on_lap_completed
        self.on_runner_started = on_runner_started
        self.on_runner_resumed = on_runner_resumed
        self.on_tag_detected = on_tag_detected
        self.on_unassigned_tag = on_unassigned_tag
        self.on_crossing_too_soon = on_crossing_too_soon
        self.speed_multiplier: float = 1.0

        # Buffer to aggregate bursts of reads: epc -> list of (timestamp, rssi)
        self.buffers: Dict[str, List[Tuple[float, float]]] = {}
        # Tags still in the field after a burst was resolved for running too long (issue #85):
        # epc -> time of its last read. Their later reads are the same visit to the mat, so they
        # are dropped until the tag has been quiet for the crossing window.
        self.lingering: Dict[str, float] = {}
        # Bursts whose resolution raised, retried first on the next tick: (epc, reads, quiet)
        self.failed_bursts: List[Tuple[str, List[Tuple[float, float]], bool]] = []
        self.failed_event_id: Optional[int] = None

        # Raw read log. The reader thread appends (deque.append is thread-safe) and flush_loop
        # writes batches to the DB. Only reads while an event is running/paused are kept.
        self.raw_queue: Deque[Tuple[int, str, float, float]] = deque()
        self.log_event_id: Optional[int] = None

        # flush_loop health (issue #83), on time.monotonic(): when it started, when a tick last
        # completed, and the latest error (cleared by the next good tick).
        self._monotonic: Callable[[], float] = time.monotonic
        self.flush_started_at: Optional[float] = None
        self.last_flush_at: Optional[float] = None
        self.flush_errors = 0
        self.last_flush_error: Optional[str] = None

    def refresh_event(self):
        """Re-reads the current event so raw reads are tagged with the right event (or dropped)."""
        event = self.db.get_event()
        log_event_id = event["id"] if event and event["status"] in ("running", "paused") else None
        if log_event_id is None or log_event_id != self.log_event_id:
            # Reset, or another event: a tag parked by the mat starts afresh in it
            self.lingering.clear()
        event_id = event["id"] if event else None
        if event_id != self.failed_event_id or (event and event["status"] == "setup"):
            # Reset, or another event. (Not End: a crossing from before End still counts.)
            self.failed_bursts.clear()
        self.failed_event_id = event_id
        self.log_event_id = log_event_id

    def maintain_clock(self):
        """Keeps the race clock honest about system clock changes (see core/clock.py)."""
        clock.clock.maintain(self.db.race_in_progress(), self.db.latest_timestamp())

    def flush_raw_reads(self):
        """Writes queued raw reads to the database."""
        batch = []
        while self.raw_queue:
            batch.append(self.raw_queue.popleft())
        if batch:
            try:
                self.db.insert_raw_reads(batch)
            except Exception:
                self.raw_queue.extendleft(reversed(batch))  # keep them for the next try
                raise

    def forget_tag(self, epc: Optional[str]):
        """The runner's start was reset (issue #113): the tag's next reads are a fresh crossing,
        even if it never left the field. Reads and failed crossings from before the reset go."""
        if not epc:
            return
        epc = epc.upper().strip()
        self.lingering.pop(epc, None)
        self.buffers.pop(epc, None)
        self.failed_bursts = [b for b in self.failed_bursts if b[0] != epc]

    def process_tag_read(self, tag_read: TagRead):
        """Called for every read from the RFID reader (mock or physical).

        Must run on the event loop, like flush_once: web/app.py hands reads over from the
        reader thread with call_soon_threadsafe, so the buffers have a single owner (issue #13).
        """
        epc = tag_read.epc.upper().strip()
        now = tag_read.timestamp

        # Notify any listeners that an RFID tag was detected (useful for Admin registration)
        if self.on_tag_detected:
            self.on_tag_detected({
                "epc": epc,
                "timestamp": now,
                "rssi": tag_read.rssi
            })

        event_id = self.log_event_id
        if event_id is not None:
            self.raw_queue.append((event_id, epc, now, tag_read.rssi))

        if epc not in self.buffers:
            self.buffers[epc] = []
        self.buffers[epc].append((now, tag_read.rssi))

    async def flush_loop(self):
        """Background coroutine that checks buffers and triggers crossings when a burst finishes.

        One failing tick (disk full, SD-card I/O error, a broken callback) is logged and the
        loop carries on; it must never end mid-race (issue #83).
        """
        ticks = 0
        failures = 0
        self.flush_started_at = self._monotonic()
        self.last_flush_at = None
        try:
            while True:
                await asyncio.sleep(FLUSH_RETRY_DELAY if failures else FLUSH_INTERVAL)
                try:
                    self.refresh_event()
                    self.flush_raw_reads()
                    ticks += 1
                    if ticks % 150 == 0:  # every ~30 s
                        self.maintain_clock()
                    await self.flush_once(clock.now())
                except Exception as e:
                    failures += 1
                    self.flush_errors += 1
                    self.last_flush_error = f"{type(e).__name__}: {e}"
                    if failures == 1 or failures % 10 == 0:  # first one, then every ~10 s
                        logger.exception(f"Flush loop tick failed ({failures} in a row); retrying")
                    continue
                if failures:
                    logger.info(f"Flush loop recovered after {failures} failed tick(s)")
                    failures = 0
                self.last_flush_at = self._monotonic()
                self.last_flush_error = None
        finally:
            self.flush_started_at = None  # stopped on purpose (shutdown): not "stalled"

    def flush_health(self) -> Dict[str, Any]:
        """Is flush_loop still ticking? Before it has started (tests, startup) it counts as ok."""
        if self.flush_started_at is None:
            return {"ok": True, "running": False, "seconds_since_tick": None,
                    "errors": self.flush_errors, "last_error": self.last_flush_error}
        since = self._monotonic() - (self.last_flush_at or self.flush_started_at)
        return {"ok": since < FLUSH_STALE_SECONDS, "running": True, "seconds_since_tick": round(since, 1),
                "errors": self.flush_errors, "last_error": self.last_flush_error}

    async def flush_once(self, now: float):
        """Resolves every burst of reads that has finished (or gone on too long) by `now`."""
        effective_window = max(0.15, self.crossing_window / max(1.0, self.speed_multiplier))
        epcs_to_process = []

        max_burst = MAX_BURST_SECONDS / max(1.0, self.speed_multiplier)
        for epc, reads in list(self.buffers.items()):
            if not reads:
                continue
            # If no new read for effective_window seconds, runner has exited antenna beam.
            # A tag that never leaves (spare bib by the mat, runner standing on it) is
            # resolved anyway once its burst is MAX_BURST_SECONDS old, so it isn't stuck.
            quiet = (now - reads[-1][0]) >= effective_window
            if quiet or (now - reads[0][0]) >= max_burst:
                epcs_to_process.append((epc, quiet))

        # A lingering tag that was quiet for the crossing window has left the field, even if it
        # is already back (its new reads are a new crossing) or left no buffer behind.
        for epc, last_read in list(self.lingering.items()):
            reads = self.buffers.get(epc)
            if ((reads[0][0] if reads else now) - last_read) >= effective_window:
                del self.lingering[epc]

        # Take the bursts before resolving any: a read arriving while a callback awaits starts
        # a new burst instead of being swept into (or dropped with) one already finished.
        # Bursts that failed last tick go first, so a runner's crossings resolve in order.
        bursts, self.failed_bursts = self.failed_bursts, []
        bursts += [(epc, self.buffers.pop(epc), quiet) for epc, quiet in epcs_to_process]
        error: Optional[Exception] = None
        failed_epcs = set()
        for epc, reads, quiet in bursts:
            if epc in failed_epcs:
                # An earlier crossing of this tag is still failing: keep this one behind it
                self.failed_bursts.append((epc, reads, quiet))
                continue
            if epc in self.lingering and (reads[0][0] - self.lingering[epc]) >= effective_window:
                # A later visit queued behind a retried burst of the same tag (the tag left
                # in between): a crossing of its own
                del self.lingering[epc]
            if epc in self.lingering:
                # Same visit to the mat as a crossing already resolved: no lap, no too-soon prompt
                if quiet:
                    del self.lingering[epc]
                else:
                    self.lingering[epc] = reads[-1][0]
                continue
            try:
                outcome = await self._resolve_crossing(epc, reads)
            except Exception as e:
                # Keep the burst for the next tick instead of losing the crossing. A retry of
                # one that was already written is recognised and not counted twice.
                error = error or e
                if now - reads[-1][0] > CROSSING_RETRY_SECONDS:
                    logger.error(f"Giving up on a crossing of {epc} at {reads[-1][0]:.1f} after "
                                 f"{CROSSING_RETRY_SECONDS:.0f}s of failures: {type(e).__name__}: {e}")
                else:
                    self.failed_bursts.append((epc, reads, quiet))
                    failed_epcs.add(epc)
                continue
            if not quiet and outcome is not None:
                self.lingering[epc] = reads[-1][0]
        if error:
            raise error  # flush_loop counts and logs the failed tick

    def _missed_start_reason(self, seconds_after_gun: float, min_lap_sec: float) -> Optional[str]:
        """Issue #72: in the crowd at the gun a tag may not be read at all. Then the runner's
        first lap crossing becomes their start and they silently lose a lap. A first crossing
        about a lap or so after the gun looks like that; it could also be someone joining late,
        so it is only flagged for the admin to decide."""
        race_seconds = seconds_after_gun * self.speed_multiplier
        typical = self.db.typical_lap_seconds()
        latest = MISSED_START_LAPS * typical if typical else MISSED_START_DEFAULT_WINDOW
        if not (min_lap_sec <= race_seconds <= latest):
            return None
        m, sec = divmod(int(round(race_seconds)), 60)
        return f"First seen {m}:{sec:02d} after the gun: start missed in the crowd, or joined late?"

    async def _resolve_crossing(self, epc: str, reads: List[Tuple[float, float]], force: bool = False) -> Optional[str]:
        """Determines peak timestamp and updates participant state (start, lap, or break resume).

        Returns what happened: "started", "lap", "resumed", "debounced", "duplicate" (this
        crossing was already recorded), or None when the crossing was ignored (unknown tag,
        event not running).
        """
        peak_time, peak_rssi = max(reads, key=lambda r: r[1])

        event_state = self.db.get_event_state()
        min_lap_sec = event_state.get("min_lap_seconds", 45.0)
        max_rest_sec = event_state.get("max_lap_rest_seconds")
        if max_rest_sec is None:
            max_rest_sec = 600.0

        effective_min_lap = min_lap_sec / max(0.1, self.speed_multiplier)
        effective_max_rest = (max_rest_sec / max(0.1, self.speed_multiplier)) if max_rest_sec > 0 else 0.0

        participant = self.db.get_participant_by_epc(epc)
        if not participant:
            logger.info(f"Tag read for unassigned EPC: {epc} (Peak RSSI: {peak_rssi:.1f} dBm)")
            if self.on_unassigned_tag:
                tag_data = {
                    "epc": epc,
                    "peak_rssi": round(peak_rssi, 1),
                    "timestamp": peak_time,
                }
                if asyncio.iscoroutinefunction(self.on_unassigned_tag):
                    await self.on_unassigned_tag(tag_data)
                else:
                    self.on_unassigned_tag(tag_data)
            return None

        bib = participant["bib_number"]
        name = participant["name"]

        # Only time crossings while the event is running. A crossing that finished just before
        # End or Pause was pressed can still be resolved here (bursts are flushed ~2s after the
        # last read; issue #91 for Pause).
        status = event_state.get("status")
        ended_in_time = status == "ended" and event_state.get("end_time") and peak_time <= event_state["end_time"]
        paused_in_time = status == "paused" and event_state.get("paused_at") and peak_time <= event_state["paused_at"]
        if status != "running" and not ended_in_time and not paused_in_time:
            logger.info(f"Event {status}: {name} (Bib {bib}) crossed mat; not timed.")
            return None

        race_start = event_state.get("start_time")
        if not race_start:
            logger.warning("Event active but no start_time set!")
            return None

        runner_start = participant.get("start_time")

        # -------------------------------------------------------------
        # CASE 1: Runner has NOT started yet -> Record CHIP START (Lap 0)
        # -------------------------------------------------------------
        if runner_start is None:
            # Race time since the gun, without any pause in between (issue #112)
            since_gun = peak_time - race_start - self.db.paused_seconds_between(race_start, peak_time)
            start_flag = None if force else self._missed_start_reason(since_gun, min_lap_sec)
            self.db.set_participant_start(bib, peak_time, flag_reason=start_flag)
            logger.info(f"🚀 CHIP START: Bib {bib} ({name}) started running at {peak_time:.2f} (RSSI: {peak_rssi:.1f} dBm)")
            if start_flag:
                logger.warning(f"⚠️ START REVIEW: Bib {bib} ({name}): {start_flag}")

            if self.on_runner_started:
                start_data = {
                    "bib_number": bib,
                    "name": name,
                    "start_time": peak_time,
                    "event_offset_seconds": round(peak_time - race_start, 1),
                    "peak_rssi": round(peak_rssi, 1),
                    "start_flag_reason": start_flag,
                }
                if asyncio.iscoroutinefunction(self.on_runner_started):
                    await self.on_runner_started(start_data)
                else:
                    self.on_runner_started(start_data)
            return "started"

        # -------------------------------------------------------------
        # CASE 2: Runner HAS started -> Evaluate Lap or Break Resume
        # -------------------------------------------------------------
        last_crossing = participant.get("last_crossing_time") or runner_start
        time_since_last = peak_time - last_crossing
        if not force and time_since_last <= 0:
            # This very crossing was already written (a retry after a failure that came later,
            # e.g. in a broadcast), or a retried one was overtaken by a later crossing or edit:
            # it isn't a crossing too soon after itself
            if time_since_last < 0:
                logger.warning(f"Retried crossing of Bib {bib} ({name}) at {peak_time:.1f} is older than "
                               f"their last crossing at {last_crossing:.1f}; not counted.")
            return "duplicate"

        # Debounce check (runner lingering near antenna). This applies to forced crossings too:
        # a lap time is always the real time since the last crossing, never an invented one.
        if time_since_last < effective_min_lap:
            logger.info(f"Debounce: Bib {bib} ({name}) {'forced crossing rejected' if force else 'read ignored'}. Only {time_since_last:.1f}s since last crossing (min {effective_min_lap:.1f}s).")
            if not force and self.on_crossing_too_soon:
                too_soon_data = {
                    "bib_number": bib,
                    "name": name,
                    "elapsed_seconds": round(time_since_last * self.speed_multiplier, 2),
                    "min_lap_seconds": round(min_lap_sec, 1),
                    "crossing_time": peak_time,
                    "peak_rssi": round(peak_rssi, 1),
                }
                if asyncio.iscoroutinefunction(self.on_crossing_too_soon):
                    await self.on_crossing_too_soon(too_soon_data)
                else:
                    self.on_crossing_too_soon(too_soon_data)
            return "debounced"

        # A runner on a manual break crossing the mat is back from it (issue #86): end the break
        # here rather than record a lap (or a second, overlapping auto-break) that includes it.
        closed = participant.get("maybe_on_break") and self.db.close_open_break(bib, peak_time, self.speed_multiplier)
        if closed:
            logger.info(f"☕ MANUAL BREAK ENDED: Bib {bib} ({name}) crossed the mat after {closed['duration_seconds']/60:.1f}m break.")
            await self._notify_resumed(bib, name, closed["duration_seconds"], peak_time)
            return "resumed"

        if not force:
            # Break / Rest detection: if enabled (max_rest_sec > 0) and time since last crossing exceeds threshold
            if max_rest_sec > 0 and time_since_last > effective_max_rest:
                break_duration = time_since_last * self.speed_multiplier
                self.db.record_break(
                    bib_number=bib,
                    start_time=last_crossing,
                    end_time=peak_time,
                    duration_seconds=round(break_duration, 2)
                )  # also moves the last crossing to peak_time
                logger.info(f"☕ BREAK DETECTED & RESUMED: Bib {bib} ({name}) resumed after {break_duration/60:.1f}m break!")

                await self._notify_resumed(bib, name, break_duration, peak_time)
                return "resumed"

            # Normal lap duration. (No floor: the min-lap debounce above already rejects short ones.)
            lap_duration = time_since_last * self.speed_multiplier
        else:
            # Forced crossing from the admin UI: skip break detection, but time it for real.
            lap_duration = time_since_last * self.speed_multiplier

        # Record completed lap
        await self._record_completed_lap(bib, name, peak_time, lap_duration, peak_rssi, manual_entry=force)
        return "lap"

    async def _notify_resumed(self, bib: int, name: str, break_duration: float, crossing_time: float):
        if self.on_runner_resumed:
            resumed_data = {
                "bib_number": bib,
                "name": name,
                "break_duration_seconds": round(break_duration, 2),
                "crossing_time": crossing_time,
            }
            if asyncio.iscoroutinefunction(self.on_runner_resumed):
                await self.on_runner_resumed(resumed_data)
            else:
                self.on_runner_resumed(resumed_data)

    async def _record_completed_lap(
        self,
        bib: int,
        name: str,
        peak_time: float,
        lap_duration: float,
        peak_rssi: float,
        manual_entry: bool = False,
        check_suspicious: bool = True,
    ) -> Dict[str, Any]:
        last_lap = self.db.get_last_lap(bib)
        lap_number = (last_lap["lap_number"] + 1) if last_lap else 1

        # Check for suspicious long lap (possible missed reading: >= 1.75x previous lap if >= 2 laps exist)
        flag_reason = None
        if check_suspicious and not manual_entry:
            prior_laps = self.db.get_laps_for_runner(bib)
            if len(prior_laps) >= 2:
                durations = sorted([l["lap_duration_seconds"] for l in prior_laps])
                median_dur = durations[len(durations) // 2]
                if lap_duration >= (1.75 * median_dur) and median_dur >= 20.0:
                    flag_reason = f"Missed reading suspected ({round(lap_duration, 1)}s vs median {round(median_dur, 1)}s)"
                    logger.warning(f"⚠️ SUSPICIOUS LAP: Bib {bib} ({name}) Lap {lap_number} flagged: {flag_reason}")

        lap_id = self.db.record_lap(
            bib_number=bib,
            lap_number=lap_number,
            crossing_time=peak_time,
            lap_duration_seconds=round(lap_duration, 2),
            peak_rssi=round(peak_rssi, 1),
            manual_entry=manual_entry,
            flag_reason=flag_reason,
        )  # also moves the last crossing to peak_time

        logger.info(f"🏁 LAP RECORDED: Bib {bib} ({name}) - Lap {lap_number} in {lap_duration:.2f}s (RSSI: {peak_rssi:.1f} dBm)")

        lap_data = {
            "id": lap_id,
            "bib_number": bib,
            "name": name,
            "lap_number": lap_number,
            "lap_duration_seconds": round(lap_duration, 2),
            "crossing_time": peak_time,
            "peak_rssi": round(peak_rssi, 1),
        }
        if self.on_lap_completed:
            if asyncio.iscoroutinefunction(self.on_lap_completed):
                await self.on_lap_completed(lap_data)
            else:
                self.on_lap_completed(lap_data)
        return lap_data

    async def accept_too_soon_crossing(
        self, bib_number: int, crossing_time: float, peak_rssi: float = -45.0
    ) -> Optional[Dict[str, Any]]:
        """Records a previously debounced (too-soon) crossing after operator confirmation."""
        event_state = self.db.get_event_state()
        if event_state.get("status") != "running":
            return None
        participant = self.db.get_participant_by_bib(bib_number)
        if not participant or participant.get("start_time") is None:
            return None
        last_crossing = participant.get("last_crossing_time") or participant["start_time"]
        if crossing_time <= last_crossing:
            return None
        # Back from a manual break sooner than a lap (issue #86): the confirmed crossing ends
        # the break instead of being a lap that includes it
        closed = self.db.close_open_break(bib_number, crossing_time, self.speed_multiplier)
        if closed:
            await self._notify_resumed(bib_number, participant["name"], closed["duration_seconds"], crossing_time)
            return {"bib_number": bib_number, "resumed": True,
                    "break_duration_seconds": closed["duration_seconds"], "crossing_time": crossing_time}
        lap_duration = (crossing_time - last_crossing) * self.speed_multiplier
        return await self._record_completed_lap(
            bib_number,
            participant["name"],
            crossing_time,
            lap_duration,
            peak_rssi,
            manual_entry=True,
            check_suspicious=False,
        )

    async def trigger_manual_crossing(self, bib_number: int, force: bool = True) -> Optional[Dict[str, Any]]:
        """Allows admin UI to manually trigger an immediate crossing for testing or missed bib.

        Returns None if the bib doesn't exist, otherwise {"participant": ..., "outcome": ...}
        where outcome is the value returned by _resolve_crossing.
        """
        participant = self.db.get_participant_by_bib(bib_number)
        if not participant:
            return None
        epc = participant["epc"]
        now = clock.now()
        reads = [(now, -42.0)]
        outcome = await self._resolve_crossing(epc, reads, force=force)
        return {"participant": participant, "outcome": outcome}
