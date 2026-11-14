"""Progress bars: indeterminate before copying starts, determinate while copying."""
import os

import pytest

from ferry.util import busy_bar, progress_mode


def info(phase, **kw):
    base = {"id": "20261114-120000-abcd", "job_name": "job", "mode": "copy", "source": "/a", "dest": "/b",
            "phase": phase, "percent": 0.0, "done_files": 0, "total_files": 0, "done_bytes": 0, "total_bytes": 0,
            "speed": 0, "elapsed": 1.0, "eta": None, "scanned": 1234, "same_files": 0, "failed": 0,
            "deleted": 0, "started": 1.0}
    base.update(kw)
    return base


def test_progress_mode_by_phase():
    assert progress_mode(info("pending")) == "busy"
    assert progress_mode(info("scanning")) == "busy"
    assert progress_mode(info("awaiting_confirmation")) == "waiting"
    for ph in ("copying", "deleting", "finishing", "done", "failed", "cancelled", "completed_with_errors"):
        assert progress_mode(info(ph)) == "percent"


def test_busy_bar_moves_and_keeps_width():
    frames = {busy_bar(20, t / 8) for t in range(40)}
    assert len(frames) > 5 and all(len(f) == 20 for f in frames)
    assert busy_bar(2, 0) == "██"


def test_cli_line_has_no_percent_before_copying():
    from ferry.cli import render_line
    line = render_line(info("scanning"), 120)
    assert "scanning…" in line and "1,234 found" in line and "%" not in line
    line = render_line(info("copying", percent=42.0, total_files=10, done_files=4), 120)
    assert "42.0%" in line


def test_telegram_has_no_percent_before_copying():
    from ferry.telegram import TelegramBot

    class FakeBot:
        machine = "pc"
        header = TelegramBot.header
    text = TelegramBot.render_run(FakeBot(), info("pending"))
    assert "Getting ready" in text and "%" not in text
    text = TelegramBot.render_run(FakeBot(), info("copying", percent=50.0))
    assert "50.0%" in text


def test_tui_rows():
    from ferry import tui
    rows = []

    class FakeTable:
        def set_rows(self, r):
            rows[:] = r

    class Fake:
        def query_one(self, *a):
            return FakeTable()
    tui.FerryTUI._render_runs(Fake(), [info("scanning"), info("copying", id="x", percent=10.0, total_files=5)])
    cells = {rid: cols for rid, cols in rows}
    assert "%" not in cells["20261114-120000-abcd"][2] and "found" in cells["20261114-120000-abcd"][3]
    assert "10.0%" in cells["x"][2]


def test_gui_bar_switches_from_indeterminate_to_determinate():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    import ferry.gui as G

    state = {"run": info("scanning")}

    class FakeClient:
        def get(self, path):
            if path == "/status":
                return {"machine": "pc", "active": [state["run"]], "jobs": 0, "telegram": False, "watching": []}
            if path == "/runs":
                return [state["run"]]
            if path == "/config":
                return {"machine_name": "pc", "telegram": {"enabled": False, "token": "", "allowed_ids": [],
                        "notify_chat_ids": [], "progress_every_percent": 25, "progress_every_minutes": 0,
                        "notify_start": True, "notify_finish": True, "notify_errors": True},
                        "defaults": {"threads": 8, "retries": 3}, "auto_resume": True, "ui": {"theme": "light"}}
            if path.startswith("/history"):
                return []
            return []

        def post(self, *a, **k):
            return {}

    w = G.MainWindow(FakeClient())
    w.refresh()
    bar = w.t_runs.cellWidget(0, 2)
    assert bar.maximum() == 0, "indeterminate while scanning"
    assert w.t_runs.item(0, 3).text() == "1,234 found" and w.t_runs.item(0, 5).text() == ""
    state["run"] = info("copying", percent=37.5, done_files=3, total_files=8, speed=1000)
    w.refresh()
    bar = w.t_runs.cellWidget(0, 2)
    assert bar.maximum() == 1000 and bar.value() == 375 and bar.text() == "37.5%"
    state["run"] = info("awaiting_confirmation")
    w.refresh()
    assert w.t_runs.cellWidget(0, 2).maximum() == 1000 and "OK" in w.t_runs.cellWidget(0, 2).text()
    w.timer.stop()
    w.close()
