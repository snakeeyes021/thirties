"""Something to read or watch from the user's lists, saying where it came from, the Media Queue first as
"up next". Invented titles and lists throughout."""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.media_picks import candidates, name_sources, offer
from thirties_core.models import TaskItem


def wish(source, kind, title, list_name):
    from thirties_core.media_sources import MediaItem, media_items_as_tasks
    return media_items_as_tasks([MediaItem(source, kind, title, item_id=title.lower().replace(" ", "-"),
                                           list_name=list_name, year=2001)])[0]


def note_item(tid, title, note, section=""):
    return TaskItem(id=tid, source_notebook="4. Media", title=title, parent_note_title=note,
                    parent_note_id="n-" + note.lower().replace(" ", "-"), section=section)


TASKS = [
    wish("letterboxd", "film", "Harbor Lights Again", "watchlist"),
    wish("goodreads", "book", "The Lantern Almanac", "to-read"),
    wish("serializd", "show", "Kettle Street", "watchlist"),
    note_item("q1", "Watch The Moth Parade", "Media Queue"),
    note_item("q2", "Read Salt and Timber", "Media Queue"),
    note_item("l1", "Blue Hour Sessions", "Want to listen"),
    note_item("x1", "Fix the gutter", "House jobs"),
]


class CandidatesTest(unittest.TestCase):
    def test_every_list_with_its_source_queue_first(self):
        picks = candidates(TASKS)
        self.assertEqual([p.title for p in picks[:2]], ["Watch The Moth Parade", "Read Salt and Timber"])
        self.assertTrue(all(p.queue for p in picks[:2]))
        sources = {p.title: p.source for p in picks}
        self.assertEqual(sources["Harbor Lights Again (2001)"], "your Letterboxd watchlist")
        self.assertEqual(sources["The Lantern Almanac (2001)"], "your Goodreads to-read shelf")
        self.assertEqual(sources["Kettle Street (2001)"], "your Serializd watchlist")
        self.assertEqual(sources["Blue Hour Sessions"], "your Want to listen")
        self.assertEqual(sources["Watch The Moth Parade"], "your Media Queue")
        self.assertNotIn("Fix the gutter", sources)  # not a media list

    def test_by_kind(self):
        watch = [p.title for p in offer(TASKS, "movie", today=DAY)]
        self.assertIn("Harbor Lights Again (2001)", watch)
        self.assertIn("Kettle Street (2001)", watch)
        self.assertNotIn("The Lantern Almanac (2001)", watch)
        self.assertEqual(watch[0], "Watch The Moth Parade")  # up next comes first
        listen = [p.title for p in offer(TASKS, "listen", today=DAY)]
        self.assertEqual(listen, ["Blue Hour Sessions"])

    def test_the_same_few_arent_offered_every_day(self):
        many = [wish("letterboxd", "film", f"Film number {i}", "watchlist") for i in range(20)]
        sched = make_scheduler()
        first = {p.id for p in offer(many, "watch", sched.state_db, DAY, limit=5)}
        from datetime import timedelta
        second = {p.id for p in offer(many, "watch", sched.state_db, DAY + timedelta(days=1), limit=5)}
        self.assertFalse(first & second)


class NameSourcesTest(unittest.TestCase):
    def test_a_pick_named_without_its_source_gets_it(self):
        offered = {"Harbor Lights Again (2001)": "your Letterboxd watchlist"}
        out = name_sources("How about Harbor Lights Again (2001)? It's light.", offered)
        self.assertEqual(out, "How about Harbor Lights Again (2001) (from your Letterboxd watchlist)? It's light.")

    def test_a_reply_that_says_the_source_is_left_alone(self):
        offered = {"Harbor Lights Again (2001)": "your Letterboxd watchlist"}
        text = "From your Letterboxd watchlist: Harbor Lights Again."
        self.assertEqual(name_sources(text, offered), text)
        self.assertEqual(name_sources("Nothing tonight.", offered), "Nothing tonight.")


class InConversationTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.engine = MockInferenceEngine()
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, list(TASKS), pending, sched, inference_engine=self.engine,
                                     active_notes=[])

    def test_the_queue_is_in_the_prompt_as_up_next(self):
        prompt = self.m.messages[0]["content"]
        self.assertIn("MEDIA QUEUE", prompt)
        self.assertIn("- Watch The Moth Parade id=q1", prompt)

    def test_suggesting_alongside_work_gives_its_time_and_the_reply_names_the_source(self):
        self.m.execute_tool("add_event", {"title": "Fold the laundry", "start": "7:00 PM",
                                          "duration": "2 hours"})
        self.engine.responses = [reply('CALL suggest_media {"kind": "watch", "alongside": "Fold the laundry"}'),
                                 reply('CALL add_event {"title": "Kettle Street", "start": "7:00 PM", '
                                       '"duration": "2 hours"}'),
                                 reply("Put on Kettle Street while you fold the laundry.")]
        out = self.m.send_user_message("find something easy to watch while I fold the laundry")
        tool_results = " ".join(str(m.get("content")) for m in self.m.brain.messages if m.get("role") == "tool"
                                or "To choose from" in str(m.get("content")))
        self.assertIn("Alongside Fold the laundry: 7:00 PM – 9:00 PM", tool_results)
        self.assertIn("Kettle Street (from your Serializd watchlist)", out)
        titles = [s.title for s in self.m.day_plan.spans if s.start_dt == at("19:00")]
        self.assertIn("Kettle Street", titles)
        self.assertIn("Fold the laundry", titles)  # both share the time


if __name__ == "__main__":
    unittest.main()
