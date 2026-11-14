# Ferry — Requirements

A cross-platform file copy and sync tool, like robocopy but friendlier, with a Telegram bot for remote monitoring and control.

## Platforms & deployment
- Windows, Linux and macOS (x64 + arm64).
- **Portable**: a single executable with no installer and no admin rights required.
- **Background service without admin**:
  - Windows: per-user Task Scheduler task / `HKCU\...\Run` entry
  - Linux: `systemd --user` unit (with a fallback to running as a detached daemon)
  - macOS: a `~/Library/LaunchAgents` plist
- Config, state and logs live next to the binary (portable mode) or in the user's own config directory.

## Interfaces
- **CLI**: native subcommands (`ferry copy`, `ferry mirror`, `ferry job run …`, `ferry status`) **plus robocopy-compatible syntax** (`ferry robo SRC DST /MIR /E /COPYALL /R:3 /W:5 /XD … /XF …`).
- **Native desktop GUI** with a tray icon.
- **TUI** for servers and systems without a display (full-screen, live progress).
- All front-ends talk to the same background engine (daemon) through a local API.

## Job types
- One-off copy (large folders/drives)
- Mirror (destination becomes identical to the source)
- Scheduled jobs (cron-like)
- Live watch & sync (filesystem events, debounced)

## Storage locations
- Local disks / USB
- Network shares (SMB/NFS, UNC paths)
- Remote over SSH/SFTP
- Cloud storage (S3, Google Drive, OneDrive, …)

## Metadata preservation
- Timestamps and attributes (hidden, read-only, executable, …)
- Windows NTFS ACLs (DACL without admin; owner/SACL when elevated)
- Unix mode bits plus uid/gid (changing the owner needs root)
- Symlinks kept as links, extended attributes, macOS metadata
- Anything the current rights don't allow is skipped and **reported clearly** (never fails silently).

## Delete/overwrite safety (configurable per job)
- Delete directly (like robocopy /MIR)
- Archive old versions to a dated folder (`.ferry-archive/YYYY-MM-DD/…`)
- Dry-run / ask for confirmation (in the GUI, TUI or Telegram) before destructive changes

## Scale
- Millions of files: a persistent index (embedded DB), resumable jobs, fast re-sync
- Huge files (hundreds of GB): resume in the middle of a file, delta/block-level copy where possible
- Parallel copying, tuned separately for many small files and for a few large ones

## Telegram bot (one bot per machine; all bots can join one group)
- Status: progress %, files/bytes done/total, current speed, **elapsed time, ETA**
- Control: start / pause / resume / cancel jobs, run saved jobs, confirm pending deletes
- Push notifications: start, finish, failure, and progress every N% / N minutes
- Error details: list of failed files with reasons; download the log or report as a file
- Access limited to an allow-list of Telegram user/chat IDs
- Works behind NAT (long polling, no incoming ports)

## Extras
- Checksum verification (xxHash3 for speed, SHA-256 optional)
- Bandwidth limits and time windows (e.g. 20 MB/s 09:00–18:00, run only 22:00–06:00)
- Include/exclude filters, size/age filters, `.gitignore`-style rules, saved job presets
- Retries with backoff (like robocopy /R /W)

## Project
- Name: **Ferry**
- Private GitHub repo with CI that builds binaries for all platforms
- Delivered all at once (not in phases)
- **Language: Python** (3.10+). Packaged with PyInstaller into single-file executables per OS: `ferry` (CLI/TUI/daemon, small) and `ferry-gui` (adds Qt desktop UI). Cloud backends via an optional bundled `rclone` binary; SFTP natively via paramiko.
