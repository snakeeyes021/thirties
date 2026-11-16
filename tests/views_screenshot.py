"""Render the app's main screens to PNGs (light theme), with invented data on a temp database.

    python3 tests/headless.py -m tests.views_screenshot [out_dir] [--only a,b] [--prefix before_]

Each scene draws one screen at a fixed size, the way a window of that size shows it (no growing to fit), and prints
what scrolls: "scrolls: down 120 px, sideways 0 px". Laptop sizes: a 1366x768 screen, the window maximised, its
content pane (with the chat open, half the width) about 683x690. Default out_dir is docs/eval_reports/ui/
(gitignored). Runs only in a private headless session (tests/headless.py), never on the user's desktop; never
touches the user's data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from tests.fixtures import DAY, at, event, make_scheduler  # noqa: E402
from thirties_core.astronomy import set_debug_time  # noqa: E402
from thirties_core.models import EventSpan, SpanCategory, SpanStatus  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# a laptop: 1366x768, maximised; the content pane with the chat open, and without it
LAPTOP_CHAT = (683, 690)
LAPTOP_FULL = (1366, 690)
SMALL_LAPTOP = (640, 642)  # 1280x720, the chat open


def _pump(seconds: float = 0.5) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


def _scrolled(widget):
    out = []

    def walk(w):
        if isinstance(w, Gtk.ScrolledWindow) and w.get_mapped():
            out.append(w)
        c = w.get_first_child()
        while c is not None:
            walk(c)
            c = c.get_next_sibling()
    walk(widget)
    return out


def scroll_report(widget) -> str:
    """How far the visible content scrolls, down and sideways (0: it fits)."""
    down = side = 0
    for s in _scrolled(widget):
        v, h = s.get_vadjustment(), s.get_hadjustment()
        down = max(down, int(v.get_upper() - v.get_page_size()))
        side = max(side, int(h.get_upper() - h.get_page_size()))
    over = max(0, widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0] - widget.get_width())
    return f"scrolls: down {max(0, down)} px, sideways {max(0, side, over)} px"


def render(widget, width: int, height: int, path: Path, dark: bool = False) -> str:
    fixed = Gtk.Fixed()
    widget.set_size_request(width, height)
    fixed.put(widget, 0, 0)
    win = Adw.ApplicationWindow(content=fixed, default_width=width, default_height=height)
    win.present()
    _pump(0.8)
    report = scroll_report(widget)
    paintable = Gtk.WidgetPaintable.new(widget)
    w, h = widget.get_width(), widget.get_height()
    snap = Gtk.Snapshot()
    rgba = Gdk.RGBA()
    rgba.parse("#222226" if dark else "#fafafb")
    snap.append_color(rgba, Graphene.Rect().init(0, 0, w, h))
    paintable.snapshot(snap, w, h)
    texture = win.get_renderer().render_texture(snap.to_node(), None)
    texture.save_to_png(str(path))
    win.destroy()
    _pump(0.1)
    return report


# --- invented data ---------------------------------------------------------------------------------------------------

def day_sched():
    sched = make_scheduler([event("standup", "10:00", "10:30", "Team standup")])
    spans = [
        ("coffee", "Make coffee, pack lunch", "07:00", "07:30", SpanStatus.DONE),
        ("email", "Email back the landlord", "08:00", "08:30", SpanStatus.UNKNOWN),
        ("lunch", "Lunch", "12:00", "12:30", SpanStatus.UNKNOWN),
        ("call", "Call the garage", "12:00", "12:30", SpanStatus.PLANNED),
        ("dev", "Garden app dev", "16:00", "17:30", SpanStatus.PLANNED),
        ("dinner", "Dinner", "18:30", "19:30", SpanStatus.PLANNED),
        ("laundry", "Laundry", "21:00", "21:30", SpanStatus.PLANNED),
    ]
    for sid, title, start, end, status in spans:
        sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY, status=status))
    for d in range(-3, 4):
        if d:
            day = DAY + timedelta(days=d)
            base = datetime.combine(day, datetime.min.time(), tzinfo=at("00:00").tzinfo)
            for i, (h, m, title) in enumerate(((9, 60, "Morning run"), (14, 90, "Shed clear-out"),
                                               (19, 60, "Spanish practice"))):
                start = base + timedelta(hours=h)
                sched.state_db.upsert_span(EventSpan(f"w{d}_{i}", title, start, start + timedelta(minutes=m),
                                                     SpanCategory.ACTIVITY))
    plan, _ = sched.build_day_plan(DAY)
    plan.spans.append(EventSpan("cal-r", "Book group (Jamie)", at("19:30"), at("21:00"), SpanCategory.CALENDAR,
                                source="calendar", shown_only=True))
    return sched, plan


def day_view(sched, plan, tab: str, period: str = "day"):
    from thirties.views.day_view import DayView
    view = DayView()
    view.set_actions(lambda *_: "", None)
    page = view.timeline
    page.threaded_week = False
    page.load_week = lambda anchor, days: ([plan if d == DAY else sched.build_day_plan(d)[0] for d in days],
                                           [{"item_id": "door", "title": "Fix the door", "list": "Home"},
                                            {"item_id": "tax", "title": "File the taxes", "list": "Admin"}])
    view.set_today(DAY)
    view.refresh_plan(plan)
    view.stack.set_visible_child_name(tab)
    if tab == "timeline":
        page.stack.set_visible_child_name(period)
    return view


# --- scenes ----------------------------------------------------------------------------------------------------------

def scene_week(out: Path, prefix: str) -> list:
    sched, plan = day_sched()
    shots = []
    for name, (w, h) in (("week_laptop_chat", LAPTOP_CHAT), ("week_laptop_nochat", LAPTOP_FULL),
                         ("week_small_laptop", SMALL_LAPTOP)):
        view = day_view(sched, plan, "timeline", "week")
        path = out / f"{prefix}{name}.png"
        shots.append((path, render(view, w, h, path)))
    return shots


def scene_year(out: Path, prefix: str) -> list:
    from tests.period_screenshot import invented
    from thirties_core.period_summary import load_year
    from thirties.views.day_view import DayView
    sched = invented()
    plan, _ = sched.build_day_plan(DAY)
    view = DayView()
    view.set_actions(lambda *_: "", None)
    page = view.timeline
    page.threaded_week = False
    page.load_year = lambda y: load_year(sched, y, DAY)
    view.set_today(DAY)
    view.refresh_plan(plan)
    view.stack.set_visible_child_name("timeline")
    page.stack.set_visible_child_name("year")
    path = out / f"{prefix}timeline_year.png"
    return [(path, render(view, 960, 1100, path))]


def scene_hand(out: Path, prefix: str) -> list:
    """The Day tab's rail with now at solar midday (the hand over its mark) and just before sunset."""
    shots = []
    for name, when in (("hand_at_midday", None), ("hand_near_sunset", "18:15")):
        sched, plan = day_sched()
        noon = plan.sunrise + (plan.sunset - plan.sunrise) / 2
        set_debug_time(noon + timedelta(minutes=4) if when is None else at(when))
        view = day_view(sched, plan, "timeline", "day")
        view.timeline.scroll_to_now()
        path = out / f"{prefix}{name}.png"
        shots.append((path, render(view, *LAPTOP_CHAT, path)))
    set_debug_time(at("15:40"))
    return shots


