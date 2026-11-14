import os
import struct
import sys
from pathlib import Path

import pytest

from ferry import shortcut

ROOT = Path(__file__).resolve().parent.parent


def test_icon_files_exist_and_are_valid():
    for s in shortcut.ICON_SIZES:
        data = shortcut.icon_path(s).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", data[16:24])
        assert (w, h) == (s, s)
    ico = (ROOT / "packaging" / "icons" / "ferry.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00" and struct.unpack("<H", ico[4:6])[0] == 7
    assert (shortcut.ASSETS / "ferry.ico").read_bytes() == ico
    icns = (ROOT / "packaging" / "icons" / "ferry.icns").read_bytes()
    assert icns[:4] == b"icns" and struct.unpack(">I", icns[4:8])[0] == len(icns)


def test_assets_are_packaged():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'ferry = ["assets/*"]' in text


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux app menu")
def test_linux_menu_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert shortcut.status() == (False, None)
    shortcut.install()
    desktop = tmp_path / "applications" / "ferry.desktop"
    text = desktop.read_text()
    assert "Icon=ferry" in text and "Exec=" in text and "-m ferry gui" in text
    assert (tmp_path / "icons" / "hicolor" / "256x256" / "apps" / "ferry.png").exists()
    assert (tmp_path / "icons" / "hicolor" / "scalable" / "apps" / "ferry.svg").exists()
    assert shortcut.status()[0]
    shortcut.uninstall()
    assert not desktop.exists() and shortcut.status() == (False, None)


@pytest.mark.skipif(os.name != "nt", reason="Windows Start menu")
def test_windows_start_menu_shortcut(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    shortcut.install()
    lnk = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Ferry.lnk"
    assert lnk.exists()
    import win32com.client
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(lnk))
    assert "ferry" in sc.Arguments and sc.IconLocation.lower().startswith(str(shortcut.ASSETS / "ferry.ico").lower())
    shortcut.uninstall()
    assert not lnk.exists()


def test_gui_icon_has_all_sizes():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from ferry.gui import app_icon
    icon = app_icon()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert {(16, 16), (32, 32), (256, 256), (512, 512)} <= sizes
