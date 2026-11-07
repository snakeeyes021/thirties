"""Profile → Routines: your routines and daily anchors.

Each routine (a note with the routine role) with how often it repeats, its placement (which steps go together,
the usual time of each part, where a missed part goes), and whether it's skipped. Each daily anchor (lunch,
dinner, the cats' dinner) with its time, length, whether it's fixed, and its days. Everything is read and changed
through the core (thirties_core/routine_profile.py, over the assistant's own set_routine / set_anchor), so what
you set here is what the assistant sees, on every device.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from gi.repository import Adw, GLib, Gtk

from thirties_core import routine_profile as rp
from thirties_core.week import DAYS

DAY_NAMES = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


class RoutinesPage(Gtk.Stack):
    """``routines`` returns the assistant's RoutineTools (or None when there's no day loaded yet). Built when first
    shown, and again after every change."""

    def __init__(self, routines: Optional[Callable[[], Any]],
                 on_changed: Optional[Callable[[], None]] = None,
                 toast: Optional[Callable[[str], None]] = None) -> None:
        super().__init__()
        self.routines, self.on_changed, self.toast = routines, on_changed, toast
        self.routine_rows: Dict[str, Adw.ExpanderRow] = {}
        self.anchor_rows: Dict[str, Adw.ExpanderRow] = {}
        self.offer_rows: List[Adw.ActionRow] = []
        empty = Adw.StatusPage(icon_name="alarm-symbolic", title="Nothing Here Yet",
                               description="Open a day first: routines come from your notes with the routine role.")
        empty.add_css_class("compact")
        self.add_named(empty, "empty")
        self.page = Adw.PreferencesPage()
        self.add_named(self.page, "routines")
        self._groups: List[Adw.PreferencesGroup] = []
        self._built = False
        self.connect("map", lambda *_: self.reload() if not self._built else None)

    # --- helpers ---

    def _tools(self):
        return self.routines() if self.routines else None

    def _say(self, text: str) -> None:
        if self.toast and text:
            self.toast(text.split(". ")[0].rstrip(".") + ".")

    def _do(self, action: Callable[[], str]) -> bool:
        """Run a change through the core; a refusal (ToolError, ValueError) is shown, not raised."""
        try:
            self._say(action())
        except Exception as e:  # the tools' refusals are plain sentences
            self._say(str(e))
            return False
        if self.on_changed:
            self.on_changed()
        GLib.idle_add(lambda: self.reload() and False)
        return True

    # --- building ---

    def reload(self) -> None:
        self._built = True
        opened = {t for t, r in {**self.routine_rows, **self.anchor_rows}.items() if r.get_expanded()}
        for group in self._groups:
            self.page.remove(group)
        self._groups, self.routine_rows, self.anchor_rows, self.offer_rows = [], {}, {}, []
        tools = self._tools()
        if tools is None or tools.db is None:
            self.set_visible_child_name("empty")
            return
        routines = rp.routine_rows(tools)
        group = Adw.PreferencesGroup(title="Routines", description=(
            "Each morning Thirties puts the day's routines on the plan by itself. Mark a note as a routine in "
            "Lists; set here how often it repeats and where its parts go."))
        if not routines:
            group.add(Adw.ActionRow(title="No routines yet", subtitle="Mark a note of steps as a routine in Lists."))
        for row in routines:
            group.add(self._routine_row(row))
        self._add(group)
        self._add(self._anchors_group(tools))
        for title, r in {**self.routine_rows, **self.anchor_rows}.items():
            r.set_expanded(title in opened)  # a change rebuilds the page: what was open stays open
        self.set_visible_child_name("routines")

    def _add(self, group: Adw.PreferencesGroup) -> None:
        self.page.add(group)
        self._groups.append(group)

    # --- a routine ---

    def _routine_row(self, row: Dict[str, Any]) -> Adw.ExpanderRow:
        title = row["title"]
        exp = Adw.ExpanderRow(title=title, subtitle=rp.summary(row))
        exp.routine = row
        kinds = [k for k, _ in rp.REPEAT_KINDS]
        repeat = Adw.ComboRow(title="Repeats", model=Gtk.StringList.new([label for _, label in rp.REPEAT_KINDS]))
        repeat.set_selected(kinds.index(row["repeat"]["kind"]) if row["repeat"]["kind"] in kinds else 0)
        every = Adw.SpinRow.new_with_range(2, 60, 1)
        every.set_title("Every how many days")
        every.set_value(row["repeat"]["n"])
        days_row = Adw.ActionRow(title="On")
        day_box = Gtk.Box(valign=Gtk.Align.CENTER)
        day_box.add_css_class("linked")
        day_buttons = {}
        for d in DAYS:
            b = Gtk.ToggleButton(label=DAY_NAMES[d], active=d in row["repeat"]["days"])
            day_box.append(b)
            day_buttons[d] = b
        days_row.add_suffix(day_box)

        def show_extras() -> None:
            kind = kinds[repeat.get_selected()]
            every.set_visible(kind == "days")
            days_row.set_visible(kind == "weekdays")
        show_extras()

        def save_repeat(*_a) -> None:
            show_extras()
            kind = kinds[repeat.get_selected()]
            chosen = [d for d, b in day_buttons.items() if b.get_active()]
            if kind == "weekdays" and not chosen:
                return  # waits for a day to be picked
            if (kind, int(every.get_value()), chosen) == (row["repeat"]["kind"], row["repeat"]["n"],
                                                         row["repeat"]["days"]):
                return
            tools = self._tools()
            self._do(lambda: rp.save_repeat(tools, title, kind, int(every.get_value()), chosen))
        repeat.connect("notify::selected", save_repeat)
        every.connect("notify::value", save_repeat)
        for b in day_buttons.values():
            b.connect("toggled", save_repeat)
        exp.repeat_row, exp.every_row, exp.day_buttons = repeat, every, day_buttons
        for r in (repeat, every, days_row):
            exp.add_row(r)

        source = Adw.ActionRow(title="Placement", subtitle=row["placement_text"])
        source.add_css_class("property")
        exp.add_row(source)
        exp.part_rows = []
        parts = row["parts"] or [{"items": list(row["steps"]), "time": "", "fallback": ""}]
        for i, part in enumerate(parts):
            exp.part_rows.append(self._part_rows(exp, i, part))
        add_part = Adw.ButtonRow(title="Add a part", start_icon_name="list-add-symbolic")
        add_part.connect("activated", lambda _b: self._save_parts(exp, extra=True))
        exp.add_row(add_part)

        if row["skip"]:
            skip = Adw.ActionRow(title=row["skip"])
            resume = Gtk.Button(label="Resume", valign=Gtk.Align.CENTER)
            resume.connect("clicked", lambda _b: self._do(lambda: rp.resume(self._tools(), title)))
            skip.add_suffix(resume)
            exp.add_row(skip)
            exp.resume_button = resume
        self.routine_rows[title] = exp
        return exp

    def _part_rows(self, exp: Adw.ExpanderRow, i: int, part: Dict[str, Any]) -> Dict[str, Adw.EntryRow]:
        """A part: its steps (together in one thirty), its usual time, and where it goes if missed."""
        steps = Adw.EntryRow(title=f"Part {i + 1}: steps, separated by commas", text=", ".join(part["items"]),
                             show_apply_button=True)
        time = Adw.EntryRow(title=f"Part {i + 1}: usual time", text=part["time"], show_apply_button=True)
        fallback = Adw.EntryRow(title=f"Part {i + 1}: if it's missed (e.g. after work)", text=part["fallback"],
                                show_apply_button=True)
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove this part")
        remove.add_css_class("flat")
        steps.add_suffix(remove)
        rows = {"steps": steps, "time": time, "fallback": fallback}
        for r in rows.values():
            r.connect("apply", lambda _r: self._save_parts(exp))
            exp.add_row(r)
        remove.connect("clicked", lambda _b: self._save_parts(exp, drop=i))
        return rows

    def _save_parts(self, exp: Adw.ExpanderRow, drop: Optional[int] = None, extra: bool = False) -> None:
        parts = []
        for k, rows in enumerate(exp.part_rows):
            if k == drop:
                continue
            parts.append({"items": [s for s in rows["steps"].get_text().split(",")],
                          "time": rows["time"].get_text().strip(), "fallback": rows["fallback"].get_text()})
        if extra:  # a new part: the routine's steps not in any part yet, at the thirty after the last one
            placed = {i.strip().lower() for p in parts for i in p["items"]}
            rest = [s for s in exp.routine["steps"] if s.lower() not in placed]
            if not rest:
                self._say("Every step is in a part already: take some out of a part first.")
                return
            parts.append({"items": rest, "time": "", "fallback": ""})
            exp.part_rows.append(self._part_rows(exp, len(exp.part_rows), parts[-1]))
            return  # saved once it has a time
        if not any(p["time"] for p in parts):
            self._say("Give each part a usual time.")
            return
        title = exp.routine["title"]
        self._do(lambda: rp.save_placement(self._tools(), title, parts))

    # --- anchors ---

    def _anchors_group(self, tools) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Daily anchors", description=(
            "Things on no list that happen at about the same time (meals, feeding a pet). They go on the plan by "
            "themselves."))
        offer = Gtk.Button(label="Offer the defaults again", valign=Gtk.Align.CENTER)
        offer.add_css_class("flat")
        group.set_header_suffix(offer)
        self.offer_button = offer
        offers_at: List[Adw.ActionRow] = []

        def show_offers(_b) -> None:
            for r in offers_at:
                group.remove(r)
            offers_at.clear()
            found = rp.default_offers(self._tools())
            if not found:
                self._say("Lunch and dinner are on already.")
            for o in found:
                r = Adw.ActionRow(title=o["title"], subtitle=f"{o['time']} · {rp.length_text(o['minutes'])} · every day")
                add = Gtk.Button(label="Add", valign=Gtk.Align.CENTER)
                add.add_css_class("suggested-action")
                add.connect("clicked", lambda _b, o=o: self._do(
                    lambda: rp.save_anchor(self._tools(), o["title"], o["time"], o["minutes"], False)))
                r.add_suffix(add)
                group.add(r)
                offers_at.append(r)
                self.offer_rows.append(r)
        offer.connect("clicked", show_offers)

        for a in rp.anchor_rows(tools):
            group.add(self._anchor_row(a))
        group.add(self._new_anchor_row())
        return group

    def _anchor_fields(self, exp: Adw.ExpanderRow, a: Dict[str, Any]) -> Dict[str, Any]:
        time = Adw.EntryRow(title="Usual time", text=a.get("time", ""), show_apply_button=True)
        minutes = Adw.SpinRow.new_with_range(5, 240, 5)
        minutes.set_title("Minutes")
        minutes.set_value(a.get("minutes", 30))
        fixed = Adw.SwitchRow(title="Fixed", subtitle="Keeps its exact time and never slides",
                              active=bool(a.get("fixed")))
        days = Adw.ActionRow(title="Days", subtitle="None picked: every day")
        box = Gtk.Box(valign=Gtk.Align.CENTER)
        box.add_css_class("linked")
        buttons = {}
        for d in DAYS:
            b = Gtk.ToggleButton(label=DAY_NAMES[d], active=d in (a.get("days") or []))
            box.append(b)
            buttons[d] = b
        days.add_suffix(box)
        for r in (time, minutes, fixed, days):
            exp.add_row(r)
        return {"time": time, "minutes": minutes, "fixed": fixed, "days": buttons}

    @staticmethod
    def _read(fields: Dict[str, Any]) -> Dict[str, Any]:
        return {"time": fields["time"].get_text().strip(), "minutes": int(fields["minutes"].get_value()),
                "fixed": fields["fixed"].get_active(),
                "days": [d for d, b in fields["days"].items() if b.get_active()]}

    def _anchor_row(self, a: Dict[str, Any]) -> Adw.ExpanderRow:
        exp = Adw.ExpanderRow(title=a["title"], subtitle=a["summary"])
        fields = self._anchor_fields(exp, a)
        title = a["title"]

        def save(*_x) -> None:
            v = self._read(fields)
            if (v["time"], v["minutes"], v["fixed"], v["days"]) == (a["time"], a["minutes"], a["fixed"], a["days"]):
                return
            self._do(lambda: rp.save_anchor(self._tools(), title, v["time"], v["minutes"], v["fixed"], v["days"]))
        fields["time"].connect("apply", save)
        fields["minutes"].connect("notify::value", save)
        fields["fixed"].connect("notify::active", save)
        for b in fields["days"].values():
            b.connect("toggled", save)
        remove = Adw.ButtonRow(title="Remove", start_icon_name="user-trash-symbolic")
        remove.add_css_class("destructive-action")
        remove.connect("activated", lambda _b: self._do(lambda: rp.remove_anchor(self._tools(), title)))
        exp.add_row(remove)
        exp.fields, exp.remove_row = fields, remove
        self.anchor_rows[title] = exp
        return exp

    def _new_anchor_row(self) -> Adw.ExpanderRow:
        exp = Adw.ExpanderRow(title="Add an anchor", subtitle="Something that happens at a set time each day")
        name = Adw.EntryRow(title="What it is (e.g. Cats' dinner)")
        exp.add_row(name)
        fields = self._anchor_fields(exp, {"time": "", "minutes": 30})
        add = Adw.ButtonRow(title="Add", start_icon_name="list-add-symbolic")
        add.add_css_class("suggested-action")

        def on_add(_b) -> None:
            v = self._read(fields)
            if not name.get_text().strip() or not v["time"]:
                self._say("Give it a name and a usual time.")
                return
            self._do(lambda: rp.save_anchor(self._tools(), name.get_text().strip(), v["time"], v["minutes"],
                                            v["fixed"], v["days"]))
        add.connect("activated", on_add)
        exp.add_row(add)
        exp.name_row, exp.fields, exp.add_row_button = name, fields, add
        self.new_anchor = exp
        return exp
