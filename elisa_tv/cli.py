"""`elisa-tv` command line: setup wizard, server, sync and helpers."""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import config, discover, guide


def _run(*args, **kw):
    """subprocess.run that never lets the child read our stdin (adb shell would swallow input)."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(*args, **kw)

PLATFORM_TOOLS = {
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}
UNIT_NAME = "elisa-tv.service"

def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str, default: str | None = None) -> str:
    sfx = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{sfx}: ").strip()
    except EOFError:
        v = ""
    return v or (default or "")


def yes(prompt: str, default: bool = True) -> bool:
    v = ask(prompt + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not v else v.startswith("y")


# ---------------------------------------------------------------------------- steps

def step_adb(cfg: dict) -> str:
    adb = config.adb_path(cfg)
    if adb:
        say(f"✓ adb: {adb}")
        return adb
    url = PLATFORM_TOOLS.get(sys.platform)
    if not url:
        raise SystemExit("adb not found; install Android platform-tools and re-run")
    say(f"Downloading Android platform-tools (adb) from {url} ...")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "pt.zip"
        urllib.request.urlretrieve(url, zp)
        with zipfile.ZipFile(zp) as z:
            z.extractall(config.DATA_DIR)
    adb = str(config.DATA_DIR / "platform-tools" / "adb")
    os.chmod(adb, 0o755)
    cfg["adb"] = adb
    say(f"✓ adb installed: {adb}")
    return adb


def wait_authorized(adb: str, host: str, port: int) -> dict:
    say(f"  Connecting to {host}:{port} — if the TV shows 'Allow network debugging?', choose "
        "'Always allow from this computer' and OK.")
    info = {}
    for _ in range(40):
        info = discover.probe(adb, host, port)
        if info["status"] == "ok":
            return info
        time.sleep(3)
    return info


def step_tv(cfg: dict, adb: str, host: str | None = None, interactive: bool = True) -> None:
    port = int(cfg["tv"].get("port") or 5555)
    if not host:
        say("Searching the local network for TVs with network debugging (ADB) enabled ...")
        cands = discover.scan_adb(port)
        mdns = discover.mdns_android_tvs()
        found = []
        for h in cands:
            info = discover.probe(adb, h, port)
            found.append(info)
            label = (f"{info.get('manufacturer', '')} {info.get('model', '')} (Android {info.get('android')})"
                     if info["status"] == "ok" else info["status"])
            say(f"  {h}: {label}" + ("  [Elisa app installed]" if info.get("has_elisa") else ""))
        tvs = [f for f in found if f["status"] == "unauthorized" or f.get("is_tv")]
        no_adb = [m for m in mdns if m["host"] not in cands]
        for m in no_adb:
            say(f"  {m['host']}: Android TV '{m['name']}' found, but network debugging is OFF "
                "(Settings → System → Developer options → Network debugging)")
        if len(tvs) == 1:
            host = tvs[0]["host"]
            if interactive and not yes(f"Use TV at {host}?"):
                host = None
        elif len(tvs) > 1 and interactive:
            for i, t in enumerate(tvs, 1):
                say(f"  {i}) {t['host']} {t.get('model', '')}")
            sel = ask("Choose TV number")
            host = tvs[int(sel) - 1]["host"] if sel.isdigit() and 0 < int(sel) <= len(tvs) else None
        if not host:
            if not interactive:
                raise SystemExit("no TV found; pass --tv-host")
            say("No TV selected/found automatically.")
            host = ask("Enter the TV's IP address")
    info = wait_authorized(adb, host, port)
    if info.get("status") != "ok":
        raise SystemExit(f"could not connect to the TV at {host}: {info}")
    cfg["tv"].update(host=host, port=port, name=f"{info.get('manufacturer')} {info.get('model')}")
    say(f"✓ TV: {cfg['tv']['name']} at {host}" + ("" if info.get("has_elisa") else
                                                    "  (note: Elisa app not installed)"))


def step_elisa(cfg: dict, interactive: bool = True, skip: bool = False, package: str | None = None) -> None:
    auth = guide.Auth()
    linked = False
    if auth.customer_active() and interactive and not yes("An Elisa account is already linked. Re-link it?", False):
        linked = True
    elif not skip and interactive and yes("Connect your Elisa Elamus account? (you confirm a code on your "
                                          "phone, like when pairing the TV)"):
        linked = elisa_login(auth)
    if linked:
        cfg["subscription"] = {"source": "account", "package": None, "channel_numbers": []}
        n = _entitled_count(auth)
        say(f"✓ Elisa account linked; {n} channels in your subscription" if n else
            "✓ Elisa account linked (could not read entitlements yet)")
        return
    pkgs = config.packages()
    if package:
        cfg["subscription"] = ({"source": "all", "package": None, "channel_numbers": []} if package == "all"
                               else {"source": "package", "package": package, "channel_numbers": []})
    elif interactive:
        names = list(pkgs)
        say("Which Elisa package do you have? (guide is downloaded only for its channels)")
        for i, n in enumerate(names, 1):
            say(f"  {i}) {n} ({len(pkgs[n]['channel_numbers'])} channels)")
        say(f"  {len(names) + 1}) other – enter channel numbers")
        say(f"  {len(names) + 2}) all Elisa channels")
        sel = ask("Choice", "1")
        i = int(sel) if sel.isdigit() else 1
        if i <= len(names):
            cfg["subscription"] = {"source": "package", "package": names[i - 1], "channel_numbers": []}
        elif i == len(names) + 1:
            nums = [int(x) for x in ask("Channel numbers, comma separated").replace(" ", "").split(",") if x]
            cfg["subscription"] = {"source": "custom", "package": None, "channel_numbers": nums}
        else:
            cfg["subscription"] = {"source": "all", "package": None, "channel_numbers": []}
    say(f"✓ guest mode; subscription: {cfg['subscription']['package'] or cfg['subscription']['source']}")


def _entitled_count(auth: guide.Auth) -> int:
    try:
        return sum(1 for c in guide.Api(auth).paged("/channels") if c.get("live"))
    except Exception:
        return 0


def elisa_login(auth: guide.Auth | None = None) -> bool:
    """Link the Elisa account with a pairing code (QR / elisaelamus.ee/lisa), like the TV app."""
    from .device_login import LoginError
    auth = auth or guide.Auth()
    for attempt in range(2):
        try:
            auth.link_customer(say=say)
            return True
        except LoginError as e:
            say(f"  ✗ {e}")
            if attempt == 0 and "expired" in str(e) and yes("  Get a new code?"):
                continue
            return False
        except OSError as e:
            say(f"  ✗ network problem talking to Elisa: {e}")
            return False
    return False


def step_http(cfg: dict, port: int | None = None) -> None:
    http = cfg["http"]
    if port:
        http["port"] = port
    if not http.get("api_key"):
        http["api_key"] = secrets.token_urlsafe(32)
    say(f"✓ MCP endpoint will listen on {http['host']}:{http['port']} (API key generated)")


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packets sent; picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def install_service(linger: bool | None = None) -> None:
    exe = shutil.which("elisa-tv") or str(Path(sys.executable).parent / "elisa-tv")
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / UNIT_NAME).write_text(f"""[Unit]
Description=Elisa TV MCP server (TV guide + TV control)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={exe} serve
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
""")
    _run(["systemctl", "--user", "daemon-reload"], check=True)
    _run(["systemctl", "--user", "enable", UNIT_NAME], check=True)
    _run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    say(f"✓ systemd user service {UNIT_NAME} installed and started")
    user = os.environ.get("USER", "")
    lingering = Path(f"/var/lib/systemd/linger/{user}").exists()
    if not lingering:
        if linger is None:
            linger = yes("Keep the server running when you are logged out (systemd linger)?")
        if linger:
            r = _run(["loginctl", "enable-linger", user])
            say("✓ linger enabled" if r.returncode == 0 else
                f"  could not enable linger; run: sudo loginctl enable-linger {user}")


def client_instructions(cfg: dict) -> str:
    url = f"http://{lan_ip()}:{cfg['http']['port']}/mcp"
    key = cfg["http"]["api_key"]
    return f"""Connect an agent on the LAN:
  URL:    {url}
  Header: Authorization: Bearer {key}

  Claude Code:
    claude mcp add --transport http elisa-tv {url} --header "Authorization: Bearer {key}"

  JSON (.mcp.json / other MCP clients):
    {json.dumps({"mcpServers": {"elisa-tv": {"type": "http", "url": url,
                                            "headers": {"Authorization": f"Bearer {key}"}}}})}

