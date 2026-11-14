"""Remote machines over SSH/SFTP: ``sftp://user@host[:port]/path``.

Authentication uses the SSH agent and the user's keys (~/.ssh). A password can be
supplied with the FERRY_SFTP_PASSWORD environment variable, and a key file with
FERRY_SFTP_KEY. Unknown host keys are trusted on first use and remembered in
Ferry's own known_hosts file; a changed host key is always rejected.
"""
from __future__ import annotations

import os
import posixpath
import shlex
import stat as statmod
import threading
from urllib.parse import unquote, urlparse

from ferry.backends.base import Backend, Entry


class _TofuPolicy:
    def __init__(self, path):
        self.path = path

    def missing_host_key(self, client, hostname, key):
        import logging
        client.get_host_keys().add(hostname, key.get_name(), key)
        try:
            client.save_host_keys(self.path)
        except OSError:
            pass
        logging.getLogger("ferry").warning("SFTP: trusting new host key for %s (%s)", hostname, key.get_name())


class SFTPBackend(Backend):
    kind = "sftp"
    mtime_window = 1.0

    def __init__(self, url: str):
        import paramiko  # imported lazily: optional at runtime
        from ferry.config import data_dir

        u = urlparse(url)
        self.url = url
        self.host = u.hostname
        self.port = u.port or 22
        self.user = unquote(u.username) if u.username else None
        password = unquote(u.password) if u.password else os.environ.get("FERRY_SFTP_PASSWORD")
        self.root = unquote(u.path) or "."
        if self.root.startswith("/~"):
            self.root = self.root[1:]
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        kh = str(data_dir() / "known_hosts")
        if os.path.exists(kh):
            self._client.load_host_keys(kh)
        self._client.set_missing_host_key_policy(_TofuPolicy(kh))
        self._client.connect(self.host, port=self.port, username=self.user, password=password,
                             key_filename=os.environ.get("FERRY_SFTP_KEY") or None, timeout=30)
        self._client.get_transport().set_keepalive(30)
        self._local = threading.local()
        if self.root.startswith("~"):
            home = self.sftp.normalize(".")
            self.root = posixpath.join(home, self.root[1:].lstrip("/"))

    @property
    def sftp(self):
        s = getattr(self._local, "sftp", None)
        if s is None:
            s = self._client.open_sftp()
            self._local.sftp = s
        return s

    def describe(self):
        return f"sftp://{self.user + '@' if self.user else ''}{self.host}:{self.port}{self.root}"

    def path(self, rel: str) -> str:
        return posixpath.join(self.root, rel) if rel else self.root

    @staticmethod
    def _entry(name, a) -> Entry:
        is_link = statmod.S_ISLNK(a.st_mode or 0)
        is_dir = statmod.S_ISDIR(a.st_mode or 0)
        return Entry(name=name, is_dir=is_dir, size=0 if is_dir else (a.st_size or 0),
                     mtime_ns=int((a.st_mtime or 0) * 1e9), mode=a.st_mode or 0, is_link=is_link)

    def list_dir(self, rel):
        out = []
        for a in self.sftp.listdir_attr(self.path(rel)):
            e = self._entry(a.filename, a)
            if e.is_link:
                try:
                    e.link_target = self.sftp.readlink(self.path(rel + "/" + a.filename if rel else a.filename))
                except OSError:
                    pass
            out.append(e)
        return out

    def stat(self, rel, follow=False):
        try:
            a = self.sftp.stat(self.path(rel)) if follow else self.sftp.lstat(self.path(rel))
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return self._entry(rel.rsplit("/", 1)[-1], a)

    def _mkdir_abs(self, path):
        try:
            self.sftp.stat(path)
        except FileNotFoundError:
            try:
                self.sftp.mkdir(path)
            except OSError:
                try:
                    self.sftp.stat(path)
                except OSError:
                    raise

    def makedirs(self, rel):
        if not rel:
            cur = "/" if self.root.startswith("/") else ""
            for p in [p for p in self.root.split("/") if p]:
                cur = posixpath.join(cur, p) if cur else p
                self._mkdir_abs(cur)
            return
        cur = ""
        for p in [p for p in rel.split("/") if p]:
            cur = f"{cur}/{p}" if cur else p
            self._mkdir_abs(self.path(cur))

    def open_read(self, rel, offset=0):
        f = self.sftp.open(self.path(rel), "rb")
        f.prefetch()
        if offset:
            f.seek(offset)
        return f

    def open_write_part(self, part_rel, offset):
        if offset:
            f = self.sftp.open(self.path(part_rel), "r+b")
            f.seek(offset)
            f.truncate(offset)
        else:
            f = self.sftp.open(self.path(part_rel), "wb")
        f.set_pipelined(True)
        return f

    def commit_part(self, part_rel, rel):
        try:
            self.sftp.posix_rename(self.path(part_rel), self.path(rel))
        except (OSError, AttributeError):
            try:
                self.sftp.remove(self.path(rel))
            except OSError:
                pass
            self.sftp.rename(self.path(part_rel), self.path(rel))

    def remove_file(self, rel):
        self.sftp.remove(self.path(rel))

    def remove_dir(self, rel):
        self.sftp.rmdir(self.path(rel))

    def rename(self, src_rel, dst_rel):
        self.makedirs(posixpath.dirname(dst_rel))
        try:
            self.sftp.posix_rename(self.path(src_rel), self.path(dst_rel))
        except (OSError, AttributeError):
            self.sftp.rename(self.path(src_rel), self.path(dst_rel))

    def make_symlink(self, target, rel):
        if self.stat(rel) is not None:
            self.remove_file(rel)
        self.sftp.symlink(target, self.path(rel))

    def set_times(self, rel, mtime_ns):
        t = mtime_ns / 1e9
        self.sftp.utime(self.path(rel), (t, t))

    def set_mode(self, rel, mode):
        self.sftp.chmod(self.path(rel), statmod.S_IMODE(mode))

    def free_space(self):
        try:
            _, out, _ = self._client.exec_command(f"df -Pk {shlex.quote(self.root)} | tail -1", timeout=15)
            return int(out.read().split()[3]) * 1024
        except Exception:
            return None

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass
