#!/usr/bin/env bash
# Turns the Raspberry Pi's Wi-Fi into the race network (issue #42).
#
# Phones, the TV and the admin's phone join this network and open http://race.lan.
# Needs Raspberry Pi OS Bookworm or later (NetworkManager). Run on the Pi:
#
#   sudo deploy/pi/hotspot.sh setup     # create/update the hotspot and start it
#   sudo deploy/pi/hotspot.sh status    # is it up, how many devices are connected
#   sudo deploy/pi/hotspot.sh remove    # back to normal Wi-Fi
#
# Settings come from the environment, or from the repo's .env file (the same file
# docker-compose.pi.yml reads):
#   HOTSPOT_SSID       network name                       (default: TimingMat)
#   HOTSPOT_PASSWORD   WPA2 password, 8-63 characters     (required)
#   HOTSPOT_COUNTRY    Wi-Fi country code, e.g. US, GB    (required: Wi-Fi stays blocked without it)
#   HOTSPOT_HOSTNAME   name phones type in the browser    (default: race.lan)
#   HOTSPOT_CHANNEL    2.4 GHz channel 1-11               (default: 6)
#   HOTSPOT_ADDRESS    the Pi's address on the network    (default: 10.42.0.1/24)
#   HOTSPOT_IFACE      Wi-Fi interface                    (default: wlan0)
#
# DRY_RUN=1 prints what would be done instead of doing it.
#
# Run it with the Pi on Ethernet, a keyboard, or the console: if you're connected over
# Wi-Fi, that connection drops when the hotspot starts (on purpose, wlan0 becomes the AP).

set -euo pipefail

CON_NAME="race-hotspot"
DNS_CONF="${HOTSPOT_DNS_CONF:-/etc/NetworkManager/dnsmasq-shared.d/race-hotspot.conf}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${HOTSPOT_ENV_FILE:-$REPO_DIR/.env}"
DRY_RUN="${DRY_RUN:-0}"

die() { echo "hotspot: $*" >&2; exit 1; }

# Prints a command shell-quoted (dry run) or runs it.
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# Reads HOTSPOT_* lines from .env without executing anything in it. Values already set in
# the environment win.
load_env_file() {
  [ -f "$ENV_FILE" ] || return 0
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      HOTSPOT_*=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    # strip one pair of surrounding quotes
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    case "$key" in
      *[![:upper:][:digit:]_]*) continue ;;  # [A-Z] also matches lowercase in some locales
    esac
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < "$ENV_FILE"
}

load_settings() {
  load_env_file
  SSID="${HOTSPOT_SSID:-TimingMat}"
  PASSWORD="${HOTSPOT_PASSWORD:-}"
  COUNTRY="$(printf '%s' "${HOTSPOT_COUNTRY:-}" | tr '[:lower:]' '[:upper:]')"
  HOSTNAME_ALIAS="${HOTSPOT_HOSTNAME:-race.lan}"
  CHANNEL="${HOTSPOT_CHANNEL:-6}"
  ADDRESS="${HOTSPOT_ADDRESS:-10.42.0.1/24}"
  IFACE="${HOTSPOT_IFACE:-wlan0}"
  PI_IP="${ADDRESS%%/*}"
}

