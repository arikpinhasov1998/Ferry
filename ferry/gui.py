"""Desktop window (Qt) with tray icon. Talks to the background service."""
from __future__ import annotations

import sys
import threading
from datetime import datetime

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPalette,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea,
                               QSpinBox, QSplitter, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QToolBar, QVBoxLayout, QWidget)

from ferry import __version__
from ferry.spec import full_location
from ferry.util import human_bytes, human_duration, progress_mode

FINAL = {"done", "completed_with_errors", "failed", "cancelled", "interrupted"}
PHASE = {"pending": "Starting", "scanning": "Scanning", "awaiting_confirmation": "Needs confirmation",
         "copying": "Copying", "deleting": "Cleaning up", "finishing": "Finishing", "done": "Done",
         "completed_with_errors": "Done with errors", "failed": "Failed", "cancelled": "Cancelled",
         "interrupted": "Interrupted"}
PHASE_ROLE = {"done": "ok", "completed_with_errors": "warn", "failed": "err",
              "awaiting_confirmation": "warn", "cancelled": "muted"}

# ------------------------------------------------------------------ themes --
THEMES = ("system", "light", "dark")
THEME_LABELS = {"system": "Match system", "light": "Light", "dark": "Dark"}
# status colours readable on each background
ROLE_COLORS = {
    "light": {"ok": "#2e7d32", "warn": "#e65100", "err": "#c62828", "muted": "#757575", "accent": "#1565c0"},
    "dark": {"ok": "#81c784", "warn": "#ffb74d", "err": "#ef9a9a", "muted": "#9e9e9e", "accent": "#64b5f6"},
}
_theme = {"mode": "system", "effective": "light"}


def _system_is_dark() -> bool:
    app = QGuiApplication.instance()
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:          # Qt < 6.5: guess from the default palette
        return app.palette().color(QPalette.Window).lightness() < 128


def _palette(dark: bool) -> QPalette:
    p = QPalette()
    if dark:
        c = {"window": "#1f2023", "base": "#2b2d31", "alt": "#303237", "text": "#e8eaed", "button": "#383a40",
             "mid": "#55585f", "dis": "#8a8f96", "hl": "#1a73e8", "link": "#8ab4f8", "tip": "#383a40"}
    else:
        c = {"window": "#f3f4f6", "base": "#ffffff", "alt": "#f7f8fa", "text": "#1f2328", "button": "#fafbfc",
             "mid": "#c9ccd1", "dis": "#9aa0a6", "hl": "#1a73e8", "link": "#1565c0", "tip": "#ffffff"}
    Q = QColor
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        dis = group == QPalette.Disabled
        txt = Q(c["dis"] if dis else c["text"])
        p.setColor(group, QPalette.Window, Q(c["window"]))
        p.setColor(group, QPalette.WindowText, txt)
        p.setColor(group, QPalette.Base, Q(c["base"]))
        p.setColor(group, QPalette.AlternateBase, Q(c["alt"]))
        p.setColor(group, QPalette.Text, txt)
        p.setColor(group, QPalette.Button, Q(c["button"]))
        p.setColor(group, QPalette.ButtonText, txt)
        p.setColor(group, QPalette.ToolTipBase, Q(c["tip"]))
        p.setColor(group, QPalette.ToolTipText, Q(c["text"]))
        p.setColor(group, QPalette.PlaceholderText, Q(c["dis"]))
        p.setColor(group, QPalette.Highlight, Q(c["mid"] if dis else c["hl"]))
        p.setColor(group, QPalette.HighlightedText, Q("#ffffff"))
        p.setColor(group, QPalette.Link, Q(c["link"]))
        p.setColor(group, QPalette.Light, Q(c["button"]).lighter(130))
        p.setColor(group, QPalette.Midlight, Q(c["mid"]).lighter(115))
        p.setColor(group, QPalette.Mid, Q(c["mid"]))
        p.setColor(group, QPalette.Dark, Q(c["mid"]).darker(150))
        p.setColor(group, QPalette.Shadow, Q("#000000"))
    return p


def apply_theme(mode: str = None) -> str:
    """Apply 'system', 'light' or 'dark'. Returns the effective look ('light'/'dark')."""
    app = QApplication.instance()
    mode = mode if mode in THEMES else _theme["mode"]
    _theme["mode"] = mode
    dark = _system_is_dark() if mode == "system" else mode == "dark"
    _theme["effective"] = "dark" if dark else "light"
    app.setStyle("Fusion")              # the same look, and proper dark mode, on every OS
    app.setPalette(_palette(dark))
    for w in app.topLevelWidgets():
        if hasattr(w, "on_theme_changed"):
            w.on_theme_changed()
    return _theme["effective"]


def color(role: str) -> str:
    return ROLE_COLORS[_theme["effective"]][role]


def phase_color(phase: str) -> str:
    return color(PHASE_ROLE.get(phase, "accent"))


def app_icon() -> QIcon:
    """The Ferry icon in every size (bold drawing for 16-32 px, detailed one above)."""
    from ferry.shortcut import ICON_SIZES, icon_path
    icon = QIcon()
    for size in ICON_SIZES:
        p = icon_path(size)
        if p.exists():
            icon.addFile(str(p), QSize(size, size))
    return icon


def _phase(r):
    if r.get("paused") and r["phase"] not in FINAL:
        return "Paused"
    if r.get("waiting_window") and r["phase"] not in FINAL:
        return "Waiting for time window"
    return PHASE.get(r["phase"], r["phase"])


