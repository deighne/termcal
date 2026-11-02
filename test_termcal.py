"""Unit tests for TermCal."""

from __future__ import annotations

import curses
import io
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).parent))
import termcal as tc


class FakeScreen:
    """Minimal curses window double with a queued input stream."""

    def __init__(self, keys: list[int], h: int = 40, w: int = 100):
        self.keys = list(keys)
        self.h = h
        self.w = w
        self.cells: dict[tuple[int, int], str] = {}

    def getch(self) -> int:
        return self.keys.pop(0) if self.keys else -1

    def unget(self, ch: int) -> None:
        self.keys.insert(0, ch)

    def nodelay(self, _flag: bool) -> None:
        pass

    def timeout(self, _ms: int) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return self.h, self.w

    def erase(self) -> None:
        self.cells.clear()

    def refresh(self) -> None:
        pass

    def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
        for i, ch in enumerate(text):
            if 0 <= y < self.h and 0 <= x + i < self.w:
                self.cells[(y, x + i)] = ch

    def rendered_text(self) -> str:
        lines = []
        for r in range(self.h):
            row = "".join(self.cells.get((r, c), " ") for c in range(self.w)).rstrip()
            if row:
                lines.append(row)
        return "\n".join(lines)


class KeyAndNumpadTests(unittest.TestCase):
    def test_ss3_numpad_digits_and_operators(self) -> None:
        seq: list[int] = []
        expected = "0123456789-+*/.,\n="
        for code in b"pqrstuvwxy" + b"mkjonlMX":
            seq.extend([27, ord("O"), code])
        scr = FakeScreen(seq)
        out = "".join(chr(tc.read_key(scr, unget=scr.unget)) for _ in range(len(expected)))
        self.assertEqual(out, expected)

    def test_curses_keypad_constants(self) -> None:
        scr = FakeScreen([curses.KEY_A1, curses.KEY_A3, curses.KEY_B2, curses.KEY_C1, curses.KEY_C3])
        out = "".join(chr(tc.read_key(scr, unget=scr.unget)) for _ in range(5))
        self.assertEqual(out, "79513")

    def test_plain_escape_preserved(self) -> None:
        scr = FakeScreen([27])
        self.assertEqual(tc.read_key(scr, unget=scr.unget), 27)

    def test_non_numpad_escape_sequence_ungetched(self) -> None:
        scr = FakeScreen([27, ord("O"), ord("P")])  # F1 (\x1bOP)
        self.assertEqual(tc.read_key(scr, unget=scr.unget), 27)
        self.assertEqual(scr.keys, [ord("O"), ord("P")])


class ParsingTests(unittest.TestCase):
    def test_parse_user_when_formats(self) -> None:
        d = date(2026, 11, 5)
        self.assertEqual(tc.parse_user_when("2026-11-05 14:30", d), (d, datetime(2026, 11, 5, 14, 30)))
        self.assertEqual(tc.parse_user_when("2026-11-05 2:15pm", d), (d, datetime(2026, 11, 5, 14, 15)))
        self.assertEqual(tc.parse_user_when("2026-11-05 12am", d), (d, datetime(2026, 11, 5, 0, 0)))
        self.assertEqual(tc.parse_user_when("2026-11-05 12pm", d), (d, datetime(2026, 11, 5, 12, 0)))
        self.assertEqual(tc.parse_user_when("2026-11-05", d), (d, None))
        self.assertEqual(tc.parse_user_when("all day", d), (d, None))
        self.assertEqual(tc.parse_user_when("3:45pm", d), (d, datetime(2026, 11, 5, 15, 45)))
        self.assertEqual(tc.parse_user_when("noon", d), (d, datetime(2026, 11, 5, 12, 0)))
        with self.assertRaises(ValueError):
            tc.parse_user_when("not-a-time", d)

    def test_parse_duration(self) -> None:
        self.assertEqual(tc.parse_duration("30m", False), timedelta(minutes=30))
        self.assertEqual(tc.parse_duration("1h 30m", False), timedelta(minutes=90))
        self.assertEqual(tc.parse_duration("1.5h", False), timedelta(minutes=90))
        self.assertEqual(tc.parse_duration("45", False), timedelta(minutes=45))
        self.assertEqual(tc.parse_duration("2d", True), timedelta(days=2))
        with self.assertRaises(ValueError):
            tc.parse_duration("forever", False)


