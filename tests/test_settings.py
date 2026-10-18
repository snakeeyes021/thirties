"""Settings: device vs shared scope, secrets, applying onto the config, syncing shared ones."""

import tempfile
import unittest
from pathlib import Path

from thirties_core.config import ThirtiesConfig
from thirties_core.scheduler import StateDatabase
from thirties_core.settings import Settings


class FakeKeyring:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def set(self, key, value):
        if value:
            self.items[key] = value
        else:
            self.items.pop(key, None)


def db():
    return StateDatabase(Path(tempfile.mkdtemp()) / "state.sqlite")


class TestSettings(unittest.TestCase):

    def test_values_round_trip_and_apply_onto_the_config(self):
        s = Settings(db())
        s.set("general.work_days", ["sat", "sun"])
        s.set("general.latitude", 51.5)
        s.set("sync.mode", "folder")
        s.set("sync.folder", "/tmp/x")
        cfg = s.apply(ThirtiesConfig())
        self.assertEqual(cfg.general.work_days, ["sat", "sun"])
        self.assertEqual(cfg.general.latitude, 51.5)
        self.assertTrue(cfg.sync.enabled)
        self.assertEqual(cfg.general.longitude, ThirtiesConfig().general.longitude)  # unset: the default stays

    def test_secrets_use_the_keyring_when_there_is_one(self):
        state = db()
        ring = FakeKeyring()
        s = Settings(state, ring)
        s.set("joplin.api_token", "abc")
        self.assertEqual(ring.items["joplin.api_token"], "abc")
        self.assertIsNone(state.get_setting("secret:joplin.api_token"))
        self.assertEqual(s.get("joplin.api_token"), "abc")
        plain = Settings(state)  # no keyring (e.g. a dev tool): stored locally instead
        plain.set("telegram.bot_token", "123:x")
        self.assertEqual(plain.get("telegram.bot_token"), "123:x")

    def test_only_shared_settings_sync_and_last_write_wins(self):
        a, b = Settings(db()), Settings(db())
        a.set("general.work_start_thirty", 18)
        a.set("assistant.brain", "slm")  # device setting: not in the records
        records = a.records()
        self.assertEqual([r["key"] for r in records], ["general.work_start_thirty"])
        self.assertEqual(b.merge_record(records[0]), 1)
        self.assertEqual(b.get("general.work_start_thirty"), 18)
        b.set("general.work_start_thirty", 20)  # newer
        self.assertEqual(b.merge_record(records[0]), 0)
        self.assertEqual(b.get("general.work_start_thirty"), 20)
        self.assertEqual(b.merge_record({"key": "assistant.brain", "value": '"frontier"', "updated_at": "9"}), 0)


if __name__ == "__main__":
    unittest.main()
