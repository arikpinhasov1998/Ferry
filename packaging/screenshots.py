"""Regenerate the screenshots in docs/images (desktop app light/dark, dialogs, terminal app).

    python packaging/screenshots.py [DEMO_ROOT]

Creates demo folders under DEMO_ROOT (default: a temp folder) and runs a private background
service with a mock Telegram server, so nothing touches your real settings. Paths in the
pictures show DEMO_ROOT, so pick a short one (e.g. /mnt in a private mount namespace:
    unshare -rm sh -c 'mount --bind "$(mktemp -d)" /mnt && python packaging/screenshots.py /mnt'
Needs the "gui" extra.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))
os.environ["FERRY_HOME"] = tempfile.mkdtemp(prefix="ferry-shots-")
os.environ["QT_QPA_PLATFORM"] = "offscreen"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------ mock Telegram --
class _TG(BaseHTTPRequestHandler):
    served = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        method = self.path.rsplit("/", 1)[-1]
        p = json.loads(body) if body and self.headers.get("Content-Type", "").startswith("application/json") else {}
        result = True
        if method == "getMe":
            result = {"username": "office_ferry_bot", "first_name": "Office Ferry"}
        elif method == "getUpdates":
            result = []
            if not _TG.served and 0 < p.get("timeout", 0) < 10:
                _TG.served = True
                result = [{"update_id": 7, "message": {"text": "/start", "chat": {"id": 555, "type": "private"},
                                                       "from": {"id": 555, "first_name": "Alex", "username": "alex"}}}]
            else:
                time.sleep(min(p.get("timeout", 0), 1))
        data = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    demo = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ferry-demo-"))
    tg = ThreadingHTTPServer(("127.0.0.1", 0), _TG)
    threading.Thread(target=tg.serve_forever, daemon=True).start()
    import ferry.telegram as T
    orig = T.TelegramApi.__init__

    def init(self, token):
        orig(self, token)
        self.base = f"http://127.0.0.1:{tg.server_address[1]}/bot{token}/"
    T.TelegramApi.__init__ = init

    from ferry.api import Client
    from ferry.config import Config
    from ferry.daemon import Daemon
    cfg = Config()
    cfg.set("api.port", free_port())
    cfg.set("machine_name", "My-PC")
    d = Daemon(cfg)
    threading.Thread(target=d.run_forever, daemon=True).start()
    c = Client(cfg)
    while not c.alive():
        time.sleep(0.1)

    def tree(base: Path, n, size):
        for i in range(n):
            sub = base / f"Album {i % 5 + 1}"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / f"photo_{i:04}.jpg").write_bytes(os.urandom(size))
    data, backup = demo / "data", demo / "backup"
    tree(data / "Photos", 60, 20000)
    tree(data / "Work", 30, 5000)
    tree(data / "Videos", 40, 400000)
    c.post("/run", {"spec": {"source": str(data / "Photos"), "dest": str(backup / "Photos"), "verify": "xxhash"}})
    c.post("/jobs", {"name": "Nightly work backup", "source": str(data / "Work"), "dest": str(backup / "Work"),
                     "mode": "mirror", "schedule": "0 2 * * *"})
    c.post("/jobs", {"name": "Notes live sync", "source": str(data / "Work"), "dest": str(backup / "Notes"),
                     "mode": "mirror", "watch": True})
    time.sleep(3)
    (backup / "Work2").mkdir(parents=True, exist_ok=True)
    (backup / "Work2" / "old-report.docx").write_text("x")
    c.post("/run", {"spec": {"name": "Work mirror", "source": str(data / "Work"), "dest": str(backup / "Work2"),
                             "mode": "mirror", "delete_mode": "confirm"}})
    r3 = c.post("/run", {"spec": {"name": "Videos to NAS", "source": str(data / "Videos"),
                                  "dest": str(backup / "Videos"), "bwlimit": "1500k", "threads": 2}})
    time.sleep(4)

    from PySide6.QtWidgets import QApplication
    import ferry.gui as G
    app = QApplication([])
    app.setWindowIcon(G.app_icon())

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    G.apply_theme("light")
    w = G.MainWindow(c)
    w.resize(1180, 640)
    w.show()
    w.refresh()
    pump(0.5)
    w.t_runs.selectRow(0)
    pump(0.5)
    w.grab().save(str(OUT / "gui-activity.png"))
    G.apply_theme("dark")
    pump(0.6)
    w.grab().save(str(OUT / "gui-activity-dark.png"))
    w.tabs.setCurrentIndex(3)
    w.load_settings()
    pump(0.4)
    w.grab().save(str(OUT / "gui-settings-dark.png"))
    G.apply_theme("light")
    pump(0.4)
    for i, name in ((1, "gui-jobs"), (2, "gui-history"), (3, "gui-settings")):
        w.tabs.setCurrentIndex(i)
        w.refresh()
        w.load_settings()
        pump(0.4)
        w.grab().save(str(OUT / f"{name}.png"))
    dlg = G.JobDialog(w)
    dlg.name.setText("Photos backup")
    dlg.source.edit.setText(r"C:\Users\me\Pictures")
    dlg.dest.edit.setText(r"E:\Backup\Pictures")
    dlg.show()
    pump(0.4)
    dlg.grab().save(str(OUT / "gui-new-job.png"))
    dlg.close()
    wiz = G.TelegramSetupDialog(w, c, "My-PC")
    wiz.show()
    wiz.token.setText("123456:demo-token")
    wiz.check_token()
    pump(4)
    wiz.grab().save(str(OUT / "gui-telegram-setup.png"))
    wiz.reject()
    c.post(f"/runs/{r3['id']}/cancel")

    from ferry.tui import FerryTUI

    async def tui():
        t = FerryTUI(c)
        async with t.run_test(size=(120, 32)) as pilot:
            await pilot.pause(1.5)
            t.save_screenshot(filename="tui.svg", path=str(OUT))
            await pilot.press("t")
            await pilot.pause(0.5)
            t.save_screenshot(filename="tui-light.svg", path=str(OUT))
    asyncio.run(tui())
    c.post("/config", {"ui.theme": "system"})
    d.stop()
    print("screenshots written to", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
