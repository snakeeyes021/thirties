"""A one-time event just ends; a task stays open (past_one_offs.py, carry.py rule 3).

Python gathers the facts that tell an event from a task (from a calendar, other people on it, a fixed time, on
a list the user keeps tasks in) and never reads the title; the morning model says which it is with propose_leftovers
``end`` and its ``kind``. Only kind "event" on a one-time item whose own time is over ends, silently: stored as
unknown, off Today, never in the question, nothing filed in Dropped. A task, or a missing or unsure kind, stays not
marked yet and is asked about. An item asked about and never answered is never dropped, filed or ended.

Invented fixtures: a pottery studio (a kiln tour with visitors at 2:40, trimming stoneware bowls at 9:10).
"""

import json
from datetime import datetime, timedelta

from tests.fixtures import DAY, TZ, at
from tests.test_today import TITLES, TodayTestCase
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import DROPPED, OpenPool
from thirties_core.lists import ListCatalog
from thirties_core.models import EventSpan, SpanCategory, TaskItem
from thirties_core.past_one_offs import _span_facts, ends, gather, line
from thirties_core.plan_tools import PlanTools

YESTERDAY = DAY - timedelta(days=1)
TOUR, TRIM, GLAZE = "Kiln tour for the visiting glaze group", "Trim the stoneware bowls", "Mix a celadon test batch"
TOUR_LINE, TRIM_LINE = f"2:40 {TOUR}", f"9:10 {TRIM}"
STUDIO = f"- [ ] {GLAZE}\n- [ ] Wedge the reclaim clay\n"
EARLIER = "2026-01-01T12:00:00+00:00"  # when a line went on Today, long before this morning


class StudioCase(TodayTestCase):
    """Today holds two lines with typed clock times from an earlier day; a studio list holds tasks."""

    def setUp(self):
        super().setUp()
        TITLES.setdefault("n_studio", "Studio jobs")
        self.api.notes["n_studio"] = {"id": "n_studio", "body": STUDIO}
        ListCatalog(self.sched.state_db).set_role("n_studio", "list", "Studio jobs")
        self.api.notes["n_today"]["body"] = f"- [ ] {TOUR_LINE}\n- [ ] {TRIM_LINE}\n"
        self.services.tasks.extend([
            TaskItem(id="t_glaze", source_notebook="1. Tasks", title=GLAZE, is_checkbox_item=True,
                     parent_note_title="Studio jobs", parent_note_id="n_studio", list_role="list"),
            TaskItem(id="td_tour", source_notebook="1. Tasks", title=TOUR_LINE, is_checkbox_item=True,
                     parent_note_title="Today To-Do", parent_note_id="n_today", list_role="today"),
            TaskItem(id="td_trim", source_notebook="1. Tasks", title=TRIM_LINE, is_checkbox_item=True,
                     parent_note_title="Today To-Do", parent_note_id="n_today", list_role="today")])
        self.a = self.t.assistant
        self.db = self.sched.state_db
        self.helper_calls, self.answers = [], {}
        self.a.ask_json = self.helper  # the scripted helper: the same model's second opinion

    def helper(self, system, user):
        """Scripted: OCCASION_PASSED for the kiln tour and the open studio hour, STILL_NEEDED for the rest, unless
        ``answers`` (by a word of the title) says otherwise; an answer of None is no answer."""
        self.helper_calls.append(user)
        title = user.split('The item: "', 1)[1].split('"', 1)[0]
        for word, answer in self.answers.items():
            if word in title:
                return None if answer is None else {"answer": answer, "reason": "scripted"}
        passed = "Kiln tour" in title or "Open studio" in title
        return {"answer": "OCCASION_PASSED" if passed else "STILL_NEEDED", "reason": "scripted"}

    def asked_about(self, title):
        return [u for u in self.helper_calls if f'The item: "{title}"' in u]

    def pool(self, title):
        return OpenPool(self.db).all().get(title.lower())

    def record(self):
        return json.loads(self.db.get_sync_meta(self.a._rollover_key()) or "{}")

    def dropped_note(self):
        return next((n for n in self.api.notes.values() if str(n.get("title", "")).startswith("Dropped ")), None)

    def propose(self, end):
        self.t.execute("rollover_proposal", {})
        named = {e["item"] for e in end}
        keep = [{"item": k["title"], "when": "this week"} for k in self.record()["leftovers"]["keep"]
                if k["title"] not in named]
        return self.t.execute("propose_leftovers", {"keep": keep, "end": end})


