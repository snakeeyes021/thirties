"""The Timeline page: the day as a stack of thirties, the week as seven shrunken days, the month as a calendar and the
year as twelve months.

Replaces the old timeline (block cards, the solar arc: confusing, not GNOME HIG, 10-30). The design
came from four mockup rounds with the user, then a refinement of the Day tab:

- **Day**: one rounded slot per thirty, top = waking up, bottom = bedtime, with no clock times anywhere (tried
  without); an item's real times are in its tooltip and its popover. The left rail is the sky, to the minute (goldenrod
  daylight, indigo dark), with the now badge on it (the sun or the moon) and a short Breguet hour hand pointing into
  the day. Work is one hatched card (a monolith) under what's placed during it. Items are cards on their slots, side
  by side when they share time, each with a round tick, a column of pips at its right edge (one per thirty, centred
  in its thirty) and notches in both edges where its thirties roll over. Free time (nothing planned, not Work, not
  Sleep, from now on) is a dashed green outline with green pips and no words; clicking it offers "Plan something
  here". Cards can be dragged to another thirty (Shift pushes what follows), resized from the top or bottom edge,
  and removed (Delete, or Remove in its popover) with Undo; calendar events are fixed.
- **Week**: "This week, no day yet" chips (items pinned to the week but not to a day) to drag onto a day, and seven
  columns on one time-of-day axis with each day's free time in the chosen units; a bar with room shows its item's
  name.
- **Month**: the month plan's efforts with their budgets, then a calendar grid, each day with its items' short
  titles (done ones ticked) and what's pinned to it; a day opens on the Day tab. Narrow, a list of the days.
- **Year**: the year plan's efforts (as a whole, by quarter), then twelve months with their efforts and budgets; a
  month opens on the Month tab.

The switch between them sits centred under the Timeline tab, over what it changes.

All the arithmetic is in thirties_core/timeline_layout.py and period_summary.py (tested without a display);
this file only draws it.
"""

from __future__ import annotations

import math
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo

from thirties_core.astronomy import get_current_time
from thirties_core.clock import format_clock
from thirties_core.plan_tools import NOT_DONE
from thirties_core.timeline_layout import (DEFAULT_UNITS, Card, DayLayout, FreeStretch, WeekLayout, amount_text,
                                           day_layout, delete_call, free_text, mark_label_top, movable, move_call,
                                           move_target, night_marks, pencil_marks, normalise_units, record_call, resize_call, resize_extent,
                                           restore_call, sun_marks, week_days, week_layout)

Tool = Callable[[str, dict], str]

# The two sky colours (the only fixed colours besides the hand's moon ring): agreed in the mockups.
GOLDENROD = (0xd4 / 255, 0xa0 / 255, 0x17 / 255)
INDIGO = (0x3b / 255, 0x4a / 255, 0x8c / 255)
PALE_INDIGO = (0x8f / 255, 0x9b / 255, 0xd6 / 255)

# Geometry of the Day tab, in px.
PITCH = 44        # one thirty
GAP = 4           # between two slots
TOP = 10          # above the first slot
RAIL = 66         # the left rail: the sky bar at SKY_X, the badge on it, the hand to its right
SKY_X, SKY_W = 10, 8
BADGE = 28
LANE_GAP = 4
PAD_RIGHT = 8
PIP = 8
PIP_INSET = 11    # pips are right-justified: their centres this far from a card's right edge
NOTCH = 10        # the dimples at a thirty boundary inside a card
GRIP = 7          # a card's top and bottom edges, for resizing
WRAP_LANE = 170   # a card narrower than this wraps its title at word boundaries (two lines) instead of cutting it
BARE_LANE = 120   # narrower still, and its tick moves into the popover a tap opens, so the title keeps the room
NIGHT_H = 30      # the folded night: a thin strip under the waking day (and, opened, the strip that folds it)

# What a press on each kind of empty stretch offers
STRETCH_TIPS = {"free": "Plan something here", "night": "Plan something here, at night",
                "past": "Add what happened here"}

# Item colours by kind (10-30): never to be confused with the sky's goldenrod (daylight) or indigo (dark),
# so no yellow, orange or deep blue-violet. The same in light and dark, and the cards' CSS (style.css, .tl-card)
# uses the same four. (light, dark), drawn, so CSS variables can't reach them.
LOOK_RGB = {
    "once": ((0x4a, 0x90, 0xd9), (0x4a, 0x90, 0xd9)),       # one-off: blue
    "daily": ((0x3f, 0xb3, 0x7f), (0x3f, 0xb3, 0x7f)),      # daily: green
    "ongoing": ((0x9b, 0x6b, 0xd3), (0x9b, 0x6b, 0xd3)),    # ongoing (project time): purple
    "calendar": ((0xe0, 0x6c, 0x75), (0xe0, 0x6c, 0x75)),   # calendar: rose
}
KIND_NAMES = (("once", "one-off"), ("daily", "daily"), ("ongoing", "project time"), ("calendar", "calendar"))


def _dark() -> bool:
    return Adw.StyleManager.get_default().get_dark()


def _rgb(look: str):
    light, dark = LOOK_RGB.get(look, LOOK_RGB["once"])
    return tuple(c / 255 for c in (dark if _dark() else light))


def _lookup(widget: Gtk.Widget, name: str, fallback: tuple) -> tuple:
    """A named colour of the theme (window_bg_color, success_color...), as (r, g, b, a)."""
    try:
        ok, rgba = widget.get_style_context().lookup_color(name)
        if ok:
            return rgba.red, rgba.green, rgba.blue, rgba.alpha
    except Exception:
        pass
    return fallback


def _fg(widget: Gtk.Widget) -> tuple:
    c = widget.get_color()
    return c.red, c.green, c.blue


def _rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _svg_path(cr, d: str) -> None:
    """The few SVG path commands the hand uses: M, H, C, Z (absolute)."""
    import re
    tokens = re.findall(r"[MHCZ]|-?\d+(?:\.\d+)?", d)
    i, x, y = 0, 0.0, 0.0
    cmd = ""
    while i < len(tokens):
        if tokens[i] in "MHCZ":
            cmd = tokens[i]
            i += 1
            if cmd == "Z":
                cr.close_path()
            continue
        if cmd == "M":
            x, y = float(tokens[i]), float(tokens[i + 1])
            cr.move_to(x, y)
            i += 2
        elif cmd == "H":
            x = float(tokens[i])
            cr.line_to(x, y)
            i += 1
        elif cmd == "C":
            pts = [float(t) for t in tokens[i:i + 6]]
            cr.curve_to(*pts)
            x, y = pts[4], pts[5]
            i += 6
        else:
            i += 1


# The Breguet hour hand the user approved (10-30, last refinement: no ring, using its height): viewBox 0 0 36 32,
# drawn at 36x32, so it fits between the badge and the item column.
HAND_W, HAND_H = 36, 32
HAND_STEM = "M1 16 H18"
HAND_SCROLLS = ("M4 16 C4 7 13 5.5 14 10.5 C14.6 13.4 11.6 14.2 10.8 12.4",
                "M4 16 C4 25 13 26.5 14 21.5 C14.6 18.6 11.6 17.8 10.8 19.6")
HAND_SPADE = "M16 16 C19 8 26 6.5 29 10 C26 11.5 25 13.5 35.5 16 C25 18.5 26 20.5 29 22 C26 25.5 19 24 16 16 Z"


HAND_OUTLINE = 2.6  # px of extra stroke, in a darker shade of the hand's own colour, on every part
# (10-30: a background-coloured outline cut a notch where the hand met today's frame; a dark-gold edge doesn't)
# (fill, edge) per sky and theme: the sun's hand gold with a dark-gold edge, the moon's pale indigo with a deep one.
HAND_COLOURS = {
    ("sun", "dark"): ((0xe0 / 255, 0xaa / 255, 0x1c / 255), (0x7a / 255, 0x5a / 255, 0x06 / 255)),
    ("sun", "light"): ((0xe8 / 255, 0xb4 / 255, 0x25 / 255), (0xa7 / 255, 0x80 / 255, 0x0f / 255)),
    ("moon", "dark"): ((0x8f / 255, 0x9b / 255, 0xd6 / 255), (0x2e / 255, 0x38 / 255, 0x6e / 255)),
    ("moon", "light"): ((0x8f / 255, 0x9b / 255, 0xd6 / 255), (0x5a / 255, 0x66 / 255, 0xa8 / 255)),
}


def hand_colours(daylight: bool) -> tuple:
    return HAND_COLOURS[("sun" if daylight else "moon", "dark" if _dark() else "light")]


def draw_hand(cr, x: float, y: float, rgb: tuple, scale: float = 1.0, mirrored: bool = False,
              outline: Optional[tuple] = None) -> None:
    """The hour hand with its tail at x and its axis at y, pointing right (left when ``mirrored``: then x is its
    right end). ``scale`` resizes it (the Week tab's pair). With ``outline`` (the panel's colour), the hand is drawn
    first in that colour HAND_OUTLINE px wider on every part (stem, scrolls, the spade's edge), then on top."""
    cr.save()
    cr.translate(x, y)
    cr.scale(-scale if mirrored else scale, scale)
    cr.translate(0, -HAND_H / 2)
    cr.set_line_cap(1)  # round
    cr.set_line_join(1)
    extra = HAND_OUTLINE / scale  # in px on screen, whatever the scale
    for colour, grow in (((outline, extra),) if outline is not None else ()) + ((rgb, 0.0),):
        cr.set_source_rgba(*colour) if len(colour) == 4 else cr.set_source_rgb(*colour)
        cr.set_line_width(2.4 + grow)
        _svg_path(cr, HAND_STEM)
        cr.stroke()
        cr.set_line_width(1.8 + grow)
        for d in HAND_SCROLLS:
            _svg_path(cr, d)
            cr.stroke()
        _svg_path(cr, HAND_SPADE)
        if grow:
            cr.set_line_width(grow)
            cr.fill_preserve()
            cr.stroke()
        else:
            cr.fill()
    cr.restore()


MARK_X = SKY_X + SKY_W + 5  # a time mark's words, right of the sky bar


HAND_CLEAR = HAND_H / 2 + 2  # a mark's words stay this far from the hand's axis


