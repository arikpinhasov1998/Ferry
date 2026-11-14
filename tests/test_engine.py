import os
import sqlite3
import stat
import threading
import time

import pytest

from conftest import make_tree
from ferry.engine import CHUNK, DELTA_MIN, RESUME_MIN, Engine, Phase
from ferry.spec import JobSpec

IS_WIN = os.name == "nt"


def run(tmp_path, src, dst, name="r", **kw):
    spec = JobSpec(source=str(src), dest=str(dst), **kw)
    e = Engine(spec, str(tmp_path / "runs" / name), defaults={"retries": 0, "retry_wait": 0})
    e.run()
    return e


def tree(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".ferry-archive"]
        for f in filenames:
            p = os.path.join(dirpath, f)
            out[os.path.relpath(p, root).replace("\\", "/")] = open(p, "rb").read()
    return out


@pytest.fixture
def src(tmp_path):
    s = tmp_path / "src"
    make_tree(s, {"a.txt": "alpha", "dir/b.txt": "bravo", "dir/sub/c.bin": os.urandom(3000),
                  "skip.tmp": "tmp"})
    (s / "emptydir").mkdir()
    return s


def test_copy_and_idempotent(tmp_path, src):
    dst = tmp_path / "dst"
    e = run(tmp_path, src, dst, "1", verify="xxhash")
    assert e.phase == Phase.DONE
    assert tree(dst) == tree(src)
    assert (dst / "emptydir").is_dir()
    assert e.done_files == 4 and e.failed == 0
    # timestamps preserved
    assert abs(os.stat(dst / "a.txt").st_mtime - os.stat(src / "a.txt").st_mtime) < 0.01
    e2 = run(tmp_path, src, dst, "2")
    assert e2.done_files == 0 and e2.same_files == 4


def test_filters_and_no_empty_dirs(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, filters={"exclude": ["*.tmp"], "exclude_dirs": ["sub"]}, empty_dirs=False)
    assert set(tree(dst)) == {"a.txt", "dir/b.txt"}
    assert not (dst / "emptydir").exists()


