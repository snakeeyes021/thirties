"""Read-only "want to read / want to watch" list connectors.

Pulls public media wishlists without logins or API tokens:

* Goodreads shelves via the public shelf RSS feed
  ``https://www.goodreads.com/review/list_rss/<user_id>?shelf=<shelf>&page=<n>``
  (100 items per page; works only when the profile is public).
* Letterboxd watchlists by parsing the public HTML pages
  ``https://letterboxd.com/<username>/watchlist/page/<n>/`` (28 films per page).
  Letterboxd offers no watchlist RSS, and its JSON endpoints sit behind a
  Cloudflare challenge. Directors are not on the watchlist page; they are
  looked up from each film page (``twitter:data1`` meta tag), a few per refresh.
* Serializd watchlists via the JSON API used by the serializd.com web app,
  ``https://serializd.onrender.com/api/user/<username>/watchlistpage_v2/<n>``.
  It needs no token, only the header ``X-Requested-With: serializd_vercel``
  (without it the API answers 401). Premiere years come from
  ``/api/show/<tmdb_id>``. This API is unofficial and may change.

Every connector takes an injectable ``opener`` (``url -> bytes``) so tests never
touch the network, and never raises on network or parse errors: it logs and
returns what it collected, setting ``ok = False``.

Pure Python, standard library only, no GUI imports.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from thirties_core.models import TaskItem

logger = logging.getLogger(__name__)

Opener = Callable[[str], bytes]

# Letterboxd answers 403 to the default "Python-urllib/x.y" agent; a descriptive
# Mozilla-compatible agent is accepted by all three services.
USER_AGENT = "Mozilla/5.0 (compatible; Thirties/0.1; +https://github.com/snakeeyes021/thirties)"
DEFAULT_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

GOODREADS_BASE = "https://www.goodreads.com"
LETTERBOXD_BASE = "https://letterboxd.com"
SERIALIZD_API = "https://serializd.onrender.com/api"
SERIALIZD_WEB = "https://www.serializd.com"
SERIALIZD_HEADERS = {
    "X-Requested-With": "serializd_vercel",
    "Accept": "application/json, text/plain, */*",
    "Origin": SERIALIZD_WEB,
    "Referer": SERIALIZD_WEB + "/",
}

SOURCE_LABELS = {"goodreads": "Goodreads", "letterboxd": "Letterboxd", "serializd": "Serializd"}
KIND_VERBS = {"book": "Read", "film": "Watch", "show": "Watch"}
# Rough default session length in thirties: a film is ~2 h, a reading or
# episode session ~1 h. The scheduler can refine these from history.
KIND_ESTIMATES = {"book": 2, "film": 4, "show": 2}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class MediaItem:
    """One entry on a media wishlist."""
    source: str                       # "goodreads" | "letterboxd" | "serializd"
    kind: str                         # "book" | "film" | "show"
    title: str
    creator: str = ""                 # author / director when known
    year: Optional[int] = None
    url: str = ""
    list_name: str = ""               # e.g. "to-read", "watchlist"
    added_at: Optional[datetime] = None
    item_id: str = ""                 # stable per-source id (book id, film slug, TMDB id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "creator": self.creator,
            "year": self.year,
            "url": self.url,
            "list_name": self.list_name,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "item_id": self.item_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MediaItem:
        added = data.get("added_at")
        return cls(
            source=data["source"],
            kind=data["kind"],
            title=data["title"],
            creator=data.get("creator") or "",
            year=data.get("year"),
            url=data.get("url") or "",
            list_name=data.get("list_name") or "",
            added_at=datetime.fromisoformat(added) if added else None,
            item_id=data.get("item_id") or "",
        )


class MediaSource(Protocol):
    """Anything that can produce a list of MediaItems."""
    cache_key: str
    ok: bool

    def fetch(self) -> List[MediaItem]:
        ...


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_opener(extra_headers: Optional[Mapping[str, str]] = None,
                timeout: float = DEFAULT_TIMEOUT) -> Opener:
    """Return an ``url -> bytes`` opener using urllib with our User-Agent."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)

    def _open(url: str) -> bytes:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(MAX_RESPONSE_BYTES)

    return _open


