"""Full-screen terminal UI (for servers and systems without a desktop)."""
from __future__ import annotations

from datetime import datetime

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, Label, RichLog, Select, Static,
                             TabbedContent, TabPane)

from ferry.util import human_bytes, human_duration, progress_bar

FINAL = {"done", "completed_with_errors", "failed", "cancelled", "interrupted"}
PHASE = {"pending": "starting", "scanning": "scanning", "awaiting_confirmation": "NEEDS CONFIRM",
         "copying": "copying", "deleting": "cleanup", "finishing": "finishing", "done": "done ✓",
         "completed_with_errors": "done, errors", "failed": "FAILED", "cancelled": "cancelled",
         "interrupted": "interrupted"}


def _phase(r):
    if r.get("paused") and r["phase"] not in FINAL:
        return "paused"
    if r.get("waiting_window") and r["phase"] not in FINAL:
        return "waiting window"
    return PHASE.get(r["phase"], r["phase"])


class Table(DataTable):
    """DataTable that updates in place and keeps the cursor."""

    def set_rows(self, rows: list[tuple[str, tuple]]):
        keys = [k for k, _ in rows]
        current = [rk.value for rk in self.rows]
        if keys == current:
            for k, cells in rows:
                for col, val in zip(self.columns, cells):
                    self.update_cell(k, col, val)
            return
        row = self.cursor_row
        self.clear()
        for k, cells in rows:
            self.add_row(*cells, key=k)
        if rows:
            self.move_cursor(row=min(row, len(rows) - 1))

    def selected_key(self) -> str | None:
        if not self.row_count:
            return None
        try:
            return self.coordinate_to_cell_key(Coordinate(self.cursor_row, 0)).row_key.value
        except Exception:
            return None


class JobForm(ModalScreen):
    """Create a saved job or run a one-off copy."""

    CSS = """
    JobForm { align: center middle; }
    #form { width: 90; height: auto; max-height: 95%; border: thick $accent; background: $surface; padding: 1 2; }
    #form Input, #form Select { margin-bottom: 1; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { margin-right: 2; }
    .lbl { color: $text-muted; }
    """

    def __init__(self, job: dict | None = None):
        super().__init__()
        self.job = job or {}

    def compose(self) -> ComposeResult:
        j = self.job
        f = j.get("filters", {})
        with VerticalScroll(id="form"):
            yield Label("[b]Edit job[/b]" if j else "[b]New copy / job[/b]")
            yield Label("Name (needed to save)", classes="lbl")
            yield Input(j.get("name", ""), id="name")
            yield Label("Source  (folder, \\\\server\\share, sftp://user@host/path, remote:path)", classes="lbl")
            yield Input(j.get("source", ""), id="source")
            yield Label("Destination", classes="lbl")
            yield Input(j.get("dest", ""), id="dest")
            yield Label("Mode", classes="lbl")
            yield Select([("Copy new & changed", "copy"), ("Mirror (make identical)", "mirror"),
                          ("Move", "move")], value=j.get("mode", "copy"), allow_blank=False, id="mode")
            yield Label("Deleted / overwritten destination files", classes="lbl")
            yield Select([("Keep old versions in .ferry-archive", "archive"), ("Ask me first", "confirm"),
                          ("Delete directly", "delete")], value=j.get("delete_mode", "archive"),
                         allow_blank=False, id="delete_mode")
            yield Label("Verify", classes="lbl")
            yield Select([("No", "none"), ("xxHash (fast)", "xxhash"), ("SHA-256", "sha256")],
                         value=j.get("verify", "none"), allow_blank=False, id="verify")
            yield Label("Exclude patterns (comma separated, e.g. *.tmp, node_modules/)", classes="lbl")
            yield Input(", ".join(f.get("exclude", []) + [d + "/" for d in f.get("exclude_dirs", [])]), id="exclude")
            yield Label("Bandwidth limit (e.g. 20M, empty = unlimited)", classes="lbl")
            yield Input(j.get("bwlimit") or "", id="bwlimit")
            yield Label("Only run during (e.g. 22:00-06:00, empty = always)", classes="lbl")
            yield Input(", ".join(j.get("windows", [])), id="windows")
            yield Label("Schedule (cron, e.g. 0 2 * * *  or @daily)", classes="lbl")
            yield Input(j.get("schedule") or "", id="schedule")
            yield Checkbox("Live sync (watch source for changes)", j.get("watch", False), id="watch")
            yield Checkbox("Dry run (only show what would happen)", False, id="dry_run")
            with Horizontal(id="buttons"):
                yield Button("Run now", variant="success", id="run")
                yield Button("Save job", variant="primary", id="save")
                yield Button("Cancel", id="cancel")
            yield Static("", id="msg")

    def _collect(self) -> dict:
        q = lambda i: self.query_one(f"#{i}")  # noqa: E731
        d = dict(self.job)
        for key in ("name", "source", "dest"):
            d[key] = q(key).value.strip()
        for key in ("mode", "delete_mode", "verify"):
            d[key] = q(key).value
        pats = [p.strip() for p in q("exclude").value.split(",") if p.strip()]
        flt = dict(d.get("filters") or {})
        flt["exclude"] = [p for p in pats if not p.endswith("/")]
        flt["exclude_dirs"] = [p.rstrip("/") for p in pats if p.endswith("/")]
        d["filters"] = flt
        d["bwlimit"] = q("bwlimit").value.strip() or None
        d["windows"] = [w.strip() for w in q("windows").value.split(",") if w.strip()]
        d["schedule"] = q("schedule").value.strip() or None
        d["watch"] = q("watch").value
        d["dry_run"] = q("dry_run").value
        if not d["name"]:
            d.pop("name")
        return d

    @on(Button.Pressed)
    def pressed(self, ev: Button.Pressed):
        if ev.button.id == "cancel":
            return self.dismiss(None)
        d = self._collect()
        if not d.get("source") or not d.get("dest"):
            self.query_one("#msg", Static).update("[red]Source and destination are required[/red]")
            return
        if ev.button.id == "save" and not d.get("name"):
            self.query_one("#msg", Static).update("[red]Give the job a name to save it[/red]")
            return
        self.dismiss((ev.button.id, d))


