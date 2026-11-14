# Ferry

Comfortable, cross-platform file **copy & sync**, like robocopy but easier to use, with:

- **CLI** (its own commands plus **robocopy-compatible** arguments), a **terminal UI** for servers, and a **desktop app** with a tray icon
- a **background service** that runs per user, **without admin rights**, on Windows, Linux and macOS
- a **Telegram bot** that shows status, elapsed time and time left, and lets you control and confirm jobs and receive notifications and logs
- **portable** single-file executables (keep settings next to the binary on a USB stick)

| | |
|---|---|
| Locations | local disks/USB, network shares (`\\server\share`, mounted SMB/NFS), **SFTP** (`sftp://user@host/path`), **cloud** via rclone (`gdrive:Backup`, `s3:bucket/x`, OneDrive, Dropbox… 70+) |
| Jobs | copy, mirror, move · one-off, **scheduled** (cron), **live sync** (watch folder) |
| Safety | per job: **archive** replaced/deleted files to `.ferry-archive/<date>/`, **ask first** (confirm in GUI/TUI/CLI/Telegram), or delete directly · dry-run |
| Metadata | timestamps, attributes/mode bits, **NTFS ACLs**, owner/group (when elevated), symlinks, xattrs; whatever your rights don't allow is skipped *and reported* |
| Scale | on-disk plan (SQLite) → flat memory for millions of files; **resumes** after a crash/reboot; resumes inside large files; **delta** in-place update of huge files |
| Extras | checksum verify (xxHash/SHA-256), bandwidth limits incl. by time of day, run-only time windows, include/exclude/.ferryignore, size/age filters, retries |

## Install

Download the zip for your OS from the GitHub Actions artifacts / Releases and unzip it anywhere:

- `ferry` / `ferry.exe` – CLI, `ferry tui`, background service (~25 MB)
- `ferry-gui` / `ferry-gui.exe` / `Ferry-gui.app` – desktop window (includes Qt)

**Portable mode:** create an empty `ferry-data` folder next to the executable; all settings, jobs and logs go there.

From source (Python ≥ 3.10):

```bash
python -m venv .venv && .venv/bin/pip install -e ".[gui,dev]"
.venv/bin/ferry --help
```

## Quick start

```bash
ferry copy ~/Photos /mnt/backup/Photos --verify
ferry mirror D:\Work \\nas\share\Work --delete-mode confirm
ferry copy ./data sftp://me@server/backups/data --bwlimit 10M --window 22:00-06:00
ferry mirror ~/Docs gdrive:Backup/Docs                     # needs rclone + "rclone config"
ferry robo C:\src D:\dst /MIR /R:2 /W:5 /XD node_modules /MT:16 /LOG:copy.log
ferry C:\src D:\dst /MIR                                   # robocopy style without "robo"
```

Runs go to the background service (auto-started) so the GUI, TUI and Telegram see them; the terminal shows live
progress. Ctrl+C asks whether to cancel or keep it running. `--bg` returns immediately, `--local` runs in-process only.

### Saved jobs, schedules and live sync

```bash
ferry job add nightly ~/Work /backup/Work --mode mirror --schedule "0 2 * * *" --verify
ferry job add live ~/Notes /mnt/nas/Notes --mode mirror --watch
ferry job add offsite ~/Data sftp://me@vps/data --bw-schedule "mon-fri 09:00-18:00=2M" --window "22:00-07:00"
ferry job list | ferry job run nightly | ferry job edit nightly --threads 16 | ferry job remove live
```

### Control

```bash
ferry status            # service + active runs        ferry status <run> -f   # follow one run
ferry pause all         ferry resume <run>             ferry cancel <run>
ferry confirm <run>     ferry confirm <run> --skip     # approve or skip pending deletions
ferry errors <run>      ferry log <run> -n 100         ferry history
ferry tui               ferry gui
```

### Background service (no admin)

```bash
ferry service install     # Windows: HKCU Run key · macOS: LaunchAgent · Linux: systemd --user (crontab fallback)
ferry service status | ferry service uninstall
ferry daemon start|stop|restart|status
```

Interrupted runs (logout, reboot, crash) resume automatically when the service starts again (`auto_resume`).

## Telegram bot

1. In Telegram, open **@BotFather** → `/newbot` → copy the token. Use **one bot per machine**.
2. `ferry telegram setup` → paste the token → send `/start` to your bot → confirm. (Or use Settings in the GUI.)
3. `ferry service install` so the bot is always on.

Commands: `/status` (progress bar, files, bytes, speed, **elapsed**, **time left + finish time**), `/jobs` (tap to run),
`/run <job> [dry]`, `/pause`, `/resume`, `/cancel`, `/confirm` (Apply all / Skip deletes / Cancel buttons),
`/errors`, `/log` (file), `/report` (CSV), `/history`, `/disk`, `/machine`, `/notify on|off`, `/every 10%|30m|off`, `/whoami`.

Notifications: job started / finished / failed, confirmation needed (with buttons), progress every N % or N minutes.

**Several machines:** add all machines' bots to one Telegram group, allow the group's chat ID
(`ferry telegram allow -100…`) and use `/status` (every machine answers) or `/status @nas` (one machine).
Only allowed user/chat IDs can use a bot.

## Filters

Gitignore-style patterns: `*.tmp` (any depth), `build/` (folders only), `/top.log` (anchored), `**/cache/**`.
A `.ferryignore` file in any source folder applies to that folder (supports `!re-include`).
Mirror never deletes excluded files in the destination, or `.ferry-archive`.

## Permissions without admin

| | without admin/root | with admin/root |
|---|---|---|
| file data, timestamps, attributes, mode bits | ✅ | ✅ |
| NTFS permissions (DACL) | ✅ (on files you own) | ✅ |
| owner/group, audit (SACL) | skipped + reported | ✅ (`--owner`) |
| symlinks on Windows | needs Developer Mode | ✅ |
| files you can't read | listed as failed | ✅ (backup privilege) |

## Robocopy compatibility

Supported: `/S /E /LEV:n /MIR /PURGE /MOV /MOVE /COPY:DATSOU /COPYALL /SEC /DCOPY /SL /SJ /XJ /XF /XD /IF /XO /XN /XC /IS /IT
/XX /XL /MAX /MIN /MAXAGE /MINAGE /MAXLAD /MINLAD /R /W /MT /L /LOG /LOG+ /UNILOG /TEE /IPG /RH /FFT /MON`;
display options (`/NP /NFL /NDL /NJH /NJS /V /ETA /BYTES …`) are accepted and ignored. Exit codes follow robocopy
(0–7 success, 8+ failures). Like robocopy, no `/S`/`/E` means only the top folder.

## Development

```bash
.venv/bin/python -m pytest -q          # tests (incl. an in-process SFTP server)
.venv/bin/python packaging/build.py    # build dist/ferry + dist/ferry-gui for this OS
```

CI (`.github/workflows/build.yml`) tests on Windows/Linux/macOS and builds zips for Windows x64, Linux x64/arm64 and
macOS arm64/x64; pushing a `v*` tag creates a GitHub release.

Code map: `engine.py` (plan + transfer), `backends/` (local, SFTP, rclone), `metadata.py` (ACLs, xattrs…),
`jobs.py` (jobs, runs, history, resume), `daemon.py` + `api.py` (service, local token-protected API),
`telegram.py`, `scheduler.py`, `watcher.py`, `cli.py` + `robocopy.py`, `tui.py`, `gui.py`, `service.py`.
