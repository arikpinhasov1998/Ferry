"""Command line interface.

    ferry copy   SRC DST [options]      copy new/changed files
    ferry mirror SRC DST [options]      make DST identical to SRC
    ferry move   SRC DST [options]      copy, then remove from SRC
    ferry robo   SRC DST [files] [/MIR /E /R:3 ...]   robocopy syntax
    ferry SRC DST /MIR ...              same as 'robo'

Runs go to the background service by default (so Telegram, the TUI and the GUI
can see them); the terminal follows the progress. Use --local to run inside
this process only, or --bg to start and return immediately.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
from datetime import datetime

from ferry import __version__
from ferry.util import human_bytes, human_duration, progress_bar

SUBCOMMANDS = {"copy", "mirror", "move", "robo", "job", "jobs", "status", "runs", "history", "pause", "resume",
               "cancel", "confirm", "errors", "log", "daemon", "service", "telegram", "config", "tui", "gui",
               "version", "help", "-h", "--help", "--version"}

FINAL = {"done", "completed_with_errors", "failed", "cancelled", "interrupted"}


def _out(msg="", end="\n"):
    sys.stdout.write(msg + end)
    sys.stdout.flush()


def _err(msg):
    sys.stderr.write(f"ferry: {msg}\n")


# ------------------------------------------------------------- job options --
def add_job_args(p: argparse.ArgumentParser):
    g = p.add_argument_group("behaviour")
    g.add_argument("--name", help="job name (for saved jobs and display)")
    g.add_argument("--delete-mode", choices=["delete", "archive", "confirm"],
                   help="what happens to destination files that are deleted/overwritten "
                        "(archive: move to .ferry-archive/<date>/; confirm: ask first). Default: archive")
    g.add_argument("--overwrite", choices=["different", "newer", "always", "never", "checksum"],
                   help="when to replace existing destination files (default: different)")
    g.add_argument("--verify", nargs="?", const="xxhash", choices=["none", "xxhash", "sha256"],
                   help="verify copies by checksum (default algorithm: xxhash)")
    g.add_argument("-t", "--threads", type=int, help="parallel transfers (default 8)")
    g.add_argument("--retries", type=int, help="retries per file (default 3)")
    g.add_argument("--retry-wait", type=float, help="seconds between retries (default 5)")
    g.add_argument("--bwlimit", help="bandwidth limit, e.g. 20M (bytes/s)")
    g.add_argument("--bw-schedule", action="append", metavar="WINDOW=LIMIT",
                   help="time-based limit, e.g. 'mon-fri 09:00-18:00=5M' (repeatable)")
    g.add_argument("--window", action="append", metavar="WINDOW",
                   help="only transfer inside this time window, e.g. '22:00-06:00' (repeatable)")
    g.add_argument("--schedule", metavar="CRON", help="cron schedule for saved jobs, e.g. '0 2 * * *' or @daily")
    g.add_argument("--watch", action="store_true", default=None, help="saved jobs: live sync on changes")
    g.add_argument("--no-empty-dirs", action="store_true", default=None, help="don't create empty folders")
    g.add_argument("--move-dirs", action="store_true", default=None, help="move mode: remove emptied folders")
    g.add_argument("--skip-new", action="store_true", default=None, help="only update existing files")
    g.add_argument("--delta", choices=["auto", "on", "off"], help="in-place update of large changed files")
    g.add_argument("--modify-window", type=float, help="timestamp tolerance in seconds (FAT: 2)")
    g.add_argument("-n", "--dry-run", action="store_true", default=None, help="show what would happen")
    g.add_argument("--no-notify", action="store_true", default=None, help="no Telegram notifications")
    f = p.add_argument_group("filters (gitignore-style patterns)")
    f.add_argument("-i", "--include", action="append", help="only files matching (repeatable)")
    f.add_argument("-x", "--exclude", action="append", help="exclude files/folders matching (repeatable)")
    f.add_argument("--exclude-dir", action="append", help="exclude folders matching (repeatable)")
    f.add_argument("--min-size")
    f.add_argument("--max-size")
    f.add_argument("--min-age", help="skip files changed more recently than this (e.g. 1h)")
    f.add_argument("--max-age", help="skip files older than this (e.g. 30d)")
    f.add_argument("--max-depth", type=int, help="folder levels to descend (0 = top folder only)")
    f.add_argument("--no-ignore-file", action="store_true", default=None, help="ignore .ferryignore files")
    m = p.add_argument_group("metadata")
    m.add_argument("--no-times", action="store_true", default=None)
    m.add_argument("--no-perms", action="store_true", default=None, help="don't copy mode bits/attributes")
    m.add_argument("--no-acl", action="store_true", default=None, help="don't copy NTFS permissions")
    m.add_argument("--owner", action="store_true", default=None, help="copy owner/group (needs admin/root)")
    m.add_argument("--no-xattrs", action="store_true", default=None)
    m.add_argument("--symlinks", choices=["copy", "follow", "skip"])


def apply_job_args(d: dict, a) -> dict:
    """Overlay the options the user actually gave onto a spec dict."""
    def put(key, val):
        if val is not None:
            d[key] = val

    put("name", a.name)
    put("delete_mode", a.delete_mode)
    put("overwrite", a.overwrite)
    put("verify", a.verify)
    put("threads", a.threads)
    put("retries", a.retries)
    put("retry_wait", a.retry_wait)
    put("bwlimit", a.bwlimit)
    if a.bw_schedule:
        items = []
        for s in a.bw_schedule:
            w, _, lim = s.rpartition("=")
            if not w:
                raise ValueError(f"--bw-schedule expects WINDOW=LIMIT, got {s!r}")
            items.append({"window": w, "limit": lim})
        d["bw_schedule"] = items
    if a.window:
        d["windows"] = a.window
    put("schedule", a.schedule)
    put("watch", a.watch)
    if a.no_empty_dirs:
        d["empty_dirs"] = False
    put("move_dirs", a.move_dirs)
    put("skip_new", a.skip_new)
    put("delta", a.delta)
    put("modify_window", a.modify_window)
    put("dry_run", a.dry_run)
    if a.no_notify:
        d["notify"] = False
    flt = dict(d.get("filters") or {})
    for key, val in (("include", a.include), ("exclude", a.exclude), ("exclude_dirs", a.exclude_dir),
                     ("min_size", a.min_size), ("max_size", a.max_size), ("min_age", a.min_age),
                     ("max_age", a.max_age), ("max_depth", a.max_depth)):
        if val is not None:
            flt[key] = val
    if a.no_ignore_file:
        flt["ignore_file"] = None
    d["filters"] = flt
    md = dict(d.get("metadata") or {})
    if a.no_times:
        md["timestamps"] = False
    if a.no_perms:
        md["permissions"] = False
    if a.no_acl:
        md["acl"] = False
    if a.owner:
        md["owner"] = True
    if a.no_xattrs:
        md["xattrs"] = False
    if a.symlinks:
        md["symlinks"] = a.symlinks
    d["metadata"] = md
    return d


# --------------------------------------------------------------- progress --
def render_line(info: dict, width: int) -> str:
    phase = info["phase"]
    if info.get("paused") and phase not in FINAL:
        phase = "paused"
    elif info.get("waiting_window") and phase not in FINAL:
        phase = "waiting for time window"
    if info["phase"] == "scanning":
        s = f"scanning… {info.get('scanned', 0):,} entries · {human_duration(info['elapsed'])}"
    else:
        eta = info.get("eta")
        s = (f"{progress_bar(info['percent'] / 100, 20)} {info['percent']:5.1f}% "
             f"{human_bytes(info['done_bytes'])}/{human_bytes(info['total_bytes'])} "
             f"{info['done_files']:,}/{info['total_files']:,} files "
             f"{human_bytes(info.get('speed', 0))}/s "
             f"⏱ {human_duration(info['elapsed'])}"
             f"{' ⏳ ' + human_duration(eta) if eta is not None else ''}"
             f"{' ❌' + str(info['failed']) if info.get('failed') else ''} [{phase}]")
    return s[:max(20, width - 1)]


def summary(info: dict) -> str:
    lines = [
        f"Result: {info['phase'].replace('_', ' ')}{' (dry run)' if info.get('dry_run') else ''}",
        f"  {info['source']} -> {info['dest']} ({info['mode']})",
        f"  copied   {info['done_files']:,} files, {human_bytes(info['done_bytes'])}",
        f"  same     {info.get('same_files', 0):,} files",
        f"  failed   {info.get('failed', 0):,}",
        f"  deleted  {info.get('deleted', 0):,}",
        f"  time     {human_duration(info['elapsed'])}"
        + (f", avg {human_bytes(info['done_bytes'] / info['elapsed'])}/s" if info['elapsed'] else ""),
    ]
    plan = info.get("plan") or {}
    if info.get("dry_run"):
        lines.append(f"  plan     {plan.get('copy', 0)} new, {plan.get('update', 0)} update, "
                     f"{plan.get('delete', 0)} delete, {plan.get('mkdir', 0)} folders")
    for w in info.get("warnings") or []:
        lines.append(f"  warning  {w['message']}" + (f" (x{w['count']})" if w["count"] > 1 else ""))
    if info.get("error"):
        lines.append(f"  error    {info['error']}")
    if info.get("log"):
        lines.append(f"  log      {info['log']}")
    if info.get("dry_run"):
        lines += dry_run_details(info)
    return "\n".join(lines)


def dry_run_details(info: dict, limit: int = 40) -> list[str]:
    """The planned changes of a dry run, read from its log, so people see *what* would happen."""
    path = info.get("log")
    if not path or not os.path.exists(path):
        return []
    planned = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "[dry-run]" in line:
                planned.append(line.split("[dry-run]", 1)[1].strip())
    if not planned:
        return ["", "Nothing would change: the destination is already up to date."]
    out = ["", "This is what would happen (nothing was changed):"]
    out += [f"  {x}" for x in planned[:limit]]
    if len(planned) > limit:
        out.append(f"  … and {len(planned) - limit:,} more (see the log)")
    return out


def follow(get_info, control, quiet=False) -> dict:
    """Show live progress until the run ends. control: dict(cancel=, confirm=)."""
    tty = sys.stdout.isatty()
    last_print = time.time()         # not a terminal: a status line every 30 s, starting after 30 s
    info = get_info()
    while True:
        try:
            info = get_info()
            if info["phase"] == "awaiting_confirmation":
                pc = info.get("pending_confirmation") or {}
                if tty:
                    _out("\r" + " " * (shutil.get_terminal_size().columns - 1) + "\r", end="")
                _out(f"\nConfirmation needed: {pc.get('deletes', 0)} deletions, {pc.get('overwrites', 0)} overwrites")
                for s in pc.get("sample", [])[:15]:
                    _out(f"   {s}")
                if sys.stdin.isatty():
                    ans = input("Apply all [a] / skip deletions+overwrites [s] / cancel [c]? ").strip().lower()
                    control["confirm"]({"a": "all", "s": "skip"}.get(ans[:1], "cancel"))
                else:
                    _out("Not interactive: waiting for confirmation from GUI/TUI/Telegram/'ferry confirm'…")
                    while get_info()["phase"] == "awaiting_confirmation":
                        time.sleep(2)
                continue
            if info["phase"] in FINAL:
                break
            if not quiet:
                if tty:
                    w = shutil.get_terminal_size().columns
                    _out("\r" + render_line(info, w).ljust(w - 1), end="")
                elif time.time() - last_print > 30:
                    _out(render_line(info, 200))
                    last_print = time.time()
            time.sleep(0.5)
        except KeyboardInterrupt:
            _out("")
            choice = "c"
            if control.get("detach") and sys.stdin.isatty():
                try:
                    choice = input("Cancel the run [c] or keep it running in the background [b]? ").strip().lower()[:1]
                except (KeyboardInterrupt, EOFError):
                    choice = "c"
            if choice == "b":
                _out(f"Still running in the background. Check with: ferry status {info['id']}")
                info["detached"] = True
                return info
            control["cancel"]()
            _out("Cancelling…")
    if tty and not quiet:
        w = shutil.get_terminal_size().columns
        _out("\r" + render_line(info, w).ljust(w - 1))
    return info


def run_spec(spec_dict: dict, a, quiet=False) -> dict:
    """Run a job via the daemon (default) or in-process (--local)."""
    from ferry.spec import JobSpec, full_location
    spec_dict = dict(spec_dict, source=full_location(spec_dict["source"]), dest=full_location(spec_dict["dest"]))
    spec = JobSpec.from_dict(spec_dict).validate()
    if not getattr(a, "local", False):
        try:
            from ferry.daemon import ensure_daemon
            client = ensure_daemon()
            info = client.post("/run", {"spec": spec.to_dict(), "trigger": "cli"})
            rid = info["id"]
            if getattr(a, "bg", False):
                _out(f"Started run {rid} in the background service. Follow with: ferry status {rid}")
                return info
            return follow(lambda: client.get(f"/runs/{rid}"),
                          {"cancel": lambda: client.post(f"/runs/{rid}/cancel"),
                           "confirm": lambda c: client.post(f"/runs/{rid}/confirm", {"choice": c}),
                           "detach": True}, quiet)
        except Exception as e:
            if getattr(a, "bg", False):
                raise
            _err(f"background service unavailable ({e}); running in this process")
    from ferry.jobs import RunManager
    mgr = RunManager()
    run = mgr.start(spec, trigger="cli")
    info = follow(run.info, {"cancel": run.engine.cancel, "confirm": run.engine.confirm}, quiet)
    run.thread.join()
    return run.info()


def _exit_code(info) -> int:
    if info.get("detached"):
        return 0
    return {"done": 0, "completed_with_errors": 1, "cancelled": 3}.get(info["phase"], 2)


# ---------------------------------------------------------------- commands --
def cmd_transfer(a) -> int:
    d = {"source": a.source, "dest": a.dest, "mode": a.command}
    apply_job_args(d, a)
    if a.save:
        from ferry.jobs import JobStore
        from ferry.spec import JobSpec, full_location
        d["name"] = a.save
        d["source"], d["dest"] = full_location(d["source"]), full_location(d["dest"])
        job = JobStore().put(JobSpec.from_dict(d))
        _out(f"Saved job '{job.name}' ({job.id}).")
        if a.save_only:
            return 0
        d = job.to_dict()
    info = run_spec(d, a, quiet=a.quiet)
    if not info.get("detached") and not a.bg:
        _out(summary(info))
    return _exit_code(info)


ROBO_HELP = """usage: ferry robo SOURCE DEST [file [file]...] [/options] [--local] [--bg]
       ferry SOURCE DEST /options          (same, "robo" can be left out)

