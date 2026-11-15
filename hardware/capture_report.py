#!/usr/bin/env python3
"""
Summarises a raw R200 capture (RAW_CAPTURE_PATH, issue #51), for soak tests (issue #5).

  python -m hardware.capture_report data/soak-20261115T2051.log [--gap 2]

Reports, per inventory arm (connect / routine / silence re-arm): how long it ran, how many
responses the module sent (one per poll when at most one tag is in view) and tag reads,
how long the reader took to answer after a re-arm, and every gap in received data longer
than --gap seconds. A run that ends with "arm silence" after a long gap means the module
stopped scanning on its own.
"""
import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from hardware.frame_capture import iter_capture
from hardware.r200_protocol import extract_frames, parse_inventory_response


@dataclass
class Session:
    reason: str
    armed_at: float
    responses: int = 0          # every frame the module sent: tag reads + "no tag" answers
    tag_reads: int = 0
    first_frame_at: Optional[float] = None
    last_frame_at: Optional[float] = None

    @property
    def answer_latency(self) -> Optional[float]:
        """Seconds from arming to the first frame back."""
        return None if self.first_frame_at is None else self.first_frame_at - self.armed_at


@dataclass
class Report:
    start: Optional[float] = None
    end: Optional[float] = None
    sessions: List[Session] = field(default_factory=list)
    gaps: List[tuple] = field(default_factory=list)  # (from, to) monotonic seconds
    epcs: Counter = field(default_factory=Counter)
    notes: List[tuple] = field(default_factory=list)  # (time, text) other than arms
    leftover_bytes: int = 0

    @property
    def duration(self) -> float:
        return 0.0 if self.start is None else self.end - self.start


def summarize(lines: Iterable[str], gap: float = 2.0) -> Report:
    report = Report()
    buffer = bytearray()
    last_rx: Optional[float] = None
    session: Optional[Session] = None
    for kind, t, text in iter_capture(lines):
        if report.start is None:
            report.start = t
        report.end = t
        if kind == "#":
            if text.startswith("arm "):
                session = Session(reason=text[4:], armed_at=t)
                report.sessions.append(session)
            else:
                report.notes.append((t, text))
            continue
        if kind != "rx":
            continue
        if last_rx is not None and t - last_rx > gap:
            report.gaps.append((last_rx, t))
        last_rx = t
        buffer.extend(bytes.fromhex(text))
        frames, buffer = extract_frames(buffer)
        if not frames or session is None:
            continue
        if session.first_frame_at is None:
            session.first_frame_at = t
        session.last_frame_at = t
        session.responses += len(frames)
        for frame in frames:
            parsed = parse_inventory_response(frame)
            if parsed:
                session.tag_reads += 1
                report.epcs[parsed[0]] += 1
    report.leftover_bytes = len(buffer)
    # Data still flowing at the end of the file isn't a gap, but a silent tail is
    if last_rx is not None and report.end - last_rx > gap:
        report.gaps.append((last_rx, report.end))
    return report


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600:d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def format_report(report: Report) -> str:
    out = [f"Capture: {_hms(report.duration)} ({report.duration:.0f} s), "
           f"{len(report.sessions)} arm(s), {sum(s.responses for s in report.sessions)} responses, "
           f"{sum(report.epcs.values())} tag reads from {len(report.epcs)} tag(s)"]
    out.append("")
    out.append(f"  {'#':>3}  {'reason':8}  {'start':>8}  {'ran':>8}  {'responses':>9}  {'per s':>6}  "
               f"{'tags':>6}  {'1st answer':>10}")
    for i, s in enumerate(report.sessions):
        end = report.sessions[i + 1].armed_at if i + 1 < len(report.sessions) else report.end
        ran = end - s.armed_at
        rate = s.responses / ran if ran > 0 else 0.0
        latency = "none" if s.answer_latency is None else f"{s.answer_latency * 1000:.0f} ms"
        out.append(f"  {i + 1:>3}  {s.reason:8}  {_hms(s.armed_at - report.start):>8}  {_hms(ran):>8}  "
                   f"{s.responses:>9}  {rate:>6.1f}  {s.tag_reads:>6}  {latency:>10}")
    out.append("")
    if report.gaps:
        out.append(f"Gaps in received data ({len(report.gaps)}):")
        for a, b in report.gaps:
            out.append(f"  at {_hms(a - report.start)}: {b - a:.1f} s")
    else:
        out.append("No gaps in received data.")
    if report.epcs:
        out.append("")
        out.append("Tag reads:")
        for epc, n in report.epcs.most_common():
            out.append(f"  {epc}  {n}")
    for t, text in report.notes:
        if text not in ("capture started",):
            out.append(f"Note at {_hms(t - report.start)}: {text}")
    if report.leftover_bytes:
        out.append(f"{report.leftover_bytes} trailing bytes didn't form a complete frame")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarise a raw R200 capture")
    parser.add_argument("path")
    parser.add_argument("--gap", type=float, default=2.0, help="report silences longer than this (s)")
    args = parser.parse_args(argv)
    with open(args.path, encoding="ascii") as f:
        print(format_report(summarize(f, gap=args.gap)))


if __name__ == "__main__":
    sys.exit(main())
