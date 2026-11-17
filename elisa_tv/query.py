"""Fast read-only queries over the local guide database."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime, timedelta

from .guide import TZ

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------- helpers

def fmt_time(ts: int, with_day: bool = True) -> str:
    d = datetime.fromtimestamp(ts, TZ)
    if not with_day:
        return d.strftime("%H:%M")
    today = datetime.now(TZ).date()
    delta = (d.date() - today).days
    day = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(delta, d.strftime("%a %d.%m"))
    return f"{day} {d:%H:%M}"


def ep_label(season, episode) -> str:
    if season and episode:
        return f"S{season:02d}E{episode:02d}"
    if episode:
        return f"E{episode:02d}"
    return ""


def status(row, now: int) -> str:
    start, end = row["start"], row["start"] + row["duration"]
    if now < start:
        return "upcoming"
    if now < end:
        return "LIVE now" + (" (can restart)" if row["replay"] else "")
    if row["replay"] and now - start <= (row["replay_depth"] or 0):
        return "catch-up"
    return "expired"


def watchable(row, now: int) -> bool:
    s = status(row, now)
    return s.startswith("LIVE") or s == "catch-up"


def parse_day(day: str | None) -> date:
    today = datetime.now(TZ).date()
    s = (day or "today").strip().lower()
    if s in ("today", "täna", ""):
        return today
    if s in ("yesterday", "eile"):
        return today - timedelta(days=1)
    if s in ("tomorrow", "homme"):
        return today + timedelta(days=1)
    if re.fullmatch(r"[+-]?\d+", s):
        return today + timedelta(days=int(s))
    if s[:3] in WEEKDAYS:  # most recent such weekday (including today)
        back = (today.weekday() - WEEKDAYS.index(s[:3])) % 7
        return today - timedelta(days=back)
    return date.fromisoformat(s)


def parse_time(at: str | None) -> int:
    if not at or at.strip().lower() == "now":
        return int(time.time())
    s = at.strip()
    m = re.fullmatch(r"(?:(\S+)\s+)?(\d{1,2})[:.](\d{2})", s)
    if m:
        d = parse_day(m.group(1))
        return int(datetime(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)), tzinfo=TZ).timestamp())
    return int(datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp())


def fts_query(text: str) -> str:
    toks = re.findall(r"\w+", text, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in toks)


LISTING_SQL = """
SELECT l.id AS listing_id, l.start, l.duration, l.channel_id,
       c.number AS ch_num, c.name AS ch_name, c.replay, c.replay_depth, c.subscribed,
       p.id AS program_id, p.title, p.episode_title, p.tagline, p.kind, p.genres,
       p.series_id, p.season, p.episode, p.year
