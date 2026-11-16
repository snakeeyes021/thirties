"""How a list note is read (list_reading.py): kinds of line, names and notes, nesting, states, what the note's body
says about progress, and never writing an id into a note. Invented lists: a club's spring fundraiser (sponsors,
raffle, venue) and a house move (utility accounts). DAY is Thu Oct 15."""

import json
import unittest
from types import SimpleNamespace

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_engine import checklist_tasks, item_id
from thirties_core import list_reading as lr

FUNDRAISER = """# Sponsors
- [ ] Ask sponsors for raffle prizes
    - [ ] Larkspur Hardware
    - [ ] Okonkwo Bakery - no contact yet
    - [x] Fennimore Dental
    - [ ] Brightwater Cycles - cancelled
    - [ ] Pack the prize parcels - waiting on postage
# Raffle
- [ ] Print the raffle tickets
    - [ ] Requires the final prize list
    - [x] The treasurer says the old printer still works fine for this.
    * use the thick card
- [ ] Which banner do we keep?
    - [ ] The blue one
    - [ ] The canvas one
- [ ] Prize ideas:
    - [ ] Picnic hamper
- [ ] https://shop.example.com/banners/item-4471
- [ ]
- [ ] Sort the volunteer rota - 1/2
- [ ] Count the float - 20
- [ ] Order lanyards (pack of 50) - $18
- [ ] Router - guest network
- [ ] Cable (short)
- [ ] Cable (long)
- [ ] Repaint the stall sign (after the primer dries)
# Venue
- [x] Book the hall
    - [ ] Return the spare chairs
- [ ] Set up the stalls
    - [x] Borrow trestle tables
    - [x] Collect the gazebo
## Paused
- [ ] Ask the council about parking
## Scrapped
- [ ] Hire a bouncy castle
## Talk it over at the AGM
- [ ] A second raffle in autumn
## Worth buying?
- [ ] A card reader for the stalls

---
## Larkspur Hardware
Hi Larkspur Hardware,
We would love a voucher for the raffle; every prize helps the club's new nets.
Cheers,
Tamsin

## Okonkwo Bakery
Hi Okonkwo Bakery,

## Fennimore Dental
How the prize gets shown at the stall, in brief.
READY

## Pemberton Books
Hi Pemberton Books,
A short note about the raffle and how a prize helps the club.
"""

MOVE = """# Utilities
- [ ] Water
    - [ ] Close the old account
    - [ ] Open the new account
- [ ] Power
    - [ ] Close the old account
"""


def tasks_of(body=FUNDRAISER, note="n_fund", title="Spring fundraiser"):
    tasks = checklist_tasks(note, title, "Club", body, [])
    for t in tasks:
        t.list_role = "list"
    return tasks


def find(tasks, title):
    return next(t for t in tasks if t.title == title)


class TestKindsOfLine(unittest.TestCase):
    """A: only entries are offered; conditions are notes on their parents; labels, questions, links and empty boxes
    are never tasks."""

    def setUp(self):
        self.tasks = tasks_of()

    def test_a_condition_or_fact_is_a_note_on_its_parent_not_an_item(self):
        titles = [t.title for t in self.tasks]
        self.assertNotIn("Requires the final prize list", titles)
        self.assertFalse(any("treasurer says" in t for t in titles))
        tickets = find(self.tasks, "Print the raffle tickets")
        conditions = [n for n in tickets.notes if n["kind"] == "condition"]
        self.assertEqual([(c["text"], c["met"]) for c in conditions],
                         [("Requires the final prize list", "no"),
                          ("The treasurer says the old printer still works fine for this.", "yes")])
        self.assertIn({"kind": "context", "text": "use the thick card"}, tickets.notes)  # a plain line under it
        self.assertEqual(tickets.blocker, "Requires the final prize list")  # unmet: it holds the parent up
        self.assertFalse(lr.do_now(tickets))

    def test_a_met_fact_never_closes_its_parent(self):
        body = "- [ ] Print the raffle tickets\n    - [x] Requires the final prize list\n"
        tickets = find(tasks_of(body), "Print the raffle tickets")
        self.assertEqual(tickets.blocker, "")
        self.assertTrue(lr.do_now(tickets))  # still open, still to do

    def test_question_options_group_label_and_link(self):
        question = find(self.tasks, "Which banner do we keep?")
        self.assertEqual(question.line_kind, lr.QUESTION)
        self.assertEqual(find(self.tasks, "The blue one").state, lr.OPTION)
        self.assertEqual(find(self.tasks, "Prize ideas:").line_kind, lr.GROUP)
        self.assertEqual(find(self.tasks, "Picnic hamper").state, "")  # a member of a group is an entry
        link = find(self.tasks, "https://shop.example.com/banners/item-4471")
        self.assertEqual((link.line_kind, link.name), (lr.LINK, "shop.example.com"))
        for t in (question, find(self.tasks, "The blue one"), find(self.tasks, "Prize ideas:"), link):
            self.assertFalse(lr.offerable(t), t.title)
        self.assertTrue(lr.offerable(find(self.tasks, "Picnic hamper")))

    def test_an_empty_box_is_skipped_and_ids_still_line_up(self):
        self.assertFalse(any(not t.title.strip() for t in self.tasks))
        rota = find(self.tasks, "Sort the volunteer rota - 1/2")
        self.assertEqual(rota.id, item_id("n_fund", "Sort the volunteer rota - 1/2", 0))

    def test_a_condition_word_at_the_top_level_is_still_an_entry(self):
        self.assertEqual(lr.read_text("Needs a new tyre", nested=False).kind, lr.ENTRY)
        self.assertEqual(lr.read_text("Needs a new tyre", nested=True).kind, lr.CONDITION)


