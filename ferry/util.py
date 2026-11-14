"""Small shared helpers: sizes, durations, time windows."""
from __future__ import annotations

import datetime as _dt
import re
import time

_SIZE_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}


def parse_size(value) -> int | None:
    """'20M' -> 20971520, '1.5G', '512k', '100' (bytes). None/'' -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGTP]?)(?:i?B)?\s*", str(value), re.IGNORECASE)
    if not m:
        raise ValueError(f"invalid size: {value!r}")
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()])


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def parse_duration(value) -> float | None:
    """'90' (seconds), '30s', '5m', '2h', '7d', '1w' -> seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    total = 0.0
    parts = re.findall(r"([\d.]+)\s*([smhdw]?)", str(value).lower())
    if not parts:
        raise ValueError(f"invalid duration: {value!r}")
    mult = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    for num, unit in parts:
        total += float(num) * mult[unit]
    return total


def progress_bar(fraction: float, width: int = 20) -> str:
    fraction = min(1.0, max(0.0, fraction or 0.0))
    full = int(fraction * width)
    return "█" * full + "░" * (width - full)


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_days(spec: str) -> set[int]:
    days: set[int] = set()
    for part in spec.lower().split(","):
        part = part.strip()
        if not part:
            continue
        if part in ("daily", "all", "*"):
            return set(range(7))
        if part == "weekdays":
            days |= {0, 1, 2, 3, 4}
            continue
        if part == "weekends":
            days |= {5, 6}
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            ia, ib = _DAYS.index(a[:3]), _DAYS.index(b[:3])
            i = ia
            while True:
                days.add(i)
                if i == ib:
                    break
                i = (i + 1) % 7
        else:
            days.add(_DAYS.index(part[:3]))
    return days


class TimeWindow:
    """A recurring daily window, e.g. '22:00-06:00' or 'mon-fri 09:00-18:00'.

    Windows that cross midnight belong to the day they start on.
    """

    def __init__(self, spec: str):
        self.spec = spec.strip()
        m = re.fullmatch(r"(?:([a-zA-Z,\-*]+)\s+)?(\d{1,2}):?(\d{2})\s*-\s*(\d{1,2}):?(\d{2})", self.spec)
        if not m:
            raise ValueError(f"invalid time window: {spec!r} (expected e.g. 'mon-fri 22:00-06:00')")
        self.days = _parse_days(m.group(1)) if m.group(1) else set(range(7))
        self.start = int(m.group(2)) * 60 + int(m.group(3))
        self.end = int(m.group(4)) * 60 + int(m.group(5))

    def contains(self, now: _dt.datetime | None = None) -> bool:
        now = now or _dt.datetime.now()
        minute = now.hour * 60 + now.minute
        wd = now.weekday()
        if self.start == self.end:  # full day
            return wd in self.days
        if self.start < self.end:
            return wd in self.days and self.start <= minute < self.end
        # crosses midnight
        if minute >= self.start and wd in self.days:
            return True
        return minute < self.end and ((wd - 1) % 7) in self.days

    def __repr__(self):
        return f"TimeWindow({self.spec!r})"


def in_any_window(windows: list[TimeWindow], now=None) -> bool:
    return not windows or any(w.contains(now) for w in windows)


def current_bwlimit(default_limit: int | None, schedule: list[tuple[TimeWindow, int | None]], now=None) -> int | None:
    """Return the bandwidth limit (bytes/s) in effect now. None = unlimited."""
    for window, limit in schedule:
        if window.contains(now):
            return limit
    return default_limit


def now_ts() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
