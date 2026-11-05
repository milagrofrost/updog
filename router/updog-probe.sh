#!/bin/sh
# Records the state of each step on the path to the internet, so log-to-html.py
# can work out what caused an outage and when it really started. Only writes
# when something changes, plus a heartbeat every hour to prove it's still alive
# and a small file every 5 minutes for spotting reboots and power loss.

WAN_DEVICE="${WAN_DEVICE:-eth1}"
MODEM="${MODEM:-192.168.100.1}"
MODEM_SUBNET="${MODEM_SUBNET:-192.168.100.}"
# Not Cloudflare, so a Cloudflare problem doesn't look like the internet being down.
INTERNET_TARGETS="${INTERNET_TARGETS:-8.8.8.8 9.9.9.9}"
UPDOG_DIR="${UPDOG_DIR:-/root/updog}"
# The link, route and modem are local, so they're cheap to check often.
INTERVAL="${INTERVAL:-5}"
CHECKS_PER_INTERNET_CHECK="${CHECKS_PER_INTERNET_CHECK:-3}"
MODEM_FAILURES_TO_REPORT=2

PROBE_LOG="$UPDOG_DIR/probe.log"
HEARTBEAT="$UPDOG_DIR/heartbeat"
HEARTBEAT_FILE_SECONDS=300
HEARTBEAT_LOG_SECONDS=3600
MAX_LOG_BYTES=1000000
KEEP_LOG_LINES=5000

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

reachable() { ping -c 1 -W 1 "$1" >/dev/null 2>&1; }

link_state() {
    if [ ! -e "/sys/class/net/$WAN_DEVICE" ]; then
        echo gone
    elif [ "$(cat "/sys/class/net/$WAN_DEVICE/carrier" 2>/dev/null)" = "1" ]; then
        echo up
    else
        echo down
    fi
}

default_gateway() {
    ip -4 route show default dev "$WAN_DEVICE" 2>/dev/null | awk '{print $3; exit}'
}

public_address() {
    ip -4 -o addr show dev "$WAN_DEVICE" scope global 2>/dev/null |
        awk -v skip="$MODEM_SUBNET" 'index($4, skip) != 1 { print $4; exit }'
}

check_gateway() { [ -n "$1" ] && reachable "$1" && echo 1 || echo 0; }

check_internet() {
    for target in $INTERNET_TARGETS; do
        reachable "$target" && { echo 1; return; }
    done
    echo 0
}

boot_id() { cat /proc/sys/kernel/random/boot_id; }

write_heartbeat_file() {
    echo "$(boot_id) $(now)" > "$HEARTBEAT.tmp" && mv "$HEARTBEAT.tmp" "$HEARTBEAT"
}

trim_log() {
    [ -f "$PROBE_LOG" ] || return
    if [ "$(wc -c < "$PROBE_LOG")" -gt "$MAX_LOG_BYTES" ]; then
        tail -n "$KEEP_LOG_LINES" "$PROBE_LOG" > "$PROBE_LOG.tmp" && mv "$PROBE_LOG.tmp" "$PROBE_LOG"
    fi
}

mkdir -p "$UPDOG_DIR"

# A heartbeat from a different boot means the router was off or restarting.
if [ -f "$HEARTBEAT" ]; then
    read -r last_boot last_seen < "$HEARTBEAT"
    if [ "$last_boot" != "$(boot_id)" ]; then
        printf '{"time":"%s","event":"reboot","down_since":"%s"}\n' "$(now)" "$last_seen" >> "$PROBE_LOG"
    fi
fi
write_heartbeat_file

last_state=""
modem=1
modem_failures=0
gateway=0
internet=0
checks=0
last_file_beat=$(date +%s)
last_log_beat=0

while :; do
    link=$(link_state)
    gateway_ip=$(default_gateway)
    address=$(public_address)
    [ -n "$gateway_ip" ] && route=1 || route=0

    # One missed ping isn't worth reporting.
    if reachable "$MODEM"; then
        modem=1
        modem_failures=0
    else
        modem_failures=$((modem_failures + 1))
        [ "$modem_failures" -ge "$MODEM_FAILURES_TO_REPORT" ] && modem=0
    fi

    checks=$((checks + 1))
    if [ "$link" != "up" ]; then
        gateway=0
        internet=0
    elif [ "$checks" -ge "$CHECKS_PER_INTERNET_CHECK" ] || [ -z "$last_state" ]; then
        checks=0
        gateway=$(check_gateway "$gateway_ip")
        internet=$(check_internet)
    fi

    state="\"link\":\"$link\",\"modem\":$modem,\"route\":$route,\"address\":\"$address\",\"gateway\":$gateway,\"internet\":$internet"
    if [ "$state" != "$last_state" ]; then
        printf '{"time":"%s",%s}\n' "$(now)" "$state" >> "$PROBE_LOG"
        logger -t updog-probe "$state"
        last_state="$state"
    fi

    timestamp=$(date +%s)
    if [ $((timestamp - last_log_beat)) -ge "$HEARTBEAT_LOG_SECONDS" ]; then
        printf '{"time":"%s","event":"heartbeat"}\n' "$(now)" >> "$PROBE_LOG"
        last_log_beat=$timestamp
        trim_log
    fi
    if [ $((timestamp - last_file_beat)) -ge "$HEARTBEAT_FILE_SECONDS" ]; then
        write_heartbeat_file
        last_file_beat=$timestamp
    fi
    sleep "$INTERVAL"
done