class TestNameAndNotes(unittest.TestCase):
    """B: notes split off only for known kinds; parentheses stay in the name unless they're notes; the title (the
    item's identity) is the whole line."""

    def test_estimates(self):
        self.assertEqual(lr.estimate_minutes("20"), 20)       # a bare whole number is minutes
        self.assertEqual(lr.estimate_minutes("1/2"), 30)      # a fraction is a part of an hour, never a date
        self.assertEqual(lr.estimate_minutes("1/6"), 10)
        self.assertEqual(lr.estimate_minutes("90+"), 90)
        self.assertEqual(lr.estimate_minutes("60+45"), 105)
        self.assertEqual(lr.estimate_minutes("3 (hours)"), 180)
        self.assertEqual(lr.estimate_minutes("2h"), 120)
        self.assertEqual(lr.estimate_minutes("?"), 0)
        rota = find(tasks_of(), "Sort the volunteer rota - 1/2")
        self.assertEqual((rota.name, rota.estimate_minutes, rota.estimated_thirties), ("Sort the volunteer rota", 30, 1))
        self.assertEqual(find(tasks_of(), "Count the float - 20").estimate_minutes, 20)

    def test_known_kinds_split_and_the_rest_stays_in_the_name(self):
        cases = {
            "Order lanyards (pack of 50) - $18": ("Order lanyards (pack of 50)", ["price"]),
            "Router - guest network": ("Router - guest network", []),
            "Tidy the shed - by Friday": ("Tidy the shed", ["when"]),
            "Tidy the shed - Saturday": ("Tidy the shed", ["when"]),
            "Ring the plumber - 3:30": ("Ring the plumber", ["when"]),
            "Hang the bunting - me": ("Hang the bunting", ["owner"]),
            "Fix the gate - waiting on hinges - 45": ("Fix the gate", ["estimate", "blocker"]),
            "Harbour Lights - 1987": ("Harbour Lights - 1987", []),  # a year in a title, not an estimate
            "Saturday - Sweep the yard": ("Sweep the yard", ["when"]),
            "FIRST - Sweep the yard": ("Sweep the yard", ["order"]),
            "Sweep the yard - 60+": ("Sweep the yard", ["estimate"]),
            "Sweep the yard --- this grew into a bigger job": ("Sweep the yard", ["update"]),
        }
        for line, (name, kinds) in cases.items():
            got = lr.read_text(line)
            self.assertEqual((got.name, [n["kind"] for n in got.notes]), (name, kinds), line)

    def test_owner_names_only_when_known(self):
        self.assertEqual(lr.read_text("Hang the bunting - Priya").name, "Hang the bunting - Priya")
        self.assertEqual(lr.read_text("Hang the bunting - Priya", owners=["Priya"]).name, "Hang the bunting")

    def test_parentheses(self):
        tasks = tasks_of()
        self.assertEqual(find(tasks, "Cable (short)").name, "Cable (short)")  # tells two items apart
        self.assertEqual(find(tasks, "Cable (long)").name, "Cable (long)")
        sign = find(tasks, "Repaint the stall sign (after the primer dries)")
        self.assertEqual(sign.name, "Repaint the stall sign")
        self.assertEqual(sign.notes[0], {"kind": "dependency", "text": "after the primer dries"})
        for text, kind in (("Buy a kettle (~$40)", "price"), ("Buy a kettle (both?)", "doubt"),
                           ("Buy a kettle (but not in stock)", "fact"), ("Buy a kettle (at the market)", "place"),
                           ("Buy a kettle (2)", None), ("Buy a kettle (stainless)", None)):
            got = lr.read_text(text)
            self.assertEqual(got.notes[0]["kind"] if got.notes else None, kind, text)

    def test_identity_is_the_whole_line(self):
        tasks = tasks_of()
        lanyards = find(tasks, "Order lanyards (pack of 50) - $18")
        self.assertEqual(lanyards.id, item_id("n_fund", "Order lanyards (pack of 50) - $18", 0))
        self.assertEqual(lanyards.name, "Order lanyards (pack of 50)")


