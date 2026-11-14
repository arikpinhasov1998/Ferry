"""The background service: runs jobs, schedules, watchers, Telegram bot and API."""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time

from ferry import __version__
from ferry.config import Config, data_dir


def self_command() -> list[str]:
    """Command line that starts this same Ferry (frozen exe or python -m ferry)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        w = exe[:-10] + "pythonw.exe"
        if os.path.exists(w):
            exe = w
    return [exe, "-m", "ferry"]


def setup_daemon_logging() -> logging.Logger:
    log = logging.getLogger("ferry")
    log.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(data_dir() / "logs" / "daemon.log", maxBytes=5_000_000,
                                             backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    log.addHandler(h)
    if sys.stderr and sys.stderr.isatty():
        s = logging.StreamHandler()
        s.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(s)
    return log


class Daemon:
    def __init__(self, config: Config | None = None):
        from ferry.jobs import RunManager
        self.config = config or Config()
        self.log = setup_daemon_logging()
        self.manager = RunManager(self.config)
        self.started = time.time()
        self.stop_event = threading.Event()
        self.scheduler = None
        self.watchers = None
        self.telegram = None
        self.api = None
        self._tg_lock = threading.Lock()

    # ----------------------------------------------------------- lifecycle --
    def run_forever(self):
        from ferry.api import ApiServer
        from ferry.scheduler import Scheduler
        from ferry.watcher import WatchManager

        try:
            self.api = ApiServer(self)
        except OSError as e:
            self.log.error("cannot listen on the API port (%s) - is Ferry already running?", e)
            print(f"Ferry daemon is already running or port {self.config.get('api.port')} is busy: {e}",
                  file=sys.stderr)
            return 1
        pid_file = data_dir() / "daemon.pid"
        pid_file.write_text(str(os.getpid()))
        self.log.info("Ferry %s daemon started (pid %d, data %s)", __version__, os.getpid(), data_dir())
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: self.stop())
                except (ValueError, OSError):
                    pass
        self.scheduler = Scheduler(self.manager)
        self.scheduler.start()
        try:
            self.watchers = WatchManager(self.manager)
            self.watchers.refresh()
        except ImportError:
            self.log.warning("watchdog not available: live sync disabled")
        self.reload_telegram()
        try:
            resumed = self.manager.resume_interrupted(bool(self.config.get("auto_resume", True)))
            if resumed:
                self.log.info("resumed %d interrupted run(s)", len(resumed))
        except Exception:
            self.log.exception("resume failed")
        self.api.serve()   # only now, so clients never see a half-started service
        while not self.stop_event.wait(15):
            self.refresh_watchers()
        self._shutdown()
        try:
            pid_file.unlink()
        except OSError:
            pass
        return 0

    def stop(self):
        self.stop_event.set()

    def _shutdown(self):
        self.log.info("shutting down")
        if self.scheduler:
            self.scheduler.stop()
        if self.watchers:
            self.watchers.stop()
        if self.telegram:
            self.telegram.stop()
        self.manager.shutdown()
        if self.api:
            self.api.stop()

    # -------------------------------------------------------------- helpers --
    def refresh_watchers(self):
        if self.watchers:
            try:
                self.watchers.refresh()
            except Exception:
                self.log.exception("watcher refresh failed")

    def reload_telegram(self):
        from ferry.telegram import TelegramBot
        with self._tg_lock:
            if self.telegram:
                self.telegram.stop()
                self.telegram = None
            tg = self.config.get("telegram") or {}
            if tg.get("enabled") and tg.get("token"):
                self.telegram = TelegramBot(self)
                self.telegram.start()

    def telegram_test(self) -> dict:
        from ferry.telegram import TelegramApi
        token = self.config.get("telegram.token")
        if not token:
            return {"ok": False, "error": "no bot token configured"}
        try:
            me = TelegramApi(token).call("getMe")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        sent = []
        for chat in self.config.get("telegram.notify_chat_ids") or self.config.get("telegram.allowed_ids") or []:
            try:
                TelegramApi(token).send(chat, f"✅ Ferry on {self.config.get('machine_name')} is connected.")
                sent.append(chat)
            except Exception as e:
                return {"ok": False, "bot": me.get("username"), "error": f"sending to {chat}: {e}"}
        return {"ok": True, "bot": me.get("username"), "sent_to": sent}

    def status_snapshot(self) -> dict:
        m = self.manager
        return {
            "machine": self.config.get("machine_name"), "version": __version__, "pid": os.getpid(),
            "uptime": time.time() - self.started,
            "active": [r.info() for r in m.active()],
            "recent": [r.info() for r in m.all_runs() if r not in m.active()][-10:],
            "jobs": len(m.jobs.list()),
            "telegram": bool(self.telegram and self.telegram.running),
            "watching": sorted(self.watchers.watchers) if self.watchers else [],
        }


# --------------------------------------------------------------- spawning --
def spawn_detached(wait: float = 15) -> bool:
    """Start ``ferry daemon run`` in the background (no admin needed)."""
    from ferry.api import Client
    client = Client()
    if client.alive():
        return True
    cmd = self_command() + ["daemon", "run"]
    log_file = open(data_dir() / "logs" / "daemon.stdout.log", "ab")
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    deadline = time.time() + wait
    while time.time() < deadline:
        if client.alive():
            return True
        time.sleep(0.3)
    return False


def ensure_daemon() -> "Client":
    from ferry.api import Client
    if not spawn_detached():
        raise RuntimeError(f"could not start the Ferry background service; see {data_dir() / 'logs'}")
    return Client()
