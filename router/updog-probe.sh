#!/bin/sh
# Records the state of each step on the path to the internet, so log-to-html.py
# can work out what caused an outage. Only writes when something changes, plus
# a small heartbeat file every 5 minutes for spotting reboots and power loss.

WAN_DEVICE="${WAN_DEVICE:-eth1}"
MODEM="${MODEM:-192.168.100.1}"
# Not Cloudflare, so a Cloudflare problem doesn't look like the internet being down.
INTERNET_TARGETS="${INTERNET_TARGETS:-8.8.8.8 9.9.9.9}"
UPDOG_DIR="${UPDOG_DIR:-/root/updog}"
INTERVAL="${INTERVAL:-15}"

PROBE_LOG="$UPDOG_DIR/probe.log"
HEARTBEAT="$UPDOG_DIR/heartbeat"
BEATS_PER_WRITE=20
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

check_modem() { reachable "$MODEM" && echo 1 || echo 0; }

check_gateway() {
    gateway=$(ip -4 route show default dev "$WAN_DEVICE" 2>/dev/null | awk '{print $3; exit}')
    [ -n "$gateway" ] && reachable "$gateway" && echo 1 || echo 0
}

check_internet() {
    for target in $INTERNET_TARGETS; do
        reachable "$target" && { echo 1; return; }
    done
    echo 0
}

boot_id() { cat /proc/sys/kernel/random/boot_id; }

write_heartbeat() {
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
write_heartbeat

last_state=""
beats=0
while :; do
    state="\"link\":\"$(link_state)\",\"modem\":$(check_modem),\"gateway\":$(check_gateway),\"internet\":$(check_internet)"
    if [ "$state" != "$last_state" ]; then
        printf '{"time":"%s",%s}\n' "$(now)" "$state" >> "$PROBE_LOG"
        last_state="$state"
    fi

    beats=$((beats + 1))
    if [ "$beats" -ge "$BEATS_PER_WRITE" ]; then
        beats=0
        write_heartbeat
        trim_log
    fi
    sleep "$INTERVAL"
done
