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
    work_start_thirty: int = 17   # 08:30 AM
    work_end_thirty: int = 31     # 03:30 PM
    sleep_start_thirty: int = 46  # 11:00 PM
    sleep_end_thirty: int = 14    # 07:00 AM


@dataclass
class JoplinConfig:
    db_path: Optional[str] = None
    api_token: str = ""
    api_port: int = 41184
    api_host: str = "127.0.0.1"
    allowed_notebooks: List[str] = field(
        default_factory=lambda: [
            "1. Tasks",
            "2. Dev",
            "3. Creative",
            "4. Media",
            "5. Misc.",
        ]
    )
    excluded_notebooks: List[str] = field(
        default_factory=lambda: ["Archive"]
    )
    whitelist_notes: List[str] = field(
        default_factory=lambda: ["30s Completed Log"]
    )


@dataclass
class CalendarConfig:
    primary_calendar_id: str = "primary"
    default_ignore_secondary: bool = False


@dataclass
class InferenceConfig:
    backend: str = "litert"
    litert_model_path: str = "~/.local/share/thirties/models/gemma-4-e2b.litertlm"
    temperature: float = 0.2


@dataclass
class ThirtiesConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    joplin: JoplinConfig = field(default_factory=JoplinConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)

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
        state_dir.mkdir(parents=True, exist_ok=True)
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

    return ThirtiesConfig(
        general=GeneralConfig(**general_data) if general_data else GeneralConfig(),
        joplin=JoplinConfig(**joplin_data) if joplin_data else JoplinConfig(),
        calendar=CalendarConfig(**calendar_data) if calendar_data else CalendarConfig(),
        inference=InferenceConfig(**inference_data) if inference_data else InferenceConfig(),
    )
