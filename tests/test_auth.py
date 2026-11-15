"""Admin PIN protection (issue #3)."""
import os
import re
import tempfile
import unittest
from unittest import mock

# Same as test_api.py, in case this module is the first to import web.app.
_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("DB_PATH", os.path.join(_tmp.name, "auth_import.db"))
os.environ.setdefault("MOCK_HARDWARE", "true")
os.environ.setdefault("SEED_DEMO", "false")

from core.db import Database  # noqa: E402
from web.auth import AdminAuth, COOKIE_NAME, MAX_FAILURES, LOCKOUT_SECONDS, is_public, resolve_pin, safe_next  # noqa: E402

PIN = "482913"
EPC = "E28011700000020101010001"


class AuthUnitTest(unittest.TestCase):
    def test_public_routes(self):
        for method, path in [("GET", "/leaderboard"), ("GET", "/runner/101"), ("HEAD", "/leaderboard"),
                             ("GET", "/api/leaderboard"), ("GET", "/api/event"), ("GET", "/static/x.css"),
                             ("GET", "/login"), ("POST", "/login"), ("POST", "/logout"), ("GET", "/")]:
            self.assertTrue(is_public(method, path), f"{method} {path}")
        for method, path in [("GET", "/admin"), ("GET", "/prepare"), ("POST", "/api/event/start"),
                             ("POST", "/api/event/reset"), ("GET", "/api/participants"),
                             ("GET", "/api/events/1/results.csv"), ("DELETE", "/api/laps/1"),
                             ("GET", "/api/events"), ("GET", "/runner/101/edit"), ("GET", "/leaderboardx")]:
            self.assertFalse(is_public(method, path), f"{method} {path}")

    def test_safe_next(self):
        self.assertEqual(safe_next("/prepare?x=1"), "/prepare?x=1")
        for bad in ["//evil.example", "https://evil.example", "/\\evil.example", "", None, "admin"]:
            self.assertEqual(safe_next(bad), "/admin", repr(bad))

    def test_lockout_after_repeated_failures(self):
        auth = AdminAuth(PIN)
        for _ in range(MAX_FAILURES):
            self.assertFalse(auth.check_pin("phone", "000000", now=100.0))
        # Locked: even the right PIN is refused, but only for this client
        self.assertFalse(auth.check_pin("phone", PIN, now=101.0))
        self.assertTrue(auth.check_pin("laptop", PIN, now=101.0))
        # Lockout expires
        self.assertTrue(auth.check_pin("phone", PIN, now=100.0 + LOCKOUT_SECONDS + 1))

    def test_correct_pin_clears_failures(self):
        auth = AdminAuth(PIN)
        for _ in range(MAX_FAILURES - 1):
            auth.check_pin("phone", "1", now=100.0)
        self.assertTrue(auth.check_pin("phone", PIN, now=100.0))
        self.assertFalse(auth.locked_out("phone", now=100.0))

    def test_token_depends_on_pin(self):
        self.assertEqual(AdminAuth(PIN).session_token(), AdminAuth(PIN).session_token())
        self.assertNotEqual(AdminAuth(PIN).session_token(), AdminAuth("111111").session_token())

    def test_resolve_pin(self):
        self.assertEqual(resolve_pin(" 1234 ", use_mock=False), "1234")
        self.assertEqual(resolve_pin(482913, use_mock=True), "482913")  # YAML may give an int
        self.assertIsNone(resolve_pin(None, use_mock=True))
        self.assertIsNone(resolve_pin("", use_mock=True))
        generated = resolve_pin(None, use_mock=False)  # real reader: never run open
        self.assertRegex(generated, r"^\d{6}$")


class AuthApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            raise unittest.SkipTest(f"fastapi TestClient unavailable: {e}")
        from web import app as web_app
        cls.web_app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(cls.tmp.name, "auth_api.db"))
        db.add_participant(101, "Alex", EPC)
        cls.patches = [mock.patch.object(web_app, "db", db),
                       mock.patch.object(web_app.lap_engine, "db", db)]
        for p in cls.patches:
            p.start()
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def make_client(self):
        return self.TestClient(self.web_app.app, follow_redirects=False)

    def setUp(self):
        # Fresh AdminAuth per test, so lockout state doesn't leak between tests
        self.auth_patch = mock.patch.object(self.web_app, "admin_auth", AdminAuth(PIN))
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.anon = self.make_client()

    def login(self, client, pin=PIN, next_url="/admin"):
        return client.post("/login", data={"pin": pin, "next": next_url})

    def admin_client(self):
        client = self.make_client()
        self.assertEqual(self.login(client).status_code, 303)
        return client

    def test_public_pages_work_without_login(self):
        for path in ["/leaderboard", "/runner/101", "/api/leaderboard", "/api/event", "/login"]:
            self.assertEqual(self.anon.get(path).status_code, 200, path)

    def test_admin_pages_redirect_to_login(self):
        res = self.anon.get("/prepare")
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/login?next=/prepare")

    def test_writes_and_private_data_need_login(self):
        for method, path in [("POST", "/api/event/start"), ("POST", "/api/event/reset"),
                             ("DELETE", "/api/participants/101"), ("DELETE", "/api/laps/1"),
                             ("POST", "/api/simulator/trigger/101"), ("GET", "/api/participants"),
                             ("GET", "/api/events/1/registrations.csv")]:
            res = self.anon.request(method, path)
            self.assertEqual(res.status_code, 401, f"{method} {path}")
            self.assertEqual(res.json()["detail"], "Admin login required")
        self.assertIsNotNone(self.web_app.db.get_participant_by_bib(101), "delete must not have run")

    def test_every_non_public_route_is_protected(self):
        """Catches a new route that forgets about auth: anything not public must refuse anonymous use."""
        from fastapi.routing import APIRoute
        checked = 0
        for route in self.web_app.app.routes:
            if not isinstance(route, APIRoute):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            for method in route.methods - {"HEAD", "OPTIONS"}:
                if is_public(method, path):
                    continue
                res = self.anon.request(method, path)
                self.assertIn(res.status_code, (401, 303), f"{method} {route.path} is not protected")
                checked += 1
        self.assertGreater(checked, 25)

    def test_wrong_pin(self):
        res = self.login(self.anon, pin="000000")
        self.assertEqual(res.status_code, 401)
        self.assertIn("Wrong PIN", res.text)
        self.assertNotIn(COOKIE_NAME, res.cookies)

    def test_login_then_admin_works(self):
        res = self.login(self.anon, next_url="/prepare")
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/prepare")
        set_cookie = res.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=lax", set_cookie)
        page = self.anon.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/logout"', page.text)
        self.assertNotIn("not password-protected", page.text)
        self.assertEqual(self.anon.get("/api/participants").status_code, 200)

    def test_login_rejects_offsite_redirect(self):
        res = self.login(self.anon, next_url="//evil.example/")
        self.assertEqual(res.headers["location"], "/admin")

    def test_forged_cookie_rejected(self):
        self.anon.cookies.set(COOKIE_NAME, AdminAuth("111111").session_token())
        self.assertEqual(self.anon.get("/api/participants").status_code, 401)

    def test_lockout_message(self):
        for _ in range(MAX_FAILURES):
            self.login(self.anon, pin="1")
        res = self.login(self.anon)  # right PIN, but locked out
        self.assertEqual(res.status_code, 401)
        self.assertIn("Too many wrong PINs", res.text)

    def test_logout(self):
        client = self.admin_client()
        self.assertEqual(client.get("/api/participants").status_code, 200)
        res = client.post("/logout")
        self.assertEqual(res.status_code, 303)
        self.assertEqual(client.get("/api/participants").status_code, 401)

    def test_delete_lap_button_only_for_admin(self):
        self.assertNotIn("deleteLap(", self.anon.get("/runner/101").text.split("<script>")[0])
        self.assertIn("Action</th>", self.admin_client().get("/runner/101").text)

    def test_auth_off_when_no_pin(self):
        with mock.patch.object(self.web_app, "admin_auth", AdminAuth(None)):
            page = self.anon.get("/admin")
            self.assertEqual(page.status_code, 200)
            self.assertIn("not password-protected", page.text)
            self.assertEqual(self.anon.get("/api/participants").status_code, 200)


if __name__ == "__main__":
    unittest.main()
