"""Rush tickets at the user's theatre, checked once a day when turned on. The theatre, its address
and its listing page are invented (tests/fixtures_tickets); nothing here touches the network."""

import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

from tests.fixtures import make_scheduler
from thirties_core import tickets
from thirties_core.settings import Settings
from thirties_core.tickets import background_duty, check, parse_performances, rush_today

PAGE = (Path(__file__).parent / "fixtures_tickets" / "performances.html").read_text(encoding="utf-8")
THU, SAT = date(2026, 11, 19), date(2026, 11, 21)
LISTING = "https://tickets.example.org/juniper-hall/performances"


class ParseTest(unittest.TestCase):
    def test_performances_with_their_availability(self):
        found = parse_performances(PAGE, THU)
        self.assertEqual([(p.starts, p.sold_out) for p in found],
                         [(datetime(2026, 11, 19, 20, 0), False), (datetime(2026, 11, 20, 20, 0), True),
                          (datetime(2026, 11, 21, 14, 0), False), (datetime(2026, 11, 21, 20, 0), True)])
        self.assertEqual(found[0].title, "The Lantern Keepers")
        self.assertEqual(found[0].availability, "Limited Availability")

    def test_rush_only_where_it_isnt_sold_out(self):
        performances = parse_performances(PAGE, THU)
        self.assertEqual([p.starts.hour for p in rush_today(performances, THU)], [20])
        self.assertEqual(rush_today(performances, date(2026, 11, 20)), [])  # sold out: no rush
        self.assertEqual([p.starts.hour for p in rush_today(performances, SAT)], [14])

    def test_the_notice(self):
        found = rush_today(parse_performances(PAGE, THU), THU)
        text = tickets.notice(found, "Juniper Hall", "$25 at the box office")
        self.assertEqual(text, "🎭 Rush tickets at Juniper Hall today: The Lantern Keepers, 8:00 PM.\n"
                               "$25 at the box office, from 7:00 PM, while they last.")
        self.assertNotIn("- ", text)
        self.assertEqual(tickets.notice(found), "🎭 Rush tickets at the theatre today: The Lantern Keepers, 8:00 PM.\n"
                                                "At the box office from 7:00 PM, while they last.")

    def test_a_page_without_a_listing_finds_nothing(self):
        self.assertEqual(parse_performances("<html><body><p>Closed for the holidays</p></body></html>", THU), [])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.sched = make_scheduler()
        self.db = self.sched.state_db
        self.settings = Settings(self.db)
        self.calls = []

        def opener(url):
            self.calls.append(url)
            return PAGE.encode("utf-8")
        self.opener = opener

    def test_off_by_default_and_never_fetches(self):
        self.assertIsNone(check(self.db, self.settings, datetime(2026, 11, 19, 12), self.opener))
        self.assertEqual(self.calls, [])
        self.assertEqual(tickets.SETTING, "tickets.rush")
        from thirties_core.settings import SETTINGS
        for key in (tickets.SETTING, tickets.THEATRE_SETTING, tickets.URL_SETTING, tickets.TERMS_SETTING):
            self.assertEqual(SETTINGS[key].scope, "device")  # one machine does the checking

    def test_on_with_no_listing_page_never_fetches(self):
        self.settings.set(tickets.SETTING, True)
        self.assertIsNone(check(self.db, self.settings, datetime(2026, 11, 19, 12), self.opener))
        self.assertEqual(self.calls, [])

    def on(self):
        self.settings.set(tickets.SETTING, True)
        self.settings.set(tickets.URL_SETTING, LISTING)

    def test_once_a_day_from_mid_morning(self):
        self.on()
        self.assertIsNone(check(self.db, self.settings, datetime(2026, 11, 19, 8), self.opener))  # too early
        found = check(self.db, self.settings, datetime(2026, 11, 19, 11), self.opener)
        self.assertEqual([p.starts.hour for p in found], [20])
        check(self.db, self.settings, datetime(2026, 11, 19, 15), self.opener)
        self.assertEqual(self.calls, [LISTING])  # one fetch a day
        check(self.db, self.settings, datetime(2026, 11, 21, 11), self.opener)
        self.assertEqual(len(self.calls), 2)

    def test_a_failed_fetch_is_retried_a_few_times_hours_apart(self):
        self.on()

        def broken(url):
            self.calls.append(url)
            raise OSError("unreachable")
        for hour in (10, 11, 13, 16, 19, 22):
            self.assertIsNone(check(self.db, self.settings, datetime(2026, 11, 19, hour), broken))
        self.assertEqual(len(self.calls), 3)  # 10, 13 and 16: then it waits for tomorrow

    def test_the_background_sends_it_once(self):
        self.on()
        self.settings.set(tickets.THEATRE_SETTING, "Juniper Hall")
        service = MagicMock()
        service.scheduler = self.sched
        text = background_duty(service, datetime(2026, 11, 19, 11), self.opener)
        self.assertIn("Rush tickets at Juniper Hall today: The Lantern Keepers, 8:00 PM", text)
        service.send.assert_called_once()
        self.assertIsNone(background_duty(service, datetime(2026, 11, 19, 12), self.opener))
        self.assertIsNone(background_duty(service, datetime(2026, 11, 20, 11), self.opener))  # sold out: nothing

    def test_the_polite_opener_honours_robots(self):
        from unittest.mock import patch

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self, n=-1):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        seen = []

        def fake(req, timeout=None):
            seen.append((req.full_url, req.get_header("User-agent")))
            return Resp(b"User-agent: *\nDisallow: /juniper-hall/\n")
        with patch("urllib.request.urlopen", fake):
            with self.assertRaises(PermissionError):
                tickets.polite_opener(LISTING)
        self.assertEqual(seen, [("https://tickets.example.org/robots.txt", tickets.AGENT)])  # never the page


class ToolTest(unittest.TestCase):
    def test_turning_it_on_in_chat(self):
        from thirties_core.assistant_tools import AssistantTools, Services
        from thirties_core.plan_tools import PlanTools
        from tests.fixtures import DAY
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        pt = PlanTools(plan, pending, sched)
        pt.assistant = AssistantTools(pt, Services.for_db(sched.state_db))
        fetched = []
        from unittest.mock import patch
        never = patch("thirties_core.tickets.polite_opener", lambda url: fetched.append(url) or PAGE.encode("utf-8"))
        never.start()
        self.addCleanup(never.stop)  # nothing here reaches the network
        self.assertIn("is off", pt.execute("rush_tickets", {}))
        out = pt.execute("rush_tickets", {"watch": True, "theatre": "Juniper Hall"})
        self.assertIn("ask the user for the address of the page that lists Juniper Hall's performances", out)
        out = pt.execute("rush_tickets", {"listing": LISTING, "terms": "$25 at the box office"})
        self.assertNotIn("is off", out)
        out = pt.execute("rush_tickets", {"watch": True})
        self.assertEqual(out, "Checking Juniper Hall for rush tickets every day from now on; a message comes on days "
                              "there are some.")
        settings = Settings(sched.state_db)
        self.assertTrue(settings.get(tickets.SETTING))
        self.assertEqual((settings.get(tickets.THEATRE_SETTING), settings.get(tickets.URL_SETTING),
                          settings.get(tickets.TERMS_SETTING)), ("Juniper Hall", LISTING, "$25 at the box office"))
        self.assertTrue(pt.execute("rush_tickets", {"listing": "the box office"}).startswith("NOT_DONE"))
        self.assertLessEqual(len(fetched), 1)  # the stand-in page, read at most once a day


if __name__ == "__main__":
    unittest.main()
