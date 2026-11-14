"""Saved jobs and the run manager (starting, tracking and controlling runs)."""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from ferry.config import Config, data_dir
from ferry.engine import Engine, Phase, read_failures
from ferry.spec import JobSpec

log = logging.getLogger("ferry")


class JobStore:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "jobs.json"
        self.lock = threading.RLock()
        self._jobs: dict[str, JobSpec] = {}
        self._mtime = 0.0
        self.load()

    def load(self):
        with self.lock:
            if self.path.exists():
                self._mtime = self.path.stat().st_mtime
                raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
                self._jobs = {j["id"]: JobSpec.from_dict(j) for j in raw}

    def _maybe_reload(self):
        try:
            if self.path.exists() and self.path.stat().st_mtime != self._mtime:
                self.load()
        except Exception:
            log.exception("could not reload jobs.json")

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([j.to_dict() for j in self._jobs.values()], indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self.path.stat().st_mtime

    def list(self) -> list[JobSpec]:
        with self.lock:
            self._maybe_reload()
            return sorted(self._jobs.values(), key=lambda j: j.name.lower())

    def get(self, key: str) -> JobSpec | None:
        """Look up by id or (case-insensitive) name."""
        with self.lock:
            self._maybe_reload()
            if key in self._jobs:
                return self._jobs[key]
            for j in self._jobs.values():
                if j.name.lower() == key.lower():
                    return j
            return None

    def put(self, job: JobSpec) -> JobSpec:
        job.validate()
        with self.lock:
            for other in self._jobs.values():
                if other.name.lower() == job.name.lower() and other.id != job.id:
                    raise ValueError(f"a job named {job.name!r} already exists")
            self._jobs[job.id] = job
            self.save()
        return job

    def delete(self, key: str) -> bool:
        with self.lock:
            j = self.get(key)
            if not j:
                return False
            del self._jobs[j.id]
            self.save()
            return True


class Run:
    def __init__(self, run_id: str, spec: JobSpec, run_dir: Path, trigger: str, job_id: str | None,
                 scope=None, defaults=None, on_event=None, quiet=False):
        self.id = run_id
        self.spec = spec
        self.job_id = job_id
        self.trigger = trigger
        self.dir = run_dir
        self.quiet = quiet          # watch runs: only notify on errors
        self.log_path = run_dir / "run.log"
        self.logger = logging.getLogger(f"ferry.run.{run_id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        self.logger.addHandler(handler)
        self._handler = handler
        self.engine = Engine(spec, str(run_dir), self.logger, scope=scope, defaults=defaults,
                             on_event=lambda kind, data: on_event and on_event(self, kind, data))
        self.thread: threading.Thread | None = None

    def info(self) -> dict:
        st = self.engine.status()
        st.update({"id": self.id, "job_id": self.job_id, "job_name": self.spec.name, "trigger": self.trigger,
                   "log": str(self.log_path)})
        return st

    def close_log(self):
        self.logger.removeHandler(self._handler)
        self._handler.close()


class RunManager:
    def __init__(self, config: Config | None = None, jobs: JobStore | None = None):
        self.config = config or Config()
        self.jobs = jobs or JobStore()
        self.runs: dict[str, Run] = {}
        self.lock = threading.RLock()
        self.listeners = []          # callables(event: str, run_info: dict, data)
        self.runs_dir = data_dir() / "runs"
        self.history_path = data_dir() / "history.jsonl"

    # -------------------------------------------------------------- events --
    def subscribe(self, fn):
        self.listeners.append(fn)

    def _emit(self, event, run: Run, data=None):
        info = run.info()
        info["quiet"] = run.quiet
        info["notify"] = run.spec.notify
        for fn in list(self.listeners):
            try:
                fn(event, info, data)
            except Exception:
                log.exception("listener failed")

    def _on_engine_event(self, run: Run, kind, data):
        if kind == "confirm":
            self._emit("confirm", run, data)
        elif kind == "file_error":
            self._emit("file_error", run, data)

    # ---------------------------------------------------------------- runs --
    def _new_run_id(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)

    def start(self, spec: JobSpec, trigger="manual", job_id=None, scope=None, quiet=False,
              run_id: str | None = None) -> Run:
        spec.validate()
        rid = run_id or self._new_run_id()
        run_dir = self.runs_dir / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "spec.json").write_text(json.dumps({"spec": spec.to_dict(), "job_id": job_id,
                                                       "trigger": trigger, "scope": scope}), encoding="utf-8")
        run = Run(rid, spec, run_dir, trigger, job_id, scope=scope, defaults=self.config.get("defaults"),
                  on_event=self._on_engine_event, quiet=quiet)
        with self.lock:
            self.runs[rid] = run

        def body():
            self._emit("start", run)
            try:
                run.engine.run()
            finally:
                run.close_log()
                self._record_history(run)
                self._emit("finish", run)
                self._prune()

        run.thread = threading.Thread(target=body, daemon=True, name=f"run-{rid}")
        run.thread.start()
        return run

    def start_job(self, key: str, trigger="manual", scope=None, quiet=False, dry_run=False) -> Run:
        job = self.jobs.get(key)
        if not job:
            raise KeyError(f"no such job: {key}")
        if self.is_job_running(job.id):
            raise RuntimeError(f"job {job.name!r} is already running")
        spec = JobSpec.from_dict(job.to_dict())
        if dry_run:
            spec.dry_run = True
        return self.start(spec, trigger=trigger, job_id=job.id, scope=scope, quiet=quiet)

    def is_job_running(self, job_id: str) -> bool:
        with self.lock:
            return any(r.job_id == job_id and r.engine.phase not in Phase.FINAL for r in self.runs.values())

    def get(self, key: str) -> Run | None:
        """Find a run by id, id prefix/suffix, or job name (latest)."""
        with self.lock:
            if key in self.runs:
                return self.runs[key]
            matches = [r for r in self.runs.values() if r.id.endswith(key) or r.id.startswith(key)]
            if not matches:
                matches = [r for r in self.runs.values() if r.spec.name.lower() == key.lower()]
            return sorted(matches, key=lambda r: r.id)[-1] if matches else None

    def active(self) -> list[Run]:
        with self.lock:
            return [r for r in self.runs.values() if r.engine.phase not in Phase.FINAL]

    def all_runs(self) -> list[Run]:
        with self.lock:
            return sorted(self.runs.values(), key=lambda r: r.id)

    def pause(self, key):
        r = self._require(key)
        r.engine.pause()
        return r

    def resume(self, key):
        r = self._require(key)
        r.engine.resume()
        return r

    def cancel(self, key):
        r = self._require(key)
        r.engine.cancel()
        return r

    def confirm(self, key, choice="all"):
        r = self._require(key)
        r.engine.confirm(choice)
        return r

    def pause_all(self):
        for r in self.active():
            r.engine.pause()

    def resume_all(self):
        for r in self.active():
            r.engine.resume()

    def _require(self, key) -> Run:
        r = self.get(key)
        if not r:
            raise KeyError(f"no such run: {key}")
        return r

    def failures(self, key, limit=1000):
        r = self.get(key)
        d = r.dir if r else self.runs_dir / key
        return read_failures(str(d), limit)

    def log_path(self, key) -> Path | None:
        r = self.get(key)
        p = (r.dir if r else self.runs_dir / key) / "run.log"
        return p if p.exists() else None

    # ------------------------------------------------------------- history --
    def _record_history(self, run: Run):
        info = run.info()
        info.pop("current", None)
        with self.lock, open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(info) + "\n")

    def history(self, limit=50) -> list[dict]:
        if not self.history_path.exists():
            return []
        lines = self.history_path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out[::-1]

    def _prune(self):
        keep = int(self.config.get("history_keep", 200))
        with self.lock:
            done = [r for r in self.all_runs() if r.engine.phase in Phase.FINAL]
            for r in done[:-20]:          # keep the latest 20 finished runs in memory
                self.runs.pop(r.id, None)
            if self.history_path.exists():
                lines = self.history_path.read_text(encoding="utf-8").splitlines()
                if len(lines) > keep * 2:
                    self.history_path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
        dirs = sorted(p for p in self.runs_dir.iterdir() if p.is_dir())
        active = {r.id for r in self.active()}
        for p in dirs[:-keep]:
            if p.name not in active:
                shutil.rmtree(p, ignore_errors=True)

    # -------------------------------------------------------------- resume --
    def resume_interrupted(self, auto: bool) -> list[str]:
        """Find runs whose process died mid-way. Restart them if ``auto``."""
        resumed = []
        for p in sorted(self.runs_dir.iterdir()):
            spec_file, db = p / "spec.json", p / "plan.db"
            if not spec_file.exists() or not db.exists() or p.name in self.runs:
                continue
            import sqlite3
            try:
                con = sqlite3.connect(db)
                row = con.execute("SELECT value FROM meta WHERE key='final_phase'").fetchone()
                planned = con.execute("SELECT value FROM meta WHERE key='planned'").fetchone()
                con.close()
            except sqlite3.Error:
                continue
            if row:   # finished normally
                continue
            if not planned or not auto:
                continue
            data = json.loads(spec_file.read_text(encoding="utf-8"))
            spec = JobSpec.from_dict(data["spec"])
            if data.get("job_id") and self.is_job_running(data["job_id"]):
                continue
            log.info("resuming interrupted run %s (%s)", p.name, spec.name)
            self.start(spec, trigger="resume", job_id=data.get("job_id"), scope=data.get("scope"), run_id=p.name)
            resumed.append(p.name)
        return resumed

    def shutdown(self, timeout=10):
        for r in self.active():
            r.engine.interrupt()
        end = time.time() + timeout
        for r in list(self.runs.values()):
            if r.thread and r.thread.is_alive():
                r.thread.join(max(0.1, end - time.time()))
