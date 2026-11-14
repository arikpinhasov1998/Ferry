"""Telegram bot: remote status, control and notifications.

One bot per machine. Put all your machines' bots in one Telegram group to see
every machine in one place; address a single machine with ``@machine`` as the
first argument (e.g. ``/status @nas``).
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

from ferry import __version__
from ferry.config import data_dir, platform_label
from ferry.engine import Phase
from ferry.util import human_bytes, human_duration, progress_bar

log = logging.getLogger("ferry.telegram")

PHASE_LABEL = {
    "pending": "⏳ starting", "scanning": "🔎 scanning", "awaiting_confirmation": "❓ waiting for confirmation",
    "copying": "🚚 copying", "deleting": "🗑 cleaning up", "finishing": "🧹 finishing",
    "done": "✅ done", "completed_with_errors": "⚠️ done with errors", "failed": "❌ failed",
    "cancelled": "⏹ cancelled", "interrupted": "⏸ interrupted",
}

COMMANDS = [
    ("status", "Progress, elapsed time and ETA of running jobs"),
    ("jobs", "Saved jobs (tap to run)"),
    ("run", "Run a saved job: /run <job> [dry]"),
    ("pause", "Pause: /pause [run|all]"),
    ("resume", "Resume: /resume [run|all]"),
    ("cancel", "Cancel a run: /cancel [run]"),
    ("confirm", "Approve pending deletions/overwrites"),
    ("errors", "Failed files of a run"),
    ("log", "Download the log of a run"),
    ("report", "Download a CSV of failed files"),
    ("history", "Recent runs"),
    ("disk", "Free space at job destinations"),
    ("machine", "Machine and Ferry info"),
    ("notify", "Notifications on/off for this chat"),
    ("every", "Progress updates: /every 10% | 15m | off"),
    ("whoami", "Show your Telegram IDs"),
    ("help", "Help"),
]


class TelegramApi:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, _timeout: float = 30, **params):
        data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + method, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=_timeout) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read())
            except Exception:
                raise RuntimeError(f"Telegram HTTP {e.code}") from None
        if not res.get("ok"):
            err = RuntimeError(res.get("description", "Telegram error"))
            err.code = res.get("error_code")
            raise err
        return res["result"]

    def send(self, chat_id, text, buttons=None, reply_to=None):
        return self.call("sendMessage", chat_id=chat_id, text=text[:4096], parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=_kb(buttons),
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                         if reply_to else None)

    def edit(self, chat_id, message_id, text, buttons=None):
        try:
            return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4096],
                             parse_mode="HTML", disable_web_page_preview=True, reply_markup=_kb(buttons))
        except RuntimeError as e:
            if "not modified" not in str(e):
                raise

    def send_document(self, chat_id, filename: str, content: bytes, caption: str = ""):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("chat_id", str(chat_id)), ("caption", caption[:1024]), ("parse_mode", "HTML")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.base + "sendDocument", data=b"".join(parts), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())


def _kb(buttons):
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


class TelegramBot:
    def __init__(self, daemon):
        self.daemon = daemon
        self.cfg = daemon.config
        self.manager = daemon.manager
        self.api = TelegramApi(self.cfg.get("telegram.token"))
        self.machine = self.cfg.get("machine_name")
        self.stop_event = threading.Event()
        self.running = False
        self.offset = None
        self.username = None
        self.last_error = None
        self.progress_state: dict[str, dict] = {}   # run id -> {"pct": float, "time": float}
        self.manager.subscribe(self._on_event)

    # ------------------------------------------------------------ lifecycle --
    def start(self):
        threading.Thread(target=self._poll_loop, daemon=True, name="tg-poll").start()
        threading.Thread(target=self._progress_loop, daemon=True, name="tg-progress").start()

    def stop(self):
        self.stop_event.set()
        try:
            self.manager.listeners.remove(self._on_event)
        except ValueError:
            pass

    # ---------------------------------------------------------- utilities --
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in (self.cfg.get("telegram.allowed_ids") or [])}

    def notify_chats(self) -> list:
        chats = self.cfg.get("telegram.notify_chat_ids") or self.cfg.get("telegram.allowed_ids") or []
        return list(dict.fromkeys(chats))

    def header(self) -> str:
        return f"🖥 <b>{esc(self.machine)}</b>"

    def broadcast(self, text, buttons=None):
        for chat in self.notify_chats():
            try:
                self.api.send(chat, text, buttons)
            except Exception as e:
                log.warning("notify %s failed: %s", chat, e)

    # ----------------------------------------------------------- polling --
    def _poll_loop(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                me = self.api.call("getMe")
                self.username = me.get("username")
                self.api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
                break
            except Exception as e:
                log.warning("Telegram not reachable yet: %s", e)
                self.last_error = ("The bot token was rejected by Telegram" if getattr(e, "code", None) == 401
                                   else f"Telegram not reachable: {e}")
                if self.stop_event.wait(min(backoff, 60)):
                    return
                backoff *= 2
        self.running = True
        self.last_error = None
        log.info("Telegram bot @%s active", self.username)
        backoff = 1
        while not self.stop_event.is_set():
            try:
                updates = self.api.call("getUpdates", _timeout=70, timeout=50, offset=self.offset,
                                        allowed_updates=["message", "callback_query"])
                backoff = 1
                self.last_error = None
            except Exception as e:
                if getattr(e, "code", None) == 409:
                    log.error("Another program is using this bot token. Each machine needs its own bot "
                              "(create one per machine with @BotFather).")
                    self.last_error = "Another machine or program is using this bot token"
                    wait = 30
                else:
                    log.warning("getUpdates failed: %s", e)
                    self.last_error = f"Telegram not reachable: {e}"
                    wait = min(backoff, 60)
                    backoff *= 2
                if self.stop_event.wait(wait):
                    break
                continue
            for upd in updates:
                self.offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        self._on_callback(upd["callback_query"])
                    elif "message" in upd:
                        self._on_message(upd["message"])
                except Exception:
                    log.exception("handling update failed")
        self.running = False

    # ---------------------------------------------------------- messages --
    def _authorized(self, user_id, chat_id) -> bool:
        ids = self.allowed_ids()
        return user_id in ids or chat_id in ids

    def _on_message(self, msg):
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        parts = text.split()
        cmd = parts[0][1:]
        if "@" in cmd:
            cmd, target = cmd.split("@", 1)
            if self.username and target.lower() != self.username.lower():
                return
        cmd = cmd.lower()
        args = parts[1:]
        # "@machine" addressing for groups with several Ferry bots
        if args and args[0].startswith("@"):
            if args[0][1:].lower() not in (self.machine.lower(), "all"):
                return
            args = args[1:]
        if cmd == "whoami":
            return self.api.send(chat_id, f"{self.header()}\nYour user ID: <code>{user_id}</code>\n"
                                          f"This chat ID: <code>{chat_id}</code>")
        if not self._authorized(user_id, chat_id):
            if msg["chat"]["type"] == "private":
                self.api.send(chat_id, f"⛔ Not authorized.\nYour user ID is <code>{user_id}</code>.\n"
                                       f"On the machine run:\n<code>ferry telegram allow {user_id}</code>")
            log.warning("unauthorized Telegram user %s in chat %s", user_id, chat_id)
            return
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return self.api.send(chat_id, "Unknown command. /help")
        try:
            handler(chat_id, args)
        except (KeyError, ValueError, RuntimeError) as e:
            self.api.send(chat_id, f"{self.header()}\n⚠️ {esc(str(e).strip(chr(39)))}")

    def _on_callback(self, cq):
        chat_id = cq["message"]["chat"]["id"]
        msg_id = cq["message"]["message_id"]
        if not self._authorized(cq["from"]["id"], chat_id):
            return self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text="Not authorized")
        data = cq.get("data", "")
        action, _, arg = data.partition(":")
        note = None
        try:
            if action == "st":
                pass
            elif action == "p":
                self.manager.pause(arg)
                note = "Paused"
            elif action == "r":
                self.manager.resume(arg)
                note = "Resumed"
            elif action == "c":
                self.manager.cancel(arg)
                note = "Cancelling…"
            elif action == "cf":
                rid, _, choice = arg.rpartition(":")
                self.manager.confirm(rid, choice)
                note = {"all": "Applying all changes", "skip": "Skipping deletions/overwrites",
                        "cancel": "Cancelled"}.get(choice)
                arg = rid
            elif action == "run":
                run = self.manager.start_job(arg, trigger="telegram")
                note = "Started"
                arg = run.id
                self.api.send(chat_id, self.render_run(run.info()), self.run_buttons(run.info()))
                return self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text=note)
            elif action == "err":
                self.cmd_errors(chat_id, [arg])
            elif action == "log":
                self.cmd_log(chat_id, [arg])
        except (KeyError, RuntimeError, ValueError) as e:
            return self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text=str(e)[:190],
                                 show_alert=True)
        self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        if action in ("st", "p", "r", "c", "cf"):
            time.sleep(0.4)
            run = self.manager.get(arg)
            if run:
                info = run.info()
                self.api.edit(chat_id, msg_id, self.render_run(info), self.run_buttons(info))

    # ---------------------------------------------------------- rendering --
    def render_run(self, r: dict, title: str | None = None) -> str:
        phase = r["phase"]
        label = PHASE_LABEL.get(phase, phase)
        if r.get("paused") and phase not in Phase.FINAL:
            label = "⏸ paused"
        elif r.get("waiting_window") and phase not in Phase.FINAL:
            label = "🌙 waiting for allowed time window"
        lines = [self.header()]
        if title:
            lines.append(title)
        lines.append(f"📦 <b>{esc(r['job_name'])}</b> · {esc(r['mode'])}{' (dry run)' if r.get('dry_run') else ''}"
                     f" — {label}")
        lines.append(f"<code>{esc(r['source'])}</code>\n→ <code>{esc(r['dest'])}</code>")
        if phase == "scanning":
            lines.append(f"🔎 {r.get('scanned', 0):,} entries scanned")
        else:
            pct = r.get("percent", 0)
            lines.append(f"{progress_bar(pct / 100, 16)} {pct:.1f}%")
            lines.append(f"📄 {r['done_files']:,} / {r['total_files']:,} files · "
                         f"💾 {human_bytes(r['done_bytes'])} / {human_bytes(r['total_bytes'])}")
        if phase not in Phase.FINAL and phase not in ("scanning", "awaiting_confirmation"):
            lim = f" (limit {human_bytes(r['bwlimit'])}/s)" if r.get("bwlimit") else ""
            lines.append(f"⚡ {human_bytes(r.get('speed', 0))}/s · {r.get('files_per_sec', 0):.1f} files/s{lim}")
        el = f"⏱ Elapsed {human_duration(r.get('elapsed'))}"
        if r.get("eta") is not None and phase not in Phase.FINAL:
            finish = (datetime.now() + timedelta(seconds=r["eta"])).strftime("%H:%M")
            el += f" · ⏳ Left {human_duration(r['eta'])} (≈{finish})"
        lines.append(el)
        extra = []
        if r.get("same_files"):
            extra.append(f"✔️ {r['same_files']:,} unchanged")
        if r.get("failed"):
            extra.append(f"❌ {r['failed']:,} failed")
        if r.get("deleted"):
            extra.append(f"🗑 {r['deleted']:,} removed")
        if r.get("skipped"):
            extra.append(f"⏭ {r['skipped']:,} skipped")
        if extra:
            lines.append(" · ".join(extra))
        pc = r.get("pending_confirmation")
        if pc:
            lines.append(f"\n❓ <b>Needs your OK:</b> {pc['deletes']:,} deletions, {pc['overwrites']:,} overwrites")
            for s in pc.get("sample", [])[:8]:
                lines.append(f"  • <code>{esc(s)}</code>")
        cur = r.get("current") or []
        if cur and phase == "copying":
            c = max(cur, key=lambda x: x["size"])
            if c["size"] > 50 * 1024 * 1024:
                lines.append(f"📍 <code>{esc(c['path'][-60:])}</code> {c['done'] * 100 // max(1, c['size'])}%")
        if r.get("warnings"):
            lines.append("⚠️ " + esc(r["warnings"][0]["message"])[:200])
        if r.get("error"):
            lines.append(f"❌ {esc(r['error'])[:300]}")
        lines.append(f"<i>run {esc(r['id'])}</i>")
        return "\n".join(lines)

    def run_buttons(self, r: dict):
        rid = r["id"]
        if r["phase"] in Phase.FINAL:
            row = [("📄 Log", f"log:{rid}")]
            if r.get("failed"):
                row.insert(0, ("❌ Errors", f"err:{rid}"))
            if r.get("job_id"):
                row.append(("🔁 Run again", f"run:{r['job_id']}"))
            return [row]
        if r["phase"] == "awaiting_confirmation":
            return [[("✅ Apply all", f"cf:{rid}:all"), ("🛡 Skip deletes", f"cf:{rid}:skip")],
                    [("⏹ Cancel", f"cf:{rid}:cancel")]]
        pr = ("▶️ Resume", f"r:{rid}") if r.get("paused") else ("⏸ Pause", f"p:{rid}")
        return [[pr, ("⏹ Cancel", f"c:{rid}"), ("🔄", f"st:{rid}")]]

    def _pick_run(self, args):
        if args:
            r = self.manager.get(args[0])
            if not r:
                raise KeyError(f"no run or job named {args[0]}")
            return r
        active = self.manager.active()
        if active:
            return active[-1]
        runs = self.manager.all_runs()
        if runs:
            return runs[-1]
        raise KeyError("no runs yet")

    # ---------------------------------------------------------- commands --
    def cmd_start(self, chat, args):
        self.cmd_help(chat, args)

    def cmd_help(self, chat, args):
        lines = [f"{self.header()} · Ferry {__version__}", ""]
        lines += [f"/{c} — {esc(d)}" for c, d in COMMANDS]
        lines += ["", "Several machines in one group? Add <code>@machine</code> after a command, "
                      "e.g. <code>/status @" + esc(self.machine) + "</code>"]
        self.api.send(chat, "\n".join(lines))

    def cmd_status(self, chat, args):
        if args:
            r = self._pick_run(args).info()
            return self.api.send(chat, self.render_run(r), self.run_buttons(r))
        active = self.manager.active()
        if not active:
            runs = self.manager.all_runs()
            text = f"{self.header()}\n💤 Idle — no jobs running."
            if runs:
                last = runs[-1].info()
                text += (f"\nLast: <b>{esc(last['job_name'])}</b> {PHASE_LABEL.get(last['phase'], last['phase'])}"
                         f" ({human_duration(last['elapsed'])})")
            return self.api.send(chat, text)
        for run in active:
            info = run.info()
            self.api.send(chat, self.render_run(info), self.run_buttons(info))

    def cmd_jobs(self, chat, args):
        jobs = self.manager.jobs.list()
        if not jobs:
            return self.api.send(chat, f"{self.header()}\nNo saved jobs. Create one with <code>ferry job add</code>.")
        lines = [self.header(), "<b>Saved jobs</b>"]
        buttons = []
        for j in jobs:
            flags = []
            if j.schedule:
                flags.append(f"⏰ {esc(j.schedule)}")
            if j.watch:
                flags.append("👁 live")
            running = "🚚 " if self.manager.is_job_running(j.id) else ""
            lines.append(f"• {running}<b>{esc(j.name)}</b> ({j.mode}) {' '.join(flags)}\n"
                         f"  <code>{esc(j.source)}</code> → <code>{esc(j.dest)}</code>")
            buttons.append((f"▶️ {j.name}"[:30], f"run:{j.id}"))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        self.api.send(chat, "\n".join(lines), rows[:50])

    def cmd_run(self, chat, args):
        if not args:
            return self.cmd_jobs(chat, args)
        dry = len(args) > 1 and args[1].lower() in ("dry", "dry-run", "test")
        run = self.manager.start_job(args[0], trigger="telegram", dry_run=dry)
        time.sleep(0.5)
        info = run.info()
        self.api.send(chat, self.render_run(info, "▶️ Started"), self.run_buttons(info))

    def cmd_pause(self, chat, args):
        if args and args[0].lower() == "all":
            self.manager.pause_all()
            return self.api.send(chat, f"{self.header()}\n⏸ All runs paused.")
        r = self._pick_run(args)
        r.engine.pause()
        self.api.send(chat, self.render_run(r.info()), self.run_buttons(r.info()))

    def cmd_resume(self, chat, args):
        if args and args[0].lower() == "all":
            self.manager.resume_all()
            return self.api.send(chat, f"{self.header()}\n▶️ All runs resumed.")
        r = self._pick_run(args)
        r.engine.resume()
        self.api.send(chat, self.render_run(r.info()), self.run_buttons(r.info()))

    def cmd_cancel(self, chat, args):
        r = self._pick_run(args)
        r.engine.cancel()
        self.api.send(chat, f"{self.header()}\n⏹ Cancelling <b>{esc(r.spec.name)}</b>…")

    def cmd_confirm(self, chat, args):
        r = self._pick_run(args)
        info = r.info()
        if info["phase"] != "awaiting_confirmation":
            return self.api.send(chat, f"{self.header()}\nNothing is waiting for confirmation.")
        self.api.send(chat, self.render_run(info), self.run_buttons(info))

    def cmd_errors(self, chat, args):
        r = self._pick_run(args)
        fails = self.manager.failures(r.id, 30)
        if not fails:
            return self.api.send(chat, f"{self.header()}\n✅ No failed files in run {esc(r.id)}.")
        lines = [self.header(), f"❌ <b>Failed files</b> — {esc(r.spec.name)} (first {len(fails)})"]
        for f in fails:
            lines.append(f"• <code>{esc(f['path'][-80:])}</code>\n  {esc((f['error'] or '')[:150])}")
        self.api.send(chat, "\n".join(lines)[:4000], [[("📄 Log", f"log:{r.id}")]])
        if r.info()["failed"] > len(fails):
            self.cmd_report(chat, [r.id])

    def cmd_report(self, chat, args):
        r = self._pick_run(args)
        fails = self.manager.failures(r.id, 1_000_000)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["action", "path", "error", "attempts"])
        for f in fails:
            w.writerow([f["kind"], f["path"], f["error"], f["attempts"]])
        self.api.send_document(chat, f"ferry-{r.id}-failures.csv", buf.getvalue().encode("utf-8"),
                               f"{self.header()} · {len(fails)} failed item(s)")

    def cmd_log(self, chat, args):
        r = self._pick_run(args)
        p = self.manager.log_path(r.id)
        if not p:
            return self.api.send(chat, "No log for this run.")
        data = p.read_bytes()
        limit = 45 * 1024 * 1024
        if len(data) > limit:
            data = b"[...truncated, showing the end...]\n" + data[-limit:]
        self.api.send_document(chat, f"ferry-{r.id}.log", data, f"{self.header()} · log of {esc(r.spec.name)}")

    def cmd_history(self, chat, args):
        hist = self.manager.history(10)
        if not hist:
            return self.api.send(chat, f"{self.header()}\nNo finished runs yet.")
        lines = [self.header(), "<b>Recent runs</b>"]
        for h in hist:
            when = datetime.fromtimestamp(h["started"]).strftime("%d.%m %H:%M") if h.get("started") else "?"
            lines.append(f"{PHASE_LABEL.get(h['phase'], h['phase'])[:2]} {when} <b>{esc(h['job_name'])}</b> — "
                         f"{h['done_files']:,} files, {human_bytes(h['done_bytes'])}, "
                         f"{human_duration(h['elapsed'])}{', ❌ ' + str(h['failed']) if h.get('failed') else ''}")
        self.api.send(chat, "\n".join(lines))

    def cmd_disk(self, chat, args):
        from ferry.backends import LocalBackend
        lines = [self.header(), "<b>Free space</b>"]
        seen = set()
        for j in self.manager.jobs.list():
            if j.dest in seen:
                continue
            seen.add(j.dest)
            try:
                b = LocalBackend(j.dest) if "://" not in j.dest else None
                free = b.free_space() if b else None
                lines.append(f"• <code>{esc(j.dest)}</code>: {human_bytes(free) if free is not None else 'n/a'}")
            except Exception:
                lines.append(f"• <code>{esc(j.dest)}</code>: n/a")
        try:
            lines.append(f"• data folder: {human_bytes(shutil.disk_usage(data_dir()).free)}")
        except OSError:
            pass
        self.api.send(chat, "\n".join(lines))

    def cmd_machine(self, chat, args):
        d = self.daemon
        jobs = self.manager.jobs.list()
        self.api.send(chat, "\n".join([
            self.header(),
            f"Ferry {__version__} on {esc(platform_label())}",
            f"Service up {human_duration(time.time() - d.started)}",
            f"{len(jobs)} saved jobs, {len(self.manager.active())} running",
            f"Scheduled: {sum(1 for j in jobs if j.schedule and j.enabled)} · "
            f"Live sync: {len(d.watchers.watchers) if d.watchers else 0}",
        ]))

    def cmd_notify(self, chat, args):
        chats = list(self.cfg.get("telegram.notify_chat_ids") or [])
        on = not args or args[0].lower() in ("on", "1", "yes")
        if on and chat not in chats:
            chats.append(chat)
        if not on and chat in chats:
            chats.remove(chat)
        self.cfg.set("telegram.notify_chat_ids", chats)
        self.api.send(chat, f"{self.header()}\n🔔 Notifications {'on' if on else 'off'} for this chat.")

    def cmd_every(self, chat, args):
        if not args:
            p = self.cfg.get("telegram.progress_every_percent")
            m = self.cfg.get("telegram.progress_every_minutes")
            return self.api.send(chat, f"Progress updates: every {p or '-'}% / {m or '-'} min. "
                                       f"Change: <code>/every 10%</code>, <code>/every 30m</code>, "
                                       f"<code>/every off</code>")
        a = args[0].lower()
        if a == "off":
            self.cfg.set("telegram.progress_every_percent", 0)
            self.cfg.set("telegram.progress_every_minutes", 0)
        elif a.endswith("%"):
            self.cfg.set("telegram.progress_every_percent", int(a[:-1]))
        else:
            self.cfg.set("telegram.progress_every_minutes", int(a.rstrip("min")))
        self.api.send(chat, f"{self.header()}\n👍 Progress updates set to {esc(a)}.")

    # ------------------------------------------------------ notifications --
    def _on_event(self, event, info, data):
        tg = self.cfg.get("telegram") or {}
        if not info.get("notify", True):
            if event != "confirm":
                return
        quiet = info.get("quiet")
        if event == "start":
            self.progress_state[info["id"]] = {"pct": 0.0, "time": time.time()}
            if tg.get("notify_start") and not quiet:
                self.broadcast(self.render_run(info, "▶️ Job started"), self.run_buttons(info))
        elif event == "finish":
            self.progress_state.pop(info["id"], None)
            failed = info["phase"] in (Phase.FAILED, Phase.PARTIAL)
            if info["phase"] == Phase.INTERRUPTED:
                return
            if (failed and tg.get("notify_errors")) or (tg.get("notify_finish") and not quiet):
                title = "❌ Job failed" if info["phase"] == Phase.FAILED else (
                    "⚠️ Finished with errors" if failed else "✅ Job finished")
                self.broadcast(self.render_run(info, title), self.run_buttons(info))
        elif event == "confirm":
            self.broadcast(self.render_run(info, "❓ Confirmation needed"), self.run_buttons(info))

    def _progress_loop(self):
        while not self.stop_event.wait(10):
            every_pct = int(self.cfg.get("telegram.progress_every_percent") or 0)
            every_min = float(self.cfg.get("telegram.progress_every_minutes") or 0)
            if not every_pct and not every_min:
                continue
            for run in self.manager.active():
                if run.quiet or not run.spec.notify:
                    continue
                info = run.info()
                if info["phase"] != "copying":
                    continue
                st = self.progress_state.setdefault(run.id, {"pct": 0.0, "time": time.time()})
                due = False
                if every_pct and info["percent"] >= st["pct"] + every_pct and info["percent"] < 100:
                    due = True
                if every_min and time.time() - st["time"] >= every_min * 60:
                    due = True
                if due:
                    st["pct"] = info["percent"] - (info["percent"] % every_pct if every_pct else 0)
                    st["time"] = time.time()
                    self.broadcast(self.render_run(info, "📈 Progress"), self.run_buttons(info))