class TextView(ModalScreen):
    CSS = """
    TextView { align: center middle; }
    #box { width: 95%; height: 90%; border: thick $accent; background: $surface; }
    """
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Close")]

    def __init__(self, title: str, text: str):
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"[b]{self.title_text}[/b]  (Esc to close)")
            log = RichLog(highlight=False, markup=False, wrap=False)
            yield log

    def on_mount(self):
        log = self.query_one(RichLog)
        for line in self.text.splitlines()[-5000:]:
            log.write(line)


class FerryTUI(App):
    TITLE = "Ferry"
    CSS = """
    #details { height: auto; max-height: 14; padding: 0 1; border-top: solid $accent; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("n", "new", "New"),
        Binding("r", "run_job", "Run job"),
        Binding("p", "pause", "Pause"),
        Binding("u", "resume", "Resume"),
        Binding("c", "cancel", "Cancel"),
        Binding("y", "confirm('all')", "Confirm"),
        Binding("s", "confirm('skip')", "Skip deletes"),
        Binding("l", "log", "Log"),
        Binding("e", "edit", "Edit job"),
        Binding("delete", "delete_job", "Delete job"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.status = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs"):
            with TabPane("Activity", id="t-activity"):
                yield Table(id="runs", cursor_type="row")
                yield Static("", id="details")
            with TabPane("Jobs", id="t-jobs"):
                yield Table(id="jobs", cursor_type="row")
            with TabPane("History", id="t-history"):
                yield Table(id="history", cursor_type="row")
        yield Footer()

    def on_mount(self):
        runs = self.query_one("#runs", Table)
        for label, width in (("Job", 22), ("State", 15), ("Progress", 21), ("Files", 17), ("Data", 21),
                             ("Speed", 11), ("Elapsed", 11), ("Left", 11), ("Run", 21)):
            runs.add_column(label, width=width)
        self.query_one("#jobs", Table).add_columns("Name", "Mode", "Source", "Destination", "Schedule", "Live",
                                                    "Enabled")
        self.query_one("#history", Table).add_columns("When", "Job", "Result", "Files", "Data", "Time",
                                                       "Failed", "Run")
        self.refresh_data()
        self.set_interval(1.0, self.refresh_data)

    # ------------------------------------------------------------- data --
    def refresh_data(self):
        try:
            st = self.client.get("/status")
            runs = self.client.get("/runs")
            jobs = self.client.get("/jobs")
        except Exception as e:
            self.sub_title = f"service unreachable: {e}"
            return
        self.status = st
        self.sub_title = (f"{st['machine']} · {len(st['active'])} active · {st['jobs']} jobs · "
                          f"Telegram {'on' if st['telegram'] else 'off'}")
        runs = sorted(runs, key=lambda r: (r["phase"] in FINAL, r["id"]), reverse=False)
        rows = []
        for r in sorted(runs, key=lambda r: (r["phase"] in FINAL, -float(r.get("started") or 0))):
            rows.append((r["id"], (
                r["job_name"][:24], _phase(r),
                f"{progress_bar(r['percent'] / 100, 12)} {r['percent']:5.1f}%",
                f"{r['done_files']:,}/{r['total_files']:,}",
                f"{human_bytes(r['done_bytes'])}/{human_bytes(r['total_bytes'])}",
                f"{human_bytes(r['speed'])}/s" if r["phase"] not in FINAL else "",
                human_duration(r["elapsed"]),
                human_duration(r["eta"]) if r.get("eta") is not None and r["phase"] not in FINAL else "",
                r["id"])))
        self.query_one("#runs", Table).set_rows(rows)
        self._runs = {r["id"]: r for r in runs}
        self._update_details()
        self.query_one("#jobs", Table).set_rows([(j["id"], (
            j["name"], j["mode"], j["source"], j["dest"], j.get("schedule") or "", "yes" if j.get("watch") else "",
            "yes" if j.get("enabled", True) else "no")) for j in jobs])
        self._jobs = {j["id"]: j for j in jobs}
        if self.query_one("#tabs", TabbedContent).active == "t-history":
            hist = self.client.get("/history?limit=100")
            self.query_one("#history", Table).set_rows([(h["id"] + str(i), (
                datetime.fromtimestamp(h["started"]).strftime("%Y-%m-%d %H:%M") if h.get("started") else "",
                h["job_name"], PHASE.get(h["phase"], h["phase"]), f"{h['done_files']:,}",
                human_bytes(h["done_bytes"]), human_duration(h["elapsed"]), str(h.get("failed") or ""),
                h["id"])) for i, h in enumerate(hist)])

    def _update_details(self):
        rid = self.query_one("#runs", Table).selected_key()
        r = getattr(self, "_runs", {}).get(rid)
        if not r:
            self.query_one("#details", Static).update("No runs yet. Press [b]n[/b] to start a copy.")
            return
        lines = [f"[b]{r['job_name']}[/b] ({r['mode']}) {r['source']} → {r['dest']}"]
        pc = r.get("pending_confirmation")
        if pc:
            lines.append(f"[yellow b]Waiting for confirmation:[/] {pc['deletes']} deletions, {pc['overwrites']} "
                         f"overwrites — [b]y[/b] apply all, [b]s[/b] skip them, [b]c[/b] cancel")
            lines += [f"   {s}" for s in pc.get("sample", [])[:6]]
        lines.append(f"unchanged {r.get('same_files', 0):,} · failed {r.get('failed', 0):,} · "
                     f"deleted {r.get('deleted', 0):,} · {r.get('files_per_sec', 0):.1f} files/s"
                     + (f" · limit {human_bytes(r['bwlimit'])}/s" if r.get("bwlimit") else ""))
        for c in (r.get("current") or [])[:5]:
            pct = c["done"] * 100 // max(1, c["size"])
            lines.append(f"   ⇢ {c['path']}  {pct}% of {human_bytes(c['size'])}")
        for w in (r.get("warnings") or [])[:3]:
            lines.append(f"[yellow]⚠ {w['message']}[/]")
        if r.get("error"):
            lines.append(f"[red]{r['error']}[/]")
        self.query_one("#details", Static).update("\n".join(lines))

    @on(DataTable.RowHighlighted, "#runs")
    def _row(self, _):
        self._update_details()

    # ---------------------------------------------------------- actions --
    def _sel_run(self):
        return self.query_one("#runs", Table).selected_key()

    def _sel_job(self):
        return self.query_one("#jobs", Table).selected_key()

    def _do(self, fn):
        try:
            fn()
        except Exception as e:
            self.notify(str(e), severity="error")
        self.refresh_data()

    def action_pause(self):
        rid = self._sel_run()
        if rid:
            self._do(lambda: self.client.post(f"/runs/{rid}/pause"))

    def action_resume(self):
        rid = self._sel_run()
        if rid:
            self._do(lambda: self.client.post(f"/runs/{rid}/resume"))

    def action_cancel(self):
        rid = self._sel_run()
        if rid:
            self._do(lambda: self.client.post(f"/runs/{rid}/cancel"))

    def action_confirm(self, choice: str):
        rid = self._sel_run()
        if rid:
            self._do(lambda: self.client.post(f"/runs/{rid}/confirm", {"choice": choice}))

    def action_log(self):
        rid = self._sel_run()
        if not rid:
            return
        try:
            text = self.client.call("GET", f"/runs/{rid}/log?tail=5000", raw=True)
        except Exception as e:
            return self.notify(str(e), severity="error")
        self.push_screen(TextView(f"Log {rid}", text))

    def action_run_job(self):
        jid = self._sel_job()
        if self.query_one("#tabs", TabbedContent).active != "t-jobs" or not jid:
            self.query_one("#tabs", TabbedContent).active = "t-jobs"
            return self.notify("Select a job and press r")
        self._do(lambda: self.client.post(f"/jobs/{jid}/run"))
        self.query_one("#tabs", TabbedContent).active = "t-activity"

    def action_delete_job(self):
        jid = self._sel_job()
        if self.query_one("#tabs", TabbedContent).active == "t-jobs" and jid:
            self._do(lambda: self.client.delete(f"/jobs/{jid}"))
            self.notify("Job deleted")

    def action_new(self):
        self.push_screen(JobForm(), self._form_done)

    def action_edit(self):
        jid = self._sel_job()
        if self.query_one("#tabs", TabbedContent).active != "t-jobs" or not jid:
            return self.notify("Select a job on the Jobs tab first")
        self.push_screen(JobForm(self._jobs[jid]), self._form_done)

    def _form_done(self, result):
        if not result:
            return
        what, d = result
        if what == "save":
            d.pop("dry_run", None)
            self._do(lambda: self.client.post("/jobs", d))
            self.notify(f"Saved job {d['name']}")
        else:
            d = {k: v for k, v in d.items() if k not in ("id",)}
            self._do(lambda: self.client.post("/run", {"spec": d, "trigger": "tui"}))
            self.query_one("#tabs", TabbedContent).active = "t-activity"


def main() -> int:
    from ferry.daemon import ensure_daemon
    client = ensure_daemon()
    FerryTUI(client).run()
    return 0
