"""Local disks, USB drives and mounted network shares (incl. UNC paths)."""
from __future__ import annotations

import os
import shutil
import stat as statmod
import sys

from ferry import metadata
from ferry.backends.base import Backend, Entry

IS_WIN = os.name == "nt"


def _long(p: str) -> str:
    """Windows: use \\\\?\\ prefix so paths longer than 260 chars work."""
    if not IS_WIN:
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def _entry(name: str, st: os.stat_result, is_link: bool, target: str | None = None) -> Entry:
    is_dir = statmod.S_ISDIR(st.st_mode)
    return Entry(name=name, is_dir=is_dir, size=0 if is_dir else st.st_size,
                 mtime_ns=st.st_mtime_ns, mode=st.st_mode, is_link=is_link, link_target=target)


class LocalBackend(Backend):
    kind = "local"

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self._root_long = _long(self.root)

    def describe(self):
        return self.root

    def path(self, rel: str) -> str:
        if not rel:
            return self._root_long
        return os.path.join(self._root_long, *rel.split("/"))

    def local_path(self, rel: str) -> str:
        return self.path(rel)

    def list_dir(self, rel: str) -> list[Entry]:
        out = []
        with os.scandir(self.path(rel)) as it:
            for de in it:
                try:
                    if de.is_symlink():
                        st = de.stat(follow_symlinks=False)
                        try:
                            target = os.readlink(de.path)
                        except OSError:
                            target = None
                        e = _entry(de.name, st, True, target)
                        e.is_dir = False
                        out.append(e)
                    else:
                        out.append(_entry(de.name, de.stat(follow_symlinks=False), False))
                except FileNotFoundError:
                    continue
        return out

    def stat(self, rel: str, follow: bool = False) -> Entry | None:
        p = self.path(rel)
        try:
            st = os.stat(p) if follow else os.lstat(p)
        except (FileNotFoundError, NotADirectoryError):
            return None
        is_link = statmod.S_ISLNK(st.st_mode)
        target = os.readlink(p) if is_link else None
        return _entry(rel.rsplit("/", 1)[-1], st, is_link, target)

    def makedirs(self, rel: str):
        os.makedirs(self.path(rel), exist_ok=True)

    def open_read(self, rel: str, offset: int = 0):
        f = open(self.path(rel), "rb")
        if offset:
            f.seek(offset)
        return f

    def open_write_part(self, part_rel: str, offset: int):
        p = self.path(part_rel)
        if offset:
            f = open(p, "r+b")
            f.seek(offset)
            f.truncate()
        else:
            f = open(p, "wb")
        return f

    def commit_part(self, part_rel: str, rel: str):
        dst = self.path(rel)
        if os.path.lexists(dst):
            metadata.make_writable(dst)
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
        os.replace(self.path(part_rel), dst)

    def remove_file(self, rel: str):
        p = self.path(rel)
        metadata.make_writable(p)
        os.unlink(p)

    def remove_dir(self, rel: str):
        os.rmdir(self.path(rel))

    def remove_tree(self, rel: str):
        def onerr(func, path, _exc):
            metadata.make_writable(path)
            func(path)
        p = self.path(rel)
        if os.path.islink(p) or not os.path.isdir(p):
            self.remove_file(rel)
        elif sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=onerr)
        else:
            shutil.rmtree(p, onerror=onerr)

    def rename(self, src_rel: str, dst_rel: str):
        dst = self.path(dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(self.path(src_rel), dst)

    def make_symlink(self, target: str, rel: str):
        p = self.path(rel)
        if os.path.lexists(p):
            self.remove_file(rel)
        is_dir = False
        if IS_WIN:
            is_dir = os.path.isdir(os.path.join(os.path.dirname(p), target))
        os.symlink(target, p, target_is_directory=is_dir)

    def set_times(self, rel: str, mtime_ns: int):
        os.utime(self.path(rel), ns=(mtime_ns, mtime_ns))

    def set_mode(self, rel: str, mode: int):
        if not IS_WIN:
            os.chmod(self.path(rel), statmod.S_IMODE(mode))

    def free_space(self):
        p = self.root
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        try:
            return shutil.disk_usage(p).free
        except OSError:
            return None
