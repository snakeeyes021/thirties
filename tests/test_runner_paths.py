"""Where the host LiteRT runner is looked for: no hard-coded checkout path."""

import unittest
from pathlib import Path

from thirties_core.inference import host_runner_candidates

HOME = Path("/home/someone")
REPO = Path("/opt/src/thirties-checkout")


class HostRunnerCandidatesTests(unittest.TestCase):
    def test_no_checkout_path_is_assumed(self):
        for flatpak in (True, False):
            pythons, scripts = host_runner_candidates(HOME, REPO, flatpak, env={})
            self.assertFalse(any("dev/Thirties" in p for p in pythons + scripts), (pythons, scripts))

    def test_outside_the_sandbox_the_package_checkout_comes_first(self):
        pythons, scripts = host_runner_candidates(HOME, REPO, False, env={})
        self.assertEqual(pythons[0], str(REPO / ".venv" / "bin" / "python"))
        self.assertEqual(scripts[0], str(REPO / "thirties_core" / "runner.py"))
        self.assertIn(str(HOME / ".local/share/thirties/venv/bin/python"), pythons)

    def test_in_the_flatpak_the_shared_copy_and_venv_are_used(self):
        pythons, scripts = host_runner_candidates(HOME, REPO, True, env={})
        self.assertNotIn(str(REPO / ".venv" / "bin" / "python"), pythons)  # the sandbox path isn't on the host
        self.assertEqual(pythons[0], str(HOME / ".local/share/thirties/venv/bin/python"))
        self.assertEqual(scripts[0], str(HOME / ".local/share/thirties/runner.py"))

    def test_both_spellings_of_home_on_image_based_systems(self):
        pythons, _ = host_runner_candidates(HOME, REPO, True, env={})
        self.assertIn("/var/home/someone/.local/share/thirties/venv/bin/python", pythons)
        pythons, _ = host_runner_candidates(Path("/var/home/someone"), REPO, True, env={})
        self.assertIn("/home/someone/.local/share/thirties/venv/bin/python", pythons)

    def test_overrides_come_first(self):
        env = {"THIRTIES_RUNNER_PYTHON": "/usr/bin/python3.12", "THIRTIES_REPO": "/srv/thirties"}
        pythons, scripts = host_runner_candidates(HOME, REPO, True, env=env)
        self.assertEqual(pythons[:2], ["/usr/bin/python3.12", "/srv/thirties/.venv/bin/python"])
        self.assertIn("/srv/thirties/thirties_core/runner.py", scripts)

    def test_no_duplicates(self):
        pythons, scripts = host_runner_candidates(HOME, REPO, False, env={"THIRTIES_REPO": str(REPO)})
        self.assertEqual(len(pythons), len(set(pythons)))
        self.assertEqual(len(scripts), len(set(scripts)))


if __name__ == "__main__":
    unittest.main()
