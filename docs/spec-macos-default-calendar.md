# Specification: Setting TermCal as Default macOS Calendar

- **Document**: Technical Specification & Design
- **Status**: Draft / Proposed
- **Target OS**: macOS (13.0+ / Ventura, Sonoma, Sequoia)
- **Target Branch**: `feature/macos-default-calendar` (targeting `develop`)

---

## 1. Overview & Objective

Enable TermCal to serve as the system-wide default calendar on macOS. When configured:
1. Double-clicking any `.ics` file (in Finder, Safari, Slack, or Mail) opens TermCal focused on the event's date, with a prompt to import it to a connected calendar.
2. Clicking `webcal://` or `ical://` calendar subscription links forwards the request to TermCal / caldir.
3. TermCal appears as an option in **Calendar.app $\rightarrow$ Settings $\rightarrow$ General $\rightarrow$ Default calendar app**.

---

## 2. macOS System Constraints & Architecture

macOS manages default applications via **LaunchServices**. Raw CLI binaries (like `/opt/homebrew/bin/termcal` or `~/.local/bin/termcal`) cannot register as default handlers for file types or URL schemes. 

To become eligible, an application must:
1. Be a valid macOS Application bundle (`.app`).
2. Reside in standard application directories (`/Applications` or `~/Applications`).
3. Declare appropriate **Uniform Type Identifiers (UTIs)** and **URL Schemes** in its `Contents/Info.plist`.
4. Register with LaunchServices (via `lsregister`).

Because TermCal is a terminal application using `curses`, the `.app` bundle acts as a lightweight **Terminal Runner** that detects the user's preferred terminal emulator and launches `termcal` inside a live window.

---

## 3. Bundle Structure: `TermCal.app`

The bundle will be installed at `~/Applications/TermCal.app` (user-level, requiring no `sudo`):

```text
~/Applications/TermCal.app/
└── Contents/
    ├── Info.plist
    ├── PkgInfo
    ├── MacOS/
    │   └── termcal-launcher     # Executable shell/python wrapper
    └── Resources/
        └── AppIcon.icns         # High-resolution terminal calendar icon
```

### 3.1 `Contents/Info.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>org.caldir.termcal</string>
    <key>CFBundleName</key>
    <string>TermCal</string>
    <key>CFBundleDisplayName</key>
    <string>TermCal</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>CFBundleExecutable</key>
    <string>termcal-launcher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>

    <!-- Document Types (.ics files) -->
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key>
            <string>iCalendar File</string>
            <key>CFBundleTypeRole</key>
            <string>Viewer</string>
            <key>LSHandlerRank</key>
            <string>Owner</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>com.apple.ical.ics</string>
                <string>public.calendar-date</string>
            </array>
        </dict>
    </array>

    <!-- URL Schemes (webcal, ical, calshow) -->
    <key>CFBundleURLTypes</key>
    <array>
        <dict>
            <key>CFBundleURLName</key>
            <string>Calendar URL</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>webcal</string>
                <string>ical</string>
                <string>calshow</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
```

### 3.2 Launcher Script: `Contents/MacOS/termcal-launcher`

When macOS passes a file or URL to an app, it supplies the argument as `$1`. The launcher handles terminal detection (iTerm2, Ghostty, Alacritty, Kitty, or native Terminal.app):

```bash
#!/bin/bash
# TermCal macOS Application Wrapper
TARGET="$1"

# Resolve termcal binary
TERMCAL_BIN="$(which termcal 2>/dev/null)"
if [ -z "$TERMCAL_BIN" ]; then
    for candidate in "$HOME/.local/bin/termcal" "/opt/homebrew/bin/termcal" "/usr/local/bin/termcal"; do
        if [ -x "$candidate" ]; then
            TERMCAL_BIN="$candidate"
            break
        fi
    done
fi

if [ -z "$TERMCAL_BIN" ]; then
    osascript -e 'display alert "TermCal Not Found" message "Could not locate termcal on PATH or in standard Homebrew/pipx locations."'
    exit 1