class _BaseSource:
    source = ""
    kind = ""
    list_name = ""

    def __init__(self, opener: Optional[Opener], max_pages: int, detail_lookups: int,
                 default_headers: Optional[Mapping[str, str]] = None) -> None:
        self.opener: Opener = opener or http_opener(default_headers)
        self.max_pages = max(1, int(max_pages))
        self.detail_lookups = max(0, int(detail_lookups))
        # Previously fetched items (by url) whose creator/year can be reused,
        # so per-item detail lookups happen once per item, not every refresh.
        self.known: Dict[str, MediaItem] = {}
        self.ok = True

    @property
    def cache_key(self) -> str:
        raise NotImplementedError

    def _get(self, url: str) -> Optional[bytes]:
        try:
            return self.opener(url)
        except Exception as exc:  # network, HTTP status, timeouts: never raise
            logger.warning("%s: fetching %s failed: %s", self.source, url, exc)
            return None

    def _fill_from_known(self, item: MediaItem) -> None:
        prev = self.known.get(item.url)
        if prev is None:
            return
        if not item.creator and prev.creator:
            item.creator = prev.creator
        if item.year is None and prev.year is not None:
            item.year = prev.year

    def _enrich(self, items: List[MediaItem]) -> None:
        """Fill missing details from known items, then from up to N lookups."""
        budget = self.detail_lookups
        for item in items:
            self._fill_from_known(item)
            if budget <= 0 or not self._needs_details(item):
                continue
            budget -= 1
            try:
                self._lookup_details(item)
            except Exception as exc:
                logger.warning("%s: detail lookup for %s failed: %s", self.source, item.url, exc)

    def _needs_details(self, item: MediaItem) -> bool:
        return False

    def _lookup_details(self, item: MediaItem) -> None:
        pass


# --------------------------------------------------------------------------- #
# Goodreads
# --------------------------------------------------------------------------- #