class TestNesting(unittest.TestCase):
    """C: parents name the item; a parent with open children is a container; ticks don't propagate; open children
    under a ticked parent are leftovers."""

    def setUp(self):
        self.tasks = tasks_of()

    def test_a_container_is_never_offered_and_shows_its_progress(self):
        ask = find(self.tasks, "Ask sponsors for raffle prizes")
        self.assertTrue(lr.is_container(ask))
        self.assertFalse(lr.offerable(ask))
        self.assertEqual(lr.progress_count(ask), "1 of 4 done")  # the cancelled one isn't counted
        self.assertTrue(lr.offerable(find(self.tasks, "Larkspur Hardware")))

    def test_a_parent_whose_children_are_all_ticked_stays_open_and_is_offered(self):
        stalls = find(self.tasks, "Set up the stalls")
        self.assertEqual(lr.progress_count(stalls), "2 of 2 done")
        self.assertFalse(lr.is_container(stalls))
        self.assertTrue(lr.offerable(stalls))

    def test_a_leftover_under_a_ticked_parent_keeps_its_parent_in_its_name(self):
        from thirties_core.item_path import label
        chairs = find(self.tasks, "Return the spare chairs")
        self.assertEqual(chairs.state, lr.LEFTOVER)
        self.assertIsNone(chairs.parent_task_id)  # its parent is ticked: not an item
        self.assertEqual(label(chairs, self.tasks), "Venue › Book the hall › Return the spare chairs")
        self.assertFalse(lr.offerable(chairs))

    def test_the_name_with_its_parents_is_what_matching_and_the_model_get(self):
        from thirties_core.item_path import label
        larkspur = find(self.tasks, "Larkspur Hardware")
        self.assertEqual(lr.match_text(larkspur), "Ask sponsors for raffle prizes Larkspur Hardware")
        self.assertEqual(label(larkspur, self.tasks), "Sponsors › Ask sponsors for raffle prizes › Larkspur Hardware")

    def test_done_matching_goes_by_parents_and_name(self):
        from thirties_core.done_on_lists import matches
        tasks = tasks_of(MOVE, "n_move", "House move")
        assistant = SimpleNamespace(s=SimpleNamespace(tasks=tasks))
        found = matches(assistant, "Close the old account", "closed the old power account")
        power = next(t for t in tasks if t.title == "Close the old account" and t.path == ["Power"])
        self.assertEqual([t.id for t in found], [power.id])

    def test_done_matching_reads_the_name_not_the_notes(self):
        from thirties_core.done_on_lists import matches
        tasks = tasks_of("- [ ] Fix the gate - waiting on hinges - 45\n- [ ] Prize ideas:\n")
        assistant = SimpleNamespace(s=SimpleNamespace(tasks=tasks))
        self.assertEqual([t.title for t in matches(assistant, "Fixed the gate", "fixed the gate")],
                         ["Fix the gate - waiting on hinges - 45"])
        self.assertEqual(matches(assistant, "Prize ideas", "done with prize ideas"), [])  # a label is never done


