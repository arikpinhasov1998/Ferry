"""Cloud storage (S3, Google Drive, OneDrive, Dropbox, B2, Azure, ... 70+ services)
through rclone: ``remote:path`` where ``remote`` is configured with ``rclone config``.

Ferry drives one ``rclone rcd`` process per backend over its local HTTP API.
The rclone executable is looked up next to Ferry first, then on PATH.
"""
from __future__ import annotations

import atexit
import base64
import datetime as _dt
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

from ferry.backends.base import Backend, Entry, parent
from ferry.config import exe_dir


class RcloneError(OSError):
    pass


def find_rclone() -> str | None:
    name = "rclone.exe" if os.name == "nt" else "rclone"
    local = exe_dir() / name
    if local.exists():
        return str(local)
    return shutil.which("rclone")


def _parse_time(s: str) -> int:
    if not s:
        return 0
    s = s.replace("Z", "+00:00")
    # trim sub-microsecond digits that fromisoformat can't handle on older Pythons
    if "." in s:
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    return int(_dt.datetime.fromisoformat(s).timestamp() * 1e9)


class RcloneDaemon:
    def __init__(self):
        exe = find_rclone()
        if not exe:
            raise RcloneError("rclone is required for cloud remotes but was not found. "
                              "Put rclone next to the ferry executable or install it on PATH "
                              "(https://rclone.org/downloads/).")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.user, self.password = "ferry", secrets.token_urlsafe(16)
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [exe, "rcd", f"--rc-addr=127.0.0.1:{self.port}", f"--rc-user={self.user}",
             f"--rc-pass={self.password}", "--use-server-modtime=false"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=flags)
        atexit.register(self.stop)
        deadline = time.time() + 20
        while True:
            try:
                self.call("rc/noop")
                break
            except Exception:
                if time.time() > deadline or self.proc.poll() is not None:
                    raise RcloneError("could not start rclone rcd")
                time.sleep(0.2)

    def call(self, method: str, timeout: float = 3600, **params):
        data = json.dumps(params).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/{method}", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:
                msg = str(e)
            if "not found" in msg.lower() or "doesn't exist" in msg.lower():
                raise FileNotFoundError(msg) from None
            raise RcloneError(msg) from None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


_daemon: RcloneDaemon | None = None


def daemon() -> RcloneDaemon:
    global _daemon
    if _daemon is None or _daemon.proc.poll() is not None:
        _daemon = RcloneDaemon()
    return _daemon


def is_rclone_spec(spec: str) -> bool:
    if "://" in spec or spec.startswith(("\\\\", "/", "~", ".")):
        return False
    if os.name == "nt" and len(spec) >= 2 and spec[1] == ":" and spec[0].isalpha():
        return False  # C:\...
    head = spec.split(":", 1)[0]
    return ":" in spec and head != "" and "/" not in head and "\\" not in head


class RcloneBackend(Backend):
    kind = "rclone"
    mtime_window = 1.0
    supports_stream = False

    def __init__(self, spec: str):
        self.spec = spec
        self.rc = daemon()

    def describe(self):
        return self.spec

    def list_dir(self, rel):
        res = self.rc.call("operations/list", fs=self.spec, remote=rel, opt={"noMimeType": True})
        out = []
        for it in res.get("list", []):
            out.append(Entry(name=it["Name"], is_dir=it.get("IsDir", False), size=max(0, it.get("Size", 0)),
                             mtime_ns=_parse_time(it.get("ModTime", ""))))
        return out

    def stat(self, rel, follow=False):
        if not rel:
            try:
                self.rc.call("operations/list", fs=self.spec, remote="", opt={"noMimeType": True})
                return Entry(name="", is_dir=True)
            except (FileNotFoundError, RcloneError):
                return None
        try:
            res = self.rc.call("operations/stat", fs=self.spec, remote=rel)
        except FileNotFoundError:
            return None
        it = res.get("item")
        if not it:
            return None
        return Entry(name=it["Name"], is_dir=it.get("IsDir", False), size=max(0, it.get("Size", 0)),
                     mtime_ns=_parse_time(it.get("ModTime", "")))

    def makedirs(self, rel):
        self.rc.call("operations/mkdir", fs=self.spec, remote=rel)

    def copy_from(self, src_fs: str, src_rel: str, rel: str):
        self.rc.call("operations/copyfile", srcFs=src_fs, srcRemote=src_rel, dstFs=self.spec, dstRemote=rel)

    def copy_to(self, rel: str, dst_fs: str, dst_rel: str):
        self.rc.call("operations/copyfile", srcFs=self.spec, srcRemote=rel, dstFs=dst_fs, dstRemote=dst_rel)

    def remove_file(self, rel):
        self.rc.call("operations/deletefile", fs=self.spec, remote=rel)

    def remove_dir(self, rel):
        self.rc.call("operations/rmdir", fs=self.spec, remote=rel)

    def remove_tree(self, rel):
        self.rc.call("operations/purge", fs=self.spec, remote=rel)

    def rename(self, src_rel, dst_rel):
        e = self.stat(src_rel)
        if e and e.is_dir:
            self.rc.call("sync/move", srcFs=f"{self.spec.rstrip('/')}/{src_rel}",
                         dstFs=f"{self.spec.rstrip('/')}/{dst_rel}", deleteEmptySrcDirs=True)
        else:
            self.rc.call("operations/movefile", srcFs=self.spec, srcRemote=src_rel,
                         dstFs=self.spec, dstRemote=dst_rel)

    def hash_file(self, rel):
        return None

    def free_space(self):
        try:
            return self.rc.call("operations/about", fs=self.spec).get("free")
        except Exception:
            return None

    def fs_for_parent(self, rel: str) -> str:
        p = parent(rel)
        return f"{self.spec.rstrip('/')}/{p}" if p else self.spec

