#!/bin/sh
# Desktop notification + chime for Odin phase transitions.
# macOS: Notification Center banner + system sound. Linux: notify-send + paplay/aplay
# when available. Anywhere else: terminal bell.
# Usage: sh odin-notify.sh "<Phase/Title>" "<Message>" [done|phase|reject|questions|awaiting_human]

PHASE_TITLE="${1:-Odin Allfather}"
MESSAGE="${2:-Phase update complete.}"
STATUS="${3:-phase}"

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