def _wired_view(sched, plan, tab, period="day", if_time=(), pencilled=(), theirs=True):
    """A DayView with the window's providers stood in by invented data, and a real tool runner on the day."""
    from thirties_core.plan_tools import PlanTools
    view = day_view(sched, plan, tab, period)
    tools = PlanTools(plan, [], sched)
    view.set_actions(lambda name, args: tools.execute(name, args), None)
    agenda = view.agenda

    def read(name, args):
        if name == "if_there_is_time_items" and if_time:
            return "If there's time:\n" + "\n".join(f"- {t}" for t in if_time)
        if name == "pencilled_in" and pencilled:
            return "On the list, no time yet:\n" + "\n".join(f"- {t}" for t in pencilled)
        if name == "not_yours_today":
            return "- cal-r"
        return ""
    agenda.read_tool = read
    agenda.pencil_action = lambda title, action: ""
    shown = {"on": theirs}
    view.set_theirs_provider(lambda: shown["on"], lambda on: shown.update(on=on))
    view.refresh_plan(plan, force=True)
    view.stack.set_visible_child_name(tab)
    return view


def scene_agenda(out: Path, prefix: str) -> list:
    """Today's Agenda at 9:50: Not marked yet (with Not reported), Now and Work's button, then other
    people's events shown, and hidden by the one switch."""
    shots = []
    set_debug_time(at("09:50"))
    for name, theirs in (("agenda_work_theirs_shown", True), ("agenda_theirs_hidden", False)):
        sched, plan = day_sched()
        view = _wired_view(sched, plan, "day", theirs=theirs, if_time=["Repot the fern"])
        path = out / f"{prefix}{name}.png"
        shots.append((path, render(view, 683, 1500, path)))
    set_debug_time(at("15:40"))
    return shots


