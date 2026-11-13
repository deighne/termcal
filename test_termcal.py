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

        # When toggling from week view, should stay in week view and toggle timeline
        state.view = "week"
        state.day_timeline = False
        with patch.object(tc, "save_prefs"):
            tc.toggle_day_timeline(state)
            self.assertEqual(state.view, "week")
            self.assertTrue(state.day_timeline)

            tc.toggle_day_timeline(state)
            self.assertEqual(state.view, "week")
            self.assertFalse(state.day_timeline)

        # When toggling from month view, should switch to day view with timeline enabled
        state.view = "month"
        state.day_timeline = False
        with patch.object(tc, "save_prefs"), patch.object(tc, "load_async"):
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

    def test_week_timeline_view(self) -> None:
        # 2026-10-20 is Tuesday; week window is Mon Oct 19 - Sun Oct 25
        state = self.make_state(focus_day=date(2026, 10, 20))
        state.view = "week"
        state.day_timeline = True
        state.events = [
            {
                "instance_id": "m1",
                "uid": "m1",
                "summary": "Monday Kickoff",
                "start": "2026-10-19T09:00:00-06:00",
                "end": "2026-10-19T10:00:00-06:00",
                "all_day": False,
                "calendar": "work",
                "location": "Main Hall",
            },
            {
                "instance_id": "m2",
                "uid": "m2",
                "summary": "Monday Afternoon",
                "start": "2026-10-19T13:00:00-06:00",
                "end": "2026-10-19T13:30:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
            {
                "instance_id": "w1",
                "uid": "w1",
                "summary": "Wednesday Overlap A",
                "start": "2026-10-21T10:00:00-06:00",
                "end": "2026-10-21T11:00:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
            {
                "instance_id": "w2",
                "uid": "w2",
                "summary": "Wednesday Overlap B",
                "start": "2026-10-21T10:30:00-06:00",
                "end": "2026-10-21T11:30:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
        ]
        tc.rebuild_rows(state)

        headers = [r for r in state.rows if r["kind"] == "header"]
        self.assertEqual(len(headers), 7)
        for h_row in headers:
            self.assertNotIn("timeline", h_row["text"])

        gaps = [r for r in state.rows if r["kind"] == "gap"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("3h free", gaps[0]["text"])

        conts = [r for r in state.rows if r["kind"] == "event_cont"]
        self.assertEqual(len(conts), 3)  # m1 (1h), w1 (1h), w2 (1h) each get 1 continuation row
        self.assertIn("Main Hall", conts[0]["text"])

        wed_events = [r for r in state.rows if r["kind"] == "event" and r["event"]["uid"] in ("w1", "w2")]
        self.assertEqual(len(wed_events), 2)
        self.assertTrue(all(ev["conflict"] for ev in wed_events))

        empties = [r for r in state.rows if r["kind"] == "empty"]
        self.assertEqual(len(empties), 5)  # 5 days in the week with no events

    def test_week_timeline_render(self) -> None:
        state = self.make_state(focus_day=date(2026, 10, 20))
        state.view = "week"
        state.day_timeline = True
        state.events = [
            {
                "instance_id": "m1",
                "uid": "m1",
                "summary": "Monday Kickoff",
                "start": "2026-10-19T09:00:00-06:00",
                "end": "2026-10-19T10:00:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
            {
                "instance_id": "m2",
                "uid": "m2",
                "summary": "Monday Afternoon",
                "start": "2026-10-19T13:00:00-06:00",
                "end": "2026-10-19T13:30:00-06:00",
                "all_day": False,
                "calendar": "work",
            },
        ]
        tc.rebuild_rows(state)
        scr = FakeScreen([], h=40, w=120)
        tc.draw(scr, state)
        rendered = scr.rendered_text()

        self.assertIn("Week of Oct 19 to Oct 25  · timeline", rendered)
        self.assertIn("Monday Kickoff", rendered)
        self.assertIn("3h free", rendered)
        self.assertIn("Monday Afternoon", rendered)


class AccountsModalTests(unittest.TestCase):
    def test_get_calendar_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cal_dir = tmp_path / "work" / ".caldir"
            cal_dir.mkdir(parents=True)
            (cal_dir / "config.toml").write_text(
                'provider = "google"\ngoogle_account = "user@example.com"\ngoogle_calendar_id = "c_12345"\n'
            )
            cal = {"slug": "work", "name": "Work", "color": "#0000ff", "read_only": False}
            info = tc.get_calendar_info(tmp_path, cal)
            self.assertEqual(info["slug"], "work")
            self.assertEqual(info["provider"], "google")
            self.assertEqual(info["account"], "user@example.com")
            self.assertFalse(info["read_only"])

    def test_dispatch_accounts_and_navigation(self) -> None:
        with patch.object(tc, "read_config", return_value={}):
            state = tc.AppState()
        state.calendars = [
            {"slug": "personal", "name": "Personal", "color": "#00ff00", "read_only": False},
            {"slug": "work", "name": "Work", "color": "#0000ff", "read_only": False},
        ]
        tc.dispatch_accounts(state)
        self.assertIsNotNone(state.modal)
        self.assertEqual(state.modal["type"], "accounts")
        self.assertEqual(len(state.modal["items"]), 2)
        self.assertEqual(state.modal["index"], 0)

        # Navigate down
        tc.handle_modal_input(state, ord("j"))
        self.assertEqual(state.modal["index"], 1)

        # Navigate up
        tc.handle_modal_input(state, ord("k"))
        self.assertEqual(state.modal["index"], 0)

    def test_accounts_modal_actions(self) -> None:
        with patch.object(tc, "read_config", return_value={"default_calendar": "personal"}):
            state = tc.AppState()
        state.calendars = [
            {"slug": "personal", "name": "Personal", "color": "#00ff00", "read_only": False},
            {"slug": "holidays", "name": "Holidays", "color": "#ff0000", "read_only": True},
        ]
        tc.dispatch_accounts(state)

        # Toggle visibility with Space
        with patch.object(tc, "save_prefs") as mock_save:
            tc.handle_modal_input(state, ord(" "))
            self.assertIn("personal", state.hidden)
            mock_save.assert_called_with({"hidden_calendars": ["personal"]})

        # Try to set read-only holidays as default -> should show error
        state.modal["index"] = 1
        tc.handle_modal_input(state, ord("d"))
        self.assertIn("read-only", state.modal["error"])

        # Set personal as default
        state.modal["index"] = 0
        with patch.object(tc, "save_prefs") as mock_save:
            tc.handle_modal_input(state, ord("d"))
            self.assertIsNone(state.modal["error"])
            self.assertEqual(state.default_calendar, "personal")
            mock_save.assert_called_with({"default_calendar": "personal"})

        # Close with Esc
        tc.handle_modal_input(state, 27)
        self.assertIsNone(state.modal)

    def test_render_accounts_modal(self) -> None:
        with patch.object(tc, "read_config", return_value={"default_calendar": "work"}):
            state = tc.AppState()
        state.calendars = [
            {"slug": "work", "name": "Work", "color": "#0000ff", "read_only": False},
        ]
        tc.dispatch_accounts(state)
        scr = FakeScreen([], h=30, w=100)
        tc.draw(scr, state)
        rendered = scr.rendered_text()

        self.assertIn("Connected Accounts & Calendars", rendered)
        self.assertIn("Work", rendered)
        self.assertIn("★", rendered)  # Default badge
        self.assertIn("caldir connect", rendered)  # Connect command hint


class DispatchCalendarTests(unittest.TestCase):
    def test_dispatch_google(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "work" / ".caldir").mkdir(parents=True)
            (tmp_path / "work" / ".caldir" / "config.toml").write_text(
                'provider = "google"\ngoogle_account = "user@gmail.com"\ngoogle_calendar_id = "c_123"\n'
            )
            with patch.object(tc, "read_config", return_value={}):
                state = tc.AppState()
            state.focus_day = date(2026, 10, 20)
            state.calendar_dir = tmp_path
            state.events = [
                {
                    "instance_id": "e1",
                    "uid": "u1",
                    "summary": "Meeting",
                    "start": "2026-10-20T10:00:00-06:00",
                    "end": "2026-10-20T11:00:00-06:00",
                    "all_day": False,
                    "calendar": "work",
                    "x_properties": [{"name": "X-GOOGLE-EVENT-ID", "value": "eid_123"}],
                }
            ]
            tc.rebuild_rows(state)

            with patch("webbrowser.open") as mock_open:
                tc.dispatch_calendar(state)
                mock_open.assert_called_once()
                self.assertIn("calendar.google.com", mock_open.call_args[0][0])
                self.assertIn("eid=", mock_open.call_args[0][0])

    def test_dispatch_outlook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "work" / ".caldir").mkdir(parents=True)
            (tmp_path / "work" / ".caldir" / "config.toml").write_text(
                'provider = "outlook"\nmicrosoft_account = "corp@company.com"\n'
            )
            with patch.object(tc, "read_config", return_value={}):
                state = tc.AppState()
            state.focus_day = date(2026, 10, 20)
            state.calendar_dir = tmp_path
            state.events = [
                {
                    "instance_id": "e1",
                    "uid": "u1",
                    "summary": "Sync",
                    "start": "2026-10-20T10:00:00-06:00",
                    "end": "2026-10-20T11:00:00-06:00",
                    "all_day": False,
                    "calendar": "work",
                }
            ]
            tc.rebuild_rows(state)

            with patch("webbrowser.open") as mock_open:
                tc.dispatch_calendar(state)
                mock_open.assert_called_once()
                self.assertIn("outlook.office.com", mock_open.call_args[0][0])

    def test_dispatch_icloud_and_local_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "work" / ".caldir").mkdir(parents=True)
            (tmp_path / "work" / ".caldir" / "config.toml").write_text(
                'provider = "icloud"\napple_id = "user@icloud.com"\n'
            )
            ics_file = tmp_path / "work" / "event.ics"
            ics_file.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n")

            with patch.object(tc, "read_config", return_value={}):
                state = tc.AppState()
            state.focus_day = date(2026, 10, 20)
            state.calendar_dir = tmp_path
            state.events = [
                {
                    "instance_id": "e1",
                    "uid": "u1",
                    "summary": "Doctor",
                    "start": "2026-10-20T10:00:00-06:00",
                    "end": "2026-10-20T11:00:00-06:00",
                    "all_day": False,
                    "calendar": "work",
                    "path": str(ics_file),
                }
            ]
            tc.rebuild_rows(state)

            with patch.object(tc, "open_local_file", return_value=True) as mock_open_local:
                tc.dispatch_calendar(state)
                mock_open_local.assert_called_once_with(ics_file)

    def test_dynamic_footer_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "work" / ".caldir").mkdir(parents=True)
            (tmp_path / "work" / ".caldir" / "config.toml").write_text('provider = "outlook"\n')

            with patch.object(tc, "read_config", return_value={}):
                state = tc.AppState()
            state.focus_day = date(2026, 10, 20)
            state.calendar_dir = tmp_path
            state.calendars = [{"slug": "work", "name": "Work", "color": "#0000ff", "read_only": False}]
            state.events = [
                {
                    "instance_id": "e1",
                    "uid": "u1",
                    "summary": "Outlook Event",
                    "start": "2026-10-20T10:00:00-06:00",
                    "end": "2026-10-20T11:00:00-06:00",
                    "all_day": False,
                    "calendar": "work",
                }
            ]
            tc.rebuild_rows(state)
            scr = FakeScreen([], h=30, w=120)
            tc.draw(scr, state)
            rendered = scr.rendered_text()
            self.assertIn("G Outlook", rendered)


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


class TargetArgumentTests(unittest.TestCase):
    def test_parse_target_date_formats(self) -> None:
        today = date.today()
        kind, d, path, info = tc.parse_target_argument("2026-12-25")
        self.assertEqual(kind, "date")
        self.assertEqual(d, date(2026, 12, 25))
        self.assertIsNone(path)
        self.assertIsNone(info)

        kind, d, _, _ = tc.parse_target_argument("today")
        self.assertEqual(kind, "date")
        self.assertEqual(d, today)

        kind, d, _, _ = tc.parse_target_argument("tomorrow")
        self.assertEqual(kind, "date")
        self.assertEqual(d, today + timedelta(days=1))

    def test_parse_target_calshow(self) -> None:
        kind, d, _, _ = tc.parse_target_argument("calshow:2026-11-15")
        self.assertEqual(kind, "date")
        self.assertEqual(d, date(2026, 11, 15))

        base = date(2026, 5, 1)
        kind, d, _, _ = tc.parse_target_argument("calshow:", default_day=base)
        self.assertEqual(kind, "date")
        self.assertEqual(d, base)

        # Cocoa epoch timestamp test
        kind, d, _, _ = tc.parse_target_argument("calshow:100000000")
        self.assertEqual(kind, "date")
        self.assertIsNotNone(d)

    def test_parse_target_urls(self) -> None:
        for url in ("webcal://example.com/cal.ics", "ical://example.com/events", "https://example.com"):
            kind, d, p, _ = tc.parse_target_argument(url)
            self.assertEqual(kind, "url")
            self.assertIsNone(d)
            self.assertIsNone(p)

    def test_parse_target_unknown(self) -> None:
        kind, d, _, _ = tc.parse_target_argument("gibberish_target_not_a_date")
        self.assertEqual(kind, "unknown")
        self.assertIsNone(d)


class RawIcsParsingTests(unittest.TestCase):
    def test_parse_raw_ics_timed_event(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:event-123@example.com\r\n"
            "SUMMARY:Q4 Planning Session\r\n"
            "DTSTART:20261105T140000Z\r\n"
            "DTEND:20261105T150000Z\r\n"
            "LOCATION:Conference Room B\r\n"
            "DESCRIPTION:Quarterly review notes\\nAction items\r\n"
            "ORGANIZER;CN=Alice:mailto:alice@example.com\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        parsed = tc.parse_raw_ics(ics)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["uid"], "event-123@example.com")
        self.assertEqual(parsed["summary"], "Q4 Planning Session")
        self.assertFalse(parsed["all_day"])
        self.assertEqual(parsed["location"], "Conference Room B")
        self.assertIn("Quarterly review notes\nAction items", parsed["description"])
        self.assertIn("mailto:alice@example.com", parsed["organizer"])
        self.assertEqual(parsed["duration"], timedelta(hours=1))

    def test_parse_raw_ics_all_day_event(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:holiday-456\n"
            "SUMMARY:Christmas Day\n"
            "DTSTART;VALUE=DATE:20261225\n"
            "DTEND;VALUE=DATE:20261226\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        parsed = tc.parse_raw_ics(ics)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["all_day"])
        self.assertEqual(parsed["start"], date(2026, 12, 25))
        self.assertEqual(parsed["duration"], timedelta(days=1))

    def test_parse_raw_ics_duration_property(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:standup-789\n"
            "SUMMARY:Daily Standup\n"
            "DTSTART:20261105T090000\n"
            "DURATION:PT45M\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        parsed = tc.parse_raw_ics(ics)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["duration"], timedelta(minutes=45))
        self.assertEqual(parsed["end"], datetime(2026, 11, 5, 9, 45))

    def test_parse_raw_ics_from_file_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test.ics"
            f.write_text(
                "BEGIN:VCALENDAR\n"
                "BEGIN:VEVENT\n"
                "SUMMARY:File Test\n"
                "DTSTART:20261010T100000\n"
                "END:VEVENT\n"
                "END:VCALENDAR\n"
            )
            parsed = tc.parse_raw_ics(f)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["summary"], "File Test")

            non_existent = Path(td) / "non_existent.ics"
            self.assertIsNone(tc.parse_raw_ics(non_existent))

            empty_file = Path(td) / "empty.ics"
            empty_file.write_text("")
            self.assertIsNone(tc.parse_raw_ics(empty_file))


class MacAppBundleTests(unittest.TestCase):
    def test_generate_app_icon_icns(self) -> None:
        data = tc.generate_app_icon_icns()
        self.assertTrue(data.startswith(b"icns"))
        self.assertGreater(len(data), 300)
        self.assertIn(b"ic07", data)
        self.assertIn(b"\x89PNG", data)

    def test_install_and_uninstall_macos_app(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "Applications" / "TermCal.app"

            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value.returncode = 0
                ok, msg = tc.install_macos_app(app_dir)
                self.assertTrue(ok)
                self.assertIn("Installed TermCal.app", msg)
                self.assertTrue((app_dir / "Contents" / "Info.plist").is_file())
                self.assertTrue((app_dir / "Contents" / "PkgInfo").is_file())
                launcher = app_dir / "Contents" / "MacOS" / "termcal-launcher"
                self.assertTrue(launcher.is_file())
                self.assertTrue(launcher.stat().st_mode & 0o111)  # executable
                self.assertTrue((app_dir / "Contents" / "Resources" / "AppIcon.icns").is_file())

                # Test uninstall
                ok, u_msg = tc.uninstall_macos_app(app_dir)
                self.assertTrue(ok)
                self.assertIn("Removed", u_msg)
                self.assertFalse(app_dir.exists())

                # Uninstall again (already uninstalled)
                ok, u_msg2 = tc.uninstall_macos_app(app_dir)
                self.assertTrue(ok)
                self.assertIn("already uninstalled", u_msg2)


class ImportEventModalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.cal_dir = Path(self.td.name) / "caldir"
        self.cal_dir.mkdir()
        (self.cal_dir / "personal").mkdir()
        self.state = tc.AppState()
        self.state.calendar_dir = self.cal_dir
        self.state.calendars = [{"slug": "personal", "name": "Personal", "read_only": False}]
        self.state.default_calendar = "personal"

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_dispatch_import_event_and_submit(self) -> None:
        ext_file = Path(self.td.name) / "invite.ics"
        ext_file.write_text(
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:import-test-1\n"
            "SUMMARY:External Conference\n"
            "DTSTART:20261120T100000\n"
            "DTEND:20261120T110000\n"
            "LOCATION:Austin, TX\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        info = tc.parse_raw_ics(ext_file)
        self.assertIsNotNone(info)

        tc.dispatch_import_event(self.state, ext_file, info)
        self.assertIsNotNone(self.state.modal)
        self.assertEqual(self.state.modal["title"], "Import Event")
        values = {f["key"]: f["value"] for f in self.state.modal["fields"]}
        self.assertEqual(values["title"], "External Conference")
        self.assertEqual(values["calendar"], "personal")

        # Submit modal
        values["title"] = "Renamed Conference"
        err = self.state.modal["on_submit"](values)
        self.assertIsNone(err)

        # Worker processes background job
        label, work_fn = self.state.jobs.get_nowait()
        self.assertEqual(label, "import")
        with patch.object(tc, "after_write") as mock_after_write:
            work_fn()
            mock_after_write.assert_called_once()

        # Check imported file exists in personal calendar
        imported = self.cal_dir / "personal" / "invite.ics"
        self.assertTrue(imported.is_file())
        self.assertIn("SUMMARY:Renamed Conference", imported.read_text())

    def test_handle_target_launch_internal_vs_external(self) -> None:
        # Internal file
        internal_file = self.cal_dir / "personal" / "event1.ics"
        internal_file.write_text(
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:internal-1\n"
            "DTSTART:20261201T090000\n"
            "SUMMARY:Internal\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        res_int = tc.handle_target_launch(self.state, str(internal_file))
        self.assertIsNotNone(res_int)
        self.assertEqual(res_int.get("internal_uid"), "internal-1")
        self.assertEqual(self.state.focus_day, date(2026, 12, 1))

        # External file
        ext_file = Path(self.td.name) / "outside.ics"
        ext_file.write_text(
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:external-2\n"
            "DTSTART:20261215T150000\n"
            "SUMMARY:Outside\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        res_ext = tc.handle_target_launch(self.state, str(ext_file))
        self.assertIsNotNone(res_ext)
        self.assertTrue(res_ext.get("external_ics"))
        self.assertEqual(self.state.focus_day, date(2026, 12, 15))


class CliAppMainTests(unittest.TestCase):
    @patch.object(tc, "install_macos_app", return_value=(True, "Installed TermCal.app OK"))
    def test_cli_install_app_flag(self, mock_install) -> None:
        with self.assertRaises(SystemExit) as ctx, patch("sys.stdout", new_callable=io.StringIO) as out:
            tc.main(["--install-app"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("Installed TermCal.app OK", out.getvalue())
        mock_install.assert_called_once()

    @patch.object(tc, "uninstall_macos_app", return_value=(True, "Removed TermCal.app OK"))
    def test_cli_uninstall_app_flag(self, mock_uninstall) -> None:
        with self.assertRaises(SystemExit) as ctx, patch("sys.stdout", new_callable=io.StringIO) as out:
            tc.main(["--uninstall-app"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("Removed TermCal.app OK", out.getvalue())
        mock_uninstall.assert_called_once()

    @patch.object(tc, "check_caldir", return_value=(True, "caldir OK"))
    @patch("curses.wrapper")
    def test_cli_target_passed_to_wrapper(self, mock_wrapper, _mock_check) -> None:
        tc.main(["2026-12-25"])
        mock_wrapper.assert_called_once_with(tc.tui, "2026-12-25")


if __name__ == "__main__":
    unittest.main()