class PathEdit(QWidget):
    def __init__(self, value="", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(value)
        self.edit.setPlaceholderText(r"C:\folder, /home/me/x, \\server\share, sftp://user@host/path, remote:path")
        btn = QPushButton("Browse…")
        btn.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", self.edit.text() or "")
        if d:
            self.edit.setText(d)

    def text(self):
        return self.edit.text().strip()


class JobDialog(QDialog):
    def __init__(self, parent, job: dict | None = None):
        super().__init__(parent)
        self.job = dict(job or {})
        self.result_action = None
        self.setWindowTitle("Edit job" if job else "New copy / job")
        self.setMinimumWidth(720)
        j = self.job
        f = j.get("filters") or {}
        m = j.get("metadata") or {}
        lay = QVBoxLayout(self)

        basic = QGroupBox("What")
        fl = QFormLayout(basic)
        self.name = QLineEdit(j.get("name", ""))
        self.name.setPlaceholderText("needed to save as a job")
        self.source = PathEdit(j.get("source", ""))
        self.dest = PathEdit(j.get("dest", ""))
        self.mode = self._combo([("Copy new and changed files", "copy"), ("Mirror (make identical)", "mirror"),
                                 ("Move (delete from source after copy)", "move")], j.get("mode", "copy"))
        self.delete_mode = self._combo([("Keep old versions in .ferry-archive", "archive"),
                                        ("Ask me before deleting/overwriting", "confirm"),
                                        ("Delete directly (like robocopy /MIR)", "delete")],
                                       j.get("delete_mode", "archive"))
        self.overwrite = self._combo([("When different (size/time)", "different"), ("Only when newer", "newer"),
                                      ("When content differs (checksum)", "checksum"), ("Always", "always"),
                                      ("Never (only new files)", "never")], j.get("overwrite", "different"))
        fl.addRow("Name", self.name)
        fl.addRow("Source", self.source)
        fl.addRow("Destination", self.dest)
        fl.addRow("Mode", self.mode)
        fl.addRow("Replaced / extra files", self.delete_mode)
        fl.addRow("Overwrite", self.overwrite)
        lay.addWidget(basic)

        tabs = QTabWidget()
        # performance & schedule
        w = QWidget()
        fl = QFormLayout(w)
        self.verify = self._combo([("No", "none"), ("xxHash (fast)", "xxhash"), ("SHA-256", "sha256")],
                                  j.get("verify", "none"))
        self.threads = QSpinBox()
        self.threads.setRange(1, 128)
        self.threads.setValue(int(j.get("threads") or 8))
        self.retries = QSpinBox()
        self.retries.setRange(0, 1000)
        self.retries.setValue(int(j.get("retries") if j.get("retries") is not None else 3))
        self.bwlimit = QLineEdit(j.get("bwlimit") or "")
        self.bwlimit.setPlaceholderText("e.g. 20M = 20 MB/s; empty = unlimited")
        self.bw_schedule = QLineEdit("; ".join(f"{i['window']}={i['limit']}" for i in j.get("bw_schedule", [])))
        self.bw_schedule.setPlaceholderText("e.g. mon-fri 09:00-18:00=5M; sat 10:00-12:00=1M")
        self.windows = QLineEdit(", ".join(j.get("windows", [])))
        self.windows.setPlaceholderText("only transfer during, e.g. 22:00-06:00")
        self.schedule = QLineEdit(j.get("schedule") or "")
        self.schedule.setPlaceholderText("cron, e.g. 0 2 * * *  (daily 02:00), @hourly, */30 * * * *")
        self.watch = QCheckBox("Live sync: copy changes as soon as they happen")
        self.watch.setChecked(bool(j.get("watch")))
        self.notify = QCheckBox("Telegram notifications for this job")
        self.notify.setChecked(j.get("notify", True))
        fl.addRow("Verify copies", self.verify)
        fl.addRow("Parallel transfers", self.threads)
        fl.addRow("Retries per file", self.retries)
        fl.addRow("Bandwidth limit", self.bwlimit)
        fl.addRow("Limit by time", self.bw_schedule)
        fl.addRow("Run only during", self.windows)
        fl.addRow("Schedule", self.schedule)
        fl.addRow("", self.watch)
        fl.addRow("", self.notify)
        tabs.addTab(w, "Speed && schedule")
        # filters
        w = QWidget()
        fl = QFormLayout(w)
        self.include = QLineEdit(", ".join(f.get("include", [])))
        self.include.setPlaceholderText("only these, e.g. *.jpg, *.png")
        self.exclude = QLineEdit(", ".join(f.get("exclude", [])))
        self.exclude.setPlaceholderText("e.g. *.tmp, ~$*, Thumbs.db")
        self.exclude_dirs = QLineEdit(", ".join(f.get("exclude_dirs", [])))
        self.exclude_dirs.setPlaceholderText("e.g. node_modules, .git, /cache")
        self.min_size = QLineEdit(str(f.get("min_size") or ""))
        self.max_size = QLineEdit(str(f.get("max_size") or ""))
        self.max_age = QLineEdit(str(f.get("max_age") or ""))
        self.max_age.setPlaceholderText("e.g. 30d")
        fl.addRow("Include files", self.include)
        fl.addRow("Exclude files", self.exclude)
        fl.addRow("Exclude folders", self.exclude_dirs)
        fl.addRow("Min size", self.min_size)
        fl.addRow("Max size", self.max_size)
        fl.addRow("Max age", self.max_age)
        fl.addRow(QLabel("Tip: a .ferryignore file in the source works like .gitignore"))
        tabs.addTab(w, "Filters")
        # metadata
        w = QWidget()
        fl = QFormLayout(w)
        self.m_times = QCheckBox("Timestamps")
        self.m_times.setChecked(m.get("timestamps", True))
        self.m_perms = QCheckBox("Permissions / attributes")
        self.m_perms.setChecked(m.get("permissions", True))
        self.m_acl = QCheckBox("NTFS permissions (ACL)")
        self.m_acl.setChecked(m.get("acl", True))
        self.m_owner = QCheckBox("Owner (needs admin/root)")
        self.m_owner.setChecked(m.get("owner", False))
        self.m_xattrs = QCheckBox("Extended attributes")
        self.m_xattrs.setChecked(m.get("xattrs", True))
        self.symlinks = self._combo([("Copy links as links", "copy"), ("Copy what they point to", "follow"),
                                     ("Skip links", "skip")], m.get("symlinks", "copy"))
        for cb in (self.m_times, self.m_perms, self.m_acl, self.m_owner, self.m_xattrs):
            fl.addRow("", cb)
        fl.addRow("Symbolic links", self.symlinks)
        tabs.addTab(w, "Permissions")
        lay.addWidget(tabs)

        self.dry_run = QCheckBox("Dry run (only show what would happen)")
        lay.addWidget(self.dry_run)
        bb = QDialogButtonBox()
        self.btn_run = bb.addButton("Run now", QDialogButtonBox.AcceptRole)
        self.btn_save = bb.addButton("Save job", QDialogButtonBox.ApplyRole)
        bb.addButton(QDialogButtonBox.Cancel)
        self.btn_run.clicked.connect(lambda: self._finish("run"))
        self.btn_save.clicked.connect(lambda: self._finish("save"))
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    @staticmethod
    def _combo(items, value):
        c = QComboBox()
        for label, v in items:
            c.addItem(label, v)
        idx = c.findData(value)
        c.setCurrentIndex(max(0, idx))
        return c

    @staticmethod
    def _list(edit):
        return [x.strip() for x in edit.text().replace(";", ",").split(",") if x.strip()]

    def spec(self) -> dict:
        d = dict(self.job)
        d.update({
            "name": self.name.text().strip(), "source": full_location(self.source.text()),
            "dest": full_location(self.dest.text()),
            "mode": self.mode.currentData(), "delete_mode": self.delete_mode.currentData(),
            "overwrite": self.overwrite.currentData(), "verify": self.verify.currentData(),
            "threads": self.threads.value(), "retries": self.retries.value(),
            "bwlimit": self.bwlimit.text().strip() or None,
            "windows": self._list(self.windows), "schedule": self.schedule.text().strip() or None,
            "watch": self.watch.isChecked(), "notify": self.notify.isChecked(),
        })
        bws = []
        for part in self.bw_schedule.text().split(";"):
            if part.strip():
                w, _, lim = part.strip().rpartition("=")
                bws.append({"window": w.strip(), "limit": lim.strip()})
        d["bw_schedule"] = bws
        flt = dict(d.get("filters") or {})
        flt.update({"include": self._list(self.include), "exclude": self._list(self.exclude),
                    "exclude_dirs": self._list(self.exclude_dirs),
                    "min_size": self.min_size.text().strip() or None, "max_size": self.max_size.text().strip() or None,
                    "max_age": self.max_age.text().strip() or None})
        d["filters"] = flt
        d["metadata"] = {"timestamps": self.m_times.isChecked(), "permissions": self.m_perms.isChecked(),
                         "acl": self.m_acl.isChecked(), "owner": self.m_owner.isChecked(),
                         "xattrs": self.m_xattrs.isChecked(), "symlinks": self.symlinks.currentData()}
        if not d["name"]:
            d.pop("name")
        return d

    def _finish(self, action):
        d = self.spec()
        if not d.get("source") or not d.get("dest"):
            return QMessageBox.warning(self, "Ferry", "Source and destination are required.")
        if action == "save" and not d.get("name"):
            return QMessageBox.warning(self, "Ferry", "Give the job a name to save it.")
        try:
            from ferry.spec import JobSpec
            JobSpec.from_dict(d).validate()
        except Exception as e:
            return QMessageBox.warning(self, "Ferry", f"Please check the settings:\n{e}")
        if action == "run":
            d["dry_run"] = self.dry_run.isChecked()
        self.result_action = action
        self.result_spec = d
        self.accept()


class _Bridge(QObject):
    """Carries results from worker threads to the Qt thread."""
    event = Signal(str, object)


class TelegramSetupDialog(QDialog):
    """Guided setup: check the token, then catch /start messages to learn who is allowed."""

    def __init__(self, parent, client, machine_name=""):
        super().__init__(parent)
        self.client = client
        self.setWindowTitle("Connect a Telegram bot")
        self.setMinimumWidth(560)
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event)
        self.stop = threading.Event()
        self.poller = None
        self.offset = None
        self.api = None
        self.bot = None
        self.found = {}          # chat id -> QCheckBox
        self.was_enabled = None

        lay = QVBoxLayout(self)
        step1 = QLabel('<b>1.</b> In Telegram, open <a href="https://t.me/BotFather">@BotFather</a>, send '
                       '<code>/newbot</code>, pick a name and copy the token it gives you.<br>'
                       '<small>Use one bot per machine. To control several machines from one place, put their '
                       'bots in one Telegram group.</small>')
        step1.setOpenExternalLinks(True)
        step1.setWordWrap(True)
        lay.addWidget(step1)
        form = QFormLayout()
        row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("123456789:AA…")
        self.show_tok = QCheckBox("Show")
        self.show_tok.toggled.connect(lambda on: self.token.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        self.check_btn = QPushButton("Check token")
        self.check_btn.clicked.connect(self.check_token)
        self.token.returnPressed.connect(self.check_token)
        row.addWidget(self.token, 1)
        row.addWidget(self.show_tok)
        row.addWidget(self.check_btn)
        form.addRow("Bot token", row)
        self.machine = QLineEdit(machine_name)
        self.machine.setPlaceholderText("shown in every message, e.g. Office-PC")
        form.addRow("Machine name", self.machine)
        lay.addLayout(form)
        self.token_status = QLabel()
        self.token_status.setWordWrap(True)
        lay.addWidget(self.token_status)

        self.step2 = QGroupBox("2. Say hello to the bot")
        v = QVBoxLayout(self.step2)
        self.step2_text = QLabel()
        self.step2_text.setOpenExternalLinks(True)
        self.step2_text.setWordWrap(True)
        v.addWidget(self.step2_text)
        self.people = QVBoxLayout()
        v.addLayout(self.people)
        self.waiting = QLabel("<i>Waiting for messages…</i>")
        v.addWidget(self.waiting)
        self.step2.setEnabled(False)
        lay.addWidget(self.step2)

        pic = QHBoxLayout()
        tip = QLabel("<small>Optional: give the bot Ferry's picture — save it, then send <code>/setuserpic</code> "
                     "to @BotFather and send the picture.</small>")
        tip.setWordWrap(True)
        save_pic = QPushButton("Save bot picture…")
        save_pic.clicked.connect(self.save_picture)
        pic.addWidget(tip, 1)
        pic.addWidget(save_pic)
        lay.addLayout(pic)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.finish_btn = self.buttons.addButton("Finish and connect", QDialogButtonBox.AcceptRole)
        self.finish_btn.setEnabled(False)
        self.buttons.accepted.connect(self.finish)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def save_picture(self):
        import shutil
        from pathlib import Path

        from ferry.shortcut import icon_path
        path, _ = QFileDialog.getSaveFileName(self, "Save bot picture", str(Path.home() / "ferry-bot.png"),
                                              "PNG image (*.png)")
        if path:
            shutil.copyfile(icon_path(512), path)

    # --------------------------------------------------------- workers --
    def _bg(self, kind, fn):
        def run():
            try:
                self.bridge.event.emit(kind, fn())
            except Exception as e:
                self.bridge.event.emit(kind + "_error", e)
        threading.Thread(target=run, daemon=True).start()

    def check_token(self):
        from ferry.telegram import TelegramApi
        tok = self.token.text().strip()
        if ":" not in tok:
            self.token_status.setText(f"<span style='color:{color('err')}'>That doesn't look like a bot token "
                                      "(it has the form 123456789:AA…).</span>")
            return
        self._stop_polling()
        self.check_btn.setEnabled(False)
        self.token_status.setText("Checking…")
        self.api = TelegramApi(tok)
        self._bg("me", lambda: self.api.call("getMe"))

    def _start_polling(self):
        # The service's own bot would compete for messages: pause it while we listen.
        if self.was_enabled is None:
            try:
                cfg = self.client.get("/config")
                self.was_enabled = bool(cfg["telegram"]["enabled"])
                if self.was_enabled:
                    self.client.post("/config", {"telegram.enabled": False})
            except Exception:
                self.was_enabled = False
        self.stop.clear()
        api, stop = self.api, self.stop

        def loop():
            try:
                api.call("deleteWebhook")
            except Exception:
                pass
            while not stop.is_set():
                try:
                    ups = api.call("getUpdates", _timeout=15, timeout=3, offset=self.offset)
                except Exception as e:
                    self.bridge.event.emit("poll_error", e)
                    stop.wait(3)
                    continue
                for u in ups:
                    self.offset = u["update_id"] + 1
                    m = u.get("message") or u.get("my_chat_member")
                    if m:
                        self.bridge.event.emit("msg", m)
        self.poller = threading.Thread(target=loop, daemon=True)
        self.poller.start()

    def _stop_polling(self):
        self.stop.set()
        if self.poller:
            self.poller.join(6)
            self.poller = None

    # ---------------------------------------------------------- events --
    def _on_event(self, kind, data):
        if kind == "me":
            self.bot = data.get("username")
            self.check_btn.setEnabled(True)
            self.token_status.setText(f"<span style='color:{color('ok')}'>✓ Token works: bot "
                                      f"<b>@{esc_html(self.bot)}</b> ({esc_html(data.get('first_name', ''))})</span>")
            self.step2_text.setText(
                f'Open <a href="https://t.me/{self.bot}">t.me/{self.bot}</a> on your phone or computer and press '
                f'<b>Start</b> (or send <code>/start</code>). Everyone who should control this machine can do '
                f'the same. To use a group, add the bot to the group and send <code>/start@{self.bot}</code> there.')
            self.step2.setEnabled(True)
            self.finish_btn.setEnabled(True)
            self._start_polling()
        elif kind == "me_error":
            self.check_btn.setEnabled(True)
            msg = ("Telegram rejected this token. Copy it again from @BotFather."
                   if getattr(data, "code", None) == 401 else f"Could not reach Telegram: {data}")
            self.token_status.setText(f"<span style='color:{color('err')}'>✗ {esc_html(msg)}</span>")
        elif kind == "poll_error":
            code = getattr(data, "code", None)
            self.waiting.setText(f"<i style='color:{color('warn')}'>" + esc_html(
                "Another program is using this bot (each machine needs its own bot)" if code == 409
                else f"Telegram not reachable, retrying… ({data})") + "</i>")
        elif kind == "msg":
            chat = data.get("chat") or {}
            cid = chat.get("id")
            if cid is None or cid in self.found:
                return
            if chat.get("type") == "private":
                u = data.get("from") or {}
                name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
                label = f"👤 {name}" + (f" (@{u['username']})" if u.get("username") else "") + f" — ID {cid}"
            else:
                label = f"👥 Group “{chat.get('title', '')}” — ID {cid} (everyone in it can control this machine)"
            cb = QCheckBox("Allow " + label)
            cb.setChecked(chat.get("type") == "private")
            self.found[cid] = cb
            self.people.addWidget(cb)
            self.waiting.setText("<i>Got it. Waiting for more people… or press Finish.</i>")
        QTimer.singleShot(0, self.adjustSize)     # grow to fit new text

    # ---------------------------------------------------------- finish --
    def finish(self):
        allowed = [cid for cid, cb in self.found.items() if cb.isChecked()]
        if not allowed and QMessageBox.question(
                self, "Telegram", "Nobody is allowed yet, so the bot will ignore everyone.\n"
                "You can add IDs later (send /whoami to the bot). Continue?") != QMessageBox.Yes:
            return
        self._stop_polling()
        try:
            if self.offset is not None:          # mark the /start messages as handled
                self.api.call("getUpdates", offset=self.offset, timeout=0)
        except Exception:
            pass
        try:
            cfg = self.client.get("/config")
            ids = sorted(set(cfg["telegram"]["allowed_ids"]) | set(allowed))
            groups = [c for c in allowed if c < 0]
            body = {"telegram.token": self.token.text().strip(), "telegram.allowed_ids": ids}
            if groups:
                body["telegram.notify_chat_ids"] = sorted(set(cfg["telegram"]["notify_chat_ids"] or []) | set(groups))
            if self.machine.text().strip():
                body["machine_name"] = self.machine.text().strip()
            body["telegram.enabled"] = True
            self.client.post("/config", body)
            self.was_enabled = None
            res = self.client.post("/telegram/test")
        except Exception as e:
            return QMessageBox.warning(self, "Telegram", f"Could not save: {e}")
        if res.get("ok"):
            QMessageBox.information(self, "Telegram", f"Connected! @{self.bot} sent a test message to "
                                    f"{len(res.get('sent_to') or [])} chat(s). Try /status in Telegram.")
        else:
            QMessageBox.warning(self, "Telegram", f"Saved, but the test message failed: {res.get('error')}")
        self.accept()

    def reject(self):
        self._stop_polling()
        if self.was_enabled:
            try:
                self.client.post("/config", {"telegram.enabled": True})
            except Exception:
                pass
        super().reject()


def esc_html(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class MainWindow(QMainWindow):
    def __init__(self, client):
        super().__init__()
        self.client = client
        self.runs: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.known_phase: dict[str, str] = {}
        self.setWindowTitle(f"Ferry {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1180, 720)

        tb = QToolBar("Main")
        tb.setIconSize(QSize(16, 16))
        tb.setMovable(False)
        self.addToolBar(tb)
        self.act_new = tb.addAction("➕ New copy", self.new_job)
        tb.addSeparator()
        self.act_pause = tb.addAction("⏸ Pause", lambda: self.run_action("pause"))
        self.act_resume = tb.addAction("▶ Resume", lambda: self.run_action("resume"))
        self.act_cancel = tb.addAction("⏹ Cancel", lambda: self.run_action("cancel"))
        self.act_confirm = tb.addAction("✔ Confirm…", self.confirm_run)
        tb.addSeparator()
        self.act_log = tb.addAction("📄 Log", self.show_log)
        self.act_errors = tb.addAction("❌ Errors", self.show_errors)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_activity()
        self._build_jobs()
        self._build_history()
        self._build_settings()
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)

        self._build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()
        self.load_settings()

    # ---------------------------------------------------------- building --
    def _table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        t.horizontalHeader().setStretchLastSection(True)
        return t

    def _build_activity(self):
        w = QSplitter(Qt.Vertical)
        self.t_runs = self._table(["Job", "State", "Progress", "Files", "Data", "Speed", "Elapsed", "Left", "Run"])
        for i, width in enumerate((170, 150, 200, 120, 170, 100, 90, 90)):
            self.t_runs.setColumnWidth(i, width)
        self.t_runs.itemSelectionChanged.connect(self.update_details)
        self.details = QTextBrowser()
        w.addWidget(self.t_runs)
        w.addWidget(self.details)
        w.setSizes([420, 220])
        self.tabs.addTab(w, "Activity")

    def _build_jobs(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        for label, fn in (("➕ New job", self.new_job), ("✏ Edit", self.edit_job), ("▶ Run", self.run_job),
                          ("🧪 Dry run", lambda: self.run_job(dry=True)), ("🗑 Delete", self.delete_job)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.t_jobs = self._table(["Name", "Mode", "Source", "Destination", "Schedule", "Live", "Enabled"])
        for i, width in enumerate((160, 70, 300, 300, 120, 50)):
            self.t_jobs.setColumnWidth(i, width)
        self.t_jobs.doubleClicked.connect(self.edit_job)
        lay.addWidget(self.t_jobs)
        self.tabs.addTab(w, "Jobs")

    def _build_history(self):
        self.t_hist = self._table(["When", "Job", "Result", "Files", "Data", "Time", "Failed", "Run"])
        for i, width in enumerate((140, 170, 150, 90, 100, 90, 70)):
            self.t_hist.setColumnWidth(i, width)
        self.tabs.addTab(self.t_hist, "History")

    def _build_settings(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("Appearance")
        hl = QHBoxLayout(g)
        hl.addWidget(QLabel("Theme"))
        self.s_theme = QComboBox()
        for t in THEMES:
            self.s_theme.addItem(THEME_LABELS[t], t)
        self.s_theme.setToolTip("“Match system” follows your computer's light/dark setting, also when it changes.")
        self.s_theme.currentIndexChanged.connect(self.change_theme)
        hl.addWidget(self.s_theme)
        hl.addStretch(1)
        lay.addWidget(g)
        g = QGroupBox("Telegram bot")
        fl = QFormLayout(g)
        top = QHBoxLayout()
        self.s_tg_state = QLabel("…")
        self.s_tg_state.setOpenExternalLinks(True)
        self.s_tg_state.setWordWrap(True)
        self.s_tg_setup = QPushButton("Set up bot…")
        self.s_tg_setup.clicked.connect(self.setup_telegram)
        t = QPushButton("Send test message")
        t.clicked.connect(self.test_telegram)
        self.s_tg_disconnect = QPushButton("Disconnect")
        self.s_tg_disconnect.clicked.connect(self.disconnect_telegram)
        top.addWidget(self.s_tg_state, 1)
        top.addWidget(self.s_tg_setup)
        top.addWidget(t)
        top.addWidget(self.s_tg_disconnect)
        fl.addRow(top)
        self.s_machine = QLineEdit()
        self.s_tg_enabled = QCheckBox("Bot enabled")
        self.s_tg_token = QLineEdit()
        self.s_tg_token.setEchoMode(QLineEdit.Password)
        self.s_tg_token.setPlaceholderText("leave empty to keep the current token")
        self.s_tg_token.setToolTip("Normally set by “Set up bot…”. Paste a new token here to replace it.")
        self.s_tg_ids = QLineEdit()
        self.s_tg_ids.setPlaceholderText("your Telegram user ID (send /whoami to the bot), comma separated")
        self.s_tg_chats = QLineEdit()
        self.s_tg_chats.setPlaceholderText("notification chats (empty = the allowed IDs); a group ID works too")
        self.s_tg_pct = QSpinBox()
        self.s_tg_pct.setRange(0, 100)
        self.s_tg_pct.setSuffix(" %")
        self.s_tg_min = QSpinBox()
        self.s_tg_min.setRange(0, 1440)
        self.s_tg_min.setSuffix(" min")
        self.s_tg_pct.setSpecialValueText("off")
        self.s_tg_min.setSpecialValueText("off")
        self.s_tg_start = QCheckBox("Notify when jobs start")
        self.s_tg_finish = QCheckBox("Notify when jobs finish")
        self.s_tg_errors = QCheckBox("Notify on errors")
        fl.addRow("Machine name", self.s_machine)
        fl.addRow("", self.s_tg_enabled)
        fl.addRow("Bot token", self.s_tg_token)
        fl.addRow("Allowed users/groups", self.s_tg_ids)
        fl.addRow("Notify chats", self.s_tg_chats)
        fl.addRow("Progress message every", self.s_tg_pct)
        fl.addRow("…and/or every", self.s_tg_min)
        for cb in (self.s_tg_start, self.s_tg_finish, self.s_tg_errors):
            fl.addRow("", cb)
        lay.addWidget(g)

        g = QGroupBox("Defaults")
        fl = QFormLayout(g)
        self.s_threads = QSpinBox()
        self.s_threads.setRange(1, 128)
        self.s_retries = QSpinBox()
        self.s_retries.setRange(0, 1000)
        self.s_resume = QCheckBox("Resume interrupted runs when the service starts")
        fl.addRow("Parallel transfers", self.s_threads)
        fl.addRow("Retries per file", self.s_retries)
        fl.addRow("", self.s_resume)
        lay.addWidget(g)

        g = QGroupBox("Background service (starts at login, no admin rights)")
        hl = QHBoxLayout(g)
        self.s_service = QLabel()
        bi = QPushButton("Install / start at login")
        bi.clicked.connect(lambda: self.service("install"))
        bu = QPushButton("Remove")
        bu.clicked.connect(lambda: self.service("uninstall"))
        hl.addWidget(self.s_service, 1)
        hl.addWidget(bi)
        hl.addWidget(bu)
        lay.addWidget(g)

        from ferry import shortcut
        if shortcut.supported():
            where = "Start menu" if sys.platform == "win32" else "app menu"
            g = QGroupBox(f"Shortcut in the {where}")
            hl = QHBoxLayout(g)
            self.s_shortcut = QLabel()
            si = QPushButton(f"Add to {where}")
            si.clicked.connect(lambda: self.shortcut("install"))
            su = QPushButton("Remove")
            su.clicked.connect(lambda: self.shortcut("uninstall"))
            hl.addWidget(self.s_shortcut, 1)
            hl.addWidget(si)
            hl.addWidget(su)
            lay.addWidget(g)
        else:
            self.s_shortcut = None
        row = QHBoxLayout()
        b = QPushButton("Save settings")
        b.setDefault(True)
        b.clicked.connect(self.save_settings)
        r = QPushButton("Undo changes")
        r.clicked.connect(self.load_settings)
        row.addStretch(1)
        row.addWidget(r)
        row.addWidget(b)
        lay.addLayout(row)
        lay.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(w)
        self.tabs.addTab(sa, "Settings")

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction("Open Ferry", self.show_normal)
        menu.addAction("New copy…", lambda: (self.show_normal(), self.new_job()))
        menu.addSeparator()
        menu.addAction("Pause all", lambda: self._call(lambda: self.client.post("/pause_all")))
        menu.addAction("Resume all", lambda: self._call(lambda: self.client.post("/resume_all")))
        menu.addSeparator()
        quit_act = QAction("Quit window (service keeps running)", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal()
                                    if reason == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("Ferry")
        self.tray.show()

    def show_normal(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, ev):
        if self.tray and self.tray.isVisible():
            self.hide()
            self.tray.showMessage("Ferry", "Still running in the tray. Jobs continue in the background service.",
                                  QSystemTrayIcon.Information, 3000)
            ev.ignore()
        else:
            ev.accept()

    # ------------------------------------------------------------- data --
    def _call(self, fn):
        try:
            return fn()
        except Exception as e:
            QMessageBox.warning(self, "Ferry", str(e))
            return None

    def _set_row(self, table, row, values, colors=None):
        for col, v in enumerate(values):
            if isinstance(v, QWidget):
                table.setCellWidget(row, col, v)
                continue
            item = table.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                table.setItem(row, col, item)
            if item.text() != str(v):
                item.setText(str(v))
            if colors and col in colors:
                item.setForeground(QColor(colors[col]))

    def refresh(self):
        try:
            st = self.client.get("/status")
            runs = self.client.get("/runs")
            jobs = self.client.get("/jobs")
        except Exception as e:
            self.status_label.setText(f"⚠ service unreachable: {e}")
            return
        self.status_label.setText(f"{st['machine']} · {len(st['active'])} running · {st['jobs']} jobs · "
                                  f"Telegram {'✓' if st['telegram'] else '✗'} · live sync {len(st['watching'])}")
        if self.tray:
            self.tray.setToolTip(f"Ferry — {len(st['active'])} running")
        self._tg_status = st
        self._update_tg_state()
        runs.sort(key=lambda r: (r["phase"] in FINAL, -float(r.get("started") or 0)))
        selected = self.selected_run()
        self.t_runs.setRowCount(len(runs))
        for i, r in enumerate(runs):
            bar = self.t_runs.cellWidget(i, 2)
            if not isinstance(bar, QProgressBar):
                bar = QProgressBar()
                bar.setRange(0, 1000)
                bar.setTextVisible(True)
                self.t_runs.setCellWidget(i, 2, bar)
            mode = progress_mode(r)
            if mode == "busy":                 # before copying: total unknown -> indeterminate
                if bar.maximum() != 0:
                    bar.setRange(0, 0)
                bar.setFormat("Scanning…" if r["phase"] == "scanning" else "Starting…")
            else:                              # copying / done -> determinate
                if bar.maximum() != 1000:
                    bar.setRange(0, 1000)
                bar.setValue(int(r["percent"] * 10))
                bar.setFormat("Waiting for your OK" if mode == "waiting" else f"{r['percent']:.1f}%")
            ph = _phase(r)
            preparing = mode == "busy"
            self._set_row(self.t_runs, i, [
                r["job_name"], ph, bar,
                f"{r.get('scanned', 0):,} found" if preparing else f"{r['done_files']:,} / {r['total_files']:,}",
                "" if preparing else f"{human_bytes(r['done_bytes'])} / {human_bytes(r['total_bytes'])}",
                f"{human_bytes(r['speed'])}/s" if r["phase"] not in FINAL and not preparing else "",
                human_duration(r["elapsed"]),
                human_duration(r["eta"]) if r.get("eta") is not None and r["phase"] not in FINAL else "",
                r["id"]], {1: phase_color(r["phase"])})
            self._notify_change(r)
        self.runs = {r["id"]: r for r in runs}
        if selected:
            for i, r in enumerate(runs):
                if r["id"] == selected:
                    self.t_runs.selectRow(i)
        elif runs and not self.t_runs.selectedItems():
            self.t_runs.selectRow(0)
        self.update_details()

        jobs_sel = self.selected_job()
        self.t_jobs.setRowCount(len(jobs))
        for i, j in enumerate(jobs):
            self._set_row(self.t_jobs, i, [j["name"], j["mode"], j["source"], j["dest"], j.get("schedule") or "",
                                           "✓" if j.get("watch") else "", "✓" if j.get("enabled", True) else "✗"])
            self.t_jobs.item(i, 0).setData(Qt.UserRole, j["id"])
            if j["id"] == jobs_sel:
                self.t_jobs.selectRow(i)
        self.jobs = {j["id"]: j for j in jobs}

        if self.tabs.currentIndex() == 2:
            hist = self.client.get("/history?limit=200")
            self.t_hist.setRowCount(len(hist))
            for i, h in enumerate(hist):
                when = datetime.fromtimestamp(h["started"]).strftime("%Y-%m-%d %H:%M") if h.get("started") else ""
                self._set_row(self.t_hist, i, [when, h["job_name"], PHASE.get(h["phase"], h["phase"]),
                                               f"{h['done_files']:,}", human_bytes(h["done_bytes"]),
                                               human_duration(h["elapsed"]), h.get("failed") or "", h["id"]],
                              {2: phase_color(h["phase"])})
        self._update_actions()

    def _notify_change(self, r):
        old = self.known_phase.get(r["id"])
        self.known_phase[r["id"]] = r["phase"]
        if old is None or old == r["phase"] or not self.tray:
            return
        if r["phase"] == "awaiting_confirmation":
            self.tray.showMessage("Ferry: confirmation needed", f"{r['job_name']} wants to delete/overwrite files",
                                  QSystemTrayIcon.Warning, 8000)
        elif r["phase"] in FINAL and r["phase"] != "interrupted":
            icon = QSystemTrayIcon.Information if r["phase"] == "done" else QSystemTrayIcon.Warning
            self.tray.showMessage(f"Ferry: {PHASE.get(r['phase'])}",
                                  f"{r['job_name']}: {r['done_files']:,} files, {human_bytes(r['done_bytes'])} "
                                  f"in {human_duration(r['elapsed'])}", icon, 6000)

    def selected_run(self):
        rows = self.t_runs.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.t_runs.item(rows[0].row(), 8)
        return it.text() if it else None

    def selected_job(self):
        rows = self.t_jobs.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.t_jobs.item(rows[0].row(), 0)
        return it.data(Qt.UserRole) if it else None

    def _update_actions(self):
        r = self.runs.get(self.selected_run() or "")
        active = bool(r and r["phase"] not in FINAL)
        self.act_pause.setEnabled(active and not r.get("paused"))
        self.act_resume.setEnabled(active and bool(r.get("paused")))
        self.act_cancel.setEnabled(active)
        self.act_confirm.setEnabled(bool(r and r["phase"] == "awaiting_confirmation"))
        self.act_log.setEnabled(bool(r))
        self.act_errors.setEnabled(bool(r and r.get("failed")))

    def update_details(self):
        r = self.runs.get(self.selected_run() or "")
        if not r:
            self.details.setHtml("<p style='color:gray'>No runs yet. Click <b>New copy</b> to start.</p>")
            return
        import html
        e = html.escape
        out = [f"<h3>{e(r['job_name'])} <span style='color:{phase_color(r['phase'])}'>"
               f"— {e(_phase(r))}</span></h3>",
               f"<p><b>{e(r['source'])}</b> → <b>{e(r['dest'])}</b> ({e(r['mode'])})</p>",
               f"<p>Copied {r['done_files']:,} of {r['total_files']:,} files · {human_bytes(r['done_bytes'])} of "
               f"{human_bytes(r['total_bytes'])} · unchanged {r.get('same_files', 0):,} · failed {r['failed']:,} · "
               f"removed {r['deleted']:,}<br>Elapsed {human_duration(r['elapsed'])}"]
        if r.get("eta") is not None and r["phase"] not in FINAL:
            out[-1] += f" · time left {human_duration(r['eta'])} · {human_bytes(r['speed'])}/s"
        out[-1] += "</p>"
        pc = r.get("pending_confirmation")
        if pc:
            out.append(f"<p style='color:{color('warn')}'><b>Waiting for your OK:</b> {pc['deletes']:,} deletions, "
                       f"{pc['overwrites']:,} overwrites. Use <b>Confirm…</b> in the toolbar.</p><ul>"
                       + "".join(f"<li>{e(s)}</li>" for s in pc.get("sample", [])[:10]) + "</ul>")
        if r.get("current"):
            out.append("<p><b>Now copying</b></p><ul>" + "".join(
                f"<li>{e(c['path'])} — {c['done'] * 100 // max(1, c['size'])}% of {human_bytes(c['size'])}</li>"
                for c in r["current"][:8]) + "</ul>")
        for w in r.get("warnings") or []:
            out.append(f"<p style='color:{color('warn')}'>⚠ {e(w['message'])}</p>")
        if r.get("error"):
            out.append(f"<p style='color:{color('err')}'><b>Error:</b> {e(r['error'])}</p>")
        html_text = "".join(out)
        if getattr(self, "_last_details", None) != html_text:
            self._last_details = html_text
            self.details.setHtml(html_text)

    # ----------------------------------------------------------- actions --
    def run_action(self, action):
        rid = self.selected_run()
        if rid:
            self._call(lambda: self.client.post(f"/runs/{rid}/{action}"))
            self.refresh()

    def confirm_run(self):
        rid = self.selected_run()
        r = self.runs.get(rid or "")
        if not r or not r.get("pending_confirmation"):
            return
        pc = r["pending_confirmation"]
        box = QMessageBox(self)
        box.setWindowTitle("Confirm changes")
        box.setText(f"<b>{r['job_name']}</b> will delete {pc['deletes']:,} and overwrite {pc['overwrites']:,} "
                    f"file(s) in the destination.")
        box.setDetailedText("\n".join(pc.get("sample", [])))
        b_all = box.addButton("Apply all", QMessageBox.AcceptRole)
        b_skip = box.addButton("Copy, but skip deletions/overwrites", QMessageBox.ActionRole)
        b_cancel = box.addButton("Cancel run", QMessageBox.RejectRole)
        box.exec()
        choice = {b_all: "all", b_skip: "skip", b_cancel: "cancel"}.get(box.clickedButton())
        if choice:
            self._call(lambda: self.client.post(f"/runs/{rid}/confirm", {"choice": choice}))

    def _text_dialog(self, title, text):
        d = QDialog(self)
        d.setWindowTitle(title)
        d.resize(1000, 600)
        lay = QVBoxLayout(d)
        te = QPlainTextEdit(text)
        te.setReadOnly(True)
        te.setFont(QFont("Monospace", 9))
        te.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(te)
        te.moveCursor(te.textCursor().End)
        d.exec()

    def show_log(self):
        rid = self.selected_run()
        if rid:
            text = self._call(lambda: self.client.call("GET", f"/runs/{rid}/log?tail=20000", raw=True))
            if text is not None:
                self._text_dialog(f"Log — {rid}", text)

    def show_errors(self):
        rid = self.selected_run()
        if rid:
            fails = self._call(lambda: self.client.get(f"/runs/{rid}/failures?limit=5000"))
            if fails is not None:
                self._text_dialog(f"Failed files — {rid}",
                                  "\n".join(f"{f['path']}\n    {f['error']}" for f in fails) or "No failures")

    def new_job(self):
        self._job_dialog(None)

    def edit_job(self, *_):
        jid = self.selected_job()
        if jid:
            self._job_dialog(self.jobs[jid])

    def _job_dialog(self, job):
        dlg = JobDialog(self, job)
        if dlg.exec() != QDialog.Accepted:
            return
        d = dlg.result_spec
        if dlg.result_action == "save":
            d.pop("dry_run", None)
            if self._call(lambda: self.client.post("/jobs", d)) is not None:
                self.statusBar().showMessage(f"Saved job {d['name']}", 4000)
                self.tabs.setCurrentIndex(1)
        else:
            d.pop("id", None)
            if self._call(lambda: self.client.post("/run", {"spec": d, "trigger": "gui"})) is not None:
                self.tabs.setCurrentIndex(0)
        self.refresh()

    def run_job(self, dry=False):
        jid = self.selected_job()
        if jid and self._call(lambda: self.client.post(f"/jobs/{jid}/run", {"dry_run": dry, "trigger": "gui"})):
            self.tabs.setCurrentIndex(0)
            self.refresh()

    def delete_job(self):
        jid = self.selected_job()
        if jid and QMessageBox.question(self, "Delete job", f"Delete job '{self.jobs[jid]['name']}'?\n"
                                        "(Files are not touched.)") == QMessageBox.Yes:
            self._call(lambda: self.client.delete(f"/jobs/{jid}"))
            self.refresh()

    # ---------------------------------------------------------- settings --
    def load_settings(self):
        cfg = self._call(lambda: self.client.get("/config"))
        if not cfg:
            return
        tg = cfg["telegram"]
        self._tg_cfg = tg
        self.s_theme.blockSignals(True)
        self.s_theme.setCurrentIndex(max(0, THEMES.index(_theme["mode"]) if _theme["mode"] in THEMES else 0))
        self.s_theme.blockSignals(False)
        self._update_tg_state()
        self.s_machine.setText(cfg.get("machine_name", ""))
        self.s_tg_enabled.setChecked(tg["enabled"])
        self.s_tg_token.setText("")
        self.s_tg_token.setPlaceholderText(f"current: {tg['token']}" if tg["token"] else "paste the bot token")
        self.s_tg_ids.setText(", ".join(str(i) for i in tg["allowed_ids"]))
        self.s_tg_chats.setText(", ".join(str(i) for i in tg["notify_chat_ids"]))
        self.s_tg_pct.setValue(int(tg["progress_every_percent"] or 0))
        self.s_tg_min.setValue(int(tg["progress_every_minutes"] or 0))
        self.s_tg_start.setChecked(tg["notify_start"])
        self.s_tg_finish.setChecked(tg["notify_finish"])
        self.s_tg_errors.setChecked(tg["notify_errors"])
        self.s_threads.setValue(int(cfg["defaults"]["threads"]))
        self.s_retries.setValue(int(cfg["defaults"]["retries"]))
        self.s_resume.setChecked(bool(cfg.get("auto_resume", True)))
        from ferry import service
        installed, where = service.status()
        self.s_service.setText(f"Installed: {where}" if installed else "Not installed — runs only while started")
        if self.s_shortcut is not None:
            from ferry import shortcut
            there, path = shortcut.status()
            self.s_shortcut.setText(f"Added: {path}" if there else "Not added — open Ferry from its folder")

    def save_settings(self):
        def ids(edit):
            out = []
            for x in edit.text().replace(";", ",").split(","):
                x = x.strip()
                if x:
                    out.append(int(x))
            return out
        try:
            body = {"machine_name": self.s_machine.text().strip() or None,
                    "telegram.allowed_ids": ids(self.s_tg_ids), "telegram.notify_chat_ids": ids(self.s_tg_chats),
                    "telegram.progress_every_percent": self.s_tg_pct.value(),
                    "telegram.progress_every_minutes": self.s_tg_min.value(),
                    "telegram.notify_start": self.s_tg_start.isChecked(),
                    "telegram.notify_finish": self.s_tg_finish.isChecked(),
                    "telegram.notify_errors": self.s_tg_errors.isChecked(),
                    "defaults.threads": self.s_threads.value(), "defaults.retries": self.s_retries.value(),
                    "auto_resume": self.s_resume.isChecked()}
        except ValueError:
            return QMessageBox.warning(self, "Ferry", "Telegram IDs must be numbers.")
        if body["machine_name"] is None:
            body.pop("machine_name")
        if self.s_tg_token.text().strip():
            body["telegram.token"] = self.s_tg_token.text().strip()
        body["telegram.enabled"] = self.s_tg_enabled.isChecked()   # last: reloads the bot with the new settings
        if self._call(lambda: self.client.post("/config", body)) is not None:
            self.statusBar().showMessage("Settings saved", 4000)
            self.load_settings()

    def change_theme(self, *_):
        mode = self.s_theme.currentData()
        if mode == _theme["mode"]:
            return
        apply_theme(mode)
        self._call(lambda: self.client.post("/config", {"ui.theme": mode}))

    def on_theme_changed(self):
        self.update_details()
        self._update_tg_state()
        self.refresh()

    def _update_tg_state(self):
        st = getattr(self, "_tg_status", None) or {}
        cfg = getattr(self, "_tg_cfg", None) or {}
        bot = st.get("telegram_bot")
        link = f' <a href="https://t.me/{bot}">@{bot}</a>' if bot else ""
        if not cfg.get("token"):
            text, col = "Not set up. Press “Set up bot…” to control Ferry from your phone.", color("muted")
        elif not cfg.get("enabled"):
            text, col = "Bot is turned off.", color("muted")
        elif st.get("telegram"):
            n = len(cfg.get("allowed_ids") or [])
            text, col = f"● Connected{link} · {n} allowed user(s)/group(s)", color("ok")
            if not n:
                text, col = f"● Connected{link}, but nobody is allowed yet: add your ID below", color("warn")
        elif st.get("telegram_error"):
            text, col = f"✗ {esc_html(st['telegram_error'])}", color("err")
        else:
            text, col = "Connecting…", color("warn")
        html = f"<span style='color:{col}'>{text}</span>"
        if self.s_tg_state.text() != html:
            self.s_tg_state.setText(html)
        self.s_tg_disconnect.setEnabled(bool(cfg.get("token")))
        self.s_tg_setup.setText("Change bot…" if cfg.get("token") else "Set up bot…")

    def setup_telegram(self):
        dlg = TelegramSetupDialog(self, self.client, self.s_machine.text().strip())
        dlg.exec()
        self.load_settings()
        QTimer.singleShot(1500, self.refresh)

    def disconnect_telegram(self):
        if QMessageBox.question(self, "Telegram", "Remove the bot token from this machine? The bot stops "
                                "answering. (Delete the bot itself in @BotFather if you don't need it.)") \
                != QMessageBox.Yes:
            return
        if self._call(lambda: self.client.post("/config", {"telegram.token": "", "telegram.enabled": False})) \
                is not None:
            self.load_settings()
            self.refresh()

    def test_telegram(self):
        res = self._call(lambda: self.client.post("/telegram/test"))
        if res:
            if res.get("ok"):
                QMessageBox.information(self, "Telegram", f"Bot @{res.get('bot')} works. "
                                        f"Test message sent to {res.get('sent_to') or 'nobody (add your ID)'}.")
            else:
                QMessageBox.warning(self, "Telegram", f"Problem: {res.get('error')}")

    def shortcut(self, what):
        from ferry import shortcut
        try:
            msg = shortcut.install() if what == "install" else shortcut.uninstall()
            QMessageBox.information(self, "Shortcut", msg)
        except Exception as e:
            QMessageBox.warning(self, "Shortcut", str(e))
        self.load_settings()

    def service(self, what):
        from ferry import service
        try:
            msg = service.install() if what == "install" else service.uninstall()
            QMessageBox.information(self, "Background service", msg)
        except Exception as e:
            QMessageBox.warning(self, "Background service", str(e))
        self.load_settings()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Ferry")
    app.setWindowIcon(app_icon())
    app.setDesktopFileName("ferry")     # Linux: match ferry.desktop so the taskbar shows the icon
    app.setQuitOnLastWindowClosed(False)
    try:
        from ferry.daemon import ensure_daemon
        client = ensure_daemon()
    except Exception as e:
        QMessageBox.critical(None, "Ferry", f"Could not start the background service:\n{e}")
        return 2
    try:
        mode = (client.get("/config").get("ui") or {}).get("theme", "system")
    except Exception:
        mode = "system"
    apply_theme(mode)
    try:   # follow the computer's light/dark switch while "Match system" is chosen
        app.styleHints().colorSchemeChanged.connect(lambda *_: _theme["mode"] == "system" and apply_theme())
    except AttributeError:
        pass
    w = MainWindow(client)
    w.show()
    if not w.tray:
        app.setQuitOnLastWindowClosed(True)
    return app.exec()


def entry():
    """Entry point of the ferry-gui executable: CLI when given arguments, else the window."""
    if len(sys.argv) > 1:
        from ferry.cli import main as cli_main
        sys.exit(cli_main())
    sys.exit(main())