Robocopy-compatible: the options mean what they mean in robocopy.
Without /S, /E or /MIR only the top folder is copied. /MIR deletes directly (no archive).
Exit code is robocopy's: 0 nothing to do, 1 copied, 2 extras deleted, 8+ failures.

Supported:
  /S /E /LEV:n                 subfolders, incl. empty ones, depth
  /MIR /PURGE                  mirror / delete extra destination files
  /MOV /MOVE                   move files / files and folders
  /COPY:DATSOU /COPYALL /SEC /DCOPY:DAT   what to copy
  /SL /SJ /XJ                  links as links / junctions
  /XF f.. /XD d.. /IF f..      exclude files / exclude folders / include files
  /XO /XN /XC /IS /IT /XX /XL  older/newer/changed/same/tweaked/extra/lonely
  /MAX:n /MIN:n /MAXAGE:n /MINAGE:n /MAXLAD:n /MINLAD:n   size and age
  /R:n /W:n /MT[:n]            retries, wait seconds, threads
  /L                           list only (dry run)
  /LOG:f /LOG+:f /UNILOG:f /TEE  log file
  /IPG:ms /RH:hhmm-hhmm        inter-packet gap / run hours
  /FFT                         2-second time tolerance
  /MON:n /MOT:m                keep watching for changes (live sync)
