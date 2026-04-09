#!/bin/sh
# Reference installer for airvpn-wireguard-picker on OPNsense.
#
# Run as root on the OPNsense host. The script:
#
#   1. Installs the bundled zipapp to /usr/local/bin/airvpn-picker
#   2. Installs the configd action file
#   3. Reloads configd so the action becomes available
#   4. Creates the log and state file parents
#   5. Prints the next manual step (GUI cron entry)
#
# This script is *idempotent*: re-running it overwrites the binary and
# the action file with the latest content.
#
# It is intentionally POSIX shell (FreeBSD's /bin/sh is no-frills sh, not
# bash). Tested with shellcheck.
#
# Usage:
#   ./install-opnsense.sh /path/to/airvpn-picker.pyz <peer-pubkey>

set -eu

BIN_SRC="${1:-}"
PEER_PUBKEY="${2:-}"

BIN_DST="/usr/local/bin/airvpn-picker"
ACTION_SRC="$(dirname "$0")/actions_airvpnpicker.conf"
ACTION_DST="/usr/local/opnsense/service/conf/actions.d/actions_airvpnpicker.conf"
LOG_FILE="/var/log/airvpn-picker.log"
STATE_FILE="/var/db/airvpn-picker.json"

if [ -z "$BIN_SRC" ] || [ -z "$PEER_PUBKEY" ]; then
  echo "usage: $0 <picker-binary-or-zipapp> <peer-pubkey>" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "error: must be run as root" >&2
  exit 1
fi

if [ ! -f "$BIN_SRC" ]; then
  echo "error: source binary not found: $BIN_SRC" >&2
  exit 1
fi

if [ ! -f "$ACTION_SRC" ]; then
  echo "error: action template not found: $ACTION_SRC" >&2
  exit 1
fi

echo "==> installing $BIN_DST"
install -m 0755 "$BIN_SRC" "$BIN_DST"

echo "==> installing $ACTION_DST"
# Substitute the peer pubkey placeholder while copying.
sed "s|REPLACE_WITH_PUBKEY|$PEER_PUBKEY|" "$ACTION_SRC" >"$ACTION_DST"
chmod 0644 "$ACTION_DST"

echo "==> ensuring $LOG_FILE and $STATE_FILE parents exist"
mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")"
touch "$LOG_FILE"
chmod 0644 "$LOG_FILE"

echo "==> reloading configd"
service configd restart

echo
echo "Installed."
echo
echo "Test it now:"
echo "  configctl airvpnpicker run"
echo
echo "Then schedule it from the GUI:"
echo "  System > Settings > Cron > +"
echo "  Command: 'AirVPN Picker: pick fastest server'"
echo "  Schedule: */30 * * * *"
echo
echo "Watch it work:"
echo "  tail -f $LOG_FILE"
echo "  wg show wg2 endpoints"
