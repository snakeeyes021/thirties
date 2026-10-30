"""Assemble the user's backlog from every connected source.

Joplin is the first task source; public media wishlists (Goodreads, Letterboxd,
Serializd) add read-only "want to read/watch" items. Clients call
``load_backlog`` instead of talking to each connector.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.config import ThirtiesConfig
from thirties_core.connectors import TaskSource
from thirties_core.models import TaskItem

logger = logging.getLogger(__name__)


DONE_HIDES_DAYS = 7  # an item recorded done stays hidden this long, even if Joplin still shows it open


def load_backlog(config: ThirtiesConfig, joplin_engine: Optional[TaskSource] = None, scheduler=None,
                 media: Optional[List[TaskItem]] = None) -> Tuple[List[TaskItem], List[Dict[str, Any]]]:
    """Return (tasks, notes) from all sources, with list roles, urgency and deferral history attached
    (and without the notes the user set to be ignored). `joplin_engine` is the task source (any
    ``connectors.TaskSource``; Joplin is the first). `media`: wishlist items already fetched (a re-read of
    the lists needn't go to the network again)."""
    tasks: List[TaskItem] = []
    notes: List[Dict[str, Any]] = []
    if joplin_engine is not None:
        tasks.extend(joplin_engine.fetch_tasks())
        notes = joplin_engine.fetch_active_notes()
    tasks.extend(load_media_tasks(config) if media is None else media)
    if scheduler is not None:
        from thirties_core.lists import ListCatalog
        catalog = ListCatalog(scheduler.state_db)
        tasks = catalog.apply(tasks)
        ignored = {nid for nid, r in catalog.roles().items() if r["role"] == "ignore"}
        notes = [n for n in notes if n["id"] not in ignored]
        scheduler.attach_history_to_tasks(tasks)
        done = scheduler.state_db.completed_since(date.today() - timedelta(days=DONE_HIDES_DAYS))
        tasks = [t for t in tasks if t.id not in done or t.list_role == "routine"]  # done is done, even if
    return tasks, notes                                                             # Joplin didn't get the tick


def load_media_tasks(config: ThirtiesConfig, opener=None) -> List[TaskItem]:
    media = getattr(config, "media", None)
    if not media or not (media.goodreads_user_id or media.letterboxd_username or media.serializd_username):
        return []
    try:
        from thirties_core.media_sources import fetch_all_media, media_items_as_tasks
        return media_items_as_tasks(fetch_all_media(config, opener=opener))
    except Exception as e:  # a wishlist outage must never break planning
        logger.warning("Media wishlists unavailable: %s", e)
        return []


__all__ = ["load_backlog", "load_media_tasks"]
