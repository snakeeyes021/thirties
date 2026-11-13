"""Run tests (or any GTK tool) in a private, headless desktop session, never the user's own.

    python3 tests/headless.py                        # the whole suite: python3 -m unittest discover tests
    python3 tests/headless.py -m unittest tests.test_gui
    python3 tests/headless.py -m tests.timeline_screenshot /tmp/shots

A GTK program that shares the user's runtime dir (/run/user/<uid>) can start their document portal and unmount it on
exit, which breaks every Flatpak app on the desktop. So the session here gets its own XDG_RUNTIME_DIR (mode 700), its
own session bus (dbus-run-session), a temporary HOME and XDG dirs, and a headless display: Xvfb when it's installed
(CI), else cage with its headless backend. The user's DISPLAY, WAYLAND_DISPLAY and session bus are never passed on.
The GTK tests run only inside this (THIRTIES_HEADLESS=1); anywhere else they're skipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DROP = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "SWAYSOCK",
        "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "GNOME_SETUP_DISPLAY")


def private_env(tmp: Path) -> dict:
    """The environment for the session: nothing that points at the user's own desktop."""
    env = {k: v for k, v in os.environ.items() if k not in DROP}
    runtime = tmp / "runtime"
    runtime.mkdir(mode=0o700)
    os.chmod(runtime, 0o700)
    home = tmp / "home"
    for sub in ("", ".config", ".local/share", ".cache", ".local/state"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    env.update({
        "XDG_RUNTIME_DIR": str(runtime), "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"), "THIRTIES_HEADLESS": "1", "GSK_RENDERER": "cairo",
        "NO_AT_BRIDGE": "1", "GTK_A11Y": "none", "GDK_DEBUG": "", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
        "PYTHONPATH": os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH", "")) if p),
    })
    return env


def _free_x_display() -> int:
    for n in range(90, 200):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            return n
    raise RuntimeError("no free X display number")


def inner(args: list) -> int:
    """Inside dbus-run-session: start the headless display and run the command on it."""
    tmp = Path(os.environ["THIRTIES_HEADLESS_TMP"])
    cmd = [sys.executable, *args]
    if shutil.which("Xvfb"):
        n = _free_x_display()
        xvfb = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(f"/tmp/.X11-unix/X{n}").exists():
                    break
                time.sleep(0.05)
            env = dict(os.environ, DISPLAY=f":{n}", GDK_BACKEND="x11")
            return subprocess.call(cmd, env=env, cwd=ROOT)
        finally:
            xvfb.terminate()
            xvfb.wait(timeout=10)
    if shutil.which("cage"):
        status = tmp / "status"
        # cage runs one program on its own headless output; it doesn't pass the exit status on, so a file does
        script = tmp / "run.sh"
        script.write_text('"$@"\necho $? > "$THIRTIES_HEADLESS_TMP/status"\n')
        # GTK draws through cage's own X server (Xwayland), as on CI's Xvfb: a Wayland popover on a later window
        # could wait forever for cage, a one-window compositor, to place it
        env = dict(os.environ, WLR_BACKENDS="headless", WLR_RENDERER="pixman", WLR_LIBINPUT_NO_DEVICES="1",
                   GDK_BACKEND="x11")
        subprocess.call(["cage", "--", "sh", str(script), *cmd], env=env, cwd=ROOT)
        return int(status.read_text().strip()) if status.exists() else 1
    print("headless: neither Xvfb nor cage is installed; can't give GTK a display", file=sys.stderr)
    return 1


def main(argv: list) -> int:
    if argv[:1] == ["--inner"]:
        return inner(argv[1:])
    args = argv or ["-m", "unittest", "discover", "tests"]
    if not shutil.which("dbus-run-session"):
        print("headless: dbus-run-session is not installed", file=sys.stderr)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="thirties-headless-"))
    try:
        env = private_env(tmp)
        env["THIRTIES_HEADLESS_TMP"] = str(tmp)
        return subprocess.call(["dbus-run-session", "--", sys.executable, str(Path(__file__).resolve()), "--inner",
                                *args], env=env, cwd=ROOT)
    finally:
        clean_up(tmp)


def clean_up(tmp: Path) -> None:
    """Unmount what the session's own services left mounted in its runtime dir (its document portal, gvfs), then
    delete it all. Only paths under ``tmp``: the user's own mounts are never touched."""
    for _ in range(20):
        mounts = [line.split()[1] for line in Path("/proc/self/mounts").read_text().splitlines()
                  if line.split()[1].startswith(str(tmp) + "/")]
        for point in mounts:
            subprocess.call(["fusermount3", "-u", "-z", point], stderr=subprocess.DEVNULL)
        shutil.rmtree(tmp, ignore_errors=True)
        if not tmp.exists():
            return
        time.sleep(0.25)
    print(f"headless: couldn't remove {tmp}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
