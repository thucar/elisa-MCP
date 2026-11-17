"""Android / Google TV control over ADB (network debugging)."""
from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

from . import config


def _run(*args, **kw):
    """subprocess.run that never lets the child read our stdin (adb shell would swallow input)."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(*args, **kw)

log = logging.getLogger("elisa_tv.tv")

KEYS = {
    "home": "HOME", "back": "BACK", "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT",
    "right": "DPAD_RIGHT", "ok": "DPAD_CENTER", "select": "DPAD_CENTER", "enter": "ENTER", "menu": "MENU",
    "info": "INFO", "guide": "GUIDE", "settings": "SETTINGS", "search": "SEARCH", "input": "TV_INPUT",
    "play_pause": "MEDIA_PLAY_PAUSE", "play": "MEDIA_PLAY", "pause": "MEDIA_PAUSE", "stop": "MEDIA_STOP",
    "rewind": "MEDIA_REWIND", "fast_forward": "MEDIA_FAST_FORWARD", "next": "MEDIA_NEXT",
    "previous": "MEDIA_PREVIOUS", "channel_up": "CHANNEL_UP", "channel_down": "CHANNEL_DOWN",
    "volume_up": "VOLUME_UP", "volume_down": "VOLUME_DOWN", "mute": "VOLUME_MUTE", "power": "POWER",
    "red": "PROG_RED", "green": "PROG_GREEN", "yellow": "PROG_YELLOW", "blue": "PROG_BLUE",
    "subtitles": "CAPTIONS", "audio_track": "MEDIA_AUDIO_TRACK",
    **{str(i): str(i) for i in range(10)},
}

APPS = {
    "elisa": "ee.elisa.smarttvclient",
    "youtube": "com.google.android.youtube.tv",
    "jupiter": "ee.err.jupiter", "err": "ee.err.jupiter",
    "stremio": "com.stremio.one",
    "tunein": "tunein.player",
    "settings": "com.android.tv.settings",
    "home": "com.google.android.apps.tv.launcherx",
}


class TVError(RuntimeError):
    pass


class TV:
    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or config.load()
        self.adb = config.adb_path(cfg)
        self.host = cfg["tv"]["host"]
        self.port = cfg["tv"]["port"]
        self.serial = f"{self.host}:{self.port}"
        self._lock = threading.Lock()
        if not self.adb:
            raise TVError("adb not found; run `elisa-tv setup`")
        if not self.host:
            raise TVError("no TV configured; run `elisa-tv setup`")

    # -- plumbing
    def _run(self, *args: str, timeout: float = 15, binary: bool = False):
        r = _run([self.adb, *args], capture_output=True, timeout=timeout)
        out = r.stdout if binary else r.stdout.decode(errors="replace")
        if r.returncode != 0 and not binary:
            out += r.stderr.decode(errors="replace")
        return out

    def ensure_connected(self) -> None:
        state = self._run("-s", self.serial, "get-state", timeout=5).strip()
        if state == "device":
            return
        self._run("connect", self.serial, timeout=10)
        for _ in range(10):
            state = self._run("-s", self.serial, "get-state", timeout=5).strip()
            if state == "device":
                return
            if "unauthorized" in state:
                raise TVError("TV has not authorized this computer: accept the 'Allow debugging?' prompt on the TV")
            time.sleep(0.5)
        raise TVError(f"cannot reach TV at {self.serial} (is it powered and network debugging on?): {state}")

    def shell(self, cmd: str, timeout: float = 15) -> str:
        with self._lock:
            self.ensure_connected()
            return self._run("-s", self.serial, "shell", cmd, timeout=timeout)

    # -- state
    def status(self) -> dict:
        power = re.search(r"mWakefulness=(\w+)", self.shell("dumpsys power | grep mWakefulness="))
        top = re.search(r"mResumedActivity: ActivityRecord\{\S+ \S+ ([^/\s]+)/(\S+)",
                        self.shell("dumpsys activity activities | grep -m1 mResumedActivity"))
        vol = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]",
                        self.shell("cmd media_session volume --stream 3 --get 2>/dev/null"))
        app = top.group(1) if top else None
        name = next((k for k, v in APPS.items() if v == app), app)
        return {"power": power.group(1) if power else "unknown", "foreground_app": name,
                "activity": top.group(2) if top else None,
                "volume": f"{vol.group(1)}/{vol.group(3)}" if vol else "unknown"}

    # -- input
    # Keypad that only declares the digit and Enter usages (0x1E-0x28), keeping the 8-byte boot
    # report layout. A full keyboard (letters) makes Android announce a keyboard configuration
    # change; Elisa then recreates its screens in the *system* language (English on this TV)
    # instead of its own (Estonian). A digits-only keypad is non-alphabetic and avoids that.
    HID_DESCRIPTOR = [5, 1, 9, 6, 161, 1,                      # Generic Desktop / Keyboard, collection
                      117, 8, 149, 2, 129, 1,                  # 2 constant bytes (modifiers, reserved)
                      149, 6, 117, 8, 21, 30, 37, 40,          # 6 key slots, logical 0x1E..0x28
                      5, 7, 25, 30, 41, 40, 129, 0,            # usages 1..0, Enter (array)
                      192]
    HID_USAGE = {**{str(i): 0x1D + i for i in range(1, 10)}, "0": 0x27, "\n": 0x28}  # "\n" = Enter

    def hid_type(self, chars: str, gap_ms: int = 350, after: str = "") -> str:
        """Type digits through a temporary virtual USB keyboard (counts as a real remote).
        `after`: shell command run right after typing, in the same call (its output is returned)."""
        lines = [{"id": 1, "command": "register", "name": "Elisa TV MCP keypad", "vid": 0x1234, "pid": 0x5678,
                  "bus": "usb", "descriptor": self.HID_DESCRIPTOR},
                 {"id": 1, "command": "delay", "duration": 1200}]
        for ch in chars:
            if ch not in self.HID_USAGE:
                raise TVError(f"cannot type {ch!r} on the virtual keypad")
            lines += [{"id": 1, "command": "report", "report": [0, 0, self.HID_USAGE[ch], 0, 0, 0, 0, 0]},
                      {"id": 1, "command": "delay", "duration": 80},
                      {"id": 1, "command": "report", "report": [0] * 8},
                      {"id": 1, "command": "delay", "duration": gap_ms}]
        body = "\n".join(json.dumps(x) for x in lines)
        return self.shell(f"cat > /data/local/tmp/elisa_tv_hid.json <<'EOF'\n{body}\nEOF\n"
                          "hid /data/local/tmp/elisa_tv_hid.json >/dev/null 2>&1" + (f"; {after}" if after else ""),
                          timeout=40)

    def keys(self, names: list[str], delay_ms: int = 0) -> str:
        names = [n.strip() for n in names]
        if names and all(n.isdigit() and len(n) == 1 for n in names):
            self.hid_type("".join(names))  # digits like the real remote (software digits go to Sony's tuner)
            return f"typed {''.join(names)} on the keypad"
        codes = []
        for n in names:
            k = n.strip().lower().replace(" ", "_")
            if k in KEYS:
                codes.append("KEYCODE_" + KEYS[k])
            elif n.upper().startswith("KEYCODE_"):
                codes.append(n.upper())
            else:
                raise TVError(f"unknown key {n!r}; known: {', '.join(KEYS)}")
        if delay_ms <= 0:
            self.shell("input keyevent " + " ".join(codes))
        else:
            for c in codes:
                self.shell(f"input keyevent {c}")
                time.sleep(delay_ms / 1000)
        return f"sent {len(codes)} key(s)"

    def text(self, s: str) -> str:
        esc = re.sub(r"([\\\"'`$&|;<>()* ])", r"\\\1", s).replace("\\ ", "%s")
        self.shell(f"input text {esc}")
        return "typed"

    def power(self, state: str) -> str:
        state = state.lower()
        code = {"on": "WAKEUP", "off": "SLEEP", "toggle": "POWER"}.get(state)
        if not code:
            raise TVError("state must be on|off|toggle")
        self.shell(f"input keyevent KEYCODE_{code}")
        return f"power {state}"

    def volume(self, level: int | None = None, delta: int | None = None) -> str:
        if level is not None:
            self.shell(f"cmd media_session volume --stream 3 --set {int(level)}")
        elif delta:
            key = "KEYCODE_VOLUME_UP" if delta > 0 else "KEYCODE_VOLUME_DOWN"
            self.shell("input keyevent " + " ".join([key] * min(abs(delta), 30)))
        return f"volume {self.status()['volume']}"

    def launch(self, app: str) -> str:
        if app.lower() == "home":
            self.shell("input keyevent KEYCODE_HOME")
            return "home screen"
        pkg = APPS.get(app.lower(), app)
        out = self.shell(f"monkey -p {pkg} -c android.intent.category.LEANBACK_LAUNCHER 1")
        if "No activities found" in out or "monkey aborted" in out:
            out = self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
            if "No activities found" in out:
                raise TVError(f"cannot launch {pkg}")
        return f"launched {pkg}"

    def installed_apps(self) -> list[str]:
        return sorted(l.split(":", 1)[1] for l in self.shell("pm list packages -3").split() if ":" in l)

    # -- screen
    def _dump(self, before: str = "") -> ET.Element | None:
        """UI hierarchy; `before` is a shell command run first in the same call (e.g. a key press)."""
        cmd = (before + "; " if before else "") + \
            "uiautomator dump /sdcard/elisa_tv_ui.xml >/dev/null 2>&1 && cat /sdcard/elisa_tv_ui.xml"
        out = self.shell(cmd, timeout=25)
        if "<hierarchy" not in out:
            return None
        return ET.fromstring(out[out.index("<?xml") if "<?xml" in out else out.index("<hierarchy"):])

    @staticmethod
    def _texts(node: ET.Element) -> list[str]:
        return [t.strip() for n in node.iter("node") for t in (n.get("text"), n.get("content-desc")) if t and t.strip()]

    def screen(self) -> str:
        """Visible UI texts (accessibility tree) with the focused element marked."""
        root = self._dump()
        if root is None:
            raise TVError("could not read the screen (UI busy); try again or use tv_screenshot")
        lines = []
        for n in root.iter("node"):
            label = n.get("text") or n.get("content-desc") or ""
            focused = n.get("focused") == "true" or n.get("selected") == "true"
            if not label.strip() and not focused:
                continue
            if focused and not label.strip():  # focused container: show what is inside it
                label = " | ".join(list(dict.fromkeys(self._texts(n)))[:6]) or "(focused element)"
            mark = ">> " if focused else "   "
            lines.append(f"{mark}{label.strip()}")
        pkg = next((n.get("package") for n in root.iter("node") if n.get("package")), "?")
        if not lines:
            return f"[app: {pkg}] no visible text or focus: probably full-screen video with no menu open"
        return f"[app: {pkg}]\n" + "\n".join(dict.fromkeys(lines))  # dedupe, keep order

    def screenshot(self, max_width: int = 960) -> bytes:
        with self._lock:
            self.ensure_connected()
            png = self._run("-s", self.serial, "exec-out", "screencap -p", timeout=20, binary=True)
        if not png.startswith(b"\x89PNG"):
            raise TVError("the TV refused the screenshot (protected/DRM content such as Elisa video is on "
                          "screen); use tv_status / tv_screen instead")
        from PIL import Image
        img = Image.open(io.BytesIO(png)).convert("RGB")
        if img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()

    # -- Elisa
    # Elisa deep link that opens one airing's detail page with its play button focused
    # ("Vaata H8 O12" = watch, "Jätka H8 O11" = continue). The link is only honoured by a
    # running Elisa instance; a cold start with the link lands on the home screen.
    ELISA_PROGRAM_LINK = "ee.elisa.smarttvclient:///android-home-channel/program/broadcastid/{b}/programid/{p}"
    PLAY_WORDS = ("Vaata", "Jätka", "Watch", "Continue", "Resume", "Смотреть", "Продолжить")

    def _focused_label(self, root: ET.Element) -> str:
        for n in root.iter("node"):
            if n.get("focused") == "true":
                t = " | ".join(list(dict.fromkeys(self._texts(n)))[:3])
                if t:
                    return t
        return ""

    ELISA_SPLASH = ("Veidi veel", "mul ei lähe enam kaua", "elu on elamust")

    def _elisa_ready(self, timeout: float = 30) -> None:
        """Elisa in the foreground and past its loading screen (any page or the player)."""
        if self.status()["foreground_app"] != "elisa":
            self.launch("elisa")
            time.sleep(3)
        end = time.time() + timeout
        while time.time() < end:
            root = self._dump()
            if root is not None and self.status()["foreground_app"] == "elisa":
                joined = " ".join(self._texts(root))
                if not any(s in joined for s in self.ELISA_SPLASH):
                    return
            time.sleep(1.5)
        raise TVError("Elisa did not finish starting (still on its loading screen)")

    def elisa_play(self, broadcast_id: str, program_id: str, expect: list[str] | None = None) -> str:
        """Open an airing in Elisa and start it. `expect`: episode labels to verify, e.g. ["H8 O12", "S8 E12"]."""
        self._elisa_ready()
        link = self.ELISA_PROGRAM_LINK.format(b=broadcast_id, p=program_id)
        self.shell(f"am start -a android.intent.action.VIEW -d '{link}' ee.elisa.smarttvclient")
        label, end = "", time.time() + 15
        while time.time() < end:
            time.sleep(1.2)
            root = self._dump()
            if root is None:
                continue
            label = self._focused_label(root)
            if label.startswith(self.PLAY_WORDS):
                break
        else:
            raise TVError(f"Elisa did not show the play button (focused: {label!r})")
        if expect and not any(e in label for e in expect):
            raise TVError(f"Elisa opened a different item than expected: {label!r} (wanted {expect[0]})")
        self.shell("input keyevent KEYCODE_DPAD_CENTER")
        time.sleep(4)
        root = self._dump()
        if root is not None and self._focused_label(root).startswith(self.PLAY_WORDS):
            raise TVError(f"pressed play but Elisa is still on the details page ({label!r})")
        return f"playing: {label}"

    # Channel numbers: Sony's system hands number keys from software ("input keyevent") to its own
    # tuner app, but passes keys from real input devices to the foreground app. A virtual USB
    # keyboard (Android's `hid` tool, allowed for the adb shell) counts as a real device, so digits
    # typed through it reach Elisa exactly like the remote's number buttons.
    def elisa_channel(self, target: int, now_title: str = "") -> str:
        """Tune Elisa live TV: type the channel number (+ Enter when in the player, which skips the digit timeout).
        Verified from the overlay read in the same shell call (it shows ~2 s): either the channel
        banner "<number> <programme>" or the info overlay "<clock> <programme>", whose programme is
        compared with `now_title` (what the guide says is on the target channel now)."""
        if self.status()["foreground_app"] != "elisa":
            self._elisa_ready()
        # Enter confirms the number at once only in the full-screen player. On a menu/page Enter
        # would activate the focused item (e.g. open a programme page), so there we type the digits
        # alone and let Elisa's digit timeout (~2 s) switch the channel.
        root = self._dump()
        in_player = root is not None and not self._texts(root)
        out = self.hid_type(str(int(target)) + ("\n" if in_player else ""),
                            after="uiautomator dump /sdcard/elisa_tv_ui.xml >/dev/null 2>&1 "
                                  "&& cat /sdcard/elisa_tv_ui.xml")
        texts: list[str] = []
        if "<hierarchy" in out:
            texts = self._texts(ET.fromstring(out[out.index("<hierarchy"):]))
        if texts and texts[0].isdigit():
            got = int(texts[0])
            if got != target:
                raise TVError(f"Elisa shows channel {got}, not {target}")
            return f"now on channel {target}" + (f": {texts[1]}" if len(texts) > 1 else "")
        if now_title and any(now_title.lower() in t.lower() or t.lower() in now_title.lower()
                             for t in texts if len(t) > 2):
            return f"now on channel {target}: {now_title}"
        if self.status()["foreground_app"] != "elisa":
            raise TVError("the number went to another app instead of Elisa")
        shown = " | ".join(texts[:3]) or "nothing"
        return f"switched to channel {target} (screen shows: {shown}; could not confirm against the guide)"
