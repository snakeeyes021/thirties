"""Rush tickets: check the user's theatre for rush tickets and tell them, daily, when there are some.

Many theatres sell rush tickets at the box office shortly before a show, at any performance that isn't sold out.
So rush tickets are likely on any day with a performance that isn't sold out, and the notice says which and from
when (an hour before, unless the user gives the theatre's own terms).

Off until the user turns it on (``tickets.rush``, on this computer only, so one machine checks), in chat ("check
the Juniper Hall listing for rush tickets") or in settings. The theatre's name, the page that lists its
performances (``tickets.listing_url``) and its rush terms ("$25 at the box office") are the user's to give, in
chat; with no listing page there's nothing to check. When it's on, the check is polite:

- at most one fetch of the listing a day (a failed one is retried at most twice, hours apart);
- the site's robots.txt is honoured, and the request carries a plain agent name and no personal details;
- a short timeout and a size cap; nothing runs at all while it's off.

A theatre's performance pages are often filled in by script, so the page to give is whichever one lists
performances with their availability. The parser reads any listing where each performance's text holds its date,
time and availability ("Sold Out", "Limited Availability"); it's tested on an invented page of that shape
(tests/fixtures_tickets).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SETTING = "tickets.rush"                 # off by default
THEATRE_SETTING = "tickets.theatre"      # the theatre's name, as the user gives it
URL_SETTING = "tickets.listing_url"      # the page that lists its performances
TERMS_SETTING = "tickets.rush_terms"     # its rush terms, as the user gives them ("$25 at the box office")
THEATRE = "the theatre"                  # what it's called when the user hasn't named it
AGENT = "Thirties (personal planner; one check a day)"
TIMEOUT = 20.0
MAX_BYTES = 2 * 1024 * 1024
CHECK_AFTER_HOUR = 10       # the day's listing, mid-morning
RETRY_HOURS = 3
MAX_TRIES = 3
RUSH_BEFORE = timedelta(hours=1)  # rush tickets go on sale this long before a show
STATE_KEY = "rush_tickets"  # sync meta: {"day", "tries", "last_try", "sent", "found": [...]}

Opener = Callable[[str], bytes]

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_DATE_RE = re.compile(r"\b(" + "|".join(m[:3] for m in _MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
                      r"(?:,?\s+(\d{4}))?", re.IGNORECASE)
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.IGNORECASE)
_SOLD_OUT_RE = re.compile(r"\bsold[\s-]*out\b|\bno availability\b|\bwait\s*list\b|\bunavailable\b", re.IGNORECASE)


@dataclass
class Performance:
    title: str
    starts: datetime
    sold_out: bool
    availability: str = ""


class _Listing(HTMLParser):
    """Collects the text of each element whose class names a performance, and the page's headings outside them."""

    VOID = ("br", "img", "input", "meta", "link", "hr", "source", "wbr")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[List[str]] = []
        self.titles: List[str] = []
        self._stack: List[tuple] = []     # (starts a performance, is a heading) for each open element
        self._heading: List[str] = []

    def _in(self, i: int) -> bool:
        return any(entry[i] for entry in self._stack)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.VOID:
            return
        cls = (dict(attrs).get("class") or "").lower()
        block = bool(re.search(r"(^|\s)performances?(-item|-row|-card)?(\s|$)", cls))
        heading = tag in ("h1", "h2") and not self._in(0) and not self._in(1)
        self._stack.append((block, heading))
        if block:
            self.blocks.append([])
        if heading:
            self._heading = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID or not self._stack:
            return
        _, heading = self._stack.pop()
        if heading:
            text = " ".join("".join(self._heading).split())
            if text:
                self.titles.append(text)

    def handle_data(self, data: str) -> None:
        if self._in(1):
            self._heading.append(data)
        if self._in(0) and self.blocks:
            self.blocks[-1].append(data)


