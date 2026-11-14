"""Include/exclude filtering with gitignore-style patterns, size and age filters.

Pattern rules (same for --include, --exclude, --exclude-dir and .ferryignore):
  * ``*.tmp``        - no slash: matches the name at any depth
  * ``build/``       - trailing slash: directories only
  * ``/logs/*.log``  - contains a slash: anchored to the job root (or to the
                       folder containing the .ferryignore file)
  * ``**``           - matches any number of folders
  * ``!pattern``     - in .ferryignore: re-include (last match wins)
"""
from __future__ import annotations

import os
import re
import sys
import time

from ferry.util import parse_duration, parse_size

CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"

# Never transferred or deleted: Ferry's own working files.
INTERNAL_NAMES = {".ferry-archive"}
PART_SUFFIX = ".ferrypart"


def glob_to_regex(pattern: str) -> str:
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class Rule:
    __slots__ = ("base", "regex", "negate", "dir_only", "raw")

    def __init__(self, pattern: str, base: str = "", negate=False, dir_only=False):
        self.raw = pattern
        pattern = pattern.replace("\\", "/")
        if pattern.startswith("!"):
            negate, pattern = True, pattern[1:]
        if pattern.endswith("/"):
            dir_only, pattern = True, pattern.rstrip("/")
        anchored = "/" in pattern
        pattern = pattern.lstrip("/")
        body = glob_to_regex(pattern)
        rx = ("^" + body + "$") if anchored else ("^(?:.*/)?" + body + "$")
        self.regex = re.compile(rx, re.IGNORECASE if CASE_INSENSITIVE else 0)
        self.base = base.strip("/")
        self.negate = negate
        self.dir_only = dir_only

    def matches(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.base:
            prefix = self.base + "/"
            if not rel.startswith(prefix):
                return False
            rel = rel[len(prefix):]
        return bool(self.regex.match(rel))


def parse_ignore_lines(lines, base: str = "") -> list[Rule]:
    rules = []
    for line in lines:
        line = line.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rules.append(Rule(line.strip(), base))
    return rules


class Filter:
    def __init__(self, include=None, exclude=None, exclude_dirs=None,
                 min_size=None, max_size=None, min_age=None, max_age=None,
                 ignore_file: str | None = ".ferryignore", max_depth: int | None = None):
        self.include = [Rule(p) for p in (include or [])]
        self.exclude = [Rule(p) for p in (exclude or [])]
        self.exclude_dirs = [Rule(p.rstrip("/\\") + "/") for p in (exclude_dirs or [])]
        self.min_size = parse_size(min_size)
        self.max_size = parse_size(max_size)
        self.min_age = parse_duration(min_age)
        self.max_age = parse_duration(max_age)
        self.ignore_file = ignore_file or None
        self.max_depth = max_depth
        self.ignore_rules: list[Rule] = []   # accumulated from .ferryignore files
        self._now = time.time()

    @classmethod
    def from_spec(cls, f: dict) -> "Filter":
        f = f or {}
        return cls(include=f.get("include"), exclude=f.get("exclude"), exclude_dirs=f.get("exclude_dirs"),
                   min_size=f.get("min_size"), max_size=f.get("max_size"),
                   min_age=f.get("min_age"), max_age=f.get("max_age"),
                   ignore_file=f.get("ignore_file", ".ferryignore"), max_depth=f.get("max_depth"))

    def add_ignore_file(self, base_rel: str, text: str):
        self.ignore_rules.extend(parse_ignore_lines(text.splitlines(), base_rel))

    def _ignored(self, rel: str, is_dir: bool) -> bool:
        state = False
        for r in self.ignore_rules:
            if r.matches(rel, is_dir):
                state = not r.negate
        return state

    def is_internal(self, name: str) -> bool:
        return name in INTERNAL_NAMES or name.endswith(PART_SUFFIX)

    def dir_allowed(self, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if self.is_internal(name):
            return False
        if self.max_depth is not None and rel.count("/") + 1 > self.max_depth:
            return False
        if any(r.matches(rel, True) for r in self.exclude_dirs):
            return False
        if any(r.matches(rel, True) for r in self.exclude):
            return False
        return not self._ignored(rel, True)

    def file_allowed(self, rel: str, size: int | None = None, mtime: float | None = None) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if self.is_internal(name):
            return False
        if self.ignore_file and name == self.ignore_file:
            return True
        if self.include and not any(r.matches(rel, False) for r in self.include):
            return False
        if any(r.matches(rel, False) for r in self.exclude):
            return False
        if self._ignored(rel, False):
            return False
        if size is not None:
            if self.min_size is not None and size < self.min_size:
                return False
            if self.max_size is not None and size > self.max_size:
                return False
        if mtime is not None:
            age = self._now - mtime
            if self.min_age is not None and age < self.min_age:
                return False
            if self.max_age is not None and age > self.max_age:
                return False
        return True
