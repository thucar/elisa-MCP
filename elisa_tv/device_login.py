"""Link an Elisa account with a pairing code — the same flow the Elisa TV app uses.

OAuth 2.0 Device Authorization Grant on Elisa's Keycloak: we get a short code, the user
opens elisaelamus.ee/lisa (or scans the QR code with a phone), logs in and confirms, and we
receive a long-lived (offline) refresh token. Nothing to copy or paste.
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEVICE_URL = "https://www.elisaelamus.ee/auth/realms/elamus/protocol/openid-connect/auth/device"
TOKEN_URL = "https://www.elisaelamus.ee/auth/realms/elamus/protocol/openid-connect/token"
CLIENT_ID = "androidtv"                    # Elisa's TV client (supports the device-code grant)
SCOPE = "offline_access openid zattoo"
DEVICE_NAME = "Elisa TV MCP"


class LoginError(RuntimeError):
    pass


def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(body).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {"error": str(e)}


def qr_text(data: str) -> str:
    """QR code drawn with Unicode half blocks (scannable from a terminal)."""
    try:
        import qrcode
    except ImportError:
        return ""
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def start(device_uuid: str) -> dict:
    code, d = _post(DEVICE_URL, {"client_id": CLIENT_ID, "scope": SCOPE, "device_uuid": device_uuid,
                                 "device_name": DEVICE_NAME, "device_type": "androidtv"})
    if code != 200 or "device_code" not in d:
        raise LoginError(f"Elisa did not issue a pairing code: {d.get('error_description') or d}")
    return d


def wait(start_resp: dict, device_uuid: str, on_tick=None) -> dict:
    """Poll until the user confirms. Returns the token response."""
    interval = int(start_resp.get("interval", 5))
    end = time.time() + int(start_resp.get("expires_in", 600))
    while time.time() < end:
        time.sleep(interval)
        code, d = _post(TOKEN_URL, {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                    "client_id": CLIENT_ID, "device_code": start_resp["device_code"],
                                    "device_uuid": device_uuid})
        if code == 200 and "access_token" in d:
            return d
        err = d.get("error")
        if err == "authorization_pending":
            if on_tick:
                on_tick(int(end - time.time()))
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "access_denied":
            raise LoginError("the login was declined")
        if err == "expired_token":
            break
        raise LoginError(f"login failed: {d.get('error_description') or err}")
    raise LoginError("the pairing code expired (10 minutes); please try again")


def login(device_uuid: str, say=print) -> dict:
    s = start(device_uuid)
    url = "https://" + s["verification_uri_complete"].removeprefix("https://")
    code = s["user_code"]
    say("")
    say("  Connect your Elisa account, just like pairing a TV:")
    say("")
    say(f"    • Scan this QR code with your phone, or open  {s['verification_uri'].removeprefix('https://')}")
    say(f"    • Log in with your Elisa account and confirm the code:   {code}")
    say("")
    qr = qr_text(url)
    if qr:
        for line in qr.splitlines():
            say("      " + line)
    say("  Waiting for confirmation (the code is valid for 10 minutes) ...")
    return wait(s, device_uuid)
