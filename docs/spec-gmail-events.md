# Specification: Syncing & Handling Gmail-Created Google Calendar Events (`fromGmail`)

- **Document**: Technical Specification & Design
- **Status**: Draft / Proposed
- **Scope**: `caldir` (`caldir-provider-google`) + `TermCal` (`termcal.py`)

---

## 1. Problem Statement

Events automatically created from emails in Gmail (flights, hotel reservations, event tickets, etc.) appear in Google Calendar with `"This event was automatically created from an email"` and `eventType: "fromGmail"`.

Currently, `caldir` (`caldir-cli 0.14.9`) does not sync `fromGmail` events down to `~/caldir/<slug>/`, so they never appear in TermCal.

### Observed Root Cause in `caldir-provider-google`
1. `caldir-provider-google/src/commands/list_events.rs` fetches events via `google-calendar` (`0.10.0`) `client.events().list_all(...)`.
2. `google_calendar::events::Events::list_all` does not expose or pass the Google Calendar API `eventTypes` query parameter.
3. While `caldir-provider-google/src/google_event/from_google.rs` already preserves non-default `event_type` values via `X-CALDIR-GOOGLE-EVENT-TYPE` (`GOOGLE_EVENT_TYPE_PROPERTY`), `fromGmail` events are not returned by the current `list_all` call.

---

## 2. Goals & Non-Goals

### Goals
1. Sync `fromGmail` events into the local caldir store (`~/caldir/<slug>/`) during `caldir sync` so they appear in `caldir events` and TermCal.
2. Preserve `X-CALDIR-GOOGLE-EVENT-TYPE:fromGmail` in the `.ics` file (already handled by `from_google.rs`).
3. Prevent sync/push failures when a `fromGmail` event is modified or deleted locally, respecting Google Calendar API restrictions on `fromGmail` events.

### Non-Goals
- Turning `fromGmail` events into arbitrary user-editable events on Google's side (Google Calendar API forbids changing core fields like `start`, `end`, `summary`, or `location` on `fromGmail` events).

---

## 3. Upstream Design: `caldir` (`caldir-provider-google`)

### 3.1 Fetching `fromGmail` Events (`commands/list_events.rs`)
- Replace or bypass `google-calendar` `0.10.0`'s `list_all` (or issue the paginated `GET /calendars/{calendarId}/events` request via `reqwest`, matching how `commands/update_event.rs` already uses `reqwest` directly for `PATCH`).
- Explicitly pass repeated `eventTypes` query parameters to include:
  - `default`
  - `fromGmail`
  - `focusTime`
  - `outOfOffice`
  - `workingLocation`
  - `birthday`
- Keep existing `timeMin`, `timeMax`, `showDeleted=true`, and `pageToken` pagination behavior.

### 3.2 Handling `from_google` Edge Cases (`google_event/from_google.rs`)
- Verify `fromGmail` payloads deserialize cleanly:
  - `visibility` may be `"private"` (`Only me`) — already mapped to `Visibility::Private`.
  - `transparency` may be `"transparent"` (`Free`) — already mapped to `Availability::Free`.
  - `attendees` may list only the recipient with `organizer` set to the recipient.
  - Ensure fallback if `i_cal_uid` is empty (fall back to `format!("{}@google.com", event.id)` rather than producing an empty `UID`).

### 3.3 Patching / Deleting `fromGmail` Events (`commands/update_event.rs`)
Google Calendar API restricts `PATCH` on `fromGmail` events to a subset of fields (`reminders`, `colorId`, `visibility`, `status`, `extendedProperties`).
- Mirror the existing `BIRTHDAY_PATCH_FIELDS` pattern in `caldir-provider-google/src/commands/update_event.rs`:
  ```rust
  const FROM_GMAIL_PATCH_FIELDS: &[&str] = &[
      "reminders",
      "colorId",
      "visibility",
      "status",
      "extendedProperties",
  ];
  ```
- When `event.x_property(GOOGLE_EVENT_TYPE_PROPERTY) == Some("fromGmail")`, filter the `PATCH` body to `FROM_GMAIL_PATCH_FIELDS` so status/RSVP/reminder updates succeed without HTTP 400/403 errors from Google.
- `DELETE` (`commands/delete_event.rs`) continues to work normally (Google allows users to delete `fromGmail` events from their calendar).

---

## 4. TermCal Design (`termcal.py`)

Once `caldir` syncs `fromGmail` `.ics` files to `~/caldir/<slug>/`, TermCal will display them automatically with no changes to event loading. Optional UX improvements in `termcal.py`:

1. **Detect `fromGmail` events** in `parse_raw_ics` / event metadata by checking `X-CALDIR-GOOGLE-EVENT-TYPE:fromGmail`.
2. **Detail pane indicator**: Show `from Gmail` alongside `recurring` / `from <organizer>` in `draw_detail()` so the user knows why the event is on their calendar.
3. **Edit guard (`e` key)**: Since Google rejects time/title changes on `fromGmail` events, show a clear status/log message (`"Gmail events can't be edited (duplicate in Google Calendar to edit)"`) rather than letting `caldir sync` fail on push. (Deleting with `x` and RSVP `a`/`d`/`m` remain allowed.)

---

## 5. Verification Plan

1. **In `caldir`**:
   - Unit tests in `caldir-provider-google` verifying:
     - `list_events` sends `eventTypes` including `fromGmail`.
     - `from_google` parses a `fromGmail` JSON payload (`private` visibility, `transparent` availability, `X-CALDIR-GOOGLE-EVENT-TYPE:fromGmail`).
     - `patch_body_without_attendees` restricts `fromGmail` patches to `FROM_GMAIL_PATCH_FIELDS`.
   - Integration test against live `dane-s-life` calendar: `caldir sync --from 2026-11-18 --to 2026-11-19` pulls down the `fromGmail` event into `~/caldir/dane-s-life/`.
2. **In `TermCal`**:
   - Unit tests in `test_termcal.py` verifying `from Gmail` detail display and edit-guard behavior when `X-CALDIR-GOOGLE-EVENT-TYPE:fromGmail` is present.
