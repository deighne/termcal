#!/usr/bin/env python3
"""TermCal: curses TUI calendar built on the caldir CLI.

caldir owns the data (a local .ics store synced to Google Calendar); TermCal is
a fast keyboard front end: read local state instantly, shell out to caldir for
every write, and sync in the background.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
import curses
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

__version__ = "0.1.0"
MIN_CALDIR_VERSION = (0, 14, 0)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CMD_TIMEOUT = 30  # seconds for local caldir reads
SYNC_TIMEOUT = "60s"  # passed to caldir --timeout for network calls
AUTO_SYNC_MINUTES = 5  # sync cadence while auto-sync is on (toggle with U)
AUTO_SYNC_DAYS_BACK = 14  # auto-syncs cover today - 14 days ...
AUTO_SYNC_MONTHS_AHEAD = 2  # ... through today + 2 months; startup and S sync everything
TICK_SECONDS = 30  # redraw cadence for the "now" marker
DETAIL_HEIGHT = 8
CALDIR_CONFIG = Path.home() / ".config" / "caldir" / "config.toml"
STATE_FILE = Path.home() / ".config" / "termcal" / "state.json"

MEETING_URL_RE = re.compile(
    r"https://[^\s<>\"']*(zoom\.us|meet\.google\.com|teams\.microsoft\.com|webex\.com)[^\s<>\"']*",
    re.IGNORECASE,
)
RSVP_GLYPH = {"accepted": "✓", "declined": "✗", "tentative": "?", "needs_action": "•"}


# ---------------------------------------------------------------------------
# caldir CLI interface
# ---------------------------------------------------------------------------


def find_caldir() -> str | None:
    found = shutil.which("caldir")
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin" / "caldir"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    return None


def parse_caldir_version(output: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", output)
    if m:
        return tuple(int(x) for x in m.groups() if x is not None)
    return None


def check_caldir() -> tuple[bool, str]:
    bin_path = find_caldir()
    if not bin_path:
        return (
            False,
            "caldir not found on PATH or in ~/.local/bin.\n"
            "Install it first: curl -sSf https://caldir.org/install.sh | sh",
        )
    try:
        ver_proc = subprocess.run([bin_path, "--version"], capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, f"Failed to execute caldir: {exc}"
    if ver_proc.returncode != 0:
        return False, f"caldir --version failed: {(ver_proc.stderr or ver_proc.stdout).strip()}"
    ver = parse_caldir_version(ver_proc.stdout)
    if not ver or ver < MIN_CALDIR_VERSION:
        min_v = ".".join(map(str, MIN_CALDIR_VERSION))
        cur_v = ".".join(map(str, ver)) if ver else ver_proc.stdout.strip()
        return False, f"caldir version {cur_v} is older than required {min_v}.\nUpdate with: caldir update"

    try:
        cal_proc = subprocess.run(
            [bin_path, "calendars", "--json"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except OSError as exc:
        return False, f"Failed to list calendars: {exc}"
    if cal_proc.returncode != 0:
        return False, f"caldir calendars failed: {(cal_proc.stderr or cal_proc.stdout).strip()}"
    try:
        cals = json.loads(cal_proc.stdout or "[]")
    except json.JSONDecodeError:
        cals = []
    if not cals:
        return (
            False,
            "No calendars found in caldir.\n"
            "Connect one first (e.g. 'caldir connect google'), then run 'caldir sync'.",
        )
    return True, f"caldir {'.'.join(map(str, ver))} OK with {len(cals)} calendar(s)."


def caldir(*args: str, timeout: int = CMD_TIMEOUT) -> subprocess.CompletedProcess:
    """Run caldir with NO_COLOR so captured output is clean text."""
    bin_path = find_caldir() or "caldir"
    env = {**os.environ, "NO_COLOR": "1"}
    return subprocess.run(
        [bin_path, *args], check=False, capture_output=True, text=True, timeout=timeout, env=env
    )


def caldir_json(*args: str):
    """Run a caldir read command with --json. Raises RuntimeError on failure."""
    result = caldir(*args, "--json")
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"caldir {args[0]} failed")
    return json.loads(result.stdout or "[]")


def read_config() -> dict:
    """Minimal parse of caldir's flat config.toml (key = "value" lines only)."""
    config: dict[str, str] = {}
    try:
        for line in CALDIR_CONFIG.read_text().splitlines():
            m = re.match(r'^\s*(\w+)\s*=\s*"?([^"]*)"?\s*$', line)
            if m:
                config[m.group(1)] = m.group(2)
    except OSError:
        pass
    return config


def build_path_index(calendar_dir: Path) -> dict[str, list[tuple[Path, str | None]]]:
    """Map UID -> [(ics path, RECURRENCE-ID or None)].

    `caldir events --json` doesn't include file paths, but rsvp/edit/delete
    need them. Scanning ~1.6k small files takes well under a second.
    """
    index: dict[str, list[tuple[Path, str | None]]] = {}
    for path in calendar_dir.glob("*/*.ics"):
        uid = rid = None
        try:
            # Unfold RFC 5545 continuation lines; long Outlook UIDs span several.
            text = re.sub(r"\r?\n[ \t]", "", path.read_text(errors="replace"))
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("UID:"):
                uid = line[4:].strip()
            elif line.startswith("RECURRENCE-ID"):
                rid = line.split(":", 1)[1].strip()
            elif line.startswith("END:VEVENT"):
                break
        if uid:
            index.setdefault(uid, []).append((path, rid))
    return index


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def parse_when(value: str) -> datetime:
    """caldir emits YYYY-MM-DD for all-day and ISO 8601 with offset otherwise."""
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    return datetime.fromisoformat(value).astimezone().replace(tzinfo=None)


def parse_user_when(text: str, default_day: date) -> tuple[date, datetime | None]:
    """Parse user date/time input into (day, datetime_or_None_for_all_day)."""
    s = text.strip()
    if not s:
        raise ValueError("Start date/time is required")
    today = date.today()
    lower = s.lower()
    day: date | None = None
    rest = s

    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+|T|$)(.*)$", s)
    if m:
        day, rest = date.fromisoformat(m.group(1)), m.group(2).strip()
    elif lower.startswith("today") and (len(lower) == 5 or lower[5].isspace()):
        day, rest = today, s[5:].strip()
    elif lower.startswith("tomorrow") and (len(lower) == 8 or lower[8].isspace()):
        day, rest = today + timedelta(days=1), s[8:].strip()
    else:
        weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        w_m = re.match(
            r"^(?:(next)\s+)?"
            r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|"
            r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b(.*)$",
            lower,
        )
        if w_m:
            target_wd = weekdays.index(w_m.group(2)[:3])
            days_ahead = (target_wd - today.weekday()) % 7
            if days_ahead == 0 or w_m.group(1):
                days_ahead += 7
            day, rest = today + timedelta(days=days_ahead), w_m.group(3).strip()

    if day is None:
        day = default_day

    if not rest or rest.lower() in ("all day", "all-day", "allday"):
        return day, None

    t_str = rest.lower().strip()
    if t_str == "noon":
        return day, datetime.combine(day, datetime.min.time()).replace(hour=12)
    if t_str == "midnight":
        return day, datetime.combine(day, datetime.min.time())

    tm = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", t_str)
    if not tm:
        raise ValueError(f"Could not parse time '{rest}' (try e.g. '2026-10-31 14:00' or 'today 3pm')")
    hour = int(tm.group(1))
    minute = int(tm.group(2) or 0)
    ampm = tm.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time '{rest}'")
    return day, datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def parse_duration(text: str, all_day: bool) -> timedelta:
    """Parse duration string like '30m', '1h30m', '1.5h', '2d', or '45'."""
    s = text.strip().lower()
    if not s or s in ("all day", "all-day", "allday"):
        return timedelta(days=1) if all_day else timedelta(minutes=30)
    if s.isdigit():
        n = int(s)
        return timedelta(days=max(1, n)) if all_day else timedelta(minutes=max(1, n))
    total_mins = 0.0
    matched = False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?)", s):
        matched = True
        val = float(m.group(1))
        unit = m.group(2)[0]
        if unit == "d":
            total_mins += val * 1440
        elif unit == "h":
            total_mins += val * 60
        elif unit == "m":
            total_mins += val
    if not matched or total_mins <= 0:
        raise ValueError(f"Could not parse duration '{text}' (try e.g. '30m', '1h', '1h30m')")
    if all_day:
        return timedelta(days=max(1, round(total_mins / 1440)))
    return timedelta(minutes=round(total_mins))


