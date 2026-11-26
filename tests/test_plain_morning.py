"""The morning says things in plain words: the facts it gets carry no internal label for it to repeat.

Each option comes with a reason, and the week's plan with how it stands. Given as the app's own values (an
urgency word, a count of done items, a verdict on the week), those got copied into the reply, where they read
as jargon the user couldn't decode. The facts give the same information as a person would put it: wanted as
soon as possible, due tomorrow, the week's items by name with which are done and how long is left. The morning
prompt asks for plain words too, and for names instead of counts.

Invented: someone who runs a small pottery studio (glazes, kiln loads, clay orders)."""

import inspect
import re
import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_today import TITLES, TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.horizons import HorizonCatalog, HorizonPlans, period_of
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem

STUDIO = (("p_slip", "Sieve the porcelain slip"), ("p_cones", "Order more witness cones"),
          ("p_shelf", "Scrape the kiln shelves"), ("p_tiles", "Fire the test tiles"))

TALLY = re.compile(r"\b\d+\s+of\s+\d+\b")       # "2 of 7", "2 of 3 done"
LABEL = re.compile(r"\((?:now|soon)[;)]")       # an urgency value given as the reason itself
WORDS = re.compile(r"\b(?:on track|behind|slipped)\b", re.IGNORECASE)


def studio_item(tid, title):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title="Studio", parent_note_id="n_studio", list_role="list")


class StudioCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:20"))
        TITLES.setdefault("n_studio", "Studio")
        self.api.notes["n_studio"] = {"id": "n_studio", "body": "".join(f"- [ ] {t}\n" for _, t in STUDIO)}
        ListCatalog(self.sched.state_db).set_role("n_studio", "list", "Studio")
        self.services.tasks.extend(studio_item(tid, title) for tid, title in STUDIO)
        self.db = self.sched.state_db
        self.a = self.t.assistant

    def title(self, tid):
        return next(t for t in self.services.tasks if t.id == tid).title

    def urgency(self, tid, urgency, due=None):
        ListCatalog(self.db).set_urgency(tid, urgency, due, note_id="n_studio", title=self.title(tid), source="user")

    def ready(self):
        ListCatalog(self.db).apply(self.services.tasks)
        HorizonCatalog(self.db).apply(self.services.tasks, DAY)

    def own(self):
        """The options' reasons this module gives itself (the plans' own reasons read "due tomorrow", "can't
        wait" already)."""
        return {c["title"]: c["why"] for c in self.a.proposals.own_candidates(self.t.day_plan)}


class TestTheOptionsSayWhyInPlainWords(StudioCase):

    def test_urgency_and_due_dates_read_as_a_person_says_them(self):
        self.urgency("p_slip", "now")
        self.urgency("p_cones", "soon")
        self.urgency("p_shelf", "soon", due=DAY + timedelta(days=1))
        self.ready()
        why = self.own()
        self.assertIn("the user wants it done as soon as possible", why["Sieve the porcelain slip"])
        self.assertIn("the user wants it done in the next few days", why["Order more witness cones"])
        self.assertTrue(why["Scrape the kiln shelves"].startswith("due tomorrow"), why)
        for reason in why.values():
            self.assertNotRegex(reason, r"^(?:now|soon)\b")
        text = self.t.execute("day_options", {})
        self.assertIn("Sieve the porcelain slip", text)
        self.assertNotRegex(text, LABEL)
        self.assertNotRegex(text, TALLY)

    def test_an_overdue_item_says_when_it_was_due(self):
        self.urgency("p_tiles", "soon", due=DAY - timedelta(days=2))
        self.ready()
        self.assertTrue(self.own()["Fire the test tiles"].startswith("overdue (it was due "), self.own())

    def test_as_soon_as_possible_still_holds_its_place_on_a_low_day(self):
        from thirties_core import energy
        from thirties_core.day_proposal import ASAP_WHY
        self.assertTrue(energy.is_due({"why": ASAP_WHY, "task": None}))

    def test_the_lists_the_model_reads_say_how_pressing_in_words(self):
        from thirties_core.brains.frontier import FrontierBrain
        self.urgency("p_slip", "now")
        self.urgency("p_cones", "soon")
        self.ready()
        notes = [{"id": "n_studio", "source_notebook": "Tasks", "title": "Studio", "body": ""}]
        tasks = ListCatalog(self.db).apply(list(self.services.tasks))
        text = FrontierBrain(self.t, None, tasks=tasks, active_notes=notes).build_backlog_summary()
        self.assertIn("'Sieve the porcelain slip' [wanted as soon as possible]", text)
        self.assertIn("'Order more witness cones' [wanted in the next few days]", text)
        self.assertNotIn("[now]", text)
        self.assertNotIn("[soon]", text)


