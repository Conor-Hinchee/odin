#!/bin/sh
# Desktop notification + chime for Odin phase transitions.
# macOS: Notification Center banner + system sound. Linux: notify-send + paplay/aplay
# when available. Anywhere else: terminal bell.
# Usage: sh odin-notify.sh "<Phase/Title>" "<Message>" [done|phase|reject|questions|awaiting_human] [state-file]
#
# With a state file, macOS banners come from terminal-notifier (when installed) and clicking one
# opens that task's page. The banner is skipped when an open Odin page with browser notifications
# allowed already covers the task, so the alert arrives once.

PHASE_TITLE="${1:-Odin Allfather}"
MESSAGE="${2:-Phase update complete.}"
STATUS="${3:-phase}"
STATE_FILE="${4:-}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

if [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ]; then
  STATE_FILE=$(cd "$(dirname "$STATE_FILE")" && pwd)/$(basename "$STATE_FILE")
  BASE_URL=$(python3 "$SCRIPT_DIR/odin_config.py" --url 2>/dev/null)
  FILE_Q=$(python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" "$STATE_FILE")
  if [ -n "$BASE_URL" ]; then
    # -k: loopback call to our own server; system curl may not trust a locally generated CA.
    COVERED=$(curl -sk --max-time 1 "${BASE_URL}/api/presence?file=${FILE_Q}" 2>/dev/null)
    case "$COVERED" in
      *'"covered": true'*) exit 0 ;;
    esac
    OPEN_URL="${BASE_URL}/go?file=${FILE_Q}"
  fi
fi

case "$STATUS" in
  done|DONE)
    MAC_SOUND="Hero"; FREEDESKTOP_SOUND="complete" ;;
  reject|REJECT)
    MAC_SOUND="Basso"; FREEDESKTOP_SOUND="dialog-error" ;;
  questions|awaiting_human|QUESTIONS|AWAITING_HUMAN)
    MAC_SOUND="Ping"; FREEDESKTOP_SOUND="dialog-question" ;;
  *)
    MAC_SOUND="Glass"; FREEDESKTOP_SOUND="message" ;;
esac

TITLE="⚡ Odin — ${PHASE_TITLE}"

case "$(uname -s)" in
  Darwin)
    if command -v terminal-notifier >/dev/null 2>&1; then
      # -sound plays the chime, so skip afplay. -group replaces this task's previous banner.
      if [ -n "$OPEN_URL" ]; then
        terminal-notifier -title "$TITLE" -message "$MESSAGE" -sound "$MAC_SOUND" \
          -group "odin-$(basename "$STATE_FILE" .html)" -open "$OPEN_URL" >/dev/null 2>&1
      else
        terminal-notifier -title "$TITLE" -message "$MESSAGE" -sound "$MAC_SOUND" >/dev/null 2>&1
      fi
      exit 0
    fi
    # Fallback: osascript banners are attributed to Script Editor, and clicking one opens it.
    # Escape double quotes for AppleScript string literals
    esc_title=$(printf '%s' "$TITLE" | sed 's/"/\\"/g')
    esc_msg=$(printf '%s' "$MESSAGE" | sed 's/"/\\"/g')
    osascript -e "display notification \"${esc_msg}\" with title \"${esc_title}\" sound name \"${MAC_SOUND}\"" >/dev/null 2>&1
    SOUND_FILE="/System/Library/Sounds/${MAC_SOUND}.aiff"
    [ -f "$SOUND_FILE" ] && afplay "$SOUND_FILE" >/dev/null 2>&1
    ;;
  Linux)
    command -v notify-send >/dev/null 2>&1 && notify-send "$TITLE" "$MESSAGE" >/dev/null 2>&1
    SOUND_FILE="/usr/share/sounds/freedesktop/stereo/${FREEDESKTOP_SOUND}.oga"
    if [ -f "$SOUND_FILE" ] && command -v paplay >/dev/null 2>&1; then
      paplay "$SOUND_FILE" >/dev/null 2>&1
    else
      printf '\a'
    fi
    ;;
  *)
    printf '\a'
    ;;
esac
exit 0
