"""Everything a running Thirties app shares: config with settings applied, the state database,
engines and the background service. Pure Python; the GTK client holds one of these.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Callable, List, Optional

from thirties_core.background import BackgroundService, DesktopNotify
from thirties_core.config import ThirtiesConfig, load_config
from thirties_core.inference import get_inference_engine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.scheduler import DeterministicScheduler
from thirties_core.settings import Settings, settings_for
from thirties_core.sync_engine import SyncEngine

logger = logging.getLogger(__name__)


class AppCore:
    def __init__(self, use_keyring: bool = True, desktop_notify: Optional[DesktopNotify] = None,
                 config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or load_config()
        self.scheduler = DeterministicScheduler(self.config)
        self.settings: Settings = settings_for(self.scheduler.state_db, use_keyring)
        self.settings.apply(self.config)
        self.desktop_notify = desktop_notify
        self.listeners: List[Callable[[], None]] = []
        self._build_engines()
        self.service: Optional[BackgroundService] = None

    def _build_engines(self) -> None:
        self.joplin_engine = JoplinEngine(self.config)
        self.inference_engine = get_inference_engine(self.config)
        self.sync_engine = SyncEngine(config=self.config, state_db=self.scheduler.state_db,
                                      joplin_engine=self.joplin_engine)

    @property
    def setup_done(self) -> bool:
        return self.settings.setup_done

    # --- the background service ---

    def start_service(self) -> None:
        if self.service is None:
            self.service = BackgroundService(self.config, self.desktop_notify, scheduler=self.scheduler,
                                             joplin_engine=self.joplin_engine, inference_engine=self.inference_engine,
                                             sync_engine=self.sync_engine)
        self.service.start()

    def stop_service(self) -> None:
        if self.service is not None:
            self.service.stop()
            self.service = None

    # --- settings changed (Preferences, setup) ---

    def reload(self) -> None:
        """Apply the current settings everywhere: the config is updated in place (so everything holding
        it sees the change), engines are rebuilt, the service restarts and listeners are told."""
        fresh = self.settings.apply(load_config())
        for section in dataclasses.fields(ThirtiesConfig):
            target, source = getattr(self.config, section.name), getattr(fresh, section.name)
            for f in dataclasses.fields(target):
                setattr(target, f.name, getattr(source, f.name))
        self._build_engines()
        was_running = self.service is not None and self.service.running
        self.stop_service()
        if was_running:
            self.start_service()
        for listener in list(self.listeners):
            try:
                listener()
            except Exception as e:
                logger.warning("A settings listener failed: %s", e)
