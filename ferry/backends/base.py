from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    mtime_ns: int = 0
    mode: int = 0
    is_link: bool = False
    link_target: str | None = None

    @property
    def mtime(self) -> float:
        return self.mtime_ns / 1e9


def join(a: str, b: str) -> str:
    return f"{a}/{b}" if a else b


def parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


class Backend:
    """A file tree addressed by '/'-separated relative paths ('' is the root)."""

    kind = "base"
    mtime_window = 0.0          # timestamp precision of this storage in seconds
    supports_stream = True      # open_read/open_write_part usable

    def describe(self) -> str:
        raise NotImplementedError

    def local_path(self, rel: str) -> str | None:
        return None

    def list_dir(self, rel: str) -> list[Entry]:
        raise NotImplementedError

    def stat(self, rel: str, follow: bool = False) -> Entry | None:
        raise NotImplementedError

    def read_text(self, rel: str, limit: int = 1 << 20) -> str | None:
        try:
            with self.open_read(rel) as f:
                return f.read(limit).decode("utf-8", "replace")
        except OSError:
            return None

    def makedirs(self, rel: str):
        raise NotImplementedError

    def open_read(self, rel: str, offset: int = 0):
        raise NotImplementedError

    def open_write_part(self, part_rel: str, offset: int):
        raise NotImplementedError

    def part_size(self, part_rel: str) -> int | None:
        e = self.stat(part_rel)
        return None if e is None or e.is_dir else e.size

    def commit_part(self, part_rel: str, rel: str):
        raise NotImplementedError

    def remove_file(self, rel: str):
        raise NotImplementedError

    def remove_dir(self, rel: str):
        """Remove an empty directory."""
        raise NotImplementedError

    def remove_tree(self, rel: str):
        for e in self.list_dir(rel):
            child = join(rel, e.name)
            if e.is_dir and not e.is_link:
                self.remove_tree(child)
            else:
                self.remove_file(child)
        self.remove_dir(rel)

    def rename(self, src_rel: str, dst_rel: str):
        raise NotImplementedError

    def make_symlink(self, target: str, rel: str):
        raise NotImplementedError

    def set_times(self, rel: str, mtime_ns: int):
        pass

    def set_mode(self, rel: str, mode: int):
        pass

    def free_space(self) -> int | None:
        return None

    def close(self):
        pass