FROM listings l JOIN programs p ON p.id = l.program_id JOIN channels c ON c.id = l.channel_id
"""


def airing_line(r, now: int, show_title: bool = True) -> str:
    parts = [f"{fmt_time(r['start'])}-{fmt_time(r['start'] + r['duration'], False)}",
             f"[{r['ch_num']} {r['ch_name']}]"]
    if show_title:
        parts.append(r["title"])
    lab = ep_label(r["season"], r["episode"])
    if lab:
        parts.append(lab)
    if r["episode_title"] and r["episode_title"] != r["title"]:
        parts.append(f"“{r['episode_title']}”")
    parts.append(f"<{status(r, now)}>" if r["subscribed"] else f"<{status(r, now)}, NOT subscribed>")
    parts.append(f"listing={r['listing_id']}")
    return " ".join(parts)


def find_channel(con: sqlite3.Connection, channel: str) -> sqlite3.Row:
    c = channel.strip()
    if c.isdigit():
        row = con.execute("SELECT * FROM channels WHERE number=?", (int(c),)).fetchone()
        if row:
            return row
    row = con.execute("SELECT * FROM channels WHERE lower(name)=lower(?) OR id=?", (c, c)).fetchone()
    if row:
        return row
    rows = con.execute("SELECT * FROM channels WHERE name LIKE ? ORDER BY number", (f"%{c}%",)).fetchall()
    if len(rows) >= 1:
        return rows[0]
    raise ValueError(f"no channel matching {channel!r}")


# --------------------------------------------------------------------------- tools

def search(con: sqlite3.Connection, query: str, scope: str = "all", limit: int = 10,
           all_channels: bool = False) -> str:
    """Search shows; results grouped per show/series."""
    now = int(time.time())
    q = fts_query(query)
    if not q:
        return "empty query"
    rows = con.execute(f"""
        WITH m AS (SELECT program_id, bm25(programs_fts, 0, 10, 6, 3, 1, 1) AS rank
                   FROM programs_fts WHERE programs_fts MATCH ?)
        {LISTING_SQL} JOIN m ON m.program_id = p.id ORDER BY m.rank, l.start""", (q,)).fetchall()
    if not rows:  # fall back to plain substring on titles
        rows = con.execute(f"{LISTING_SQL} WHERE p.title LIKE ? OR p.title_original LIKE ? ORDER BY l.start",
                           (f"%{query}%", f"%{query}%")).fetchall()
    groups: dict[str, dict] = {}
    skipped = 0
    for r in rows:
        if not all_channels and not r["subscribed"]:
            skipped += 1
            continue
        st = status(r, now)
        if scope == "catchup" and not watchable(r, now):
            continue
        if scope == "upcoming" and st != "upcoming":
            continue
        if scope == "live" and not st.startswith("LIVE"):
            continue
        if st == "expired" and scope != "all":
            continue
        key = r["series_id"] or f"t:{r['title'].lower()}"
        g = groups.setdefault(key, {"r": r, "airings": [], "episodes": set()})
        g["airings"].append(r)
        g["episodes"].add((r["season"], r["episode"], r["episode_title"]))
    if not groups:
        extra = f" ({skipped} airings on channels outside the subscription; all_channels=True shows them)" if skipped else ""
        return f"No matches for {query!r} (scope={scope}).{extra}"
    out = []
    for g in list(groups.values())[:limit]:
        r = g["r"]
        air = g["airings"]
        live = [a for a in air if status(a, now).startswith("LIVE")]
        catch = [a for a in air if status(a, now) == "catch-up"]
        up = [a for a in air if status(a, now) == "upcoming"]
        head = f"■ {r['title']} ({r['kind'] or '?'}; {r['genres'] or '-'}"
        head += f"; {r['year']})" if r["year"] else ")"
        if r["series_id"]:
            head += f" series={r['series_id']}"
        out.append(head)
        out.append(f"  {len(g['episodes'])} distinct episodes/airings in guide: "
                   f"{len(live)} live, {len(catch)} catch-up, {len(up)} upcoming")
        for a in live[:2]:
            out.append("  " + airing_line(a, now, False))
        for a in sorted(catch, key=lambda a: -a["start"])[:3]:
            out.append("  " + airing_line(a, now, False))
        for a in up[:3]:
            out.append("  " + airing_line(a, now, False))
    if len(groups) > limit:
        out.append(f"... {len(groups) - limit} more shows matched")
    return "\n".join(out)


def _episode_key(r) -> tuple:
    if r["episode"]:
        return ("n", r["season"] or 0, r["episode"])
    if r["episode_title"] and r["episode_title"] != r["title"]:
        return ("t", r["episode_title"].lower())
    return ("p", r["program_id"])


def episodes(con: sqlite3.Connection, series: str | None = None, title: str | None = None,
             relative_to: str | None = None, direction: str | None = None,
             only_watchable: bool = False, limit: int = 15, all_channels: bool = False) -> str:
    """Episodes of a series, ordered by season/episode, with their best airing."""
    now = int(time.time())
    if not series:
        if not title:
            return "give series id or title"
        # candidate series whose title matches; prefer exact title, then the one containing
        # the referenced episode, then the one with the latest airing that has started
        cands = con.execute("""SELECT p.series_id, p.title, p.year, max(l.start) AS last,
                                      max(lower(p.title)=lower(?)) AS exact
                               FROM programs_fts f JOIN programs p ON p.id=f.program_id
                               JOIN listings l ON l.program_id=p.id
                               WHERE programs_fts MATCH ? AND p.series_id IS NOT NULL AND l.start<=?
                               GROUP BY p.series_id ORDER BY exact DESC, last DESC""",
                            (title.strip(), fts_query(title), now)).fetchall()
        if not cands:
            return f"No series found for {title!r}. Try search_guide()."
        series = cands[0]["series_id"]
        m = re.fullmatch(r"[sS](\d+)\s*[eE](\d+)", (relative_to or "").strip())
        for c in cands:
            if m and con.execute("SELECT 1 FROM programs WHERE series_id=? AND season=? AND episode=?",
                                 (c["series_id"], int(m.group(1)), int(m.group(2)))).fetchone():
                series = c["series_id"]
                break
            if relative_to and not m and con.execute(
                    "SELECT 1 FROM listings l JOIN programs p ON p.id=l.program_id WHERE p.series_id=? "
                    "AND (l.id=? OR p.id=?)", (c["series_id"], relative_to, relative_to)).fetchone():
                series = c["series_id"]
                break
        others = [c for c in cands if c["series_id"] != series and c["exact"]]
    else:
        others = []
    rows = con.execute(f"{LISTING_SQL} WHERE p.series_id=?" + ("" if all_channels else " AND c.subscribed=1")
                       + " ORDER BY l.start", (series,)).fetchall()
    if not rows:
        return f"No airings for series {series}."
    eps: dict[tuple, list] = {}
    for r in rows:
        eps.setdefault(_episode_key(r), []).append(r)

    def order(item):
        k, airs = item
        return (0, k[1], k[2]) if k[0] == "n" else (1, min(a["start"] for a in airs), 0)

    ordered = sorted(eps.items(), key=order)

    if relative_to:
        ref = relative_to.strip()
        m = re.fullmatch(r"[sS](\d+)\s*[eE](\d+)", ref)
        idx = None
        for i, (k, airs) in enumerate(ordered):
            if m and k[0] == "n" and (k[1], k[2]) == (int(m.group(1)), int(m.group(2))):
                idx = i
            elif any(ref in (a["listing_id"], a["program_id"]) for a in airs):
                idx = i
        if idx is None:
            return f"Reference {ref!r} not found in series {series}."
        if (direction or "earlier") == "earlier":
            ordered = list(reversed(ordered[:idx]))  # nearest earlier first
        else:
            ordered = ordered[idx + 1:]

    out = [f"■ {rows[0]['title']} series={series} — {len(eps)} episodes in guide window"]
    for c in others:
        out.append(f"  (another series with the same title: {c['title']} ({c['year'] or '?'}) series={c['series_id']})")
    shown = 0
    for k, airs in ordered:
        w = [a for a in airs if watchable(a, now)]
        up = [a for a in airs if status(a, now) == "upcoming"]
        if only_watchable and not w:
            continue
        a0 = airs[0]
        lab = ep_label(a0["season"], a0["episode"]) or "(no ep. number)"
        et = f" “{a0['episode_title']}”" if a0["episode_title"] and a0["episode_title"] != a0["title"] else ""
        out.append(f"• {lab}{et}" + (f" — {a0['tagline'][:160]}" if a0["tagline"] else ""))
        best = sorted(w, key=lambda a: -a["start"])[:1] + up[:1]
        if not best:
            best = sorted(airs, key=lambda a: -a["start"])[:1]
        for a in best:
            out.append("    " + airing_line(a, now, False))
        shown += 1
        if shown >= limit:
            break
    if shown == 0:
        out.append("(no matching episodes)")
    return "\n".join(out)


def channel_schedule(con: sqlite3.Connection, channel: str, day: str | None = None,
                     from_time: str | None = None) -> str:
    now = int(time.time())
    ch = find_channel(con, channel)
    d = parse_day(day)
    start = int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())
    end = int(datetime.combine(d + timedelta(days=1), datetime.min.time(), TZ).timestamp())
    if from_time:
        start = max(start, parse_time(f"{d.isoformat()}T{from_time}" if ":" in from_time and "T" not in from_time
                                      else from_time))
    rows = con.execute(f"{LISTING_SQL} WHERE l.channel_id=? AND l.start+l.duration>? AND l.start<? ORDER BY l.start",
                       (ch["id"], start, end)).fetchall()
    out = [f"■ {ch['number']} {ch['name']} — {d:%A %d.%m.%Y}"
           + (f" (catch-up {ch['replay_depth'] // 86400} days)" if ch["replay"] else " (no catch-up)")
           + ("" if ch["subscribed"] else " — NOT in the household subscription, cannot be watched")]
    for r in rows:
        lab = ep_label(r["season"], r["episode"])
        out.append(f"{fmt_time(r['start'], False)} {r['title']}" + (f" {lab}" if lab else "")
                   + (f" “{r['episode_title']}”" if r["episode_title"] and r["episode_title"] != r["title"] else "")
                   + f" <{status(r, now)}> listing={r['listing_id']}")
    if not rows:
        out.append("(no listings stored for this day)")
    return "\n".join(out)


def whats_on(con: sqlite3.Connection, at: str | None = None, group: str | None = None,
             max_number: int | None = None, all_channels: bool = False) -> str:
    t = parse_time(at)
    sql = f"{LISTING_SQL} WHERE l.start<=? AND l.start+l.duration>?"
    args: list = [t, t]
    if not all_channels:
        sql += " AND c.subscribed=1"
    if group:
        sql += " AND c.groups LIKE ?"
        args.append(f"%{group}%")
    if max_number:
        sql += " AND c.number<=?"
        args.append(max_number)
    rows = con.execute(sql + " ORDER BY c.number", args).fetchall()
    out = [f"On air at {fmt_time(t)}:"]
    for r in rows:
        nxt = con.execute("SELECT p.title, l.start FROM listings l JOIN programs p ON p.id=l.program_id "
                          "WHERE l.channel_id=? AND l.start>=? ORDER BY l.start LIMIT 1",
                          (r["channel_id"], r["start"] + r["duration"])).fetchone()
        lab = ep_label(r["season"], r["episode"])
        line = (f"{r['ch_num']:>3} {r['ch_name']}: {r['title']}" + (f" {lab}" if lab else "")
                + f" ({fmt_time(r['start'], False)}-{fmt_time(r['start'] + r['duration'], False)})")
        if nxt:
            line += f" → {fmt_time(nxt['start'], False)} {nxt['title']}"
        out.append(line + f" listing={r['listing_id']}")
    return "\n".join(out)


def channels(con: sqlite3.Connection, group: str | None = None, all_channels: bool = False) -> str:
    where = ["groups LIKE ?"] if group else []
    if not all_channels:
        where.append("subscribed=1")
    rows = con.execute("SELECT * FROM channels" + (" WHERE " + " AND ".join(where) if where else "")
                       + " ORDER BY number", ((f"%{group}%",) if group else ())).fetchall()
    return "\n".join(
        f"{r['number']:>3} {r['name']}"
        + (f" — catch-up {r['replay_depth'] // 86400}d" if r["replay"] else " — no catch-up")
        + (f" [{r['groups']}]" if r["groups"] else "")
        + ("" if r["subscribed"] else " (NOT in subscription)") for r in rows)


def listing(con: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return con.execute(f"{LISTING_SQL} WHERE l.id=?", (listing_id,)).fetchone()
