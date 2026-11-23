#!/bin/bash
# CI only: run a command for 12 s under Xvfb and a private D-Bus, with an empty
# home. Fails if it exits early or prints a Python traceback.
set -u
log=$(mktemp)
HOME=$(mktemp -d)
export HOME GSK_RENDERER=cairo NO_AT_BRIDGE=1 LANG=C.UTF-8
if [ -z "${DISPLAY:-}" ]; then
    Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
    export DISPLAY=:99
    sleep 2
fi
timeout 12 dbus-run-session -- "$@" >"$log" 2>&1
rc=$?
cat "$log"
if grep -q Traceback "$log"; then echo "::error::Python traceback in $*"; exit 1; fi
if [ "$rc" != 124 ]; then echo "::error::$* exited early with code $rc"; exit 1; fi
echo "$* ran 12 s without errors"
