import os
import threading
import time

import pytest

from conftest import free_port, make_tree
from ferry.api import ApiError, Client
from ferry.config import Config
from ferry.daemon import Daemon


def wait_for(cond, timeout=15, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


@pytest.fixture
def daemon():
    cfg = Config()
    cfg.set("api.port", free_port())
    d = Daemon(cfg)
    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    c = Client(cfg)
    wait_for(c.alive)
    yield d, c
    d.stop()
    t.join(20)


def test_api_runs_and_jobs(daemon, tmp_path):
    d, c = daemon
    src = tmp_path / "src"
    make_tree(src, {"a.txt": "a", "b/c.txt": "c"})
    info = c.post("/run", {"spec": {"source": str(src), "dest": str(tmp_path / "d1")}})
    final = wait_for(lambda: (r := c.get(f"/runs/{info['id']}"))["phase"] == "done" and r)
    assert final["done_files"] == 2
    assert "copied a.txt" in c.call("GET", f"/runs/{info['id']}/log", raw=True)

    job = c.post("/jobs", {"name": "nightly", "source": str(src), "dest": str(tmp_path / "d2"), "mode": "mirror"})
    assert [j["name"] for j in c.get("/jobs")] == ["nightly"]
    with pytest.raises(ApiError):
        c.post("/jobs", {"name": "Nightly", "source": "/x", "dest": "/y"})     # duplicate name
    r = c.post("/jobs/nightly/run")
    wait_for(lambda: c.get(f"/runs/{r['id']}")["phase"] == "done")
    assert (tmp_path / "d2" / "b" / "c.txt").read_text() == "c"
    assert c.get("/history")[0]["job_name"] == "nightly"
    c.delete(f"/jobs/{job['id']}")
    assert c.get("/jobs") == []
    st = c.get("/status")
    assert st["active"] == [] and st["machine"]


def test_api_auth(daemon):
    d, c = daemon
    bad = Client(d.config)
    bad.config = Config()
    bad.config.data = dict(d.config.data, api=dict(d.config.data["api"], token="wrong"))
    with pytest.raises(ApiError) as e:
        bad.get("/status")
    assert e.value.status == 401


def test_config_masks_token(daemon):
    d, c = daemon
    cfg = c.post("/config", {"telegram.token": "123456:ABCDEFGHIJKLMNOP"})
    assert "ABCDEFGHIJ" not in str(cfg) and "token" not in cfg["api"]
    with pytest.raises(ApiError):
        c.post("/config", {"api.port": 1})


def test_live_watch(daemon, tmp_path):
    pytest.importorskip("watchdog")
    d, c = daemon
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, {"a.txt": "a"})
    c.post("/jobs", {"name": "live", "source": str(src), "dest": str(dst), "mode": "mirror",
                     "delete_mode": "delete", "watch": True, "watch_delay": 1})
    wait_for(lambda: c.get("/status")["watching"])
    make_tree(src, {"new/file.txt": "hello"})
    wait_for(lambda: (dst / "new" / "file.txt").exists(), timeout=20)
    os.remove(src / "new" / "file.txt")
    wait_for(lambda: not (dst / "new" / "file.txt").exists(), timeout=20)


def test_interrupted_run_resumes_on_restart(tmp_path):
    src = tmp_path / "src"
    make_tree(src, {f"f{i:02}.bin": os.urandom(30000) for i in range(40)})
    cfg = Config()
    cfg.set("api.port", free_port())
    d = Daemon(cfg)
    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    c = Client(cfg)
    wait_for(c.alive)
    r = c.post("/run", {"spec": {"source": str(src), "dest": str(tmp_path / "dst"), "threads": 1,
                                 "bwlimit": "100k"}})
    wait_for(lambda: c.get(f"/runs/{r['id']}")["done_files"] >= 3)
    d.stop()                       # service stops mid-run (e.g. logout / reboot)
    t.join(20)
    d2 = Daemon(Config())
    t2 = threading.Thread(target=d2.run_forever, daemon=True)
    t2.start()
    wait_for(c.alive)
    info = wait_for(lambda: (x := c.get(f"/runs/{r['id']}"))["phase"] == "done" and x, timeout=60)
    assert info["trigger"] == "resume"
    assert len(os.listdir(tmp_path / "dst")) == 40
    d2.stop()
    t2.join(20)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.docs = []

    def send(self, chat, text, buttons=None, reply_to=None):
        self.sent.append((chat, text, buttons))

    def edit(self, chat, mid, text, buttons=None):
        self.sent.append((chat, text, buttons))

    def send_document(self, chat, name, content, caption=""):
        self.docs.append((chat, name, content))

    def call(self, method, **kw):
        return {}


def test_telegram_commands(daemon, tmp_path):
    from ferry.telegram import TelegramBot
    d, c = daemon
    d.config.set("telegram.allowed_ids", [42])
    bot = TelegramBot(d)
    fake = bot.api = FakeTelegram()
    bot.username = "ferry_test_bot"

    def msg(text, uid=42, chat=42):
        bot._on_message({"text": text, "chat": {"id": chat, "type": "private"}, "from": {"id": uid}})
        return fake.sent[-1][1] if fake.sent else ""

    assert "Not authorized" in msg("/status", uid=7, chat=7)
    assert "<code>7</code>" in msg("/whoami", uid=7, chat=7)
    assert "Idle" in msg("/status")
    src = tmp_path / "src"
    make_tree(src, {"x.txt": "x"})
    c.post("/jobs", {"name": "photos", "source": str(src), "dest": str(tmp_path / "dst")})
    assert "photos" in msg("/jobs")
    assert fake.sent[-1][2][0][0][1].startswith("run:")
    assert "Started" in msg("/run photos")
    wait_for(lambda: not d.manager.active())
    out = msg("/status photos")
    assert "done" in out and "1 / 1 files" in out
    assert "Recent runs" in msg("/history")
    msg("/log photos")
    assert fake.docs and fake.docs[-1][1].endswith(".log")
    assert "No failed files" in msg("/errors photos")
    # machine addressing in groups: other machine's command is ignored
    n = len(fake.sent)
    bot._on_message({"text": "/status @othermachine", "chat": {"id": 42, "type": "group"}, "from": {"id": 42}})
    assert len(fake.sent) == n
    msg("/every 10%")
    assert d.config.get("telegram.progress_every_percent") == 10
    bot.stop()


def test_telegram_notifications(daemon, tmp_path):
    from ferry.telegram import TelegramBot
    d, c = daemon
    d.config.set("telegram.allowed_ids", [42])
    bot = TelegramBot(d)
    fake = bot.api = FakeTelegram()
    src = tmp_path / "src"
    make_tree(src, {"x.txt": "x"})
    make_tree(tmp_path / "dst", {"extra.txt": "e"})
    r = c.post("/run", {"spec": {"source": str(src), "dest": str(tmp_path / "dst"), "mode": "mirror",
                                 "delete_mode": "confirm"}})
    wait_for(lambda: any("Confirmation needed" in s[1] for s in fake.sent))
    confirm_msg = [s for s in fake.sent if "Confirmation needed" in s[1]][-1]
    assert "extra.txt" in confirm_msg[1]
    data = confirm_msg[2][0][0][1]                         # "cf:<run>:all"
    bot._on_callback({"id": "1", "data": data, "from": {"id": 42},
                      "message": {"chat": {"id": 42}, "message_id": 5}})
    wait_for(lambda: any("Job finished" in s[1] for s in fake.sent))
    assert not (tmp_path / "dst" / "extra.txt").exists()
    assert c.get(f"/runs/{r['id']}")["deleted"] == 1
    bot.stop()
