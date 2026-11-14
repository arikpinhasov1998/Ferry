"""Locations of config/state and the global settings file.

Portable mode: if a folder named ``ferry-data`` (or a file ``ferry.portable``)
exists next to the executable, everything is stored in ``ferry-data`` beside it.
``FERRY_HOME`` overrides everything.
"""
from __future__ import annotations

import copy
import json
import os
import platform
import secrets
import socket
import sys
import threading
from pathlib import Path

DEFAULT_PORT = 47821

DEFAULTS = {
    "machine_name": socket.gethostname(),
    "api": {"host": "127.0.0.1", "port": DEFAULT_PORT, "token": ""},
    "telegram": {
        "enabled": False,
        "token": "",
        "allowed_ids": [],          # Telegram user IDs and/or chat IDs allowed to use the bot
        "notify_chat_ids": [],      # where push notifications go (defaults to allowed_ids)
        "notify_start": True,
        "notify_finish": True,
        "notify_errors": True,
        "progress_every_percent": 25,   # 0 = off
        "progress_every_minutes": 0,    # 0 = off
    },
    "defaults": {"threads": 8, "retries": 3, "retry_wait": 5},
    "auto_resume": True,            # resume interrupted runs when the daemon starts
    "history_keep": 200,
}


def exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    env = os.environ.get("FERRY_HOME")
    if env:
        p = Path(env).expanduser()
    else:
        base = exe_dir()
        if (base / "ferry-data").is_dir() or (base / "ferry.portable").exists():
            p = base / "ferry-data"
        elif os.name == "nt":
            p = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ferry"
        elif sys.platform == "darwin":
            p = Path.home() / "Library" / "Application Support" / "Ferry"
        else:
            p = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ferry"
    p.mkdir(parents=True, exist_ok=True)
    for sub in ("runs", "logs"):
        (p / sub).mkdir(exist_ok=True)
    return p


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _write_private(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Config:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "config.json"
        self.data: dict = {}
        self.load()

    def load(self):
        with self._lock:
            raw = {}
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self.data = _deep_merge(DEFAULTS, raw)
            if not self.data["api"].get("token"):
                self.data["api"]["token"] = secrets.token_urlsafe(24)
                self.save()
            elif not self.path.exists():
                self.save()

    def save(self):
        with self._lock:
            _write_private(self.path, json.dumps(self.data, indent=2))

    def get(self, dotted: str, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value):
        with self._lock:
            cur = self.data
            parts = dotted.split(".")
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value
            self.save()


def platform_label() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()})"
