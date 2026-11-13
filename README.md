# TermCal

A fast, keyboard-driven terminal calendar built on [caldir](https://caldir.org).

caldir owns the data (a local directory of `.ics` files synced to Google Calendar, iCloud, CalDAV, or Outlook). TermCal provides an instant terminal UI on top: read local state directly, edit and create events via a modal dialog, and push/pull sync in the background.

## Screen

```
 TermCal  Thu Oct 29, 2026                                2 invites  synced 10:02  auto-sync on
 1  ● Personal    2  ● work@example.com    3  ● Holidays

 Thursday, Oct 29  · today
    all day       ●   Dentist Appointment
    09:30-10:00   ● ✓ Team Standup ⧉
    ── now 10:12 ─────────────────────────────────────────────
  ▸ 10:30-11:00   ● • Design Review ⧉
    12:30-13:00   ● ✓ Lunch with Alex
 ───────────────────────────────────────────────────────────────
  Design Review
  work@example.com  · recurring  · 4 invited, 3 accepted
  ⧉  https://meet.google.com/...
  from Alex Rivera
 10:12:03 sync complete
 ←→ Day | [ ] Wk | T Today | W Week | C Month | I Invites | V Timeline | A Accounts | 1-9 Cals | Q Quit
 O Join | G Google | A Accept | D Decline | M Maybe | N New | E Edit | X Delete | S Sync | U Auto-sync
```

Every key is listed in the two-line footer—there is no hidden help screen. Glyphs: `●` calendar color, `✓` accepted, `✗` declined, `?` maybe, `•` needs response, `⧉` has a meeting link. Past and declined events are dimmed.

## Requirements

- **Python 3.11+**
- **[caldir](https://caldir.org) (v0.14+)** on `PATH` or in `~/.local/bin`
- At least one connected calendar in caldir

## Installation

### 1. Install caldir & connect your calendar

If you don't already have `caldir` installed:

```bash
curl -sSf https://caldir.org/install.sh | sh
```

Connect your provider and perform your first sync:

```bash
caldir connect google    # or icloud / caldav / outlook
caldir sync
```

### 2. Install TermCal

Using [Homebrew](https://brew.sh):

```bash
brew install deighne/tap/termcal
```

Or using [pipx](https://pypa.github.io/pipx/):

```bash
pipx install git+https://github.com/deighne/termcal.git
```

Or run directly from a local clone:

```bash
git clone https://github.com/deighne/termcal.git
cd termcal
./termcal
```

You can verify your installation and connected calendars at any time with:

```bash
termcal --check
```

## Keys

| Key | Action |
| --- | --- |
| `↑` `↓` / `j` `k` | Move between events (or previous / next week in month view) |
| `←` `→` / `h` `l` | Previous / next day |
| `[` `]` | Previous / next week (or previous / next month in month view) |
| `t` | Jump to today |
| `w` | Toggle day / week view |
| `v` | Toggle visual timeline in day and week views (proportional blocks, condensed free time, overlap conflicts) |
| `c` | Toggle full calendar month view (`Enter` opens the focused day) |
| `i` | Toggle pending invites view |
| `Enter` / `o` | Open meeting link (Zoom, Google Meet, Teams, Webex) |
| `g` | Open event in external calendar (Google Calendar web, Outlook web, or default desktop calendar) |
| `a` `d` `m` | RSVP accept / decline / maybe (`caldir rsvp`) |
| `n` | New event modal: title, when, duration, location, notes, calendar |
| `e` | Edit event modal: title, when, duration, location, notes (this event vs. entire series) |
| `x` | Delete event (prompts for confirmation) |
| `s` | Full sync now (push + pull across caldir's full range) |
| `u` | Toggle auto-sync (remembered between runs) |
| `r` | Reload from the local store |
| `1`-`9` | Toggle calendar visibility |
| `A` / `,` | Connected accounts & calendars modal (view providers, accounts, paths, set default, toggle visibility) |
| `q` | Quit |

## How it maps to caldir

| TermCal | caldir |
| --- | --- |
| Load visible range | `caldir events --from --to --json` (local read, ~0.3s) |
| Invite count / view | `caldir invites --json` |
| Calendar legend + colors | `caldir calendars --json` (hex mapped to xterm-256) |
| Startup / `s` | `caldir sync` (full range) |
| Auto-sync | `caldir sync --from today-14d --to today+2mo` |
| `n` (New event) | `caldir new TITLE -s WHEN -d DURATION [-l LOCATION] -C CAL` |
| `a` / `d` / `m` | `caldir rsvp PATH accept\|decline\|maybe` |
| `g` | Builds URL from `X-GOOGLE-EVENT-ID` & `google_calendar_id` |
| `e` / `x` | Updates `.ics` files / writes `RECURRENCE-ID` overrides |

"Me" for RSVP status is any calendar whose name is an email address. You can specify additional emails with `TERMCAL_EMAILS=user@example.com,other@example.com`.

## Auto-sync

Press `u` to toggle auto-sync; your preference is saved in `~/.config/termcal/state.json`.

While auto-sync is enabled, TermCal runs a full `caldir sync` at startup, and a windowed sync (2 weeks back through 2 months ahead) every 5 minutes and immediately after any RSVP, new event, edit, or delete. Pressing `s` runs a full sync on demand. Syncing runs in the background and never blocks navigation or key input.

## macOS Default Calendar & CLI Targets

TermCal can be opened with specific targets or registered as your system-wide default calendar on macOS.

### CLI Target Arguments

```bash
# Open focused on a specific date
termcal 2026-12-25
termcal tomorrow

# Open an external .ics invite (prompts to import into a connected calendar)
termcal ~/Downloads/invite.ics

# Open from a calshow: URI
termcal calshow:2026-11-15
```

### Setting as Default macOS Calendar

To install the lightweight `~/Applications/TermCal.app` runner and register with LaunchServices:

```bash
termcal --install-app
```

Then select TermCal as your default calendar:
1. Open **Calendar.app** $\rightarrow$ **Settings** (`Cmd-,`)
2. Set **Default calendar app** to **TermCal**

To remove the application bundle and unregister:

```bash
termcal --uninstall-app
```

## Development

```bash
make hooks        # Enable git pre-commit hook (runs linter, formatter check, and tests)
make lint         # Run ruff check, ruff format --check, and unit tests
make test         # Run unit tests verbosely
make fix          # Auto-fix linting and formatting
make install-dev  # Install in editable mode with pipx
```

## License

[MIT](LICENSE)