def fmt_duration(start: str, end: str, all_day: bool) -> str:
    if all_day:
        days = max(1, (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days)
        return f"{days}d"
    mins = max(1, round((parse_when(end) - parse_when(start)).total_seconds() / 60))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h{m}m"
    return f"{h}h" if h else f"{m}m"


def ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def update_ics_event(
    path: Path,
    event: dict,
    *,
    title: str,
    day: date,
    start_dt: datetime | None,
    duration: timedelta,
    location: str,
    description: str,
    scope: str,
) -> Path:
    """Update an existing .ics file or write a recurring instance override."""
    raw = path.read_text(errors="replace")
    crlf = "\r\n" if "\r\n" in raw else "\n"
    unfolded = re.sub(r"\r?\n[ \t]", "", raw)
    lines = unfolded.splitlines()

    tzid = event.get("tzid")
    if not tzid:
        for line in lines:
            m = re.match(r"^DTSTART;[^:]*TZID=([^;:]+)", line)
            if m:
                tzid = m.group(1)
                break

    if start_dt is None:
        end_day = day + max(timedelta(days=1), duration)
        dtstart_line = f"DTSTART;VALUE=DATE:{day:%Y%m%d}"
        dtend_line = f"DTEND;VALUE=DATE:{end_day:%Y%m%d}"
    else:
        end_dt = start_dt + duration
        if tzid:
            dtstart_line = f"DTSTART;TZID={tzid}:{start_dt:%Y%m%dT%H%M%S}"
            dtend_line = f"DTEND;TZID={tzid}:{end_dt:%Y%m%dT%H%M%S}"
        else:
            dtstart_line = f"DTSTART:{start_dt:%Y%m%dT%H%M%S}"
            dtend_line = f"DTEND:{end_dt:%Y%m%dT%H%M%S}"

    now_stamp = f"DTSTAMP:{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    rid = event.get("recurrence_id")
    is_master = path.name.startswith("_recurring__")

    if scope == "instance" and rid and is_master:
        # Build a standalone override .ics for this single occurrence
        header_lines: list[str] = []
        vevent_lines: list[str] = []
        in_vevent = False
        for line in lines:
            if line == "BEGIN:VEVENT":
                in_vevent = True
                continue
            if line == "END:VEVENT":
                break
            if not in_vevent:
                if line != "END:VCALENDAR":
                    header_lines.append(line)
            else:
                prop = line.split(":", 1)[0].split(";", 1)[0].upper()
                if prop in (
                    "RRULE",
                    "EXDATE",
                    "RDATE",
                    "RECURRENCE-ID",
                    "DTSTART",
                    "DTEND",
                    "DURATION",
                    "SUMMARY",
                    "LOCATION",
                    "DESCRIPTION",
                    "DTSTAMP",
                ):
                    continue
                vevent_lines.append(line)

        rid_line = f"RECURRENCE-ID;{rid}" if "=" in rid else f"RECURRENCE-ID:{rid}"
        vevent_lines.extend([now_stamp, rid_line, dtstart_line, dtend_line, f"SUMMARY:{ics_escape(title)}"])
        if location:
            vevent_lines.append(f"LOCATION:{ics_escape(location)}")
        if description:
            vevent_lines.append(f"DESCRIPTION:{ics_escape(description)}")

        out_lines = [*header_lines, "BEGIN:VEVENT", *vevent_lines, "END:VEVENT", "END:VCALENDAR"]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "event"
        prefix = f"{day:%Y-%m-%d}" if start_dt is None else f"{start_dt:%Y-%m-%dT%H%M}"
        override_path = path.parent / f"{prefix}__{slug}.ics"
        n = 2
        while override_path.exists() and override_path != path:
            override_path = path.parent / f"{prefix}__{slug}-{n}.ics"
            n += 1
        override_path.write_text(crlf.join(out_lines) + crlf)
        return override_path

    # In-place update of non-recurring event, existing override, or recurring series master
    if scope == "series" and is_master and start_dt is not None:
        # Preserve the master's original start date so the series anchor doesn't jump
        for line in lines:
            if line.startswith("DTSTART"):
                val = line.split(":", 1)[1].strip()
                if len(val) >= 8 and val[:8].isdigit():
                    orig_date = date(int(val[:4]), int(val[4:6]), int(val[6:8]))
                    adj_start = datetime.combine(orig_date, start_dt.time())
                    adj_end = adj_start + duration
                    if tzid:
                        dtstart_line = f"DTSTART;TZID={tzid}:{adj_start:%Y%m%dT%H%M%S}"
                        dtend_line = f"DTEND;TZID={tzid}:{adj_end:%Y%m%dT%H%M%S}"
                    else:
                        dtstart_line = f"DTSTART:{adj_start:%Y%m%dT%H%M%S}"
                        dtend_line = f"DTEND:{adj_end:%Y%m%dT%H%M%S}"
                break

    new_lines: list[str] = []
    in_vevent = False
    for line in lines:
        if line == "BEGIN:VEVENT":
            in_vevent = True
            new_lines.append(line)
            continue
        if line == "END:VEVENT" and in_vevent:
            in_vevent = False
            new_lines.extend([now_stamp, dtstart_line, dtend_line, f"SUMMARY:{ics_escape(title)}"])
            if location:
                new_lines.append(f"LOCATION:{ics_escape(location)}")
            if description:
                new_lines.append(f"DESCRIPTION:{ics_escape(description)}")
            new_lines.append(line)
            continue
        if in_vevent:
            prop = line.split(":", 1)[0].split(";", 1)[0].upper()
            if prop in ("DTSTART", "DTEND", "DURATION", "SUMMARY", "LOCATION", "DESCRIPTION", "DTSTAMP"):
                continue
        new_lines.append(line)

    path.write_text(crlf.join(new_lines) + crlf)
    return path


def event_day(event: dict) -> date:
    return parse_when(event["start"]).date()


def meeting_url(event: dict) -> str | None:
    if event.get("url"):
        return event["url"]
    for field in ("location", "description"):
        m = MEETING_URL_RE.search(event.get(field) or "")
        if m:
            return m.group(0)
    return None


def my_status(event: dict, my_emails: set[str]) -> str | None:
    if event.get("rsvp"):
        return event["rsvp"]
    for attendee in event.get("attendees") or []:
        if (attendee.get("email") or "").lower() in my_emails:
            return attendee.get("status")
    return None


def google_remote(calendar_dir: Path, slug: str) -> dict[str, str]:
    """google_calendar_id / google_account from a calendar's .caldir/config.toml."""
    remote: dict[str, str] = {}
    try:
        text = (calendar_dir / slug / ".caldir" / "config.toml").read_text()
    except OSError:
        return remote
    for key in ("provider", "google_calendar_id", "google_account"):
        m = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE)
        if m:
            remote[key] = m.group(1)
    return remote


def google_event_url(event: dict, remote: dict[str, str]) -> str | None:
    """Build the calendar.google.com link for one event instance.

    eid is base64url("<event id> <calendar id>"). Recurring instances need an
    _<start> suffix unless X-GOOGLE-EVENT-ID already carries one.
    """
    cal_id = remote.get("google_calendar_id")
    event_id = next(
        (x["value"] for x in event.get("x_properties") or [] if x["name"] == "X-GOOGLE-EVENT-ID"), None
    )
    if not cal_id or not event_id:
        return None
    if event.get("recurring") and not re.search(r"_\d{8}(T\d{6}Z)?$", event_id):
        if event["all_day"]:
            event_id += "_" + event["start"].replace("-", "")
        else:
            start = datetime.fromisoformat(event["start"]).astimezone(UTC)
            event_id += start.strftime("_%Y%m%dT%H%M%SZ")
    eid = base64.urlsafe_b64encode(f"{event_id} {cal_id}".encode()).decode().rstrip("=")
    url = f"https://calendar.google.com/calendar/event?eid={eid}"
    if remote.get("google_account"):
        url += f"&authuser={remote['google_account']}"
    return url


