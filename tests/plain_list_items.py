"""A test seam for tests about where Python's first guess places a leftover it may place.

Python's first guess gives a day only to leftovers never planned for a day now over (carry.replan_leftovers,
past_one_offs.planned_over); the rest wait, not marked yet, for the one morning question. Tests of the placement
itself make their leftovers by planning them yesterday, so here those items count as plain list items.
"""

from unittest.mock import patch


def as_plain_list_items(case) -> None:
    """For the rest of this test, leftovers count as never planned for a day now over."""
    p = patch("thirties_core.past_one_offs.planned_over", new=lambda facts, day: False)
    p.start()
    case.addCleanup(p.stop)
