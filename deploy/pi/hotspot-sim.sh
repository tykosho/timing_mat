#!/usr/bin/env bash
# Tests the race hotspot (issue #42) with simulated phones, no real phones needed.
#
# The kernel's mac80211_hwsim module makes software Wi-Fi radios. One runs the hotspot
# (set up by deploy/pi/hotspot.sh, the same as on race day); the others join it as
# "phones", each in its own network namespace so its traffic really goes over the
# simulated Wi-Fi. Each phone joins with WPA2, gets an address by DHCP, looks up the
# race hostname through the Pi's DNS and loads the site, all at the same time.
#
#   sudo deploy/pi/hotspot-sim.sh run [PHONES]   # default 10 phones
#   sudo deploy/pi/hotspot-sim.sh clean          # remove the simulated radios and hotspot
#
# The real Wi-Fi (wlan0) isn't touched. Settings come from .env like hotspot.sh
# (HOTSPOT_PASSWORD and HOTSPOT_COUNTRY are required). The app must be running and
# reachable on port 80 on the Pi (docker-compose.pi.yml maps it; for mock mode add
# "80:8000" to the ports). What this does NOT tell you: how many real phones the Pi's
# own Wi-Fi chip handles, or range. Simulated radios have no radio limits.
#
# Needs: iw, wpasupplicant, udhcpc, dnsutils, curl (sudo apt install iw wpasupplicant udhcpc dnsutils).

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${HOTSPOT_ENV_FILE:-$REPO_DIR/.env}"
RUN_DIR=/run/hotspot-sim
HELPER_DIR=/var/lib/hotspot-sim   # /run is mounted noexec, so the DHCP hook lives here
NS_PREFIX=hsim

die() { echo "hotspot-sim: $*" >&2; exit 1; }

env_value() {  # reads KEY=value from .env without running it; the environment wins
  local key="$1" line
  if [ -n "${!key:-}" ]; then printf '%s' "${!key}"; return; fi
  [ -f "$ENV_FILE" ] || return 0
  line="$(grep -E "^$key=" "$ENV_FILE" | tail -1)"
  line="${line#*=}"; line="${line#[\"\']}"; line="${line%[\"\']}"
  printf '%s' "$line"
}

