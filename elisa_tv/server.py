"""MCP server: Elisa TV guide + TV control. Serves streamable HTTP on the LAN (API-key protected)."""
from __future__ import annotations

import hmac
import json
import logging
import sys
import threading
import time
from contextlib import closing

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import config, guide
from . import query as q
from .tv import TV, TVError

log = logging.getLogger("elisa_tv.server")

INSTRUCTIONS = """\
Controls the household's Android/Google TV and knows the Elisa Elamus (Estonian IPTV) TV guide.

GUIDE (local database, instant): times are Europe/Tallinn. Each airing has a status:
LIVE now / catch-up (watchable from the start within the channel's replay window) / upcoming /
expired. Every airing line carries listing=<id>. Only channels in the household's Elisa
subscription are in the guide.
Typical flow: search_guide("pilvede all") -> series_episodes(series=..., relative_to="S08E12",
direction="earlier", only_watchable=True) to step back through episodes already seen ->
elisa_play(listing_id) to start it on the TV. For live TV: elisa_tune_channel("ETV").

TV: tv_status first. Someone may be watching: do not change what is on screen unless asked.
tv_screen reads the visible UI as text (">>" marks the focused item); use it after each step
when navigating menus with tv_keys. tv_screenshot returns an image when text is not enough.

Answer viewers briefly; they are watching TV, not reading reports."""

server = MCPServer("elisa-tv", instructions=INSTRUCTIONS)
_tv: TV | None = None


def tv() -> TV:
    global _tv
    if _tv is None:
        _tv = TV()
    return _tv


def _db():
    return closing(guide.connect(readonly=True))


# ============================================================================ guide

@server.tool()
def search_guide(query: str, scope: str = "all", limit: int = 8) -> str:
    """Full-text search of the TV guide (titles, original titles, episode titles, descriptions).
    Prefix and accent-insensitive ("pilvede" finds "Pilvede all").
    scope: "all" | "catchup" (live or watchable from start) | "upcoming" | "live".
    Results are grouped per show with live, recent catch-up and upcoming airings."""
    with _db() as con:
        return q.search(con, query, scope, limit)


@server.tool()
def series_episodes(series: str = "", title: str = "", relative_to: str = "", direction: str = "earlier",
                    only_watchable: bool = False, limit: int = 15) -> str:
    """Episodes of a series in season/episode order with their best airing (latest catch-up/live + next upcoming).
    series: series id from search_guide, or give title instead.
    relative_to: an episode like "S04E25", or a listing/program id; with direction "earlier"
    (nearest earlier first) or "later" to step through episodes.
    only_watchable: only episodes playable right now (live or catch-up)."""
    with _db() as con:
        return q.episodes(con, series or None, title or None, relative_to or None, direction, only_watchable, limit)


@server.tool()
def channel_schedule(channel: str, day: str = "today", from_time: str = "") -> str:
    """Day schedule for one channel. channel: number ("1") or name ("ETV", "kanal 2").
    day: today | yesterday | tomorrow | weekday name (most recent, e.g. "sat") | +N/-N | YYYY-MM-DD.
    from_time: optional "HH:MM"."""
    with _db() as con:
        return q.channel_schedule(con, channel, day, from_time or None)


@server.tool()
def whats_on(at: str = "now", group: str = "", max_channel_number: int = 0) -> str:
    """What is on every subscribed channel at a moment, and what comes next.
    at: "now", "HH:MM" (today), "yesterday 21:00", "sat 20:30" or ISO datetime.
    group: channel group filter e.g. "Eesti kanalid". max_channel_number: e.g. 15."""
    with _db() as con:
        return q.whats_on(con, at, group or None, max_channel_number or None)


@server.tool()
def list_channels(group: str = "", all_channels: bool = False) -> str:
    """Subscribed channels (all_channels=True: every Elisa channel) with number, catch-up depth, groups."""
    with _db() as con:
        return q.channels(con, group or None, all_channels)


@server.tool()
def program_details(listing_id: str) -> str:
    """Full description and details of one airing."""
    with closing(guide.connect()) as con:
        r = q.listing(con, listing_id)
        if not r:
            return f"unknown listing {listing_id}"
        desc = con.execute("SELECT description FROM programs WHERE id=?", (r["program_id"],)).fetchone()[0]
        if not desc:
            try:
                desc = guide.fetch_program_details(con, r["program_id"]).get("description", "")
            except Exception as e:
                desc = f"(description unavailable: {e})"
        return "\n".join([q.airing_line(r, int(time.time())),
                          f"genres: {r['genres']}  year: {r['year']}  kind: {r['kind']}",
                          f"series={r['series_id']} program={r['program_id']}", r["tagline"] or "", desc or ""])


@server.tool()
def guide_status() -> str:
    """Local guide freshness, size, and Elisa login state."""
    with _db() as con:
        ls = guide.last_sync(con)
        n = con.execute("SELECT count(*), min(start), max(start) FROM listings").fetchone()
        nc = con.execute("SELECT count(*), sum(subscribed) FROM channels").fetchone()
    auth = guide.Auth().status()
    if not ls:
        return f"guide not synced yet; {auth}"
    return (f"last sync {q.fmt_time(ls)}; {nc[1]}/{nc[0]} channels subscribed; {n[0]} airings "
            f"from {q.fmt_time(n[1])} to {q.fmt_time(n[2])}; {auth}")


@server.tool()
def refresh_guide(full: bool = False) -> str:
    """Re-download the guide now (normally automatic every 2 hours). full=True re-fetches all 3 weeks."""
    return json.dumps(guide.sync(force=full))


# ============================================================================ TV

def _safe(fn):
    try:
        return fn()
    except TVError as e:
        return f"TV error: {e}"