def _parse_year(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\b(\d{4})\b", text)
    if not m:
        return None
    year = int(m.group(1))
    return year if year > 0 else None


def _parse_rfc822(text: Optional[str]) -> Optional[datetime]:
    if not text or not text.strip():
        return None
    try:
        return parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        return None


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_goodreads_rss(data: bytes, shelf: str) -> List[MediaItem]:
    """Parse one page of a Goodreads shelf RSS feed. Raises ET.ParseError on junk."""
    root = ET.fromstring(data)
    items: List[MediaItem] = []
    for node in root.iter("item"):
        def text(tag: str) -> str:
            return (node.findtext(tag) or "").strip()

        title = text("title")
        if not title:
            continue
        book_id = text("book_id")
        url = f"{GOODREADS_BASE}/book/show/{book_id}" if book_id else text("link")
        items.append(MediaItem(
            source="goodreads",
            kind="book",
            title=title,
            creator=text("author_name"),
            year=_parse_year(text("book_published")),
            url=url,
            list_name=shelf,
            added_at=_parse_rfc822(text("user_date_added")),
            item_id=book_id or url,
        ))
    return items


class GoodreadsShelf(_BaseSource):
    """A public Goodreads shelf, read from its RSS feed."""
    source = "goodreads"
    kind = "book"

    def __init__(self, user_id: str | int, shelf: str = "to-read", opener: Optional[Opener] = None,
                 max_pages: int = 20, per_page: int = 100) -> None:
        super().__init__(opener, max_pages, 0)
        # Accept "12345", "12345-jane-doe" or a full profile URL.
        m = re.search(r"(\d+)", str(user_id))
        self.user_id = m.group(1) if m else ""
        self.shelf = shelf or "to-read"
        self.list_name = self.shelf
        self.per_page = per_page

    @property
    def cache_key(self) -> str:
        return f"goodreads-{self.user_id}-{self.shelf}"

    def page_url(self, page: int) -> str:
        query = urllib.parse.urlencode({"shelf": self.shelf, "page": page})
        return f"{GOODREADS_BASE}/review/list_rss/{self.user_id}?{query}"

    def fetch(self) -> List[MediaItem]:
        self.ok = True
        if not self.user_id:
            logger.warning("goodreads: no numeric user id configured")
            self.ok = False
            return []
        results: List[MediaItem] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            data = self._get(self.page_url(page))
            if data is None:
                self.ok = False
                break
            try:
                page_items = parse_goodreads_rss(data, self.shelf)
            except ET.ParseError as exc:
                logger.warning("goodreads: could not parse shelf %r page %d: %s", self.shelf, page, exc)
                self.ok = False
                break
            new = [it for it in page_items if it.item_id not in seen]
            for it in new:
                seen.add(it.item_id)
            results.extend(new)
            if not new or len(page_items) < self.per_page:
                break
        else:
            logger.info("goodreads: stopped at page cap (%d pages)", self.max_pages)
        return results


# --------------------------------------------------------------------------- #
# Letterboxd
# --------------------------------------------------------------------------- #

_NAME_WITH_YEAR = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")


class _LetterboxdListParser(HTMLParser):
    """Collect film posters and the "next page" link from a Letterboxd list page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.films: List[Dict[str, str]] = []
        self.has_next = False
        self._pending_alt: Optional[Dict[str, str]] = None

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        slug = attrs.get("data-item-slug") or attrs.get("data-film-slug")
        if slug:
            film = {
                "slug": slug,
                "name": attrs.get("data-item-name") or attrs.get("data-film-name") or "",
                "link": attrs.get("data-item-link") or attrs.get("data-target-link")
                or attrs.get("data-film-link") or "",
                "year": attrs.get("data-film-release-year") or "",
            }
            self.films.append(film)
            # Older markup keeps the title only in the poster <img alt>.
            self._pending_alt = film if not film["name"] else None
            return
        if tag == "img" and self._pending_alt is not None:
            self._pending_alt["name"] = attrs.get("alt", "")
            self._pending_alt = None
        if tag == "a" and "next" in attrs.get("class", "").split() and attrs.get("href"):
            self.has_next = True


class _MetaParser(HTMLParser):
    """Collect <meta name|property=... content=...> tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        if tag != "meta":
            return
        attrs = {k: (v or "") for k, v in attrs_list}
        key = attrs.get("name") or attrs.get("property")
        if key and key not in self.meta:
            self.meta[key] = attrs.get("content", "")


def parse_letterboxd_list(html: str) -> Tuple[List[MediaItem], bool]:
    """Parse a Letterboxd watchlist page into (items, has_next_page)."""
    parser = _LetterboxdListParser()
    parser.feed(html)
    parser.close()
    items: List[MediaItem] = []
    for film in parser.films:
        name = film["name"].strip()
        year = _parse_year(film["year"])
        m = _NAME_WITH_YEAR.match(name)
        if m:
            name, year = m.group(1), year or int(m.group(2))
        link = film["link"] or f"/film/{film['slug']}/"
        url = link if link.startswith("http") else LETTERBOXD_BASE + link
        items.append(MediaItem(
            source="letterboxd",
            kind="film",
            title=name or film["slug"].replace("-", " ").title(),
            year=year,
            url=url,
            list_name="watchlist",
            item_id=film["slug"],
        ))
    return items, parser.has_next


def parse_letterboxd_director(html: str) -> str:
    """Extract the director(s) from a Letterboxd film page's meta tags."""
    parser = _MetaParser()
    parser.feed(html)
    parser.close()
    for n in ("1", "2"):
        if parser.meta.get(f"twitter:label{n}", "").lower().startswith("directed by"):
            return parser.meta.get(f"twitter:data{n}", "").strip()
    return ""


class LetterboxdWatchlist(_BaseSource):
    """A public Letterboxd watchlist, scraped from its HTML pages."""
    source = "letterboxd"
    kind = "film"
    list_name = "watchlist"

    def __init__(self, username: str, opener: Optional[Opener] = None, max_pages: int = 40,
                 detail_lookups: int = 0) -> None:
        super().__init__(opener, max_pages, detail_lookups)
        self.username = (username or "").strip().strip("/").lower()

    @property
    def cache_key(self) -> str:
        return f"letterboxd-{self.username}-watchlist"

    def page_url(self, page: int) -> str:
        user = urllib.parse.quote(self.username)
        if page <= 1:
            return f"{LETTERBOXD_BASE}/{user}/watchlist/"
        return f"{LETTERBOXD_BASE}/{user}/watchlist/page/{page}/"

    def fetch(self) -> List[MediaItem]:
        self.ok = True
        if not self.username:
            logger.warning("letterboxd: no username configured")
            self.ok = False
            return []
        results: List[MediaItem] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            data = self._get(self.page_url(page))
            if data is None:
                self.ok = False
                break
            page_items, has_next = parse_letterboxd_list(data.decode("utf-8", "replace"))
            new = [it for it in page_items if it.item_id not in seen]
            for it in new:
                seen.add(it.item_id)
            results.extend(new)
            if not new or not has_next:
                break
        else:
            logger.info("letterboxd: stopped at page cap (%d pages)", self.max_pages)
        self._enrich(results)
        return results

    def _needs_details(self, item: MediaItem) -> bool:
        return not item.creator

    def _lookup_details(self, item: MediaItem) -> None:
        data = self._get(item.url)
        if data is not None:
            item.creator = parse_letterboxd_director(data.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# Serializd
# --------------------------------------------------------------------------- #

def serializd_show_url(name: str, show_id: int | str) -> str:
    """Public show page URL in the form the web app uses: /show/<Name-Slug>-<id>."""
    slug = re.sub(r"['’]", "", name or "")
    slug = re.sub(r"[^\w\- ]", "", slug).strip()
    slug = re.sub(r"\s+", "-", slug)
    return f"{SERIALIZD_WEB}/show/{slug}-{show_id}" if slug else f"{SERIALIZD_WEB}/show/{show_id}"


def parse_serializd_watchlist(data: bytes) -> Tuple[List[MediaItem], int]:
    """Parse one watchlistpage_v2 response into (items, total_pages)."""
    payload = json.loads(data.decode("utf-8"))
    items: List[MediaItem] = []
    for entry in payload.get("items") or []:
        show_id = entry.get("showId")
        name = (entry.get("showName") or "").strip()
        if show_id is None or not name:
            continue
        items.append(MediaItem(
            source="serializd",
            kind="show",
            title=name,
            url=serializd_show_url(name, show_id),
            list_name="watchlist",
            added_at=_parse_iso(entry.get("dateAdded")),
            item_id=str(show_id),
        ))
    total = payload.get("totalPages")
    return items, total if isinstance(total, int) else 0


class SerializdWatchlist(_BaseSource):
    """A Serializd watchlist, read from the web app's unauthenticated JSON API."""
    source = "serializd"
    kind = "show"
    list_name = "watchlist"

    def __init__(self, username: str, opener: Optional[Opener] = None, max_pages: int = 20,
                 detail_lookups: int = 0) -> None:
        super().__init__(opener, max_pages, detail_lookups, default_headers=SERIALIZD_HEADERS)
        self.username = (username or "").strip().strip("/")

    @property
    def cache_key(self) -> str:
        return f"serializd-{self.username.lower()}-watchlist"

    def page_url(self, page: int) -> str:
        user = urllib.parse.quote(self.username)
        return f"{SERIALIZD_API}/user/{user}/watchlistpage_v2/{page}?sort_by=date_added_desc"

    def fetch(self) -> List[MediaItem]:
        self.ok = True
        if not self.username:
            logger.warning("serializd: no username configured")
            self.ok = False
            return []
        results: List[MediaItem] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            data = self._get(self.page_url(page))
            if data is None:
                self.ok = False
                break
            try:
                page_items, total_pages = parse_serializd_watchlist(data)
            except (ValueError, AttributeError) as exc:
                logger.warning("serializd: could not parse page %d: %s", page, exc)
                self.ok = False
                break
            new = [it for it in page_items if it.item_id not in seen]
            for it in new:
                seen.add(it.item_id)
            results.extend(new)
            if not new or page >= total_pages:
                break
        else:
            logger.info("serializd: stopped at page cap (%d pages)", self.max_pages)
        self._enrich(results)
        return results

    def _needs_details(self, item: MediaItem) -> bool:
        return item.year is None

    def _lookup_details(self, item: MediaItem) -> None:
        data = self._get(f"{SERIALIZD_API}/show/{urllib.parse.quote(item.item_id)}")
        if data is not None:
            show = json.loads(data.decode("utf-8"))
            item.year = _parse_year(show.get("premiereDate"))


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def default_cache_dir() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return cache_home / "thirties" / "media"


class MediaCache:
    """One JSON file per source under the cache directory."""

    def __init__(self, cache_dir: Optional[Path | str] = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()

    def _path(self, key: str) -> Path:
        return self.cache_dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")

    def load(self, key: str) -> Optional[Tuple[datetime, List[MediaItem]]]:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            items = [MediaItem.from_dict(d) for d in payload.get("items", [])]
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("media cache %s unreadable, ignoring: %s", path, exc)
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return fetched_at, items

    def save(self, key: str, items: Iterable[MediaItem], fetched_at: datetime) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "fetched_at": fetched_at.isoformat(),
                "items": [it.to_dict() for it in items],
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("could not write media cache %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def build_sources(media_config: Any, opener: Optional[Opener] = None) -> List[_BaseSource]:
    """Instantiate the sources enabled in a MediaConfig."""
    lookups = int(getattr(media_config, "detail_lookups_per_refresh", 0) or 0)
    sources: List[_BaseSource] = []
    if media_config.goodreads_user_id:
        shelves = list(dict.fromkeys(media_config.goodreads_shelves or ["to-read"]))
        for shelf in shelves:
            sources.append(GoodreadsShelf(media_config.goodreads_user_id, shelf, opener=opener))
    if media_config.letterboxd_username:
        sources.append(LetterboxdWatchlist(media_config.letterboxd_username, opener=opener,
                                           detail_lookups=lookups))
    if media_config.serializd_username:
        sources.append(SerializdWatchlist(media_config.serializd_username, opener=opener,
                                          detail_lookups=lookups))
    return sources


def fetch_all_media(config: Any, opener: Optional[Opener] = None, now: Optional[datetime] = None,
                    cache_dir: Optional[Path | str] = None) -> List[MediaItem]:
    """Fetch every configured media list, using the on-disk cache, and merge them.

    ``config`` may be a ThirtiesConfig (its ``media`` section is used) or a
    MediaConfig. A source is refetched only when its cache is older than
    ``cache_hours``. If a refetch fails, the stale cached list is used instead
    so a network outage never empties the backlog. Never raises.
    """
    media = getattr(config, "media", config)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ttl = timedelta(hours=float(getattr(media, "cache_hours", 12.0) or 0.0))
    cache = MediaCache(cache_dir)

    merged: List[MediaItem] = []
    for src in build_sources(media, opener):
        cached = cache.load(src.cache_key)
        if cached is not None:
            fetched_at, cached_items = cached
            if ttl > timedelta(0) and timedelta(0) <= now - fetched_at < ttl:
                merged.extend(cached_items)
                continue
            src.known = {it.url: it for it in cached_items}
        try:
            items = src.fetch()
        except Exception as exc:  # sources should not raise; be safe anyway
            logger.warning("%s: fetch failed: %s", src.cache_key, exc)
            items, src.ok = [], False
        if src.ok:
            cache.save(src.cache_key, items, now)
            merged.extend(items)
        elif cached is not None:
            logger.info("%s: refresh failed, using cached list from %s", src.cache_key, cached[0])
            merged.extend(cached[1])
        else:
            merged.extend(items)

    unique: List[MediaItem] = []
    seen: set[Tuple[str, str]] = set()
    for item in merged:
        key = (item.source, item.item_id or item.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def media_task_title(item: MediaItem) -> str:
    """'Watch: Shine (1996, Scott Hicks)' / 'Read: Dune (1965, Frank Herbert)'."""
    details = [str(item.year)] if item.year else []
    if item.creator:
        details.append(item.creator)
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{KIND_VERBS.get(item.kind, 'Do')}: {item.title}{suffix}"


def media_items_as_tasks(items: Iterable[MediaItem]) -> List[TaskItem]:
    """Turn media items into read-only backlog TaskItems."""
    tasks: List[TaskItem] = []
    seen: set[str] = set()
    for item in items:
        task_id = f"media:{item.source}:{item.item_id or item.url}"
        if task_id in seen:
            continue
        seen.add(task_id)
        label = SOURCE_LABELS.get(item.source, item.source.title())
        tasks.append(TaskItem(
            id=task_id,
            source_notebook=f"Media > {label} {item.list_name}".rstrip(),
            title=media_task_title(item),
            description=item.url,
            estimated_thirties=KIND_ESTIMATES.get(item.kind, 1),
            tags=["media", item.source, item.kind, "read-only"],
        ))
    return tasks