If a firewall is active, allow TCP {cfg['http']['port']} from the LAN (e.g. `sudo ufw allow from 192.168.0.0/16 to any port {cfg['http']['port']} proto tcp`)."""


# ---------------------------------------------------------------------------- commands

def cmd_setup(a) -> None:
    interactive = not a.non_interactive
    cfg = config.load()
    say("== Elisa TV MCP setup ==\n")
    adb = step_adb(cfg)
    config.save(cfg)
    say("\n-- TV --")
    step_tv(cfg, adb, a.tv_host, interactive)
    config.save(cfg)
    say("\n-- Elisa Elamus --")
    step_elisa(cfg, interactive, skip=a.skip_elisa_login, package=a.package)
    config.save(cfg)
    say("\n-- Server --")
    step_http(cfg, a.port)
    config.save(cfg)
    say("Downloading the TV guide (about a minute) ...")
    logging.basicConfig(level=logging.WARNING)
    st = guide.sync(cfg=cfg)
    say(f"✓ guide: {st}")
    if not a.no_service:
        install_service(linger=False if not interactive else None)
    say("\n" + client_instructions(cfg))
    say(f"\nConfig: {config.CONFIG_PATH}\nData:   {config.DATA_DIR}")


def cmd_elisa_login(a) -> None:
    if not elisa_login():
        say("not linked")
        return
    cfg = config.load()
    cfg["subscription"] = {"source": "account", "package": None, "channel_numbers": []}
    config.save(cfg)
    say("✓ Elisa account linked; channel list now follows your subscription.")
    say("  Apply now: systemctl --user restart elisa-tv && elisa-tv sync")


def cmd_status(a) -> None:
    cfg = config.load()
    say(f"config: {config.CONFIG_PATH}")
    say(f"TV: {cfg['tv'].get('name')} at {cfg['tv'].get('host')}:{cfg['tv'].get('port')}")
    try:
        from .tv import TV
        say(f"TV state: {TV(cfg).status()}")
    except Exception as e:
        say(f"TV state: unavailable ({e})")
    say(f"Elisa: {guide.Auth().status()}")
    sub = cfg["subscription"]
    say(f"subscription: {sub.get('package') or sub.get('source')}")
    from .server import guide_status
    say(f"guide: {guide_status()}")
    r = _run(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True, text=True)
    say(f"service: {r.stdout.strip() or r.stderr.strip()}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="elisa-tv", description="Elisa TV guide + TV control MCP server")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="interactive setup (TV, Elisa login, service)")
    s.add_argument("--tv-host", help="TV IP (skip network search)")
    s.add_argument("--port", type=int, help="HTTP port (default 8765)")
    s.add_argument("--skip-elisa-login", action="store_true", help="use guest mode")
    s.add_argument("--package", help="guest mode channel package (e.g. 'Pisike Elamus' or 'all')")
    s.add_argument("--no-service", action="store_true", help="do not install the systemd service")
    s.add_argument("--non-interactive", action="store_true")
    s.set_defaults(fn=cmd_setup)
    sv = sub.add_parser("serve", help="run the MCP server (HTTP; --stdio for local clients)")
    sv.add_argument("--stdio", action="store_true")
    sv.set_defaults(fn=lambda a: __import__("elisa_tv.server", fromlist=["serve"]).serve(stdio=a.stdio))
    sy = sub.add_parser("sync", help="download the guide now")
    sy.add_argument("--force", action="store_true", help="re-fetch all days")
    sy.set_defaults(fn=lambda a: (logging.basicConfig(level=logging.INFO), say(json.dumps(guide.sync(a.force)))))
    sub.add_parser("status", help="show configuration and health").set_defaults(fn=cmd_status)
    sub.add_parser("elisa-login", help="(re)link the Elisa account with a pairing code").set_defaults(
        fn=cmd_elisa_login)
    sub.add_parser("elisa-logout", help="forget the Elisa account (guest mode)").set_defaults(
        fn=lambda a: (guide.Auth().clear_customer(), say("✓ account unlinked")))
    sub.add_parser("client-config", help="print how to connect an agent").set_defaults(
        fn=lambda a: say(client_instructions(config.load())))
    sub.add_parser("install-service", help="(re)install the systemd user service").set_defaults(
        fn=lambda a: install_service())
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