class ModalAndViewTests(unittest.TestCase):
    def make_state(self) -> tc.AppState:
        with patch.object(tc, "read_config", return_value={"default_calendar": "personal"}):
            state = tc.AppState()
        state.calendars = [
            {"slug": "personal", "name": "Personal", "color": "#00ff00", "read_only": False},
            {"slug": "work", "name": "Work", "color": "#0000ff", "read_only": False},
            {"slug": "holidays", "name": "Holidays", "color": "#ff0000", "read_only": True},
        ]
        state.focus_day = date(2026, 10, 31)
        return state

    def test_new_modal_numpad_typing_and_submit(self) -> None:
        state = self.make_state()
        tc.dispatch_new(state)
        self.assertIsNotNone(state.modal)

        # Type title "Sync 12" using numpad SS3 for "12", then Tab to When, clear, type numpad time
        keys = [
            *[ord(c) for c in "Sync "],
            27,
            ord("O"),
            ord("q"),  # numpad 1
            27,
            ord("O"),
            ord("r"),  # numpad 2
            9,  # Tab to When
            21,  # Ctrl-U clear
            *[ord(c) for c in "2026-10-31 "],
            curses.KEY_C1,  # keypad 1
            curses.KEY_B2,  # keypad 5
            ord(":"),
            27,
            ord("O"),
            ord("p"),  # numpad 0
            27,
            ord("O"),
            ord("p"),  # numpad 0
            27,
            ord("O"),
            ord("M"),  # numpad Enter to submit
        ]
        scr = FakeScreen(keys)
        with patch.object(tc, "run_bg") as mock_bg:
            while scr.keys:
                k = tc.read_key(scr, unget=scr.unget)
                tc.handle_modal_input(state, k)
            self.assertIsNone(state.modal)
            self.assertEqual(mock_bg.call_count, 1)

    def test_modal_validation_error_keeps_modal_open(self) -> None:
        state = self.make_state()
        tc.dispatch_new(state)
        tc.handle_modal_input(state, 10)  # Enter with empty title
        self.assertIsNotNone(state.modal)
        self.assertEqual(state.modal["error"], "Title is required")

    def test_month_view_toggle_and_navigation(self) -> None:
        state = self.make_state()
        with patch.object(tc, "load_async") as mock_load:
            tc.handle_normal_input(state, ord("c"))
            self.assertEqual(state.view, "month")
            mock_load.assert_not_called()

            tc.handle_normal_input(state, curses.KEY_RIGHT)
            self.assertEqual(state.focus_day, date(2026, 11, 1))
            tc.handle_normal_input(state, curses.KEY_DOWN)
            self.assertEqual(state.focus_day, date(2026, 11, 8))

            scr = FakeScreen([])
            with patch.object(curses, "has_colors", return_value=False):
                tc.draw(scr, state)
            self.assertIn("November 2026", scr.rendered_text())

            tc.handle_normal_input(state, 10)  # Enter opens focused day
            self.assertEqual(state.view, "day")
            self.assertEqual(mock_load.call_count, 1)


