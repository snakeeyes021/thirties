"""Unit tests for thirties_core.media_sources.

Fixtures in tests/fixtures_media/ (no network access in tests) are synthetic: made-up titles, people and ids, written
in the shape of each source's responses:
- goodreads_*.xml: a shelf's RSS feed, pages 1 and 2 (/review/list_rss/<user>?shelf=to-read[&page=2]).
- letterboxd_watchlist_*.html: a watchlist's poster grid, the first and the last page (/<user>/watchlist/).
- letterboxd_film_*.html: the <meta> tags of a film page.
- serializd_*.json: /api/user/<user>/watchlistpage_v2/<n> and /api/show/<id>.
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
LB1 = "https://letterboxd.com/samplefan/watchlist/"
LB2 = "https://letterboxd.com/samplefan/watchlist/page/2/"
LB_FILM = "https://letterboxd.com/film/a-quiet-orchard-2017/"
SZ1 = "https://serializd.onrender.com/api/user/tvfan/watchlistpage_v2/1?sort_by=date_added_desc"
SZ2 = "https://serializd.onrender.com/api/user/tvfan/watchlistpage_v2/2?sort_by=date_added_desc"
SZ_SHOW = "https://serializd.onrender.com/api/show/7001"


class TestGoodreads(unittest.TestCase):

    def test_parses_shelf_rss(self) -> None:
        opener = FakeOpener({GR1: fixture("goodreads_to_read_page1.xml")})
        src = GoodreadsShelf("1-sample-reader", opener=opener)
        items = src.fetch()
        self.assertTrue(src.ok)
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first.source, "goodreads")
        self.assertEqual(first.kind, "book")
        self.assertEqual(first.title, "The Lantern Almanac")
        self.assertEqual(first.creator, "Mara Ellingsen")
        self.assertEqual(first.year, 1934)
        self.assertEqual(first.url, "https://www.goodreads.com/book/show/900101")
        self.assertEqual(first.list_name, "to-read")
        self.assertEqual(first.item_id, "900101")
        self.assertEqual(first.added_at, datetime(2026, 10, 8, 22, 19, 5,
                                                  tzinfo=timezone(timedelta(hours=-7))))
        # CDATA titles and XML entities in author names are decoded.
        self.assertEqual(items[1].title, "A Field Guide to Quiet Machines: Notes on Thinking")
        self.assertEqual(items[2].creator, "Rowan O'Dell")
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
        self.assertEqual(items[3].title.split(":")[0], "Pantry Reset")

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
        src = LetterboxdWatchlist("SampleFan", opener=opener)
        items = src.fetch()
        self.assertTrue(src.ok)
        self.assertEqual([i.title for i in items], ["A Winter Harbour", "Millbrook"])
        self.assertEqual([i.year for i in items], [1962, 1975])
        self.assertEqual(items[1].url, "https://letterboxd.com/film/millbrook/")
        self.assertEqual(items[1].item_id, "millbrook")
        self.assertEqual(items[1].kind, "film")
        self.assertEqual(items[1].list_name, "watchlist")
        self.assertEqual(items[1].creator, "")
        # The last page has no "Older" link, so no further requests.
        self.assertEqual(opener.calls, [LB1])

    def test_follows_next_link_and_page_cap(self) -> None:
        routes = {LB1: fixture("letterboxd_watchlist_page1.html"),
                  LB2: fixture("letterboxd_watchlist_last_page.html")}
        opener = FakeOpener(routes)
        items = LetterboxdWatchlist("samplefan", opener=opener).fetch()
        self.assertEqual(opener.calls, [LB1, LB2])
        self.assertEqual([i.item_id for i in items],
                         ["the-paper-lighthouse", "a-quiet-orchard-2017", "vela",
                          "a-winter-harbour", "millbrook"])
        self.assertEqual(items[1].title, "A Quiet Orchard")
        self.assertEqual(items[1].year, 2017)

        capped = FakeOpener(routes)
        items = LetterboxdWatchlist("samplefan", opener=capped, max_pages=1).fetch()
        self.assertEqual(len(items), 3)
        self.assertEqual(capped.calls, [LB1])

    def test_director_lookups_are_capped_and_reuse_known(self) -> None:
        routes = {LB1: fixture("letterboxd_watchlist_page1.html"),
                  LB2: fixture("letterboxd_watchlist_last_page.html"),
                  LB_FILM: fixture("letterboxd_film_a-quiet-orchard-2017.html")}
        opener = FakeOpener(routes)
        src = LetterboxdWatchlist("samplefan", opener=opener, detail_lookups=2)
        src.known = {"https://letterboxd.com/film/the-paper-lighthouse/":
                     MediaItem("letterboxd", "film", "The Paper Lighthouse",
                               creator="Theo Marsh", url="https://letterboxd.com/film/the-paper-lighthouse/")}
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            items = src.fetch()  # the vela page is missing: logged, not raised
        self.assertTrue(src.ok)
        self.assertEqual(items[0].creator, "Theo Marsh")   # from known, no request
        self.assertEqual(items[1].creator, "Ines Calder")  # looked up
        self.assertEqual(items[2].creator, "")              # lookup failed
        film_calls = [c for c in opener.calls if "/film/" in c]
        self.assertEqual(film_calls, [LB_FILM, "https://letterboxd.com/film/vela/"])

    def test_network_failure(self) -> None:
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            src = LetterboxdWatchlist("samplefan", opener=failing_opener)
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
        self.assertEqual([i.title for i in items], ["Harbor Lights", "The Long Shift", "Quayside"])
        first = items[0]
        self.assertEqual(first.source, "serializd")
        self.assertEqual(first.kind, "show")
        self.assertEqual(first.item_id, "7001")
        self.assertEqual(first.url, "https://www.serializd.com/show/Harbor-Lights-7001")
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
                             SZ_SHOW: fixture("serializd_show_7001.json")})
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
            goodreads_user_id="1", letterboxd_username="samplefan", serializd_username="tvfan",
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
        self.assertEqual(files, ["goodreads-1-to-read.json", "letterboxd-samplefan-watchlist.json",
                                 "serializd-tvfan-watchlist.json"])
        payload = json.loads((self.cache_dir / "goodreads-1-to-read.json").read_text())
        self.assertEqual(payload["fetched_at"], self.now.isoformat())
        self.assertEqual(payload["items"][0]["title"], "The Lantern Almanac")

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
        payload = json.loads((self.cache_dir / "letterboxd-samplefan-watchlist.json").read_text())
        self.assertEqual(payload["fetched_at"], self.now.isoformat())

    def test_network_failure_without_cache_returns_empty(self) -> None:
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            items = fetch_all_media(self.config, opener=failing_opener, now=self.now,
                                    cache_dir=self.cache_dir)
        self.assertEqual(items, [])
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_known_details_carry_forward_after_expiry(self) -> None:
        cfg = ThirtiesConfig(media=MediaConfig(letterboxd_username="samplefan",
                                               detail_lookups_per_refresh=5))
        routes = {LB1: fixture("letterboxd_watchlist_page1.html"),
                  LB2: fixture("letterboxd_watchlist_last_page.html"),
                  LB_FILM: fixture("letterboxd_film_a-quiet-orchard-2017.html")}
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            fetch_all_media(cfg, opener=FakeOpener(routes), now=self.now, cache_dir=self.cache_dir)
        later = FakeOpener(routes)
        with self.assertLogs("thirties_core.media_sources", logging.WARNING):
            items = fetch_all_media(cfg, opener=later, now=self.now + timedelta(days=1),
                                    cache_dir=self.cache_dir)
        orchard = next(i for i in items if i.item_id == "a-quiet-orchard-2017")
        self.assertEqual(orchard.creator, "Ines Calder")
        self.assertNotIn(LB_FILM, later.calls)

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
letterboxd_username = "samplefan"
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
        self.assertEqual(cfg.media.letterboxd_username, "samplefan")
        self.assertEqual(cfg.media.serializd_username, "tvfan")
        self.assertEqual(cfg.media.cache_hours, 6.0)

    def test_example_config_parses(self) -> None:
        example = Path(__file__).parent.parent / "config.example.toml"
        cfg = load_config(example)
        self.assertEqual(cfg.media, MediaConfig())


class TestMediaItemsAsTasks(unittest.TestCase):

    def test_formatting(self) -> None:
        items = [
            MediaItem("letterboxd", "film", "Low Tide", creator="Ada Brook", year=1996,
                      url="https://letterboxd.com/film/low-tide/", list_name="watchlist", item_id="low-tide"),
            MediaItem("goodreads", "book", "The Lantern Almanac", creator="Mara Ellingsen",
                      year=1934, url="https://www.goodreads.com/book/show/900101",
                      list_name="to-read", item_id="900101"),
            MediaItem("serializd", "show", "Harbor Lights", year=2001,
                      url="https://www.serializd.com/show/Harbor-Lights-7001",
                      list_name="watchlist", item_id="7001"),
            MediaItem("letterboxd", "film", "VELA", url="https://letterboxd.com/film/vela/",
                      list_name="watchlist", item_id="vela"),
            # Duplicate id is dropped.
            MediaItem("letterboxd", "film", "Low Tide", list_name="watchlist", item_id="low-tide"),
        ]
        tasks = media_items_as_tasks(items)
        self.assertEqual(len(tasks), 4)
        tide, book, show, vela = tasks
        self.assertEqual(tide.id, "media:letterboxd:low-tide")
        self.assertEqual(tide.source_notebook, "Media > Letterboxd watchlist")
        self.assertEqual(tide.title, "Watch: Low Tide (1996, Ada Brook)")
        self.assertEqual(tide.description, "https://letterboxd.com/film/low-tide/")
        self.assertEqual(tide.estimated_thirties, 4)
        self.assertIn("read-only", tide.tags)
        self.assertEqual(book.id, "media:goodreads:900101")
        self.assertEqual(book.source_notebook, "Media > Goodreads to-read")
        self.assertEqual(book.title, "Read: The Lantern Almanac (1934, Mara Ellingsen)")
        self.assertEqual(show.id, "media:serializd:7001")
        self.assertEqual(show.source_notebook, "Media > Serializd watchlist")
        self.assertEqual(show.title, "Watch: Harbor Lights (2001)")
        self.assertEqual(vela.title, "Watch: VELA")
        self.assertFalse(tide.is_checkbox_item)


if __name__ == "__main__":
    unittest.main()
