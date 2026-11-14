"""Build portable single-file executables with PyInstaller.

    python packaging/build.py            # both: ferry (CLI/TUI/service) and ferry-gui
    python packaging/build.py cli        # only the small CLI build
    python packaging/build.py gui        # only the desktop build

Output goes to dist/. PyInstaller can't cross-compile: run this on each OS
(the GitHub Actions workflow does that for Windows, Linux and macOS).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

COMMON = [
    "--noconfirm", "--clean",
    "--collect-submodules", "textual",
    "--collect-data", "textual",
    "--collect-submodules", "watchdog",
    "--hidden-import", "xxhash",
    "--hidden-import", "paramiko",
]
if IS_WIN:
    COMMON += ["--hidden-import", "win32security", "--hidden-import", "win32api"]
if IS_MAC:
    COMMON += ["--hidden-import", "xattr"]


def run(args):
    print(">", " ".join(args))
    subprocess.run([sys.executable, "-m", "PyInstaller", *args], cwd=ROOT, check=True)


def entry(name: str, code: str) -> str:
    p = ROOT / "build" / f"{name}_entry.py"
    p.parent.mkdir(exist_ok=True)
    p.write_text(code)
    return str(p)


def build_cli():
    script = entry("ferry", "import sys\nfrom ferry.cli import main\nsys.exit(main())\n")
    run([*COMMON, "--onefile", "--console", "--name", "ferry",
         "--exclude-module", "PySide6", "--exclude-module", "tkinter", "--paths", str(ROOT), script])


def build_gui():
    script = entry("ferry_gui", "from ferry.gui import entry\nentry()\n")
    excludes = []
    for mod in ("QtWebEngineCore", "QtWebEngineWidgets", "QtQml", "QtQuick", "Qt3DCore", "QtMultimedia",
                "QtPdf", "QtCharts", "QtDataVisualization", "QtBluetooth", "QtNetwork", "QtSql", "QtTest"):
        excludes += ["--exclude-module", f"PySide6.{mod}"]
    args = [*COMMON, *excludes, "--windowed", "--name", "ferry-gui", "--paths", str(ROOT),
            "--exclude-module", "tkinter"]
    if IS_MAC:
        args += ["--onedir", "--osx-bundle-identifier", "com.ferry.gui"]   # .app bundle
    else:
        args += ["--onefile"]
    run([*args, script])


def _add(z: zipfile.ZipFile, path: Path, arcname: str):
    if path.is_symlink():   # keep symlinks (macOS .app frameworks rely on them)
        info = zipfile.ZipInfo(arcname)
        info.external_attr = (0o120777 << 16)
        z.writestr(info, os.readlink(path))
    elif path.is_file():
        info = zipfile.ZipInfo.from_file(path, arcname)
        info.external_attr = ((path.stat().st_mode | 0o644) & 0o777 | 0o100000) << 16
        with open(path, "rb") as fh:
            z.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)


def package():
    """Zip the outputs as dist/ferry-<version>-<os>-<arch>.zip."""
    sys.path.insert(0, str(ROOT))
    from ferry import __version__
    osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    arch = platform.machine().lower().replace("amd64", "x64").replace("x86_64", "x64").replace("aarch64", "arm64")
    out = DIST / f"ferry-{__version__}-{osname}-{arch}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(DIST.iterdir()):
            if p.suffix == ".zip":
                continue
            if p.is_dir():
                if p.suffix == ".app":
                    for f in sorted(p.rglob("*")):
                        _add(z, f, str(f.relative_to(DIST)))
            else:
                _add(z, p, p.name)
        z.writestr("README.txt", (ROOT / "packaging" / "PORTABLE.txt").read_text())
    print("packaged", out)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "cli"):
        build_cli()
    if what in ("all", "gui"):
        build_gui()
    if IS_MAC:
        shutil.rmtree(DIST / "ferry-gui", ignore_errors=True)   # keep only the .app
    package()