class IcsAndPathTests(unittest.TestCase):
    def test_build_path_index_and_resolve_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cal_dir = Path(tmp) / "work"
            cal_dir.mkdir(parents=True)
            master = cal_dir / "_recurring__standup.ics"
            override = cal_dir / "2026-10-31T0930__standup.ics"
            master.write_text(
                "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
                "UID:very-long-uid-\r\n folded-part@caldir\r\n"
                "DTSTART;TZID=America/Chicago:20261001T093000\r\n"
                "RRULE:FREQ=DAILY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
            )
            override.write_text(
                "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
                "UID:very-long-uid-folded-part@caldir\r\n"
                "RECURRENCE-ID;TZID=America/Chicago:20261031T093000\r\n"
                "DTSTART;TZID=America/Chicago:20261031T100000\r\n"
                "END:VEVENT\r\nEND:VCALENDAR\r\n"
            )

            with patch.object(tc, "read_config", return_value={}):
                state = tc.AppState()
            state.path_index = tc.build_path_index(Path(tmp))

            ev_override = {
                "uid": "very-long-uid-folded-part@caldir",
                "recurrence_id": "TZID=America/Chicago:20261031T093000",
            }
            ev_other_day = {
                "uid": "very-long-uid-folded-part@caldir",
                "recurrence_id": "TZID=America/Chicago:20261101T093000",
            }
            self.assertEqual(tc.resolve_path(state, ev_override), override)
            self.assertEqual(tc.resolve_path(state, ev_other_day), master)

    def test_update_ics_event_instance_and_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "_recurring__standup.ics"
            master.write_text(
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:CALDIR\r\n"
                "BEGIN:VEVENT\r\nUID:standup-1@caldir\r\nSUMMARY:Standup\r\n"
                "DTSTART;TZID=America/Chicago:20261001T093000\r\n"
                "DTEND;TZID=America/Chicago:20261001T100000\r\n"
                "RRULE:FREQ=DAILY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
            )
            ev = {
                "uid": "standup-1@caldir",
                "summary": "Standup",
                "start": "2026-10-31T09:30:00-05:00",
                "end": "2026-10-31T10:00:00-05:00",
                "all_day": False,
                "recurring": True,
                "recurrence_id": "TZID=America/Chicago:20261031T093000",
                "tzid": "America/Chicago",
            }
            out = tc.update_ics_event(
                master,
                ev,
                title="Late Standup",
                day=date(2026, 10, 31),
                start_dt=datetime(2026, 10, 31, 10, 30),
                duration=timedelta(minutes=45),
                location="Room B",
                description="Moved later",
                scope="instance",
            )
            self.assertNotEqual(out, master)
            self.assertTrue(out.exists())
            override_text = out.read_text()
            self.assertIn("RECURRENCE-ID;TZID=America/Chicago:20261031T093000", override_text)
            self.assertIn("SUMMARY:Late Standup", override_text)
            self.assertNotIn("RRULE", override_text)
            # Master remains untouched
            self.assertIn("SUMMARY:Standup", master.read_text())

            # Now update entire series time and title
            tc.update_ics_event(
                master,
                ev,
                title="Team Standup",
                day=date(2026, 10, 31),
                start_dt=datetime(2026, 10, 31, 9, 45),
                duration=timedelta(minutes=15),
                location="Zoom",
                description="",
                scope="series",
            )
            master_text = master.read_text()
            self.assertIn("SUMMARY:Team Standup", master_text)
            # Anchor date (20261001) preserved while time updated to 094500
            self.assertIn("DTSTART;TZID=America/Chicago:20261001T094500", master_text)
            self.assertIn("DTEND;TZID=America/Chicago:20261001T100000", master_text)
            self.assertIn("RRULE:FREQ=DAILY", master_text)


