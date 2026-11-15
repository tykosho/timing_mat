"""Admin PIN login for the race control pages and every write endpoint (issue #3).

Everything is admin-only unless it's on the public list below, so a new route is protected
by default. The leaderboard, runner pages and their read-only data stay open for the TV and
runners' phones.

The session is an HttpOnly cookie holding an HMAC of the PIN, so it survives restarts and
changing the PIN logs everyone out. SameSite=Lax keeps other sites from posting to the API
with it.
"""
import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from fastapi import Request
from starlette.requests import HTTPConnection
from fastapi.responses import JSONResponse, RedirectResponse

logger = logging.getLogger("Auth")

COOKIE_NAME = "timing_admin"
COOKIE_MAX_AGE = 7 * 24 * 3600

# (method, path regex) pairs anyone may use. Everything else needs the admin cookie.
PUBLIC_ROUTES = [
    ("GET", r"/"),
    ("GET", r"/leaderboard"),
    ("GET", r"/runner/\d+"),
    ("GET", r"/static/.*"),
    ("GET", r"/login"),
    ("POST", r"/login"),
    ("POST", r"/logout"),
    ("GET", r"/api/leaderboard"),
    ("GET", r"/api/event"),
    ("GET", r"/favicon\.ico"),
]
_PUBLIC = [(m, re.compile(p + r"/?")) for m, p in PUBLIC_ROUTES]

# Brute-force throttle: this many wrong PINs from one client locks it out for LOCKOUT_SECONDS.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60.0


class AdminAuth:
    def __init__(self, pin: Optional[str]):
        self.pin = (pin or "").strip() or None
        self._failures: Dict[str, List[float]] = {}

    @property
    def enabled(self) -> bool:
        return self.pin is not None

    # --- cookie ---
    def session_token(self) -> str:
        return hmac.new(self.pin.encode(), b"timing-mat admin session v1", hashlib.sha256).hexdigest()

    def is_admin(self, request: HTTPConnection) -> bool:
        """Works for HTTP requests and WebSockets (both carry the cookie)."""
        if not self.enabled:
            return True
        cookie = request.cookies.get(COOKIE_NAME, "")
        return hmac.compare_digest(cookie, self.session_token())

    # --- login throttling ---
    def _recent_failures(self, client: str, now: float) -> List[float]:
        recent = [t for t in self._failures.get(client, []) if now - t < LOCKOUT_SECONDS]
        self._failures[client] = recent
        return recent

    def locked_out(self, client: str, now: Optional[float] = None) -> bool:
        return len(self._recent_failures(client, now or time.monotonic())) >= MAX_FAILURES

    def check_pin(self, client: str, pin: str, now: Optional[float] = None) -> bool:
        now = now or time.monotonic()
        if self.locked_out(client, now):
            return False
        if self.enabled and hmac.compare_digest(pin.strip().encode(), self.pin.encode()):
            self._failures.pop(client, None)
            return True
        self._failures.setdefault(client, []).append(now)
        logger.warning(f"Wrong admin PIN from {client}")
        return False


def is_public(method: str, path: str) -> bool:
    method = "GET" if method == "HEAD" else method
    return any(m == method and rx.fullmatch(path) for m, rx in _PUBLIC)


def safe_next(target: Optional[str]) -> str:
    """Only allow redirects back into this site (no //evil.com or absolute URLs)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/admin"


def resolve_pin(configured: Optional[str], use_mock: bool) -> Optional[str]:
    """The PIN to use. With a real reader and none configured, make one up rather than run open."""
    configured = (str(configured).strip() if configured is not None else "") or None
    if configured:
        return configured
    if use_mock:
        logger.warning("ADMIN_PIN is not set: admin pages and API are UNPROTECTED (fine for local mock mode).")
        return None
    generated = f"{secrets.randbelow(10**6):06d}"
    logger.warning("=" * 60)
    logger.warning(f"ADMIN_PIN is not set. Generated a one-time admin PIN: {generated}")
    logger.warning("Set ADMIN_PIN (env or config.yaml admin.pin) to choose your own.")
    logger.warning("=" * 60)
    return generated


def make_middleware(get_auth):
    """HTTP middleware. `get_auth` returns the current AdminAuth (so tests can swap it)."""
    async def admin_guard(request: Request, call_next):
        auth = get_auth()
        if is_public(request.method, request.url.path) or auth.is_admin(request):
            return await call_next(request)
        if request.method == "GET" and not request.url.path.startswith("/api/"):
            nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(url=f"/login?next={quote(nxt)}", status_code=303)
        return JSONResponse(status_code=401, content={"detail": "Admin login required"})
    return admin_guard
