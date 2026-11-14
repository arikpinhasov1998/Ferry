import datetime as dt

import pytest

from ferry import robocopy
from ferry.filters import Filter
from ferry.scheduler import Cron
from ferry.spec import JobSpec
from ferry.util import TimeWindow, current_bwlimit, human_bytes, human_duration, parse_duration, parse_size


def test_sizes_and_durations():
    assert parse_size("20M") == 20 * 1024 ** 2
    assert parse_size("1.5G") == int(1.5 * 1024 ** 3)
    assert parse_size("512k") == 512 * 1024
    assert parse_size("100") == 100
    assert parse_size(None) is None
    with pytest.raises(ValueError):
        parse_size("lots")
    assert parse_duration("90") == 90
    assert parse_duration("2h30m") == 9000
    assert parse_duration("7d") == 7 * 86400
    assert human_bytes(1536) == "1.5 KB"
    assert human_duration(3725) == "1h 02m 05s"


def test_time_windows():
    night = TimeWindow("22:00-06:00")
    assert night.contains(dt.datetime(2026, 11, 13, 23, 0))
    assert night.contains(dt.datetime(2026, 11, 14, 5, 59))
    assert not night.contains(dt.datetime(2026, 11, 14, 12, 0))
    work = TimeWindow("mon-fri 09:00-18:00")
    assert work.contains(dt.datetime(2026, 11, 13, 10, 0))      # Friday
    assert not work.contains(dt.datetime(2026, 11, 14, 10, 0))  # Saturday
    # Friday-night window continues into Saturday morning
    fri_night = TimeWindow("fri 22:00-06:00")
    assert fri_night.contains(dt.datetime(2026, 11, 14, 3, 0))
    assert not fri_night.contains(dt.datetime(2026, 11, 15, 3, 0))
    sched = [(work, 5 * 1024 * 1024)]
    assert current_bwlimit(None, sched, dt.datetime(2026, 11, 13, 10, 0)) == 5 * 1024 * 1024
    assert current_bwlimit(100, sched, dt.datetime(2026, 11, 14, 10, 0)) == 100
    with pytest.raises(ValueError):
        TimeWindow("tonight")


def test_filters():
    f = Filter(exclude=["*.tmp", "build/", "/top.txt"], exclude_dirs=["node_modules"])
    assert not f.file_allowed("a/b/x.tmp")
    assert f.file_allowed("a/b/x.txt")
    assert not f.dir_allowed("src/build")
    assert f.file_allowed("src/build")            # 'build/' only matches folders
    assert not f.file_allowed("top.txt")
    assert f.file_allowed("sub/top.txt")          # anchored pattern
    assert not f.dir_allowed("x/node_modules")
    assert not f.file_allowed("dir/.f.abc.ferrypart")
    inc = Filter(include=["*.jpg", "docs/**/*.md"])
    assert inc.file_allowed("a/b.jpg") and inc.file_allowed("docs/x/y/z.md")
    assert not inc.file_allowed("a/b.png")
    sized = Filter(min_size="1k", max_size="1M")
    assert not sized.file_allowed("a", 10) and sized.file_allowed("a", 5000) and not sized.file_allowed("a", 2 << 20)
    ig = Filter()
    ig.add_ignore_file("proj", "*.log\n!keep.log\n# comment\n")
    assert not ig.file_allowed("proj/a.log")
    assert ig.file_allowed("proj/keep.log")
    assert ig.file_allowed("other/a.log")         # rules apply below their folder only


def test_cron():
    c = Cron("*/15 2-4 * * mon-fri")
    assert c.matches(dt.datetime(2026, 11, 13, 2, 30))
    assert not c.matches(dt.datetime(2026, 11, 13, 2, 31))
    assert not c.matches(dt.datetime(2026, 11, 14, 2, 30))   # Saturday
    assert Cron("@daily").next_after(dt.datetime(2026, 11, 13, 23, 59)) == dt.datetime(2026, 11, 14, 0, 0)
    assert Cron("0 0 1 * *").matches(dt.datetime(2026, 12, 1, 0, 0))
    with pytest.raises(ValueError):
        Cron("61 * * * *")


def test_spec_validation():
    j = JobSpec(source="/a", dest="/b").validate()
    assert j.name == "a" and j.mode == "copy" and j.delete_mode == "archive"
    with pytest.raises(ValueError):
        JobSpec(source="/a", dest="/b", mode="sync").validate()
    with pytest.raises(ValueError):
        JobSpec(source="/a", dest="/b", windows=["soon"]).validate()
    with pytest.raises(ValueError):
        JobSpec(source="/a", dest="/b", schedule="every day").validate()
    assert JobSpec.from_dict(j.to_dict()) == j


def test_robocopy_translation():
    r = robocopy.parse([r"C:\src", r"D:\dst", "*.docx", "/MIR", "/XD", "node_modules", ".git", "/XF", "*.tmp",
                        "/R:2", "/W:1", "/MT:16", "/COPYALL", "/RH:2200-0600", "/LOG+:out.log", "/NP"])
    s = r.spec
    assert s["source"] == r"C:\src" and s["dest"] == r"D:\dst"
    assert s["mode"] == "mirror" and s["delete_mode"] == "delete"
    assert s["filters"]["include"] == ["*.docx"]
    assert s["filters"]["exclude_dirs"] == ["node_modules", ".git"]
    assert s["filters"]["exclude"] == ["*.tmp"]
    assert s["retries"] == 2 and s["retry_wait"] == 1 and s["threads"] == 16
    assert s["metadata"]["owner"] and s["metadata"]["acl"]
    assert s["windows"] == ["22:00-06:00"]
    assert r.log_file == "out.log" and r.log_append
    assert "max_depth" not in s["filters"]
    # robocopy without /S /E copies only the top folder; /XO -> newer
    r2 = robocopy.parse(["/data/a", "/data/b", "/XO", "/SL"])
    assert r2.spec["filters"]["max_depth"] == 0
    assert r2.spec["overwrite"] == "newer"
    assert r2.spec["metadata"]["symlinks"] == "copy"
    assert robocopy.exit_code({"phase": "done", "done_files": 3, "deleted": 1}) == 3
    assert robocopy.exit_code({"phase": "completed_with_errors", "done_files": 1}) == 9


def test_full_location_and_relative_paths_rejected(tmp_path, monkeypatch):
    import os
    import pytest
    from ferry.spec import JobSpec, full_location
    monkeypatch.chdir(tmp_path)
    assert full_location("Photos") == str(tmp_path / "Photos")
    assert full_location("~/x") == os.path.abspath(os.path.expanduser("~/x"))
    for remote in ("sftp://me@host/data", "gdrive:Backup/Docs"):
        assert full_location(remote) == remote
    with pytest.raises(ValueError, match="full path"):
        JobSpec.from_dict({"source": "Photos", "dest": str(tmp_path / "b")}).validate()
    JobSpec.from_dict({"source": full_location("Photos"), "dest": "gdrive:b"}).validate()


def test_theme_setting_and_terminal_guess(monkeypatch):
    from ferry.config import Config
    from ferry.tui import terminal_is_dark
    assert Config().get("ui.theme") == "system"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert terminal_is_dark() is False
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert terminal_is_dark() is True
    monkeypatch.delenv("COLORFGBG")
    assert terminal_is_dark() is True