def draw_sun_mark(cr, y: float, name: str, t, fg: tuple, hand_y: Optional[float] = None) -> None:
    """A time mark on the rail: a notch across the sky bar, and its name (a word a line) over its time beside it,
    never wider than the rail: at a larger font it's drawn smaller rather than running into the cards (10-31). With
    the hour hand at ``hand_y``, the words move just clear of it rather than under it; the notch stays."""
    cr.set_source_rgba(*fg, 0.85)
    cr.rectangle(SKY_X - 3, y - 1, SKY_W + 6, 2)
    cr.fill()
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string("Sans 7"))
    layout.set_text("\n".join(name.split() + [format_clock(t)]), -1)
    w, h = layout.get_pixel_size()
    room = RAIL - MARK_X - LANE_GAP
    scale = min(1.0, room / w) if w else 1.0
    cr.save()
    cr.set_source_rgba(*fg, 0.6)
    cr.translate(MARK_X, mark_label_top(y, h * scale, hand_y, HAND_CLEAR))
    cr.scale(scale, scale)
    cr.move_to(0, 0)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def draw_badge(cr, cx: float, cy: float, daylight: bool, bg: tuple) -> None:
    """The now badge on the sky bar: the sun in daylight, the moon after dark, ringed in goldenrod or pale indigo."""
    ring = GOLDENROD if daylight else PALE_INDIGO
    r = BADGE / 2
    cr.set_source_rgba(*bg)
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.fill()
    cr.set_source_rgb(*ring)
    cr.set_line_width(2)
    cr.arc(cx, cy, r - 1, 0, 2 * math.pi)
    cr.stroke()
    if daylight:
        cr.arc(cx, cy, 4.2, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(1.6)
        cr.set_line_cap(1)
        for i in range(8):
            a = i * math.pi / 4
            cr.move_to(cx + math.cos(a) * 6.6, cy + math.sin(a) * 6.6)
            cr.line_to(cx + math.cos(a) * 9.0, cy + math.sin(a) * 9.0)
        cr.stroke()
    else:  # a crescent: the moon's disc less a shifted one
        cr.new_path()
        cr.arc(cx, cy, 7, 0, 2 * math.pi)
        cr.new_sub_path()
        cr.arc_negative(cx + 3.6, cy - 2.4, 6, 2 * math.pi, 0)
        cr.set_fill_rule(0)  # winding: the reversed circle cuts
        cr.fill()


def draw_pips(cr, x: float, y_of: Callable[[float], float], row: int, rows: int, rgba: tuple) -> None:
    """One pip per thirty, right-justified (centred PIP_INSET from the right edge ``x``), each centred in its thirty."""
    cr.set_source_rgba(*rgba)
    for i in range(rows):
        cy = y_of(row + i) + PITCH / 2
        _rounded(cr, x - PIP_INSET - PIP / 2, cy - PIP / 2, PIP, PIP, 2)
        cr.fill()


def draw_notches(cr, x: float, w: float, y_of: Callable[[float], float], row: int, rows: int, bg: tuple,
                 outline: Optional[tuple] = None, size: float = NOTCH) -> None:
    """Dimples on both edges at each thirty boundary inside a card, so its thirties can be counted. Drawn on top, so
    they cut the accent strip too. A free stretch's notches carry its dotted outline round them."""
    for i in range(1, rows):
        cy = y_of(row + i)
        for cx, a0, a1 in ((x, -math.pi / 2, math.pi / 2), (x + w, math.pi / 2, 3 * math.pi / 2)):
            cr.new_path()
            cr.arc(cx, cy, size / 2, a0, a1)
            cr.close_path()
            cr.set_source_rgba(*bg)
            cr.fill()
            if outline is not None:
                cr.new_path()
                cr.arc(cx, cy, size / 2, a0, a1)
                cr.set_source_rgba(*outline)
                cr.set_line_width(1.5)
                cr.set_dash([1.2, 1.6])
                cr.stroke()
                cr.set_dash([])


# --- the Day tab -------------------------------------------------------------------------------------------------

PENCIL_WORD = "pencilled in"


def draw_pencil(cr, widget, mark, x: float, y: float, w: float, h: float, fg: tuple) -> None:
    """Something pencilled in for a later day: a faint dashed outline and its title in light italics, then
    "pencilled in"; no tick, no fill of its kind's colour, nothing to drag."""
    _rounded(cr, x, y, w, h, 7)
    cr.set_source_rgba(*fg, 0.03)
    cr.fill_preserve()
    cr.set_source_rgba(*fg, 0.38)
    cr.set_line_width(1.2)
    cr.set_dash([4, 3])
    cr.stroke()
    cr.set_dash([])
    from thirties_core.titles import title_of
    layout = widget.create_pango_layout("")
    layout.set_markup(f"<i>{GLib.markup_escape_text(title_of(mark.title))}</i>"
                      f"  <span size='small' alpha='75%'>{PENCIL_WORD}</span>", -1)
    layout.set_width(int(max(10, w - 20) * Pango.SCALE))
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    _tw, th = layout.get_pixel_size()
    cr.save()
    cr.rectangle(x, y, w, h)
    cr.clip()
    cr.move_to(x + 12, y + min(h / 2 - th / 2, 10))
    cr.set_source_rgba(*fg, 0.55)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


class CardWidget(Gtk.Box):
    """An item on its slots: tick and title (and a quiet line when there's room); the canvas draws its pips and
    notches, and handles dragging and resizing it."""

    def __init__(self, card: Card, on_tick: Callable[[Card], None], on_open: Callable[["CardWidget"], None],
                 on_delete: Callable[[Card], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, focusable=True)
        self.card = card
        span = card.span
        self.add_css_class("tl-card")
        self.add_css_class(card.look)
        self.add_css_class(card.state)
        if not movable(span):
            self.add_css_class("fixed")
        if card.current:
            self.add_css_class("current")
        multi = card.rows > 1

        self.tick = Gtk.Button(valign=Gtk.Align.START if multi else Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                               margin_top=11 if multi else 0)
        self.tick.add_css_class("tl-tick")
        icon = {"done": "object-select-symbolic", "partial": "object-select-symbolic",
                "skipped": "window-close-symbolic"}.get(card.state)
        if icon:
            self.tick.set_child(Gtk.Image(icon_name=icon, pixel_size=12))
            self.tick.add_css_class("checked" if card.state != "skipped" else "crossed")
        self.tick.set_sensitive(card.tickable)
        if card.tickable:
            self.tick.set_cursor_from_name("pointer")  # a pointing hand over the circle
        self.tick.set_tooltip_text(f"Mark {span.title} done" if card.tickable else (card.status or None))
        self.tick.update_property([Gtk.AccessibleProperty.LABEL], [f"Done: {span.title}"])
        self.tick.connect("clicked", lambda _b: on_tick(card))
        self.append(self.tick)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True,
                       valign=Gtk.Align.START if multi else Gtk.Align.CENTER, margin_top=10 if multi else 0)
        self.title_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.title_label.add_css_class("tl-title")
        from thirties_core.titles import title_of
        title = GLib.markup_escape_text(title_of(span.title))  # the short title (11-06: no raw lines on cards)
        if card.state == "done" and card.status != "Worked on":
            title = f"<s>{title}</s>"  # done and finished; a session on something ongoing isn't struck through
        elif card.state == "moot":
            title = f"<i>{title}</i>"
        if not multi and card.status:
            title += f"  <span size='small' alpha='70%'>{GLib.markup_escape_text(card.status)}</span>"
        self.title_label.set_markup(title)
        text.append(self.title_label)
        quiet = [card.status] if card.status else []
        quiet += card.notes
        from thirties_core.others_events import marker  # shared with someone else (others_events.py)
        shared = marker(span) if span.shared else ""
        if shared:
            quiet.append(shared)
            if not multi:
                title += f"  <span size='small' alpha='70%'>{GLib.markup_escape_text(shared)}</span>"
                self.title_label.set_markup(title)
        if multi and quiet:
            self.caption = Gtk.Label(label=" · ".join(quiet), xalign=0, ellipsize=Pango.EllipsizeMode.END)
            self.caption.add_css_class("caption")
            self.caption.add_css_class("dim-label")
            text.append(self.caption)
        self.append(text)
        self.append(Gtk.Box(width_request=PIP_INSET + PIP))  # room for the pip column the canvas draws
        self.fit = "roomy"

        from thirties_core.titles import title_of
        tip = [title_of(span.title), card.times]
        if card.status:
            tip.append(card.status)
        tip += card.notes
        if shared:
            tip.append(shared)
        if span.category.value == "calendar":
            tip.append("From your calendar: fixed")
        self.set_tooltip_text("\n".join(tip))
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{span.title}, {card.times}"])

        # a tap (or right-click) selects it and shows its real times; Delete removes it
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._pressed)
        click.connect("released", self._released, on_open)
        self.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, _code, _state: (
            on_delete(card) or True) if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace) else False)
        self.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._hover)
        self.add_controller(motion)
        self._press = (0.0, 0.0)

    def set_fit(self, fit: str) -> None:
        """How much room the card has: roomy (one line, ellipsized past its end), tight (the title wraps at word
        boundaries onto a second line) or bare (tight, and the tick lives in the card's popover instead)."""
        if fit == self.fit:
            return
        self.fit = fit
        tight = fit != "roomy"
        self.title_label.set_wrap(tight)
        self.title_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title_label.set_lines(2 if tight else -1)
        self.set_spacing(5 if tight else 8)
        self.tick.set_visible(fit != "bare")
        (self.add_css_class if tight else self.remove_css_class)("tight")

    def _pressed(self, _gesture, _n, x, y) -> None:
        self._press = (x, y)

    def _released(self, gesture, _n, x, y, on_open) -> None:
        if abs(x - self._press[0]) > 4 or abs(y - self._press[1]) > 4:
            return  # that was a drag
        self.grab_focus()
        on_open(self)

    def _hover(self, _motion, _x, y) -> None:
        if not movable(self.card.span):
            self.set_cursor_from_name(None)
            return
        edge = y <= GRIP or y >= self.get_height() - GRIP
        self.set_cursor_from_name("ns-resize" if edge else "grab")


