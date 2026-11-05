#!/bin/sh
# shellcheck disable=SC2154  # The BACKUP_ settings come from backup.conf.
# Backs up the router's config, encrypts it and pushes it to a private git repo.
# Only the public key lives here, so this router can't read its own old backups.
#
#   updog-backup.sh           Run if there's been no backup for 20 hours
#   updog-backup.sh --force   Run now
#
# Settings come from backup.conf: BACKUP_REPO, BACKUP_RECIPIENT and BACKUP_SSH_KEY.

set -u

# cron starts this with almost nothing set.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME="${HOME:-/root}"

STATE_DIR="${UPDOG_DIR:-/root/updog}"
CONF="${BACKUP_CONF:-$STATE_DIR/backup.conf}"
WORK="${BACKUP_WORK:-/tmp/updog-backup}"
LOG="$STATE_DIR/backup.log"
LAST_SUCCESS="$STATE_DIR/last-backup"
BACKUP_FILE="router-backup.tar.gz.age"
MIN_AGE_SECONDS=72000
MAX_LOG_LINES=500

# The files sysupgrade misses. Binaries are skipped, since they can be downloaded.
EXTRA_FILES="/etc/bird.conf
/root/homelab-health.sh
/root/update-tailscale.sh
/root/expand-root.sh
/root/.profile
/root/.ssh/known_hosts
/usr/sbin/ntpd.hotplug-helper"
SKIP_FILES='^/usr/bin/cloudflared$|^/usr/sbin/tailscaled?$|^/usr/bin/age$'
# A backup without these is broken, whatever else it contains.
MUST_CONTAIN="etc/config/network etc/config/firewall etc/sysupgrade.conf"

log() {
    mkdir -p "$STATE_DIR"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >> "$LOG"
    if [ "$(wc -l < "$LOG")" -gt "$MAX_LOG_LINES" ]; then
        tail -n $((MAX_LOG_LINES / 2)) "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

fail() {
    log "FAILED: $1"
    logger -t updog-backup -p daemon.err "FAILED: $1"
    echo "Backup failed: $1" >&2
    exit 1
}

cleanup() { rm -rf "$WORK"; }

packages_list() {
    if command -v apk >/dev/null 2>&1; then
        apk list -I
    else
        opkg list-installed
    fi
}

recently_backed_up() {
    [ -f "$LAST_SUCCESS" ] &&
        [ $(($(date +%s) - $(date -r "$LAST_SUCCESS" +%s))) -lt "$MIN_AGE_SECONDS" ]
}

if [ "${1:-}" != "--force" ] && recently_backed_up; then
    exit 0
fi

[ -f "$CONF" ] || fail "no settings file at $CONF"
# shellcheck disable=SC1090
. "$CONF"
: "${BACKUP_REPO:?}" "${BACKUP_RECIPIENT:?}" "${BACKUP_SSH_KEY:?}"
command -v age >/dev/null 2>&1 || fail "age isn't installed"
command -v git >/dev/null 2>&1 || fail "git isn't installed"

# Everything happens in /tmp, which is RAM, so this adds no wear to the SD card.
mkdir "$WORK" 2>/dev/null || fail "another backup is running, or $WORK was left behind"
trap cleanup EXIT
trap 'exit 1' INT TERM

packages_list > "$WORK/openwrt-packages.txt" || fail "couldn't list packages"
{
    sysupgrade -l 2>/dev/null | grep -vE "$SKIP_FILES"
    echo "$EXTRA_FILES" | while read -r file; do
        [ -e "$file" ] && echo "$file"
    done
    echo "$CONF"
    echo "$BACKUP_SSH_KEY"
    echo "$WORK/openwrt-packages.txt"
} | sort -u > "$WORK/files.txt"

tar czf "$WORK/backup.tar.gz" -T "$WORK/files.txt" 2>/dev/null || fail "couldn't build the archive"
gzip -t "$WORK/backup.tar.gz" || fail "the archive is corrupt"
tar tzf "$WORK/backup.tar.gz" > "$WORK/contents.txt" || fail "couldn't read the archive"
for file in $MUST_CONTAIN; do
    grep -qx "$file" "$WORK/contents.txt" || fail "the archive is missing $file"
done

age -r "$BACKUP_RECIPIENT" -o "$WORK/backup.age" "$WORK/backup.tar.gz" || fail "encryption failed"
rm -f "$WORK/backup.tar.gz"

export GIT_SSH_COMMAND="dbclient -i $BACKUP_SSH_KEY"
export GIT_SSH_VARIANT=simple

git clone --quiet --depth 1 "$BACKUP_REPO" "$WORK/repo" 2>"$WORK/git.err" ||
    fail "couldn't reach the repo: $(tail -1 "$WORK/git.err")"
mv "$WORK/backup.age" "$WORK/repo/$BACKUP_FILE"
cd "$WORK/repo" || fail "couldn't open the clone"
size=$(wc -c < "$BACKUP_FILE")
count=$(wc -l < "$WORK/contents.txt")
git add "$BACKUP_FILE"
git -c user.name="$(uci -q get system.@system[0].hostname || echo router)" -c user.email=router@updog.invalid \
    commit --quiet -m "Backup: $count files, $size bytes" 2>"$WORK/git.err" ||
    fail "couldn't commit: $(tail -1 "$WORK/git.err")"
git push --quiet origin HEAD 2>"$WORK/git.err" || fail "couldn't push: $(tail -1 "$WORK/git.err")"
cd / || true

date -u +%Y-%m-%dT%H:%M:%SZ > "$LAST_SUCCESS"
log "OK: $count files, $size bytes"
