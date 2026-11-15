"""Soak-test summary of a raw R200 capture (issue #5)."""
import os
import unittest

from hardware.capture_report import format_report, main, summarize
from tests.test_serial_reader import EPC, tag_frame

NO_TAG = "AA01FF00011516DD"
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "r200_capture_2026-11-15.log")


def line(t, kind, text):
    return f"2026-11-15T00:00:00.000Z {t:.3f} {kind} {text}"


class SummarizeTest(unittest.TestCase):
    def test_module_stopping_then_silence_rearm(self):
        lines = [line(0.0, "#", "capture started"), line(0.0, "#", "arm connect"),
                 line(0.0, "tx", "AA0027000322FFFF4ADD")]
        lines += [line(0.1 + i * 0.1, "rx", NO_TAG * 3) for i in range(10)]  # 30 responses
        lines += [line(1.05, "rx", tag_frame().hex().upper())]
        # module goes quiet at ~1.05 s; watchdog re-arms 30 s later and it answers again
        lines += [line(31.1, "#", "arm silence"), line(31.2, "rx", NO_TAG)]
        r = summarize(lines)
        self.assertEqual([s.reason for s in r.sessions], ["connect", "silence"])
        self.assertEqual(r.sessions[0].responses, 31)
        self.assertEqual(r.sessions[0].tag_reads, 1)
        self.assertAlmostEqual(r.sessions[0].answer_latency, 0.1)
        self.assertAlmostEqual(r.sessions[1].answer_latency, 0.1, places=3)
        self.assertEqual(len(r.gaps), 1)
        self.assertAlmostEqual(r.gaps[0][1] - r.gaps[0][0], 30.15, places=2)
        self.assertEqual(r.epcs[EPC], 1)
        text = format_report(r)
        self.assertIn("silence", text)
        self.assertIn("30.1 s", text)

    def test_frame_split_across_chunks_counts_once(self):
        frame = tag_frame().hex().upper()
        r = summarize([line(0, "#", "arm connect"), line(0.1, "rx", frame[:10]),
                       line(0.2, "rx", frame[10:])])
        self.assertEqual(r.sessions[0].responses, 1)
        self.assertEqual(r.leftover_bytes, 0)

    def test_silent_tail_is_a_gap(self):
        r = summarize([line(0, "#", "arm connect"), line(0.1, "rx", NO_TAG),
                       line(10.0, "#", "reader stopped")])
        self.assertEqual(len(r.gaps), 1)

    def test_real_capture(self):
        with open(FIXTURE) as f:
            r = summarize(f)
        self.assertEqual(len(r.sessions), 1)
        self.assertEqual(r.sessions[0].responses, 2827)
        self.assertEqual(sum(r.epcs.values()), 8)
        self.assertEqual(r.gaps, [])

    def test_cli(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        with redirect_stdout(out):
            main([FIXTURE])
        self.assertIn("2827 responses", out.getvalue())


if __name__ == "__main__":
    unittest.main()
