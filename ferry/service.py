"""Run Ferry as a per-user background service - no administrator rights needed.

* Windows: ``HKCU\\...\\Run`` entry (starts at logon, hidden), started immediately.
* macOS:   ``~/Library/LaunchAgents/com.ferry.daemon.plist`` (launchctl, KeepAlive).
* Linux:   ``systemd --user`` unit (Restart=on-failure), with an ``@reboot`` crontab
           entry as fallback where systemd user sessions aren't available.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ferry.config import data_dir
from ferry.daemon import self_command, spawn_detached

LABEL = "com.ferry.daemon"
UNIT = "ferry.service"


def _cmd() -> list[str]:
    return self_command() + ["daemon", "run"]


def _stop_manual_daemon():
    """Stop a daemon started by hand so the service manager can own the process."""
    from ferry.api import Client
    c = Client()
    if c.alive():
        try:
            c.post("/shutdown")
        except Exception:
            pass
        for _ in range(40):
            if not c.alive():
                break
            time.sleep(0.25)


def _env() -> dict:
    env = {}
    if os.environ.get("FERRY_HOME"):
        env["FERRY_HOME"] = os.environ["FERRY_HOME"]
    return env


def _run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


# ------------------------------------------------------------------ Windows --
def _win_install():
    import winreg
    cmd = subprocess.list2cmdline(_cmd())
    if _env():
        # pass FERRY_HOME via cmd /c set ... (Run entries have no environment option)
        cmd = f'cmd /c "set FERRY_HOME={_env()["FERRY_HOME"]}&& {cmd}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Ferry", 0, winreg.REG_SZ, cmd)
    spawn_detached()
    return "Installed: starts automatically when you log in (registry Run key, no admin needed). Running now."


def _win_uninstall():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "Ferry")
    except FileNotFoundError:
        pass
    return "Removed from startup."


def _win_status():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            return True, winreg.QueryValueEx(k, "Ferry")[0]
    except FileNotFoundError:
        return False, None


# -------------------------------------------------------------------- macOS --
def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _mac_install():
    p = _plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    logs = data_dir() / "logs"
    plist = {"Label": LABEL, "ProgramArguments": _cmd(), "RunAtLoad": True,
             "KeepAlive": {"SuccessfulExit": False}, "ProcessType": "Background",
             "StandardOutPath": str(logs / "daemon.stdout.log"), "StandardErrorPath": str(logs / "daemon.stdout.log"),
             "EnvironmentVariables": _env() or {"FERRY_SERVICE": "1"}}
    with open(p, "wb") as f:
        plistlib.dump(plist, f)
    uid = os.getuid()
    _run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    _stop_manual_daemon()
    r = _run(["launchctl", "bootstrap", f"gui/{uid}", str(p)])
    if r.returncode != 0:
        r = _run(["launchctl", "load", "-w", str(p)])
    return f"Installed LaunchAgent {p} (starts at login, restarts on failure)."


def _mac_uninstall():
    p = _plist_path()
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    if p.exists():
        _run(["launchctl", "unload", "-w", str(p)])
        p.unlink()
    return "LaunchAgent removed."


def _mac_status():
    p = _plist_path()
    return p.exists(), str(p) if p.exists() else None


# -------------------------------------------------------------------- Linux --
def _unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT


def _has_systemd_user() -> bool:
    if not shutil.which("systemctl"):
        return False
    return _run(["systemctl", "--user", "show-environment"]).returncode == 0


def _linux_install():
    if _has_systemd_user():
        p = _unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        env_lines = "".join(f'Environment="{k}={v}"\n' for k, v in _env().items())
        p.write_text(
            "[Unit]\nDescription=Ferry file copy & sync service\nAfter=network-online.target\n\n"
            f"[Service]\nType=simple\nExecStart={shlex.join(_cmd())}\n{env_lines}"
            "Restart=on-failure\nRestartSec=10\nKillSignal=SIGTERM\nTimeoutStopSec=30\n\n"
            "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _stop_manual_daemon()
        r = _run(["systemctl", "--user", "enable", "--now", UNIT])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        msg = f"Installed systemd user service {p}."
        linger = _run(["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"]).stdout
        if "Linger=yes" not in linger:
            r = _run(["loginctl", "enable-linger"])
            if r.returncode == 0:
                msg += " Enabled lingering: it runs even when you are not logged in."
            else:
                msg += (" Note: it runs while you are logged in. To keep it running after logout/reboot "
                        "without login, an admin can run: loginctl enable-linger " + os.environ.get("USER", ""))
        return msg
    # fallback: crontab @reboot
    if not shutil.which("crontab"):
        raise RuntimeError("neither systemd --user nor crontab available; start it with 'ferry daemon start'")
    current = _run(["crontab", "-l"]).stdout
    lines = [ln for ln in current.splitlines() if "# ferry-daemon" not in ln]
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _env().items())
    lines.append(f"@reboot {env} {shlex.join(_cmd())} >/dev/null 2>&1 # ferry-daemon")
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    spawn_detached()
    return "Installed @reboot crontab entry (systemd user session not available). Running now."


def _linux_uninstall():
    msgs = []
    p = _unit_path()
    if p.exists():
        _run(["systemctl", "--user", "disable", "--now", UNIT])
        p.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
        msgs.append("systemd user service removed.")
    if shutil.which("crontab"):
        current = _run(["crontab", "-l"]).stdout
        if "# ferry-daemon" in current:
            lines = [ln for ln in current.splitlines() if "# ferry-daemon" not in ln]
            subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)
            msgs.append("crontab entry removed.")
    return " ".join(msgs) or "No service was installed."


def _linux_status():
    p = _unit_path()
    if p.exists():
        active = _run(["systemctl", "--user", "is-active", UNIT]).stdout.strip()
        return True, f"{p} ({active})"
    if shutil.which("crontab") and "# ferry-daemon" in _run(["crontab", "-l"]).stdout:
        return True, "crontab @reboot entry"
    return False, None


def install() -> str:
    if os.name == "nt":
        return _win_install()
    if sys.platform == "darwin":
        return _mac_install()
    return _linux_install()


def uninstall() -> str:
    if os.name == "nt":
        return _win_uninstall()
    if sys.platform == "darwin":
        return _mac_uninstall()
    return _linux_uninstall()


def status() -> tuple[bool, str | None]:
    if os.name == "nt":
        return _win_status()
    if sys.platform == "darwin":
        return _mac_status()
    return _linux_status()