class TheModelSaysWhichItIs(StudioCase):

    def test_a_past_event_ends_silently_stored_as_unknown(self):
        out = self.propose([{"item": TOUR_LINE, "kind": "event", "why": "the visitors came and went"}])
        self.assertIn("Ended silently (", out)
        rec = self.pool(TOUR_LINE)
        self.assertNotEqual(rec["horizon"], DROPPED)
        self.assertEqual(rec.get("outcome"), "unknown")
        self.assertTrue(rec.get("ended"))
        self.assertNotIn(TOUR_LINE.lower(), OpenPool(self.db).all(DAY))  # never offered again
        self.assertNotIn(TOUR, self.body("n_today"))  # off Today by the guarded remove, never ticked
        self.assertIsNone(self.dropped_note())  # nothing filed in Dropped
        self.assertEqual(self.db.completed_since(YESTERDAY), set())
        self.assertEqual(self.body("n_studio"), STUDIO)  # the home list is untouched
        notice = self.a.leftover_notice()
        self.assertNotIn(TOUR, notice)  # never in the notice or the one question
        self.assertNotIn("Ended", notice)
        self.assertIn(TRIM_LINE, notice)  # the task kept with no kind is asked about instead
        before = [r["title"] for rows in self.a.before.rows().values() if isinstance(rows, list) for r in rows
                  if isinstance(r, dict) and "title" in r]
        self.assertNotIn(TOUR_LINE, before)

    def test_an_event_on_an_earlier_plan_is_stored_as_unknown_there(self):
        from thirties_core.models import SpanStatus
        self.t.user_message = "the open studio hour starts 4:10"
        try:
            self.t.execute("add_event", {"title": "Open studio hour", "start": "4:10 PM", "duration": "30 min",
                                         "day": YESTERDAY.isoformat()})
        finally:
            self.t.user_message = None
        self.propose([{"item": "Open studio hour", "kind": "event"}])
        span = next(s for s in self.t.plan_for(YESTERDAY.isoformat()).spans if s.title == "Open studio hour")
        self.assertEqual(span.status, SpanStatus.UNKNOWN)  # not skipped
        self.assertTrue(self.pool("Open studio hour").get("ended"))
        self.assertNotIn("Open studio hour", self.a.leftover_notice())

    def test_a_past_timed_task_stays_open(self):
        out = self.propose([{"item": TRIM_LINE, "kind": "task"}])
        self.assertNotIn("Ended silently (", out)
        rec = self.pool(TRIM_LINE)
        self.assertNotEqual(rec.get("horizon"), DROPPED)
        self.assertFalse(rec.get("ended"))
        self.assertTrue(rec.get("unknown"))
        self.assertIn(f"- [ ] {TRIM_LINE}", self.body("n_today"))
        self.assertIsNone(self.dropped_note())

    def test_a_missing_or_unsure_kind_stays_unknown(self):
        for kind in (None, "unsure", "maybe"):
            with self.subTest(kind=kind):
                self.setUp()
                end = {"item": TOUR_LINE, **({"kind": kind} if kind else {})}
                out = self.propose([end])
                self.assertIn("Not marked yet (no day", out)
                rec = self.pool(TOUR_LINE)
                self.assertNotEqual(rec.get("horizon"), DROPPED)
                self.assertFalse(rec.get("ended"))
                self.assertFalse(rec.get("start"))
                self.assertIn(f"- [ ] {TOUR_LINE}", self.body("n_today"))
                self.assertIsNone(self.dropped_note())

    def test_an_event_with_no_time_of_its_own_over_does_not_end(self):
        self.t.execute("rollover_proposal", {})
        data = self.record()
        for k in data["leftovers"]["keep"]:
            if k["title"] == TOUR_LINE:
                k["facts"] = {**(k.get("facts") or {}), "time_by_user": False, "dates": [], "planned_day": "",
                              "today_line": False}
        self.db.set_sync_meta(self.a._rollover_key(), json.dumps(data))
        keep = [{"item": k["title"]} for k in data["leftovers"]["keep"] if k["title"] != TOUR_LINE]
        self.t.execute("propose_leftovers", {"keep": keep, "end": [{"item": TOUR_LINE, "kind": "event"}]})
        self.assertFalse(self.pool(TOUR_LINE).get("ended"))
        self.assertIn(f"- [ ] {TOUR_LINE}", self.body("n_today"))

    def test_the_check_is_structure_only(self):
        past = {"time_by_user": True, "planned_day": YESTERDAY.isoformat(), "kind": "once"}
        self.assertTrue(ends(past, DAY, "event"))
        self.assertTrue(ends(past, DAY, " Event "))
        for kind in ("task", "unsure", "", None):
            self.assertFalse(ends(past, DAY, kind))
        self.assertFalse(ends({**past, "kind": "ongoing"}, DAY, "event"))
        self.assertFalse(ends({**past, "kind": "daily"}, DAY, "event"))
        self.assertTrue(ends({**past, "time_by_user": False}, DAY, "event"))  # a time Thirties chose: over too
        self.assertFalse(ends({**past, "planned_day": ""}, DAY, "event"))  # never planned on a day now over

    def test_the_rule_and_the_tool_say_kind(self):
        from thirties_core.assistant_tools import ASSISTANT_TOOL_SPECS
        from thirties_core.past_one_offs import RULE
        spec = next(s for s in ASSISTANT_TOOL_SPECS if s["name"] == "propose_leftovers")
        self.assertIn("kind is required", spec["parameters"]["end"])
        self.assertIn("When unsure, unknown", RULE)
        self.assertIn("never a deadline", RULE)
        self.t.execute("rollover_proposal", {})
        self.assertIn("When unsure, unknown", self.a.leftover_brief())


