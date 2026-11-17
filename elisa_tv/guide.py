"""Elisa Elamus TV guide: API client, auth, local SQLite store and sync.

The guide is fetched from Elisa's web frontend API (the one https://elisaelamus.ee/kava
uses) and stored locally so searches answer in milliseconds without the network.
Only channels in the household subscription are requested from the API.
"""
from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger("elisa_tv.guide")

TZ = ZoneInfo("Europe/Tallinn")
API = "https://www.elisaelamus.ee/api/frontend"
TOKEN_URL = "https://www.elisaelamus.ee/auth/realms/elamus/protocol/openid-connect/token"
CLIENT_ID = "browser"
HEADERS = {
    "x-operating-system": "browser",
    "x-device-type": "generic",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
}

PAST_DAYS = 14          # Elisa's guide exposes 14 days of history
FUTURE_DAYS = 7         # ...and 7 days ahead
REFRESH_PAST_DAYS = 1   # past days that are still re-fetched on every sync
STALE_AFTER = 2 * 3600  # seconds after which a sync is due
KEEP_DAYS = 31          # listings older than this are pruned

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS channels(
  id TEXT PRIMARY KEY, number INTEGER, name TEXT, replay INTEGER,
  replay_depth INTEGER, logo_url TEXT, groups TEXT, subscribed INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS programs(
  rid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, title TEXT, title_original TEXT, episode_title TEXT,
  tagline TEXT, description TEXT, kind TEXT, category TEXT, genres TEXT,
  series_id TEXT, season INTEGER, episode INTEGER, year INTEGER, image_url TEXT);
CREATE TABLE IF NOT EXISTS listings(
  id TEXT PRIMARY KEY, channel_id TEXT, program_id TEXT,
  start INTEGER, duration INTEGER, seen_at INTEGER);
CREATE INDEX IF NOT EXISTS listings_start ON listings(start);
CREATE INDEX IF NOT EXISTS listings_chan_start ON listings(channel_id, start);
CREATE INDEX IF NOT EXISTS listings_prog ON listings(program_id);
CREATE INDEX IF NOT EXISTS programs_series ON programs(series_id);
CREATE VIRTUAL TABLE IF NOT EXISTS programs_fts USING fts5(
  program_id UNINDEXED, title, title_original, episode_title, tagline, description,
  tokenize="unicode61 remove_diacritics 2");
CREATE TABLE IF NOT EXISTS fetched_days(day TEXT PRIMARY KEY, fetched_at INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if readonly and config.DB_PATH.exists():
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    else:
        con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------- auth

class Auth:
    """Elisa Keycloak tokens.

    "customer" mode uses the refresh token of a logged-in Elisa account (set up with
    `elisa-tv elisa-login`). Without it, or if it has expired, an anonymous guest token
    (what elisaelamus.ee uses for visitors) is used; the guide works either way.
    """

    def __init__(self) -> None:
        self.state: dict = {}
        if config.AUTH_PATH.exists():
            try:
                self.state = json.loads(config.AUTH_PATH.read_text())
            except ValueError:
                self.state = {}
        self.state.setdefault("device_uuid", str(uuid.uuid4()))
        self.state.setdefault("customer", None)
        self.state.setdefault("guest", None)

    def _save(self) -> None:
        config.write_secret(config.AUTH_PATH, self.state)

    @staticmethod
    def _post(body: dict) -> dict:
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(body).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=20) as r:
            t = json.load(r)
        now = time.time()
        return {"access_token": t["access_token"], "refresh_token": t.get("refresh_token"),
                "expires_at": now + t.get("expires_in", 300) - 30,
                "refresh_expires_at": (now + t["refresh_expires_in"] - 60) if t.get("refresh_expires_in")
                else None,  # 0/None = offline token without idle expiry
                "scope": t.get("scope", "")}

    def _refresh(self, kind: str) -> bool:
        tok = self.state.get(kind) or {}
        if not tok.get("refresh_token"):
            return False
        if tok.get("refresh_expires_at") and tok["refresh_expires_at"] < time.time():
            return False
        try:
            new = self._post({"grant_type": "refresh_token", "client_id": tok.get("client_id") or CLIENT_ID,
                              "refresh_token": tok["refresh_token"],
                              "device_uuid": tok.get("device_uuid") or self.state["device_uuid"]})
        except urllib.error.HTTPError as e:
            log.warning("%s token refresh failed: %s", kind, e)
            if kind == "customer" and e.code in (400, 401):
                tok["invalid"] = True
                self._save()
            return False
        new["device_uuid"] = tok.get("device_uuid") or self.state["device_uuid"]
        new["obtained_by"] = tok.get("obtained_by")
        new["client_id"] = tok.get("client_id") or CLIENT_ID
        self.state[kind] = new
        self._save()
        return True

    def customer_active(self) -> bool:
        c = self.state.get("customer")
        return bool(c and not c.get("invalid"))

    def link_customer(self, say=print) -> None:
        """Link the Elisa account with a pairing code (device flow, like the Elisa TV app)."""
        from . import device_login
        t = device_login.login(self.state["device_uuid"], say=say)
        now = time.time()
        self.state["customer"] = {
            "access_token": t["access_token"], "refresh_token": t["refresh_token"],
            "expires_at": now + t.get("expires_in", 300) - 30,
            "refresh_expires_at": (now + t["refresh_expires_in"] - 60) if t.get("refresh_expires_in") else None,
            "scope": t.get("scope", ""), "device_uuid": self.state["device_uuid"],
            "client_id": device_login.CLIENT_ID, "obtained_by": "device-code"}
        self._save()

    def clear_customer(self) -> None:
        self.state["customer"] = None
        self._save()

    def token(self) -> str:
        now = time.time()
        if self.customer_active():
            c = self.state["customer"]
            if c.get("access_token") and c.get("expires_at", 0) > now:
                return c["access_token"]
            if self._refresh("customer"):
                return self.state["customer"]["access_token"]
            log.warning("customer login unusable; falling back to guest token")
        g = self.state.get("guest") or {}
        if g.get("access_token") and g.get("expires_at", 0) > now:
            return g["access_token"]
        if self._refresh("guest"):
            return self.state["guest"]["access_token"]
        self.state["guest"] = self._post({"grant_type": "password", "client_id": CLIENT_ID, "scope": "guest",
                                          "device_uuid": self.state["device_uuid"]})
        self._save()
        return self.state["guest"]["access_token"]

    def keepalive(self) -> None:
        """Refresh the customer session before its refresh token idles out."""
        c = self.state.get("customer")
        if not self.customer_active():
            return
        exp = c.get("refresh_expires_at")
        if exp is None or exp - time.time() < 3600 or c.get("expires_at", 0) < time.time():
            self._refresh("customer")

    def invalidate(self) -> None:
        for k in ("customer", "guest"):
            if self.state.get(k):
                self.state[k]["expires_at"] = 0

    def status(self) -> str:
        c = self.state.get("customer")
        if not c:
            return "guest mode (no Elisa account linked)"
        if c.get("invalid"):
            return "Elisa account login EXPIRED — run `elisa-tv elisa-login` on the server; using guest mode"
        exp = c.get("refresh_expires_at")
        return "Elisa account linked" + (f"; session valid until {datetime.fromtimestamp(exp, TZ):%d.%m %H:%M} "
                                         "(kept alive automatically)" if exp else " (offline token)")


class Api:
    def __init__(self, auth: Auth | None = None) -> None:
        self.auth = auth or Auth()

    def get(self, path: str, **params) -> dict:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)  # lists -> repeated keys
        for attempt in range(4):
            req = urllib.request.Request(url, headers={**HEADERS, "Authorization": f"Bearer {self.auth.token()}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.auth.invalidate()
                elif e.code < 500 and e.code != 429:
                    raise
                log.warning("GET %s -> %s (attempt %d)", path, e.code, attempt + 1)
            except (urllib.error.URLError, TimeoutError) as e:
                log.warning("GET %s failed: %s (attempt %d)", path, e, attempt + 1)
            time.sleep(2 ** attempt)
        raise RuntimeError(f"GET {path} failed repeatedly")

    def paged(self, path: str, **params) -> list[dict]:
        out, page = [], 1
        while True:
            d = self.get(path, page=page, **params)
            out.extend(d.get("results", []))
            if page >= d.get("pages", 1):
                return out
            page += 1


# --------------------------------------------------------------------------- sync

def _day_start(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


PROGRAM_COLS = ("id", "title", "title_original", "episode_title", "tagline", "description", "kind",
                "category", "genres", "series_id", "season", "episode", "year", "image_url")


def _upsert_program(con: sqlite3.Connection, p: dict) -> None:
    old = con.execute(f"SELECT rid, {','.join(PROGRAM_COLS)} FROM programs WHERE id=?", (p["id"],)).fetchone()
    desc = p.get("description") or (old["description"] if old else "") or ""
    row = (p["id"], p.get("title") or "", p.get("title_original") or "", p.get("episode_title") or "",
           p.get("tagline") or "", desc, p.get("kind"), p.get("category"),
           ",".join(p.get("genres") or []), p.get("program_series_id"), p.get("season_number"),
           p.get("episode_number"), p.get("release_year"), p.get("image_url"))
    if old and tuple(old)[1:] == row:
        return
    if old:
        rid = old["rid"]
        con.execute(f"UPDATE programs SET {','.join(c + '=?' for c in PROGRAM_COLS)} WHERE rid=?", (*row, rid))
        con.execute("DELETE FROM programs_fts WHERE rowid=?", (rid,))
    else:
        rid = con.execute(f"INSERT INTO programs({','.join(PROGRAM_COLS)}) VALUES ({','.join('?' * len(row))})",
                          row).lastrowid
    con.execute("INSERT INTO programs_fts(rowid, program_id, title, title_original, episode_title, tagline, "
                "description) VALUES (?,?,?,?,?,?,?)", (rid, p["id"], row[1], row[2], row[3], row[4], desc))


def subscribed_ids(chans: list[dict], cfg: dict, account: bool) -> set[str]:
    sub = cfg["subscription"]
    src = sub.get("source", "all")
    if src == "account":
        if account:
            ent = {c["id"] for c in chans if c.get("live")}
            if ent:
                return ent
        log.warning("subscription source 'account' but no entitled channels visible; using all channels")
        return {c["id"] for c in chans}
    if src == "package":
        nums = set(config.packages().get(sub.get("package") or "", {}).get("channel_numbers", []))
    elif src == "custom":
        nums = set(sub.get("channel_numbers") or [])
    else:
        return {c["id"] for c in chans}
    return {c["id"] for c in chans if c.get("number") in nums}


def sync_channels(api: Api, con: sqlite3.Connection, cfg: dict) -> tuple[int, list[str]]:
    chans = api.paged("/channels")
    groups: dict[str, list[str]] = {}
    for g in api.get("/channel_groups").get("channel_groups", []):
        for cid in g.get("channel_ids", []):
            groups.setdefault(cid, []).append(g["title"])
    account = api.auth.customer_active()
    subs = subscribed_ids(chans, cfg, account)
    with con:
        for c in chans:
            # with a linked account the top-level flags are the household's real entitlements
            m = c if account and c.get("live") else (c.get("max") or {})
            con.execute("INSERT OR REPLACE INTO channels(id, number, name, replay, replay_depth, logo_url, groups, "
                        "subscribed) VALUES (?,?,?,?,?,?,?,?)", (
                            c["id"], c.get("number"), c.get("name"), int(bool(m.get("replay"))),
                            m.get("replay_depth"), c.get("logo_url"), ", ".join(groups.get(c["id"], [])),
                            int(c["id"] in subs)))
    return len(chans), sorted(subs)


def sync_day(api: Api, con: sqlite3.Connection, d: date, channel_ids: list[str]) -> int:
    """One day of guide for the given channels only (same channel_ids filter the website uses)."""
    start = _day_start(d)
    duration = _day_start(d + timedelta(days=1)) - start  # 23/24/25 h around DST
    rows = api.paged("/guide", start=start, duration=duration, channel_ids=channel_ids)
    wanted = set(channel_ids)
    kept = [r for r in rows if r.get("channel_id") in wanted]
    if len(kept) != len(rows):
        log.warning("%s: API returned %d listings for other channels (dropped)", d, len(rows) - len(kept))
    now = int(time.time())
    with con:
        for r in kept:
            p = r.get("program") or {}
            if not p.get("id"):
                continue
            _upsert_program(con, p)
            con.execute("INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?)",
                        (r["id"], r["channel_id"], p["id"], r["start"], r["duration"], now))
        ids = [r["id"] for r in kept]
        if ids:  # listings in this window no longer returned were cancelled/moved
            con.execute(f"DELETE FROM listings WHERE start>=? AND start<? AND seen_at<? "
                        f"AND id NOT IN ({','.join('?' * len(ids))})", (start, start + duration, now, *ids))
        con.execute("INSERT OR REPLACE INTO fetched_days VALUES (?,?)", (d.isoformat(), now))
    return len(kept)


def sync(force: bool = False, cfg: dict | None = None) -> dict:
    """Bring the local guide up to date. Safe to call concurrently (file lock)."""
    cfg = cfg or config.load()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOCK_PATH, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "another sync is already running"}
        t0 = time.time()
        api, con = Api(), connect()
        try:
            n, channel_ids = sync_channels(api, con, cfg)
            stats = {"channels": n, "subscribed_channels": len(channel_ids), "days_fetched": 0, "listings": 0}
            prev = con.execute("SELECT value FROM meta WHERE key='synced_channels'").fetchone()
            if prev is None or prev[0] != ",".join(channel_ids):
                force = True  # channel set changed: re-fetch everything
            with con:
                con.execute(f"DELETE FROM listings WHERE channel_id NOT IN ({','.join('?' * len(channel_ids))})",
                            channel_ids)
            today = datetime.now(TZ).date()
            fetched = {r["day"] for r in con.execute("SELECT day FROM fetched_days")}
            for off in range(-PAST_DAYS, FUTURE_DAYS + 1):
                d = today + timedelta(days=off)
                if off < -REFRESH_PAST_DAYS and d.isoformat() in fetched and not force:
                    continue
                stats["listings"] += sync_day(api, con, d, channel_ids)
                stats["days_fetched"] += 1
            cutoff = int(time.time()) - KEEP_DAYS * 86400
            with con:
                con.execute("DELETE FROM listings WHERE start<?", (cutoff,))
                con.execute("DELETE FROM fetched_days WHERE day<?",
                            ((today - timedelta(days=KEEP_DAYS)).isoformat(),))
                for (rid,) in con.execute(
                        "SELECT rid FROM programs WHERE id NOT IN (SELECT program_id FROM listings)").fetchall():
                    con.execute("DELETE FROM programs WHERE rid=?", (rid,))
                    con.execute("DELETE FROM programs_fts WHERE rowid=?", (rid,))
                con.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (str(int(time.time())),))
                con.execute("INSERT OR REPLACE INTO meta VALUES ('synced_channels', ?)", (",".join(channel_ids),))
            if force:
                con.execute("VACUUM")
            stats["seconds"] = round(time.time() - t0, 1)
            log.info("sync done: %s", stats)
            return stats
        finally:
            con.close()


def last_sync(con: sqlite3.Connection) -> int:
    r = con.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    return int(r[0]) if r else 0


def fetch_program_details(con: sqlite3.Connection, program_id: str) -> dict:
    p = Api().get(f"/programs/{program_id}")
    with con:
        _upsert_program(con, p)
    return p