class TestStates(unittest.TestCase):
    """D: drop words and dropped sections, sections setting state, blockers, and a blocker on the final step."""

    def setUp(self):
        self.tasks = tasks_of()

    def test_sections_set_the_state_of_what_is_under_them(self):
        expect = {"Ask the council about parking": lr.ON_HOLD, "Hire a bouncy castle": lr.DROPPED,
                  "A second raffle in autumn": lr.LATER_TALK, "A card reader for the stalls": lr.UNDECIDED}
        for title, state in expect.items():
            t = find(self.tasks, title)
            self.assertEqual(t.state, state, title)
            self.assertFalse(lr.offerable(t), title)

    def test_section_names(self):
        for name, want in (("Current", (lr.CURRENT, "")), ("In progress", (lr.CURRENT, "")),
                           ("Today", (lr.CURRENT, "")), ("Ongoing", (lr.LONG_RUNNING, "")),
                           ("Probation", (lr.ON_HOLD, "")), ("Paused", (lr.ON_HOLD, "")),
                           ("Done", (lr.CLOSED, "")), ("Finished", (lr.CLOSED, "")),
                           ("Scrapped", (lr.DROPPED, "")), ("Cancelled", (lr.DROPPED, "")),
                           ("Should I get one?", (lr.UNDECIDED, "")), ("Bring up with the coach", (lr.LATER_TALK, "")),
                           ("Previous plan", (lr.REFERENCE, "")), ("Blocked by the boiler swap", ("", "the boiler swap")),
                           ("Garden", ("", ""))):
            self.assertEqual(lr.section_state(name), want, name)
        for t in (find(tasks_of("## Current\n- [ ] Rake leaves\n"), "Rake leaves"),
                  find(tasks_of("## Ongoing\n- [ ] Learn the ukulele\n"), "Learn the ukulele")):
            self.assertTrue(lr.offerable(t), t.title)

    def test_bold_and_label_lines_count_as_sections(self):
        body = "**Scrapped**\n- [ ] Paint the van\n\nPaused:\n- [ ] Clean the loft\n"
        tasks = tasks_of(body)
        self.assertEqual(find(tasks, "Paint the van").state, lr.DROPPED)
        self.assertEqual(find(tasks, "Clean the loft").state, lr.ON_HOLD)

    def test_a_blocked_by_heading_blocks_everything_under_it(self):
        tasks = tasks_of("### Blocked by the boiler swap\n- [ ] Bleed the radiators\n- [ ] Paint the airing cupboard\n")
        for t in tasks:
            self.assertEqual(t.blocker, "the boiler swap")
            self.assertTrue(lr.offerable(t))  # it can go on a later plan...
            self.assertFalse(lr.do_now(t))    # ...but it isn't for now

    def test_a_drop_word_unticked_is_dropped_too(self):
        cycles = find(self.tasks, "Brightwater Cycles - cancelled")
        self.assertEqual((cycles.state, cycles.name), (lr.DROPPED, "Brightwater Cycles"))
        self.assertFalse(lr.offerable(cycles))

    def test_a_blocker_keeps_it_out_of_do_now(self):
        bakery = find(self.tasks, "Okonkwo Bakery - no contact yet")
        self.assertEqual(bakery.blocker, "no contact yet")
        self.assertFalse(lr.do_now(bakery))

    def test_a_blocker_on_the_last_step_leaves_the_work_before_it(self):
        parcels = find(self.tasks, "Pack the prize parcels - waiting on postage")
        self.assertEqual(parcels.blocker, "waiting on postage")
        self.assertTrue(parcels.blocker_last_step)
        self.assertTrue(lr.do_now(parcels))
        self.assertIn("only the last step waits", lr.prompt_tag(parcels))