class TheFactsThatTellThemApart(StudioCase):

    def test_a_list_the_user_keeps_tasks_in(self):
        f = gather(self.a, GLAZE, "t_glaze", DAY)
        self.assertTrue(f["task_list"])
        self.assertIn("on a list the user keeps tasks in", line(f, DAY))
        self.assertFalse(gather(self.a, TRIM_LINE, "td_trim", DAY)["task_list"])  # Today is not such a list

    def test_from_a_calendar_with_someone_else_and_fixed(self):
        span = EventSpan(id="c1", title="Open studio with the glaze group", category=SpanCategory.CALENDAR,
                         start_dt=datetime.combine(YESTERDAY, datetime.min.time(), TZ) + timedelta(hours=14, minutes=40),
                         end_dt=datetime.combine(YESTERDAY, datetime.min.time(), TZ) + timedelta(hours=15, minutes=40),
                         source="calendar", whose="Robin", shared=True)
        f = {"planned_day": "", "home": "", "kind": "once", "offered": 0, "dates": [], **_span_facts(self.a, span)}
        said = line(f, DAY)
        self.assertIn("from a calendar", said)
        self.assertIn("shared with Robin", said)
        self.assertIn("a fixed time", said)
        plain = EventSpan(id="p1", title=TRIM, category=SpanCategory.TASK, start_dt=span.start_dt, end_dt=span.end_dt)
        f = _span_facts(self.a, plain)
        self.assertEqual((f["calendar"], f["others"], f["fixed"]), (False, "", False))


class KeptOnlyAsATask(StudioCase):
    """Keep holds a one-off with a past slot only under kind "task"."""

    def keep_all(self, tour_kind=None):
        self.t.execute("rollover_proposal", {})
        keep = [{"item": k["title"], "when": "tomorrow", "kind": "task"} for k in self.record()["leftovers"]["keep"]
                if k["title"] != TOUR_LINE]
        keep.append({"item": TOUR_LINE, "when": "tomorrow", **({"kind": tour_kind} if tour_kind else {})})
        return self.t.execute("propose_leftovers", {"keep": keep})

    def test_a_past_timed_one_off_kept_with_no_kind_stays_unknown_with_no_day(self):
        for kind in (None, "event", "unsure"):
            with self.subTest(kind=kind):
                self.setUp()
                out = self.keep_all(kind)
                self.assertIn("Not marked yet (no day", out)
                rec = self.pool(TOUR_LINE)
                self.assertTrue(rec.get("unknown"))
                self.assertFalse(rec.get("start"))
                self.assertNotEqual(rec.get("when"), "tomorrow")
                self.assertNotEqual(rec.get("horizon"), DROPPED)
                self.assertIn(f"- [ ] {TOUR_LINE}", self.body("n_today"))

    def test_with_kind_task_it_is_kept(self):
        self.answers = {"Kiln tour": "STILL_NEEDED"}  # the helper agrees
        self.keep_all("task")
        rec = self.pool(TOUR_LINE)
        self.assertFalse(rec.get("unknown"))
        self.assertTrue(rec.get("start"))
        self.assertTrue(self.pool(TRIM_LINE).get("start"))  # the other one, kept as a task

    def test_the_check_is_structure_only(self):
        from thirties_core.past_one_offs import keeps
        past = {"time_by_user": True, "planned_day": YESTERDAY.isoformat(), "kind": "once"}
        self.assertTrue(keeps(past, DAY, "task"))
        for kind in ("event", "unsure", "", None):
            self.assertFalse(keeps(past, DAY, kind))
        self.assertTrue(keeps({**past, "kind": "ongoing"}, DAY, None))
        self.assertFalse(keeps({**past, "time_by_user": False}, DAY, None))  # a time Thirties chose: over too
        self.assertTrue(keeps({**past, "planned_day": ""}, DAY, None))  # a plain list item: kept as before
        self.assertFalse(keeps({"today_line": True, "kind": "once"}, DAY, None))  # on an earlier day's list


