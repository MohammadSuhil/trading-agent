#!/bin/zsh
# Install (or remove) the Trading Agent launchd agents.
#
#   infra/install_launchd.sh install | uninstall | status
#
# These are USER agents (~/Library/LaunchAgents) and need no sudo. They make
# the datastores, the API and the 18:45 nightly run survive logout and reboot.
#
# ---------------------------------------------------------------------------
# RUNNING WHILE THE MAC IS ASLEEP -- read this, it is the part that is not
# automatic and the part that needs your decision.
#
# launchd alone does NOT wake a sleeping Mac. A StartCalendarInterval job whose
# window passes while asleep runs at the next WAKE, which may be the next
# morning. That is safe here (the run is idempotent and bhavcopies are
# immutable) but it is not "ran at 18:45".
#
# To make the machine wake for it, schedule a power event. This needs sudo, so
# it is printed for you to run rather than run for you:
#
#   sudo pmset repeat wakeorpoweron MTWRF 18:40:00
#
# Verify with `pmset -g sched`, and undo with `sudo pmset repeat cancel`.
# On a laptop this only fires on AC power unless you also allow wake on
# battery. The machine wakes, runs the job, and returns to sleep on its own
# idle timer.
# ---------------------------------------------------------------------------
set -u
ROOT=/Users/msuhi00/Projects/trading-agent
DEST="$HOME/Library/LaunchAgents"
AGENTS=(com.tradingagent.clickhouse com.tradingagent.mongod com.tradingagent.api com.tradingagent.nightly)
UID_N=$(id -u)

case "${1:-status}" in
install)
  mkdir -p "$DEST" "$ROOT/data/logs"
  for a in $AGENTS; do
    src="$ROOT/infra/launchd/$a.plist"
    [ -f "$src" ] || { echo "  skip   $a (no plist)"; continue; }
    plutil -lint "$src" >/dev/null || { echo "  FAIL   $a: malformed plist"; continue; }
    cp "$src" "$DEST/$a.plist"
    launchctl bootout "gui/$UID_N/$a" 2>/dev/null
    if launchctl bootstrap "gui/$UID_N" "$DEST/$a.plist" 2>/dev/null; then
      echo "  ok     $a"
    else
      echo "  FAIL   $a (see: launchctl print gui/$UID_N/$a)"
    fi
  done
  echo
  echo "To also WAKE the Mac for the 18:45 run, this needs sudo — run it yourself:"
  echo "    sudo pmset repeat wakeorpoweron MTWRF 18:40:00"
  ;;
uninstall)
  for a in $AGENTS; do
    launchctl bootout "gui/$UID_N/$a" 2>/dev/null && echo "  stopped $a"
    rm -f "$DEST/$a.plist"
  done
  echo "  (scheduled wake, if set, is separate: sudo pmset repeat cancel)"
  ;;
status)
  for a in $AGENTS; do
    if launchctl print "gui/$UID_N/$a" >/dev/null 2>&1; then
      state=$(launchctl print "gui/$UID_N/$a" 2>/dev/null | awk '/state = /{print $3; exit}')
      echo "  loaded   $a  (${state:-unknown})"
    else
      echo "  absent   $a"
    fi
  done
  echo
  echo "scheduled power events:"
  pmset -g sched | sed 's/^/  /'
  ;;
*)
  echo "usage: $0 install|uninstall|status" >&2; exit 2;;
esac
