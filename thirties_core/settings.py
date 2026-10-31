"""User settings, as chosen in the app's setup and Preferences.

Settings overlay the defaults in ``config.py`` (and a developer's optional
``config.toml``): ``apply_settings`` writes them onto a ThirtiesConfig, which
the rest of the core reads as before.

Each setting has a scope:
- "shared": about the person (work hours, sleep, wishlists, permissions). Synced
  between devices, last write wins.
- "device": about this machine (brain, location, sync folder, Telegram bridge).
  Never synced.
Secrets (API key, Joplin token, bot token) are device settings kept in the
desktop keyring when the app enables it, else in the local database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setting:
    key: str             # "<config section>.<field>", or a free key without a config field
    scope: str           # "shared" or "device"
    secret: bool = False


_ALL = [
    # about the person: synced
    Setting("general.work_days", "shared"),
    Setting("general.work_start_thirty", "shared"),
    Setting("general.work_end_thirty", "shared"),
    Setting("general.sleep_start_thirty", "shared"),
    Setting("general.sleep_end_thirty", "shared"),
    Setting("general.week", "shared"),  # what's different on some weekdays (week.py)
    Setting("general.temperature_unit", "shared"),
    Setting("general.week_start", "shared"),
    Setting("media.goodreads_user_id", "shared"),
    Setting("media.letterboxd_username", "shared"),
    Setting("media.serializd_username", "shared"),
    Setting("joplin.write_back", "shared"),
    Setting("joplin.allowed_notebooks", "shared"),
    Setting("joplin.excluded_notebooks", "shared"),
    Setting("joplin.whitelist_notes", "shared"),
    Setting("joplin.someday_sections", "shared"),
    Setting("joplin.reset_routines", "shared"),
    Setting("joplin.file_done", "shared"),
    Setting("assistant.checkins", "shared"),
    Setting("assistant.checkin_after_minutes", "shared"),
    Setting("assistant.message_level", "shared"),
    Setting("telegram.notify_block_transitions", "shared"),
    Setting("calendar.modes", "shared"),  # per calendar: mine / show only / ignore (by name: uids differ per device)
    Setting("telegram.mirror_desktop", "shared"),
    Setting("timeline.free_units", "shared"),  # free time in "blocks" or "hours" (timeline_layout.py)
    Setting("assistant.sunrise_notice", "shared"),
    Setting("assistant.sunset_notice", "shared"),
    # about this machine
    Setting("assistant.brain", "device"),
    Setting("general.latitude", "device"),
    Setting("general.longitude", "device"),
    Setting("general.timezone", "device"),
    Setting("general.place_name", "device"),
    Setting("inference.sources", "device"),
    Setting("inference.gemini_api_key", "device", secret=True),
    Setting("joplin.api_token", "device", secret=True),
    Setting("joplin.launch_when_needed", "device"),
    # the headless sync peer (joplin_peer.py): per machine, since each computer runs its own
    Setting("joplin.peer_enabled", "device"),
    Setting("joplin.peer_target", "device"),
    Setting("joplin.peer_target_values", "device"),
    Setting("joplin.peer_token", "device", secret=True),
    Setting("joplin.peer_password", "device", secret=True),
    Setting("joplin.peer_master_password", "device", secret=True),
    Setting("sync.mode", "device"),
    Setting("sync.folder", "device"),
    Setting("telegram.enabled", "device"),
    Setting("telegram.bot_token", "device", secret=True),
    Setting("telegram.allowed_chat_ids", "device"),
    Setting("setup.done", "device"),
    Setting("background.enabled", "device"),
]
SETTINGS: Dict[str, Setting] = {s.key: s for s in _ALL}


class SecretStore:
    """The desktop keyring through libsecret (the Secret portal inside the Flatpak)."""

    SCHEMA_NAME = "tech.redfoxlabs.Thirties"

    def __init__(self) -> None:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        self._secret = Secret
        self._schema = Secret.Schema.new(self.SCHEMA_NAME, Secret.SchemaFlags.NONE,
                                         {"key": Secret.SchemaAttributeType.STRING})

    def get(self, key: str) -> Optional[str]:
        return self._secret.password_lookup_sync(self._schema, {"key": key}, None)

    def set(self, key: str, value: str) -> None:
        if value:
            self._secret.password_store_sync(self._schema, {"key": key}, self._secret.COLLECTION_DEFAULT,
                                             f"Thirties: {key}", value, None)
        else:
            self._secret.password_clear_sync(self._schema, {"key": key}, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Settings:
    """Read and write settings; shared ones carry timestamps for sync."""

    def __init__(self, state_db, secrets: Optional[SecretStore] = None) -> None:
        self.db = state_db
        self.secrets = secrets

    # --- values ---

    def get(self, key: str, default: Any = None) -> Any:
        spec = SETTINGS.get(key, Setting(key, "device"))
        raw: Optional[str]
        if spec.secret and self.secrets is not None:
            try:
                value = self.secrets.get(key)
            except Exception as e:
                logger.warning("Keyring unavailable for %s: %s", key, e)
                value = None
            if value is not None:
                return value
            raw = self.db.get_setting(f"secret:{key}")
            return json.loads(raw) if raw is not None else default
        if spec.secret:
            raw = self.db.get_setting(f"secret:{key}")
        elif spec.scope == "shared":
            raw = self.db.get_shared_setting(key)
        else:
            raw = self.db.get_setting(key)
        return json.loads(raw) if raw is not None else default

    def set(self, key: str, value: Any) -> None:
        spec = SETTINGS.get(key, Setting(key, "device"))
        if spec.secret:
            if self.secrets is not None:
                try:
                    self.secrets.set(key, str(value or ""))
                    self.db.delete_setting(f"secret:{key}")
                    return
                except Exception as e:
                    logger.warning("Keyring unavailable for %s, keeping it locally: %s", key, e)
            self.db.set_setting(f"secret:{key}", json.dumps(value))
        elif spec.scope == "shared":
            self.db.set_shared_setting(key, json.dumps(value), _now())
        else:
            self.db.set_setting(key, json.dumps(value))

    def is_set(self, key: str) -> bool:
        return self.get(key) is not None

    @property
    def setup_done(self) -> bool:
        return bool(self.get("setup.done", False))

    # --- the config the rest of the core reads ---

    def apply(self, config) -> Any:
        """Overlay every stored setting onto a ThirtiesConfig (in place) and return it."""
        for key in SETTINGS:
            section, _, name = key.partition(".")
            target = getattr(config, section, None)
            if target is None or not hasattr(target, name):
                continue
            value = self.get(key)
            if value is not None:
                setattr(target, name, value)
        config.sync.enabled = config.sync.mode in ("folder", "joplin")
        return config

    # --- sync (shared settings only) ---

    def records(self) -> List[Dict[str, Any]]:
        return self.db.get_shared_setting_records()

    def merge_record(self, record: Dict[str, Any]) -> int:
        if SETTINGS.get(record.get("key", ""), Setting("", "device")).scope != "shared":
            return 0
        return self.db.merge_shared_setting(record)


def settings_for(state_db, use_keyring: bool = False) -> Settings:
    secrets = None
    if use_keyring:
        try:
            secrets = SecretStore()
        except Exception as e:
            logger.warning("No keyring; secrets stay in the local database: %s", e)
    return Settings(state_db, secrets)


def apply_settings(config, state_db, use_keyring: bool = False):
    """The config with this device's and the person's settings applied."""
    return settings_for(state_db, use_keyring).apply(config)