class BothMustAgree(StudioCase):
    """The morning model and the helper (the same model, asked one thing) must agree to end or to keep."""

    def test_end_and_occasion_passed_ends_silently(self):
        self.propose([{"item": TOUR_LINE, "kind": "event"}])
        self.assertEqual(len(self.asked_about(TOUR_LINE)), 1)
        self.assertTrue(self.pool(TOUR_LINE).get("ended"))
        self.assertNotIn(TOUR, self.a.leftover_notice())

    def test_keep_and_still_needed_is_kept(self):
        self.t.execute("rollover_proposal", {})
        self.t.execute("propose_leftovers", {"keep": [{"item": TRIM_LINE, "when": "friday", "kind": "task"}],
                                             "unknown": [{"item": TOUR_LINE}]})
        self.assertEqual(len(self.asked_about(TRIM_LINE)), 1)
        self.assertTrue(self.pool(TRIM_LINE).get("start"))
        self.assertFalse(self.pool(TRIM_LINE).get("unknown"))

    def test_disagreement_goes_to_the_question(self):
        self.answers = {"Kiln tour": "STILL_NEEDED", "Trim": "OCCASION_PASSED"}
        self.t.execute("rollover_proposal", {})
        self.t.execute("propose_leftovers", {"keep": [{"item": TRIM_LINE, "when": "friday", "kind": "task"}],
                                             "end": [{"item": TOUR_LINE, "kind": "event"}]})
        for title in (TOUR_LINE, TRIM_LINE):
            rec = self.pool(title)
            self.assertTrue(rec.get("unknown"), title)
            self.assertFalse(rec.get("start"), title)
            self.assertFalse(rec.get("ended"), title)
            self.assertIn(f"- [ ] {title}", self.body("n_today"))
        self.assertIn("Did they happen?", self.a.leftover_notice())

    def test_cant_tell_or_no_answer_goes_to_the_question(self):
        for answer in ("CANT_TELL", None, "MAYBE"):
            with self.subTest(answer=answer):
                self.setUp()
                self.answers = {"Kiln tour": answer, "Trim": answer}
                self.t.execute("rollover_proposal", {})
                self.t.execute("propose_leftovers", {"keep": [{"item": TRIM_LINE, "when": "friday", "kind": "task"}],
                                                     "end": [{"item": TOUR_LINE, "kind": "event"}]})
                for title in (TOUR_LINE, TRIM_LINE):
                    self.assertTrue(self.pool(title).get("unknown"), title)
                    self.assertFalse(self.pool(title).get("ended"), title)
                self.assertIn("Did they happen?", self.a.leftover_notice())

    def test_no_helper_at_all_goes_to_the_question(self):
        del self.a.ask_json
        self.propose([{"item": TOUR_LINE, "kind": "event"}])
        self.assertFalse(self.pool(TOUR_LINE).get("ended"))
        self.assertTrue(self.pool(TOUR_LINE).get("unknown"))

    def test_a_helper_that_fails_goes_to_the_question(self):
        def broken(system, user):
            raise TimeoutError("engine timeout")
        self.a.ask_json = broken
        self.propose([{"item": TOUR_LINE, "kind": "event"}])
        self.assertFalse(self.pool(TOUR_LINE).get("ended"))

    def test_no_call_for_items_left_unknown_or_not_past(self):
        self.t.execute("rollover_proposal", {})
        self.t.execute("propose_leftovers", {"keep": [], "unknown": [{"item": TOUR_LINE}, {"item": TRIM_LINE}]})
        self.assertEqual(self.helper_calls, [])
        self.assertEqual(self.asked_about(GLAZE), [])

    def test_a_retried_proposal_asks_once(self):
        self.t.execute("rollover_proposal", {})
        args = {"keep": [{"item": TRIM_LINE, "when": "friday", "kind": "task"}],
                "end": [{"item": TOUR_LINE, "kind": "event"}]}
        self.t.execute("propose_leftovers", args)
        self.t.execute("propose_leftovers", args)
        self.assertEqual(len(self.asked_about(TRIM_LINE)), 1)