class TestTheWeekByName(StudioCase):

    def test_the_morning_week_line_names_the_items_and_counts_nothing(self):
        from thirties_core.day_proposal import week_line
        week = period_of("week", DAY)
        plans = HorizonPlans(self.db)
        plans.put("p_slip", "Sieve the porcelain slip", "n_studio", "Studio", week)
        plans.put("p_cones", "Order more witness cones", "n_studio", "Studio", week)
        plans.put("p_tiles", "Fire the test tiles", "n_studio", "Studio", week)
        self.db.record_task_completed("p_cones", DAY)
        line = week_line(self.t)
        self.assertTrue(line.startswith("Planned for this week: "), line)
        for title in ("Sieve the porcelain slip", "Order more witness cones", "Fire the test tiles"):
            self.assertIn(title, line)
        self.assertIn("done: Order more witness cones", line)
        self.assertIn("days left", line)
        self.assertNotRegex(line, TALLY)
        self.assertNotRegex(line, WORDS)

    def test_no_tally_and_no_label_in_any_summary(self):
        from thirties_core.on_track import Track, summary
        cases = [Track(planned=3, done=0, planned_titles=["A", "B", "C"], days_left=3),
                 Track(planned=3, done=1, slipped=["B"], planned_titles=["A", "B", "C"], done_titles=["A"],
                       days_left=2),
                 Track(planned=2, done=2, planned_titles=["A", "B"], done_titles=["A", "B"], days_left=1),
                 Track(planned=2, done=1, planned_titles=["A", "B"], done_titles=["A"]),
                 Track(planned=2, done=0, planned_titles=["A", "B"], starts_in=4)]
        for t in cases:
            for plan in (False, True):
                text = summary(t, plan)
                self.assertNotRegex(text, TALLY)
                self.assertNotRegex(text, WORDS)
        self.assertEqual(summary(cases[0]), "none done yet; " + "3 days" + " left")
        self.assertEqual(summary(cases[3]), "done: A; not done: B")


class TestThePromptAsksForPlainWords(unittest.TestCase):

    def test_the_rule_is_in_the_morning_prompt(self):
        from thirties_core.day_proposal import MORNING_INSTRUCTION, PLAIN_WHY
        self.assertIn(PLAIN_WHY, MORNING_INSTRUCTION)
        for label in ("for now", "urgency", "horizon", "pool"):
            self.assertIn(label, PLAIN_WHY)
        self.assertIn("name the items instead of counting them", PLAIN_WHY)
        self.assertNotIn("on track or behind", MORNING_INSTRUCTION)

    def test_the_first_message_of_the_day_carries_it_too(self):
        from thirties_core.day_proposal import PLAIN_WHY
        from thirties_core.morning import in_turn_instruction
        self.assertIn(PLAIN_WHY, in_turn_instruction("", ""))

    def test_the_standing_rule_for_proposing_the_day(self):
        from thirties_core.brains import frontier
        self.assertIn("say why in one line, in plain words the user would use", inspect.getsource(frontier))


if __name__ == "__main__":
    unittest.main()
