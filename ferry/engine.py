"""The copy/sync engine.

A run has two stages:

1. **Plan** - walk source and destination side by side, one directory at a time,
   and write every needed action into an on-disk SQLite plan (``plan.db``).
   Memory use stays flat even for millions of files, and totals for the
   progress/ETA are known before copying starts.
2. **Execute** - mkdirs, then files in parallel (bandwidth limit, time windows,
   pause/cancel, retries, resume inside large files, delta update, verify),
   then deletions (delete / archive / after confirmation), then directory
   metadata.

If the process stops mid-run, the plan is reused and only unfinished actions run.
"""
from __future__ import annotations

import collections
import hashlib
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ferry import metadata as meta
from ferry.backends import Backend, LocalBackend, RcloneBackend, join, open_backend, parent
from ferry.filters import CASE_INSENSITIVE, PART_SUFFIX, Filter
from ferry.spec import JobSpec
from ferry.util import TimeWindow, current_bwlimit, in_any_window, parse_size

CHUNK = 1024 * 1024            # 1 MiB I/O blocks
RESUME_MIN = 8 * CHUNK          # files >= 8 MiB resume from a partial copy
DELTA_MIN = 64 * CHUNK          # files >= 64 MiB may be updated in place (delta)
DELTA_BLOCK = 4 * CHUNK

PENDING, DONE, FAILED, SKIPPED = 0, 1, 2, 3


class Phase:
    PENDING = "pending"
    SCANNING = "scanning"
    CONFIRM = "awaiting_confirmation"
    COPYING = "copying"
    DELETING = "deleting"
    FINISHING = "finishing"
    DONE = "done"
    PARTIAL = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    FINAL = {DONE, PARTIAL, FAILED, CANCELLED, INTERRUPTED}


def mtime_granularity(ns: int) -> float:
    """Guess a stored timestamp's precision from its value (FAT 2s, HFS+/SFTP 1s, exFAT 10ms, NTFS 100ns)."""
    if ns % 1_000_000_000 == 0:
        return 2.0
    if ns % 10_000_000 == 0:
        return 0.01
    if ns % 1000 == 0:
        return 1e-6
    return 1e-7


class Cancelled(Exception):
    pass


class VerifyError(Exception):
    pass


class TokenBucket:
    """Shared bandwidth limiter. ``rate_fn`` returns bytes/s or None (unlimited)."""

    def __init__(self, rate_fn):
        self.rate_fn = rate_fn
        self.lock = threading.Lock()
        self.tokens = 0.0
        self.last = time.monotonic()

    def consume(self, n: int, check):
        while True:
            rate = self.rate_fn()
            if not rate:
                return
            with self.lock:
                now = time.monotonic()
                self.tokens = min(float(rate), self.tokens + (now - self.last) * rate)
                self.last = now
                if self.tokens >= n or (self.tokens >= 0 and n > rate):
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / rate
            check()
            time.sleep(min(wait, 0.5))


def _hasher(algo: str):
    if algo == "xxhash":
        try:
            import xxhash
            return xxhash.xxh3_128()
        except ImportError:
            return hashlib.blake2b(digest_size=16)
    return hashlib.sha256()