def fmt_time(dt: datetime, hour24: bool) -> str:
    return dt.strftime("%H:%M") if hour24 else dt.strftime("%-I:%M%p").lower()


def hex_to_256(hex_color: str) -> int:
    """Nearest xterm-256 color-cube index for a #rrggbb string."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return -1
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))

    def level(v: int) -> int:
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40

    return 16 + 36 * level(r) + 6 * level(g) + level(b)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_prefs() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({**load_prefs(), **prefs}, indent=2))
    except OSError:
        pass


class AppState:
    def __init__(self):
        self.config = read_config()
        self.calendar_dir = Path(self.config.get("calendar_dir", "~/caldir")).expanduser()
        self.hour24 = self.config.get("time_format", "24h") == "24h"
        self.default_calendar = self.config.get("default_calendar", "")
        self.calendars: list[dict] = []
        self.hidden: set[str] = set()
        self.my_emails: set[str] = set()
        self.events: list[dict] = []
        self.invites: list[dict] = []
        self.path_index: dict[str, list[tuple[Path, str | None]]] = {}
        self.focus_day = date.today()
        self.view = "day"  # day | week | month | invites
        self.day_timeline = load_prefs().get("day_timeline", self.config.get("day_timeline", False))
        self.cursor = 0
        self.rows: list[dict] = []
        self.log: list[str] = []
        self.busy: str | None = None  # label of the job the worker is running
        self.jobs: queue.Queue = queue.Queue()
        self.syncs_pending: set[str] = set()  # "full" / "window" syncs queued or running
        self.last_sync: datetime | None = None
        self.auto_sync = load_prefs().get("auto_sync", True)
        self.next_sync = 0.0  # time.monotonic() deadline for auto-sync
        self.prompt: dict | None = None  # active input line, see start_prompt()
        self.modal: dict | None = None  # active modal dialog for new / edit event
        self.lock = threading.Lock()
        self.dirty = True

    # -- window ------------------------------------------------------------

    def window(self) -> tuple[date, date]:
        if self.view == "week":
            start = self.focus_day - timedelta(days=self.focus_day.weekday())
            return start, start + timedelta(days=6)
        return self.focus_day, self.focus_day

    def selected(self) -> dict | None:
        if (
            self.view != "month"
            and 0 <= self.cursor < len(self.rows)
            and self.rows[self.cursor]["kind"] == "event"
        ):
            return self.rows[self.cursor]["event"]
        return None


def log_append(state: AppState, message: str) -> None:
    with state.lock:
        state.log.append(f"{datetime.now():%H:%M:%S} {message}")
        state.log = state.log[-50:]
        state.dirty = True


def run_bg(state: AppState, label: str, fn) -> None:
    """Queue a caldir job (sync, rsvp, new, edit, delete).

    One worker runs jobs in order so caldir never writes to the store from two
    processes at once. Navigation doesn't go through here; see load_async().
    """
    if state.busy:
        log_append(state, f"queued {label} (after {state.busy})")
    state.jobs.put((label, fn))


def job_worker(state: AppState) -> None:
    while True:
        label, fn = state.jobs.get()
        state.busy = label
        state.dirty = True
        try:
            fn()
        except Exception as exc:  # surface everything in the log line
            log_append(state, f"{label} failed: {exc}")
        finally:
            state.busy = None
            state.dirty = True


def queue_sync(state: AppState, full: bool = False) -> None:
    """Queue one sync; requests already covered by a pending sync collapse into it.

    full=True syncs caldir's whole default range (about a year back, ~7s on the
    work calendar). Otherwise only the auto-sync window, which takes ~1-2s.
    """
    kind = "full" if full else "window"
    if "full" in state.syncs_pending or kind in state.syncs_pending:
        return
    state.syncs_pending.add(kind)

    def work():
        try:
            do_sync(state, full)
        finally:
            state.syncs_pending.discard(kind)

    run_bg(state, "full sync" if full else "sync", work)


def load_async(state: AppState) -> None:
    """Reload the visible range on its own thread so it never waits on a sync."""

    def work():
        try:
            reload_local(state)
        except Exception as exc:
            log_append(state, f"load failed: {exc}")

    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_calendars(state: AppState) -> None:
    state.calendars = caldir_json("calendars")
    state.my_emails = {c["name"].lower() for c in state.calendars if "@" in c["name"]}
    extra = os.environ.get("TERMCAL_EMAILS", "")
    state.my_emails |= {e.strip().lower() for e in extra.split(",") if e.strip()}


def reload_local(state: AppState, reindex: bool = False) -> None:
    """Re-read the local store for the visible window. Fast; no network.

    The UID->path index only changes when files do, so it's rebuilt after
    syncs and writes (reindex=True), not on every day change. If the user
    navigates while caldir is answering, the result is stale and the read is
    repeated for the new window.
    """
    index = build_path_index(state.calendar_dir) if reindex or not state.path_index else state.path_index
    while True:
        snapshot = (state.window(), state.view)
        start, end = snapshot[0]
        events = caldir_json("events", "--from", start.isoformat(), "--to", end.isoformat())
        invites = caldir_json("invites")
        with state.lock:
            if snapshot != (state.window(), state.view):
                continue
            state.events = [e for e in events if e.get("status") != "cancelled"]
            state.invites = invites
            state.path_index = index
            rebuild_rows(state)
            return


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for last in (31, 30, 29, 28):  # clamp e.g. Dec 31 + 2 months to Feb 28/29
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)


def sync_window() -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=AUTO_SYNC_DAYS_BACK), add_months(today, AUTO_SYNC_MONTHS_AHEAD)


def do_sync(state: AppState, full: bool = False) -> None:
    """caldir sync (push + pull). Any sync, manual or automatic, resets the timer."""
    args = ["sync", "--timeout", SYNC_TIMEOUT]
    if full:
        label = "full sync"
    else:
        start, end = sync_window()
        args += ["--from", start.isoformat(), "--to", end.isoformat()]
        label = f"sync {start:%b %-d} to {end:%b %-d}"
    log_append(state, f"{label}…")
    result = caldir(*args, timeout=120)
    state.next_sync = time.monotonic() + AUTO_SYNC_MINUTES * 60
    if result.returncode != 0:
        log_append(state, f"sync error: {(result.stderr or result.stdout).strip()[:120]}")
    else:
        state.last_sync = datetime.now()
        log_append(state, f"{label} complete")
    reload_local(state, reindex=True)


def after_write(state: AppState) -> None:
    """Called after rsvp/new/edit/delete: show the change now, then queue a sync."""
    reload_local(state, reindex=True)
    if state.auto_sync:
        queue_sync(state)
    else:
        log_append(state, "saved locally (auto-sync off, S to sync)")


def auto_sync_loop(state: AppState) -> None:
    while True:
        if (
            state.auto_sync
            and time.monotonic() >= state.next_sync
            and not state.syncs_pending
            and not state.prompt
            and not state.modal
        ):
            queue_sync(state)
        time.sleep(5)


def toggle_auto_sync(state: AppState) -> None:
    state.auto_sync = not state.auto_sync
    save_prefs({"auto_sync": state.auto_sync})
    if state.auto_sync:
        log_append(state, f"auto-sync on (every {AUTO_SYNC_MINUTES}m and after changes)")
        queue_sync(state)
    else:
        log_append(state, "auto-sync off")


# ---------------------------------------------------------------------------
# Row model & timeline
# ---------------------------------------------------------------------------


def event_row_count(duration_minutes: int) -> int:
    """Proportional row count: <=35m -> 1 row, 36-80m -> 2 rows, >80m -> 3 rows."""
    if duration_minutes <= 35:
        return 1
    if duration_minutes <= 80:
        return 2
    return 3


def fmt_duration_badge(minutes: int) -> str:
    if minutes <= 0:
        return ""
    if minutes >= 60:
        h = minutes // 60
        m = minutes % 60
        return f" ({h}h {m}m)" if m else f" ({h}h)"
    return f" ({minutes}m)"


def fmt_gap_bar(start: datetime, end: datetime, minutes: int, hour24: bool) -> str:
    if minutes >= 60:
        h = minutes // 60
        m = minutes % 60
        dur_str = f"{h}h {m}m" if m else f"{h}h"
    else:
        dur_str = f"{minutes}m"
    return f"┄┄ {fmt_time(start, hour24)} - {fmt_time(end, hour24)} ({dur_str} free) ┄┄"


def event_continuation_lines(e: dict, duration_minutes: int) -> list[str]:
    count = event_row_count(duration_minutes)
    if count <= 1:
        return []

    loc = (e.get("location") or "").strip()
    url = meeting_url(e)
    attendees = e.get("attendees") or []
    going = sum(1 for a in attendees if a.get("status") == "accepted")
    desc = (e.get("description") or "").strip().splitlines()
    desc_clean = [line.strip() for line in desc if line.strip()]

    candidates: list[str] = []
    if loc:
        candidates.append(f"📍 {loc[:50]}")
    if url and url != loc:
        candidates.append(f"⧉  {url[:50]}")
    if attendees:
        att_str = f"{len(attendees)} invited, {going} accepted" if going else f"{len(attendees)} invited"
        candidates.append(att_str)
    if desc_clean and len(candidates) < count - 1:
        candidates.append(desc_clean[0][:50])

    lines: list[str] = []
    for i in range(count - 1):
        lines.append(candidates[i] if i < len(candidates) else "")
    return lines


def event_dot_x(state: AppState, col_idx: int) -> int:
    time_w = 16 if not state.hour24 else 12
    base_x = 3 + time_w + 2
    return base_x + min(col_idx * 4, 16)


def build_day_timeline(state: AppState, rows: list[dict], visible: list[dict]) -> None:
    """Build visual day timeline: earliest to latest event, condensed gaps, overlap indentation."""
    day = state.focus_day
    todays = [e for e in visible if event_day(e) == day]
    all_day_events = sorted((e for e in todays if e["all_day"]), key=lambda e: e.get("summary") or "")
    timed_events = sorted(
        (e for e in todays if not e["all_day"]),
        key=lambda e: (parse_when(e["start"]), parse_when(e["end"])),
    )

    label = day.strftime("%A, %b %-d") + ("  · today" if day == date.today() else "") + "  · timeline"
    rows.append({"kind": "header", "text": label})

    for e in all_day_events:
        rows.append({"kind": "event", "event": e, "timeline": True, "all_day": True})

    if not timed_events:
        if not all_day_events:
            rows.append({"kind": "empty", "text": "Nothing scheduled"})
        else:
            rows.append({"kind": "empty", "text": "No timed events"})
        return

    now = datetime.now()
    is_today = day == date.today()
    placed_now = not is_today

    # Conflict detection
    conflicts: set[str] = set()
    for i in range(len(timed_events)):
        s_i, e_i = parse_when(timed_events[i]["start"]), parse_when(timed_events[i]["end"])
        for j in range(i + 1, len(timed_events)):
            s_j, e_j = parse_when(timed_events[j]["start"]), parse_when(timed_events[j]["end"])
            if max(s_i, s_j) < min(e_i, e_j):
                conflicts.add(timed_events[i]["instance_id"])
                conflicts.add(timed_events[j]["instance_id"])

    # Column allocation for concurrent / overlapping events
    col_ends: list[datetime] = []
    event_cols: dict[str, int] = {}
    for e in timed_events:
        s = parse_when(e["start"])
        end = parse_when(e["end"])
        placed_col = None
        for c, c_end in enumerate(col_ends):
            if c_end <= s:
                col_ends[c] = end
                placed_col = c
                break
        if placed_col is None:
            placed_col = len(col_ends)
            col_ends.append(end)
        event_cols[e["instance_id"]] = placed_col

    # Merged busy intervals
    intervals = [(parse_when(e["start"]), parse_when(e["end"])) for e in timed_events]
    merged: list[list[datetime]] = []
    for s, e_dt in sorted(intervals):
        if not merged or s > merged[-1][1]:
            merged.append([s, e_dt])
        else:
            merged[-1][1] = max(merged[-1][1], e_dt)

    # Process events and gaps
    for b_idx, (b_start, b_end) in enumerate(merged):
        block_events = [e for e in timed_events if b_start <= parse_when(e["start"]) < b_end]
        for e in block_events:
            e_start = parse_when(e["start"])
            e_end = parse_when(e["end"])
            if not placed_now and now <= e_start:
                rows.append({"kind": "now", "text": fmt_time(now, state.hour24)})
                placed_now = True

            dur_min = max(0, int((e_end - e_start).total_seconds() // 60))
            col = event_cols[e["instance_id"]]
            conflict = e["instance_id"] in conflicts
            rows.append(
                {
                    "kind": "event",
                    "event": e,
                    "timeline": True,
                    "col": col,
                    "conflict": conflict,
                    "dur_min": dur_min,
                }
            )
            for cont_text in event_continuation_lines(e, dur_min):
                rows.append(
                    {
                        "kind": "event_cont",
                        "event": e,
                        "col": col,
                        "text": cont_text,
                    }
                )

        # Gap before next busy block
        if b_idx < len(merged) - 1:
            gap_s = merged[b_idx][1]
            gap_e = merged[b_idx + 1][0]
            gap_min = int((gap_e - gap_s).total_seconds() // 60)
            if gap_min >= 10:
                if not placed_now and gap_s <= now <= gap_e:
                    m_before = int((now - gap_s).total_seconds() // 60)
                    m_after = int((gap_e - now).total_seconds() // 60)
                    if m_before >= 15:
                        rows.append({"kind": "gap", "text": fmt_gap_bar(gap_s, now, m_before, state.hour24)})
                    rows.append({"kind": "now", "text": fmt_time(now, state.hour24)})
                    placed_now = True
                    if m_after >= 15:
                        rows.append({"kind": "gap", "text": fmt_gap_bar(now, gap_e, m_after, state.hour24)})
                else:
                    if not placed_now and now <= gap_s:
                        rows.append({"kind": "now", "text": fmt_time(now, state.hour24)})
                        placed_now = True
                    rows.append({"kind": "gap", "text": fmt_gap_bar(gap_s, gap_e, gap_min, state.hour24)})

    if not placed_now:
        rows.append({"kind": "now", "text": fmt_time(now, state.hour24)})
        placed_now = True


def rebuild_rows(state: AppState) -> None:
    """Flatten events into display rows: day headers, events, and a now marker."""
    rows: list[dict] = []
    visible = [e for e in state.events if e["calendar"] not in state.hidden]

    if state.view == "invites":
        pending = [e for e in state.invites if e["calendar"] not in state.hidden]
        rows.append({"kind": "header", "text": f"Pending invites ({len(pending)})"})
        rows += [{"kind": "event", "event": e, "show_date": True} for e in pending]
        if not pending:
            rows.append({"kind": "empty", "text": "No pending invites"})
    elif state.view == "day" and state.day_timeline:
        build_day_timeline(state, rows, visible)
    else:
        start, end = state.window()
        now = datetime.now()
        day = start
        while day <= end:
            todays = sorted(
                (e for e in visible if event_day(e) == day),
                key=lambda e: (not e["all_day"], parse_when(e["start"])),
            )
            label = day.strftime("%A, %b %-d") + ("  · today" if day == date.today() else "")
            rows.append({"kind": "header", "text": label})
            placed_now = day != now.date()
            for e in todays:
                if not placed_now and not e["all_day"] and parse_when(e["start"]) > now:
                    rows.append({"kind": "now", "text": fmt_time(now, state.hour24)})
                    placed_now = True
                rows.append({"kind": "event", "event": e})
            if not placed_now:
                rows.append({"kind": "now", "text": fmt_time(now, state.hour24)})
            if not todays:
                rows.append({"kind": "empty", "text": "Nothing scheduled"})
            day += timedelta(days=1)

    # Keep the cursor on the same event across rebuilds when possible.
    prev = state.selected()
    state.rows = rows
    if prev:
        for i, row in enumerate(rows):
            if row["kind"] == "event" and row["event"]["instance_id"] == prev["instance_id"]:
                state.cursor = i
                break
        else:
            state.cursor = first_event_row(state)
    else:
        state.cursor = first_event_row(state)
    state.dirty = True


def first_event_row(state: AppState) -> int:
    """Prefer the next upcoming event today; else the first event."""
    now = datetime.now()
    events = [i for i, r in enumerate(state.rows) if r["kind"] == "event"]
    for i in events:
        e = state.rows[i]["event"]
        if not e["all_day"] and parse_when(e["end"]) > now:
            return i
    return events[0] if events else 0


def move_cursor(state: AppState, direction: int) -> None:
    i = state.cursor + direction
    while 0 <= i < len(state.rows):
        if state.rows[i]["kind"] == "event":
            state.cursor = i
            return
        i += direction


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def resolve_path(state: AppState, event: dict) -> Path | None:
    """Find the .ics file for an event instance (override file beats master)."""
    if event.get("path"):
        return Path(event["path"])
    candidates = state.path_index.get(event["uid"], [])
    rid = event.get("recurrence_id")
    if rid:
        rid_core = rid.split(":")[-1]
        for path, file_rid in candidates:
            if file_rid and file_rid.endswith(rid_core):
                return path
    for path, file_rid in candidates:
        if file_rid is None:
            return path
    return candidates[0][0] if candidates else None


def calendar_writable(state: AppState, slug: str) -> bool:
    return not any(c["slug"] == slug and c.get("read_only") for c in state.calendars)


def dispatch_rsvp(state: AppState, response: str) -> None:
    event = state.selected()
    if not event:
        return
    path = resolve_path(state, event)
    if not path:
        log_append(state, "can't find .ics for this event")
        return

    def work():
        result = caldir("rsvp", str(path), response, "--timeout", SYNC_TIMEOUT, timeout=120)
        msg = (result.stdout or result.stderr).strip().splitlines()
        log_append(state, f"rsvp {response}: {msg[-1] if msg else 'done'}")
        after_write(state)

    run_bg(state, f"rsvp {response}", work)


def dispatch_open(state: AppState) -> None:
    event = state.selected()
    url = meeting_url(event) if event else None
    if not url:
        log_append(state, "no meeting link on this event")
        return
    webbrowser.open(url)
    log_append(state, f"opened {url[:60]}")


def dispatch_google(state: AppState) -> None:
    """Open the event in Google Calendar, e.g. to use Propose new time."""
    event = state.selected()
    if not event:
        return
    remote = google_remote(state.calendar_dir, event["calendar"])
    url = google_event_url(event, remote) if remote.get("provider") == "google" else None
    if not url:
        log_append(state, "no Google Calendar link for this event")
        return
    webbrowser.open(url)
    log_append(state, "opened in Google Calendar")


def dispatch_edit(state: AppState) -> None:
    event = state.selected()
    if not event:
        return
    if not calendar_writable(state, event["calendar"]):
        log_append(state, "calendar is read-only")
        return
    path = resolve_path(state, event)
    if not path:
        log_append(state, "can't find .ics for this event")
        return

    when_val = event["start"][:10] if event["all_day"] else f"{parse_when(event['start']):%Y-%m-%d %H:%M}"
    dur_val = fmt_duration(event["start"], event["end"], event["all_day"])
    desc_val = " / ".join(
        line.strip() for line in (event.get("description") or "").splitlines() if line.strip()
    )

    fields = [
        {"key": "title", "label": "Title", "value": event.get("summary") or ""},
        {"key": "when", "label": "When", "value": when_val},
        {"key": "duration", "label": "Duration", "value": dur_val},
        {"key": "location", "label": "Location", "value": event.get("location") or ""},
        {"key": "description", "label": "Notes", "value": desc_val},
    ]
    is_master = bool(
        event.get("recurring") and event.get("recurrence_id") and path.name.startswith("_recurring__")
    )
    if is_master:
        fields.append(
            {
                "key": "scope",
                "label": "Apply to",
                "value": "this event",
                "choices": ["this event", "entire series"],
            }
        )

    def on_submit(values: dict[str, str]) -> str | None:
        title = values["title"].strip()
        if not title:
            return "Title is required"
        try:
            day, start_dt = parse_user_when(values["when"], event_day(event))
            duration = parse_duration(values["duration"], start_dt is None)
        except ValueError as exc:
            return str(exc)
        loc = values["location"].strip()
        desc = values["description"].strip()
        scope = "series" if values.get("scope") == "entire series" else "instance"

        def work():
            update_ics_event(
                path,
                event,
                title=title,
                day=day,
                start_dt=start_dt,
                duration=duration,
                location=loc,
                description=desc,
                scope=scope,
            )
            log_append(state, f"updated {title}")
            after_write(state)

        run_bg(state, "edit", work)
        return None

    state.modal = {
        "title": "Edit Event",
        "fields": fields,
        "index": 0,
        "error": None,
        "on_submit": on_submit,
    }


def dispatch_delete(state: AppState) -> None:
    event = state.selected()
    if not event:
        return
    if event.get("recurring"):
        log_append(state, "deleting recurring events isn't supported yet")
        return
    if not calendar_writable(state, event["calendar"]):
        log_append(state, "calendar is read-only")
        return
    path = resolve_path(state, event)
    if not path:
        log_append(state, "can't find .ics for this event")
        return

    def confirmed(answer: str):
        if answer.strip().lower() != "y":
            log_append(state, "delete cancelled")
            return

        def work():
            path.unlink()
            log_append(state, f"deleted {event['summary']}")
            after_write(state)

        run_bg(state, "delete", work)

    start_prompt(state, f"Delete '{event['summary']}'? (y/N)", confirmed)


def dispatch_new(state: AppState) -> None:
    writable = [c["slug"] for c in state.calendars if not c.get("read_only")]
    if not writable:
        log_append(state, "no writable calendars available")
        return
    default_cal = state.default_calendar if state.default_calendar in writable else writable[0]
    default_when = f"{state.focus_day.isoformat()} 09:00"
    if state.focus_day == date.today():
        next_hour = (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        if next_hour.date() == date.today():
            default_when = f"{state.focus_day.isoformat()} {next_hour:%H:%M}"

    fields = [
        {"key": "title", "label": "Title", "value": ""},
        {"key": "when", "label": "When", "value": default_when},
        {"key": "duration", "label": "Duration", "value": "30m"},
        {"key": "location", "label": "Location", "value": ""},
        {"key": "description", "label": "Notes", "value": ""},
        {"key": "calendar", "label": "Calendar", "value": default_cal, "choices": writable},
    ]

    def on_submit(values: dict[str, str]) -> str | None:
        title = values["title"].strip()
        if not title:
            return "Title is required"
        try:
            day, start_dt = parse_user_when(values["when"], state.focus_day)
            duration = parse_duration(values["duration"], start_dt is None)
        except ValueError as exc:
            return str(exc)

        cal_slug = values["calendar"].strip() or default_cal
        if cal_slug not in writable:
            for c in state.calendars:
                if c["name"].lower() == cal_slug.lower() and c["slug"] in writable:
                    cal_slug = c["slug"]
                    break
            else:
                return f"Unknown calendar '{cal_slug}' (use ←/→ to choose)"

        start_arg = day.isoformat() if start_dt is None else f"{start_dt:%Y-%m-%d %H:%M}"
        if start_dt is None:
            dur_arg = f"{max(1, duration.days)}d"
        else:
            dur_arg = f"{max(1, round(duration.total_seconds() / 60))}m"

        loc = values["location"].strip()
        desc = values["description"].strip()
        args = ["new", title, "-s", start_arg, "-d", dur_arg, "-C", cal_slug]
        if loc:
            args += ["-l", loc]

        def work():
            before = set((state.calendar_dir / cal_slug).glob("*.ics"))
            result = caldir(*args)
            if result.returncode != 0:
                log_append(state, f"new failed: {(result.stderr or result.stdout).strip()[:120]}")
                return
            if desc:
                after = set((state.calendar_dir / cal_slug).glob("*.ics"))
                created = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
                if created:
                    raw = created[0].read_text(errors="replace")
                    crlf = "\r\n" if "\r\n" in raw else "\n"
                    desc_line = f"DESCRIPTION:{ics_escape(desc)}{crlf}END:VEVENT"
                    created[0].write_text(raw.replace("END:VEVENT", desc_line, 1))
            log_append(state, f"created {title}")
            after_write(state)

        run_bg(state, "new", work)
        return None

    state.modal = {
        "title": "New Event",
        "fields": fields,
        "index": 0,
        "error": None,
        "on_submit": on_submit,
    }


def toggle_calendar(state: AppState, n: int) -> None:
    if n >= len(state.calendars):
        return
    slug = state.calendars[n]["slug"]
    state.hidden ^= {slug}
    log_append(state, f"{'hid' if slug in state.hidden else 'showing'} {state.calendars[n]['name']}")
    with state.lock:
        rebuild_rows(state)


def toggle_day_timeline(state: AppState) -> None:
    was_month = state.view == "month"
    if state.view != "day":
        state.view = "day"
        state.day_timeline = True
    else:
        state.day_timeline = not state.day_timeline
    save_prefs({"day_timeline": state.day_timeline})
    log_append(state, f"timeline {'on' if state.day_timeline else 'off'}")
    if was_month:
        load_async(state)
    else:
        with state.lock:
            rebuild_rows(state)


def change_day(state: AppState, delta: int) -> None:
    state.focus_day += timedelta(days=delta)
    state.cursor = 0
    if state.view != "month":
        load_async(state)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

KEYPAD_MAP = {
    curses.KEY_A1: ord("7"),
    curses.KEY_A3: ord("9"),
    curses.KEY_B2: ord("5"),
    curses.KEY_C1: ord("1"),
    curses.KEY_C3: ord("3"),
}

SS3_KEYPAD_MAP = {
    ord("p"): ord("0"),
    ord("q"): ord("1"),
    ord("r"): ord("2"),
    ord("s"): ord("3"),
    ord("t"): ord("4"),
    ord("u"): ord("5"),
    ord("v"): ord("6"),
    ord("w"): ord("7"),
    ord("x"): ord("8"),
    ord("y"): ord("9"),
    ord("m"): ord("-"),
    ord("k"): ord("+"),
    ord("j"): ord("*"),
    ord("o"): ord("/"),
    ord("n"): ord("."),
    ord("l"): ord(","),
    ord("M"): 10,  # Numpad Enter
    ord("X"): ord("="),
}


def read_key(stdscr, unget=curses.ungetch) -> int:
    """Read a key and normalize application-keypad (numpad) sequences to ASCII."""
    key = stdscr.getch()
    if key == 27:
        stdscr.nodelay(True)
        try:
            nxt = stdscr.getch()
            if nxt == ord("O"):
                third = stdscr.getch()
                if third in SS3_KEYPAD_MAP:
                    return SS3_KEYPAD_MAP[third]
                if third != -1:
                    unget(third)
                unget(nxt)
            elif nxt != -1:
                unget(nxt)
        finally:
            stdscr.timeout(250)
    return KEYPAD_MAP.get(key, key)


def start_prompt(state: AppState, label: str, on_submit, default: str = "", choices=None) -> None:
    state.prompt = {"label": label, "value": default, "on_submit": on_submit, "choices": choices or []}
    state.dirty = True


def handle_prompt_input(state: AppState, key: int) -> None:
    p = state.prompt
    if key == 27:  # Esc
        state.prompt = None
    elif key in (10, 13, curses.KEY_ENTER):
        state.prompt = None
        p["on_submit"](p["value"])
    elif key in (curses.KEY_BACKSPACE, 127, 8):
        p["value"] = p["value"][:-1]
    elif key == 9 and p["choices"]:  # Tab cycles choices
        choices = p["choices"]
        idx = choices.index(p["value"]) if p["value"] in choices else -1
        p["value"] = choices[(idx + 1) % len(choices)]
    elif key == 21:  # Ctrl-U clears
        p["value"] = ""
    elif 32 <= key < 127:
        p["value"] += chr(key)
    state.dirty = True


def handle_modal_input(state: AppState, key: int) -> None:
    m = state.modal
    if not m:
        return
    fields = m["fields"]
    f = fields[m["index"]]
    choices = f.get("choices")

    if key == 27:  # Esc
        state.modal = None
    elif key in (curses.KEY_UP, curses.KEY_BTAB):
        m["index"] = (m["index"] - 1) % len(fields)
        m["error"] = None
    elif key in (curses.KEY_DOWN, 9):  # Down or Tab
        m["index"] = (m["index"] + 1) % len(fields)
        m["error"] = None
    elif key in (10, 13, curses.KEY_ENTER):
        values = {item["key"]: item["value"] for item in fields}
        err = m["on_submit"](values)
        if err:
            m["error"] = err
        else:
            state.modal = None
    elif choices and key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord(" ")):
        step = -1 if key == curses.KEY_LEFT else 1
        idx = choices.index(f["value"]) if f["value"] in choices else 0
        f["value"] = choices[(idx + step) % len(choices)]
        m["error"] = None
    elif not choices and key in (curses.KEY_BACKSPACE, 127, 8):
        f["value"] = f["value"][:-1]
        m["error"] = None
    elif not choices and key == 21:  # Ctrl-U clears field
        f["value"] = ""
        m["error"] = None
    elif not choices and 32 <= key < 127:
        f["value"] += chr(key)
        m["error"] = None
    state.dirty = True


def handle_normal_input(state: AppState, key: int) -> bool:
    """Returns False to quit."""
    k = chr(key) if 0 <= key < 256 else ""
    if key in (curses.KEY_UP,) or k == "k":
        if state.view == "month":
            change_day(state, -7)
        else:
            move_cursor(state, -1)
    elif key in (curses.KEY_DOWN,) or k == "j":
        if state.view == "month":
            change_day(state, 7)
        else:
            move_cursor(state, 1)
    elif key in (curses.KEY_LEFT,) or k == "h":
        change_day(state, -1)
    elif key in (curses.KEY_RIGHT,) or k == "l":
        change_day(state, 1)
    elif k == "[":
        if state.view == "month":
            state.focus_day = add_months(state.focus_day, -1)
        else:
            change_day(state, -7)
    elif k == "]":
        if state.view == "month":
            state.focus_day = add_months(state.focus_day, 1)
        else:
            change_day(state, 7)
    elif k == "t":
        state.focus_day = date.today()
        if state.view != "month":
            load_async(state)
    elif k == "w":
        state.view = "day" if state.view == "week" else "week"
        load_async(state)
    elif k == "c":
        state.view = "day" if state.view == "month" else "month"
        if state.view == "day":
            load_async(state)
    elif k == "i":
        state.view = "day" if state.view == "invites" else "invites"
        with state.lock:
            rebuild_rows(state)
    elif k == "v":
        toggle_day_timeline(state)
    elif key in (10, 13, curses.KEY_ENTER) or k == "o":
        if state.view == "month" and key in (10, 13, curses.KEY_ENTER):
            state.view = "day"
            load_async(state)
        else:
            dispatch_open(state)
    elif k == "g":
        dispatch_google(state)
    elif k == "a":
        dispatch_rsvp(state, "accept")
    elif k == "d":
        dispatch_rsvp(state, "decline")
    elif k == "m":
        dispatch_rsvp(state, "maybe")
    elif k == "n":
        dispatch_new(state)
    elif k == "e":
        dispatch_edit(state)
    elif k == "x":
        dispatch_delete(state)
    elif k == "s":
        queue_sync(state, full=True)
    elif k == "u":
        toggle_auto_sync(state)
    elif k == "r":
        load_async(state)
    elif k.isdigit() and k != "0":
        toggle_calendar(state, int(k) - 1)
    elif k in ("q", "Q"):
        return False
    state.dirty = True
    return True


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

FOOTER = [
    "←→ Day | [ ] Week | T Today | W Week | C Month | I Invites | V Timeline | 1-9 Cals | R Reload | Q Quit",
    "O Join | G Google | A Accept | D Decline | M Maybe | N New | E Edit | X Delete | S Sync | U Auto-sync",
]


def addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that never throws on the bottom-right cell or narrow terminals."""
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        with contextlib.suppress(curses.error):
            win.addstr(y, x, text[: max(0, w - x - (1 if y == h - 1 else 0))], attr)