class DayCanvas(Gtk.Widget):
    """The waking day: slots, the sky rail, the now badge and hand, Work, cards, free stretches. Children are real
    widgets (ticks and free stretches are buttons); the rest is drawn. Dragging a card moves it, dragging its top or
    bottom edge resizes it (both snap to thirties); each becomes one tool call (timeline_layout.move_call, ...)."""

    __gtype_name__ = "ThirtiesTimelineDay"

    def __init__(self) -> None:
        super().__init__()
        self.layout: Optional[DayLayout] = None
        self.cards: List[CardWidget] = []
        self.work: List[tuple] = []         # (label, Card)
        self.frees: List[tuple] = []        # (button, FreeStretch): every empty stretch, free, night or past
        self.notes: List[tuple] = []        # (label, SlotNote)
        self.night_btn: Optional[Gtk.Button] = None  # the night strip: folded, it opens; opened, it folds
        self.on_night: Callable[[bool], None] = lambda open_: None
        self.on_tick: Callable[[Card], None] = lambda card: None
        self.on_free: Callable[[Gtk.Widget, FreeStretch], None] = lambda w, f: None
        self.on_change: Callable[[str, dict], None] = lambda name, args: None
        self.on_delete: Callable[[Card], None] = lambda card: None
        # card -> a widget for its popover (the user's effort estimate), or None
        self.extra_for: Callable[[Card], Optional[Gtk.Widget]] = lambda card: None
        self.ghost: Optional[tuple] = None  # (x, y, w, h) while dragging
        self._drag: Optional[dict] = None
        self.set_hexpand(True)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

    # --- content ---

    def _children(self) -> List[Gtk.Widget]:
        return [b for b, _ in self.frees] + [lbl for lbl, _ in self.work] + list(self.cards) + \
            [lbl for lbl, _ in self.notes] + ([self.night_btn] if self.night_btn is not None else [])

    def _clear(self) -> None:
        for w in self._children():
            w.unparent()
        self.cards, self.work, self.frees, self.notes, self.night_btn = [], [], [], [], None

    def do_dispose(self) -> None:
        self._clear()

    def set_layout(self, layout: DayLayout) -> None:
        self._clear()
        self.layout = layout
        for f in layout.stretches:
            b = Gtk.Button()
            b.add_css_class("tl-past" if f.kind == "past" else "tl-free")
            b.set_tooltip_text(STRETCH_TIPS[f.kind])
            b.update_property([Gtk.AccessibleProperty.LABEL], [STRETCH_TIPS[f.kind]])
            b.connect("clicked", lambda btn, f=f: self.on_free(btn, f))
            b.set_parent(self)
            self.frees.append((b, f))
        if layout.night is not None:
            n = layout.night
            label = (f"Fold the night ({format_clock(n.start)} – {format_clock(n.end)})" if n.open
                     else f"Night, {format_clock(n.start)} – {format_clock(n.end)}: open to plan in it")
            self.night_btn = Gtk.Button(label=label, tooltip_text=label)
            self.night_btn.add_css_class("tl-night-strip")
            self.night_btn.connect("clicked", lambda _b: self.on_night(not self.layout.night.open))
            self.night_btn.set_parent(self)
        for w in layout.work:
            from thirties_core.titles import title_of
            lbl = Gtk.Label(label=title_of(w.span.title), xalign=0, valign=Gtk.Align.START, can_target=True,
                            tooltip_text=f"{title_of(w.span.title)}\n{w.times}")
            lbl.add_css_class("tl-work-label")
            lbl.set_parent(self)
            self.work.append((lbl, w))
        for c in layout.cards:
            w = CardWidget(c, lambda card: self.on_tick(card), self._open_card, lambda card: self.on_delete(card))
            w.set_parent(self)
            self.cards.append(w)
        for n in layout.notes:
            lbl = Gtk.Label(label=n.text, xalign=0, ellipsize=Pango.EllipsizeMode.END, can_target=False)
            lbl.add_css_class("caption")
            lbl.add_css_class("tl-note")
            lbl.set_parent(self)
            self.notes.append((lbl, n))
        self.queue_resize()

    def _open_card(self, w: CardWidget) -> Gtk.Popover:
        """Tapping a card shows its real times (and what's noted on it), with Remove for what can be removed."""
        card = w.card
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6,
                      margin_start=8, margin_end=8)
        from thirties_core.titles import title_of
        title = Gtk.Label(label=title_of(card.span.title), xalign=0, wrap=True)
        title.add_css_class("heading")
        box.append(title)
        lines = [card.times] + ([card.status] if card.status else []) + card.notes
        for line in lines:
            lbl = Gtk.Label(label=line, xalign=0, wrap=True)
            lbl.add_css_class("dim-label")
            box.append(lbl)
        pop = Gtk.Popover(child=box)
        if w.fit == "bare" and card.tickable:  # its tick is hidden on the card: here it is
            done = Gtk.Button(label="Done", halign=Gtk.Align.START, margin_top=4)
            done.connect("clicked", lambda _b: (pop.popdown(), self.on_tick(card)))
            box.append(done)
            pop.done_button = done
        extra = self.extra_for(card)
        if extra is not None:
            box.append(Gtk.Separator(margin_top=4, margin_bottom=2))
            box.append(extra)
            pop.extra = extra
        if delete_call(card.span) is not None:
            remove = Gtk.Button(label="Remove", halign=Gtk.Align.START, margin_top=4)
            remove.add_css_class("destructive-action")
            remove.connect("clicked", lambda _b: (pop.popdown(), self.on_delete(card)))
            box.append(remove)
            pop.remove_button = remove
        pop.set_parent(w)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        if w.get_mapped():
            pop.popup()
        return pop

    # --- dragging and resizing ---

    def card_at(self, x: float, y: float) -> Optional[CardWidget]:
        width = self.get_width()
        for w in reversed(self.cards):
            cx, cy, cw, ch = self.lane_rect(width, w.card)
            if cx <= x <= cx + cw and cy <= y <= cy + ch:
                return w
        return None

    def begin_gesture(self, x: float, y: float) -> Optional[dict]:
        """What a press at (x, y) would drag: a card's body (move) or its top or bottom edge (resize)."""
        w = self.card_at(x, y)
        if w is None or not movable(w.card.span):
            return None
        picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and picked is not w:
            if isinstance(picked, Gtk.Button):
                return None  # the tick
            picked = picked.get_parent()
        _cx, cy, _cw, ch = self.lane_rect(self.get_width(), w.card)
        local = y - cy
        mode = "top" if local <= GRIP else "bottom" if local >= ch - GRIP else "move"
        return {"card": w.card, "mode": mode, "grab": int(max(0.0, y - self.y_of(w.card.row)) // PITCH),
                "x": x, "y": y}

    def gesture_call(self, drag: dict, dy: float, push: bool = False):
        """The tool call a drag of ``dy`` px produces (None: nothing changes)."""
        rows = (drag["y"] + dy - TOP) / PITCH
        if drag["mode"] == "move":
            return move_call(self.layout, drag["card"], drag["grab"], rows, push)
        return resize_call(self.layout, drag["card"], drag["mode"], rows)

    def _drag_begin(self, gesture, x, y) -> None:
        self._drag = self.begin_gesture(x, y) if self.layout else None
        if self._drag is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _drag_update(self, _gesture, dx, dy) -> None:
        d = self._drag
        if d is None or (abs(dx) < 4 and abs(dy) < 4):
            return
        card = d["card"]
        x, _y, w, _h = self.lane_rect(self.get_width(), card)
        rows = (d["y"] + dy - TOP) / PITCH
        if d["mode"] == "move":
            first, count = move_target(self.layout, card, d["grab"], rows), card.rows
        else:
            first, count = resize_extent(self.layout, card, d["mode"], rows)
        count = min(count, len(self.layout.slots) - first)
        self.ghost = (x, self.y_of(first) + GAP / 2, w, count * PITCH - GAP)
        night = self.layout.night
        if night is not None and not night.open and d["y"] + dy >= self.y_of(len(self.layout.slots)):
            self.on_night(True)  # dragged into the folded night: it opens (its rows come after, so nothing moves)
        self.queue_draw()

    def _drag_end(self, gesture, dx, dy) -> None:
        d, self._drag = self._drag, None
        self.ghost = None
        self.queue_draw()
        if d is None or (abs(dx) < 4 and abs(dy) < 4):
            return
        push = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        call = self.gesture_call(d, dy, push)
        if call is not None:
            GLib.idle_add(lambda: self.on_change(*call) and False)

    # --- geometry ---

    @staticmethod
    def y_of(row: float) -> float:
        return TOP + row * PITCH

    def lane_rect(self, width: float, card: Card) -> tuple:
        avail = width - RAIL - PAD_RIGHT
        lane_w = (avail - (card.lanes - 1) * LANE_GAP) / card.lanes
        x = RAIL + card.lane * (lane_w + LANE_GAP)
        y = self.y_of(card.row) + GAP / 2
        return x, y, lane_w, card.rows * PITCH - GAP

    def full_rect(self, width: float, row: int, rows: int) -> tuple:
        return RAIL, self.y_of(row) + GAP / 2, width - RAIL - PAD_RIGHT, rows * PITCH - GAP

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 320, 560, -1, -1
        rows = len(self.layout.slots) if self.layout else 0
        h = TOP * 2 + rows * PITCH + (NIGHT_H if self.layout is not None and self.layout.night else 0)
        return h, h, -1, -1

    def _place(self, child: Gtk.Widget, x: float, y: float, w: float, h: float) -> None:
        child.measure(Gtk.Orientation.HORIZONTAL, -1)
        child.measure(Gtk.Orientation.VERTICAL, int(w))
        child.allocate(max(1, int(w)), max(1, int(h)), -1, Gsk.Transform().translate(Graphene.Point().init(x, y)))

    @staticmethod
    def fit_for(lane_w: float) -> str:
        return "bare" if lane_w < BARE_LANE else "tight" if lane_w < WRAP_LANE else "roomy"

    def _refit(self, width: float) -> None:
        """Side by side on a phone, a card has little width: it wraps its title, or hands its tick to the popover."""
        for w in self.cards:
            w.set_fit(self.fit_for(self.lane_rect(width, w.card)[2]))

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if any(w.fit != self.fit_for(self.lane_rect(width, w.card)[2]) for w in self.cards):
            GLib.idle_add(lambda: self._refit(self.get_width()) or False)  # never reshape a child mid-allocation
        for w in self.cards:
            self._place(w, *self.lane_rect(width, w.card))
        for b, f in self.frees:
            self._place(b, *self.full_rect(width, f.row, f.rows))
        for lbl, c in self.work:
            x, y, w, _h = self.full_rect(width, c.row, 1)
            self._place(lbl, x + 14, y + 10, w / 2, 20)
        for lbl, n in self.notes:
            self._place(lbl, RAIL + 12, self.y_of(n.row) + GAP / 2, (width - RAIL - PAD_RIGHT) / 2, PITCH - GAP)
        if self.night_btn is not None:
            self._place(self.night_btn, RAIL, self.y_of(len(self.layout.slots)) + GAP / 2, width - RAIL - PAD_RIGHT,
                        NIGHT_H - GAP)

    # --- drawing ---

    def do_snapshot(self, snapshot) -> None:
        layout = self.layout
        if layout is None:
            return
        width, height = self.get_width(), self.get_height()
        fg = _fg(self)
        bg = _lookup(self, "window_bg_color", (0.13, 0.13, 0.15, 1) if _dark() else (0.98, 0.98, 0.98, 1))
        success = _lookup(self, "success_color", (0.18, 0.76, 0.49, 1))
        green = (success[0], success[1], success[2], 0.85)
        rect = Graphene.Rect().init(0, 0, width, height)
        cr = snapshot.append_cairo(rect)
        x0, slot_w = RAIL, width - RAIL - PAD_RIGHT

        # the slots: subtle rounded bands tinted from the foreground, so they show in light and dark
        for s in layout.slots:
            y = self.y_of(s.row) + GAP / 2
            if s.sleep:
                cr.set_source_rgba(*INDIGO, 0.18)
            else:
                cr.set_source_rgba(*fg, 0.03 if s.past else 0.055)
            _rounded(cr, x0, y, slot_w, PITCH - GAP, 8)
            cr.fill()

        # the sky bar, to the minute, rounded at its ends
        top, bottom = self.y_of(0) + GAP / 2, self.y_of(len(layout.slots)) - GAP / 2
        cr.save()
        _rounded(cr, SKY_X, top, SKY_W, bottom - top, SKY_W / 2)
        cr.clip()
        for band in layout.sky:
            cr.set_source_rgb(*(GOLDENROD if band.daylight else INDIGO))
            cr.rectangle(SKY_X, self.y_of(band.top), SKY_W, self.y_of(band.bottom) - self.y_of(band.top))
            cr.fill()
        cr.restore()

        # Work: one hatched card, its own grey accent strip, pips and notches; under everything placed on it
        for _lbl, c in self.work:
            x, y, w, h = self.full_rect(width, c.row, c.rows)
            cr.save()
            _rounded(cr, x, y, w, h, 9)
            cr.clip()
            cr.set_source_rgba(*bg)
            cr.paint()
            cr.set_source_rgba(*fg, 0.06)
            cr.paint()
            cr.set_source_rgba(*fg, 0.08)
            cr.set_line_width(1.5)
            for k in range(-int(h), int(w) + int(h), 9):
                cr.move_to(x + k, y + h)
                cr.line_to(x + k + h, y)
            cr.stroke()
            cr.set_source_rgba(*fg, 0.35)
            cr.rectangle(x, y, 3, h)
            cr.fill()
            cr.restore()
            draw_pips(cr, x + w, self.y_of, c.row, c.rows, (*fg, 0.6))
            draw_notches(cr, x, w, self.y_of, c.row, c.rows, bg)
        del cr

        for b, _f in self.frees:
            self.snapshot_child(b, snapshot)
        for lbl, _c in self.work:
            self.snapshot_child(lbl, snapshot)
        if layout.pencil:  # a later day's draft: in pencil, over the free time it would take; never a card
            cr = snapshot.append_cairo(rect)
            for m in layout.pencil:
                draw_pencil(cr, self, m, RAIL + 6, self.y_of(m.top) + GAP / 2 + 2, width - RAIL - PAD_RIGHT - 12,
                            max(18.0, (m.bottom - m.top) * PITCH - GAP - 4), fg)
            del cr
        cr = snapshot.append_cairo(rect)
        for _b, f in self.frees:  # green pips, and notches that carry the dotted outline round them
            if f.kind == "past":
                continue  # the past: a quiet outline, no free time to count
            x, _y, w, _h = self.full_rect(width, f.row, f.rows)
            draw_pips(cr, x + w, self.y_of, f.row, f.rows, green)
            draw_notches(cr, x, w, self.y_of, f.row, f.rows, bg, outline=(success[0], success[1], success[2], 0.7),
                         size=11)
        del cr

        for w in self.cards:
            self.snapshot_child(w, snapshot)
        for lbl, _n in self.notes:
            self.snapshot_child(lbl, snapshot)
        if self.night_btn is not None:
            self.snapshot_child(self.night_btn, snapshot)

        cr = snapshot.append_cairo(rect)
        # each card: a pip per thirty at its own right edge, and its notches (cutting its accent strip too)
        for w in self.cards:
            c = w.card
            x, _y, cw, _ch = self.lane_rect(width, c)
            draw_pips(cr, x + cw, self.y_of, c.row, c.rows,
                      (*fg, 0.6 * (0.6 if c.state in ("done", "skipped", "moot") else 1)))
            draw_notches(cr, x, cw, self.y_of, c.row, c.rows, bg)
        if self.ghost is not None:  # where a dragged card will land
            accent = Adw.StyleManager.get_default().get_accent_color().to_standalone_rgba(_dark())
            gx, gy, gw, gh = self.ghost
            _rounded(cr, gx, gy, gw, gh, 9)
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.18)
            cr.fill_preserve()
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.9)
            cr.set_line_width(2)
            cr.stroke()
        # the day's time marks on the rail: sunrise, solar noon, sunset (a notch across the sky bar, name and time)
        hand_y = self.y_of(layout.now_row) if layout.now_row is not None else None
        for name, t, row in sun_marks(layout):
            draw_sun_mark(cr, self.y_of(row), name, t, fg, hand_y)
        for t, row in night_marks(layout):  # the opened night's hours, in the same quiet marks (time only)
            draw_sun_mark(cr, self.y_of(row), "", t, fg, hand_y)
        # now: the badge on the sky bar and the hour hand into the day (a mark it's passing steps aside)
        if layout.now_row is not None:
            cy = self.y_of(layout.now_row)
            cx = SKY_X + SKY_W / 2
            draw_badge(cr, cx, cy, layout.now_daylight, bg)
            fill, edge = hand_colours(layout.now_daylight)
            draw_hand(cr, cx + BADGE / 2 - 2, cy, fill, outline=edge)