class Engine:
    def __init__(self, spec: JobSpec, run_dir: str, logger: logging.Logger | None = None,
                 scope: list[str] | None = None, defaults: dict | None = None, on_event=None):
        self.spec = spec
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.log = logger or logging.getLogger("ferry.engine")
        self.scope = scope
        self.on_event = on_event or (lambda kind, data: None)
        defaults = defaults or {}
        self.threads = max(1, int(spec.threads or defaults.get("threads", 8)))
        self.retries = int(spec.retries if spec.retries is not None else defaults.get("retries", 3))
        self.retry_wait = float(spec.retry_wait if spec.retry_wait is not None else defaults.get("retry_wait", 5))
        self.meta = meta.MetaOptions.from_spec(spec.metadata)
        self.filter = Filter.from_spec(spec.filters)
        self.windows = [TimeWindow(w) for w in spec.windows]
        self.bw_default = parse_size(spec.bwlimit)
        self.bw_schedule = [(TimeWindow(i["window"]), parse_size(i.get("limit"))) for i in spec.bw_schedule]
        self.bucket = TokenBucket(lambda: current_bwlimit(self.bw_default, self.bw_schedule))
        self.archive_stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        self.phase = Phase.PENDING
        self.error: str | None = None
        self.started = None
        self.ended = None
        self._interrupted = False
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._confirm = threading.Event()
        self._confirm_choice: str | None = None
        self._in_window = True
        self._window_checked = 0.0
        self.lock = threading.Lock()
        self.db_lock = threading.Lock()
        self.db: sqlite3.Connection | None = None
        self._pending_writes = 0

        # stats
        self.plan_counts = collections.Counter()
        self.total_files = self.total_bytes = 0
        self.done_files = self.done_bytes = 0
        self.failed = self.skipped = self.deleted = 0
        self.same_files = 0
        self.scanned_entries = 0
        self.xfer_bytes = 0
        self.current: dict[int, list] = {}            # thread id -> [rel, done, size]
        self.samples: collections.deque = collections.deque(maxlen=60)
        self.warnings: dict[str, list] = {}           # key -> [message, count]
        self.pending_confirmation: dict | None = None

        self.src: Backend | None = None
        self.dst: Backend | None = None
        self.single_file: str | None = None

    # ------------------------------------------------------------ control --
    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def cancel(self):
        self._cancel.set()
        self._confirm.set()

    def interrupt(self):
        """Stop now but keep the plan so the run resumes next time (daemon shutdown)."""
        self._interrupted = True
        self.cancel()

    def confirm(self, choice: str = "all"):
        """choice: 'all' (apply deletes/overwrites), 'skip' (only safe changes), 'cancel'."""
        self._confirm_choice = choice
        if choice == "cancel":
            self._cancel.set()
        self._confirm.set()

    @property
    def paused(self):
        return self._pause.is_set()

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _gate(self):
        """Block while paused or outside the allowed time windows."""
        while True:
            self._check()
            if self._pause.is_set():
                time.sleep(0.3)
                continue
            now = time.monotonic()
            if now - self._window_checked > 5:
                self._window_checked = now
                self._in_window = in_any_window(self.windows)
            if not self._in_window:
                time.sleep(1)
                self._window_checked = 0
                continue
            return

    # ------------------------------------------------------------ helpers --
    def warn(self, key: str, msg: str):
        with self.lock:
            w = self.warnings.get(key)
            if w:
                w[1] += 1
                return
            self.warnings[key] = [msg, 1]
        self.log.warning(msg)

    def _open_db(self):
        path = os.path.join(self.run_dir, "plan.db")
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS actions(
              id INTEGER PRIMARY KEY, kind TEXT, rel TEXT, size INTEGER DEFAULT 0,
              mtime_ns INTEGER DEFAULT 0, is_dir INTEGER DEFAULT 0, extra TEXT,
              status INTEGER DEFAULT 0, error TEXT, attempts INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS ix_kind_status ON actions(kind, status);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)

    def _meta_get(self, key, default=None):
        with self.db_lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _meta_set(self, key, value):
        with self.db_lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def _set_status(self, aid: int, status: int, error: str | None = None, attempts: int = 0):
        with self.db_lock:
            if self._pending_writes == 0:
                self.db.execute("BEGIN")
            self.db.execute("UPDATE actions SET status=?, error=?, attempts=? WHERE id=?",
                            (status, error, attempts, aid))
            self._pending_writes += 1
            if self._pending_writes >= 500:
                self.db.execute("COMMIT")
                self._pending_writes = 0

    def _flush_db(self):
        with self.db_lock:
            if self._pending_writes:
                self.db.execute("COMMIT")
                self._pending_writes = 0

    def _sample(self):
        self.samples.append((time.monotonic(), self.xfer_bytes, self.done_files))

    def rates(self) -> tuple[float, float]:
        """(bytes/s, files/s) over the last ~30 seconds."""
        if len(self.samples) < 2:
            return 0.0, 0.0
        t1, b1, f1 = self.samples[-1]
        for t0, b0, f0 in self.samples:
            if t1 - t0 <= 30:
                break
        dt = t1 - t0
        if dt <= 0:
            return 0.0, 0.0
        return (b1 - b0) / dt, (f1 - f0) / dt

    def eta(self) -> float | None:
        if self.phase not in (Phase.COPYING, Phase.DELETING) or self.paused:
            return None
        bps, fps = self.rates()
        rem_b = max(0, self.total_bytes - self.done_bytes)
        rem_f = max(0, self.total_files - self.done_files - self.failed)
        if rem_b == 0 and rem_f == 0:
            return 0.0
        estimates = []
        if bps > 0 and rem_b:
            estimates.append(rem_b / bps)
        if fps > 0 and rem_f:
            estimates.append(rem_f / fps)
        return max(estimates) if estimates else None

    def status(self) -> dict:
        bps, fps = self.rates()
        elapsed = ((self.ended or time.time()) - self.started) if self.started else 0
        if self.total_bytes:
            pct = self.done_bytes / self.total_bytes
        elif self.total_files:
            pct = self.done_files / self.total_files
        else:
            pct = 1.0 if self.phase in Phase.FINAL else 0.0
        with self.lock:
            current = [{"path": c[0], "done": c[1], "size": c[2]} for c in self.current.values()]
            warnings = [{"message": m, "count": n} for m, n in self.warnings.values()]
        return {
            "phase": self.phase, "paused": self.paused, "waiting_window": not self._in_window,
            "error": self.error, "dry_run": self.spec.dry_run,
            "source": self.spec.source, "dest": self.spec.dest, "mode": self.spec.mode,
            "started": self.started, "ended": self.ended, "elapsed": elapsed, "eta": self.eta(),
            "percent": round(pct * 100, 1),
            "total_files": self.total_files, "total_bytes": self.total_bytes,
            "done_files": self.done_files, "done_bytes": self.done_bytes,
            "failed": self.failed, "skipped": self.skipped, "deleted": self.deleted,
            "same_files": self.same_files, "scanned": self.scanned_entries,
            "speed": bps, "files_per_sec": fps,
            "plan": dict(self.plan_counts), "current": current, "warnings": warnings,
            "pending_confirmation": self.pending_confirmation,
            "bwlimit": current_bwlimit(self.bw_default, self.bw_schedule),
        }

    # --------------------------------------------------------------- run ---
    def run(self) -> str:
        self.started = self.started or time.time()
        sampler = threading.Thread(target=self._sampler, daemon=True)
        sampler_stop = threading.Event()
        self._sampler_stop = sampler_stop
        try:
            self._open_db()
            self._open_backends()
            planned = self._meta_get("planned") == "1"
            if planned:
                self.log.info("Resuming previous plan")
                self._load_totals()
            else:
                self.phase = Phase.SCANNING
                self.on_event("phase", self.phase)
                sampler.start()
                self._plan()
                self._meta_set("planned", "1")
                self._load_totals()
            if not sampler.is_alive():
                sampler.start()
            self._log_plan_summary()
            if self.spec.dry_run:
                self._dry_run_report()
                self.phase = Phase.DONE
                return self.phase
            self._maybe_confirm()
            self.phase = Phase.COPYING
            self.on_event("phase", self.phase)
            self._exec_mkdirs()
            self._exec_parallel(("copy", "update", "link", "src_delete"), self._do_file)
            self.phase = Phase.DELETING
            self._exec_parallel(("delete",), self._do_delete)
            self.phase = Phase.FINISHING
            self._exec_dirmeta()
            self._flush_db()
            self.phase = Phase.PARTIAL if self.failed else Phase.DONE
        except Cancelled:
            self.phase = Phase.INTERRUPTED if self._interrupted else Phase.CANCELLED
            self.log.info("Run %s", "interrupted (will resume)" if self._interrupted else "cancelled")
        except Exception as e:
            self.phase = Phase.FAILED
            self.error = f"{type(e).__name__}: {e}"
            self.log.exception("Run failed")
        finally:
            sampler_stop.set()
            self.ended = time.time()
            self._sample()
            try:
                self._flush_db()
                if self.db:
                    if self.phase != Phase.INTERRUPTED:
                        self._meta_set("final_phase", self.phase)
                    self.db.close()
            except Exception:
                pass
            for b in (self.src, self.dst):
                if b:
                    b.close()
            self.log.info("Finished: %s - %d files copied (%d bytes), %d failed, %d deleted, %d unchanged",
                          self.phase, self.done_files, self.done_bytes, self.failed, self.deleted, self.same_files)
        return self.phase

    def _sampler(self):
        while not self._sampler_stop.wait(1.0):
            self._sample()

    def _open_backends(self):
        self.src = open_backend(self.spec.source)
        self.dst = open_backend(self.spec.dest)
        if isinstance(self.src, LocalBackend) and os.path.isfile(self.src.root):
            # single file: copy just that file into the destination folder
            name = os.path.basename(self.src.root)
            self.src = LocalBackend(os.path.dirname(self.src.root))
            self.single_file = name
            if self.spec.mode == "mirror":
                raise ValueError("mirror needs a folder as the source, not a single file")
            from ferry.filters import Rule
            self.filter.include = [Rule("/" + name)]
            self.filter.max_depth = 0
        root = self.src.stat("", follow=True)
        if root is None or not root.is_dir:
            raise FileNotFoundError(f"source not found or not a folder: {self.spec.source}")
        if not self.spec.dry_run:
            self.dst.makedirs("")
        if self.meta.owner and not meta.is_elevated():
            self.warn("owner-elevation",
                      "Copying owners needs admin/root - owners will be skipped where not permitted")
        if self.meta.acl and meta.IS_WIN:
            try:
                import win32security  # noqa: F401
            except ImportError:
                self.warn("acl", "NTFS ACLs cannot be copied: pywin32 missing in this build")

    # -------------------------------------------------------------- plan ---
    def _plan(self):
        buf: list[tuple] = []

        def add(kind, rel, size=0, mtime_ns=0, is_dir=0, extra=None):
            buf.append((kind, rel, size, mtime_ns, is_dir, extra))
            self.plan_counts[kind] += 1
            if len(buf) >= 2000:
                flush()

        def flush():
            with self.db_lock:
                self.db.execute("BEGIN")
                self.db.executemany("INSERT INTO actions(kind, rel, size, mtime_ns, is_dir, extra) "
                                    "VALUES(?,?,?,?,?,?)", buf)
                self.db.execute("COMMIT")
            buf.clear()

        src, dst, spec, flt = self.src, self.dst, self.spec, self.filter
        window = max(spec.modify_window, src.mtime_window, dst.mtime_window)
        fold_dst = CASE_INSENSITIVE and isinstance(dst, LocalBackend)
        mirror = spec.mode == "mirror"
        move = spec.mode == "move"

        if self.scope:
            stack = [(d, dst.stat(d) is not None, False) for d in sorted(set(self.scope), reverse=True)]
        else:
            stack = [("", dst.stat("") is not None, True)]

        visited: set[str] = set()
        while stack:
            self._check()
            d, dst_exists, recurse = stack.pop()
            if self.scope:   # scoped (live-sync) scans may reach a folder twice
                if d in visited:
                    continue
                visited.add(d)
            try:
                src_entries = src.list_dir(d)
            except FileNotFoundError:
                if self.scope and d:
                    src_entries = None  # scoped dir vanished; parent scan handles deletion
                else:
                    raise
            except OSError as e:
                add("scan_error", d, extra=str(e))
                self.warn("scan", f"cannot read source folder {d or '/'}: {e}")
                continue
            if src_entries is None:
                continue
            self.scanned_entries += len(src_entries)
            if flt.ignore_file and any(e.name == flt.ignore_file for e in src_entries):
                text = src.read_text(join(d, flt.ignore_file))
                if text:
                    flt.add_ignore_file(d, text)
            dst_map = {}
            if dst_exists:
                try:
                    for e in dst.list_dir(d):
                        dst_map[e.name.casefold() if fold_dst else e.name] = e
                except FileNotFoundError:
                    pass
                except OSError as e:
                    self.warn("scan-dst", f"cannot read destination folder {d or '/'}: {e}")
            seen = set()
            subdirs = []
            for e in sorted(src_entries, key=lambda x: x.name):
                rel = join(d, e.name)
                key = e.name.casefold() if fold_dst else e.name
                de = dst_map.get(key)
                if e.is_link:
                    if self.meta.symlinks == "skip":
                        seen.add(key)
                        continue
                    if self.meta.symlinks == "follow":
                        t = src.stat(rel, follow=True)
                        if t is None:
                            self.warn("broken-link", f"broken symlink skipped: {rel}")
                            seen.add(key)
                            continue
                        t.name = e.name
                        e = t
                    else:
                        seen.add(key)
                        if not flt.file_allowed(rel):
                            continue
                        if de is not None and de.is_link and de.link_target == e.link_target:
                            self.same_files += 1
                            continue
                        add("link", rel, mtime_ns=e.mtime_ns, extra=e.link_target or "")
                        continue
                if e.is_dir:
                    seen.add(key)
                    if not flt.dir_allowed(rel):
                        continue
                    exists = de is not None and de.is_dir and not de.is_link
                    if not exists:
                        if de is not None:
                            add("delete", rel, is_dir=int(de.is_dir), extra="replace")
                        if spec.empty_dirs:
                            add("mkdir", rel, is_dir=1)
                        subdirs.append((rel, False, True))
                    elif recurse:
                        subdirs.append((rel, True, True))
                    add("dirmeta", rel, mtime_ns=e.mtime_ns, is_dir=1)
                    continue
                # regular file
                seen.add(key)
                if not flt.file_allowed(rel, e.size, e.mtime):
                    continue
                if de is None:
                    if spec.skip_new:
                        continue
                    add("copy", rel, e.size, e.mtime_ns)
                elif de.is_dir or de.is_link:
                    add("delete", rel, is_dir=int(de.is_dir), extra="replace")
                    add("copy", rel, e.size, e.mtime_ns)
                elif self._needs_update(rel, e, de, window):
                    add("update", rel, e.size, e.mtime_ns)
                else:
                    self.same_files += 1
                    if move:
                        add("src_delete", rel, e.size, e.mtime_ns)
            if mirror:
                for key, de in dst_map.items():
                    if key in seen:
                        continue
                    rel = join(d, de.name)
                    if flt.is_internal(de.name):
                        continue
                    if de.is_dir and not flt.dir_allowed(rel):
                        continue
                    if not de.is_dir and not flt.file_allowed(rel, de.size, de.mtime):
                        continue
                    add("delete", rel, de.size, is_dir=int(de.is_dir))
            # depth-first keeps the dirmeta order parent-before-child; push reversed for name order
            stack.extend(reversed(subdirs))
        if buf:
            flush()

    def _needs_update(self, rel, e, de, window) -> bool:
        ow = self.spec.overwrite
        if ow == "always":
            return True
        if ow == "never":
            return False
        window = max(window, mtime_granularity(de.mtime_ns))
        dt = (e.mtime_ns - de.mtime_ns) / 1e9
        if ow == "newer":
            return dt > window or (abs(dt) <= window and e.size != de.size)
        if e.size != de.size:
            return True
        if ow == "checksum":
            try:
                return self._hash(self.src, rel) != self._hash(self.dst, rel)
            except Exception:
                return True
        return abs(dt) > window

    def _load_totals(self):
        rows = self.db.execute("SELECT kind, status, COUNT(*), COALESCE(SUM(size),0) FROM actions "
                               "GROUP BY kind, status").fetchall()
        self.plan_counts = collections.Counter()
        self.total_files = self.total_bytes = self.done_files = self.done_bytes = 0
        self.failed = self.deleted = self.skipped = 0
        for kind, status, n, size in rows:
            self.plan_counts[kind] += n
            if kind in ("copy", "update", "link"):
                self.total_files += n
                self.total_bytes += size
                if status == DONE:
                    self.done_files += n
                    self.done_bytes += size
            if status == FAILED:
                self.failed += n
            if status == SKIPPED:
                self.skipped += n
            if kind == "delete" and status == DONE:
                self.deleted += n
        # failed actions get another chance when resuming
        with self.db_lock:
            self.db.execute("UPDATE actions SET status=0 WHERE status=2 AND kind!='scan_error'")
        self.failed = self.db.execute("SELECT COUNT(*) FROM actions WHERE status=2").fetchone()[0]

    def _log_plan_summary(self):
        c = self.plan_counts
        self.log.info("Plan: %d new, %d updated, %d links, %d folders, %d to delete, %d unchanged, %s to copy",
                      c["copy"], c["update"], c["link"], c["mkdir"], c["delete"], self.same_files,
                      self.total_bytes)

    def _dry_run_report(self):
        for kind, rel, size in self.db.execute(
                "SELECT kind, rel, size FROM actions WHERE kind NOT IN ('dirmeta') ORDER BY id"):
            self.log.info("[dry-run] %-10s %s%s", kind, rel, f" ({size} bytes)" if size else "")

    def _maybe_confirm(self):
        if self.spec.delete_mode != "confirm" or self._meta_get("confirmed"):
            return
        n_del = self.db.execute("SELECT COUNT(*) FROM actions WHERE kind='delete' AND status=0").fetchone()[0]
        n_upd = self.db.execute("SELECT COUNT(*) FROM actions WHERE kind='update' AND status=0").fetchone()[0]
        if not n_del and not n_upd:
            return
        sample = [r[0] for r in self.db.execute(
            "SELECT rel FROM actions WHERE kind IN ('delete','update') AND status=0 ORDER BY id LIMIT 15")]
        self.pending_confirmation = {"deletes": n_del, "overwrites": n_upd, "sample": sample}
        self.phase = Phase.CONFIRM
        self.log.info("Waiting for confirmation: %d deletions, %d overwrites", n_del, n_upd)
        self.on_event("confirm", self.pending_confirmation)
        self._confirm.wait()
        self._check()
        choice = self._confirm_choice or "all"
        if choice == "skip":
            with self.db_lock:
                self.db.execute("UPDATE actions SET status=3 WHERE kind IN ('delete','update') AND status=0")
                # a 'copy' that replaces a conflicting entry depends on its 'replace' delete
                self.db.execute("UPDATE actions SET status=3 WHERE kind='copy' AND status=0 AND rel IN "
                                "(SELECT rel FROM actions WHERE kind='delete' AND extra='replace')")
            self._load_totals()
            self.log.info("Confirmation: skipping deletions and overwrites")
        else:
            self.log.info("Confirmation: applying all changes")
        self._meta_set("confirmed", choice)
        self.pending_confirmation = None

    # ----------------------------------------------------------- execute ---
    def _iter_actions(self, kinds, order="id", where=""):
        desc = order != "id"
        q = (f"SELECT id, kind, rel, size, mtime_ns, is_dir, extra FROM actions WHERE status=0 AND kind IN "
             f"({','.join('?' * len(kinds))}) {where} AND id{'<' if desc else '>'}? "
             f"ORDER BY id{' DESC' if desc else ''} LIMIT 1000")
        last = (1 << 62) if desc else 0
        # page through by id so concurrent status updates don't disturb the cursor
        while True:
            with self.db_lock:
                rows = self.db.execute(q, (*kinds, last)).fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1][0]

    def _exec_mkdirs(self):
        # 'replace' deletes must run before the mkdir/copy that takes their place
        for row in self._iter_actions(("delete",), where="AND extra='replace'"):
            self._do_delete(row)
        for aid, _k, rel, *_ in self._iter_actions(("mkdir",)):
            self._gate()
            try:
                self.dst.makedirs(rel)
                self._set_status(aid, DONE)
            except Exception as e:
                self._set_status(aid, FAILED, str(e))
                with self.lock:
                    self.failed += 1
                self.log.error("mkdir failed %s: %s", rel, e)

    def _exec_parallel(self, kinds, fn):
        sem = threading.BoundedSemaphore(self.threads * 4)
        errors = []

        def wrapped(row):
            try:
                fn(row)
            except Cancelled:
                pass
            except Exception as e:  # pragma: no cover - fn handles its own errors
                errors.append(e)
                self.log.exception("unexpected error")
            finally:
                sem.release()

        with ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="ferry") as pool:
            for row in self._iter_actions(kinds):
                self._check()
                sem.acquire()
                if self._cancel.is_set():
                    sem.release()
                    break
                pool.submit(wrapped, row)
        self._flush_db()
        self._check()

    def _with_retries(self, row, func):
        aid, kind, rel = row[0], row[1], row[2]
        state = {"archived": False}
        last_err = None
        for attempt in range(self.retries + 1):
            self._gate()
            try:
                func(row, state)
                self._set_status(aid, DONE, None, attempt + 1)
                return True
            except Cancelled:
                raise
            except FileNotFoundError as e:
                if kind != "delete" and self.src.stat(rel) is None:
                    self.log.info("skipped (source vanished): %s", rel)
                    self._set_status(aid, SKIPPED, "source vanished")
                    with self.lock:
                        self.skipped += 1
                    return False
                last_err = e
            except Exception as e:
                last_err = e
            if attempt < self.retries:
                self.log.warning("retry %d/%d %s: %s", attempt + 1, self.retries, rel, last_err)
                end = time.monotonic() + self.retry_wait
                while time.monotonic() < end:
                    self._check()
                    time.sleep(0.2)
        self._set_status(aid, FAILED, f"{type(last_err).__name__}: {last_err}", self.retries + 1)
        with self.lock:
            self.failed += 1
        self.log.error("FAILED %s %s: %s", kind, rel, last_err)
        self.on_event("file_error", {"path": rel, "error": str(last_err)})
        return False

    # ----------------------------------------------------------- files ---
    def _do_file(self, row):
        aid, kind, rel, size = row[0], row[1], row[2], row[3]
        tid = threading.get_ident()
        with self.lock:
            self.current[tid] = [rel, 0, size]
        try:
            if kind == "link":
                ok = self._with_retries(row, self._make_link)
            elif kind == "src_delete":
                ok = self._with_retries(row, lambda r, s: self.src.remove_file(r[2]))
                if ok:
                    self.log.info("removed source (already at destination): %s", rel)
                return
            else:
                ok = self._with_retries(row, self._copy_file)
            if ok:
                with self.lock:
                    self.done_files += 1
                if kind != "link":
                    self.log.info("%s %s (%d bytes)", "copied" if kind == "copy" else "updated", rel, size)
        finally:
            with self.lock:
                self.current.pop(tid, None)

    def _progress(self, n: int):
        with self.lock:
            self.done_bytes += n
            self.xfer_bytes += n
            c = self.current.get(threading.get_ident())
            if c:
                c[1] += n

    def _make_link(self, row, state):
        rel, target = row[2], row[6]
        try:
            self.dst.make_symlink(target, rel)
        except OSError as e:
            if meta.IS_WIN and getattr(e, "winerror", None) == 1314:
                raise OSError("creating symlinks on Windows needs Developer Mode or admin rights") from None
            raise
        src_p, dst_p = self.src.local_path(rel), self.dst.local_path(rel)
        if src_p and dst_p:
            meta.apply_local(src_p, dst_p, os.lstat(src_p), self.meta, self.warn, is_link=True)

    def _archive(self, rel: str):
        target = f"{self.spec.archive_dir}/{self.archive_stamp}/{rel}"
        self.dst.rename(rel, target)
        self.log.info("archived old version: %s -> %s", rel, target)

    def _copy_file(self, row, state):
        aid, kind, rel, size, mtime_ns = row[0], row[1], row[2], row[3], row[4]
        src, dst = self.src, self.dst
        se = src.stat(rel, follow=self.meta.symlinks == "follow")
        if se is None:
            raise FileNotFoundError(rel)
        if se.size != size or se.mtime_ns != mtime_ns:
            size = se.size  # changed since planning: copy the current version
            with self.lock:
                self.total_bytes += se.size - row[3]
        counted = [0]

        def progress(n):
            counted[0] += n
            self._progress(n)

        try:
            de = dst.stat(rel) if kind == "update" else None
            if de is not None and kind == "update" and not state["archived"] and self.spec.delete_mode == "archive":
                self._archive(rel)
                state["archived"] = True
                de = None
            if isinstance(src, RcloneBackend) or isinstance(dst, RcloneBackend):
                self._copy_rclone(rel, size, progress)
                return self._after_copy(rel, se, None)
            use_delta = (de is not None and not de.is_dir and src.local_path(rel) and dst.local_path(rel)
                         and self.spec.delta != "off" and self.spec.delete_mode != "archive"
                         and (self.spec.delta == "on" or (size >= DELTA_MIN and de.size > 0)))
            if use_delta:
                src_hash = self._copy_delta(rel, size, progress)
            else:
                src_hash = self._copy_stream(rel, size, mtime_ns, progress)
            self._after_copy(rel, se, src_hash)
        except BaseException:
            with self.lock:   # roll back progress of the failed attempt
                self.done_bytes -= counted[0]
            raise

    def _copy_stream(self, rel, size, mtime_ns, progress) -> str | None:
        src, dst = self.src, self.dst
        name = rel.rsplit("/", 1)[-1]
        tag = hashlib.md5(f"{size}:{mtime_ns}".encode()).hexdigest()[:8]
        part = join(parent(rel), f".{name}.{tag}{PART_SUFFIX}")
        offset = 0
        if size >= RESUME_MIN:
            ps = dst.part_size(part)
            if ps and ps <= size:
                offset = (ps // CHUNK) * CHUNK
                if offset:
                    self.log.info("resuming %s at %d bytes", rel, offset)
        h = _hasher(self.spec.verify) if self.spec.verify != "none" and offset == 0 else None
        try:
            fi = src.open_read(rel, offset)
            try:
                fo = dst.open_write_part(part, offset)
            except FileNotFoundError:
                fi.close()
                dst.makedirs(parent(rel))   # parent folder not created yet (e.g. /S mode)
                fi = src.open_read(rel, offset)
                fo = dst.open_write_part(part, offset)
            with fi, fo:
                if offset:
                    with self.lock:
                        self.done_bytes += offset
                    c = self.current.get(threading.get_ident())
                    if c:
                        c[1] += offset
                while True:
                    self._gate()
                    buf = fi.read(CHUNK)
                    if not buf:
                        break
                    self.bucket.consume(len(buf), self._check)
                    fo.write(buf)
                    if h:
                        h.update(buf)
                    progress(len(buf))
        except BaseException:
            if offset:
                with self.lock:
                    self.done_bytes -= offset
            if size < RESUME_MIN:
                try:
                    dst.remove_file(part)
                except Exception:
                    pass
            raise
        dst.commit_part(part, rel)
        return h.hexdigest() if h else None

    def _copy_delta(self, rel, size, progress) -> None:
        sp, dp = self.src.local_path(rel), self.dst.local_path(rel)
        meta.make_writable(dp)
        written = 0
        with open(sp, "rb") as fi, open(dp, "r+b") as fo:
            pos = 0
            while True:
                self._gate()
                a = fi.read(DELTA_BLOCK)
                if not a:
                    break
                b = fo.read(len(a))
                if a != b:
                    self.bucket.consume(len(a), self._check)
                    fo.seek(pos)
                    fo.write(a)
                    written += len(a)
                pos += len(a)
                progress(len(a))
            fo.truncate(pos)
        self.log.info("delta update %s: rewrote %d of %d bytes", rel, written, size)
        return None

    def _copy_rclone(self, rel, size, progress):
        src, dst = self.src, self.dst
        if isinstance(dst, RcloneBackend) and isinstance(src, RcloneBackend):
            dst.copy_from(src.spec, rel, rel)
        elif isinstance(dst, RcloneBackend) and isinstance(src, LocalBackend):
            dst.copy_from(src.root, rel, rel)
        elif isinstance(src, RcloneBackend) and isinstance(dst, LocalBackend):
            src.copy_to(rel, dst.root, rel)
        else:
            raise OSError("cloud <-> SFTP: configure the SSH host as an rclone remote instead")
        progress(size)

    def _after_copy(self, rel, se, src_hash):
        src, dst = self.src, self.dst
        sp, dp = src.local_path(rel), dst.local_path(rel)
        if sp and dp:
            meta.apply_local(sp, dp, os.stat(sp) if self.meta.symlinks == "follow" else os.lstat(sp),
                             self.meta, self.warn)
        elif not isinstance(dst, RcloneBackend):
            if self.meta.permissions and se.mode and not meta.IS_WIN:
                try:
                    dst.set_mode(rel, se.mode)
                except Exception as e:
                    self.warn("mode", f"permission bits not copied: {e}")
            if self.meta.timestamps:
                dst.set_times(rel, se.mtime_ns)
        if self.spec.verify != "none":
            if isinstance(src, RcloneBackend) or isinstance(dst, RcloneBackend):
                self.warn("verify-rclone", "cloud transfers are verified by rclone's own checksums")
            else:
                sh = src_hash or self._hash(src, rel)
                dh = self._hash(dst, rel)
                if sh != dh:
                    raise VerifyError(f"checksum mismatch after copy ({sh[:12]} != {dh[:12]})")
        if self.spec.mode == "move":
            src.remove_file(rel)

    def _hash(self, backend, rel) -> str:
        h = _hasher(self.spec.verify if self.spec.verify != "none" else "xxhash")
        with backend.open_read(rel) as f:
            while True:
                self._check()
                buf = f.read(CHUNK)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()

    # --------------------------------------------------------- deletes ---
    def _do_delete(self, row):
        aid, rel, is_dir, extra = row[0], row[2], row[5], row[6]

        def go(r, state):
            if self.dst.stat(rel) is None:
                return
            if self.spec.delete_mode == "archive":
                self._archive(rel)
                return
            if is_dir:
                self.dst.remove_tree(rel)
            else:
                self.dst.remove_file(rel)
            if extra != "replace":
                self.log.info("deleted extra %s %s", "folder" if is_dir else "file", rel)

        if self._with_retries(row, go):
            with self.lock:
                self.deleted += 1

    # -------------------------------------------------------- dir meta ---
    def _exec_dirmeta(self):
        move_dirs = self.spec.mode == "move" and self.spec.move_dirs
        if not (self.meta.timestamps or self.meta.permissions or self.meta.acl or move_dirs):
            return
        sp_root = self.src.local_path("")
        for aid, _k, rel, _s, mtime_ns, *_ in self._iter_actions(("dirmeta",), order="id DESC"):
            self._check()
            try:
                if move_dirs:
                    try:
                        self.src.remove_dir(rel)
                    except OSError:
                        pass
                sp, dp = self.src.local_path(rel), self.dst.local_path(rel)
                if sp and dp and os.path.isdir(dp):
                    if os.path.isdir(sp):
                        meta.apply_local(sp, dp, os.stat(sp), self.meta, self.warn, is_dir=True)
                    elif self.meta.timestamps:
                        os.utime(dp, ns=(mtime_ns, mtime_ns))
                elif self.meta.timestamps and not isinstance(self.dst, RcloneBackend) and self.dst.stat(rel):
                    self.dst.set_times(rel, mtime_ns)
                self._set_status(aid, DONE)
            except Exception as e:
                self._set_status(aid, DONE, f"metadata: {e}")
                self.warn("dirmeta", f"folder metadata not fully copied (e.g. {rel}): {e}")
        # root folder metadata
        if sp_root and self.dst.local_path("") and not self.single_file:
            try:
                meta.apply_local(sp_root, self.dst.local_path(""), os.stat(sp_root), self.meta, self.warn,
                                 is_dir=True)
            except Exception:
                pass
        self._flush_db()


def read_failures(run_dir: str, limit: int = 1000) -> list[dict]:
    path = os.path.join(run_dir, "plan.db")
    if not os.path.exists(path):
        return []
    db = sqlite3.connect(path)
    try:
        return [{"kind": k, "path": r, "error": e, "attempts": a} for k, r, e, a in db.execute(
            "SELECT kind, rel, error, attempts FROM actions WHERE status=2 ORDER BY id LIMIT ?", (limit,))]
    finally:
        db.close()