def cal_attr(state: AppState, slug: str) -> int:
    for i, c in enumerate(state.calendars):
        if c["slug"] == slug:
            try:
                return curses.color_pair(10 + i) if curses.has_colors() else 0
            except curses.error:
                return 0
    return 0


def draw_event_row(stdscr, state: AppState, y: int, row: dict, selected: bool, width: int) -> None:
    e = row["event"]
    if e["all_day"]:
        when = "all day"
    else:
        start, end = parse_when(e["start"]), parse_when(e["end"])
        when = f"{fmt_time(start, state.hour24)}-{fmt_time(end, state.hour24)}"
    if row.get("show_date"):
        when = f"{event_day(e):%a %b %-d} {when}"
    status = my_status(e, state.my_emails)
    glyph = RSVP_GLYPH.get(status or "", " ")
    link = " ⧉" if meeting_url(e) else ""
    past = not e["all_day"] and parse_when(e["end"]) < datetime.now()

    base = curses.A_REVERSE if selected else (curses.A_DIM if past or status == "declined" else 0)
    addstr(stdscr, y, 0, " " * width, base)
    addstr(stdscr, y, 1, "▸" if selected else " ", base)

    if row.get("timeline") and not e["all_day"]:
        col_idx = row.get("col", 0)
        conflict = row.get("conflict", False)
        dur_min = row.get("dur_min", 0)
        dot_x = event_dot_x(state, col_idx)

        addstr(stdscr, y, 3, when, base)
        if col_idx > 0:
            addstr(stdscr, y, dot_x - 4, "  ↳ ", base | (curses.A_DIM if not selected else 0))
        addstr(stdscr, y, dot_x, "●", base | cal_attr(state, e["calendar"]))
        addstr(stdscr, y, dot_x + 2, glyph, base)
        dur_badge = fmt_duration_badge(dur_min)
        conflict_badge = " ⚡" if conflict else ""
        title_attr = base if past else base | curses.A_BOLD
        addstr(
            stdscr,
            y,
            dot_x + 4,
            f"{e['summary'] or '(no title)'}{dur_badge}{conflict_badge}{link}",
            title_attr,
        )
    else:
        col = 3 + (25 if row.get("show_date") else 14)
        addstr(stdscr, y, 3, when, base)
        addstr(stdscr, y, col, "●", base | cal_attr(state, e["calendar"]))
        addstr(stdscr, y, col + 2, glyph, base)
        title_attr = base if past else base | curses.A_BOLD
        addstr(stdscr, y, col + 4, f"{e['summary'] or '(no title)'}{link}", title_attr)


