"""Render ferry/assets/ferry.svg into every icon file the apps and builds need.

    python packaging/make_icons.py

Writes (all committed to the repo, so builds don't need to run this):
  ferry/assets/icon-<size>.png      window, tray and dialog icons (16 … 512 px)
  packaging/icons/ferry.ico         Windows .exe icon (16 … 256 px, PNG-compressed)
  packaging/icons/ferry.icns        macOS .app icon (16 … 1024 px)
  packaging/icons/ferry-1024.png    big PNG (store pages, Telegram bot picture, social preview)
  docs/images/logo.png              README logo
Needs PySide6 (the "gui" extra). Pillow is not required.
"""
from __future__ import annotations

import io
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPainterPath  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "ferry" / "assets" / "ferry.svg"
SVG_SMALL = ROOT / "ferry" / "assets" / "ferry-small.svg"   # bolder drawing for tiny sizes
SMALL_MAX = 32
# the rounded tile of each drawing (x, y, size, radius in the 1024 grid); QtSvg ignores <clipPath>
TILES = {SVG: (64, 64, 896, 200), SVG_SMALL: (32, 32, 960, 200)}
APP_SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
# ICNS chunk types that hold PNG data, by pixel size
ICNS_TYPES = [(b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128), (b"ic08", 256), (b"ic09", 512),
              (b"ic10", 1024), (b"ic11", 32), (b"ic12", 64), (b"ic13", 256), (b"ic14", 512)]


def render(renderer: QSvgRenderer, size: int, tile) -> bytes:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    k = size / 1024
    x, y, w, r = (v * k for v in tile)
    clip = QPainterPath()
    clip.addRoundedRect(x, y, w, w, r, r)
    p.setClipPath(clip)
    renderer.render(p)
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def write_ico(path: Path, pngs: dict[int, bytes]):
    sizes = [s for s in ICO_SIZES if s in pngs]
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        data = pngs[s]
        out.write(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for s in sizes:
        out.write(pngs[s])
    path.write_bytes(out.getvalue())


def write_icns(path: Path, pngs: dict[int, bytes]):
    chunks = b"".join(t + struct.pack(">I", 8 + len(pngs[s])) + pngs[s] for t, s in ICNS_TYPES)
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(chunks)) + chunks)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 (needed for painting)
    renderers = {}
    for svg in (SVG, SVG_SMALL):
        renderers[svg] = QSvgRenderer(str(svg))
        if not renderers[svg].isValid():
            print(f"cannot read {svg}", file=sys.stderr)
            return 1
    pngs = {}
    for s in sorted(set(APP_SIZES + ICO_SIZES + [1024])):
        svg = SVG_SMALL if s <= SMALL_MAX else SVG
        pngs[s] = render(renderers[svg], s, TILES[svg])
    assets = ROOT / "ferry" / "assets"
    for s in APP_SIZES:
        (assets / f"icon-{s}.png").write_bytes(pngs[s])
    icons = ROOT / "packaging" / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    write_ico(icons / "ferry.ico", pngs)
    write_ico(assets / "ferry.ico", pngs)          # Start-menu shortcut icon when running from source
    write_icns(icons / "ferry.icns", pngs)
    (icons / "ferry-1024.png").write_bytes(pngs[1024])
    (ROOT / "docs" / "images" / "logo.png").write_bytes(pngs[256])
    print("icons written:", ", ".join(str(p.relative_to(ROOT)) for p in
                                      [assets, icons / "ferry.ico", icons / "ferry.icns", icons / "ferry-1024.png"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
