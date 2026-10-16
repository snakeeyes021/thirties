"""Configuration management and path resolution for Thirties."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class GeneralConfig:
    latitude: float = 38.8799
    longitude: float = -77.1067
    timezone: str = "America/New_York"
    place_name: str = ""  # shown in Preferences ("Arlington, Virginia"); the coordinates are what count
    work_start_thirty: int = 17   # 08:30 AM
    work_end_thirty: int = 31     # last work thirty starts 3:30 PM, so work ends 4:00 PM
    sleep_start_thirty: int = 46  # 11:00 PM
    sleep_end_thirty: int = 14    # 07:00 AM (exclusive: sleep ends at this time)
    # Days that get default work (mon..sun). Other days start with no work.
    work_days: List[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])


@dataclass
class JoplinConfig:
    db_path: Optional[str] = None
    api_token: str = ""
    api_port: int = 41184
    api_host: str = "127.0.0.1"
    write_back: bool = False          # the user allowed Thirties to check off, add and break down tasks
    launch_when_needed: bool = False  # start Joplin (minimized) when a write-back needs it and it isn't running
    # Notebooks to read tasks from (empty: all of them except the excluded ones), and single notes to add.
    allowed_notebooks: List[str] = field(default_factory=list)
    excluded_notebooks: List[str] = field(default_factory=lambda: ["Archive"])
    whitelist_notes: List[str] = field(default_factory=list)
    # Note headings whose checklist items are "someday/maybe": kept visible, never pushed.
    someday_sections: List[str] = field(
        default_factory=lambda: ["Probation", "Someday", "Maybe", "Someday/Maybe", "Backburner"]
    )


@dataclass
class CalendarConfig:
    primary_calendar_id: str = "primary"
    default_ignore_secondary: bool = False  # true: events on calendars that aren't yours are ignored entirely
    # Calendars to read, by display name or uid (empty: all enabled calendars).
    include_calendars: List[str] = field(default_factory=list)
    exclude_calendars: List[str] = field(default_factory=lambda: ["Birthdays & Anniversaries"])
    # Your addresses, used to read your RSVP on invites; found automatically from GNOME Online Accounts.
    my_emails: List[str] = field(default_factory=list)


@dataclass
class InferenceConfig:
    backend: str = "gemini"  # "gemini" (agy if installed, else the API), "gemini_api", "antigravity", "litert", "mock"
    gemini_model: str = "gemini-3.8-flash-low"
    gemini_api_key: str = ""
    agy_path: Optional[str] = None
    # Where the Gemini brain gets its model, in order of preference: "agy" (subscription) and/or "api" (key).
    sources: List[str] = field(default_factory=lambda: ["agy", "api"])
    litert_model_path: str = "~/.local/share/thirties/models/gemma-4-e2b.litertlm"
    temperature: float = 0.2
    timeout_seconds: float = 60.0


@dataclass
class AssistantConfig:
    brain: str = "frontier"  # "frontier" (large model, one rich prompt) or "slm" (on-device Gemma pipeline)
    slm_model_path: str = ""  # empty: look in ~/.local/share/thirties/models
    slm_voice: bool = True    # SLM: rewrite template replies naturally (facts are verified, else the template is used)
    checkins: bool = True     # ask "did you get to X?" after planned items end (Telegram)
    checkin_after_minutes: int = 10


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    allowed_chat_ids: List[int] = field(default_factory=list)
    notify_block_transitions: bool = True
    poll_interval_seconds: float = 2.0


@dataclass
class SyncConfig:
    mode: str = "off"     # "off", "folder" (files in a folder the user picks) or "joplin" (a Joplin note)
    folder: str = ""
    enabled: bool = False  # derived from mode
    notebook_name: str = "Archive"
    sync_subfolder: str = "Thirties_System"
    sync_note_title: str = "Thirties_State_Sync"
    device_id: Optional[str] = None
    auto_push_on_change: bool = True
    auto_pull_on_startup: bool = True


@dataclass
class MediaConfig:
    """Public media wishlists (read-only, no logins). Empty values disable a source."""
    goodreads_user_id: str = ""
    goodreads_shelves: List[str] = field(default_factory=lambda: ["to-read"])
    letterboxd_username: str = ""
    serializd_username: str = ""
    cache_hours: float = 12.0
    # Per refresh, at most this many extra page loads to fill in missing
    # Letterboxd directors / Serializd premiere years (results are remembered).
    detail_lookups_per_refresh: int = 25


@dataclass
class ThirtiesConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    joplin: JoplinConfig = field(default_factory=JoplinConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    media: MediaConfig = field(default_factory=MediaConfig)

    def resolve_joplin_db_path(self) -> Optional[Path]:
        """Locate Joplin's database.sqlite, checking configured path, ~/.config, and Flatpak paths."""
        candidates: List[Path] = []

        if self.joplin.db_path:
            candidates.append(Path(os.path.expanduser(self.joplin.db_path)))

        home = Path.home()
        candidates.extend([
            home / ".config" / "joplin-desktop" / "database.sqlite",
            home / ".var" / "app" / "net.cozic.joplin_desktop" / "config" / "joplin-desktop" / "database.sqlite",
        ])

        for path in candidates:
            if path.is_file():
                return path

        return None

    def resolve_state_db_path(self) -> Path:
        """Return the path to Thirties local state database."""
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        state_dir = data_home / "thirties"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return state_dir / "state.sqlite"


def get_default_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "thirties" / "config.toml"


def load_config(config_path: Optional[Path | str] = None) -> ThirtiesConfig:
    """Load configuration from a TOML file, falling back to defaults if not found."""
    path = Path(config_path) if config_path else get_default_config_path()
    path = Path(os.path.expanduser(str(path)))

    if not path.is_file():
        return ThirtiesConfig()

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return ThirtiesConfig()

    general_data = data.get("general", {})
    joplin_data = data.get("joplin", {})
    calendar_data = data.get("calendar", {})
    inference_data = data.get("inference", {})
    sync_data = data.get("sync", {})
    telegram_data = data.get("telegram", {})
    assistant_data = data.get("assistant", {})
    media_data = data.get("media", {})

    if sync_data.get("enabled") and "mode" not in sync_data:
        sync_data = {**sync_data, "mode": "joplin"}

    return ThirtiesConfig(
        general=GeneralConfig(**general_data) if general_data else GeneralConfig(),
        joplin=JoplinConfig(**joplin_data) if joplin_data else JoplinConfig(),
        calendar=CalendarConfig(**calendar_data) if calendar_data else CalendarConfig(),
        inference=InferenceConfig(**inference_data) if inference_data else InferenceConfig(),
        sync=SyncConfig(**sync_data) if sync_data else SyncConfig(),
        telegram=TelegramConfig(**telegram_data) if telegram_data else TelegramConfig(),
        assistant=AssistantConfig(**assistant_data) if assistant_data else AssistantConfig(),
        media=MediaConfig(**media_data) if media_data else MediaConfig(),
    )
