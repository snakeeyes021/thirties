"""Unit tests for thirties_core.media_sources.

Fixtures in tests/fixtures_media/ (no network access in tests):
- goodreads_*.xml: trimmed live responses (Oct 2026) of
  https://www.goodreads.com/review/list_rss/1?shelf=to-read[&page=2]
  (user 1 is Goodreads co-founder Otis Chandler, a public account).
- letterboxd_watchlist_*.html: trimmed live pages 1 and 22 of
  https://letterboxd.com/dave/watchlist/ (Letterboxd co-founder Dave Vis, public).
- letterboxd_film_*.html: <meta> tags of https://letterboxd.com/film/a-ghost-story-2017/.
- serializd_*.json: hand-written in the exact shape returned by
  https://serializd.onrender.com/api/user/<user>/watchlistpage_v2/<n> and /api/show/<id>.
"""

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError

from thirties_core.config import MediaConfig, ThirtiesConfig, load_config
from thirties_core.media_sources import (
    SERIALIZD_HEADERS,
    GoodreadsShelf,
    LetterboxdWatchlist,
    MediaItem,
    SerializdWatchlist,
    fetch_all_media,
    media_items_as_tasks,
)

FIXTURES = Path(__file__).parent / "fixtures_media"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeOpener:
    """Maps URLs to bytes (or exceptions); records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.routes:
            raise URLError(f"no route for {url}")
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


def failing_opener(url: str) -> bytes:
    raise URLError("network is down")


GR1 = "https://www.goodreads.com/review/list_rss/1?shelf=to-read&page=1"
GR2 = "https://www.goodreads.com/review/list_rss/1?shelf=to-read&page=2"
GR3 = "https://www.goodreads.com/review/list_rss/1?shelf=to-read&page=3"
LB1 = "https://letterboxd.com/dave/watchlist/"
LB2 = "https://letterboxd.com/dave/watchlist/page/2/"
LB_GHOST = "https://letterboxd.com/film/a-ghost-story-2017/"
SZ1 = "https://serializd.onrender.com/api/user/tvfan/watchlistpage_v2/1?sort_by=date_added_desc"
SZ2 = "https://serializd.onrender.com/api/user/tvfan/watchlistpage_v2/2?sort_by=date_added_desc"
SZ_SHOW = "https://serializd.onrender.com/api/show/1274"


class TestGoodreads(unittest.TestCase):

    def test_parses_shelf_rss(self) -> None:
        opener = FakeOpener({GR1: fixture("goodreads_to_read_page1.xml")})
        src = GoodreadsShelf("1-otis-chandler", opener=opener)
        items = src.fetch()
        self.assertTrue(src.ok)
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first.source, "goodreads")
        self.assertEqual(first.kind, "book")
        self.assertEqual(first.title, "Tender Is the Night")
        self.assertEqual(first.creator, "F. Scott Fitzgerald")
        self.assertEqual(first.year, 1934)
        self.assertEqual(first.url, "https://www.goodreads.com/book/show/46164")
        self.assertEqual(first.list_name, "to-read")
        self.assertEqual(first.item_id, "46164")
        self.assertEqual(first.added_at, datetime(2026, 10, 8, 22, 19, 5,
                                                  tzinfo=timezone(timedelta(hours=-7))))
        # CDATA titles and XML entities in author names are decoded.
        self.assertEqual(items[1].title, "A Thousand Brains: A New Theory of Intelligence")
        self.assertEqual(items[2].creator, "Colin O'Brady")
        # Fewer than a full page (100) means no second request.
        self.assertEqual(opener.calls, [GR1])

    def test_pagination_and_page_cap(self) -> None:
        routes = {GR1: fixture("goodreads_to_read_page1.xml"),
                  GR2: fixture("goodreads_to_read_page2.xml")}
        opener = FakeOpener(routes)
        items = GoodreadsShelf("1", opener=opener, per_page=3).fetch()
        # Page 2 has only 2 items (< per_page), so pagination stops there.
        self.assertEqual(len(items), 5)
        self.assertEqual(opener.calls, [GR1, GR2])
        self.assertEqual(items[3].title.split(":")[0], "Food Fix")

        capped = FakeOpener(routes)
        items = GoodreadsShelf("1", opener=capped, per_page=3, max_pages=1).fetch()
        self.assertEqual(len(items), 3)
        self.assertEqual(capped.calls, [GR1])

    def test_repeated_page_stops(self) -> None:
        page = fixture("goodreads_to_read_page1.xml")
        opener = FakeOpener({GR1: page, GR2: page, GR3: page})
        items = GoodreadsShelf("1", opener=opener, per_page=3).fetch()
        self.assertEqual(len(items), 3)
        self.assertEqual(opener.calls, [GR1, GR2])

    def test_network_failure_and_garbage(self) -> None:
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            src = GoodreadsShelf("1", opener=failing_opener)
            self.assertEqual(src.fetch(), [])
            self.assertFalse(src.ok)
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            src = GoodreadsShelf("1", opener=FakeOpener({GR1: b"404 - invalid user_id"}))
            self.assertEqual(src.fetch(), [])
            self.assertFalse(src.ok)

    def test_partial_failure_keeps_earlier_pages(self) -> None:
        opener = FakeOpener({GR1: fixture("goodreads_to_read_page1.xml")})
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            src = GoodreadsShelf("1", opener=opener, per_page=3)
            items = src.fetch()
        self.assertEqual(len(items), 3)
        self.assertFalse(src.ok)


class TestLetterboxd(unittest.TestCase):

    def test_parses_watchlist_page(self) -> None:
        opener = FakeOpener({LB1: fixture("letterboxd_watchlist_last_page.html")})
        src = LetterboxdWatchlist("Dave", opener=opener)
        items = src.fetch()
        self.assertTrue(src.ok)
        self.assertEqual([i.title for i in items], ["An Autumn Afternoon", "Nashville"])
        self.assertEqual([i.year for i in items], [1962, 1975])
        self.assertEqual(items[1].url, "https://letterboxd.com/film/nashville/")
        self.assertEqual(items[1].item_id, "nashville")
        self.assertEqual(items[1].kind, "film")
        self.assertEqual(items[1].list_name, "watchlist")
        self.assertEqual(items[1].creator, "")
        # The last page has no "Older" link, so no further requests.
        self.assertEqual(opener.calls, [LB1])

    def test_follows_next_link_and_page_cap(self) -> None:
        routes = {LB1: fixture("letterboxd_watchlist_page1.html"),
                  LB2: fixture("letterboxd_watchlist_last_page.html")}
        opener = FakeOpener(routes)
        items = LetterboxdWatchlist("dave", opener=opener).fetch()
        self.assertEqual(opener.calls, [LB1, LB2])
        self.assertEqual([i.item_id for i in items],
                         ["the-history-of-concrete", "a-ghost-story-2017", "naza",
                          "an-autumn-afternoon", "nashville"])
        self.assertEqual(items[1].title, "A Ghost Story")
        self.assertEqual(items[1].year, 2017)

        capped = FakeOpener(routes)
        items = LetterboxdWatchlist("dave", opener=capped, max_pages=1).fetch()
        self.assertEqual(len(items), 3)
        self.assertEqual(capped.calls, [LB1])

    def test_director_lookups_are_capped_and_reuse_known(self) -> None:
        routes = {LB1: fixture("letterboxd_watchlist_page1.html"),
                  LB2: fixture("letterboxd_watchlist_last_page.html"),
                  LB_GHOST: fixture("letterboxd_film_a-ghost-story-2017.html")}
        opener = FakeOpener(routes)
        src = LetterboxdWatchlist("dave", opener=opener, detail_lookups=2)
        src.known = {"https://letterboxd.com/film/the-history-of-concrete/":
                     MediaItem("letterboxd", "film", "The History of Concrete",
                               creator="John Wilson", url="https://letterboxd.com/film/the-history-of-concrete/")}
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            items = src.fetch()  # the naza page is missing: logged, not raised
        self.assertTrue(src.ok)
        self.assertEqual(items[0].creator, "John Wilson")   # from known, no request
        self.assertEqual(items[1].creator, "David Lowery")  # looked up
        self.assertEqual(items[2].creator, "")              # lookup failed
        film_calls = [c for c in opener.calls if "/film/" in c]
        self.assertEqual(film_calls, [LB_GHOST, "https://letterboxd.com/film/naza/"])

    def test_network_failure(self) -> None:
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            src = LetterboxdWatchlist("dave", opener=failing_opener)
            self.assertEqual(src.fetch(), [])
            self.assertFalse(src.ok)


class TestSerializd(unittest.TestCase):

    def test_parses_and_paginates(self) -> None:
        opener = FakeOpener({SZ1: fixture("serializd_watchlist_page1.json"),
                             SZ2: fixture("serializd_watchlist_page2.json")})
        src = SerializdWatchlist("tvfan", opener=opener)
        items = src.fetch()
        self.assertTrue(src.ok)
        self.assertEqual(opener.calls, [SZ1, SZ2])
        self.assertEqual([i.title for i in items], ["Six Feet Under", "The Wire", "Sherlock"])
        first = items[0]
        self.assertEqual(first.source, "serializd")
        self.assertEqual(first.kind, "show")
        self.assertEqual(first.item_id, "1274")
        self.assertEqual(first.url, "https://www.serializd.com/show/Six-Feet-Under-1274")
        self.assertEqual(first.added_at, datetime(2024, 5, 20, 3, 57, 9, tzinfo=timezone.utc))
        self.assertIsNone(first.year)

    def test_page_cap(self) -> None:
        opener = FakeOpener({SZ1: fixture("serializd_watchlist_page1.json")})
        items = SerializdWatchlist("tvfan", opener=opener, max_pages=1).fetch()
        self.assertEqual(len(items), 2)
        self.assertEqual(opener.calls, [SZ1])

    def test_year_lookup(self) -> None:
        opener = FakeOpener({SZ1: fixture("serializd_watchlist_page1.json"),
                             SZ2: fixture("serializd_watchlist_page2.json"),
                             SZ_SHOW: fixture("serializd_show_1274.json")})
        items = SerializdWatchlist("tvfan", opener=opener, detail_lookups=1).fetch()
        self.assertEqual(items[0].year, 2001)
        self.assertIsNone(items[1].year)
        self.assertEqual(opener.calls.count(SZ_SHOW), 1)

    def test_unauthorized_or_failure(self) -> None:
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            src = SerializdWatchlist("tvfan", opener=FakeOpener({SZ1: b"<!DOCTYPE html>"}))
            self.assertEqual(src.fetch(), [])
            self.assertFalse(src.ok)
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            self.assertEqual(SerializdWatchlist("tvfan", opener=failing_opener).fetch(), [])

    def test_required_header_is_declared(self) -> None:
        self.assertEqual(SERIALIZD_HEADERS["X-Requested-With"], "serializd_vercel")


class TestFetchAllMediaAndCache(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name)
        self.config = ThirtiesConfig(media=MediaConfig(
            goodreads_user_id="1", letterboxd_username="dave", serializd_username="tvfan",
            cache_hours=12.0, detail_lookups_per_refresh=0))
        self.routes = {GR1: fixture("goodreads_to_read_page1.xml"),
                       LB1: fixture("letterboxd_watchlist_last_page.html"),
                       SZ1: fixture("serializd_watchlist_page1.json"),
                       SZ2: fixture("serializd_watchlist_page2.json")}
        self.now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merges_sources_and_writes_cache(self) -> None:
        opener = FakeOpener(self.routes)
        items = fetch_all_media(self.config, opener=opener, now=self.now, cache_dir=self.cache_dir)
        self.assertEqual([i.source for i in items],
                         ["goodreads"] * 3 + ["letterboxd"] * 2 + ["serializd"] * 3)
        files = sorted(p.name for p in self.cache_dir.iterdir())
        self.assertEqual(files, ["goodreads-1-to-read.json", "letterboxd-dave-watchlist.json",
                                 "serializd-tvfan-watchlist.json"])
        payload = json.loads((self.cache_dir / "goodreads-1-to-read.json").read_text())
        self.assertEqual(payload["fetched_at"], self.now.isoformat())
        self.assertEqual(payload["items"][0]["title"], "Tender Is the Night")

    def test_cache_hit_and_expiry(self) -> None:
        fetch_all_media(self.config, opener=FakeOpener(self.routes), now=self.now,
                        cache_dir=self.cache_dir)

        # Within the TTL: no network at all, identical items (datetimes round-trip).
        offline = FakeOpener({})
        first = fetch_all_media(self.config, opener=FakeOpener(self.routes), now=self.now,
                                cache_dir=self.cache_dir)
        cached = fetch_all_media(self.config, opener=offline, now=self.now + timedelta(hours=11),
                                 cache_dir=self.cache_dir)
        self.assertEqual(offline.calls, [])
        self.assertEqual(cached, first)

        # Past the TTL: everything is refetched.
        online = FakeOpener(self.routes)
        fetch_all_media(self.config, opener=online, now=self.now + timedelta(hours=13),
                        cache_dir=self.cache_dir)
        self.assertEqual(sorted(online.calls), sorted([GR1, LB1, SZ1, SZ2]))

    def test_failed_refresh_falls_back_to_stale_cache(self) -> None:
        fresh = fetch_all_media(self.config, opener=FakeOpener(self.routes), now=self.now,
                                cache_dir=self.cache_dir)
        later = self.now + timedelta(days=2)
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            stale = fetch_all_media(self.config, opener=failing_opener, now=later,
                                    cache_dir=self.cache_dir)
        self.assertEqual(stale, fresh)
        # The stale cache was not overwritten, so the next attempt retries.
        payload = json.loads((self.cache_dir / "letterboxd-dave-watchlist.json").read_text())
        self.assertEqual(payload["fetched_at"], self.now.isoformat())

    def test_network_failure_without_cache_returns_empty(self) -> None:
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            items = fetch_all_media(self.config, opener=failing_opener, now=self.now,
                                    cache_dir=self.cache_dir)
        self.assertEqual(items, [])
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_known_details_carry_forward_after_expiry(self) -> None:
        cfg = ThirtiesConfig(media=MediaConfig(letterboxd_username="dave",
                                               detail_lookups_per_refresh=5))
        routes = {LB1: fixture("letterboxd_watchlist_page1.html"),
                  LB2: fixture("letterboxd_watchlist_last_page.html"),
                  LB_GHOST: fixture("letterboxd_film_a-ghost-story-2017.html")}
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            fetch_all_media(cfg, opener=FakeOpener(routes), now=self.now, cache_dir=self.cache_dir)
        later = FakeOpener(routes)
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            items = fetch_all_media(cfg, opener=later, now=self.now + timedelta(days=1),
                                    cache_dir=self.cache_dir)
        ghost = next(i for i in items if i.item_id == "a-ghost-story-2017")
        self.assertEqual(ghost.creator, "David Lowery")
        self.assertNotIn(LB_GHOST, later.calls)

    def test_nothing_configured(self) -> None:
        opener = FakeOpener({})
        self.assertEqual(fetch_all_media(ThirtiesConfig(), opener=opener, now=self.now,
                                         cache_dir=self.cache_dir), [])
        self.assertEqual(opener.calls, [])

    def test_multiple_shelves_dedupe(self) -> None:
        cfg = MediaConfig(goodreads_user_id="1", goodreads_shelves=["to-read", "favorites"])
        fav = "https://www.goodreads.com/review/list_rss/1?shelf=favorites&page=1"
        opener = FakeOpener({GR1: fixture("goodreads_to_read_page1.xml"),
                             fav: fixture("goodreads_to_read_page1.xml")})
        items = fetch_all_media(cfg, opener=opener, now=self.now, cache_dir=self.cache_dir)
        self.assertEqual(len(items), 3)
        self.assertEqual(opener.calls, [GR1, fav])

    def test_default_cache_dir_uses_xdg(self) -> None:
        import os
        from unittest import mock
        from thirties_core.media_sources import default_cache_dir
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/tmp/xdg-test"}):
            self.assertEqual(default_cache_dir(), Path("/tmp/xdg-test/thirties/media"))


class TestMediaConfig(unittest.TestCase):

    def test_defaults(self) -> None:
        cfg = load_config(Path("/nonexistent/path/config.toml"))
        self.assertEqual(cfg.media.goodreads_user_id, "")
        self.assertEqual(cfg.media.goodreads_shelves, ["to-read"])
        self.assertEqual(cfg.media.letterboxd_username, "")
        self.assertEqual(cfg.media.serializd_username, "")
        self.assertEqual(cfg.media.cache_hours, 12.0)

    def test_load_media_section(self) -> None:
        toml_content = """
