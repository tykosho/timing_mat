"""QR codes for the printable /join page: join the race Wi-Fi, then open the leaderboard (issue #42).

Generated on the Pi as inline SVG, so the page works with no internet.
"""
import os
import re
from typing import Optional

import qrcode
import qrcode.image.svg
from markupsafe import Markup


def hotspot_settings() -> dict:
    """The race network, as configured for deploy/pi/hotspot.sh (env / .env)."""
    return {
        "ssid": os.getenv("HOTSPOT_SSID", "").strip(),
        "password": os.getenv("HOTSPOT_PASSWORD", ""),
        "hostname": os.getenv("HOTSPOT_HOSTNAME", "").strip(),
    }


def _escape_wifi(value: str) -> str:
    # The WIFI: QR format uses ; , : as separators and \\ and " for escaping
    for char in ("\\", ";", ",", ":", '"'):
        value = value.replace(char, "\\" + char)
    return value


def wifi_qr_payload(ssid: str, password: Optional[str]) -> str:
    """What phones' cameras understand as "join this network"."""
    if password:
        return f"WIFI:T:WPA;S:{_escape_wifi(ssid)};P:{_escape_wifi(password)};;"
    return f"WIFI:T:nopass;S:{_escape_wifi(ssid)};;"


def qr_svg(data: str) -> Markup:
    """An inline SVG QR code that scales to its container."""
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    svg = img.to_string(encoding="unicode")
    # Drop the fixed mm size so CSS controls it; the viewBox keeps the aspect ratio
    svg = re.sub(r'^<svg width="[^"]*" height="[^"]*" ', '<svg role="img" ', svg, count=1)
    return Markup(svg)


def site_url(hostname: str, request_base: str) -> str:
    """The address to put on the poster: the hotspot name if configured, else how this page was reached."""
    if hostname:
        return f"http://{hostname}"
    return request_base.rstrip("/")
