"""The soak test (issue #69): a short race on the simulated clock runs in CI; the long ones by hand."""
import unittest

from tools.soak import check_laps, passes_seen, run_fast


class SoakCheckTest(unittest.TestCase):
    def test_passes_seen_groups_reads_per_pass(self):
        self.assertEqual(passes_seen([]), 0)
        self.assertEqual(passes_seen([100.0, 100.3, 101.1, 190.0, 190.5, 281.0]), 3)

    def test_reader_misses_are_not_app_errors(self):
        crossings = {"A": 5, "B": 5}
        seen = {"A": 5, "B": 4}         # B's start was never read
        laps = {"A": 4, "B": 3}         # right, given what the app saw
        app_errors, misses = check_laps(crossings, seen, laps)
        self.assertEqual(app_errors, [])
        self.assertEqual(len(misses), 1)
        self.assertIn("1 of 5 passes never read", misses[0])
        app_errors, _ = check_laps(crossings, seen, {"A": 5, "B": 3})  # a double count
        self.assertEqual(len(app_errors), 1)


class ShortSoakTest(unittest.TestCase):
    def test_quarter_hour_race_with_packs_and_faults(self):
        lines = []
        result = run_fast(runners=24, hours=0.25, seed=3, report=lines.append, track_memory=False)
        self.assertTrue(result["ok"], result["app_errors"][:5])
        self.assertEqual(result["unknown_tags"], 0)
        self.assertGreater(result["laps"], 200)
        self.assertGreater(result["corrupted_frames"], 100)
        self.assertGreaterEqual(result["rows"][-1]["rearms"], 2, "routine re-arm every 300 s")

    def test_a_start_missed_in_the_crowd_is_flagged(self):
        """#72: seed 10 has one runner whose tag isn't read at the gun; the app flags them."""
        result = run_fast(runners=60, hours=0.05, seed=10, report=lambda *_: None, track_memory=False)
        self.assertEqual(result["missed_starts"], 1)
        self.assertEqual(result["flagged_starts"], 1)
        self.assertEqual(result["missed_starts_not_flagged"], [])
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
