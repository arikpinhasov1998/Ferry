# Changelog

## 1.2.0 — 2026-11-14

### New
- **Ferry has an icon** — a little ferry boat — everywhere: the Windows `.exe` files (Explorer, taskbar, Start
  menu), the macOS app (Finder, Dock, Launchpad), the window title bar, the tray, dialogs and the Linux taskbar.
  A bolder version is used at small sizes (16–32 px) so it stays readable.
- **Start menu / app menu shortcut**: Settings → *Add to Start menu* (Windows) / *Add to app menu* (Linux), or
  `ferry shortcut install|uninstall|status`.
- **Progress bars are indeterminate until copying starts**: while starting and scanning (when the total is not
  known yet) the bar moves back and forth and shows how many files were found; once copying begins it shows the
  real percentage. Same in the desktop app, terminal app, command line and Telegram. Speed is hidden until
  copying starts, and a run waiting for your OK shows a still bar.
- Telegram setup: **Save bot picture…** to give your bot Ferry's icon via @BotFather's `/setuserpic`.
- README logo; `packaging/make_icons.py` renders every icon file from `ferry/assets/ferry.svg`, and
  `packaging/screenshots.py` regenerates the documentation screenshots.

### Fixed
- **"Match system" theme on Linux** showed light even when the desktop was dark: inside the portable build Qt's
  GTK plugin can't read the desktop settings. Ferry now asks the desktop's settings portal directly (GNOME, KDE…),
  with fallbacks, and also notices when you switch while Ferry is open.
- **Linux dock/taskbar showed a gear instead of Ferry's icon** (Wayland ignores a window's own icon). The desktop
  app now registers a hidden `ferry.desktop` with the icon when it starts, and keeps it up to date if Ferry is
  moved; "Add to app menu" makes it visible in the menu.

## 1.1.0 — 2026-11-14

### New
- **Guided Telegram setup in the desktop app** (Settings → Telegram bot → *Set up bot…*): paste the token, it is
  checked right away, then everyone (or a group) who presses Start in Telegram can be allowed with one click.
  Settings shows whether the bot is connected and why not (wrong token, no internet, token used by another
  computer), plus a *Disconnect* button.
- **Light, dark and "match system" themes** for the desktop app (Settings → Appearance) and the terminal app
  (press `t`). "Match system" follows the computer's setting live.
- **A dry run now lists the files** that would be copied, updated or deleted, not just the counts.
- `ferry robo --help` shows the supported robocopy options.
- New **[User Guide](docs/GUIDE.md)** and a simpler README with screenshots.

### Fixed
- Relative paths (`ferry copy Photos Backup`, or a saved job using `Photos`) were resolved in the background
  service's folder instead of yours. They are now turned into full paths everywhere.
- Windows administrators can now copy files they normally can't read (backup privilege, like robocopy `/B`),
  as documented. Before, those files failed.
- The background service took more than 15 seconds to start on some Macs (slow network name lookup).
- `ferry job run` accepts `-q`; output that isn't a terminal no longer starts with a stale progress line.

### Behind the scenes
- New Windows tests for NTFS permissions, file attributes and unreadable files.
- CI uses current GitHub Actions (Node 24) and the modern license metadata.

## 1.0.0 — 2026-11-14

First release: copy, mirror and move with resume, archive/confirm safety, filters, checksum verification,
bandwidth limits and time windows, schedules and live sync, SFTP and rclone clouds, NTFS ACLs and POSIX
metadata, background service without admin rights, Telegram bot, desktop app, terminal app, robocopy-compatible
command line, and portable mode.