def scene_timeline_untimed(out: Path, prefix: str) -> list:
    """The Day tab with today's If there's time as chips over the day, and a Not reported card's live tick; then a
    later day's pencilled-in list, on the Agenda and the Timeline."""
    shots = []
    set_debug_time(at("13:10"))
    sched, plan = day_sched()
    view = _wired_view(sched, plan, "timeline", "day", if_time=["Repot the fern", "Call the plumber back"])
    path = out / f"{prefix}timeline_day_if_time.png"
    shots.append((path, render(view, 683, 900, path)))
    later, _ = sched.build_day_plan(DAY + timedelta(days=2))
    for tab in ("day", "timeline"):
        view = _wired_view(sched, later, tab, "day", pencilled=["Buy stamps", "Water the plants"])
        path = out / f"{prefix}{'agenda' if tab == 'day' else 'timeline'}_later_day_pencilled.png"
        shots.append((path, render(view, 683, 760 if tab == "day" else 700, path)))
    set_debug_time(at("15:40"))
    return shots


def scene_popovers(out: Path, prefix: str) -> list:
    """What pops up, drawn on its own: a chip's actions, a card's popover with the effort estimate."""
    shots = []
    set_debug_time(at("13:10"))
    sched, plan = day_sched()
    view = _wired_view(sched, plan, "timeline", "day", if_time=["Repot the fern"])
    chip = view.timeline.untimed_chips["Repot the fern"]
    box = chip.get_popover().get_child()
    chip.get_popover().set_child(None)
    path = out / f"{prefix}popover_if_time_chip.png"
    shots.append((path, render(_framed(box), 220, 260, path)))
    page = view.timeline
    page.estimate_of = lambda task_id: (3.0, "hours") if task_id == "t_garden" else (0.0, "hours")
    next(s for s in plan.spans if s.title == "Garden app dev").task_id = "t_garden"
    view.refresh_plan(plan, force=True)
    card = next(w for w in page.canvas.cards if w.card.span.title == "Garden app dev")
    pop = page.canvas._open_card(card)
    content = pop.get_child()
    pop.set_child(None)
    path = out / f"{prefix}popover_card_effort.png"
    shots.append((path, render(_framed(content), 320, 230, path)))
    set_debug_time(at("15:40"))
    return shots


