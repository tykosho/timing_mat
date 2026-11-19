"""Pi Wi-Fi hotspot: deploy/pi/hotspot.sh (dry run) and the printable /join page (issue #42)."""
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from web.join import qr_svg, site_url, wifi_qr_payload

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "pi", "hotspot.sh")
SIM_SCRIPT = os.path.join(ROOT, "deploy", "pi", "hotspot-sim.sh")


@unittest.skipIf(shutil.which("bash") is None, "bash not available")
class HotspotScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = os.path.join(self.tmp.name, ".env")

    def run_script(self, *args, env_file_text=None, **env):
        if env_file_text is not None:
            with open(self.env_file, "w") as f:
                f.write(env_file_text)
        full_env = {"PATH": os.environ["PATH"], "DRY_RUN": "1", "HOTSPOT_ENV_FILE": self.env_file}
        full_env.update(env)
        return subprocess.run(["bash", SCRIPT, *args], env=full_env, capture_output=True, text=True, timeout=30)

    @staticmethod
    def commands(stdout):
        """The commands a dry run would execute, as argument lists."""
        return [shlex.split(line[2:]) for line in stdout.splitlines() if line.startswith("+ ") and "write " not in line]

    def nmcli_add(self, stdout):
        return next(c for c in self.commands(stdout) if c[:3] == ["nmcli", "connection", "add"])

    @staticmethod
    def setting(cmd, key):
        return cmd[cmd.index(key) + 1]

    def test_setup_creates_a_wpa2_access_point(self):
        res = self.run_script("setup", HOTSPOT_PASSWORD="longenough", HOTSPOT_COUNTRY="US")
        self.assertEqual(res.returncode, 0, res.stderr)
        add = self.nmcli_add(res.stdout)
        for key, value in [("ssid", "TimingMat"), ("802-11-wireless.mode", "ap"), ("802-11-wireless.channel", "6"),
                           ("ipv4.method", "shared"), ("ipv4.addresses", "10.42.0.1/24"),
                           ("wifi-sec.key-mgmt", "wpa-psk"), ("wifi-sec.psk", "longenough"),
                           ("connection.autoconnect-priority", "100"), ("autoconnect", "yes")]:
            self.assertEqual(self.setting(add, key), value, key)
        cmds = self.commands(res.stdout)
        self.assertIn(["raspi-config", "nonint", "do_wifi_country", "US"], cmds)
        self.assertIn(["rfkill", "unblock", "wifi"], cmds)
        self.assertEqual(cmds[-1], ["nmcli", "connection", "up", "race-hotspot"])
        self.assertIn("address=/race.lan/10.42.0.1", res.stdout)
        self.assertIn("http://race.lan", res.stdout)

    def test_settings_from_env_file_are_passed_verbatim(self):
        evil = os.path.join(self.tmp.name, "pwned")
        res = self.run_script("setup", env_file_text=(
            "ADMIN_PIN=482913\n"
            'HOTSPOT_PASSWORD="run fast; lap 42"\n'
            "HOTSPOT_SSID='Track Day'\n"
            "HOTSPOT_COUNTRY=gb\n"
            "HOTSPOT_HOSTNAME=timer.lan\n"
            "# HOTSPOT_CHANNEL=11\n"
            f"HOTSPOT_EVIL=$(touch {evil})\n"
        ))
        self.assertEqual(res.returncode, 0, res.stderr)
        add = self.nmcli_add(res.stdout)
        self.assertEqual(self.setting(add, "wifi-sec.psk"), "run fast; lap 42")
        self.assertEqual(self.setting(add, "ssid"), "Track Day")
        self.assertEqual(self.setting(add, "802-11-wireless.channel"), "6", "commented-out line ignored")
        self.assertIn(["raspi-config", "nonint", "do_wifi_country", "GB"], self.commands(res.stdout))
        self.assertIn("address=/timer.lan/10.42.0.1", res.stdout)
        self.assertFalse(os.path.exists(evil), ".env must be read, not executed")

    def test_environment_overrides_env_file(self):
        res = self.run_script("setup", env_file_text="HOTSPOT_PASSWORD=fromthefile\nHOTSPOT_COUNTRY=US\n",
                              HOTSPOT_PASSWORD="fromtheenv1")
        self.assertEqual(self.setting(self.nmcli_add(res.stdout), "wifi-sec.psk"), "fromtheenv1")

    def test_invalid_settings_are_rejected(self):
        ok = {"HOTSPOT_PASSWORD": "longenough", "HOTSPOT_COUNTRY": "US"}
        cases = [
            ({"HOTSPOT_PASSWORD": ""}, "Set HOTSPOT_PASSWORD"),
            ({"HOTSPOT_PASSWORD": "short"}, "8-63 characters"),
            ({"HOTSPOT_PASSWORD": "x" * 64}, "8-63 characters"),
            ({"HOTSPOT_COUNTRY": ""}, "Set HOTSPOT_COUNTRY"),
            ({"HOTSPOT_COUNTRY": "USA"}, "two capital letters"),
            ({"HOTSPOT_CHANNEL": "13"}, "1 to 11"),
            ({"HOTSPOT_HOSTNAME": "race lan"}, "HOTSPOT_HOSTNAME"),
            ({"HOTSPOT_SSID": "x" * 33}, "1-32 characters"),
            ({"HOTSPOT_ADDRESS": "10.42.0.1"}, "HOTSPOT_ADDRESS"),
        ]
        for override, message in cases:
            with self.subTest(override=override):
                res = self.run_script("setup", **{**ok, **override})
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(message, res.stderr)
                self.assertNotIn("nmcli", res.stdout, "nothing runs when settings are invalid")

    def test_status_and_remove(self):
        status = self.run_script("status")
        self.assertIn("connection show race-hotspot", status.stdout)
        remove = self.run_script("remove")
        self.assertIn(["nmcli", "connection", "delete", "race-hotspot"], self.commands(remove.stdout))
        self.assertIn(["rm", "-f", "/etc/NetworkManager/dnsmasq-shared.d/race-hotspot.conf"], self.commands(remove.stdout))

    def test_usage(self):
        res = self.run_script()
        self.assertEqual(res.returncode, 2)
        self.assertIn("sudo deploy/pi/hotspot.sh setup", res.stdout)

    def test_real_run_needs_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        res = self.run_script("setup", DRY_RUN="0", HOTSPOT_PASSWORD="longenough", HOTSPOT_COUNTRY="US")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Run with sudo", res.stderr)