def draw_event_cont_row(stdscr, state: AppState, y: int, row: dict, selected: bool, width: int) -> None:
    e = row["event"]
    status = my_status(e, state.my_emails)
    past = not e["all_day"] and parse_when(e["end"]) < datetime.now()
    base = curses.A_REVERSE if selected else (curses.A_DIM if past or status == "declined" else 0)
    addstr(stdscr, y, 0, " " * width, base)

    col_idx = row.get("col", 0)
    dot_x = event_dot_x(state, col_idx)

    addstr(stdscr, y, dot_x, "│", base | cal_attr(state, e["calendar"]))
    text = row.get("text", "")
    if text:
        text_attr = base | (curses.A_DIM if not selected else 0)
        addstr(stdscr, y, dot_x + 4, text, text_attr)


def draw_gap_row(stdscr, y: int, row: dict, width: int) -> None:
    addstr(stdscr, y, 0, " " * width, 0)
    addstr(stdscr, y, 3, row["text"], curses.A_DIM)


def draw_month(stdscr, state: AppState, top: int) -> None:
    d = state.focus_day
    today = date.today()
    addstr(stdscr, top, 2, f"{d:%B %Y}", curses.A_BOLD | curses.A_UNDERLINE)
    addstr(stdscr, top + 2, 2, "Mo  Tu  We  Th  Fr  Sa  Su", curses.A_DIM)
    for row_idx, week in enumerate(calendar.monthcalendar(d.year, d.month)):
        y = top + 3 + row_idx
        for col_idx, day_num in enumerate(week):
            if not day_num:
                continue
            cell_date = date(d.year, d.month, day_num)
            attr = 0
            if cell_date == d:
                attr |= curses.A_REVERSE | curses.A_BOLD
            elif cell_date == today:
                attr |= curses.A_BOLD | curses.A_UNDERLINE
            addstr(stdscr, y, 2 + col_idx * 4, f"{day_num:2d}", attr)
    hint = f"{d:%A, %b %-d, %Y}" + ("  · today" if d == today else "")
    addstr(stdscr, top + 10, 2, hint, curses.A_BOLD)
    addstr(
        stdscr,
        top + 11,
        2,
        "←→/↑↓ day/week · [ ] prev/next month · Enter open day · C close",
        curses.A_DIM,
    )