hwsim_phys() {  # simulated radios, in creation order
  local p
  for p in /sys/class/ieee80211/*; do
    [ -e "$p/device/driver" ] && [ "$(basename "$(readlink -f "$p/device/driver")")" = mac80211_hwsim ] \
      && basename "$p"
  done | sort -V
}

phy_iface() { ls "/sys/class/ieee80211/$1/device/net" 2>/dev/null | head -1; }

clean() {
  local ns
  pkill -f "wpa_supplicant .*$RUN_DIR" 2>/dev/null
  for ns in $(ip netns list | awk '{print $1}' | grep "^$NS_PREFIX"); do
    ip netns pids "$ns" | xargs -r kill 2>/dev/null
    ip netns delete "$ns"
  done
  if [ -f "$RUN_DIR/ap_iface" ]; then
    HOTSPOT_ENV_FILE="$ENV_FILE" "$REPO_DIR/deploy/pi/hotspot.sh" remove >/dev/null 2>&1
    # Forget the simulated phones' leases, so every run is a crowd of new phones
    rm -f "/var/lib/NetworkManager/dnsmasq-$(cat "$RUN_DIR/ap_iface").leases"
  fi
  sleep 1
  modprobe -r mac80211_hwsim 2>/dev/null
  rm -rf "$RUN_DIR" "$HELPER_DIR"
}

# udhcpc calls this with the lease in its environment. Only set the address: the stock
# script would rewrite the Pi's own /etc/resolv.conf.
write_dhcp_script() {
  mkdir -p "$HELPER_DIR"
  cat > "$HELPER_DIR/dhcp.sh" <<'EOF'
#!/bin/sh
case "$1" in
  bound|renew)
    ip addr flush dev "$interface"
    ip addr add "$ip/${mask:-24}" dev "$interface"
    [ -n "${router:-}" ] && ip route replace default via "${router%% *}" dev "$interface"
    echo "$ip ${dns%% *}" > "$LEASE_FILE" ;;
esac
EOF
  chmod 755 "$HELPER_DIR/dhcp.sh"
}

phone_up() {  # phone_up <n> <phy>: join the hotspot, get a lease. Prints a status line.
  local n="$1" phy="$2" ns="$NS_PREFIX$1" ifc t0 tj t1 state
  ip netns add "$ns"
  iw phy "$phy" set netns name "$ns" || { echo "phone $n: couldn't move $phy"; return 1; }
  ifc="$(ip netns exec "$ns" ls /sys/class/net | grep -v '^lo$' | head -1)"
  ip netns exec "$ns" ip link set lo up
  ip netns exec "$ns" ip link set "$ifc" up
  printf 'network={\n  ssid="%s"\n  psk="%s"\n  key_mgmt=WPA-PSK\n  proto=RSN\n}\n' "$SSID" "$PASSWORD" \
    > "$RUN_DIR/$ns.conf"
  t0=$(date +%s.%N)
  ip netns exec "$ns" wpa_supplicant -B -i "$ifc" -c "$RUN_DIR/$ns.conf" -P "$RUN_DIR/$ns.pid" \
    -f "$RUN_DIR/$ns.wpa.log" >/dev/null
  for _ in $(seq 1 60); do
    state="$(ip netns exec "$ns" iw dev "$ifc" link 2>/dev/null | head -1)"
    case "$state" in Connected*) break ;; esac
    sleep 0.25
  done
  case "$state" in Connected*) ;; *) echo "phone $n: did not join '$SSID'"; return 1 ;; esac
  # "Connected" comes before the WPA handshake; wait for it so DHCP isn't sent too early
  for _ in $(seq 1 40); do
    grep -q CTRL-EVENT-CONNECTED "$RUN_DIR/$ns.wpa.log" 2>/dev/null && break
    sleep 0.25
  done
  tj=$(date +%s.%N)
  LEASE_FILE="$RUN_DIR/$ns.lease" ip netns exec "$ns" \
    udhcpc -i "$ifc" -n -q -t 10 -T 1 -s "$HELPER_DIR/dhcp.sh" > "$RUN_DIR/$ns.dhcp.log" 2>&1 \
    || { echo "phone $n: joined, but got no address (DHCP)"; return 1; }
  t1=$(date +%s.%N)
  printf 'phone %s: joined in %.1f s, got %s %.1f s later\n' "$n" "$(awk "BEGIN{print $tj - $t0}")" \
    "$(cut -d' ' -f1 "$RUN_DIR/$ns.lease")" "$(awk "BEGIN{print $t1 - $tj}")"
}

phone_check() {  # phone_check <n>: DNS + the pages a spectator opens. Prints failures only.
  local n="$1" ns="$NS_PREFIX$1" dns addr code out fail=0
  read -r _ dns < "$RUN_DIR/$ns.lease"
  addr="$(ip netns exec "$ns" dig +short +time=2 +tries=2 "@$dns" "$HOSTNAME_ALIAS" A | head -1)"
  [ "$addr" = "$PI_IP" ] || { echo "phone $n: $HOSTNAME_ALIAS resolved to '${addr:-nothing}', not $PI_IP"; return 1; }
  out="$(ip netns exec "$ns" curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 10 \
         --resolve "$HOSTNAME_ALIAS:80:$addr" "http://$HOSTNAME_ALIAS/")"
  case "$out" in
    "307 http://$HOSTNAME_ALIAS/leaderboard") ;;
    "307 http://$HOSTNAME_ALIAS/crossings")
      echo "phone $n: http://$HOSTNAME_ALIAS/ opens the crew screen (no ADMIN_PIN set?)"; fail=1 ;;
    *) echo "phone $n: http://$HOSTNAME_ALIAS/ gave '$out'"; return 1 ;;
  esac
  code="$(ip netns exec "$ns" curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
          --resolve "$HOSTNAME_ALIAS:80:$addr" "http://$HOSTNAME_ALIAS/leaderboard")"
  [ "$code" = 200 ] || { echo "phone $n: leaderboard gave HTTP $code"; return 1; }
  code="$(ip netns exec "$ns" curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
          --resolve "$HOSTNAME_ALIAS:80:$addr" "http://$HOSTNAME_ALIAS/admin")"
  [ "$code" = 303 ] || { echo "phone $n: /admin gave HTTP $code without logging in"; fail=1; }
  return $fail
}

run() {
  local phones="${1:-10}" phys ap_phy ap_if i ok=0 joined=0 t0 t1
  [ "$(id -u)" -eq 0 ] || die "Run with sudo."
  for c in iw wpa_supplicant udhcpc dig curl nmcli; do
    command -v "$c" >/dev/null || die "$c not found (sudo apt install iw wpasupplicant udhcpc dnsutils curl)"
  done
  SSID="$(env_value HOTSPOT_SSID)"; SSID="${SSID:-TimingMat}"
  PASSWORD="$(env_value HOTSPOT_PASSWORD)"
  HOSTNAME_ALIAS="$(env_value HOTSPOT_HOSTNAME)"; HOSTNAME_ALIAS="${HOSTNAME_ALIAS:-race.lan}"
  local address; address="$(env_value HOTSPOT_ADDRESS)"; PI_IP="${address:-10.42.0.1/24}"; PI_IP="${PI_IP%%/*}"
  [ -n "$PASSWORD" ] || die "Set HOTSPOT_PASSWORD in $ENV_FILE"
  if nmcli -t -f NAME connection show | grep -qx race-hotspot && [ ! -f "$RUN_DIR/ap_iface" ]; then
    die "A real race-hotspot connection exists; this would replace it. Remove it first (hotspot.sh remove)."
  fi

  clean
  mkdir -p "$RUN_DIR" && chmod 700 "$RUN_DIR"   # holds the Wi-Fi password in the phones' configs
  modprobe mac80211_hwsim radios=$((phones + 1)) || die "No mac80211_hwsim module in this kernel."
  sleep 2
  mapfile -t phys < <(hwsim_phys)
  [ "${#phys[@]}" -ge $((phones + 1)) ] || die "Only ${#phys[@]} simulated radios came up."
  ap_phy="${phys[0]}"; ap_if="$(phy_iface "$ap_phy")"
  echo "$ap_if" > "$RUN_DIR/ap_iface"
  write_dhcp_script

  echo "== Hotspot on simulated radio $ap_if (deploy/pi/hotspot.sh setup)"
  HOTSPOT_ENV_FILE="$ENV_FILE" HOTSPOT_IFACE="$ap_if" "$REPO_DIR/deploy/pi/hotspot.sh" setup \
    > "$RUN_DIR/setup.log" 2>&1 || { cat "$RUN_DIR/setup.log"; die "hotspot.sh setup failed"; }
  echo "   up: $(iw dev "$ap_if" info | awk '/ssid/{print "ssid "$2} /type/{print "type "$2}' | tr '\n' ' ')"

  echo "== $phones phones joining at once"
  for i in $(seq 1 "$phones"); do phone_up "$i" "${phys[$i]}" > "$RUN_DIR/up.$i" 2>&1 & done
  wait
  for i in $(seq 1 "$phones"); do
    cat "$RUN_DIR/up.$i"; grep -q ", got " "$RUN_DIR/up.$i" && joined=$((joined + 1))
  done
  echo "   access point sees $(iw dev "$ap_if" station dump | grep -c '^Station') stations"

  echo "== Each phone: DNS for $HOSTNAME_ALIAS, then /, /leaderboard and /admin (all at once)"
  t0=$(date +%s.%N)
  for i in $(seq 1 "$phones"); do
    [ -f "$RUN_DIR/$NS_PREFIX$i.lease" ] && { phone_check "$i" > "$RUN_DIR/chk.$i" 2>&1; echo $? >> "$RUN_DIR/chk.$i"; } &
  done
  wait
  t1=$(date +%s.%N)
  for i in $(seq 1 "$phones"); do
    [ -f "$RUN_DIR/chk.$i" ] || continue
    sed '$d' "$RUN_DIR/chk.$i"; [ "$(tail -1 "$RUN_DIR/chk.$i")" = 0 ] && ok=$((ok + 1))
  done
  printf '   %s/%s phones passed in %.1f s\n' "$ok" "$joined" "$(awk "BEGIN{print $t1 - $t0}")"

  echo "== Internet through the Pi (phone 1)"
  if [ -f "$RUN_DIR/${NS_PREFIX}1.lease" ]; then
    local dns; read -r _ dns < "$RUN_DIR/${NS_PREFIX}1.lease"
    local addr code
    addr="$(ip netns exec "${NS_PREFIX}1" dig +short +time=3 "@$dns" www.raspberrypi.com A | grep -E '^[0-9.]+$' | tail -1)"
    code="$(ip netns exec "${NS_PREFIX}1" curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
            --resolve "www.raspberrypi.com:443:${addr:-0.0.0.0}" https://www.raspberrypi.com/)"
    echo "   https://www.raspberrypi.com/ -> HTTP ${code:-none} (needs Ethernet with internet on the Pi)"
  fi

  echo
  echo "Result: $joined/$phones joined, $ok/$phones passed. Clean up with: sudo $0 clean"
  [ "$ok" -eq "$phones" ]
}

case "${1:-}" in
  run) shift; run "$@" ;;
  clean) [ "$(id -u)" -eq 0 ] || die "Run with sudo."; clean; echo "Cleaned up." ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