def _framed(child):
    frame = Gtk.Frame(child=child)
    frame.add_css_class("view")
    box = Gtk.Box(margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
    box.append(frame)
    return box


def scene_conversations(out: Path, prefix: str) -> list:
    from thirties.views.conversations_dialog import AllConversationsDialog
    from thirties_core.memory import ConversationStore
    from tests.fixtures import TZ
    sched = make_scheduler()
    store = ConversationStore(sched.state_db)
    for days_ago, words, reply, summary in (
            (0, "Move lunch to 1 and dinner to 7", "Done: lunch at 1:00 PM, dinner at 7:00 PM.", ""),
            (0, "What's next?", "Garden app dev at 4:00 PM, then dinner.", ""),
            (1, "Plan the shed clear-out for Saturday", "Saturday 10:00 AM, three blocks.",
             "Shed clear-out set for Saturday morning."),
            (4, "Book the dentist", "Tuesday 3:00 PM is free.", "Dentist on Tuesday at 3.")):
        conv = store.create(DAY - timedelta(days=days_ago))
        conv.messages = [{"role": "user", "content": words}, {"role": "assistant", "content": reply}]
        conv.summary = summary
        store.save(conv)
    dialog = AllConversationsDialog(store, DAY, TZ, lambda d, i: None)
    content = dialog.get_child()
    dialog.set_child(None)
    path = out / f"{prefix}all_conversations.png"
    shots = [(path, render(content, 520, 640, path))]
    dialog2 = AllConversationsDialog(store, DAY, TZ, lambda d, i: None)
    dialog2.search.set_text("shed")
    _pump(0.4)
    content = dialog2.get_child()
    dialog2.set_child(None)
    path = out / f"{prefix}all_conversations_search.png"
    shots.append((path, render(content, 520, 400, path)))
    return shots


def scene_chat(out: Path, prefix: str) -> list:
    from thirties.views.chat_panel import ChatPanel
    panel = ChatPanel()
    panel.text_buffer.set_text("Can you move the dentsit to Thursday aftenoon?")
    path = out / f"{prefix}chat_spell_check.png"
    return [(path, render(panel, 520, 220, path))]


def scene_preferences(out: Path, prefix: str) -> list:
    """What to read, with list settings stood in by their own names (they're on another branch)."""
    import types
    from unittest import mock
    from thirties.views import settings_rows
    from thirties_core.app_core import AppCore
    from thirties_core.config import ThirtiesConfig
    import thirties_core
    core = AppCore(use_keyring=False, config=ThirtiesConfig())
    ctx = settings_rows.SettingsContext(core)
    list_settings = types.SimpleNamespace(ONE_OFF_KEY="lists.one_off_notes", DONE_NOTE_KEY="lists.done_note",
                                          ONE_OFF_CHOICES={"item": "", "someday": "", "ignore": ""})
    notes = [{"id": "n_log", "title": "What I did", "body": ""}]
    with mock.patch.object(core.joplin_engine, "list_notebooks", return_value=["1. Tasks", "Projects", "Archive"]), \
            mock.patch.object(core.joplin_engine, "fetch_active_notes", return_value=notes), \
            mock.patch.dict(sys.modules, {"thirties_core.list_settings": list_settings}), \
            mock.patch.object(thirties_core, "list_settings", list_settings, create=True):
        group = settings_rows.notebooks_group(ctx)
    group.also_row.set_text("Packing list, Garden plan")
    page = Adw.PreferencesPage()
    page.add(group)
    path = out / f"{prefix}preferences_what_to_read.png"
    return [(path, render(page, 640, 520, path))]


def scene_before_today(out: Path, prefix: str) -> list:
    """Before today: a missed item's question with Break It Down, and an item put off several times."""
    from thirties.views.before_today import BeforeTodayPage
    page = BeforeTodayPage()
    page.run_tool = lambda name, args: ""
    page.rows_provider = lambda: {"ask": [{"title": "Shed clear-out", "why": "missed 3 days running"}],
                                  "keep": [{"title": "Call the plumber back", "why": "", "when": "this week",
                                            "at": None}]}
    page.offers_provider = lambda: [{"task_id": "t_tax", "title": "File the taxes", "asked": "2026-10-15",
                                     "answer": ""}]
    page.refresh()
    path = out / f"{prefix}before_today_break_it_down.png"
    return [(path, render(page, 683, 520, path))]


def scene_before_today_narrow(out: Path, prefix: str) -> list:
    """Before today at phone width (360 px): each title has the row's width, its buttons on a line under it."""
    from thirties.views.before_today import BeforeTodayPage
    page = BeforeTodayPage()
    page.run_tool = lambda name, args: ""
    page.rows_provider = lambda: {
        "keep": [{"title": "Return the library books before the weekend", "why": "a week old", "when": "this week",
                  "at": None},
                 {"title": "Call the plumber back", "why": "it was due yesterday", "when": "today",
                  "at": at("16:00")}],
        "drop": [{"title": "Watch the meteor shower", "why": "the night is gone", "when": "next week", "at": None}],
        "restore": ["Repaint the porch railing"],
        "archive": [("2026-10-14", [{"title": "Sort the spare cables", "why": "moot", "when": "", "at": None}])]}
    page.offers_provider = lambda: [{"task_id": "t_tax", "title": "File the quarterly taxes", "asked": "2026-10-15",
                                     "answer": ""}]
    page.refresh()
    path = out / f"{prefix}before_today_narrow.png"
    return [(path, render(page, 360, 1100, path))]


def scene_reminders(out: Path, prefix: str) -> list:
    """Preferences: the Reminders switches (rush tickets, keep in touch) and the notes left for the engineers, with
    modules stood in by their own names (they're on another branch)."""
    import types
    from unittest import mock
    from thirties.views import settings_rows
    from thirties.views.engineer_notes_dialog import EngineerNotesDialog
    from thirties_core.app_core import AppCore
    from thirties_core.config import ThirtiesConfig
    import thirties_core
    notes = [types.SimpleNamespace(id="a1", at="2026-11-14T09:05:00-05:00", via="telegram",
                                   text="the check-in came twice this morning",
                                   message="note to the engineers: the check-in came twice this morning. Also, "
                                           "move lunch to 1", state="open"),
             types.SimpleNamespace(id="b2", at="2026-11-15T14:10:00-05:00", via="app",
                                   text="the week view feels cramped on the laptop",
                                   message="note to the devs: the week view feels cramped on the laptop", state="open")]
    modules = {"thirties_core.tickets": types.SimpleNamespace(SETTING="tickets.rush"),
               "thirties_core.people": types.SimpleNamespace(SETTING="people.nudges"),
               "thirties_core.engineer_notes": types.SimpleNamespace(
                   EngineerNotes=lambda db: types.SimpleNamespace(notes=lambda: list(notes)))}
    core = AppCore(use_keyring=False, config=ThirtiesConfig())
    ctx = settings_rows.SettingsContext(core)
    with mock.patch.dict(sys.modules, modules), \
            mock.patch.object(thirties_core, "tickets", modules["thirties_core.tickets"], create=True), \
            mock.patch.object(thirties_core, "people", modules["thirties_core.people"], create=True), \
            mock.patch.object(thirties_core, "engineer_notes", modules["thirties_core.engineer_notes"], create=True):
        page = Adw.PreferencesPage()
        page.add(settings_rows.reminders_group(ctx))
        page.add(settings_rows.engineer_notes_group(ctx))
    path = out / f"{prefix}preferences_reminders_and_engineer_notes.png"
    shots = [(path, render(page, 640, 420, path))]
    dialog = EngineerNotesDialog(notes)
    content = dialog.get_child()
    dialog.set_child(None)
    path = out / f"{prefix}engineer_notes.png"
    shots.append((path, render(content, 520, 460, path)))
    return shots


def scene_days_ahead(out: Path, prefix: str) -> list:
    """A later day with week draft pencilled in, on the Agenda and the Timeline."""
    sched, _ = day_sched()
    later = DAY + timedelta(days=2)
    plan, _ = sched.build_day_plan(later)
    drafts = [{"title": "Paint the hall", "task_id": "t_paint", "start": at("10:00", 2).isoformat(), "minutes": 90},
              {"title": "Call the plumber", "task_id": "", "start": at("13:30", 2).isoformat(), "minutes": 30},
              {"title": "Repot the fern", "task_id": "", "start": at("16:30", 2).isoformat(), "minutes": 30}]
    shots = []
    for tab in ("day", "timeline"):
        view = day_view(sched, plan, tab, "day")
        view.set_drafts_provider(lambda day: drafts if day == later else [])
        view.refresh_plan(plan, force=True)
        name = "agenda_days_ahead_pencilled" if tab == "day" else "timeline_days_ahead_pencilled"
        path = out / f"{prefix}{name}.png"
        shots.append((path, render(view, 683, 1150 if tab == "day" else 900, path)))
    return shots


def scene_after_work(out: Path, prefix: str) -> list:
    """Finished work tapped at 4:35 PM: the Agenda's button, and the chat with what log_day_times says
    after work (the thirties left and a few that fit), as Thirties' own message."""
    from thirties.views.chat_panel import ChatPanel
    from thirties_core.conversation import ConversationManager
    from thirties_core.day_actions import work_reply
    from thirties_core.inference import MockInferenceEngine
    shots = []
    set_debug_time(at("16:20"))
    sched, plan = day_sched()
    view = _wired_view(sched, plan, "day")
    path = out / f"{prefix}agenda_finished_work_button.png"
    shots.append((path, render(view, 683, 1400, path)))
    result = ("Recorded: finished work 4:35 PM. After work: 2 daylight thirties and 8 dark thirties free before bed. "
              "A few that fit:\nShed clear-out (1 h 30 min)\nCall the plumber (30 min)\nRepot the fern (30 min)")
    said, rest = work_reply(result)
    manager = ConversationManager(plan, [], [], sched, inference_engine=MockInferenceEngine(), active_notes=[])
    manager.add_assistant_message(rest)
    panel = ChatPanel(manager)
    path = out / f"{prefix}chat_after_work.png"
    shots.append((path, render(panel, 520, 420, path)))
    print(f"(the notice says: {said})")
    set_debug_time(at("15:40"))
    return shots


SCENES = {"days_ahead": scene_days_ahead, "after_work": scene_after_work, "reminders": scene_reminders, "week": scene_week, "year": scene_year, "hand": scene_hand, "agenda": scene_agenda,
          "untimed": scene_timeline_untimed, "popovers": scene_popovers, "conversations": scene_conversations,
          "chat": scene_chat, "preferences": scene_preferences, "before_today": scene_before_today,
          "before_today_narrow": scene_before_today_narrow}


def main() -> int:
    from tests.test_gui import isolated
    if not isolated():
        print("Run this through tests/headless.py: it needs a private headless session.", file=sys.stderr)
        return 2
    args = sys.argv[1:]
    only, prefix = None, ""
    if "--only" in args:
        i = args.index("--only")
        only = set(args[i + 1].split(","))
        del args[i:i + 2]
    if "--prefix" in args:
        i = args.index("--prefix")
        prefix = args[i + 1]
        del args[i:i + 2]
    out = Path(args[0]) if args else ROOT / "docs" / "eval_reports" / "ui"
    out.mkdir(parents=True, exist_ok=True)
    pkg = tempfile.mkdtemp(prefix="thirties_shot_pkg_")
    os.symlink(ROOT / "src", os.path.join(pkg, "thirties"))
    sys.path.insert(0, pkg)
    Adw.init()
    css = Gtk.CssProvider()
    css.load_from_path(str(ROOT / "src" / "style.css"))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    set_debug_time(at("15:40"))
    try:
        for name, scene in SCENES.items():
            if only is None or name in only:
                for path, report in scene(out, prefix):
                    print(f"{path}  {report}")
    finally:
        set_debug_time(None)
        shutil.rmtree(pkg, True)
    _ = date
    return 0


if __name__ == "__main__":
    sys.exit(main())