class CaldirCheckTests(unittest.TestCase):
    def test_parse_caldir_version(self) -> None:
        self.assertEqual(tc.parse_caldir_version("caldir-cli 0.14.9"), (0, 14, 9))
        self.assertEqual(tc.parse_caldir_version("caldir 1.2.3"), (1, 2, 3))
        self.assertEqual(tc.parse_caldir_version("caldir 0.15"), (0, 15))
        self.assertIsNone(tc.parse_caldir_version("no version here"))

    @patch("shutil.which", return_value=None)
    @patch.object(Path, "is_file", return_value=False)
    def test_check_caldir_missing(self, _mock_is_file, _mock_which) -> None:
        ok, msg = tc.check_caldir()
        self.assertFalse(ok)
        self.assertIn("caldir not found", msg)

    @patch.object(tc, "find_caldir", return_value="/usr/local/bin/caldir")
    @patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["caldir", "--version"], returncode=0, stdout="caldir 0.13.0\n"
        ),
    )
    def test_check_caldir_too_old(self, _mock_run, _mock_find) -> None:
        ok, msg = tc.check_caldir()
        self.assertFalse(ok)
        self.assertIn("older than required", msg)

    @patch.object(tc, "find_caldir", return_value="/usr/local/bin/caldir")
    def test_check_caldir_no_calendars(self, _mock_find) -> None:
        def side_effect(cmd, **_kwargs):
            if "--version" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="caldir 0.14.9\n")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]\n")

        with patch("subprocess.run", side_effect=side_effect):
            ok, msg = tc.check_caldir()
            self.assertFalse(ok)
            self.assertIn("No calendars found", msg)

    @patch.object(tc, "find_caldir", return_value="/usr/local/bin/caldir")
    def test_check_caldir_ok(self, _mock_find) -> None:
        def side_effect(cmd, **_kwargs):
            if "--version" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="caldir 0.14.9\n")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='[{"slug": "c1", "name": "C1"}]\n'
            )

        with patch("subprocess.run", side_effect=side_effect):
            ok, msg = tc.check_caldir()
            self.assertTrue(ok)
            self.assertIn("OK with 1 calendar(s)", msg)


