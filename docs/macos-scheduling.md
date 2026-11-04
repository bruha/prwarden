# Scheduling on macOS

Use scheduling only after a targeted dry run and a controlled live run succeed.
The examples below run at 08:00, 12:00, and 16:00, Monday through Friday, in
the Mac's local time zone.

Replace every `/Users/your-name` path and verify executable locations first:

```sh
command -v python3
command -v gh
command -v agy
```

Use the executable for the configured review engine instead of `agy` when
another adapter is selected. Shell aliases are not available to either
scheduler, so every executable and project path must be explicit.

The examples below use `/opt/homebrew/bin/python3`. The stock
`/usr/bin/python3` also runs this tool, because the project keeps a Python 3.9
floor for exactly that reason
([why](code-conventions.md#why-python-39)), but Apple pins it to 3.9.6 and
Python 3.9 reached upstream end of life in October 2025. Use whichever absolute
path `command -v python3` reports for the interpreter you choose.

Which one to schedule depends on where the checkouts live. macOS asks before a
program reads `Documents`, `Desktop`, or `Downloads`, and it remembers the answer
for one exact binary. Homebrew replaces the interpreter on every Python upgrade,
patch releases included, so each upgrade asks again. An unattended run then waits
at that prompt until somebody answers it, and launchd starts no other run of the job
while it waits: an upgrade on a Saturday evening held the following Monday's 08:00
run until 08:33. Two ways to keep that from happening:

- Schedule `/usr/bin/python3`. It is signed by Apple, and macOS matches a signed
  program by its signing identity rather than by the file, so one approval should
  outlast system and Xcode updates. The cost is running on 3.9.6.
- Keep Homebrew's interpreter and stop it moving underneath the schedule with
  `brew pin python@3.15` (the formula you use), then answer the prompt by hand
  after each upgrade you choose to make.

Checkouts outside the protected folders avoid the question entirely.

An interpreter denied that access does not wait: it fails at once, and because it
cannot read the project it cannot report anything, so the only trace is `No module
named 'src'` in the job's log. Check System Settings > Privacy & Security > Files &
Folders for the interpreter, which `/usr/bin/python3` lists as `python3`, and enable
the folder there before switching the job to it.

## Recommendation

Prefer a per-user LaunchAgent for a Mac workstation. It has a declarative
configuration, an explicit environment and working directory, a dedicated log,
and `launchctl` commands for inspection. It runs only while that user's launchd
session is available.

Cron is acceptable for a simple existing cron setup, but its environment is
minimal and macOS privacy controls can make access to repositories under
`Documents`, `Desktop`, or other protected locations harder to diagnose. Do not
grant Full Disk Access to cron or Python merely to avoid configuring explicit
paths. A LaunchAgent is not a security sandbox and does not bypass macOS privacy
controls; test the exact scheduled command against every configured checkout.

Run the reviewer under `caffeinate` so the machine stays awake for the whole run.
A Mac that was asleep at the scheduled minute runs the job at its next wake, which
is often a DarkWake lasting under a minute, and nothing about a background job stops
the machine from going straight back to sleep underneath it. The review CLIs then
die mid-invocation and report an engine failure with no response. `-i` holds off
idle sleep for exactly as long as the run takes, and `-s` adds system sleep while on
AC power; neither turns the display on. Size the assertion window by the run, not by
the schedule: a run reviewing several pull requests at two levels can take half an
hour.

Both approaches invoke the live-delivery command. `.env` must still contain
`AUTO_REVIEW_DRY_RUN=0`, so the existing two live-delivery gates remain active.
The JSON state lock rejects a second reviewer process using the same state file,
so the two processes do not deliver concurrently. Schedule intervals should
allow for the configured per-invocation timeout, adversarial verification, and
possible validation-correction retries.

## Option 1: LaunchAgent

Create `~/Library/LaunchAgents/com.example.prwarden.plist` with the
following content. Substitute the exact Python, home, project, and log paths.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.prwarden</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>-s</string>
    <string>/opt/homebrew/bin/python3</string>
    <string>-m</string>
    <string>src.prwarden</string>
    <string>--live</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/your-name/Documents/projects/prwarden</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/Users/your-name</string>
    <key>PATH</key>
    <string>/Users/your-name/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONPATH</key>
    <string>/Users/your-name/Documents/projects/prwarden</string>
  </dict>

  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
  </array>

  <key>StandardOutPath</key>
  <string>/Users/your-name/Library/Logs/prwarden.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/your-name/Library/Logs/prwarden.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
```

Validate and load it:

```sh
PLIST=/Users/your-name/Library/LaunchAgents/com.example.prwarden.plist
LABEL=com.example.prwarden

plutil -lint "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl print "gui/$(id -u)/$LABEL"
```

Run one immediate live test through the loaded LaunchAgent and inspect its log:

```sh
launchctl kickstart -k "gui/$(id -u)/$LABEL"
tail -n 100 /Users/your-name/Library/Logs/prwarden.log
```

`launchctl print` shows `state = not running` between scheduled runs; that is
normal for a calendar-triggered job. Check `last exit code` and the log to
determine the last result.

After editing the plist, unload and load it again so launchd reads the changes:

```sh
launchctl bootout "gui/$(id -u)" "$PLIST"
plutil -lint "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
```

### Disable or remove the LaunchAgent

Unload it first. Renaming the plist keeps a recoverable copy that launchd will
not discover as an active `.plist` configuration:

```sh
PLIST=/Users/your-name/Library/LaunchAgents/com.example.prwarden.plist

launchctl bootout "gui/$(id -u)" "$PLIST"
mv "$PLIST" "$PLIST.disabled"
```

Verify that it is no longer loaded:

```sh
launchctl print "gui/$(id -u)/com.example.prwarden"
```

That command should report that the service cannot be found. Delete the
`.disabled` backup later only when it is no longer needed.

## Option 2: cron

The cron command must change to the project directory and define the minimal
environment needed by Python, GitHub CLI, and the selected authenticated review
engine. Add this as one physical line after replacing all paths:

```cron
0 8,12,16 * * 1-5 cd /Users/your-name/Documents/projects/prwarden && HOME=/Users/your-name PATH=/Users/your-name/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin /usr/bin/caffeinate -i -s /opt/homebrew/bin/python3 -m src.prwarden --live >> /Users/your-name/Library/Logs/prwarden.log 2>&1 # PRWARDEN
```

Install it with `crontab -e`, or without opening an editor:

```sh
CRON_LINE='0 8,12,16 * * 1-5 cd /Users/your-name/Documents/projects/prwarden && HOME=/Users/your-name PATH=/Users/your-name/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin /usr/bin/caffeinate -i -s /opt/homebrew/bin/python3 -m src.prwarden --live >> /Users/your-name/Library/Logs/prwarden.log 2>&1 # PRWARDEN'

(crontab -l 2>/dev/null | grep -v 'PRWARDEN'; printf '%s\n' "$CRON_LINE") | crontab -
```

The installation command removes an existing marked reviewer entry before
adding the current line. Verify the installed entry and inspect the same log
after a scheduled run:

```sh
crontab -l | grep PRWARDEN
tail -n 100 /Users/your-name/Library/Logs/prwarden.log
```

An absent log usually means the cron command has not run, cannot create the log,
or cannot access one of the parent directories. Run the exact command manually
with the same `HOME`, `PATH`, executable, and working directory before changing
permissions.

### Remove only the reviewer cron entry

Back up the complete crontab, remove only the marked reviewer line, and verify
the result:

```sh
crontab -l > /Users/your-name/Desktop/crontab-before-prwarden.txt
crontab -l | grep -v 'PRWARDEN' | crontab -
crontab -l
```

Do not use `crontab -r` unless every cron entry for the current user should be
deleted; it removes the entire crontab, not only this reviewer.

## Troubleshooting checklist

- Confirm `.env` has mode `0600` and both live-delivery gates are enabled.
- Use absolute paths; schedulers do not load interactive aliases or shell setup.
- Confirm `gh api user` and the selected engine's authentication command work
  with the scheduled `HOME` and `PATH`. If GitHub's current-user endpoints are
  degraded, the run falls back to `gh api graphql -f query='{viewer{login}}'`;
  set `GITHUB_REVIEWER_USERNAME` in `.env` when both are unavailable.
- Runs act as the reviewer account whichever GitHub CLI account is active, so
  `gh auth switch` for other work is safe. They need that account to stay logged
  in: `gh auth status` must list it, or startup stops with "GitHub CLI has no
  login for" the account.
- Confirm every repository path in `repositories.json` is accessible from the
  scheduled process. A run that started late after an interpreter upgrade was
  waiting for macOS to grant it `Documents`; a job log ending in `No module named
  'src'` is an interpreter that was refused.
- Inspect the dedicated log and the scheduler's last exit status.
- Check `.state/review-state.json.lock` only after confirming that no reviewer
  process is still running. Do not delete an active lock to force overlap.
