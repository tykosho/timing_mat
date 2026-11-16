"""Browser smoke tests: the real app in headless Chromium (issue #38).

Starts uvicorn with the mock reader and an admin PIN, then checks that every page's
JavaScript runs, login works, and live updates arrive. Skipped when Playwright or a
browser isn't installed; CI's `browser` job installs both:

    pip install -r requirements.txt -r requirements-browser.txt
    python -m playwright install chromium
    python -B -m unittest -v tests.test_browser
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # not installed: the whole module is skipped
    sync_playwright = None

PIN = "482913"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_server = None
_tmp = None
BASE = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_json(path: str):
    with urllib.request.urlopen(BASE + path, timeout=5) as res:
        return json.load(res)


def setUpModule():
    global _server, _tmp, BASE
    if sync_playwright is None:
        raise unittest.SkipTest("playwright not installed (pip install -r requirements-browser.txt)")
    _tmp = tempfile.TemporaryDirectory()
    port = _free_port()
    BASE = f"http://127.0.0.1:{port}"
    env = dict(os.environ, MOCK_HARDWARE="true", SEED_DEMO="true", ADMIN_PIN=PIN,
               DB_PATH=os.path.join(_tmp.name, "race.db"), PYTHONDONTWRITEBYTECODE="1")
    log = open(os.path.join(_tmp.name, "server.log"), "w")
    _server = subprocess.Popen([sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1",
                                "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 30
    while True:
        try:
            _get_json("/api/event")
            break
        except Exception:
            if _server.poll() is not None or time.time() > deadline:
                log.close()
                with open(os.path.join(_tmp.name, "server.log")) as f:
                    raise RuntimeError("app didn't start:\n" + f.read()[-3000:])
            time.sleep(0.2)


def tearDownModule():
    if _server:
        _server.terminate()
        try:
            _server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server.kill()
    if _tmp:
        _tmp.cleanup()


def _launch(p):
    """Playwright's bundled Chromium, or an installed Chrome as a fallback (e.g. on a Mac)."""
    for kwargs in ({}, {"channel": "chrome"}):
        try:
            return p.chromium.launch(**kwargs)
        except Exception:
            continue
    raise unittest.SkipTest("no browser: run `python -m playwright install chromium`")


def _clock_seconds(text: str) -> int:
    h, m, s = (int(x) for x in text.strip().split(":"))
    return h * 3600 + m * 60 + s


class BrowserTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = _launch(cls.pw)
        # A running event with the simulator sped up, so there's live data to see
        admin = cls.admin_context()
        admin.request.post(BASE + "/api/event/start")
        admin.request.post(BASE + "/api/simulator/speed", data={"speed": 20.0})
        admin.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    @classmethod
    def admin_context(cls, **kwargs):
        ctx = cls.browser.new_context(**kwargs)
        res = ctx.request.post(BASE + "/login", form={"pin": PIN, "next": "/admin"}, max_redirects=0)
        assert res.status == 303, res.status
        return ctx

    def open(self, ctx, path):
        """Opens a page and collects uncaught JS errors and console errors."""
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        # Failed requests are reported with their URL (the console only says "Failed to load resource")
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        # (Chrome sometimes asks for /favicon.ico on its own; the app doesn't have one.)
        page.on("response", lambda r: errors.append(f"HTTP {r.status}: {r.url}")
                if r.status >= 400 and not r.url.endswith("/favicon.ico") else None)
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        return page, errors


class PagesLoadTest(BrowserTestCase):
    def test_public_pages_have_no_js_errors(self):
        ctx = self.browser.new_context()
        for path in ["/leaderboard", "/runner/101", "/login"]:
            with self.subTest(path=path):
                page, errors = self.open(ctx, path)
                self.assertEqual(errors, [], path)
                self.assertTrue(page.url.endswith(path), page.url)
        ctx.close()

    def test_admin_pages_have_no_js_errors(self):
        ctx = self.admin_context()
        for path in ["/admin", "/prepare", "/crossings", "/replay", "/simulator", "/join", "/runner/101"]:
            with self.subTest(path=path):
                page, errors = self.open(ctx, path)
                self.assertEqual(errors, [], path)
                self.assertTrue(page.url.endswith(path), f"{path} -> {page.url}")
        ctx.close()


class LoginTest(BrowserTestCase):
    def test_login_flow(self):
        ctx = self.browser.new_context()
        page = ctx.new_page()
        page.goto(BASE + "/admin")
        self.assertIn("/login?next=/admin", page.url)
        page.fill("input[name=pin]", "000000")
        page.click("button[type=submit]")
        self.assertTrue(page.get_by_text("Wrong PIN").is_visible())
        page.fill("input[name=pin]", PIN)
        page.click("button[type=submit]")
        page.wait_for_url(BASE + "/admin")
        self.assertTrue(page.locator('form[action="/logout"]').is_visible())
        ctx.close()