def _legend_swatch(kind: str) -> Gtk.DrawingArea:
    area = Gtk.DrawingArea(content_width=16, content_height=10, valign=Gtk.Align.CENTER)

    def draw(widget, cr, w, h):
        if kind in LOOK_RGB:
            cr.set_source_rgb(*_rgb(kind))
            _rounded(cr, 0, 0, w, h, 3)
            cr.fill()
        elif kind == "work":
            fg = _fg(widget)
            _rounded(cr, 0, 0, w, h, 3)
            cr.set_source_rgba(*fg, 0.08)
            cr.fill_preserve()
            cr.clip()
            cr.set_source_rgba(*fg, 0.35)
            cr.set_line_width(1.2)
            for k in range(-h, w + h, 4):
                cr.move_to(k, h)
                cr.line_to(k + h, 0)
            cr.stroke()
        else:
            cr.set_source_rgb(*(GOLDENROD if kind == "daylight" else INDIGO))
            _rounded(cr, 0, 0, w, h, 3)
            cr.fill()
    area.set_draw_func(draw)
    return area


def _legend(kinds_only: bool = False) -> Gtk.Widget:
    """The key: the sky's two colours and Work (Day tab), and the item colours by kind (both tabs). Wraps on a
    narrow window, keeping each swatch with its word."""
    box = Adw.WrapBox(child_spacing=12, line_spacing=4, align=0.5, margin_top=6, margin_bottom=18)
    box.add_css_class("tl-legend")
    entries = ([] if kinds_only else [("daylight", "daylight"), ("dark", "dark"), ("work", "work")]) + list(KIND_NAMES)
    box.entries = [name for _, name in entries]
    for kind, name in entries:
        pair = Gtk.Box(spacing=6)
        pair.append(_legend_swatch(kind))
        lbl = Gtk.Label(label=name)
        lbl.add_css_class("caption")
        pair.append(lbl)
        box.append(pair)
    if not kinds_only:
        hint = Gtk.Label(label="each slot is a thirty · one pip per thirty")
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box.append(hint)
    return box


# --- the Week tab ------------------------------------------------------------------------------------------------

COL_PAD = 2                     # the waking day's band inside a column
WORK_INSET = 6                  # items during Work sit this far inside its container, on each side
WEEK_HAND_SCALE = 31 / HAND_W   # today's two hands, ~31x28 px, inside the column
TODAY_FRAME = 3                 # the hands start just inside today's accent outline
WEEK_HEIGHT = 480               # a day's column: tall enough for a thirty's bar to hold a line of its name
WEEK_MIN_HEIGHT = 320           # on a short screen it gives way to this, so less of the week is below the fold
NAME_MIN_H, NAME_MIN_W = 11, 26  # a bar shows its item's name when it's at least this big
NAME_LINE_H = 10                # one line of the names' small text


