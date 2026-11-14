"""Pick a backend from a location string.

  C:\\data, /home/me, ~/x, \\\\server\\share\\dir, ./rel   -> local (incl. mounted shares)
  sftp://user@host:22/path  (also ssh://)               -> SFTP
  remote:path                                           -> rclone remote (cloud)
"""
from __future__ import annotations

from ferry.backends.base import Backend, Entry, join, parent
from ferry.backends.local import LocalBackend
from ferry.backends.rclone import RcloneBackend, is_rclone_spec


def open_backend(spec: str) -> Backend:
    s = spec.strip()
    low = s.lower()
    if low.startswith(("sftp://", "ssh://")):
        from ferry.backends.sftp import SFTPBackend
        return SFTPBackend(s)
    if low.startswith("file://"):
        return LocalBackend(s[7:])
    if is_rclone_spec(s):
        return RcloneBackend(s)
    return LocalBackend(s)


__all__ = ["Backend", "Entry", "join", "parent", "open_backend", "LocalBackend", "RcloneBackend"]
