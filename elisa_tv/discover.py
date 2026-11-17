"""Find Android/Google TVs on the local network."""
from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor


def _run(*args, **kw):
    """subprocess.run that never lets the child read our stdin (adb shell would swallow input)."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(*args, **kw)


def local_networks() -> list[ipaddress.IPv4Network]:
    """IPv4 networks of this machine's interfaces (capped at /22 so a scan stays quick)."""
    nets = []
    try:
        data = json.loads(_run(["ip", "-j", "-4", "addr", "show", "scope", "global"],
                                         capture_output=True, text=True, timeout=5).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        data = []
    for iface in data:
        if iface.get("ifname", "").startswith(("docker", "br-", "veth", "virbr", "tun", "wg")):
            continue
        for a in iface.get("addr_info", []):
            net = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network
            if net.prefixlen < 22:
                net = ipaddress.ip_interface(f"{a['local']}/22").network
            if net.is_private and net not in nets:
                nets.append(net)
    return nets


def _open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_adb(port: int = 5555, timeout: float = 0.5) -> list[str]:
    hosts = [str(h) for net in local_networks() for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=128) as ex:
        found = [h for h, ok in zip(hosts, ex.map(lambda h: _open(h, port, timeout), hosts)) if ok]
    return found


def mdns_android_tvs() -> list[dict]:
    """Android TVs announcing the remote-control service (works even with ADB disabled)."""
    if not shutil.which("avahi-browse"):
        return []
    try:
        out = _run(["avahi-browse", "-rpt", "_androidtvremote2._tcp"], capture_output=True,
                             text=True, timeout=8).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    tvs = {}
    for line in out.splitlines():
        f = line.split(";")
        if f[0] == "=" and len(f) > 8 and f[2] == "IPv4":
            tvs[f[7]] = {"host": f[7], "name": f[3].replace("\\032", " ")}
    return list(tvs.values())


def probe(adb: str, host: str, port: int = 5555) -> dict:
    """Connect with adb and describe the device. Status: ok | unauthorized | error."""
    serial = f"{host}:{port}"
    _run([adb, "connect", serial], capture_output=True, timeout=10)
    state = _run([adb, "-s", serial, "get-state"], capture_output=True, text=True, timeout=5)
    s = (state.stdout + state.stderr).strip()
    if "unauthorized" in s:
        return {"host": host, "status": "unauthorized"}
    if state.stdout.strip() != "device":
        return {"host": host, "status": "error", "detail": s}

    def prop(p: str) -> str:
        return _run([adb, "-s", serial, "shell", "getprop", p], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    feats = _run([adb, "-s", serial, "shell", "pm", "list", "features"], capture_output=True,
                           text=True, timeout=10).stdout
    return {"host": host, "status": "ok", "manufacturer": prop("ro.product.manufacturer"),
            "model": prop("ro.product.model"), "android": prop("ro.build.version.release"),
            "is_tv": "android.software.leanback" in feats or "tv" in prop("ro.build.characteristics"),
            "has_elisa": "ee.elisa.smarttvclient" in _run(
                [adb, "-s", serial, "shell", "pm", "list", "packages", "ee.elisa.smarttvclient"],
                capture_output=True, text=True, timeout=10).stdout}
