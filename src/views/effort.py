"""The user's own effort estimate for an item on their lists: "Takes about [3] [hours]", in the unit they pick
(blocks, hours, minutes, days or points). Kept for the item through set_estimate; the unit picked becomes the one
they usually estimate in (set_effort_unit). The data is efforts.py; the calls are day_actions.py's."""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from thirties_core.day_actions import ESTIMATE_UNITS

UNIT_NAMES = {"thirties": "blocks", "hours": "hours", "minutes": "minutes", "days": "days", "points": "points"}
STEPS = {"thirties": (1, 0), "hours": (0.5, 1), "minutes": (5, 0), "days": (0.5, 1), "points": (1, 0)}


class EffortEditor(Gtk.Box):
    """``amount``, ``unit``: the estimate kept now (amount 0: none yet, ``unit`` the usual one). ``on_save(amount,
    unit)`` keeps it."""

    def __init__(self, amount: float, unit: str, on_save: Callable[[float, str], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.on_save = on_save
        unit = unit if unit in ESTIMATE_UNITS else "hours"
        head = Gtk.Label(label="Your estimate", xalign=0)
        head.add_css_class("caption-heading")
        self.append(head)
        line = Gtk.Box(spacing=6)
        line.append(Gtk.Label(label="Takes about", xalign=0))
        self.amount = Gtk.SpinButton.new_with_range(0.5, 9999, 1)
        self.amount.set_valign(Gtk.Align.CENTER)
        self.amount.update_property([Gtk.AccessibleProperty.LABEL], ["How much"])
        line.append(self.amount)
        self.unit = Gtk.DropDown.new_from_strings([UNIT_NAMES[u] for u in ESTIMATE_UNITS])
        self.unit.set_valign(Gtk.Align.CENTER)
        self.unit.update_property([Gtk.AccessibleProperty.LABEL], ["In"])
        line.append(self.unit)
        self.append(line)
        self.save = Gtk.Button(label="Save Estimate", halign=Gtk.Align.START)
        self.save.connect("clicked", self._saved)
        self.append(self.save)
        self.unit.set_selected(ESTIMATE_UNITS.index(unit))
        self._steps(unit)
        self.amount.set_value(amount if amount else {"thirties": 2, "minutes": 30}.get(unit, 1))
        self._changed()
        self.unit.connect("notify::selected", lambda *_: (self._steps(self.chosen_unit()), self._changed()))
        self.amount.connect("value-changed", lambda *_: self._changed())

    def chosen_unit(self) -> str:
        return ESTIMATE_UNITS[self.unit.get_selected()]

    def _steps(self, unit: str) -> None:
        step, digits = STEPS[unit]
        self.amount.set_digits(digits)
        self.amount.set_increments(step, step * 4)
        self.amount.set_range(step, 9999)

    def _saved(self, _button) -> None:
        self.on_save(self.amount.get_value(), self.chosen_unit())
        self.save.set_label("Saved")
        self.save.set_sensitive(False)

    def _changed(self) -> None:
        self.save.set_label("Save Estimate")
        self.save.set_sensitive(True)


__all__ = ["EffortEditor", "UNIT_NAMES"]