def _when(text: str, today: date) -> Optional[datetime]:
    m = _DATE_RE.search(text)
    if m:
        month = next(i for i, name in enumerate(_MONTHS, 1) if name.startswith(m.group(1).lower()[:3]))
        day_n, year = int(m.group(2)), int(m.group(3)) if m.group(3) else None
    else:
        m = _NUM_DATE_RE.search(text)
        if not m:
            return None
        month, day_n = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
    try:
        when = date(year or today.year, month, day_n)
    except ValueError:
        return None
    if year is None and when < today - timedelta(days=180):
        when = when.replace(year=when.year + 1)
    t = _TIME_RE.search(text[m.end():]) or _TIME_RE.search(text)
    if not t:
        return None
    hour = int(t.group(1)) % 12 + (12 if t.group(3).lower() == "p" else 0)
    return datetime.combine(when, time(hour, int(t.group(2) or 0)))


def parse_performances(html: str, today: date) -> List[Performance]:
    """The performances a listing page shows: each with its title, start (naive, the theatre's local time) and
    whether it's sold out."""
    p = _Listing()
    p.feed(html or "")
    title = p.titles[0] if p.titles else THEATRE
    out = []
    for block in p.blocks:
        text = " ".join(" ".join(block).split())
        starts = _when(text, today)
        if starts is None:
            continue
        avail = next((a for a in ("Sold Out", "Limited Availability", "Good Availability", "Available")
                      if a.lower() in text.lower()), "")
        out.append(Performance(title, starts, bool(_SOLD_OUT_RE.search(text)), avail))
    return out


def rush_today(performances: List[Performance], today: date) -> List[Performance]:
    """Today's performances that aren't sold out: rush tickets are offered at those."""
    return sorted((p for p in performances if p.starts.date() == today and not p.sold_out), key=lambda p: p.starts)


def theatre_name(settings) -> str:
    """The theatre's name as the user gave it, or "the theatre"."""
    return str(settings.get(THEATRE_SETTING, "") or "").strip() or THEATRE


def notice(found: List[Performance], name: str = THEATRE, terms: str = "") -> str:
    from thirties_core.clock import format_clock
    shows = [f"{p.title}, {format_clock(p.starts)}" for p in found]
    first = found[0]
    lead = (f"🎭 Rush tickets at {name} today: {shows[0]}." if len(shows) == 1 else
            f"🎭 Rush tickets at {name} today:\n" + "\n".join(shows))
    terms = (terms or "").strip().rstrip(".")
    when = (f"from {format_clock(first.starts - RUSH_BEFORE)}"
            + (" (an hour before each show)" if len(shows) > 1 else ""))
    second = f"{terms[:1].upper()}{terms[1:]}, {when}" if terms else f"At the box office {when}"
    return lead + f"\n{second}, while they last."


# --- fetching, politely ---

