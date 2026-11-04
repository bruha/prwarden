# Scheduling

`prwarden schedule` runs PR Warden live on a timer: a per-user LaunchAgent on
macOS, a systemd user timer on Linux. It replaces the hand-written LaunchAgent and
cron entries in [macOS scheduling](macos-scheduling.md), which remains the
reference for what the agent does and why.

```sh
prwarden schedule install                      # Mon-Fri at 08:00, 12:00, 16:00
prwarden schedule install --at 09:30,15:00 --days mon,wed,fri
prwarden schedule install --print              # show it, change nothing
prwarden schedule status
prwarden schedule remove
```

Installing again replaces the schedule. Times are the machine's local time.

## Always live

A schedule runs `prwarden --live`, and `install` refuses until `.env` sets
`AUTO_REVIEW_DRY_RUN=0`. A dry run records nothing, so a scheduled one would review
every open pull request again at every slot, spending credits each time for a log
nobody reads. Preview one pull request first, then open the gate:

```sh
prwarden --repo your-org/example-app --pr 123   # dry run: nothing is posted
```

## What it installs

- **The command.** An installed copy runs the `prwarden` found on `PATH`, which
  Homebrew and pipx keep at one path across upgrades. A clone runs the interpreter
  that ran `install`, on the clone's code. A non-default `--env-file` is passed on.
- **The environment.** `HOME`, a `PATH` naming where GitHub CLI, Git, and every
  configured AI CLI were found, and `PRWARDEN_HOME` and the XDG directories when
  they are set, because a scheduler reads none of the shell's startup files.
- **Catching up.** A slot missed while the machine slept or was off runs once when
  it is back. On macOS the run is held awake with `caffeinate -i -s` for exactly as
  long as it takes.
- **Logs.** macOS writes `~/Library/Logs/prwarden.log`. Linux sends output to the
  journal: `journalctl --user -u prwarden.service`.

## One schedule per machine

On macOS `install` refuses while another LaunchAgent already runs PR Warden, such as
one written by hand from the macOS guide, because two schedules would review the
same pull requests. Remove the other one first:

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.example.prwarden.plist
rm ~/Library/LaunchAgents/com.example.prwarden.plist
```

`status` lists any such agent too. A clone and an installed copy keep separate
state, so schedule only one of them.

## Platform notes

- **macOS** asks before a program reads `Desktop`, `Documents`, or `Downloads`, and
  an unattended run waits at that question. `install` names every configured path
  inside them. Clones PR Warden manages itself live outside them, under
  `~/.local/share/prwarden`; see [macOS scheduling](macos-scheduling.md) for the
  choices when a clone must stay in `Documents`.
- **Linux** runs user timers only while you are logged in unless lingering is on,
  and `install` says so when it is off: `loginctl enable-linger "$USER"`.