class WeekColumnArea(Gtk.DrawingArea):
    """One day of the week, shrunken: its waking day, items as small bars, and Work as a container (one hatched grey
    rounded block the column's full width, "Work" small at its top-left, a faint inner outline) holding the items
    during it, inset WORK_INSET on each side; items outside work take the full width (the mockup the user approved,
    10-30). Today is outlined in the accent colour, with two small hour hands at the current time just inside its
    edges, pointing in from both sides (no line across). A drop target for the week's chips."""

    def __init__(self, on_drop: Callable[[str, date], None]) -> None:
        super().__init__(content_height=WEEK_HEIGHT, hexpand=True)
        self.column = None
        self.axis_hint = False
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        self.drop = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        self.drop.connect("drop", lambda _t, value, _x, _y: self.column is not None and not self.column.is_past
                          and bool(on_drop(str(value), self.column.day) or True))
        self.drop.connect("enter", lambda *_: self._hover(True))
        self.drop.connect("leave", lambda *_: self._hover(False))
        self.add_controller(self.drop)

    def _hover(self, on: bool):
        self.axis_hint = on and self.column is not None and not self.column.is_past
        self.queue_draw()
        return Gdk.DragAction.COPY if self.axis_hint else 0

    def set_column(self, column) -> None:
        self.column = column
        self.queue_draw()

    def _bar_rects(self, w: float, h: float):
        c = self.column
        for b in c.bars:
            inset = COL_PAD + (WORK_INSET if b.in_work else 2)
            inner_x, inner_w = inset, w - 2 * inset
            lane_w = (inner_w - (b.lanes - 1) * 2) / b.lanes
            yield b, (inner_x + b.lane * (lane_w + 2), 4 + b.top * (h - 8), lane_w,
                      max(4.0, (b.bottom - b.top) * (h - 8) - 1))

    def _draw(self, widget, cr, w, h) -> None:
        c = self.column
        if c is None:
            return
        fg = _fg(widget)
        top, bottom = 4 + c.awake[0] * (h - 8), 4 + c.awake[1] * (h - 8)
        cr.set_source_rgba(*fg, 0.035 if c.is_past else 0.06)
        _rounded(cr, COL_PAD, top, w - 2 * COL_PAD, bottom - top, 8)
        cr.fill()
        for a, b in c.work:  # Work: a hatched container the column's width, under the items during it
            x, y, ww, wh = COL_PAD, 4 + a * (h - 8), w - 2 * COL_PAD, (b - a) * (h - 8)
            cr.save()
            _rounded(cr, x, y, ww, wh, 7)
            cr.clip()
            cr.set_source_rgba(*fg, 0.07)
            cr.paint()
            cr.set_source_rgba(*fg, 0.09)
            cr.set_line_width(1.2)
            for k in range(-int(wh), int(ww) + int(wh), 7):
                cr.move_to(x + k, y + wh)
                cr.line_to(x + k + wh, y)
            cr.stroke()
            cr.restore()
            _rounded(cr, x + 1.5, y + 1.5, ww - 3, wh - 3, 6)  # the faint inner outline
            cr.set_source_rgba(*fg, 0.16)
            cr.set_line_width(1)
            cr.stroke()
            if wh >= 14:
                label = widget.create_pango_layout("Work")
                label.set_font_description(Pango.FontDescription.from_string("Sans Bold 7"))
                cr.move_to(x + 4, y + 2)
                cr.set_source_rgba(*fg, 0.5)
                PangoCairo.show_layout(cr, label)
        for b, (x, y, bw, bh) in self._bar_rects(w, h):
            dim = b.state in ("done", "skipped", "moot") or c.is_past
            cr.set_source_rgba(*_rgb(b.look), 0.45 if dim else 0.9)
            _rounded(cr, x, y, bw, bh, min(4, bh / 2))
            cr.fill()
            self._draw_name(widget, cr, b, x, y, bw, bh, dim, fg)
        if c.is_today:
            accent = Adw.StyleManager.get_default().get_accent_color().to_standalone_rgba(_dark())
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
            cr.set_line_width(2)
            _rounded(cr, 1, top - 1, w - 2, bottom - top + 2, 9)
            cr.stroke()
            if c.now is not None:  # two hands inside the column's edges, pointing in at now
                y = 4 + c.now * (h - 8)
                fill, edge = hand_colours(c.now_daylight)
                draw_hand(cr, TODAY_FRAME, y, fill, scale=WEEK_HAND_SCALE, outline=edge)
                draw_hand(cr, w - TODAY_FRAME, y, fill, scale=WEEK_HAND_SCALE, mirrored=True, outline=edge)
        if self.axis_hint:
            accent = Adw.StyleManager.get_default().get_accent_color().to_standalone_rgba(_dark())
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.15)
            _rounded(cr, COL_PAD, top, w - 2 * COL_PAD, bottom - top, 8)
            cr.fill()

    @staticmethod
    def _draw_name(widget, cr, bar, x: float, y: float, bw: float, bh: float, dim: bool, fg: tuple) -> None:
        """The item's short title inside its bar, where the bar has room for a line of small text (an hour or more
        at the usual size); a done one with a tick. Dark text on a full bar (every item colour is mid-toned), the
        theme's own on a faded one."""
        if bh < NAME_MIN_H or bw < NAME_MIN_W:
            return
        from thirties_core.titles import title_of
        text = title_of(bar.span.title)
        if bar.state in ("done", "partial"):
            text = "✓ " + text
        layout = widget.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string("Sans 7"))
        layout.set_width(int((bw - 6) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        lines = max(1, int((bh - 2) // NAME_LINE_H))
        layout.set_height(-lines)
        cr.save()
        cr.rectangle(x, y, bw, bh)
        cr.clip()
        cr.move_to(x + 3, y + 1)
        cr.set_source_rgba(*fg, 0.9) if dim else cr.set_source_rgba(0.08, 0.08, 0.1, 0.88)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    def _tooltip(self, widget, x, y, _keyboard, tooltip) -> bool:
        if self.column is None:
            return False
        for b, (bx, by, bw, bh) in self._bar_rects(self.get_width(), self.get_height()):
            if bx <= x <= bx + bw and by <= y <= by + bh:
                s = b.span
                from thirties_core.titles import title_of
                tooltip.set_text(f"{title_of(s.title)}\n{format_clock(s.start_dt)} – {format_clock(s.end_dt)}")
                return True
        return False


class WeekPage(Gtk.ScrolledWindow):

    def __init__(self) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.on_pin: Callable[[str, date], None] = lambda item_id, day: None
        self.units = DEFAULT_UNITS
        self.layout: Optional[WeekLayout] = None
        self.chips: List[Dict[str, str]] = []
        self.today: Optional[date] = None
        clamp = Adw.Clamp(maximum_size=900, tightening_threshold=600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                      margin_start=12, margin_end=12)
        self.head = head = Gtk.Box(spacing=12)
        self.title = Gtk.Label(label="This week", xalign=0, hexpand=True)
        self.title.add_css_class("title-4")
        self.total = Gtk.Label(xalign=1)
        self.total.add_css_class("heading")
        self.total.add_css_class("success")
        head.append(self.title)
        head.append(self.total)
        box.append(head)

        self.chip_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.chip_group.add_css_class("card")
        self.chip_group.add_css_class("tl-chips")
        chip_title = Gtk.Label(label="This week, no day yet", xalign=0)
        chip_title.add_css_class("caption-heading")
        chip_title.add_css_class("dim-label")
        self.chip_group.append(chip_title)
        self.chip_box = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self.chip_group.append(self.chip_box)
        box.append(self.chip_group)

        self.grid = Gtk.Grid(column_homogeneous=True, column_spacing=6, row_spacing=4)
        self.heads: List[Gtk.Label] = []
        self.areas: List[WeekColumnArea] = []
        self.frees: List[Gtk.Label] = []
        for i in range(7):
            head_lbl = Gtk.Label()
            head_lbl.add_css_class("caption-heading")
            area = WeekColumnArea(lambda item_id, day: self.on_pin(item_id, day))
            free = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
            free.add_css_class("caption")
            self.grid.attach(head_lbl, i, 0, 1, 1)
            self.grid.attach(area, i, 1, 1, 1)
            self.grid.attach(free, i, 2, 1, 1)
            self.heads.append(head_lbl)
            self.areas.append(area)
            self.frees.append(free)
        box.append(self.grid)
        self.legend = _legend(kinds_only=True)  # the item colours by kind
        box.append(self.legend)
        self.loading = Gtk.Label(label="Loading the week…")
        self.loading.add_css_class("dim-label")
        box.append(self.loading)
        clamp.set_child(box)
        self.set_child(clamp)
        self.column_height = WEEK_HEIGHT
        self._fitting = False
        for prop in ("notify::page-size", "notify::upper"):
            self.get_vadjustment().connect(prop, lambda *_: self._queue_fit())

    def fit_height(self, page: float, upper: float) -> int:
        """The columns' height that fits the week in the space shown (``page``), from the page's whole height now
        (``upper``, with the columns at their current height): WEEK_HEIGHT where there's room (a thirty's bar
        holds its name), down to WEEK_MIN_HEIGHT on a short screen. The week never scrolls sideways."""
        rest = upper - self.column_height
        return int(max(WEEK_MIN_HEIGHT, min(WEEK_HEIGHT, page - rest)))

    def _queue_fit(self) -> None:
        if not self._fitting:
            self._fitting = True
            GLib.idle_add(self._fit)

    def _fit(self) -> bool:
        self._fitting = False
        adj = self.get_vadjustment()
        if adj.get_page_size() <= 0:
            return False
        height = self.fit_height(adj.get_page_size(), adj.get_upper())
        if height != self.column_height:
            self.column_height = height
            for area in self.areas:
                area.set_content_height(height)
        return False

    def show_week(self, layout: WeekLayout, chips: List[Dict[str, str]], today: date, this_week: bool) -> None:
        self.layout, self.chips, self.today = layout, chips, today
        self.loading.set_visible(False)
        self.title.set_text("This week" if this_week else
                            f"The week of {layout.columns[0].day.strftime('%b %-d')}" if layout.columns else "")
        for i, col in enumerate(layout.columns[:7]):
            self.heads[i].set_text(col.day.strftime("%a %-d"))
            for cls in ("accent", "dim-label"):
                self.heads[i].remove_css_class(cls)
            if col.is_today:
                self.heads[i].add_css_class("accent")
            elif col.is_past:
                self.heads[i].add_css_class("dim-label")
            self.areas[i].set_column(col)
        self.set_units(self.units)
        child = self.chip_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.chip_box.remove(child)
            child = nxt
        self.chip_group.set_visible(bool(chips))
        for chip in chips:
            self.chip_box.append(self._chip(chip))

    def set_units(self, units: str) -> None:
        self.units = normalise_units(units)
        if self.layout is None:
            return
        self.total.set_text(free_text(self.layout.total_free, self.units))
        for i, col in enumerate(self.layout.columns[:7]):
            self.frees[i].set_text("" if col.is_past else free_text(col.free_blocks, self.units))

    def _chip(self, chip: Dict[str, str]) -> Gtk.Widget:
        from thirties_core.titles import title_of
        button = Gtk.MenuButton(label=title_of(chip["title"]), tooltip_text=(f"{title_of(chip['title'])} ({chip['list']}): drag onto a "
                                                                   "day, or pick one" if chip.get("list") else
                                                                   "Drag onto a day, or pick one"))
        button.add_css_class("tl-chip")
        days = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=4)
        pop = Gtk.Popover(child=days)
        for col in (self.layout.columns if self.layout else []):
            if col.is_past:
                continue
            b = Gtk.Button(label=col.day.strftime("%A") + (" (today)" if col.is_today else ""))
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, d=col.day: (pop.popdown(), self.on_pin(chip["item_id"], d)))
            days.append(b)
        button.set_popover(pop)
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(chip["item_id"]))
        button.add_controller(source)
        button.chip = chip
        return button


# --- the Month and Year tabs ---------------------------------------------------------------------------------------

MONTH_NAMES = 4        # a month's day shows this many names, then "+2 more"
YEAR_LINES = 4         # a year's month shows this many efforts, then "+2 more"
MONTH_NARROW = "max-width: 520sp"  # narrower than this, the month is a list of its days rather than a grid


def _hex(look: str) -> str:
    r, g, b = (int(c * 255) for c in _rgb(look))
    return f"#{r:02x}{g:02x}{b:02x}"


def item_markup(entry) -> str:
    """A day's item in a month: a dot in its kind's colour, then its short title; done (or worked on), a tick and
    faded (struck through when finished, as on the Day tab); not reported, faded and slanted."""
    title = GLib.markup_escape_text(entry.title)
    if entry.done:
        body = title if entry.look == "ongoing" else f"<s>{title}</s>"
        return f"<span alpha='60%'>✓ {body}</span>"
    if entry.state == "moot":
        return f"<span alpha='55%'><i>{title}</i></span>"
    return f"<span foreground='{_hex(entry.look)}'>●</span> {title}"


def pinned_markup(effort) -> str:
    """Planned for that day, not on its schedule yet: a hollow dot."""
    return f"<span alpha='70%'>○ <i>{GLib.markup_escape_text(effort.title)}</i></span>"


def shown_items(day) -> List:
    """What a month's day lists: its items (not what was skipped) in time order, then what's pinned to it."""
    return [e for e in day.items if e.state != "skipped"] + list(day.pinned)


def _markup_of(x) -> str:
    return pinned_markup(x) if hasattr(x, "sessions") else item_markup(x)


def _period_head(what: str) -> tuple:
    """A period's head: previous, its name, next, and a quiet total at the end."""
    head = Gtk.Box(spacing=6)
    prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=f"Previous {what}", valign=Gtk.Align.CENTER)
    nxt = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=f"Next {what}", valign=Gtk.Align.CENTER)
    for b in (prev, nxt):
        b.add_css_class("flat")
        b.add_css_class("circular")
    title = Gtk.Label(xalign=0)
    title.add_css_class("title-4")
    total = Gtk.Label(xalign=1, hexpand=True)
    total.add_css_class("dim-label")
    head.append(prev)
    head.append(title)
    head.append(nxt)
    head.append(total)
    return head, prev, title, nxt, total