class LiveUpdatesTest(BrowserTestCase):
    def test_leaderboard_lap_count_updates_without_reload(self):
        ctx = self.browser.new_context()
        page, errors = self.open(ctx, "/leaderboard")
        page.wait_for_function(
            "() => parseInt(document.getElementById('lapCount').innerText) > 0", timeout=60_000)
        self.assertEqual(errors, [])
        ctx.close()

    def test_lap_messages_carry_one_row_and_the_page_stays_in_order(self):
        """#71: laps send only the runner's row; the merged board matches the server's."""
        ctx = self.browser.new_context()
        frames = []
        page = ctx.new_page()
        page.on("websocket", lambda ws: ws.on("framereceived", lambda p: frames.append(p)))
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/leaderboard")

        def laps():
            return [json.loads(f) for f in frames if '"lap_completed"' in f]

        deadline = time.time() + 60
        while len(laps()) < 3 and time.time() < deadline:
            page.wait_for_timeout(250)
        self.assertGreaterEqual(len(laps()), 3, "simulated laps arrive")
        for msg in laps():
            self.assertNotIn("leaderboard", msg)
            self.assertEqual(msg["row"]["bib_number"], msg["lap"]["bib_number"])

        def shown_vs_server():
            return page.evaluate("""async () => {
                const server = await (await fetch('/api/leaderboard')).json();
                const key = b => b.map(r => r.bib_number + ':' + r.total_laps).join(',');
                return [key(liveBoard), key(server)];
            }""")
        same = False
        for _ in range(40):  # a lap can land between the two reads: try again
            shown, server = shown_vs_server()
            if shown == server:
                same = True
                break
            page.wait_for_timeout(100)
        self.assertTrue(same, f"page {shown} vs server {server}")
        self.assertEqual(errors, [])
        ctx.close()

    def test_merge_keeps_the_server_order(self):
        ctx = self.browser.new_context()
        page, errors = self.open(ctx, "/leaderboard")
        result = page.evaluate("""() => {
            const board = [
                {bib_number: 1, total_laps: 3, last_crossing_time_laps: 100},
                {bib_number: 2, total_laps: 3, last_crossing_time_laps: 120},
                {bib_number: 3, total_laps: 2, last_crossing_time_laps: 90},
                {bib_number: 4, total_laps: 0, last_crossing_time_laps: null},
            ];
            const lapFor3 = mergeLeaderboardRow(board, {bib_number: 3, total_laps: 3, last_crossing_time_laps: 130});
            const lapFor2 = mergeLeaderboardRow(board, {bib_number: 2, total_laps: 4, last_crossing_time_laps: 140});
            const newcomer = mergeLeaderboardRow(board, {bib_number: 5, total_laps: 0, last_crossing_time_laps: null});
            const full = boardFromMessage(board, {leaderboard: [{bib_number: 9}], row: {bib_number: 1}});
            return [lapFor3, lapFor2, newcomer, full, board].map(b => b.map(r => r.bib_number));
        }""")
        self.assertEqual(result, [[1, 2, 3, 4], [2, 1, 3, 4], [1, 2, 3, 4, 5], [9], [1, 2, 3, 4]])
        self.assertEqual(errors, [])
        ctx.close()

    def test_race_timer_is_right_when_the_device_clock_is_off(self):
        """#2: the TV/phone clock 2 h behind the Pi must not change the race timer."""
        ctx = self.browser.new_context()
        ctx.add_init_script("(() => { const real = Date.now; Date.now = () => real() - 2 * 3600 * 1000; })()")
        page, errors = self.open(ctx, "/leaderboard")
        page.wait_for_timeout(1500)
        shown = _clock_seconds(page.inner_text("#raceTimer"))
        server = _get_json("/api/event")["elapsed_seconds"]
        self.assertAlmostEqual(shown, server, delta=3)
        self.assertEqual(errors, [])
        ctx.close()

    def test_reader_health_strip_updates_live(self):
        """#48: the admin page's reader strip shows the simulator reading, via the WebSocket."""
        ctx = self.admin_context()
        page, errors = self.open(ctx, "/admin")
        page.wait_for_function(
            "() => document.getElementById('readerHealth').dataset.state === 'ok'"
            " && /Simulator: reading .* [1-9][0-9.]* reads\\/s/.test(document.getElementById('readerHealthText').innerText)",
            timeout=30_000)
        self.assertEqual(errors, [])
        ctx.close()

    def test_crossings_cumulative_laps_banner(self):
        """#49: total laps banner sits between the top bar and the feed, and counts up live."""
        ctx = self.admin_context()
        page, errors = self.open(ctx, "/crossings")
        order = page.evaluate("""() => {
            const header = document.querySelector('header'), banner = document.getElementById('cumulativeBanner'),
                  feed = document.getElementById('crossingFeed');
            const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
            return after(header, banner) && after(banner, feed);
        }""")
        self.assertTrue(order, "banner must be below the top bar and above the crossing feed")

        def shown():
            return int(page.inner_text("#cumulativeLaps").replace(",", ""))

        start = shown()
        page.wait_for_function(f"() => parseInt(document.getElementById('cumulativeLaps').innerText.replace(/,/g, '')) > {start}",
                               timeout=60_000)
        # Matches the server's own total (allowing for a lap landing in between)
        server_total = sum(r["total_laps"] for r in _get_json("/api/leaderboard"))
        self.assertLessEqual(abs(shown() - server_total), 2)
        self.assertIn("km", page.inner_text("#cumulativeDistance"))
        self.assertEqual(errors, [])
        ctx.close()

    def test_raw_tag_reads_reach_admin_only(self):
        """#12: tag_detected goes to the logged-in admin page, not to public screens."""
        counts = {"admin": 0, "public": 0}

        def watch(page, who):
            def on_ws(ws):
                def frame(payload):
                    if '"tag_detected"' in str(payload):
                        counts[who] += 1
                ws.on("framereceived", frame)
            page.on("websocket", on_ws)

        admin_ctx, public_ctx = self.admin_context(), self.browser.new_context()
        admin_page, public_page = admin_ctx.new_page(), public_ctx.new_page()
        watch(admin_page, "admin")
        watch(public_page, "public")
        admin_page.goto(BASE + "/admin")
        public_page.goto(BASE + "/leaderboard")
        deadline = time.time() + 30
        while counts["admin"] == 0 and time.time() < deadline:
            admin_page.wait_for_timeout(250)
        public_page.wait_for_timeout(1000)
        self.assertGreater(counts["admin"], 0)
        self.assertEqual(counts["public"], 0)
        admin_ctx.close()
        public_ctx.close()


if __name__ == "__main__":
    unittest.main()
