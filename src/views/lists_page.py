"""Profile → Lists: what each of your notes is for, and how pressing each item is.

Every note Thirties reads, grouped by notebook. Each note has a role (routine,
today, menu, list, project, reference, or ignore); in lists, projects and menus,
each item has an urgency (now, soon, whenever, someday) or a date. The model's
guesses are starting values, marked "guessed"; whatever you choose here sticks.
Nothing is written into the notes.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from gi.repository import Adw, GLib, Gtk

from thirties_core.lists import ROLE_URGENCY, ROLES, SORTED_ROLES, URGENCIES, ListCatalog
from thirties_core.models import TaskItem

ROLE_NAMES = {"routine": "Routine", "today": "Today", "menu": "Menu", "list": "List", "project": "Project",
              "reference": "Reference", "ignore": "Ignore"}
URGENCY_NAMES = {"now": "Now", "soon": "Soon", "whenever": "Whenever", "someday": "Someday"}

Loader = Callable[[], Tuple[List[Dict[str, Any]], List[TaskItem]]]


class ListsPage(Gtk.Stack):
    """Built when first shown (reading the notes takes a moment), and again on reload()."""

    def __init__(self, catalog: Optional[ListCatalog], load: Optional[Loader],
                 on_changed: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        self.catalog, self.load, self.on_changed = catalog, load, on_changed
        self.note_rows: Dict[str, Adw.ExpanderRow] = {}
        self.item_buttons: Dict[str, Dict[str, Gtk.ToggleButton]] = {}
        self.notebook_switches: Dict[str, Gtk.Switch] = {}
        empty = Adw.StatusPage(icon_name="view-list-symbolic", title="No Lists Yet",
                               description="Connect Joplin in Preferences → Tasks, and your notes show up here.")
        empty.add_css_class("compact")
        self.add_named(empty, "empty")
        self.page = Adw.PreferencesPage()
        self.add_named(self.page, "lists")
        self._built = False
        self.connect("map", lambda *_: self.reload() if not self._built else None)

    def _changed(self) -> None:
        if self.on_changed:
            self.on_changed()

    def reload(self) -> None:
        self._built = True
        for group in getattr(self, "_groups", []):
            self.page.remove(group)
        self._groups, self.note_rows, self.item_buttons, self.notebook_switches = [], {}, {}, {}
        notes, tasks = self.load() if self.load and self.catalog else ([], [])
        if not notes:
            self.set_visible_child_name("empty")
            return
        intro = Adw.PreferencesGroup(description=(
            "What each note is for, and how pressing each item is. Thirties guesses (marked “guessed”); what you "
            "choose here sticks. Nothing is written into your notes."))
        self.page.add(intro)
        self._groups.append(intro)
        roles, urgencies = self.catalog.roles(), self.catalog.urgencies()
        by_note: Dict[str, List[TaskItem]] = {}
        for t in tasks:
            if t.is_checkbox_item and t.parent_note_id:
                by_note.setdefault(t.parent_note_id, []).append(t)
        by_notebook: Dict[str, List[Dict[str, Any]]] = {}
        for n in notes:
            by_notebook.setdefault(n.get("source_notebook") or "Notes", []).append(n)
        for notebook in sorted(by_notebook):
            group = Adw.PreferencesGroup(title=notebook)
            reading = any((roles.get(n["id"]) or {}).get("role") != "ignore" for n in by_notebook[notebook])
            switch = Gtk.Switch(active=reading, valign=Gtk.Align.CENTER,
                                tooltip_text="Read this notebook (off: every note in it is ignored)")
            switch.connect("notify::active", self._on_notebook, by_notebook[notebook])
            group.set_header_suffix(switch)
            self.notebook_switches[notebook] = switch
            for note in by_notebook[notebook]:
                group.add(self._note_row(note, roles.get(note["id"]), by_note.get(note["id"], []), urgencies))
            self.page.add(group)
            self._groups.append(group)
        self.set_visible_child_name("lists")

    def _on_notebook(self, switch: Gtk.Switch, _pspec, notes: List[Dict[str, Any]]) -> None:
        """Off: every note in the notebook is ignored. On: they're sorted again (guessed next time round)."""
        for note in notes:
            if switch.get_active():
                if self.catalog.role(note["id"]) == "ignore":
                    self.catalog.clear_role(note["id"])
            else:
                self.catalog.set_role(note["id"], "ignore", note["title"], source="user")
        GLib.idle_add(lambda: self.reload() and False)
        self._changed()

    # --- a note ---

    def _note_row(self, note, role_row, items: List[TaskItem], urgencies) -> Adw.ExpanderRow:
        role = role_row["role"] if role_row else None
        row = Adw.ExpanderRow(title=note["title"])
        row.note_id = note["id"]
        self._describe(row, role_row, len(items))
        names = list(ROLES)
        choice = Adw.ComboRow(title="What it's for", model=Gtk.StringList.new([ROLE_NAMES[r] for r in names]))
        choice.set_subtitle(ROLES[role] if role else "Not sorted yet")
        if role:
            choice.set_selected(names.index(role))
        choice.connect("notify::selected", self._on_role, row, names, note)
        row.add_row(choice)
        row.items, row.urgencies, row.item_rows = items, urgencies, []
        row.connect("notify::expanded", lambda r, _p: self._fill_items(r) if r.get_expanded() else None)
        self.note_rows[note["id"]] = row
        return row

    def _describe(self, row: Adw.ExpanderRow, role_row, count: int) -> None:
        if not role_row:
            row.set_subtitle("Not sorted yet")
            return
        guessed = " · guessed" if role_row["source"] == "guess" else ""
        row.set_subtitle(f"{ROLE_NAMES[role_row['role']]}{guessed}" + (f" · {count} items" if count else ""))

    def _on_role(self, combo: Adw.ComboRow, _pspec, row: Adw.ExpanderRow, names: List[str], note) -> None:
        role = names[combo.get_selected()]
        if role == self.catalog.role(note["id"]) and self.catalog.roles()[note["id"]]["source"] == "user":
            return
        self.catalog.set_role(note["id"], role, note["title"], source="user")
        combo.set_subtitle(ROLES[role])
        self._describe(row, self.catalog.roles()[note["id"]], len(row.items))
        self._fill_items(row, force=True)
        self._changed()

    # --- its items ---

    def _fill_items(self, row: Adw.ExpanderRow, force: bool = False) -> None:
        if row.item_rows and not force:
            return
        for r in row.item_rows:
            row.remove(r)
        row.item_rows = []
        role = self.catalog.role(row.note_id)
        if role not in SORTED_ROLES:
            if row.items and role in ROLE_URGENCY:
                hint = Adw.ActionRow(title=f"Items take their urgency from the note: {ROLE_URGENCY[role]}")
                hint.add_css_class("dim-label")
                row.add_row(hint)
                row.item_rows.append(hint)
            return
        depth: Dict[str, int] = {}
        for t in row.items:
            depth[t.id] = depth.get(t.parent_task_id, -1) + 1 if t.parent_task_id else 0
            r = self._item_row(t, row.urgencies.get(t.id), depth[t.id])
            row.add_row(r)
            row.item_rows.append(r)

    def _item_row(self, task: TaskItem, current, depth: int) -> Adw.ActionRow:
        r = Adw.ActionRow(title=("    " * depth) + task.title)
        r.set_title_lines(2)
        if current and current["source"] == "guess":
            r.set_subtitle("guessed")
        box = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)
        box.add_css_class("linked")
        buttons: Dict[str, Gtk.ToggleButton] = {}
        first = None
        for name in URGENCIES:
            b = Gtk.ToggleButton(label=URGENCY_NAMES[name])
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.set_active(bool(current) and current["urgency"] == name and not current.get("due"))
            b.connect("toggled", self._on_urgency, task, name, r)
            box.append(b)
            buttons[name] = b
        r.add_suffix(box)
        pick = Gtk.MenuButton(icon_name="x-office-calendar-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Due by a date")
        if current and current.get("due"):
            pick.set_label(date.fromisoformat(current["due"]).strftime("%b %-d"))
            pick.add_css_class("accent")
        cal = Gtk.Calendar()
        popover = Gtk.Popover(child=cal)
        cal.connect("day-selected", self._on_due, task, pick, popover, r)
        pick.set_popover(popover)
        r.add_suffix(pick)
        self.item_buttons[task.id] = buttons
        r.due_button = pick
        return r

    def _on_urgency(self, button: Gtk.ToggleButton, task: TaskItem, name: str, r: Adw.ActionRow) -> None:
        if not button.get_active():
            return
        self.catalog.set_urgency(task.id, name, None, note_id=task.parent_note_id or "", title=task.title, source="user")
        r.set_subtitle("")
        r.due_button.set_label("")
        r.due_button.set_icon_name("x-office-calendar-symbolic")
        r.due_button.remove_css_class("accent")
        self._changed()

    def _on_due(self, cal: Gtk.Calendar, task: TaskItem, pick: Gtk.MenuButton, popover: Gtk.Popover,
                r: Adw.ActionRow) -> None:
        d = cal.get_date()
        due = date(d.get_year(), d.get_month(), d.get_day_of_month())
        days = (due - date.today()).days
        self.catalog.set_urgency(task.id, "now" if days <= 1 else "soon", due, note_id=task.parent_note_id or "",
                                 title=task.title, source="user")
        for b in self.item_buttons[task.id].values():
            b.set_active(False)
        pick.set_label(due.strftime("%b %-d"))
        pick.add_css_class("accent")
        r.set_subtitle("")
        popover.popdown()
        self._changed()