def draw_detail(stdscr, state: AppState, top: int, width: int) -> None:
    e = state.selected()
    addstr(stdscr, top, 0, "─" * width, curses.A_DIM)
    if not e:
        return
    cal = next((c["name"] for c in state.calendars if c["slug"] == e["calendar"]), e["calendar"])
    attendees = e.get("attendees") or []
    going = sum(1 for a in attendees if a.get("status") == "accepted")
    lines = [
        (e["summary"] or "(no title)", curses.A_BOLD),
        (
            f"{cal}"
            + ("  · recurring" if e.get("recurring") else "")
            + (f"  · {len(attendees)} invited, {going} accepted" if attendees else ""),
            0,
        ),
    ]
    if e.get("location"):
        lines.append((f"📍 {e['location']}", 0))
    if meeting_url(e):
        lines.append((f"⧉  {meeting_url(e)}", curses.A_UNDERLINE))
    if e.get("organizer") and e["organizer"].get("email") not in state.my_emails:
        org = e["organizer"]
        lines.append((f"from {org.get('name') or org.get('email')}", curses.A_DIM))
    for line in (e.get("description") or "").strip().splitlines():
        if line.strip():
            lines.append((line.strip(), curses.A_DIM))
    for i, (text, attr) in enumerate(lines[: DETAIL_HEIGHT - 1]):
        addstr(stdscr, top + 1 + i, 2, text, attr)


