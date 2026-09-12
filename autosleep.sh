#!/bin/bash
# Let a woken Mac go back to sleep once nobody is using Dispatch on it.
#
# Waking a laptop for Dispatch pins it awake with `pmset disablesleep` -- with
# the lid closed nothing weaker holds it up. But a pin with no expiry is a
# battery leak: a machine woken at 9pm and not consciously released is still
# awake in the morning. This is what expires it.
#
# Idle is measured from the server's .last_activity stamp, which it touches on
# every authenticated request, so an open phone keeps the machine up and a
# forgotten one does not. Installed and armed by wake-macbookpro.sh.
set -uo pipefail

IDLE_MIN=${1:-30}
STAMP="$HOME/claude-dispatch/.last_activity"

# Only ever undo OUR pin. If sleep isn't disabled, this machine is either
# already free to sleep or someone else is holding it, and neither is ours.
[ "$(pmset -g | awk '/SleepDisabled/{print $2}')" = "1" ] || exit 0

now=$(date +%s)
last=$(stat -f %m "$STAMP" 2>/dev/null || echo 0)
if [ "$last" = 0 ]; then          # no stamp yet: start the clock, don't sleep
  touch "$STAMP" 2>/dev/null
  exit 0
fi

[ $(( (now - last) / 60 )) -ge "$IDLE_MIN" ] || exit 0

logger -t dispatch-autosleep "idle ${IDLE_MIN}m with no Dispatch use - sleeping"
sudo -n pmset -a disablesleep 0 2>/dev/null
pkill -f 'caffeinate -dimsu' 2>/dev/null
# Forced, not idle: a stray `caffeinate` asserting PreventSystemSleep forever
# would otherwise keep the machine up no matter what disablesleep says.
pmset sleepnow