[media]
goodreads_user_id = "12345"
goodreads_shelves = ["to-read", "currently-reading"]
letterboxd_username = "dave"
serializd_username = "tvfan"
cache_hours = 6.0
"""
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml_content)
            path = Path(f.name)
        try:
            cfg = load_config(path)
        finally:
            path.unlink()
        self.assertEqual(cfg.media.goodreads_user_id, "12345")
        self.assertEqual(cfg.media.goodreads_shelves, ["to-read", "currently-reading"])
        self.assertEqual(cfg.media.letterboxd_username, "dave")
        self.assertEqual(cfg.media.serializd_username, "tvfan")
        self.assertEqual(cfg.media.cache_hours, 6.0)

    def test_example_config_parses(self) -> None:
        example = Path(__file__).parent.parent / "config.example.toml"
        cfg = load_config(example)
        self.assertEqual(cfg.media, MediaConfig())


class TestMediaItemsAsTasks(unittest.TestCase):

    def test_formatting(self) -> None:
        items = [
            MediaItem("letterboxd", "film", "Shine", creator="Scott Hicks", year=1996,
                      url="https://letterboxd.com/film/shine/", list_name="watchlist", item_id="shine"),
            MediaItem("goodreads", "book", "Tender Is the Night", creator="F. Scott Fitzgerald",
                      year=1934, url="https://www.goodreads.com/book/show/46164",
                      list_name="to-read", item_id="46164"),
            MediaItem("serializd", "show", "Six Feet Under", year=2001,
                      url="https://www.serializd.com/show/Six-Feet-Under-1274",
                      list_name="watchlist", item_id="1274"),
            MediaItem("letterboxd", "film", "NAZA", url="https://letterboxd.com/film/naza/",
                      list_name="watchlist", item_id="naza"),
            # Duplicate id is dropped.
            MediaItem("letterboxd", "film", "Shine", list_name="watchlist", item_id="shine"),
        ]
        tasks = media_items_as_tasks(items)
        self.assertEqual(len(tasks), 4)
        shine, book, show, naza = tasks
        self.assertEqual(shine.id, "media:letterboxd:shine")
        self.assertEqual(shine.source_notebook, "Media > Letterboxd watchlist")
        self.assertEqual(shine.title, "Watch: Shine (1996, Scott Hicks)")
        self.assertEqual(shine.description, "https://letterboxd.com/film/shine/")
        self.assertEqual(shine.estimated_thirties, 4)
        self.assertIn("read-only", shine.tags)
        self.assertEqual(book.id, "media:goodreads:46164")
        self.assertEqual(book.source_notebook, "Media > Goodreads to-read")
        self.assertEqual(book.title, "Read: Tender Is the Night (1934, F. Scott Fitzgerald)")
        self.assertEqual(show.id, "media:serializd:1274")
        self.assertEqual(show.source_notebook, "Media > Serializd watchlist")
        self.assertEqual(show.title, "Watch: Six Feet Under (2001)")
        self.assertEqual(naza.title, "Watch: NAZA")
        self.assertFalse(shine.is_checkbox_item)


if __name__ == "__main__":
    unittest.main()
