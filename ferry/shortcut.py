"""Put Ferry, with its icon, in the system's app menu (no admin rights needed).

  Windows: a shortcut in your Start menu  (%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Ferry.lnk)
  Linux:   ~/.local/share/applications/ferry.desktop + icons in ~/.local/share/icons/hicolor
  macOS:   not needed - drag ferry-gui.app into Applications (it already carries the icon)
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ferry import __version__

ASSETS = Path(__file__).resolve().parent / "assets"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
DESKTOP_ID = "ferry"            # also the Qt desktop file name, so Linux shows the icon in the taskbar


def icon_path(size: int = 256) -> Path:
    return ASSETS / f"icon-{size}.png"


def gui_command() -> list[str]:
    """How to start the desktop app from a menu."""
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve()
        name = "ferry-gui.exe" if os.name == "nt" else "ferry-gui"
        if here.name.lower() == name:
            return [str(here)]
        sibling = here.parent / name
        if sibling.exists():
            return [str(sibling)]
        return [str(here), "gui"]
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"
    return [exe, "-m", "ferry", "gui"]


# ---------------------------------------------------------------- Linux ---
def _xdg_data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _linux_paths():
    data = _xdg_data()
    return data / "applications" / f"{DESKTOP_ID}.desktop", data / "icons" / "hicolor"


def _linux_write(visible: bool) -> Path:
    """Write ferry.desktop + icons. visible=False keeps it out of the app menu (NoDisplay) but still lets the
    dock/taskbar find Ferry's icon: on Wayland (GNOME, KDE) a window's own icon is ignored and the
    compositor only uses the icon of the .desktop file whose name matches the app id ("ferry")."""
    desktop, hicolor = _linux_paths()
    for s in ICON_SIZES:
        d = hicolor / f"{s}x{s}" / "apps"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon_path(s), d / f"{DESKTOP_ID}.png")
    scalable = hicolor / "scalable" / "apps"
    scalable.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS / "ferry.svg", scalable / f"{DESKTOP_ID}.svg")
    desktop.parent.mkdir(parents=True, exist_ok=True)
    cmd = " ".join(shlex.quote(c) for c in gui_command())
    desktop.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Ferry\n"
        "GenericName=File copy and sync\n"
        "Comment=Copy and sync files, folders and permissions\n"
        f"Exec={cmd}\n"
        f"Icon={DESKTOP_ID}\n"
        "Terminal=false\n"
        "Categories=Utility;FileTools;\n"
        "Keywords=copy;sync;backup;mirror;robocopy;rsync;\n"
        + ("" if visible else "NoDisplay=true\n")
        + f"X-Ferry-Version={__version__}\n", encoding="utf-8")
    desktop.chmod(0o755)
    for tool in (["update-desktop-database", str(desktop.parent)], ["gtk-update-icon-cache", "-q", "-t", str(hicolor)]):
        if shutil.which(tool[0]):
            subprocess.run(tool, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return desktop


def _linux_install() -> str:
    desktop = _linux_write(visible=True)
    return f"Added Ferry to your app menu ({desktop})."


def _linux_uninstall() -> str:
    desktop, _ = _linux_paths()
    if desktop.exists() and "NoDisplay=true" not in desktop.read_text(errors="replace"):
        _linux_write(visible=False)          # keep the hidden entry so the dock still shows the icon
        return "Removed Ferry from your app menu."
    return "Ferry was not in your app menu."


def _linux_remove_all():
    """Remove every trace (used by tests and full uninstall)."""
    desktop, hicolor = _linux_paths()
    desktop.unlink(missing_ok=True)
    for s in ICON_SIZES:
        (hicolor / f"{s}x{s}" / "apps" / f"{DESKTOP_ID}.png").unlink(missing_ok=True)
    (hicolor / "scalable" / "apps" / f"{DESKTOP_ID}.svg").unlink(missing_ok=True)


def ensure_linux_icon() -> bool:
    """Called when the desktop app starts on Linux: make sure the dock can show Ferry's icon.

    Creates a hidden ferry.desktop if there is none, and refreshes ours when Ferry moved or was
    updated. Leaves a ferry.desktop that someone else wrote alone. Returns True if it wrote one.
    """
    if not sys.platform.startswith("linux"):
        return False
    desktop, hicolor = _linux_paths()
    try:
        if desktop.exists():
            text = desktop.read_text(errors="replace")
            if "X-Ferry-Version=" not in text:
                return False                     # not ours
            cmd = " ".join(shlex.quote(c) for c in gui_command())
            fresh = (f"Exec={cmd}\n" in text and f"X-Ferry-Version={__version__}\n" in text
                     and (hicolor / "256x256" / "apps" / f"{DESKTOP_ID}.png").exists())
            if fresh:
                return False
            _linux_write(visible="NoDisplay=true" not in text)
            return True
        _linux_write(visible=False)
        return True
    except OSError:
        return False


# -------------------------------------------------------------- Windows ---
def _win_lnk() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Ferry.lnk"


def _win_install() -> str:
    import win32com.client
    lnk = _win_lnk()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    cmd = gui_command()
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(lnk))
    sc.TargetPath = cmd[0]
    sc.Arguments = subprocess.list2cmdline(cmd[1:])
    sc.WorkingDirectory = str(Path(cmd[0]).parent)
    # the frozen ferry-gui.exe carries the icon itself; otherwise point at the .ico file
    sc.IconLocation = f"{cmd[0]},0" if cmd[0].lower().endswith("ferry-gui.exe") else str(ASSETS / "ferry.ico")
    sc.Description = "Copy and sync files, folders and permissions"
    sc.Save()
    return f"Added Ferry to your Start menu ({lnk})."


def _win_uninstall() -> str:
    lnk = _win_lnk()
    if lnk.exists():
        lnk.unlink()
        return "Removed Ferry from your Start menu."
    return "Ferry was not in your Start menu."


# ------------------------------------------------------------------ API ---
def supported() -> bool:
    return os.name == "nt" or sys.platform.startswith("linux")


def install() -> str:
    if os.name == "nt":
        return _win_install()
    if sys.platform == "darwin":
        return "On macOS drag ferry-gui.app into your Applications folder; it already has its icon."
    return _linux_install()


def uninstall() -> str:
    if os.name == "nt":
        return _win_uninstall()
    if sys.platform == "darwin":
        return "On macOS remove ferry-gui.app from Applications."
    return _linux_uninstall()


def status() -> tuple[bool, str | None]:
    if os.name == "nt":
        p = _win_lnk()
    elif sys.platform == "darwin":
        return False, None
    else:
        p = _linux_paths()[0]
        if p.exists() and "NoDisplay=true" in p.read_text(errors="replace"):
            return False, None               # only the hidden icon entry, not in the menu
    return p.exists(), (str(p) if p.exists() else None)
