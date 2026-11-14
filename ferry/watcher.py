"""Live sync: watch local sources and run an incremental sync of changed folders."""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("ferry.watch")

MAX_SCOPED_DIRS = 2000   # beyond this a full scan is cheaper


class JobWatcher:
    def __init__(self, manager, job):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        self.manager = manager
        self.job_id = job.id
        self.root = os.path.abspath(os.path.expanduser(job.source))
        self.delay = max(1.0, float(job.watch_delay or 5))
        self.dirs: set[str] = set()
        self.full = False
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in ("opened", "closed_no_write"):
                    return
                for p in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
                    if p:
                        watcher._changed(os.fsdecode(p), event.is_directory)

        self.observer = Observer()
        self.observer.schedule(Handler(), self.root, recursive=True)

    def start(self):
        self.observer.start()
        log.info("watching %s", self.root)

    def stop(self):
        self.observer.stop()
        if self.timer:
            self.timer.cancel()

    def _changed(self, path: str, is_dir: bool):
        rel = os.path.relpath(path, self.root).replace("\\", "/")
        if rel.startswith("..") or ".ferrypart" in rel:
            return
        # scan the parent folder; it notices new, changed and removed entries
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if rel == ".":
            d = ""
        with self.lock:
            self.dirs.add(d)
            if len(self.dirs) > MAX_SCOPED_DIRS:
                self.full = True
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self._fire)
            self.timer.daemon = True
            self.timer.start()

    def _fire(self):
        if self.manager.is_job_running(self.job_id):
            # try again after the current run finishes
            with self.lock:
                self.timer = threading.Timer(self.delay, self._fire)
                self.timer.daemon = True
                self.timer.start()
            return
        with self.lock:
            dirs, full = self.dirs, self.full
            self.dirs, self.full = set(), False
            self.timer = None
        if not dirs and not full:
            return
        scope = None if full else sorted(dirs)
        log.info("change detected for job %s: %s", self.job_id, "full scan" if scope is None else scope[:5])
        self.manager.start_job(self.job_id, trigger="watch", scope=scope, quiet=True)


class WatchManager:
    def __init__(self, manager):
        self.manager = manager
        self.watchers: dict[str, JobWatcher] = {}

    def refresh(self):
        wanted = {}
        for job in self.manager.jobs.list():
            if job.watch and job.enabled:
                from ferry.backends import LocalBackend, open_backend
                try:
                    if isinstance(open_backend(job.source), LocalBackend) and os.path.isdir(
                            os.path.expanduser(job.source)):
                        wanted[job.id] = job
                    else:
                        log.warning("watch only works for local/mounted sources: %s", job.name)
                except Exception as e:
                    log.warning("cannot watch %s: %s", job.name, e)
        for jid in list(self.watchers):
            if jid not in wanted or self.watchers[jid].root != os.path.abspath(os.path.expanduser(wanted[jid].source)):
                self.watchers.pop(jid).stop()
        for jid, job in wanted.items():
            if jid not in self.watchers:
                try:
                    w = JobWatcher(self.manager, job)
                    w.start()
                    self.watchers[jid] = w
                except Exception as e:
                    log.error("could not watch %s: %s", job.name, e)

    def stop(self):
        for w in self.watchers.values():
            w.stop()
        self.watchers.clear()