def quarter_name(start: date) -> str:
    """A quarter as its months, "July–September": plain words, never "Q3"."""
    last = date(start.year, (start.month - 1) // 3 * 3 + 3, 1)
    first = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    return f"{first.strftime('%B')}–{last.strftime('%B')}"


def _counts(done: int, planned: int) -> str:
    bits = ([f"{done} done"] if done else []) + ([f"{planned} planned"] if planned else [])
    return " · ".join(bits)


def _children_of(widget: Gtk.Widget) -> List[Gtk.Widget]:
    out, child = [], widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class EffortsCard(Gtk.Box):
    """The efforts pinned to a period, under a heading per part ("This month"; "This year", "October–December"): each with its
    budget as a bar and "2 of 6 sessions done", or where it stands. Hidden when there are none."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("card")
        self.add_css_class("tl-period-card")
        self.lines: List[tuple] = []  # (heading, effort), for the tests

    def show(self, sections: List[tuple]) -> None:
        _clear(self)
        self.lines = []
        sections = [(h, efforts) for h, efforts in sections if efforts]
        self.set_visible(bool(sections))
        for heading, efforts in sections:
            lbl = Gtk.Label(label=heading, xalign=0)
            lbl.add_css_class("caption-heading")
            lbl.add_css_class("dim-label")
            self.append(lbl)
            for e in efforts:
                self.append(self._row(e))
                self.lines.append((heading, e))

    @staticmethod
    def _row(e) -> Gtk.Widget:
        """Its title and budget bar on one line, and what's noted on it under them (so a phone keeps the title)."""
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        line = Gtk.Box(spacing=10)
        title = Gtk.Label(label=e.title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                          tooltip_text=f"{e.title} ({e.list})" if e.list else e.title)
        if e.status in ("done", "gone"):
            title.add_css_class("dim-label")
        line.append(title)
        if e.budgeted:
            bar = Gtk.LevelBar(min_value=0, max_value=e.sessions, value=min(e.done, e.sessions),
                               valign=Gtk.Align.CENTER, width_request=96)
            line.append(bar)
        row.append(line)
        if e.note():
            note = Gtk.Label(label=e.note(), xalign=0)
            note.add_css_class("caption")
            note.add_css_class("dim-label")
            row.append(note)
        return row


class MonthPage(Adw.BreakpointBin):
    """A month: its plan's efforts, then a calendar grid of its weeks, each day a button with its items' short
    titles (done ones ticked) that opens the day. On a narrow window, a list of the days that have something."""

    def __init__(self) -> None:
        super().__init__(width_request=300, height_request=200)
        self.on_open_day: Callable[[date], None] = lambda d: None
        self.on_step: Callable[[int], None] = lambda step: None
        self.summary = None
        self.cells: Dict[date, Gtk.Button] = {}
        self.rows: Dict[date, Adw.ActionRow] = {}
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=980, tightening_threshold=600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                      margin_start=12, margin_end=12)
        head, self.prev, self.title, self.next, self.total = _period_head("month")
        self.prev.connect("clicked", lambda _b: self.on_step(-1))
        self.next.connect("clicked", lambda _b: self.on_step(1))
        box.append(head)
        self.efforts = EffortsCard()
        self.efforts.set_visible(False)
        box.append(self.efforts)
        self.views = Gtk.Stack(vhomogeneous=False, hhomogeneous=False)
        self.grid = Gtk.Grid(column_homogeneous=True, column_spacing=4, row_spacing=4)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.empty = Gtk.Label(label="Nothing planned this month", margin_top=12)
        self.empty.add_css_class("dim-label")
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        listing.append(self.list)
        listing.append(self.empty)
        self.views.add_named(self.grid, "grid")
        self.views.add_named(listing, "list")
        box.append(self.views)
        self.legend = _legend(kinds_only=True)
        box.append(self.legend)
        self.loading = Gtk.Label(label="Loading the month…")
        self.loading.add_css_class("dim-label")
        box.append(self.loading)
        clamp.set_child(box)
        scroll.set_child(clamp)
        self.set_child(scroll)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(MONTH_NARROW))
        narrow.add_setter(self.views, "visible-child-name", "list")
        self.add_breakpoint(narrow)

    def show_month(self, summary) -> None:
        self.summary = summary
        self.loading.set_visible(False)
        m, today = summary.month, summary.today
        self.title.set_text(m.start.strftime("%B %Y"))
        self.total.set_text(_counts(summary.done, summary.planned))
        self.efforts.show([("This month" if m.contains(today) else m.start.strftime("%B"), summary.efforts)])
        self._fill_grid(summary)
        self._fill_list(summary)

    def _fill_grid(self, summary) -> None:
        _clear(self.grid)
        self.cells = {}
        for col, d in enumerate(summary.weeks[0].days if summary.weeks else []):
            head = Gtk.Label(label=d.day.strftime("%a"))
            head.add_css_class("caption-heading")
            head.add_css_class("dim-label")
            self.grid.attach(head, col, 0, 1, 1)
        for r, week in enumerate(summary.weeks, start=1):
            for col, day in enumerate(week.days):
                cell = self._cell(day)
                self.grid.attach(cell, col, r, 1, 1)
                self.cells[day.day] = cell

    def _cell(self, day) -> Gtk.Button:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.START)
        number = Gtk.Label(label=str(day.day.day), xalign=0)
        number.add_css_class("tl-day-number")
        box.append(number)
        shown = shown_items(day)
        names = []
        for x in shown[:MONTH_NAMES]:
            lbl = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=True)
            lbl.set_markup(_markup_of(x))
            lbl.add_css_class("tl-month-item")
            box.append(lbl)
            names.append(lbl)
        if len(shown) > MONTH_NAMES:
            more = Gtk.Label(label=f"+{len(shown) - MONTH_NAMES} more", xalign=0)
            more.add_css_class("tl-month-item")
            more.add_css_class("dim-label")
            box.append(more)
        button = Gtk.Button(child=box)
        button.add_css_class("flat")
        button.add_css_class("tl-month-day")
        for cls, on in (("other", not day.in_month), ("today", day.is_today), ("past", day.is_past)):
            if on:
                button.add_css_class(cls)
        when = day.day.strftime("%A, %B %-d")
        tip = [when] + [f"{format_clock(e.start)}  {e.title}" + ("  (done)" if e.done else "")
                        for e in day.items if e.state != "skipped"] + \
              [f"{e.title}  (planned for this day)" for e in day.pinned]
        button.set_tooltip_text("\n".join(tip))
        button.update_property([Gtk.AccessibleProperty.LABEL], [f"{when}: open the day"])
        button.connect("clicked", lambda _b, d=day.day: self.on_open_day(d))
        button.names = names
        return button

    def _fill_list(self, summary) -> None:
        self.list.remove_all()
        self.rows = {}
        for day in summary.days:
            shown = shown_items(day)
            if not shown and not day.is_today:
                continue
            title = day.day.strftime("%A %-d") + (" (today)" if day.is_today else "")
            row = Adw.ActionRow(title=title, activatable=True, subtitle_lines=0)
            row.set_subtitle("\n".join(_markup_of(x) for x in shown) or "Nothing planned")
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, d=day.day: self.on_open_day(d))
            self.list.append(row)
            self.rows[day.day] = row
        self.list.set_visible(bool(self.rows))
        self.empty.set_visible(not self.rows)


class YearPage(Gtk.ScrolledWindow):
    """A year: the year plan's efforts (as a whole, then by quarter), then its twelve months, each with what was
    done and what's planned in it and the efforts pinned to it and its weeks; a month opens on the Month tab."""

    def __init__(self) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.on_open_month: Callable[[date], None] = lambda d: None
        self.on_step: Callable[[int], None] = lambda step: None
        self.summary = None
        self.cards: List[Gtk.Button] = []
        clamp = Adw.Clamp(maximum_size=980, tightening_threshold=600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                      margin_start=12, margin_end=12)
        head, self.prev, self.title, self.next, self.total = _period_head("year")
        self.prev.connect("clicked", lambda _b: self.on_step(-1))
        self.next.connect("clicked", lambda _b: self.on_step(1))
        box.append(head)
        self.efforts = EffortsCard()
        self.efforts.set_visible(False)
        box.append(self.efforts)
        self.months = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, min_children_per_line=1,
                                  max_children_per_line=4, column_spacing=8, row_spacing=8)
        box.append(self.months)
        self.loading = Gtk.Label(label="Loading the year…")
        self.loading.add_css_class("dim-label")
        box.append(self.loading)
        clamp.set_child(box)
        self.set_child(clamp)

    def show_year(self, summary) -> None:
        from thirties_core.horizons import period_label
        self.summary = summary
        self.loading.set_visible(False)
        y, today = summary.year, summary.today
        self.title.set_text(str(y.start.year))
        self.total.set_text(f"{summary.done} done" if summary.done else "")
        whole = "This year" if y.contains(today) else str(y.start.year)
        self.efforts.show([(whole, summary.efforts)] +
                          [(quarter_name(q.start), efforts) for q, efforts in summary.quarters])
        self.months.remove_all()
        self.cards = []
        for card in summary.months:
            button = self._card(card, lambda p: period_label(p, today))
            self.months.append(button)
            self.cards.append(button)

    def _card(self, card, label: Callable) -> Gtk.Button:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.START)
        name = Gtk.Label(label=card.month.start.strftime("%B"), xalign=0)
        name.add_css_class("heading")
        name.add_css_class("tl-month-name")
        box.append(name)
        empty = "" if card.is_past or card.efforts or card.weeks else "Nothing planned yet"
        counts = Gtk.Label(label=_counts(card.done, card.planned) or empty, xalign=0, visible=bool(
            _counts(card.done, card.planned) or empty))
        counts.add_css_class("caption")
        counts.add_css_class("dim-label")
        box.append(counts)
        lines = [(e.title, e.note()) for e in card.efforts] + \
                [(e.title, label(e.where)) for e in card.weeks]
        texts = []
        for title, note in lines[:YEAR_LINES]:
            text = f"{title} · {note}" if note else title
            lbl = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END, tooltip_text=text,
                            max_width_chars=22)  # its full text never widens every month's card
            lbl.add_css_class("caption")
            box.append(lbl)
            texts.append(text)
        if len(lines) > YEAR_LINES:
            more = Gtk.Label(label=f"+{len(lines) - YEAR_LINES} more", xalign=0)
            more.add_css_class("caption")
            more.add_css_class("dim-label")
            box.append(more)
        button = Gtk.Button(child=box, width_request=190)
        button.add_css_class("flat")
        button.add_css_class("tl-year-month")
        if card.is_current:
            button.add_css_class("current")
        elif card.is_past:
            button.add_css_class("past")
        button.update_property([Gtk.AccessibleProperty.LABEL],
                               [f"{card.month.start.strftime('%B %Y')}: open the month"])
        button.connect("clicked", lambda _b, d=card.month.start: self.on_open_month(d))
        button.lines = texts
        return button


# --- the page ----------------------------------------------------------------------------------------------------

PERIODS = (("day", "Day"), ("week", "Week"), ("month", "Month"), ("year", "Year"))