validate() {
  [ -n "$SSID" ] && [ "${#SSID}" -le 32 ] || die "HOTSPOT_SSID must be 1-32 characters."
  [ -n "$PASSWORD" ] || die "Set HOTSPOT_PASSWORD (8-63 characters) in .env or the environment."
  if [ "${#PASSWORD}" -lt 8 ] || [ "${#PASSWORD}" -gt 63 ]; then
    die "HOTSPOT_PASSWORD must be 8-63 characters (WPA2), got ${#PASSWORD}."
  fi
  case "$COUNTRY" in
    [[:upper:]][[:upper:]]) ;;
    "") die "Set HOTSPOT_COUNTRY to your two-letter country code (e.g. US). Pi OS keeps Wi-Fi blocked without it." ;;
    *) die "HOTSPOT_COUNTRY must be two capital letters, e.g. US, GB, DE (got '$COUNTRY')." ;;
  esac
  case "$CHANNEL" in
    1|2|3|4|5|6|7|8|9|10|11) ;;
    *) die "HOTSPOT_CHANNEL must be a 2.4 GHz channel from 1 to 11 (got '$CHANNEL')." ;;
  esac
  case "$ADDRESS" in
    *.*.*.*/*) ;;
    *) die "HOTSPOT_ADDRESS must look like 10.42.0.1/24 (got '$ADDRESS')." ;;
  esac
  case "$HOSTNAME_ALIAS" in
    *[!a-zA-Z0-9.-]*|"") die "HOTSPOT_HOSTNAME may only use letters, digits, dots and dashes (got '$HOSTNAME_ALIAS')." ;;
  esac
}

preflight() {
  [ "$DRY_RUN" = "1" ] && return 0
  [ "$(id -u)" -eq 0 ] || die "Run with sudo."
  command -v nmcli >/dev/null || die "nmcli not found. This needs Raspberry Pi OS Bookworm or later (NetworkManager)."
}

con_exists() {
  [ "$DRY_RUN" = "1" ] && return 1
  nmcli -t -f NAME connection show | grep -qx "$CON_NAME"
}

cmd_setup() {
  load_settings
  validate
  preflight

  echo "Setting Wi-Fi country to $COUNTRY"
  if [ "$DRY_RUN" = "1" ] || command -v raspi-config >/dev/null; then
    run raspi-config nonint do_wifi_country "$COUNTRY"
  else
    run iw reg set "$COUNTRY"
  fi
  run rfkill unblock wifi

  if con_exists; then
    echo "Replacing existing '$CON_NAME' connection"
    run nmcli connection delete "$CON_NAME"
  fi

  echo "Creating access point '$SSID' on $IFACE ($PI_IP, channel $CHANNEL)"
  # shared = NetworkManager runs DHCP + DNS for the clients (and NATs to Ethernet if the
  # Pi has it). powersave 2 = off, which keeps the AP responsive. Priority 100 makes it
  # win over any saved Wi-Fi network at boot.
  run nmcli connection add type wifi ifname "$IFACE" con-name "$CON_NAME" ssid "$SSID" \
    autoconnect yes connection.autoconnect-priority 100 \
    802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel "$CHANNEL" \
    802-11-wireless.powersave 2 \
    ipv4.method shared ipv4.addresses "$ADDRESS" ipv6.method disabled \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
    wifi-sec.psk "$PASSWORD"

  echo "Pointing http://$HOSTNAME_ALIAS at the Pi"
  local dns_line="address=/$HOSTNAME_ALIAS/$PI_IP"
  if [ "$DRY_RUN" = "1" ]; then
    echo "+ write $DNS_CONF: $dns_line"
  else
    mkdir -p "$(dirname "$DNS_CONF")"
    printf '# Written by deploy/pi/hotspot.sh: phones on the race network resolve this name to the Pi\n%s\n' \
      "$dns_line" > "$DNS_CONF"
  fi

  run nmcli connection up "$CON_NAME"

  cat <<EOF

Hotspot is up and will start on every boot.
  Network:  $SSID
  Open:     http://$HOSTNAME_ALIAS   (or http://$PI_IP)
  QR codes: http://$HOSTNAME_ALIAS/join   (admin login; print it for the start area)

Start the timer if it isn't running:  docker compose -f docker-compose.pi.yml up -d
EOF
}

cmd_status() {
  load_settings
  preflight
  if ! con_exists && [ "$DRY_RUN" != "1" ]; then
    echo "No '$CON_NAME' connection. Run: sudo $0 setup"
    exit 1
  fi
  run nmcli -f GENERAL.STATE,802-11-wireless.ssid,802-11-wireless.channel,IP4.ADDRESS connection show "$CON_NAME"
  if [ "$DRY_RUN" != "1" ] && command -v iw >/dev/null; then
    echo "Connected devices: $(iw dev "$IFACE" station dump | grep -c '^Station' || true)"
  fi
  if [ "$DRY_RUN" != "1" ] && [ -f "$DNS_CONF" ]; then
    echo "DNS: $(grep '^address=' "$DNS_CONF")"
  fi
}

cmd_remove() {
  load_settings
  preflight
  if con_exists; then
    run nmcli connection delete "$CON_NAME"
  elif [ "$DRY_RUN" = "1" ]; then
    run nmcli connection delete "$CON_NAME"
  else
    echo "No '$CON_NAME' connection to remove."
  fi
  run rm -f "$DNS_CONF"
  echo "Hotspot removed. To join a normal Wi-Fi network: sudo nmcli device wifi connect <SSID> --ask"
}

case "${1:-}" in
  setup) cmd_setup ;;
  status) cmd_status ;;
  remove) cmd_remove ;;
  *) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