class TheHelpersPrompt(StudioCase):

    def test_it_is_filled_from_stored_facts(self):
        from thirties_core.occasion_helper import SYSTEM, fill
        self.services.conversations.append(YESTERDAY, {"role": "user", "content": "glaze group kiln tour, 2:40 sharp"})
        self.services.conversations.append(YESTERDAY, {"role": "user", "content": "wedging clay all morning"})
        self.t.execute("rollover_proposal", {})
        item = next(k for k in self.record()["leftovers"]["keep"] if k["title"] == TOUR_LINE)
        text = fill(self.a, item, DAY)
        self.assertTrue(text.startswith("Today is Thursday, October 15, 2026.\n"), text)
        self.assertIn(f'The item: "{TOUR_LINE}"', text)
        self.assertIn("Planned for: Wednesday, October 14 at 2:40 PM (the user's own time).\n", text)
        self.assertIn("Where it lives: only on that day's list.", text)
        self.assertIn("Dates in its name: none.", text)
        self.assertIn('word for word: "glaze group kiln tour, 2:40 sharp".', text)
        self.assertNotIn("wedging", text)
        self.assertIn("dates: if the occasion", text)  # one prompt, whole (split only in the source)
        self.assertIn('Reply only with: {"answer": "OCCASION_PASSED" | "STILL_NEEDED" | "CANT_TELL"', text)
        for brace in ("{title}", "{weekday}", "{same_day_items}", "{user_words_or_none}", "{holiday_note}", "#"):
            self.assertNotIn(brace, text)
        self.assertTrue(SYSTEM.startswith("You help a personal planning app decide"))
        self.assertTrue(SYSTEM.endswith("Answer with a single JSON object and nothing else."))

    def test_the_other_items_of_that_day(self):
        from thirties_core.occasion_helper import same_day_items
        self.t.execute("add_event", {"title": "Load the bisque kiln", "start": "9:40 AM", "duration": "30 min",
                                     "day": YESTERDAY.isoformat()})
        self.t.execute("add_event", {"title": "Sweep the wheel room", "start": "4:20 PM", "duration": "30 min",
                                     "day": YESTERDAY.isoformat()})
        self.t.execute("log_outcome", {"event": "Load the bisque kiln", "day": YESTERDAY.isoformat(), "status": "done"})
        said = same_day_items(self.a, TOUR_LINE, YESTERDAY)
        self.assertIn("Load the bisque kiln (done)", said)
        self.assertIn("Sweep the wheel room (not marked)", said)
        self.assertLess(said.index("bisque"), said.index("wheel room"))
        self.assertEqual(same_day_items(self.a, TOUR_LINE, YESTERDAY - timedelta(days=5)), "none")

    def test_the_first_json_object_of_a_reply(self):
        from thirties_core.occasion_helper import first_json
        self.assertEqual(first_json('Sure. {"answer": "CANT_TELL", "reason": "x"} and {"b": 1}'),
                         {"answer": "CANT_TELL", "reason": "x"})
        self.assertEqual(first_json('{oops} {"answer": "STILL_NEEDED"}'), {"answer": "STILL_NEEDED"})
        for bad in ("", None, "no json here", "[1, 2]"):
            self.assertIsNone(first_json(bad))