class TimelinePage(Gtk.Box):
    """Day and Week tabs. The window gives it the tools: ``run_tool`` (changes, then refreshes), and providers for
    the week (``load_week``), planning a free stretch (``stretch_options``, ``suggestions``) and the units."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, vexpand=True)
        self.run_tool: Optional[Tool] = None
        self.ongoing: Optional[Callable[[Any], bool]] = None
        # days -> (plans, chips): runs on a worker thread (reading 7 days touches the calendar); None: no week
        self.load_week: Optional[Callable[[date, List[date]], tuple]] = None
        self.stretch_options: Optional[Callable[[FreeStretch, int], list]] = None  # placement.Slot list
        self.suggestions: Optional[Callable[[], List[Dict[str, str]]]] = None
        # (title, undo) -> shows a toast with Undo (the window's toast overlay)
        self.toast: Optional[Callable[[str, Callable[[], None]], None]] = None
        self.get_units: Callable[[], str] = lambda: DEFAULT_UNITS
        self.set_units: Callable[[str], None] = lambda units: None
        self.week_start = 0
        self.threaded_week = True
        self.plan = None
        self.today: Optional[date] = None
        self.layout: Optional[DayLayout] = None
        self.night_open = False  # the night strip, opened by a click or a drag into it
        self.show_theirs: Callable[[], bool] = lambda: True  # other people's events shown (one switch)
        # the day's things with no time: () -> (kind, titles), kind "if_time" (today's If there's time) or
        # "pencilled" (a later day's list); and (kind, title, action) -> acts on one, as the Agenda does
        self.untimed: Optional[Callable[[], tuple]] = None
        # a later day's draft, pencilled in (week_draft.draft_for): day -> [{"title", "start", "minutes"}]
        self.drafts: Optional[Callable[[date], list]] = None
        self.untimed_act: Callable[[str, str, str], None] = lambda kind, title, action: None
        # the user's effort estimates: task id -> (amount, unit), (0, the usual unit) when there's none;
        # None where estimates aren't kept
        self.estimate_of: Optional[Callable[[str], tuple]] = None
        self._week_token = 0
        # the Month and Year tabs: providers run on a worker thread (month -> MonthSummary, year -> YearSummary);
        # opening a day from them goes through the window (open_day), then shows it on the Day tab
        self.load_month: Optional[Callable[[date], Any]] = None
        self.load_year: Optional[Callable[[int], Any]] = None
        self.open_day: Optional[Callable[[date], None]] = None
        self.month_shown: Optional[date] = None  # a month browsed to (its first day); None: the shown day's
        self.year_shown: Optional[int] = None
        self._period_token = 0
        self._anchor: Optional[date] = None

        self.stack = Adw.ViewStack(vexpand=True)
        # Day, Week, Month, Year: one centred switch right under the Timeline tab it belongs to, over the content
        # it changes (11-15: it sat at the far left, away from both)
        bar = Gtk.Box(spacing=12, margin_top=2, margin_bottom=6, margin_start=12, margin_end=12,
                      halign=Gtk.Align.CENTER)
        self.periods = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.periods.add_css_class("round")
        for name, label in PERIODS:
            self.periods.add(Adw.Toggle(name=name, label=label))
        self.periods.set_active_name("day")
        self.periods.update_property([Gtk.AccessibleProperty.LABEL], ["Show a day, a week, a month or a year"])
        self.periods.connect("notify::active-name", self._on_period)
        bar.append(self.periods)
        self.append(bar)
        self.append(self.stack)
        # free time in blocks or hours (a shared setting): beside the Week tab's free time, the one place it shows
        self.units_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER, tooltip_text="Free time in blocks or hours")
        self.units_toggle.add_css_class("round")
        self.units_toggle.add(Adw.Toggle(name="blocks", label="Blocks"))
        self.units_toggle.add(Adw.Toggle(name="hours", label="Hours"))
        self.units_toggle.set_active_name(DEFAULT_UNITS)
        self.units_toggle.connect("notify::active-name", self._on_units)

        self.canvas = DayCanvas()
        self.canvas.on_tick = self._tick_card
        self.canvas.on_free = self._plan_here
        self.canvas.on_change = self._change
        self.canvas.on_delete = self._delete
        self.canvas.on_night = self.set_night
        self.canvas.extra_for = self._effort_editor
        self.day_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=480)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6, margin_top=4)
        self.untimed_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False, margin_bottom=8)
        self.untimed_group.add_css_class("card")
        self.untimed_group.add_css_class("tl-chips")
        self.untimed_title = Gtk.Label(xalign=0)
        self.untimed_title.add_css_class("caption-heading")
        self.untimed_title.add_css_class("dim-label")
        self.untimed_group.append(self.untimed_title)
        self.untimed_box = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self.untimed_group.append(self.untimed_box)
        self.untimed_chips: Dict[str, Gtk.MenuButton] = {}
        inner.append(self.untimed_group)
        inner.append(self.canvas)
        self.day_legend = _legend()
        inner.append(self.day_legend)
        clamp.set_child(inner)
        self.day_scroll.set_child(clamp)
        self.week = WeekPage()
        self.week.on_pin = self._pin
        self.week.head.append(self.units_toggle)
        self.month = MonthPage()
        self.month.on_open_day = self._open_day
        self.month.on_step = self._step_month
        self.year = YearPage()
        self.year.on_open_month = self._open_month
        self.year.on_step = self._step_year
        self.stack.add_titled_with_icon(self.day_scroll, "day", "Day", "view-list-symbolic")
        self.stack.add_titled_with_icon(self.week, "week", "Week", "x-office-calendar-symbolic")
        self.stack.add_titled_with_icon(self.month, "month", "Month", "x-office-calendar-symbolic")
        self.stack.add_titled_with_icon(self.year, "year", "Year", "x-office-calendar-symbolic")
        self.stack.connect("notify::visible-child-name", self._on_tab)
        self._on_tab()
        self.add_css_class("timeline")

    # --- the day ---

    def now(self) -> Optional[datetime]:
        return get_current_time(self.plan.sunrise.tzinfo) if self.plan is not None else None

    def sync_units(self) -> None:
        """Follow the units setting (changed in Preferences, or on another device)."""
        units = normalise_units(self.get_units())
        if self.units_toggle.get_active_name() != units:
            self.units_toggle.set_active_name(units)
        self.week.set_units(units)

    def refresh(self, plan, today: Optional[date] = None) -> None:
        """Show a day (and, when the Week tab is up, its week)."""
        self.plan = plan
        self.today = today or self.today or plan.target_date
        if plan.target_date != self._anchor:  # another day: the Month and Year tabs follow it again
            self._anchor, self.month_shown, self.year_shown = plan.target_date, None, None
        self.sync_units()
        self.layout = self._lay_out()
        self.canvas.set_layout(self.layout)
        self._fill_untimed()
        self._reload_tab()

    def _fill_untimed(self) -> None:
        """Today's If there's time, or a later day's pencilled-in list, as chips over the day; each opens Done,
        Schedule it and the ways to put it off, the same as the Agenda's rows."""
        from thirties.views.agenda import IF_TIME_CHOICES, IF_TIME_TITLE, PENCIL_CHOICES, untimed_actions
        _clear(self.untimed_box)
        self.untimed_chips = {}
        kind, titles = "", []
        if self.untimed is not None:
            try:
                kind, titles = self.untimed() or ("", [])
            except Exception:
                kind, titles = "", []
        self.untimed_group.set_visible(bool(titles))
        if not titles:
            return
        self.untimed_title.set_text(IF_TIME_TITLE if kind == "if_time" else "On the list, no time yet")
        from thirties_core.titles import title_of
        for title in titles:
            chip = Gtk.MenuButton(label=title_of(title), tooltip_text=title)
            chip.add_css_class("tl-chip")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            pop = Gtk.Popover(child=box)

            def act(t, action, k=kind, p=pop):
                p.popdown()
                self.untimed_act(k, t, action)
            choices = IF_TIME_CHOICES if kind == "if_time" else PENCIL_CHOICES
            done, schedule, _later, menu = untimed_actions(title, choices, act)
            done.set_child(Gtk.Label(label="Done", xalign=0))  # in a menu, words rather than a bare tick
            box.append(done)
            box.append(schedule)
            box.append(Gtk.Separator())
            chip.actions = {"done": done, "schedule": schedule}
            for (action, _label), b in zip(choices[1], _children_of(menu)):  # putting it off: inline, not a submenu
                menu.remove(b)
                box.append(b)
                chip.actions[action] = b
            for b in chip.actions.values():  # one column of words, all from the left
                if isinstance(b.get_child(), Gtk.Label):
                    b.get_child().set_xalign(0)
            chip.set_popover(pop)
            self.untimed_box.append(chip)
            self.untimed_chips[title] = chip

    def _lay_out(self) -> DayLayout:
        try:
            theirs = bool(self.show_theirs())
        except Exception:
            theirs = True
        layout = day_layout(self.plan, self.now(), self.ongoing, self.night_open, show_theirs=theirs)
        day = self.plan.target_date
        if self.drafts is not None and self.today is not None and day > self.today:
            try:
                layout.pencil = pencil_marks(layout, self.drafts(day) or [])
            except Exception:
                layout.pencil = []
        return layout

    def tick(self) -> None:
        """A minute on: the hand moves; a new thirty changes what's free (on the Week tab too: 10-31, its hands
        stood still while the screen was up)."""
        if self.plan is None:
            return
        self._tick_week()
        layout = self._lay_out()
        if [(f.row, f.rows, f.kind) for f in layout.stretches] != \
                [(f.row, f.rows, f.kind) for f in self.layout.stretches] or \
                [c.current for c in layout.cards] != [c.current for c in self.layout.cards]:
            self.layout = layout
            self.canvas.set_layout(layout)
        else:
            self.layout.now_row, self.layout.now_daylight = layout.now_row, layout.now_daylight
            self.canvas.queue_draw()

    def scroll_to_now(self) -> None:
        """Keep the hand in view (another day: the top)."""
        attempts = [0]

        def go() -> bool:
            attempts[0] += 1
            adj = self.day_scroll.get_vadjustment()
            if self.layout is None:
                return False
            if adj.get_page_size() == 0 or self.canvas.get_height() == 0:
                return attempts[0] < 20
            if self.layout.now_row is None:
                adj.set_value(0)
                return False
            ok, point = self.canvas.compute_point(self.day_scroll.get_child(),
                                                  Graphene.Point().init(0, DayCanvas.y_of(self.layout.now_row)))
            y = point.y if ok else DayCanvas.y_of(self.layout.now_row)
            adj.set_value(max(0.0, min(y - adj.get_page_size() / 3, adj.get_upper() - adj.get_page_size())))
            return False
        GLib.timeout_add(100, go)

    def set_night(self, open_: bool) -> None:
        """Open the night strip (a click, or a drag reaching it) or fold it again."""
        if self.plan is None or open_ == self.night_open:
            return
        self.night_open = open_
        self.layout = self._lay_out()
        self.canvas.set_layout(self.layout)

    def _tick_card(self, card: Card) -> None:
        """Ticks it done through the same tool the Agenda uses; happening now means it finished at the tick."""
        if self.run_tool is None or not card.tickable:
            return
        args = {"event": card.span.id, "status": "done"}
        if card.finishes_now:
            args["finished"] = format_clock(self.now())
        self.run_tool("log_outcome", args)

    def move_to(self, span_id: str, start: datetime) -> bool:
        """Drag to move: to a thirty, sharing it with what's there, a fixed item too (placed by hand; item 17:
        moving one item never moves another)."""
        if self.run_tool is None:
            return False
        self.run_tool("update_event", {"event": span_id, "start": format_clock(start), "alongside": True})
        return True

    def _effort_editor(self, card: Card) -> Optional[Gtk.Widget]:
        """The user's estimate for an item from their lists (it's kept per item; a one-off plan entry has none)."""
        task_id = card.span.task_id
        if self.estimate_of is None or not task_id or not movable(card.span):
            return None
        from thirties.views.effort import EffortEditor
        from thirties_core.day_actions import estimate_calls
        try:
            amount, unit = self.estimate_of(task_id)
        except Exception:
            return None

        def save(value: float, chosen: str) -> None:
            if self.run_tool is None:
                return
            usual = self.estimate_of("")[1]  # no item: the unit the user usually estimates in
            for call in estimate_calls(task_id, value, chosen, usual):
                self.run_tool(*call)
        return EffortEditor(amount, unit, save)

    def _change(self, name: str, args: dict) -> str:
        """A drag or a resize: one tool call, like the assistant's."""
        return self.run_tool(name, args) if self.run_tool is not None else ""

    def _delete(self, card: Card) -> None:
        """Remove an item (remove_event), with Undo putting it back at its own times."""
        call = delete_call(card.span)
        if call is None or self.run_tool is None:
            return
        result = self.run_tool(*call) or ""
        if result.startswith(NOT_DONE):
            return
        span = card.span
        if self.toast is not None:
            self.toast(f"Removed {span.title}", lambda: self.run_tool(*restore_call(span)))

    # --- planning something into a free stretch ---

    def _plan_here(self, anchor: Gtk.Widget, stretch: FreeStretch) -> Gtk.Popover:
        pop = PlanHerePopover(self, stretch)
        pop.set_parent(anchor)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        if anchor.get_mapped():  # a popover on a widget that isn't on screen has no surface to pop up from
            pop.popup()
        self.plan_popover = pop
        return pop

    # --- the week ---

    def _on_period(self, *_):
        name = self.periods.get_active_name()
        if name and name != self.stack.get_visible_child_name():
            self.stack.set_visible_child_name(name)

    def _on_tab(self, *_):
        name = self.stack.get_visible_child_name()
        if name and self.periods.get_active_name() != name:
            self.periods.set_active_name(name)
        self._reload_tab()

    def _reload_tab(self) -> None:
        if self.plan is None:
            return
        name = self.stack.get_visible_child_name()
        if name == "week":
            self.reload_week()
        elif name == "month":
            self.reload_month()
        elif name == "year":
            self.reload_year()

    # --- the month and the year ---

    @property
    def month_anchor(self) -> Optional[date]:
        if self.month_shown is not None:
            return self.month_shown
        return self.plan.target_date.replace(day=1) if self.plan is not None else None

    @property
    def year_anchor(self) -> Optional[int]:
        if self.year_shown is not None:
            return self.year_shown
        return self.plan.target_date.year if self.plan is not None else None

    def _in_background(self, read: Callable[[], Any], show: Callable[[Any], None]) -> None:
        """Read on a worker thread (a month touches the calendars), show on the main loop; only the latest read
        is shown."""
        self._period_token += 1
        token = self._period_token

        def work():
            try:
                return read()
            except Exception:
                import logging
                logging.getLogger(__name__).warning("Loading the timeline failed", exc_info=True)
                return None

        def done(result):
            if result is not None and token == self._period_token:
                show(result)
            return False

        if not self.threaded_week:
            done(work())
            return
        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    def reload_month(self) -> None:
        if self.load_month is None or self.plan is None:
            return
        month = self.month_anchor
        self._in_background(lambda: self.load_month(month), self.month.show_month)

    def reload_year(self) -> None:
        if self.load_year is None or self.plan is None:
            return
        year = self.year_anchor
        self._in_background(lambda: self.load_year(year), self.year.show_year)

    def _step_month(self, step: int) -> None:
        first = self.month_anchor
        if first is None:
            return
        month = first.month - 1 + step
        self.month_shown = date(first.year + month // 12, month % 12 + 1, 1)
        self.reload_month()

    def _step_year(self, step: int) -> None:
        if self.year_anchor is None:
            return
        self.year_shown = self.year_anchor + step
        self.reload_year()

    def _open_day(self, day: date) -> None:
        """A day of the month, opened on the Day tab."""
        if self.open_day is not None:
            self.open_day(day)
        self.stack.set_visible_child_name("day")

    def _open_month(self, first: date) -> None:
        """A month of the year, opened on the Month tab."""
        self.month_shown = first
        if self.stack.get_visible_child_name() == "month":
            self.reload_month()
        else:
            self.stack.set_visible_child_name("month")

    def reload_week(self) -> None:
        if self.load_week is None or self.plan is None:
            return
        anchor, today, now = self.plan.target_date, self.today or self.plan.target_date, self.now()
        days = week_days(anchor, self.week_start)
        self._week_token += 1
        token = self._week_token

        def work():
            try:
                plans, chips = self.load_week(anchor, days)
                layout = week_layout(plans, now, today, self.ongoing)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("Loading the week failed", exc_info=True)
                return None
            return layout, chips, plans

        def show(result):
            if result is None or token != self._week_token:
                return False
            layout, chips, plans = result
            self.week.show_week(layout, chips, today, this_week=today in days)
            self._week_plans, self._week_today = plans, today  # for the tick: the hands move with no reload
            return False

        if not self.threaded_week:
            show(work())
            return
        threading.Thread(target=lambda: GLib.idle_add(show, work()), daemon=True).start()

    def _tick_week(self) -> None:
        """Lay the shown week out again for now, from the plans already loaded (no reload), and redraw its days."""
        plans = getattr(self, "_week_plans", None)
        if not plans or self.stack.get_visible_child_name() != "week":
            return
        layout = week_layout(plans, self.now(), self._week_today, self.ongoing)
        for area, col in zip(self.week.areas, layout.columns[:7]):
            area.set_column(col)
        self.week.layout = layout
        self.week.set_units(self.week.units)  # today's free time, from now on

    def _pin(self, item_id: str, day: date) -> None:
        if self.run_tool is not None:
            self.run_tool("pin_to_period", {"task": item_id, "when": day.isoformat()})
            self.reload_week()

    def _on_units(self, *_):
        units = normalise_units(self.units_toggle.get_active_name())
        self.week.set_units(units)
        if units != normalise_units(self.get_units()):
            self.set_units(units)


class PlanHerePopover(Gtk.Popover):
    """"Plan something here": what (typed, or one of the lists' candidates), how long, and where in the stretch
    (the slot_options reasoning: right after what's before, in the middle, right before what's next)."""

    def __init__(self, page: TimelinePage, stretch: FreeStretch) -> None:
        super().__init__()
        self.page, self.stretch = page, stretch
        self.task_id: Optional[str] = None
        units = normalise_units(page.get_units())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8, width_request=280)
        past = stretch.kind == "past"  # over already: a record of what happened then, not a plan
        head = Gtk.Label(label="What happened here?" if past else "Plan something here", xalign=0)
        head.add_css_class("heading")
        box.append(head)
        sub = Gtk.Label(label=f"{amount_text(stretch.rows, units)}, {format_clock(stretch.start)} – "
                              f"{format_clock(stretch.end)}", xalign=0)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        box.append(sub)
        self.entry = Gtk.Entry(placeholder_text="What did you do?" if past else "What?", activates_default=True)
        self.entry.connect("changed", self._on_typed)
        box.append(self.entry)

        suggestions = []
        if page.suggestions is not None and not past:
            try:
                suggestions = page.suggestions() or []
            except Exception:
                suggestions = []
        if suggestions:
            wrap = Adw.WrapBox(child_spacing=4, line_spacing=4)
            for s in suggestions:
                from thirties_core.titles import title_of
                b = Gtk.Button(label=title_of(s["title"]), tooltip_text=s.get("why") or None)
                b.add_css_class("tl-chip")
                b.connect("clicked", lambda _b, s=s: self._pick(s))
                wrap.append(b)
            box.append(wrap)

        length = Gtk.Box(spacing=8)
        length.append(Gtk.Label(label="How long", xalign=0, hexpand=True))
        self.blocks = Gtk.SpinButton.new_with_range(1, max(1, stretch.rows), 1)
        self.blocks.connect("value-changed", lambda *_: self._fill_where())
        self.length_label = Gtk.Label()
        length.append(self.blocks)
        length.append(self.length_label)
        box.append(length)
        self.where_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(self.where_box)
        self.add_btn = Gtk.Button(label="Add", sensitive=False)
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.connect("clicked", self._add)
        box.append(self.add_btn)
        self.set_child(box)
        self.set_default_widget(self.add_btn)
        self.choices: List[tuple] = []
        self._fill_where()

    def _fill_where(self) -> None:
        n = int(self.blocks.get_value())
        self.length_label.set_text("block" if n == 1 else "blocks")
        child = self.where_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.where_box.remove(child)
            child = nxt
        slots = []
        if self.page.stretch_options is not None and self.stretch.kind != "past":
            try:
                slots = self.page.stretch_options(self.stretch, n * 30) or []
            except Exception:
                slots = []
        if not slots:  # no reasoning available (or the past): the start of the stretch, or its end
            from thirties_core.placement import Slot
            span = (self.stretch.start, self.stretch.end)
            slots = [Slot(self.stretch.start, self.stretch.start + timedelta(minutes=30 * n), "at the start", span)]
            if n < self.stretch.rows:
                slots.append(Slot(self.stretch.end - timedelta(minutes=30 * n), self.stretch.end, "at the end", span))
        self.choices = []
        group = None
        for slot in slots:
            check = Gtk.CheckButton(label=slot.where[:1].upper() + slot.where[1:], group=group,
                                    tooltip_text=f"{format_clock(slot.start)} – {format_clock(slot.end)}")
            group = group or check
            self.where_box.append(check)
            self.choices.append((check, slot))
        self.choices[0][0].set_active(True)

    def _pick(self, suggestion: Dict[str, str]) -> None:
        self.entry.set_text(suggestion["title"])
        self.task_id = suggestion.get("task_id")  # set after the text: typing clears it

    def _on_typed(self, *_):
        self.task_id = None
        self.add_btn.set_sensitive(bool(self.entry.get_text().strip()))

    def chosen(self):
        return next((slot for check, slot in self.choices if check.get_active()), self.choices[0][1])

    def _add(self, *_):
        title = self.entry.get_text().strip()
        if not title or self.page.run_tool is None:
            return
        slot = self.chosen()
        if self.stretch.kind == "past":  # a record of what happened then: done, at those times
            self.popdown()
            self.page.run_tool(*record_call(title, slot.start, int(self.blocks.get_value()) * 30))
            return
        args = {"title": title, "start": format_clock(slot.start),
                "duration": f"{int(self.blocks.get_value()) * 30} minutes"}
        if self.task_id:
            args.update(category="task", task_id=self.task_id)
        self.popdown()
        self.page.run_tool("add_event", args)


__all__ = ["TimelinePage", "DayCanvas", "WeekPage", "MonthPage", "YearPage", "PlanHerePopover", "draw_hand",
           "draw_badge"]