class TimelineTests(unittest.TestCase):
    def make_state(self, focus_day: date | None = None) -> tc.AppState:
        with patch.object(tc, "read_config", return_value={"default_calendar": "work"}):
            state = tc.AppState()
        state.calendars = [
            {"slug": "work", "name": "Work", "color": "#0000ff", "read_only": False},
        ]
        state.focus_day = focus_day or date(2026, 10, 20)
        state.view = "day"
        state.day_timeline = True
        return state

    def test_toggle_timeline(self) -> None:
        state = self.make_state()
        state.day_timeline = False
        with patch.object(tc, "save_prefs") as mock_save:
            tc.toggle_day_timeline(state)
            self.assertTrue(state.day_timeline)
            mock_save.assert_called_with({"day_timeline": True})

            tc.toggle_day_timeline(state)
            self.assertFalse(state.day_timeline)
            mock_save.assert_called_with({"day_timeline": False})

        # When toggling from week view, should switch to day view with timeline enabled
        state.view = "week"
        state.day_timeline = False
        with patch.object(tc, "save_prefs"):
            tc.toggle_day_timeline(state)
            self.assertEqual(state.view, "day")
            self.assertTrue(state.day_timeline)

    def test_v_key_handles_toggle(self) -> None:
        state = self.make_state()
        state.day_timeline = False
        with patch.object(tc, "save_prefs"):
            tc.handle_normal_input(state, ord("v"))
            self.assertTrue(state.day_timeline)
            tc.handle_normal_input(state, ord("v"))
            self.assertFalse(state.day_timeline)

    def test_duration_and_gap_helpers(self) -> None:
        self.assertEqual(tc.event_row_count(30), 1)
        self.assertEqual(tc.event_row_count(60), 2)
        self.assertEqual(tc.event_row_count(120), 3)

        self.assertEqual(tc.fmt_duration_badge(30), " (30m)")
        self.assertEqual(tc.fmt_duration_badge(60), " (1h)")
        self.assertEqual(tc.fmt_duration_badge(90), " (1h 30m)")

        gap = tc.fmt_gap_bar(datetime(2026, 11, 2, 10, 30), datetime(2026, 11, 2, 13, 0), 150, True)
        self.assertIn("10:30 - 13:00", gap)
        self.assertIn("2h 30m free", gap)

    def test_timeline_empty_day(self) -> None:
        state = self.make_state()
        state.events = []
        tc.rebuild_rows(state)

        kinds = [r["kind"] for r in state.rows]
        self.assertIn("header", kinds)
        self.assertIn("empty", kinds)
        self.assertIn("timeline", state.rows[0]["text"])
        self.assertEqual(state.rows[1]["text"], "Nothing scheduled")

    def test_timeline_all_day_only(self) -> None:
        state = self.make_state()
        state.events = [
            {
                "instance_id": "h1",
                "uid": "h1",
                "summary": "Holiday",
                "start": "2026-10-20",
                "end": "2026-10-21",
                "all_day": True,
                "calendar": "work",
            }
        ]
        tc.rebuild_rows(state)

        event_rows = [r for r in state.rows if r["kind"] == "event"]
        self.assertEqual(len(event_rows), 1)
        self.assertEqual(event_rows[0]["event"]["summary"], "Holiday")
        empty_rows = [r for r in state.rows if r["kind"] == "empty"]
        self.assertEqual(empty_rows[0]["text"], "No timed events")

    def test_timeline_events_and_gap_condensation(self) -> None:
        state = self.make_state()
        state.events = [
            {
                "instance_id": "e1",
                "uid": "e1",
                "summary": "Morning Standup",
                "start": "2026-10-20T09:00:00-06:00",
                "end": "2026-10-20T10:00:00-06:00",
                "all_day": False,
                "calendar": "work",
                "location": "Room 101",
            },
            {
                "instance_id": "e2",
                "uid": "e2",
                "summary": "Afternoon 1:1",
                "start": "2026-10-20T13:00:00-06:00",
                "end": "2026-10-20T13:30:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
        ]
        tc.rebuild_rows(state)

        # Standup starts at 09:00, 1h -> main event row + 1 continuation row
        # Gap 10:00-13:00 (3h free)
        # 1:1 starts at 13:00, 30m -> 1 row
        kinds = [r["kind"] for r in state.rows]
        self.assertEqual(kinds[0], "header")
        self.assertEqual(kinds[1], "event")
        self.assertEqual(state.rows[1]["event"]["summary"], "Morning Standup")
        self.assertEqual(state.rows[1]["dur_min"], 60)

        # Continuation row for 1h meeting
        self.assertEqual(kinds[2], "event_cont")
        self.assertIn("Room 101", state.rows[2]["text"])

        # Gap row
        self.assertEqual(kinds[3], "gap")
        e1_end = tc.parse_when(state.events[0]["end"])
        e2_start = tc.parse_when(state.events[1]["start"])
        expected_range = f"{tc.fmt_time(e1_end, state.hour24)} - {tc.fmt_time(e2_start, state.hour24)}"
        self.assertIn(expected_range, state.rows[3]["text"])
        self.assertIn("3h free", state.rows[3]["text"])

        # Afternoon meeting
        self.assertEqual(kinds[4], "event")
        self.assertEqual(state.rows[4]["event"]["summary"], "Afternoon 1:1")

    def test_timeline_overlapping_events_and_columns(self) -> None:
        state = self.make_state()
        state.events = [
            {
                "instance_id": "e1",
                "uid": "e1",
                "summary": "Big Presentation",
                "start": "2026-10-20T10:00:00-06:00",
                "end": "2026-10-20T11:00:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
            {
                "instance_id": "e2",
                "uid": "e2",
                "summary": "Side Sync",
                "start": "2026-10-20T10:30:00-06:00",
                "end": "2026-10-20T11:30:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
        ]
        tc.rebuild_rows(state)

        event_rows = [r for r in state.rows if r["kind"] == "event"]
        self.assertEqual(len(event_rows), 2)
        # e1 has col 0 and conflict True
        self.assertEqual(event_rows[0]["col"], 0)
        self.assertTrue(event_rows[0]["conflict"])
        # e2 has col 1 and conflict True
        self.assertEqual(event_rows[1]["col"], 1)
        self.assertTrue(event_rows[1]["conflict"])

        # No gap between them because intervals overlap (10:00 - 11:30 is contiguous)
        gaps = [r for r in state.rows if r["kind"] == "gap"]
        self.assertEqual(len(gaps), 0)

    def test_cursor_navigation_skips_gaps_and_continuations(self) -> None:
        state = self.make_state()
        state.events = [
            {
                "instance_id": "e1",
                "uid": "e1",
                "summary": "Event 1",
                "start": "2026-10-20T09:00:00-06:00",
                "end": "2026-10-20T10:00:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
            {
                "instance_id": "e2",
                "uid": "e2",
                "summary": "Event 2",
                "start": "2026-10-20T14:00:00-06:00",
                "end": "2026-10-20T14:30:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
        ]
        tc.rebuild_rows(state)
        # First event is at index 1
        self.assertEqual(state.selected()["summary"], "Event 1")

        # Move down: should skip cont row (idx 2) and gap row (idx 3) and land on Event 2 (idx 4)
        tc.move_cursor(state, 1)
        self.assertEqual(state.selected()["summary"], "Event 2")

        # Move up: returns directly to Event 1
        tc.move_cursor(state, -1)
        self.assertEqual(state.selected()["summary"], "Event 1")

    def test_timeline_now_marker_today(self) -> None:
        today = date.today()
        state = self.make_state(focus_day=today)
        state.events = [
            {
                "instance_id": "e1",
                "uid": "e1",
                "summary": "Morning Meeting",
                "start": f"{today.isoformat()}T08:00:00-06:00",
                "end": f"{today.isoformat()}T09:00:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
            {
                "instance_id": "e2",
                "uid": "e2",
                "summary": "Evening Wrapup",
                "start": f"{today.isoformat()}T19:00:00-06:00",
                "end": f"{today.isoformat()}T19:30:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
        ]
        tc.rebuild_rows(state)
        kinds = [r["kind"] for r in state.rows]
        self.assertIn("now", kinds)

    def test_render_timeline_screen(self) -> None:
        state = self.make_state()
        state.events = [
            {
                "instance_id": "e1",
                "uid": "e1",
                "summary": "Sprint Planning",
                "start": "2026-10-20T09:00:00-06:00",
                "end": "2026-10-20T10:00:00-06:00",
                "all_day": False,
                "calendar": "work",
                "location": "Boardroom",
            },
            {
                "instance_id": "e2",
                "uid": "e2",
                "summary": "Quick Sync",
                "start": "2026-10-20T09:30:00-06:00",
                "end": "2026-10-20T10:00:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
            {
                "instance_id": "e3",
                "uid": "e3",
                "summary": "Project Demo",
                "start": "2026-10-20T14:00:00-06:00",
                "end": "2026-10-20T15:00:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
        ]
        tc.rebuild_rows(state)
        scr = FakeScreen([], h=30, w=120)
        tc.draw(scr, state)
        rendered = scr.rendered_text()

        # Check key timeline visual elements
        self.assertIn("timeline", rendered)
        self.assertIn("Sprint Planning", rendered)
        self.assertIn("(1h)", rendered)
        self.assertIn("Quick Sync", rendered)
        self.assertIn("↳", rendered)  # Overlap branch
        self.assertIn("⚡", rendered)  # Conflict icon
        self.assertIn("Boardroom", rendered)  # Continuation line
        self.assertIn("4h free", rendered)  # Condensed gap divider
        self.assertIn("V Timeline", rendered)  # Footer key guide


class CliArgTests(unittest.TestCase):
    def test_version_flag(self) -> None:
        with self.assertRaises(SystemExit) as ctx, patch("sys.stdout", new_callable=io.StringIO) as out:
            tc.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("0.1.0", out.getvalue())

    def test_help_flag(self) -> None:
        with self.assertRaises(SystemExit) as ctx, patch("sys.stdout", new_callable=io.StringIO) as out:
            tc.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("TermCal", out.getvalue())

    @patch.object(tc, "check_caldir", return_value=(True, "caldir 0.14.9 OK with 1 calendar(s)."))
    def test_check_flag_success(self, _mock_check) -> None:
        with self.assertRaises(SystemExit) as ctx, patch("sys.stdout", new_callable=io.StringIO) as out:
            tc.main(["--check"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("✓", out.getvalue())

    @patch.object(tc, "check_caldir", return_value=(False, "caldir not found"))
    def test_check_flag_failure(self, _mock_check) -> None:
        with self.assertRaises(SystemExit) as ctx, patch("sys.stderr", new_callable=io.StringIO) as err:
            tc.main(["--check"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("✗", err.getvalue())


if __name__ == "__main__":
    unittest.main()