def test_mirror_delete_and_archive(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    make_tree(dst, {"extra.txt": "x", "olddir/f.txt": "y"})
    (src / "a.txt").write_text("alpha changed")
    e = run(tmp_path, src, dst, "2", mode="mirror", delete_mode="archive")
    assert e.phase == Phase.DONE
    assert tree(dst) == tree(src)
    arch = list((dst / ".ferry-archive").iterdir())[0]
    assert (arch / "extra.txt").read_text() == "x"
    assert (arch / "olddir" / "f.txt").read_text() == "y"
    assert (arch / "a.txt").read_text() == "alpha"           # old version kept
    make_tree(dst, {"extra2.txt": "z"})
    e = run(tmp_path, src, dst, "3", mode="mirror", delete_mode="delete")
    assert not (dst / "extra2.txt").exists() and e.deleted == 1
    assert (dst / ".ferry-archive").exists()                  # never deleted by mirror


def test_mirror_keeps_excluded(tmp_path, src):
    dst = tmp_path / "dst"
    make_tree(dst, {"keep.tmp": "mine"})
    run(tmp_path, src, dst, mode="mirror", delete_mode="delete", filters={"exclude": ["*.tmp"]})
    assert (dst / "keep.tmp").read_text() == "mine"


def test_confirm_skip(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    make_tree(dst, {"extra.txt": "x"})
    (src / "a.txt").write_text("new content")
    spec = JobSpec(source=str(src), dest=str(dst), mode="mirror", delete_mode="confirm")
    e = Engine(spec, str(tmp_path / "runs" / "2"))
    t = threading.Thread(target=e.run)
    t.start()
    for _ in range(100):
        if e.phase == Phase.CONFIRM:
            break
        time.sleep(0.05)
    assert e.phase == Phase.CONFIRM
    assert e.pending_confirmation == {"deletes": 1, "overwrites": 1, "sample": ["a.txt", "extra.txt"]}
    e.confirm("skip")
    t.join(10)
    assert e.phase == Phase.DONE
    assert (dst / "extra.txt").exists()
    assert (dst / "a.txt").read_text() == "alpha"


def test_confirm_all(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    make_tree(dst, {"extra.txt": "x"})
    spec = JobSpec(source=str(src), dest=str(dst), mode="mirror", delete_mode="confirm")
    e = Engine(spec, str(tmp_path / "runs" / "2"))
    t = threading.Thread(target=e.run)
    t.start()
    while e.phase != Phase.CONFIRM:
        time.sleep(0.02)
    e.confirm("all")
    t.join(10)
    assert not (dst / "extra.txt").exists()


def test_move(tmp_path, src):
    dst = tmp_path / "dst"
    expected = tree(src)
    e = run(tmp_path, src, dst, mode="move", move_dirs=True)
    assert e.phase == Phase.DONE
    assert tree(dst) == expected
    assert tree(src) == {}
    assert not (src / "dir").exists()


def test_dry_run(tmp_path, src):
    dst = tmp_path / "dst"
    e = run(tmp_path, src, dst, dry_run=True)
    assert e.phase == Phase.DONE and not dst.exists()
    assert e.plan_counts["copy"] == 4


def test_single_file_source(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src / "dir" / "b.txt", dst)
    assert tree(dst) == {"b.txt": b"bravo"}


def test_overwrite_policies(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    (dst / "a.txt").write_text("dest is newer")
    future = time.time() + 100
    os.utime(dst / "a.txt", (future, future))
    run(tmp_path, src, dst, "2", overwrite="newer")
    assert (dst / "a.txt").read_text() == "dest is newer"
    run(tmp_path, src, dst, "3", overwrite="never")
    assert (dst / "a.txt").read_text() == "dest is newer"
    run(tmp_path, src, dst, "4")   # different (default)
    assert (dst / "a.txt").read_text() == "alpha"
    # same size + same mtime but different content: only checksum notices
    (dst / "a.txt").write_text("ALPHA")
    st = os.stat(src / "a.txt")
    os.utime(dst / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns))
    run(tmp_path, src, dst, "5")
    assert (dst / "a.txt").read_text() == "ALPHA"
    run(tmp_path, src, dst, "6", overwrite="checksum")
    assert (dst / "a.txt").read_text() == "alpha"


@pytest.mark.skipif(IS_WIN, reason="posix permissions")
def test_permissions_and_symlinks(tmp_path, src):
    os.chmod(src / "a.txt", 0o640)
    os.symlink("a.txt", src / "link")
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    assert stat.S_IMODE(os.stat(dst / "a.txt").st_mode) == 0o640
    assert os.path.islink(dst / "link") and os.readlink(dst / "link") == "a.txt"
    dst2 = tmp_path / "dst2"
    run(tmp_path, src, dst2, "2", metadata={"symlinks": "follow"})
    assert not os.path.islink(dst2 / "link") and (dst2 / "link").read_text() == "alpha"


def test_readonly_destination_is_overwritten(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    os.chmod(dst / "a.txt", 0o444)
    (src / "a.txt").write_text("updated")
    os.chmod(src / "a.txt", 0o444)
    e = run(tmp_path, src, dst, "2", delete_mode="delete")
    assert e.failed == 0 and (dst / "a.txt").read_text() == "updated"


def test_resume_partial_file(tmp_path):
    src = tmp_path / "src"
    data = os.urandom(RESUME_MIN + 3 * CHUNK + 123)
    make_tree(src, {"big.bin": data})
    dst = tmp_path / "dst"
    dst.mkdir()
    # simulate an interrupted transfer: a partial file with the right tag
    import hashlib
    st = os.stat(src / "big.bin")
    tag = hashlib.md5(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:8]
    part = dst / f".big.bin.{tag}.ferrypart"
    part.write_bytes(data[:RESUME_MIN + CHUNK + 500])
    e = run(tmp_path, src, dst, verify="sha256")
    assert e.phase == Phase.DONE
    assert (dst / "big.bin").read_bytes() == data
    assert not part.exists()
    assert e.xfer_bytes < len(data)      # only the remainder was transferred


def test_delta_update(tmp_path):
    src = tmp_path / "src"
    data = bytearray(os.urandom(DELTA_MIN + 10))
    make_tree(src, {"vm.img": bytes(data)})
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    data[5000:5010] = b"0123456789"
    (src / "vm.img").write_bytes(bytes(data))
    e = run(tmp_path, src, dst, "2", delete_mode="delete", verify="xxhash")
    assert (dst / "vm.img").read_bytes() == bytes(data)
    assert e.failed == 0


def test_interrupt_and_resume_plan(tmp_path):
    src = tmp_path / "src"
    make_tree(src, {f"f{i:03}.bin": os.urandom(20000) for i in range(60)})
    dst = tmp_path / "dst"
    spec = JobSpec(source=str(src), dest=str(dst), threads=1, bwlimit="200k")
    rd = str(tmp_path / "runs" / "x")
    e = Engine(spec, rd)
    t = threading.Thread(target=e.run)
    t.start()
    while e.done_files < 5:
        time.sleep(0.02)
    e.interrupt()
    t.join(10)
    assert e.phase == Phase.INTERRUPTED
    db = sqlite3.connect(os.path.join(rd, "plan.db"))
    assert db.execute("SELECT value FROM meta WHERE key='final_phase'").fetchone() is None
    db.close()
    spec.bwlimit = None
    e2 = Engine(spec, rd)
    e2.run()
    assert e2.phase == Phase.DONE
    assert len(tree(dst)) == 60 and tree(dst) == tree(src)
    assert e2.done_files == 60


def test_bandwidth_limit(tmp_path):
    src = tmp_path / "src"
    make_tree(src, {"x.bin": os.urandom(3 * 1024 * 1024)})
    t0 = time.time()
    run(tmp_path, src, tmp_path / "dst", bwlimit="2M")
    assert time.time() - t0 >= 0.9


def test_pause_resume_cancel(tmp_path):
    src = tmp_path / "src"
    make_tree(src, {f"f{i}.bin": os.urandom(50000) for i in range(40)})
    spec = JobSpec(source=str(src), dest=str(tmp_path / "dst"), threads=2, bwlimit="500k")
    e = Engine(spec, str(tmp_path / "runs" / "p"))
    t = threading.Thread(target=e.run)
    t.start()
    while e.done_files < 2:
        time.sleep(0.02)
    e.pause()
    time.sleep(0.6)
    n = e.done_files
    time.sleep(0.6)
    assert e.done_files <= n + 2          # in-flight files may finish their current chunk
    assert e.status()["paused"]
    e.resume()
    time.sleep(0.3)
    e.cancel()
    t.join(10)
    assert e.phase == Phase.CANCELLED


def test_scoped_scan(tmp_path, src):
    dst = tmp_path / "dst"
    run(tmp_path, src, dst, "1")
    make_tree(src, {"dir/new.txt": "n", "dir/newsub/deep.txt": "d", "a.txt": "changed outside scope"})
    spec = JobSpec(source=str(src), dest=str(dst), mode="mirror", delete_mode="delete")
    e = Engine(spec, str(tmp_path / "runs" / "2"), scope=["dir", "dir/newsub"])
    e.run()
    assert (dst / "dir" / "new.txt").read_text() == "n"
    assert (dst / "dir" / "newsub" / "deep.txt").read_text() == "d"
    assert (dst / "a.txt").read_text() == "alpha"     # not in scope: untouched
    assert e.plan_counts["copy"] == 2                   # no duplicate actions


def test_missing_source_fails(tmp_path):
    e = run(tmp_path, tmp_path / "nope", tmp_path / "dst")
    assert e.phase == Phase.FAILED and "source not found" in e.error


@pytest.mark.skipif(IS_WIN or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs non-root posix")
def test_unreadable_file_reported(tmp_path, src):
    os.chmod(src / "a.txt", 0)
    e = run(tmp_path, src, tmp_path / "dst")
    os.chmod(src / "a.txt", 0o644)
    assert e.phase == Phase.PARTIAL and e.failed == 1
    from ferry.engine import read_failures
    f = read_failures(os.path.join(tmp_path, "runs", "r"))
    assert f[0]["path"] == "a.txt" and "Permission" in f[0]["error"]


def test_sftp_roundtrip(tmp_path, src, sftp_server):
    root, port = sftp_server
    url = f"sftp://tester@127.0.0.1:{port}/backup"
    e = run(tmp_path, src, url, "up", verify="xxhash")
    assert e.phase == Phase.DONE, e.error
    assert tree(root / "backup") == tree(src)
    e = run(tmp_path, src, url, "up2")
    assert e.same_files == 4 and e.done_files == 0
    back = tmp_path / "back"
    e = run(tmp_path, url, back, "down", mode="mirror")
    assert e.phase == Phase.DONE, e.error
    assert tree(back) == tree(src)

