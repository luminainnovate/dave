#!/usr/bin/env bash
# Live view of the orchestrator's conversation with the local models.
#
#   ./trace.sh            follow the transcript as it happens
#   ./trace.sh -n 200     start with the last 200 lines
#   ./trace.sh --all      replay the whole file from the beginning
#   ./trace.sh --plain    no colour (for piping / less)
#
# The transcript is written by tracer.py. Verbosity knobs live there:
#   BRAIN_TRACE_FULL=1    never clip long blocks (file contents, system prompts)
#   BRAIN_TRACE_BG=0      hide Open WebUI's title/tag background pings
# Set them before starting the orchestrator, not here.

set -euo pipefail

cd "$(dirname "$0")"
FILE="${BRAIN_TRACE_FILE:-trace.log}"
LINES=60
COLOR=1
FROM_START=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n)       LINES="$2"; shift 2 ;;
        --all)    FROM_START=1; shift ;;
        --plain)  COLOR=0; shift ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)        echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

[[ -t 1 ]] || COLOR=0

if [[ ! -f "$FILE" ]]; then
    echo "No transcript yet at $FILE — waiting for the first request…"
    : > "$FILE"
fi

if [[ $FROM_START -eq 1 ]]; then
    TAIL_ARGS=(-n +1 -F "$FILE")
else
    TAIL_ARGS=(-n "$LINES" -F "$FILE")
fi

if [[ $COLOR -eq 0 ]]; then
    exec tail "${TAIL_ARGS[@]}"
fi

# Colour by line marker. Body lines (indented) inherit a dim grey.
tail "${TAIL_ARGS[@]}" | awk '
    BEGIN {
        dim   = "\033[38;5;245m"; bold = "\033[1m";     off  = "\033[0m"
        blue  = "\033[38;5;39m";  green= "\033[38;5;77m"
        amber = "\033[38;5;214m"; pink = "\033[38;5;170m"
        red   = "\033[38;5;203m"; cyan = "\033[38;5;80m"
    }
    {
        # Timestamp + turn id prefix stays dim; colour the marker that follows.
        if ($0 ~ /^[0-9]{2}:[0-9]{2}:[0-9]{2} [0-9a-f-]{4} /) {
            head = substr($0, 1, 14); rest = substr($0, 15)
        } else { head = ""; rest = $0 }

             if (rest ~ /^═+$/)          c = dim
        else if (rest ~ /^TURN /)         c = bold cyan
        else if (rest ~ /^▸ ROUTE/)       c = bold blue
        else if (rest ~ /^▸ TRIAGE/)      c = blue
        else if (rest ~ /^▸ (USER|SYSTEM)/) c = bold green
        else if (rest ~ /^▸ /)            c = blue
        else if (rest ~ /^── HOP/)        c = bold amber
        else if (rest ~ /^◂ THINKING/)    c = pink
        else if (rest ~ /^◂ MODEL/)       c = green
        else if (rest ~ /^⚙ CALL/)        c = bold amber
        else if (rest ~ /^↩ RESULT/)      c = amber
        else if (rest ~ /^✗/)             c = bold red
        else if (rest ~ /^★/)             c = bold green
        else                              c = dim      # indented body text

        printf "%s%s%s%s%s%s\n", dim, head, off, c, rest, off
        fflush()
    }
'