class TheCallPath(StudioCase):

    def test_the_frontier_brain_asks_its_own_engine(self):
        from thirties_core.brains.frontier import FrontierBrain

        class Engine:
            def __init__(self):
                self.seen = []

            def chat(self, messages, tools=None):
                self.seen.append((messages, tools))
                return {"role": "assistant", "content": 'Here: {"answer": "OCCASION_PASSED", "reason": "over"}'}
        engine = Engine()
        brain = FrontierBrain(self.t, engine)
        self.assertEqual(self.a.ask_json, brain.ask_json)  # the active brain's helper
        self.assertEqual(brain.ask_json("sys", "usr"), {"answer": "OCCASION_PASSED", "reason": "over"})
        self.assertEqual(engine.seen[-1][0], [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}])

        class Down:
            def chat(self, messages, tools=None):
                raise ConnectionError("unavailable")
        brain.inference_engine = Down()
        self.assertIsNone(brain.ask_json("sys", "usr"))

    def test_the_device_brain_uses_json_call(self):
        from thirties_core.brains.slm.brain import SlmBrain

        class Runtime:
            def json_call(self, system, user, schema, max_output_tokens=256):
                return {"answer": "STILL_NEEDED", "reason": "still owed"}
        brain = SlmBrain(self.t, runtime=Runtime())
        self.assertEqual(brain.ask_json("sys", "usr")["answer"], "STILL_NEEDED")

        class Broken:
            def json_call(self, *a, **k):
                raise RuntimeError("no model")
        self.assertIsNone(SlmBrain(self.t, runtime=Broken()).ask_json("sys", "usr"))


class AtATimeThirtiesChose(StudioCase):
    """Planned yesterday at a time Thirties chose: still a past one-off, so it needs kind and the helper too."""

    EVENING = "Open studio evening"

    def setUp(self):
        super().setUp()
        self.t.execute("add_event", {"title": self.EVENING, "start": "4:10 PM", "duration": "30 min",
                                     "day": YESTERDAY.isoformat()})

    def keep(self, kind=None):
        self.t.execute("rollover_proposal", {})
        titles = [k["title"] for k in self.record()["leftovers"]["keep"]]
        self.assertIn(self.EVENING, titles)
        keep = [{"item": t, "when": "saturday", **({"kind": kind} if kind else {})} for t in titles]
        self.t.execute("propose_leftovers", {"keep": keep})
        return self.pool(self.EVENING)

    def test_the_model_never_proposing_leaves_python_s_guess_with_no_day(self):
        from tests.test_today import as_earlier_lines
        with as_earlier_lines():  # a plain list item put on Today on an earlier day, never planned at a time
            self.t.execute("add_to_today", {"items": [GLAZE]})
        with self.db._get_connection() as conn:
            conn.execute("UPDATE today_links SET updated_at = ?", (EARLIER,))
        self.db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")
        self.a.start_the_day()
        out = self.t.execute("rollover_proposal", {})  # and no propose_leftovers after it
        for title in (self.EVENING, TOUR_LINE, TRIM_LINE):
            rec = self.pool(title)
            self.assertTrue(rec.get("unknown"), title)
            self.assertFalse(rec.get("start"), title)
            self.assertNotEqual(rec.get("when"), "tomorrow")
        self.assertIn("Not marked yet:", out)
        self.assertIn(self.EVENING, out.split("Not marked yet:", 1)[1])
        self.assertNotIn(f"{self.EVENING}, tomorrow", out)
        self.assertIn(f"{self.EVENING} (NOT MARKED YET", self.a.leftover_brief())
        glaze = self.pool(GLAZE)
        self.assertIsNotNone(glaze)
        self.assertFalse(glaze.get("unknown"))  # the plain list item keeps the usual first guess
        self.assertTrue(glaze.get("start"))
        self.assertEqual(self.helper_calls, [])  # no second opinion without a proposal

    def test_its_facts_say_a_time_thirties_chose(self):
        self.t.execute("rollover_proposal", {})
        item = next(k for k in self.record()["leftovers"]["keep"] if k["title"] == self.EVENING)
        self.assertFalse(item["facts"]["time_by_user"])
        self.assertEqual(item["facts"]["planned_day"], YESTERDAY.isoformat())

    def test_keep_without_agreement_goes_to_the_question_never_a_day(self):
        rec = self.keep("task")  # the helper says its occasion has passed: they disagree
        self.assertEqual(len(self.asked_about(self.EVENING)), 1)
        self.assertTrue(rec.get("unknown"))
        self.assertFalse(rec.get("start"))
        self.assertNotEqual(rec.get("when"), "saturday")
        self.assertIn(self.EVENING, self.a.leftover_notice())

    def test_keep_with_no_kind_goes_to_the_question_without_a_call(self):
        rec = self.keep()
        self.assertEqual(self.asked_about(self.EVENING), [])
        self.assertTrue(rec.get("unknown"))
        self.assertFalse(rec.get("start"))

    def test_no_helper_answer_goes_to_the_question(self):
        self.answers = {"Open studio": None}
        rec = self.keep("task")
        self.assertTrue(rec.get("unknown"))
        self.assertFalse(rec.get("start"))

    def test_kept_when_both_agree(self):
        self.answers = {"Open studio": "STILL_NEEDED"}
        rec = self.keep("task")
        self.assertFalse(rec.get("unknown"))
        self.assertTrue(rec.get("start"))

    def test_the_users_own_word_places_it_with_no_second_opinion(self):
        self.t.execute("rollover_proposal", {})
        self.t.user_message = "open studio evening again on saturday"
        try:
            self.t.execute("propose_leftovers", {"keep": [{"item": k["title"], "when": "saturday"} for k in
                                                          self.record()["leftovers"]["keep"]]})
        finally:
            self.t.user_message = None
        self.assertEqual(self.asked_about(self.EVENING), [])
        self.assertTrue(self.pool(self.EVENING).get("start"))

    def test_both_agree_to_end_it(self):
        self.t.execute("rollover_proposal", {})
        keep = [{"item": k["title"], "kind": "task"} for k in self.record()["leftovers"]["keep"]
                if k["title"] != self.EVENING]
        self.t.execute("propose_leftovers", {"keep": keep, "end": [{"item": self.EVENING, "kind": "event"}]})
        self.assertTrue(self.pool(self.EVENING).get("ended"))