@server.tool()
def tv_status() -> str:
    """TV power state, foreground app and volume."""
    return _safe(lambda: json.dumps(tv().status()))


@server.tool()
def tv_keys(keys: list[str], delay_ms: int = 0) -> str:
    """Press remote buttons in order. Names: home back up down left right ok menu info guide settings
    search input play_pause play pause stop rewind fast_forward next previous channel_up channel_down
    volume_up volume_down mute power red green yellow blue subtitles 0-9 (or raw KEYCODE_*).
    delay_ms: pause between presses (use ~300 when navigating menus)."""
    return _safe(lambda: tv().keys(keys, delay_ms))


@server.tool()
def tv_type_text(text: str) -> str:
    """Type text into the focused text field (e.g. a search box)."""
    return _safe(lambda: tv().text(text))


@server.tool()
def tv_power(state: str) -> str:
    """Turn the TV screen on, off (standby) or toggle. state: on | off | toggle."""
    return _safe(lambda: tv().power(state))


@server.tool()
def tv_volume(level: int = -1, delta: int = 0) -> str:
    """Set absolute volume (level >= 0) or change it by delta steps (+/-)."""
    return _safe(lambda: tv().volume(level if level >= 0 else None, delta or None))


@server.tool()
def tv_launch_app(app: str) -> str:
    """Open an app: elisa, youtube, jupiter (ERR), stremio, tunein, settings, home, or a package name."""
    return _safe(lambda: tv().launch(app))


@server.tool()
def tv_installed_apps() -> str:
    """User-installed app packages on the TV."""
    return _safe(lambda: "\n".join(tv().installed_apps()))


@server.tool()
def tv_screen() -> str:
    """Read the TV's current screen as text (UI labels; '>>' marks the focused item)."""
    return _safe(lambda: tv().screen())


@server.tool()
def tv_screenshot() -> Image | str:
    """Screenshot of the TV (JPEG, 960px wide). Video surfaces may appear black (DRM)."""
    try:
        return Image(data=tv().screenshot(), format="jpeg")
    except TVError as e:
        return f"TV error: {e}"


@server.tool()
def elisa_play(listing_id: str) -> str:
    """Play an airing in the Elisa app: a catch-up (past) episode/show, or a programme on now.
    Use the listing=<id> from search_guide / series_episodes / channel_schedule.
    Opens the programme in Elisa and presses play; a partly watched one continues where it was left.
    Takes ~10 s. Interrupts whatever is on screen."""
    with _db() as con:
        r = q.listing(con, listing_id)
    if not r:
        return f"unknown listing {listing_id}"
    now = int(time.time())
    st = q.status(r, now)
    if not r["subscribed"]:
        return f"{r['ch_name']} is not in the household subscription"
    if st == "upcoming":
        return f"not aired yet: starts {q.fmt_time(r['start'])}"
    if st == "expired":
        return "no longer available (outside the channel's catch-up window)"
    # Elisa labels episodes "H8 O12" in Estonian, "S8 E12" in English
    expect = [f"H{r['season']} O{r['episode']}", f"S{r['season']} E{r['episode']}"] \
        if r["season"] and r["episode"] else []
    t0 = time.time()
    res = _safe(lambda: tv().elisa_play(r["listing_id"], r["program_id"], expect))
    return f"{res} — {q.airing_line(r, now)} ({time.time() - t0:.0f} s)"


@server.tool()
def elisa_tune_channel(channel: str) -> str:
    """Switch Elisa LIVE TV to a channel (number or name, e.g. "ETV", "13") by typing its number like
    the remote does (number + Enter, switches at once), then confirming from the on-screen overlay. ~4 s."""
    with _db() as con:
        try:
            ch = q.find_channel(con, channel)
        except ValueError as e:
            return str(e)
    if not ch["subscribed"]:
        return f"{ch['name']} is not in the household subscription"
    with _db() as con:
        now = int(time.time())
        cur = con.execute("SELECT p.title FROM listings l JOIN programs p ON p.id=l.program_id "
                          "WHERE l.channel_id=? AND l.start<=? AND l.start+l.duration>?",
                          (ch["id"], now, now)).fetchone()
    t0 = time.time()
    res = _safe(lambda: tv().elisa_channel(ch["number"], cur[0] if cur else ""))
    return f"{res} ({ch['name']}, {time.time() - t0:.0f} s)"


# ============================================================================ runtime

def _background() -> None:
    while True:
        try:
            guide.Auth().keepalive()
        except Exception:
            log.exception("auth keepalive failed")
        try:
            with closing(guide.connect()) as con:
                due = time.time() - guide.last_sync(con) > guide.STALE_AFTER
            if due:
                guide.sync()
        except Exception:
            log.exception("background sync failed")
        time.sleep(600)


class ApiKeyMiddleware:
    """Require `Authorization: Bearer <api_key>` on every HTTP request except /health."""

    def __init__(self, app, api_key: str) -> None:
        self.app, self.expected = app, f"Bearer {api_key}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/health":
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"ok"})
                return
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"invalid or missing API key"}'})
                return
        await self.app(scope, receive, send)


def serve(stdio: bool = False) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    threading.Thread(target=_background, name="guide-sync", daemon=True).start()
    if stdio:
        server.run()
        return
    import uvicorn
    cfg = config.load()
    http = cfg["http"]
    if not http.get("api_key"):
        raise SystemExit("no API key configured; run `elisa-tv setup`")
    app = server.streamable_http_app(
        streamable_http_path="/mcp", host=http["host"],
        # access is controlled by the API key; LAN clients reach us by IP or hostname
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    log.info("serving MCP on http://%s:%s/mcp", http["host"], http["port"])
    uvicorn.run(ApiKeyMiddleware(app, http["api_key"]), host=http["host"], port=int(http["port"]),
                log_level="warning")