def polite_opener(url: str) -> bytes:
    """One plain GET, after checking robots.txt: no cookies, no personal details, a short timeout, a size cap."""
    parts = urllib.parse.urlsplit(url)
    robots = urllib.robotparser.RobotFileParser(f"{parts.scheme}://{parts.netloc}/robots.txt")
    try:
        req = urllib.request.Request(robots.url, headers={"User-Agent": AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            robots.parse(resp.read(256 * 1024).decode("utf-8", "replace").splitlines())
    except Exception:
        robots.parse([])  # no robots.txt: nothing is disallowed
    if not robots.can_fetch(AGENT, url):
        raise PermissionError(f"{parts.netloc} asks automated readers not to fetch {parts.path}")
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(MAX_BYTES)


def _state(db) -> Dict[str, Any]:
    try:
        return json.loads(db.get_sync_meta(STATE_KEY) or "{}")
    except ValueError:
        return {}


def check(db, settings, now: datetime, opener: Optional[Opener] = None) -> Optional[List[Performance]]:
    """Today's rush-ticket performances, fetching the listing at most once a day (a failure is retried a couple of
    times, hours apart). None when it's off, not time yet, or the listing couldn't be read."""
    url = str(settings.get(URL_SETTING, "") or "").strip()
    if not settings.get(SETTING, False) or not url:
        return None
    today = now.date()
    state = _state(db)
    if state.get("day") != today.isoformat():
        state = {"day": today.isoformat(), "tries": 0}
    if "found" in state:
        return [Performance(f["title"], datetime.fromisoformat(f["starts"]), False) for f in state["found"]]
    if now.hour < CHECK_AFTER_HOUR or state.get("tries", 0) >= MAX_TRIES:
        return None
    last = state.get("last_try")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=RETRY_HOURS):
        return None
    state["tries"] = state.get("tries", 0) + 1
    state["last_try"] = now.isoformat()
    try:
        html = (opener or polite_opener)(url).decode("utf-8", "replace")
        found = rush_today(parse_performances(html, today), today)
    except Exception as e:
        logger.warning("Checking %s for rush tickets failed: %s", theatre_name(settings), e)
        db.set_sync_meta(STATE_KEY, json.dumps(state))
        return None
    state["found"] = [{"title": p.title, "starts": p.starts.isoformat()} for p in found]
    db.set_sync_meta(STATE_KEY, json.dumps(state))
    return found


def background_duty(service, now: datetime, opener: Optional[Opener] = None) -> Optional[str]:
    """The daily check, from the background service's tick: a notice once a day when there are rush tickets."""
    from thirties_core.settings import Settings
    db = service.scheduler.state_db
    settings = Settings(db)
    found = check(db, settings, now.replace(tzinfo=None) if now.tzinfo else now, opener)
    if not found:
        return None
    state = _state(db)
    if state.get("sent"):
        return None
    if found[-1].starts - RUSH_BEFORE < (now.replace(tzinfo=None) if now.tzinfo else now):
        return None  # the last one's rush window has started already: too late to plan around
    text = notice(found, theatre_name(settings), str(settings.get(TERMS_SETTING, "") or ""))
    service.send(text, "tickets", now=now)
    state["sent"] = True
    db.set_sync_meta(STATE_KEY, json.dumps(state))
    return text


# --- tools ---

TICKET_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "rush_tickets",
    "description": "Turn the daily check of the user's theatre for rush tickets on or off (on: a message on days "
                   "there are some), or ask whether there are any today. The theatre, the page listing its "
                   "performances and its rush terms are the user's to give; ask for the page when it isn't known.",
    "parameters": {"watch": "true to check every day, false to stop (leave out to ask about today).",
                   "theatre": "The theatre's name, as the user says it (optional).",
                   "listing": "The address of the page that lists its performances (optional).",
                   "terms": "Its rush terms as the user gives them, e.g. '$25 at the box office' (optional)."},
    "required": [],
}]


def tool(assistant, watch: Any = None, theatre: Any = None, listing: Any = None, terms: Any = None) -> str:
    from thirties_core.assistant_tools import ToolError
    from thirties_core.settings import Settings
    db = assistant._db
    if db is None:
        raise ToolError("This needs the app's storage.")
    settings = Settings(db)
    listing = str(listing or "").strip()
    if listing and not re.match(r"https?://[^\s/]+\.[^\s]+$", listing):
        raise ToolError(f"'{listing}' isn't a web address; give the page that lists the theatre's performances.")
    for key, value in ((THEATRE_SETTING, theatre), (URL_SETTING, listing), (TERMS_SETTING, terms)):
        if str(value or "").strip():
            settings.set(key, str(value).strip())
    name = theatre_name(settings)
    has_page = bool(str(settings.get(URL_SETTING, "") or "").strip())
    if watch not in (None, ""):
        on = str(watch).strip().lower() in ("true", "1", "yes", "on")
        settings.set(SETTING, on)
        if not on:
            return f"No more rush-ticket checks for {name}."
        if not has_page:
            return (f"Turned on, but there's no page to check yet: ask the user for the address of the page that "
                    f"lists {name}'s performances.")
        return f"Checking {name} for rush tickets every day from now on; a message comes on days there are some."
    if not settings.get(SETTING, False):
        return f"The daily rush-ticket check for {name} is off; say so to turn it on."
    if not has_page:
        return f"There's no page to check for {name} yet: ask the user for the page that lists its performances."
    found = check(db, settings, assistant.pt._now().replace(tzinfo=None))
    if found is None:
        return f"Today's {name} listing hasn't been read yet (it's checked once a day, from mid-morning)."
    if not found:
        return f"No rush tickets at {name} today: no performance, or it's sold out."
    return notice(found, name, str(settings.get(TERMS_SETTING, "") or ""))


__all__ = ["Performance", "parse_performances", "rush_today", "notice", "check", "background_duty", "tool",
           "TICKET_TOOL_SPECS", "SETTING", "THEATRE_SETTING", "URL_SETTING", "TERMS_SETTING", "theatre_name",
           "polite_opener"]