class ThePlannedTime(StudioCase):

    def test_a_typed_time_with_no_am_or_pm_reads_as_the_waking_day(self):
        self.assertEqual(gather(self.a, TOUR_LINE, "td_tour", DAY)["planned_at"], "2:40 PM")
        self.assertEqual(gather(self.a, TRIM_LINE, "td_trim", DAY)["planned_at"], "9:10 AM")

    def test_a_stated_time_counts_only_while_the_row_is_still_there(self):
        from datetime import time
        self.t.user_message = "the glaze demo is at 4:40"
        try:
            self.t.execute("add_event", {"title": "Glaze demo", "start": "4:40 PM", "duration": "30 min",
                                         "day": YESTERDAY.isoformat()})
        finally:
            self.t.user_message = None
        plan = self.t.plan_for(YESTERDAY.isoformat())
        span = next(s for s in plan.spans if s.title == "Glaze demo")
        self.assertIsNotNone(span.given_start)
        f = gather(self.a, "Glaze demo", "", DAY)
        self.assertEqual((f["planned_at"], f["time_by_user"]), ("4:40 PM", True))
        span.given_start = datetime.combine(YESTERDAY, time(13, 0), TZ)  # a time from before it moved
        self.t._persist(span)
        f = gather(self.a, "Glaze demo", "", DAY)
        self.assertFalse(f["time_by_user"])
        self.assertNotEqual(f["planned_at"], "1:00 PM")
        self.assertEqual(f["planned_at"], "4:30 PM")  # where its row is, by the half hour


class NeverVanish(StudioCase):
    """Asked about, never answered: two more mornings go by and the item is still there."""

    def test_two_mornings_with_no_answer(self):
        self.a.start_the_day()
        self.t.execute("rollover_proposal", {})
        first = dict(self.pool(TRIM_LINE))
        self.assertTrue(first.get("unknown"))
        for offset in (1, 2):
            set_debug_time(at("08:10", offset))
            plan, pending = self.sched.build_day_plan(DAY + timedelta(days=offset))
            t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.db, self.services.tasks,
                                                                              self.services.joplin))
            t.assistant.start_the_day()
            t.execute("rollover_proposal", {})
            for title in (TRIM_LINE, TOUR_LINE):
                rec = OpenPool(self.db).all().get(title.lower())
                self.assertIsNotNone(rec, (offset, title))
                self.assertNotEqual(rec.get("horizon"), DROPPED)
                self.assertFalse(rec.get("ended"))
                self.assertFalse(rec.get("pending_drop"))
                self.assertIn(f"- [ ] {title}", self.body("n_today"))
            rec = OpenPool(self.db).all()[TRIM_LINE.lower()]
            for key in ("horizon", "when", "start", "end", "unknown"):
                self.assertEqual(rec.get(key), first.get(key), (offset, key))
        self.assertIsNone(self.dropped_note())
        self.assertEqual(self.body("n_studio"), STUDIO)


if __name__ == "__main__":
    import unittest
    unittest.main()