Display options (/NP /NFL /NDL /NJH /NJS /V /ETA /BYTES ...) are accepted and ignored.

Examples:
  ferry robo C:\\src D:\\dst /MIR /R:2 /W:5 /XD node_modules /MT:16 /LOG:copy.log
  ferry robo C:\\src D:\\dst *.docx /S /XO
"""


def cmd_robo(argv: list[str], a_local=False, a_bg=False) -> int:
    from ferry import robocopy
    r = robocopy.parse(argv)
    for w in r.warnings:
        _err(w)
    ns = argparse.Namespace(local=a_local, bg=a_bg)
    info = run_spec(r.spec, ns)
    if info.get("detached"):
        return 0
    text = summary(info)
    _out(text)
    if r.log_file:
        log_text = ""
        if info.get("log") and os.path.exists(info["log"]):
            with open(info["log"], encoding="utf-8", errors="replace") as f:
                log_text = f.read()
        with open(r.log_file, "a" if r.log_append else "w", encoding="utf-8") as f:
            f.write(log_text + "\n" + text + "\n")
    return robocopy.exit_code(info)


def _client(start=False):
    from ferry.api import Client
    if start:
        from ferry.daemon import ensure_daemon
        return ensure_daemon()
    c = Client()
    if not c.alive():
        raise RuntimeError("the background service is not running (start it: ferry daemon start)")
    return c


def cmd_job(a) -> int:
    from ferry.jobs import JobStore
    from ferry.spec import JobSpec, full_location
    store = JobStore()
    sub = a.job_cmd or "list"
    if sub == "list":
        jobs = store.list()
        if a.json:
            _out(json.dumps([j.to_dict() for j in jobs], indent=2))
            return 0
        if not jobs:
            _out("No saved jobs. Create one: ferry job add NAME SRC DST [options]")
        for j in jobs:
            extra = []
            if j.schedule:
                extra.append(f"schedule={j.schedule}")
            if j.watch:
                extra.append("live")
            if not j.enabled:
                extra.append("disabled")
            _out(f"{j.name:20} {j.mode:7} {j.source} -> {j.dest}  {' '.join(extra)}")
        return 0
    if sub == "add":
        d = {"name": a.job_name, "source": full_location(a.source), "dest": full_location(a.dest), "mode": a.mode}
        apply_job_args(d, a)
        job = store.put(JobSpec.from_dict(d))
        _out(f"Saved job '{job.name}' ({job.id}).")
        return 0
    job = store.get(a.job_name)
    if not job:
        raise KeyError(f"no such job: {a.job_name}")
    if sub == "show":
        _out(json.dumps(job.to_dict(), indent=2))
    elif sub == "edit":
        d = job.to_dict()
        if a.source:
            d["source"] = full_location(a.source)
        if a.dest:
            d["dest"] = full_location(a.dest)
        if a.mode:
            d["mode"] = a.mode
        if a.disable:
            d["enabled"] = False
        if a.enable:
            d["enabled"] = True
        if a.no_schedule:
            d["schedule"] = None
        if a.no_watch:
            d["watch"] = False
        apply_job_args(d, a)
        store.put(JobSpec.from_dict(d))
        _out(f"Updated job '{d['name']}'.")
    elif sub == "remove":
        store.delete(job.id)
        _out(f"Removed job '{job.name}'.")
    elif sub == "run":
        if a.local:
            d = job.to_dict()
            if a.dry_run:
                d["dry_run"] = True
            info = run_spec(d, a, quiet=a.quiet)
        else:
            c = _client(start=True)
            info = c.post(f"/jobs/{job.id}/run", {"dry_run": bool(a.dry_run), "trigger": "cli"})
            rid = info["id"]
            if a.bg:
                _out(f"Started run {rid}. Follow with: ferry status {rid}")
                return 0
            info = follow(lambda: c.get(f"/runs/{rid}"),
                          {"cancel": lambda: c.post(f"/runs/{rid}/cancel"),
                           "confirm": lambda ch: c.post(f"/runs/{rid}/confirm", {"choice": ch}),
                           "detach": True}, a.quiet)
        if not info.get("detached"):
            _out(summary(info))
        return _exit_code(info)
    return 0


def cmd_status(a) -> int:
    c = _client()
    if a.run:
        if a.follow:
            info = follow(lambda: c.get(f"/runs/{a.run}"),
                          {"cancel": lambda: c.post(f"/runs/{a.run}/cancel"),
                           "confirm": lambda ch: c.post(f"/runs/{a.run}/confirm", {"choice": ch}),
                           "detach": True})
        else:
            info = c.get(f"/runs/{a.run}")
        if a.json:
            _out(json.dumps(info, indent=2))
        else:
            _out(summary(info) if info["phase"] in FINAL else
                 f"{info['job_name']} ({info['id']})\n  " + render_line(info, 200))
        return 0
    st = c.get("/status")
    if a.json:
        _out(json.dumps(st, indent=2))
        return 0
    _out(f"Ferry {st['version']} on {st['machine']} · up {human_duration(st['uptime'])} · "
         f"{st['jobs']} jobs · Telegram {'on' if st['telegram'] else 'off'}"
         f"{' · live: ' + str(len(st['watching'])) if st['watching'] else ''}")
    if not st["active"]:
        _out("No runs active.")
    for r in st["active"]:
        _out(f"\n{r['job_name']}  [{r['id']}]  {r['source']} -> {r['dest']}")
        _out("  " + render_line(r, 160))
        for cur in r.get("current", [])[:5]:
            _out(f"    · {cur['path']} ({human_bytes(cur['done'])}/{human_bytes(cur['size'])})")
    return 0


def cmd_runs(a) -> int:
    c = _client()
    for r in c.get("/runs"):
        _out(f"{r['id']}  {r['job_name']:20} {r['phase']:22} {r['percent']:5.1f}%  "
             f"{human_duration(r['elapsed'])}")
    return 0


def cmd_history(a) -> int:
    from ferry.jobs import RunManager
    for h in RunManager().history(a.limit):
        when = datetime.fromtimestamp(h["started"]).strftime("%Y-%m-%d %H:%M") if h.get("started") else "?"
        _out(f"{when}  {h['id']}  {h['job_name']:20} {h['phase']:22} {h['done_files']:>8,} files "
             f"{human_bytes(h['done_bytes']):>10}  {human_duration(h['elapsed'])}"
             f"{'  failed=' + str(h['failed']) if h.get('failed') else ''}")
    return 0


def cmd_control(a) -> int:
    c = _client()
    key = a.run
    if a.command in ("pause", "resume") and key == "all":
        c.post(f"/{a.command}_all")
        _out(f"All runs {a.command}d.")
        return 0
    if a.command == "confirm":
        info = c.post(f"/runs/{key}/confirm", {"choice": "skip" if a.skip else ("cancel" if a.reject else "all")})
    else:
        info = c.post(f"/runs/{key}/{a.command}")
    _out(f"{info['job_name']} ({info['id']}): {a.command} ok")
    return 0


def cmd_errors(a) -> int:
    from ferry.jobs import RunManager
    try:
        fails = _client().get(f"/runs/{a.run}/failures?limit={a.limit}")
    except Exception:
        fails = RunManager().failures(a.run, a.limit)
    if not fails:
        _out("No failed items.")
    for f in fails:
        _out(f"{f['kind']:8} {f['path']}\n         {f['error']}")
    return 0


def cmd_log(a) -> int:
    from ferry.jobs import RunManager
    p = RunManager().log_path(a.run)
    if not p:
        try:
            _out(_client().call("GET", f"/runs/{a.run}/log?tail={a.tail or 0}", raw=True))
            return 0
        except Exception:
            raise KeyError(f"no log for run {a.run}")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-a.tail:] if a.tail else lines:
        _out(line)
    return 0


def cmd_daemon(a) -> int:
    from ferry.api import Client
    from ferry.config import data_dir
    from ferry.daemon import Daemon, spawn_detached
    sub = a.daemon_cmd
    if sub == "run":
        return Daemon().run_forever()
    c = Client()
    if sub == "start":
        if c.alive():
            _out("Already running.")
            return 0
        ok = spawn_detached()
        _out("Started." if ok else f"Failed to start; see {data_dir() / 'logs'}")
        return 0 if ok else 2
    if sub in ("stop", "restart"):
        if c.alive():
            c.post("/shutdown")
            for _ in range(60):
                if not c.alive():
                    break
                time.sleep(0.5)
            _out("Stopped.")
        else:
            _out("Not running.")
        if sub == "restart":
            ok = spawn_detached()
            _out("Started." if ok else "Failed to start.")
        return 0
    if sub == "status":
        if c.alive():
            p = c.get("/ping")
            _out(f"Running: Ferry {p['version']} pid {p['pid']} on {p['machine']} ({p['platform']})")
        else:
            _out("Not running.")
        _out(f"Data folder: {data_dir()}")
        return 0
    return 1


def cmd_service(a) -> int:
    from ferry import service
    if a.service_cmd == "install":
        _out(service.install())
    elif a.service_cmd == "uninstall":
        _out(service.uninstall())
    else:
        installed, where = service.status()
        _out(f"Installed: {where}" if installed else "Not installed (ferry service install)")
    return 0


def cmd_telegram(a) -> int:
    from ferry.api import Client
    from ferry.config import Config
    from ferry.telegram import TelegramApi
    cfg = Config()
    sub = a.tg_cmd or "status"

    def reload_daemon():
        c = Client(cfg)
        if c.alive():
            c.post("/config", {"telegram.enabled": cfg.get("telegram.enabled")})

    if sub == "status":
        tg = cfg.get("telegram")
        _out(f"enabled: {tg['enabled']}\ntoken: {'set' if tg['token'] else 'not set'}\n"
             f"allowed IDs: {tg['allowed_ids']}\nnotify chats: {tg['notify_chat_ids'] or '(allowed IDs)'}\n"
             f"progress: every {tg['progress_every_percent']}% / {tg['progress_every_minutes']} min\n"
             f"machine name: {cfg.get('machine_name')}")
        return 0
    if sub == "setup":
        _out("1. In Telegram, talk to @BotFather, send /newbot and copy the token.\n"
             "   (Use one bot per machine - put them all in one group if you like.)")
        token = a.token or getpass.getpass("Bot token (hidden): ").strip()
        api = TelegramApi(token)
        me = api.call("getMe")
        _out(f"✓ Bot @{me['username']} found.")
        # stop the daemon's bot while we poll for the owner
        cfg.set("telegram.enabled", False)
        reload_daemon()
        api.call("deleteWebhook")
        _out(f"2. Open https://t.me/{me['username']} and send /start to it (waiting up to 3 minutes)…")
        offset, user = None, None
        deadline = time.time() + 180
        while time.time() < deadline and not user:
            for upd in api.call("getUpdates", _timeout=40, timeout=25, offset=offset):
                offset = upd["update_id"] + 1
                m = upd.get("message")
                if m and m.get("chat", {}).get("type") == "private":
                    user = m["from"]
                    break
        api.call("getUpdates", offset=offset, timeout=0)   # acknowledge
        cfg.set("telegram.token", token)
        if user:
            name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
            ans = input(f"Allow {name} (@{user.get('username')}, id {user['id']})? [Y/n] ").strip().lower()
            if ans in ("", "y", "yes"):
                ids = set(cfg.get("telegram.allowed_ids") or []) | {user["id"]}
                cfg.set("telegram.allowed_ids", sorted(ids))
        else:
            _out("No message received. Add your ID later: ferry telegram allow <id> (send /whoami to the bot).")
        if a.machine_name:
            cfg.set("machine_name", a.machine_name)
        cfg.set("telegram.enabled", True)
        reload_daemon()
        if not Client(cfg).alive():
            _out("Start the background service so the bot answers: ferry service install   (or: ferry daemon start)")
        for chat in cfg.get("telegram.allowed_ids"):
            try:
                api.send(chat, f"✅ Ferry on <b>{cfg.get('machine_name')}</b> is connected. Try /status")
            except Exception:
                pass
        _out("✓ Telegram configured.")
        return 0
    if sub in ("allow", "deny"):
        ids = set(cfg.get("telegram.allowed_ids") or [])
        for i in a.ids:
            (ids.add if sub == "allow" else ids.discard)(int(i))
        cfg.set("telegram.allowed_ids", sorted(ids))
        reload_daemon()
        _out(f"Allowed IDs: {sorted(ids)}")
        return 0
    if sub in ("enable", "disable"):
        cfg.set("telegram.enabled", sub == "enable")
        reload_daemon()
        _out(f"Telegram {sub}d.")
        return 0
    if sub == "test":
        c = Client(cfg)
        if c.alive():
            _out(json.dumps(c.post("/telegram/test"), indent=2))
        else:
            from ferry.daemon import Daemon
            _out("(service not running - testing directly)")
            d = Daemon.__new__(Daemon)
            d.config = cfg
            _out(json.dumps(Daemon.telegram_test(d), indent=2))
        return 0
    return 1


def cmd_config(a) -> int:
    from ferry.config import Config, data_dir
    cfg = Config()
    if a.config_cmd == "path":
        _out(str(data_dir()))
    elif a.config_cmd == "set":
        try:
            val = json.loads(a.value)
        except ValueError:
            val = a.value
        cfg.set(a.key, val)
        from ferry.api import Client
        c = Client(cfg)
        if c.alive() and not a.key.startswith("api."):
            c.post("/config", {a.key: val})
        _out(f"{a.key} = {json.dumps(val)}")
    else:
        from ferry.api import _mask
        _out(json.dumps(_mask(cfg.data), indent=2))
    return 0


# ------------------------------------------------------------------ parser --
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ferry", description="Ferry - comfortable file copy & sync "
                                "(robocopy-like). Run 'ferry <command> -h' for details.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Examples:\n"
                                       "  ferry copy ~/Photos /mnt/backup/Photos --verify\n"
                                       "  ferry mirror D:\\Work \\\\nas\\share\\Work --delete-mode confirm\n"
                                       "  ferry copy ./data sftp://me@server/backups/data --bwlimit 10M\n"
                                       "  ferry mirror ~/Docs gdrive:Backup/Docs   (rclone remote)\n"
                                       "  ferry robo C:\\src D:\\dst /MIR /R:2 /W:5 /XD node_modules\n"
                                       "  ferry job add nightly ~/Work /backup/Work --mode mirror --schedule @daily\n"
                                       "  ferry tui | ferry gui | ferry telegram setup | ferry service install")
    p.add_argument("--version", action="version", version=f"ferry {__version__}")
    sub = p.add_subparsers(dest="command")

    for mode, helptext in (("copy", "copy new and changed files"), ("mirror", "make destination identical"),
                           ("move", "copy then delete from source")):
        sp = sub.add_parser(mode, help=helptext)
        sp.add_argument("source")
        sp.add_argument("dest")
        sp.add_argument("--local", action="store_true", help="run in this process, not the background service")
        sp.add_argument("--bg", action="store_true", help="start in the background service and return")
        sp.add_argument("-q", "--quiet", action="store_true", help="no live progress line")
        sp.add_argument("--save", metavar="NAME", help="also save as a job with this name")
        sp.add_argument("--save-only", action="store_true", help="with --save: save without running")
        add_job_args(sp)

    sp = sub.add_parser("robo", help="robocopy-compatible syntax: ferry robo SRC DST [files] [/options]",
                        add_help=False)
    sp.add_argument("rest", nargs=argparse.REMAINDER)

    sp = sub.add_parser("job", aliases=["jobs"], help="manage saved jobs")
    jsub = sp.add_subparsers(dest="job_cmd")
    jl = jsub.add_parser("list")
    jl.add_argument("--json", action="store_true")
    ja = jsub.add_parser("add", help="ferry job add NAME SRC DST [options]")
    ja.add_argument("job_name")
    ja.add_argument("source")
    ja.add_argument("dest")
    ja.add_argument("--mode", choices=["copy", "mirror", "move"], default="copy")
    add_job_args(ja)
    for name in ("show", "remove"):
        x = jsub.add_parser(name)
        x.add_argument("job_name")
    je = jsub.add_parser("edit", help="change options of a saved job")
    je.add_argument("job_name")
    je.add_argument("--source")
    je.add_argument("--dest")
    je.add_argument("--mode", choices=["copy", "mirror", "move"])
    je.add_argument("--enable", action="store_true")
    je.add_argument("--disable", action="store_true")
    je.add_argument("--no-schedule", action="store_true")
    je.add_argument("--no-watch", action="store_true")
    add_job_args(je)
    jr = jsub.add_parser("run")
    jr.add_argument("job_name")
    jr.add_argument("-n", "--dry-run", action="store_true")
    jr.add_argument("--bg", action="store_true")
    jr.add_argument("--local", action="store_true")
    jr.add_argument("-q", "--quiet", action="store_true", help="no live progress line")

    sp = sub.add_parser("status", help="service status, or details of one run")
    sp.add_argument("run", nargs="?")
    sp.add_argument("-f", "--follow", action="store_true", help="live progress of the run")
    sp.add_argument("--json", action="store_true")
    sub.add_parser("runs", help="runs known to the service")
    sp = sub.add_parser("history", help="finished runs")
    sp.add_argument("-n", "--limit", type=int, default=20)
    for name in ("pause", "resume", "cancel", "confirm"):
        sp = sub.add_parser(name, help=f"{name} a run (id, id suffix or job name; pause/resume accept 'all')")
        sp.add_argument("run")
        if name == "confirm":
            sp.add_argument("--skip", action="store_true", help="continue but skip deletions/overwrites")
            sp.add_argument("--reject", action="store_true", help="cancel the run")
    sp = sub.add_parser("errors", help="failed files of a run")
    sp.add_argument("run")
    sp.add_argument("-n", "--limit", type=int, default=200)
    sp = sub.add_parser("log", help="log of a run")
    sp.add_argument("run")
    sp.add_argument("-n", "--tail", type=int, default=0)

    sp = sub.add_parser("daemon", help="background service process: start|stop|restart|status|run")
    sp.add_argument("daemon_cmd", choices=["start", "stop", "restart", "status", "run"])
    sp = sub.add_parser("service", help="start automatically at login (no admin): install|uninstall|status")
    sp.add_argument("service_cmd", choices=["install", "uninstall", "status"])

    sp = sub.add_parser("telegram", help="Telegram bot: setup|status|allow|deny|enable|disable|test")
    tsub = sp.add_subparsers(dest="tg_cmd")
    ts = tsub.add_parser("setup")
    ts.add_argument("--token")
    ts.add_argument("--machine-name", help="name shown in messages (default: host name)")
    tsub.add_parser("status")
    for name in ("allow", "deny"):
        x = tsub.add_parser(name)
        x.add_argument("ids", nargs="+")
    tsub.add_parser("enable")
    tsub.add_parser("disable")
    tsub.add_parser("test")

    sp = sub.add_parser("config", help="show|set|path")
    csub = sp.add_subparsers(dest="config_cmd")
    csub.add_parser("show")
    csub.add_parser("path")
    cs = csub.add_parser("set", help="e.g. ferry config set defaults.threads 16")
    cs.add_argument("key")
    cs.add_argument("value")

    sub.add_parser("tui", help="full-screen terminal UI")
    sub.add_parser("gui", help="desktop window")
    sub.add_parser("version")
    return p


def _launch_gui() -> int:
    try:
        from ferry.gui import main as gui_main
    except ImportError:
        exe = shutil.which("ferry-gui")
        from ferry.config import exe_dir
        cand = exe_dir() / ("ferry-gui.exe" if os.name == "nt" else "ferry-gui")
        if cand.exists():
            exe = str(cand)
        if exe:
            import subprocess
            subprocess.Popen([exe])
            return 0
        _err("the desktop UI isn't included in this build; use 'ferry tui' or download ferry-gui")
        return 2
    return gui_main()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if not argv:
        if sys.stdin is None or not sys.stdin.isatty():
            return _launch_gui()   # double-clicked
        build_parser().print_help()
        return 0
    try:
        if argv[0] == "robo" and (len(argv) == 1 or set(argv[1:2]) & {"-h", "--help", "/?", "-?"}):
            _out(ROBO_HELP)
            return 0
        if argv[0] not in SUBCOMMANDS and len(argv) >= 2:
            return cmd_robo(argv)       # "ferry SRC DST /MIR ..."
        a = build_parser().parse_args(argv)
        cmd = a.command
        if cmd in ("copy", "mirror", "move"):
            return cmd_transfer(a)
        if cmd == "robo":
            rest = a.rest
            local = "--local" in rest
            bg = "--bg" in rest
            return cmd_robo([x for x in rest if x not in ("--local", "--bg")], local, bg)
        if cmd in ("job", "jobs"):
            return cmd_job(a)
        if cmd == "status":
            return cmd_status(a)
        if cmd == "runs":
            return cmd_runs(a)
        if cmd == "history":
            return cmd_history(a)
        if cmd in ("pause", "resume", "cancel", "confirm"):
            return cmd_control(a)
        if cmd == "errors":
            return cmd_errors(a)
        if cmd == "log":
            return cmd_log(a)
        if cmd == "daemon":
            return cmd_daemon(a)
        if cmd == "service":
            return cmd_service(a)
        if cmd == "telegram":
            return cmd_telegram(a)
        if cmd == "config":
            return cmd_config(a)
        if cmd == "tui":
            from ferry.tui import main as tui_main
            return tui_main()
        if cmd == "gui":
            return _launch_gui()
        if cmd in ("version", None):
            _out(f"ferry {__version__}")
            return 0
    except KeyboardInterrupt:
        return 130
    except (KeyError, ValueError, RuntimeError, FileNotFoundError, PermissionError) as e:
        _err(str(e).strip("'\""))
        return 2
    except Exception as e:
        from ferry.api import ApiError
        if isinstance(e, ApiError):
            _err(str(e))
            return 2
        raise
    return 0
