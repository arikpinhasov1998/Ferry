"""Cron-style schedules (5 fields: minute hour day-of-month month day-of-week).

Supports ``*``, lists ``1,15``, ranges ``1-5``, steps ``*/10`` and ``8-18/2``,
names (``mon``, ``jan``) and shortcuts ``@hourly @daily @weekly @monthly``.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
            "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *"}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DOW = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


def _field(spec: str, lo: int, hi: int, names=None) -> set[int]:
    out: set[int] = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _val(x, names), _val(y, names)
        else:
            a = _val(part, names)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ValueError(f"cron value out of range: {spec!r}")
        out.update(range(a, b + 1, step))
    return out


def _val(x: str, names):
    if names and x[:3] in names:
        return names[x[:3]]
    return int(x)


class Cron:
    def __init__(self, expr: str):
        self.expr = expr.strip()
        e = _ALIASES.get(self.expr.lower(), self.expr)
        parts = e.split()
        if len(parts) != 5:
            raise ValueError(f"cron needs 5 fields (min hour day month weekday): {expr!r}")
        self.minute = _field(parts[0], 0, 59)
        self.hour = _field(parts[1], 0, 23)
        self.dom = _field(parts[2], 1, 31)
        self.month = _field(parts[3], 1, 12, _MONTHS)
        dow = _field(parts[4].replace("7", "0"), 0, 6, _DOW)
        self.dow = dow
        self.dom_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    def matches(self, t: _dt.datetime) -> bool:
        if t.minute not in self.minute or t.hour not in self.hour or t.month not in self.month:
            return False
        cron_dow = (t.weekday() + 1) % 7
        dom_ok, dow_ok = t.day in self.dom, cron_dow in self.dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok   # classic cron: either matches

    def next_after(self, t: _dt.datetime) -> _dt.datetime | None:
        t = t.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if self.matches(t):
                return t
            t += _dt.timedelta(minutes=1)
        return None


class Scheduler(threading.Thread):
    """Starts saved jobs whose cron schedule matches the current minute."""

    def __init__(self, manager):
        super().__init__(daemon=True, name="ferry-scheduler")
        self.manager = manager
        self.stop_event = threading.Event()
        self.log = logging.getLogger("ferry.scheduler")
        self._last_minute = None

    def run(self):
        while not self.stop_event.wait(5):
            now = _dt.datetime.now().replace(second=0, microsecond=0)
            if now == self._last_minute:
                continue
            self._last_minute = now
            for job in self.manager.jobs.list():
                if not job.schedule or not job.enabled:
                    continue
                try:
                    if Cron(job.schedule).matches(now):
                        if self.manager.is_job_running(job.id):
                            self.log.info("schedule: %s still running, skipping this slot", job.name)
                            continue
                        self.log.info("schedule: starting %s", job.name)
                        self.manager.start_job(job.id, trigger="schedule")
                except Exception:
                    self.log.exception("schedule error for %s", job.name)

    def stop(self):
        self.stop_event.set()