def draw_modal(stdscr, state: AppState, h: int, w: int) -> None:
    m = state.modal
    if not m:
        return
    fields = m["fields"]
    box_w = min(max(56, w - 8), 76)
    box_h = len(fields) * 2 + 6
    top = max(1, (h - box_h) // 2)
    left = max(2, (w - box_w) // 2)

    for r in range(box_h):
        addstr(stdscr, top + r, left, " " * box_w, 0)
    addstr(stdscr, top, left, "┌" + "─" * (box_w - 2) + "┐", curses.A_BOLD)
    for r in range(1, box_h - 1):
        addstr(stdscr, top + r, left, "│", curses.A_BOLD)
        addstr(stdscr, top + r, left + box_w - 1, "│", curses.A_BOLD)
    addstr(stdscr, top + box_h - 1, left, "└" + "─" * (box_w - 2) + "┘", curses.A_BOLD)

    title = f" {m['title']} "
    addstr(stdscr, top, left + 2, title, curses.A_BOLD | curses.A_REVERSE)

    label_w = 12
    val_w = max(10, box_w - label_w - 6)
    for i, f in enumerate(fields):
        y = top + 2 + i * 2
        focused = i == m["index"]
        marker = "▸ " if focused else "  "
        lbl_attr = curses.A_BOLD if focused else curses.A_DIM
        addstr(stdscr, y, left + 2, f"{marker}{f['label']:<{label_w - 2}}", lbl_attr)

        val = f["value"]
        if f.get("choices"):
            cal_name = next((c["name"] for c in state.calendars if c["slug"] == val), None)
            shown = f"{val} ({cal_name})" if cal_name and cal_name != val else val
            disp = f"◂ {shown} ▸"
            val_attr = (curses.A_REVERSE | curses.A_BOLD) if focused else curses.A_BOLD
            addstr(stdscr, y, left + 2 + label_w, disp[:val_w].ljust(val_w), val_attr)
        else:
            if focused:
                shown = val[-(val_w - 2) :] if len(val) > val_w - 2 else val
                disp = f"{shown}▏"
                addstr(stdscr, y, left + 2 + label_w, disp.ljust(val_w), curses.A_REVERSE)
            else:
                shown = val[:val_w] if val else "—"
                attr = 0 if val else curses.A_DIM
                addstr(stdscr, y, left + 2 + label_w, shown.ljust(val_w), attr)

    footer_y = top + box_h - 2
    if m.get("error"):
        err_attr = curses.color_pair(1) | curses.A_BOLD
        addstr(stdscr, footer_y, left + 2, f"! {m['error']}"[: box_w - 4], err_attr)
    else:
        hint = "↑↓/Tab field · ←→ cycle choice · Enter save · Esc cancel"
        addstr(stdscr, footer_y, left + 2, hint[: box_w - 4], curses.A_DIM)


def draw(stdscr, state: AppState) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    # Header
    start, end = state.window()
    title = {
        "day": f"{start:%a %b %-d, %Y}" + ("  · timeline" if state.day_timeline else ""),
        "week": f"Week of {start:%b %-d} to {end:%b %-d}",
        "month": f"{state.focus_day:%B %Y}",
        "invites": "Invites",
    }[state.view]
    addstr(stdscr, 0, 0, " " * w, curses.A_REVERSE)
    addstr(stdscr, 0, 1, f"TermCal  {title}", curses.A_REVERSE | curses.A_BOLD)
    right = []
    if state.invites:
        right.append(f"{len(state.invites)} invites")
    if state.busy:
        right.append(f"⟳ {state.busy}")
    elif state.last_sync:
        right.append(f"synced {state.last_sync:%H:%M}")
    right.append("auto-sync on" if state.auto_sync else "auto-sync off")
    r = "  ".join(right)
    addstr(stdscr, 0, max(0, w - len(r) - 2), r, curses.A_REVERSE)

    # Calendar legend
    x = 1
    for i, c in enumerate(state.calendars[:9]):
        hidden = c["slug"] in state.hidden
        label = f"{i + 1}  ● {c['name'][:22]}"
        addstr(stdscr, 1, x, label, cal_attr(state, c["slug"]) | (curses.A_DIM if hidden else 0))
        if hidden:
            addstr(stdscr, 1, x + 3, "○", curses.A_DIM)
        x += len(label) + 4

    # Body
    body_top, body_bottom = 3, h - DETAIL_HEIGHT - 3
    if state.view == "month":
        draw_month(stdscr, state, body_top)
    else:
        visible = max(1, body_bottom - body_top)
        scroll = max(0, state.cursor - visible + 2)
        for i, row in enumerate(state.rows[scroll : scroll + visible]):
            y = body_top + i
            kind = row["kind"]
            if kind == "header":
                addstr(stdscr, y, 1, row["text"], curses.A_BOLD | curses.A_UNDERLINE)
            elif kind == "now":
                addstr(stdscr, y, 3, f"── now {row['text']} " + "─" * (w - 20), curses.color_pair(1))
            elif kind == "empty":
                addstr(stdscr, y, 3, row["text"], curses.A_DIM)
            elif kind == "gap":
                draw_gap_row(stdscr, y, row, w)
            elif kind == "event_cont":
                sel_ev = state.selected()
                is_sel = sel_ev is not None and row["event"]["instance_id"] == sel_ev["instance_id"]
                draw_event_cont_row(stdscr, state, y, row, is_sel, w)
            else:
                draw_event_row(stdscr, state, y, row, scroll + i == state.cursor, w)

    draw_detail(stdscr, state, h - DETAIL_HEIGHT - 3, w)

    # Log + two-line key guide; an active prompt takes over the second line
    if state.log:
        addstr(stdscr, h - 3, 1, state.log[-1], curses.A_DIM)
    addstr(stdscr, h - 2, 0, f" {FOOTER[0]}".ljust(w), curses.A_REVERSE)
    if state.prompt:
        p = state.prompt
        line = f" {p['label']}: {p['value']}"
        addstr(stdscr, h - 1, 0, " " * w, curses.A_REVERSE)
        addstr(stdscr, h - 1, 0, line, curses.A_REVERSE | curses.A_BOLD)
        addstr(stdscr, h - 1, max(0, w - 22), "Enter ok · Esc cancel", curses.A_REVERSE)
    else:
        addstr(stdscr, h - 1, 0, f" {FOOTER[1]}".ljust(w), curses.A_REVERSE)

    if state.modal:
        draw_modal(stdscr, state, h, w)

    stdscr.refresh()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def init_colors(state: AppState) -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)  # now marker
    for i, c in enumerate(state.calendars[:50]):
        color = hex_to_256(c.get("color", "")) if curses.COLORS >= 256 else curses.COLOR_CYAN
        curses.init_pair(10 + i, color, -1)


def tui(stdscr):
    curses.curs_set(0)
    with contextlib.suppress(curses.error):
        curses.putp(b"\x1b>")  # keep numeric keypad in normal digit mode (DECPNM)
    stdscr.timeout(250)
    state = AppState()
    try:
        load_calendars(state)
        reload_local(state)
    except FileNotFoundError:
        raise SystemExit("caldir not found on PATH. Install it first: https://caldir.org") from None
    init_colors(state)
    threading.Thread(target=job_worker, args=(state,), daemon=True).start()
    if state.auto_sync:
        queue_sync(state, full=True)  # startup catches up on everything once
    threading.Thread(target=auto_sync_loop, args=(state,), daemon=True).start()

    last_tick = time.monotonic()
    while True:
        if time.monotonic() - last_tick > TICK_SECONDS:
            last_tick = time.monotonic()
            with state.lock:
                rebuild_rows(state)  # moves the now marker
        if state.dirty:
            state.dirty = False
            with state.lock:
                draw(stdscr, state)
                with contextlib.suppress(curses.error):
                    curses.putp(b"\x1b>")
        key = read_key(stdscr)
        if key == -1:
            continue
        if key == curses.KEY_RESIZE:
            state.dirty = True
        elif state.modal:
            handle_modal_input(state, key)
        elif state.prompt:
            handle_prompt_input(state, key)
        elif not handle_normal_input(state, key):
            break


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="termcal",
        description="TermCal: curses TUI calendar built on the caldir CLI.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify caldir installation and configuration without starting the TUI",
    )
    args = parser.parse_args(argv)

    if args.check:
        ok, msg = check_caldir()
        if ok:
            print(f"✓ {msg}")
            sys.exit(0)
        else:
            print(f"✗ {msg}", file=sys.stderr)
            sys.exit(1)

    ok, msg = check_caldir()
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(tui)


if __name__ == "__main__":
    main()