class TestBodyEvidence(unittest.TestCase):
    """E: per-item sections of the body show progress; never ticking anything; an unmatched section is raised once."""

    def setUp(self):
        self.tasks = tasks_of()

    def test_progress_from_the_body(self):
        expect = {"Larkspur Hardware": "ready",        # signed off
                  "Okonkwo Bakery - no contact yet": "started",  # a greeting only
                  "Brightwater Cycles - cancelled": "not_started",
                  "Pack the prize parcels - waiting on postage": "not_started"}
        for title, progress in expect.items():
            self.assertEqual(find(self.tasks, title).progress, progress, title)
        self.assertEqual(find(self.tasks, "Larkspur Hardware").state, "")  # evidence never ticks or closes it
        dental = next(ln for ln in lr.read_note(FUNDRAISER).lines if ln.text == "Fennimore Dental")
        self.assertEqual(dental.progress, "ready")  # marked ready (and ticked by the user, not by Thirties)

    def test_drafting(self):
        body = "- [ ] Sponsors\n    - [ ] Larkspur Hardware\n\n## Larkspur Hardware\nHi Larkspur Hardware,\nA first go at it\n"
        self.assertEqual(find(tasks_of(body), "Larkspur Hardware").progress, "drafting")

    def test_the_container_says_how_far_its_items_have_got(self):
        ask = find(self.tasks, "Ask sponsors for raffle prizes")
        tag = lr.prompt_tag(ask, self.tasks)
        self.assertIn("container: 1 of 4 done", tag)
        self.assertIn("1 ready", tag)
        self.assertIn("1 only started", tag)

    def test_a_section_with_no_item_is_a_possible_missing_item(self):
        reading = lr.read_note(FUNDRAISER)
        self.assertEqual([s.head for s in reading.unmatched], ["Pemberton Books"])

    def test_a_plain_notes_heading_under_a_list_is_not_a_missing_item(self):
        body = "- [ ] Sweep the yard\n\n## Notes\nThe broom is in the shed.\n"
        self.assertEqual(lr.read_note(body).unmatched, [])

    def test_raised_once(self):
        sched = make_scheduler()
        notes = [{"id": "n_fund", "title": "Spring fundraiser", "body": FUNDRAISER}]
        first = lr.once_lines(sched.state_db, self.tasks, notes)
        self.assertTrue(any("Venue › Book the hall › Return the spare chairs" in x for x in first), first)
        self.assertTrue(any("Pemberton Books" in x for x in first), first)
        self.assertEqual(lr.once_lines(sched.state_db, self.tasks, notes), [])


class TestWhatTheConsumersSee(ListsTestCase):
    """Every part of Thirties gets the same reading: the day's options, the plans for a week and a month, the model's
    backlog, and filing."""

    notes = {"n_today": ("Today To-Do", "1. Tasks", "", "today"),
             "n_fund": ("Spring fundraiser", "Club", FUNDRAISER, "list")}

    HELD = ("Ask sponsors for raffle prizes", "Return the spare chairs", "Ask the council about parking",
            "Hire a bouncy castle", "A second raffle in autumn", "A card reader for the stalls",
            "Which banner do we keep?", "The blue one", "Prize ideas:", "https://shop.example.com/banners/item-4471",
            "Brightwater Cycles - cancelled")

    def setUp(self):
        super().setUp()
        for t in self.services.tasks:
            t.urgency = "now"  # everything pressing: only the reading keeps an item off the day

    def test_the_days_options(self):
        cands = self.t.assistant.proposals.candidates(self.t.day_plan)
        titles = [c["task"].title for c in cands if c.get("task") is not None]
        self.assertIn("Ask sponsors for raffle prizes: Larkspur Hardware", [c["title"] for c in cands])
        self.assertIn("Larkspur Hardware", titles)
        self.assertIn("Pack the prize parcels - waiting on postage", titles)  # only the sending waits
        for held in self.HELD + ("Okonkwo Bakery - no contact yet", "Print the raffle tickets"):
            self.assertNotIn(held, titles)

    def test_the_week_and_month_plans(self):
        from thirties_core.horizons import candidates_for_month, candidates_for_week
        for got in (candidates_for_week(self.t, limit=None), candidates_for_month(self.t, limit=None)):
            titles = [c.title for c in got]
            for held in self.HELD:
                self.assertNotIn(held, titles)

    def test_the_models_backlog(self):
        from thirties_core.brains.frontier import FrontierBrain
        tasks = self.services.tasks
        text = FrontierBrain(self.t, None, tasks=tasks, active_notes=self.active_notes()).build_backlog_summary()
        ask = find(tasks, "Ask sponsors for raffle prizes")
        line = next(x for x in text.splitlines() if f"[ID: {ask.id}]" in x)
        self.assertIn("container: 1 of 4 done", line)
        chairs = find(tasks, "Return the spare chairs")
        self.assertIn("left open under a finished item", next(x for x in text.splitlines() if chairs.id in x))
        self.assertIn("HOW THE LISTS READ", text)

    def test_a_ticked_dropped_line_is_filed_in_dropped_not_done(self):
        from thirties_core.done_log import DoneLog
        log = DoneLog(self.sched.state_db)
        log.sweep(self.t.assistant)  # the first look
        body = self.api.notes["n_fund"]["body"]
        self.api.notes["n_fund"]["body"] = body.replace("- [ ] Hire a bouncy castle", "- [x] Hire a bouncy castle")
        set_debug_time(at("14:05"))
        log.sweep(self.t.assistant)
        set_debug_time(at("14:40"))
        moved = log.sweep(self.t.assistant)
        self.assertEqual(len(moved), 1, moved)
        titles = {n.get("title") for n in self.api.notes.values()}
        self.assertIn("Dropped 2026-10", titles)
        self.assertNotIn("Done 2026-10", titles)
        self.assertNotIn("bouncy castle", self.api.notes["n_fund"]["body"])

    def test_a_drop_word_typed_as_it_is_ticked_is_filed_in_dropped(self):
        from thirties_core.done_log import DoneLog
        log = DoneLog(self.sched.state_db)
        log.sweep(self.t.assistant)
        body = self.api.notes["n_fund"]["body"]
        self.api.notes["n_fund"]["body"] = body.replace("- [ ] Count the float - 20", "- [x] Count the float - 20 - nope")
        set_debug_time(at("14:05"))
        log.sweep(self.t.assistant)
        set_debug_time(at("14:40"))
        log.sweep(self.t.assistant)
        dropped = next(n for n in self.api.notes.values() if n.get("title") == "Dropped 2026-10")
        self.assertIn("Count the float - 20 - nope", dropped["body"])
        self.assertIn("· dropped: nope", dropped["body"])