@unittest.skipIf(shutil.which("bash") is None, "bash not available")
class HotspotSimScriptTest(unittest.TestCase):
    """deploy/pi/hotspot-sim.sh needs root and a Pi kernel to really run; check what we can here."""
    def run_sim(self, *args):
        return subprocess.run(["bash", SIM_SCRIPT, *args], env={"PATH": os.environ["PATH"]},
                              capture_output=True, text=True, timeout=30)

    def test_parses(self):
        self.assertEqual(subprocess.run(["bash", "-n", SIM_SCRIPT]).returncode, 0)

    def test_usage(self):
        res = self.run_sim()
        self.assertEqual(res.returncode, 2)
        self.assertIn("simulated phones", res.stdout)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "needs a non-root user")
    def test_needs_root(self):
        for cmd in ("run", "clean"):
            res = self.run_sim(cmd)
            self.assertEqual(res.returncode, 1)
            self.assertIn("sudo", res.stderr)

class JoinHelpersTest(unittest.TestCase):
    def test_wifi_payload(self):
        self.assertEqual(wifi_qr_payload("TimingMat", "longenough"), "WIFI:T:WPA;S:TimingMat;P:longenough;;")
        self.assertEqual(wifi_qr_payload("Open", ""), "WIFI:T:nopass;S:Open;;")

    def test_wifi_payload_escapes_separators(self):
        self.assertEqual(wifi_qr_payload('a;b,c', 'p:w\\d"x'), 'WIFI:T:WPA;S:a\\;b\\,c;P:p\\:w\\\\d\\"x;;')

    def test_qr_svg_scales(self):
        svg = str(qr_svg("http://race.lan/leaderboard"))
        self.assertTrue(svg.startswith('<svg role="img" '))
        self.assertNotIn('width="', svg.split(">", 1)[0])
        self.assertIn("viewBox", svg)

    def test_site_url(self):
        self.assertEqual(site_url("race.lan", "http://10.42.0.1:8000/"), "http://race.lan")
        self.assertEqual(site_url("", "http://10.42.0.1:8000/"), "http://10.42.0.1:8000")


class JoinPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "join_import.db"))
        os.environ.setdefault("MOCK_HARDWARE", "true")
        from core.db import Database
        from web import app as web_app
        cls.app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = mock.patch.object(web_app, "db", Database(os.path.join(cls.tmp.name, "join.db")))
        cls.db_patch.start()
        cls.client = TestClient(web_app.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def get(self, **env):
        with mock.patch.dict(os.environ, env), mock.patch.object(self.app.admin_auth, "pin", None):
            return self.client.get("/join")

    def test_shows_both_codes_when_the_hotspot_is_configured(self):
        res = self.get(HOTSPOT_SSID="Track <b>Day</b>", HOTSPOT_PASSWORD="run fast; lap 42", HOTSPOT_HOSTNAME="race.lan")
        self.assertEqual(res.status_code, 200)
        self.assertIn('id="join-wifi"', res.text)
        self.assertIn("Track &lt;b&gt;Day&lt;/b&gt;", res.text, "SSID is escaped")
        self.assertIn("run fast; lap 42", res.text)
        self.assertIn("<code>http://race.lan</code>", res.text)
        self.assertEqual(res.text.count("<svg"), 2)

    def test_without_hotspot_only_the_leaderboard_code(self):
        res = self.get(HOTSPOT_SSID="", HOTSPOT_PASSWORD="", HOTSPOT_HOSTNAME="")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('id="join-wifi"', res.text)
        self.assertIn("isn't configured", res.text)
        self.assertIn("<code>http://testserver</code>", res.text)
        self.assertEqual(res.text.count("<svg"), 1)

    def test_admin_only(self):
        from web.auth import AdminAuth
        with mock.patch.object(self.app, "admin_auth", AdminAuth("482913")):
            res = self.client.get("/join")
        self.assertEqual(res.status_code, 303)
        self.assertIn("/login?next=/join", res.headers["location"])


if __name__ == "__main__":
    unittest.main()