fi

CMD="'$TERMCAL_BIN'"
if [ -n "$TARGET" ]; then
    CMD="$CMD '$TARGET'"
fi

# Detect active / preferred terminal emulator
if pgrep -qx "iTerm2"; then
    osascript <<EOF
tell application "iTerm"
    create window with default profile command "$CMD"
    activate
end tell
EOF
elif pgrep -qx "ghostty"; then
    open -na "Ghostty" --args -e "$TERMCAL_BIN $TARGET"
elif pgrep -qx "kitty"; then
    kitty --detach "$TERMCAL_BIN" "$TARGET"
else
    # Default macOS Terminal.app fallback
    osascript <<EOF
tell application "Terminal"
    do script "$CMD"
    activate
end tell
EOF
fi
```

---

## 4. CLI Argument Support in `termcal.py`

TermCal must accept optional positional arguments to handle incoming system requests:

### 4.1 CLI Syntax
```bash
termcal [OPTIONS] [TARGET]
```

Where `TARGET` can be:
- **An `.ics` file path**: e.g. `termcal ~/Downloads/invite.ics`
- **An ISO Date**: e.g. `termcal 2026-12-25`
- **A Calendar URI**: e.g. `termcal webcal://example.com/cal.ics` or `termcal calshow:2026-11-15`

### 4.2 Handling `.ics` File Ingestion
When invoked with an `.ics` file:
1. Parse the primary `VEVENT` block using `termcal.py`'s built-in parser.
2. Extract start date, end date, summary, organizer, and location.
3. Launch TermCal with `state.focus_day` set to the event's start date.
4. If the `.ics` file is external (not already in `state.calendar_dir`), trigger an **"Import Event" Modal**:
   - Shows event details.
   - Dropdown choice of connected calendars to import to (`Personal`, `Work`, etc.).
   - On confirm: copies the `.ics` into the selected calendar's folder and triggers `after_write(state)`.

---

## 5. Built-in Installer Command: `termcal --install-app`

Users should not need to manually create bundle directories or edit XML plists. TermCal will provide self-installation and uninstallation flags:

```bash
# Creates ~/Applications/TermCal.app and registers with LaunchServices
termcal --install-app

# Unregisters and removes ~/Applications/TermCal.app
termcal --uninstall-app
```

### Implementation Details:
1. `termcal --install-app`:
   - Writes `~/Applications/TermCal.app/Contents/Info.plist`.
   - Generates executable `Contents/MacOS/termcal-launcher`.
   - Embeds an SVG/ICNS calendar icon into `Contents/Resources/AppIcon.icns`.
   - Executes LaunchServices registration:
     ```bash
     /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f ~/Applications/TermCal.app
     ```
   - Prints confirmation:
     ```text
     ✓ Installed TermCal.app to ~/Applications/TermCal.app
     ✓ Registered with macOS LaunchServices

     To make TermCal your default calendar:
       1. Open Calendar.app -> Settings (Cmd-,)
       2. Change "Default calendar app" to TermCal

     Or run with duti:
       brew install duti && duti -s org.caldir.termcal com.apple.ical.ics all
     ```

---

## 6. Verification & Automated Testing Plan

1. **Unit Tests in `test_termcal.py`**:
   - `test_cli_target_date`: verify `termcal 2026-12-25` sets `focus_day`.
   - `test_cli_target_ics`: verify passing an `.ics` file parses the date and opens preview modal.
   - `test_generate_app_bundle`: mock filesystem and verify `Info.plist` and launcher script generation.
2. **Interactive Testing on macOS**:
   - Run `termcal --install-app`.
   - Verify `TermCal.app` appears in `Calendar.app` $\rightarrow$ Settings $\rightarrow$ Default calendar app dropdown.
   - Double-click an `.ics` file in Finder and verify TermCal opens focused on that date.
