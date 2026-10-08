#!/bin/sh
# One short native power-button press; no polling or work after waking.
set -eu
case "$1" in
    /dev/input/by-path/platform-30370000.snvs:snvs-powerkey-event|/dev/input/event[0-9]*) ;;
    *) echo 'Unexpected power-button path.' >&2; exit 1 ;;
esac
[ -c "$1" ] || { echo 'Power-button device is missing.' >&2; exit 1; }
# Allow the SSH request to exit before the tablet's network is suspended.
sleep 1
# rM2 armv7 input_event: timeval(8), type(2), code(2), value(4).
# EV_KEY/KEY_POWER down + SYN_REPORT, up + SYN_REPORT. No long press.
printf '\000\000\000\000\000\000\000\000\001\000\164\000\001\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\001\000\164\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000' > "$1"