class TestNeverWriteAnId(unittest.TestCase):
    """F: a line whose text is an item id or a bare id-like token is refused and logged, never written."""

    def setUp(self):
        self.sched = make_scheduler()
        cfg = self.sched.config
        cfg.joplin.write_back, cfg.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n_list": {"id": "n_list", "title": "Odd jobs", "body": "- [ ] Oil the hinges\n"}})
        from thirties_core.joplin_writeback import JoplinWriter
        self.writer = JoplinWriter(cfg, self.sched.state_db, self.api)

    def test_what_looks_like_an_id(self):
        for text in ("7c0e5b2a9d4f8e13", "  7C0E5B2A9D4F8E13 ", "media:goodreads:5521", "ID: 7c0e5b2a",
                     "3f2b8c1e-4d5a-4b6c-9e7f-0a1b2c3d4e5f", "task_9f3e21"):
            self.assertTrue(lr.looks_like_id(text), text)
        for text in ("Oil the hinges", "Buy 2 cables", "deadbeef", "Call 555 0199", "Q3 report", "Facade"):
            self.assertFalse(lr.looks_like_id(text), text)

    def test_adding_an_id_as_a_line_is_refused_and_logged(self):
        from thirties_core.joplin_writeback import WriteLog
        status, msg = self.writer.submit({"op": "add_task", "note_id": "n_list", "title": "7c0e5b2a9d4f8e13"})
        self.assertEqual(status, "failed")
        self.assertIn("id", msg)
        self.assertEqual(self.api.notes["n_list"]["body"], "- [ ] Oil the hinges\n")
        logged = WriteLog(self.sched.state_db).since("")
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["ok"], 0)

    def test_every_change_that_writes_words_is_checked(self):
        for op in ({"op": "rename", "note_id": "n_list", "title": "Oil the hinges", "new_title": "7c0e5b2a9d4f8e13"},
                   {"op": "split", "note_id": "n_list", "title": "Oil the hinges",
                    "parts": ["Buy oil", "media:letterboxd:88"]},
                   {"op": "break_down", "note_id": "n_list", "parent_title": "Oil the hinges",
                    "steps": ["7c0e5b2a9d4f8e13"]}):
            status, _ = self.writer.submit(op)
            self.assertEqual(status, "failed", op["op"])
        self.assertEqual(self.api.notes["n_list"]["body"], "- [ ] Oil the hinges\n")

    def test_a_queued_one_is_never_delivered(self):
        from datetime import datetime, timezone
        self.sched.state_db.outbox_add(json.dumps({"op": "add_task", "note_id": "n_list", "title": "7c0e5b2a9d4f8e13"}),
                                       datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.writer.flush(launch=False), 0)
        self.assertEqual(self.api.notes["n_list"]["body"], "- [ ] Oil the hinges\n")

    def test_words_still_go_through(self):
        status, _ = self.writer.submit({"op": "add_task", "note_id": "n_list", "title": "Sand the gate"})
        self.assertEqual(status, "done")
        self.assertIn("- [ ] Sand the gate", self.api.notes["n_list"]["body"])
        # looking an item up by its words is never refused, even when the words are odd
        self.assertIsNone(lr.refused_text({"op": "complete", "note_id": "n_list", "title": "7c0e5b2a9d4f8e13"}))


if __name__ == "__main__":
    unittest.main()
