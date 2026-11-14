"""Job specification: everything that describes one copy/sync job."""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field, fields

from ferry.util import TimeWindow, parse_size

MODES = ("copy", "mirror", "move")
DELETE_MODES = ("delete", "archive", "confirm")
OVERWRITE = ("different", "newer", "always", "never", "checksum")
VERIFY = ("none", "xxhash", "sha256")


@dataclass
class JobSpec:
    source: str
    dest: str
    name: str = ""
    id: str = ""
    mode: str = "copy"                 # copy | mirror | move
    delete_mode: str = "archive"       # what happens to deleted/overwritten destination files
    overwrite: str = "different"       # when an existing destination file is replaced
    verify: str = "none"               # none | xxhash | sha256
    threads: int | None = None
    retries: int | None = None
    retry_wait: float | None = None
    bwlimit: str | None = None         # e.g. "20M" (bytes/second)
    bw_schedule: list = field(default_factory=list)   # [{"window": "mon-fri 09:00-18:00", "limit": "5M"}]
    windows: list = field(default_factory=list)       # only run inside these windows, e.g. ["22:00-06:00"]
    schedule: str | None = None        # cron expression, e.g. "0 2 * * *"
    watch: bool = False                # live sync on file changes
    watch_delay: float = 5.0           # seconds to wait for changes to settle
    metadata: dict = field(default_factory=dict)      # see metadata.MetaOptions
    filters: dict = field(default_factory=dict)       # include/exclude/exclude_dirs/min_size/...
    empty_dirs: bool = True            # create empty directories (robocopy /E vs /S)
    move_dirs: bool = False            # move mode: also remove emptied source directories
    skip_new: bool = False             # only update files that already exist (robocopy /XL)
    delta: str = "auto"                # auto | on | off  (in-place block update of large files)
    modify_window: float = 0.0         # extra timestamp tolerance in seconds (precision is auto-detected)
    archive_dir: str = ".ferry-archive"
    dry_run: bool = False
    enabled: bool = True               # for schedules/watch
    notify: bool = True                # push Telegram notifications for this job

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:8]
        if not self.name:
            base = re.split(r"[\\/:]", self.source.rstrip("/\\"))[-1] if self.source else ""
            base = re.sub(r"[^\w .\-]", "_", base).strip()[:40]
            self.name = base or self.id

    @classmethod
    def from_dict(cls, d: dict) -> "JobSpec":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> "JobSpec":
        if not self.source or not self.dest:
            raise ValueError("source and destination are required")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.delete_mode not in DELETE_MODES:
            raise ValueError(f"delete_mode must be one of {DELETE_MODES}")
        if self.overwrite not in OVERWRITE:
            raise ValueError(f"overwrite must be one of {OVERWRITE}")
        if self.verify not in VERIFY:
            raise ValueError(f"verify must be one of {VERIFY}")
        if not re.fullmatch(r"[\w .\-]+", self.name):
            raise ValueError("job name may contain letters, digits, space, '.', '-' and '_' only")
        parse_size(self.bwlimit)
        for w in self.windows:
            TimeWindow(w)
        for item in self.bw_schedule:
            TimeWindow(item["window"])
            parse_size(item.get("limit"))
        if self.schedule:
            from ferry.scheduler import Cron
            Cron(self.schedule)
        return self
