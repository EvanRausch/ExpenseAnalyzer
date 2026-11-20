#!/usr/bin/env bash
# service.sh — Install or uninstall the CSV watchdog as a macOS launchd service.
#
# Usage:
#   ./service.sh install    — Load the service; starts now and on every login
#   ./service.sh uninstall  — Stop and remove the service
#   ./service.sh status     — Show whether the service is running
#   ./service.sh logs       — Tail live output from the watchdog

set -euo pipefail

LABEL="com.budgettracker.csvwatchdog"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/launchd/${LABEL}.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$(cd "$(dirname "$0")" && pwd)/logs"

case "${1:-}" in

  install)
    mkdir -p "$LOG_DIR"
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$PLIST_SRC" "$PLIST_DEST"
    launchctl load -w "$PLIST_DEST"
    echo "✓ Service installed and started."
    echo "  Logs: $LOG_DIR/csvwatchdog.log"
    echo "  Run './service.sh status' to confirm."
    ;;

  uninstall)
    if launchctl list | grep -q "$LABEL"; then
      launchctl unload -w "$PLIST_DEST"
      echo "✓ Service stopped."
    fi
    rm -f "$PLIST_DEST"
    echo "✓ Service removed."
    ;;

  status)
    if launchctl list | grep -q "$LABEL"; then
      PID=$(launchctl list | grep "$LABEL" | awk '{print $1}')
      if [ "$PID" = "-" ]; then
        echo "Service is registered but NOT currently running (may have crashed)."
        echo "Check logs/csvwatchdog.error.log for details."
      else
        echo "✓ Service is running (PID $PID)."
      fi
    else
      echo "Service is not installed. Run './service.sh install' to set it up."
    fi
    ;;

  logs)
    tail -f "$LOG_DIR/csvwatchdog.log" "$LOG_DIR/csvwatchdog.error.log" 2>/dev/null \
      || echo "No log files yet. Has the service been started?"
    ;;

  *)
    echo "Usage: $0 {install|uninstall|status|logs}"
    exit 1
    ;;

esac
