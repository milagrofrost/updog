#!/bin/sh
# shellcheck disable=SC2154  # The BACKUP_ settings come from backup.conf.
# Backs up the router's config, encrypts it and pushes it to a private git repo.
# Only the public key lives here, so this router can't read its own old backups.
#
#   updog-backup.sh           Run if there's been no backup for 20 hours
#   updog-backup.sh --force   Run now
#
# Settings come from backup.conf: BACKUP_REPO, BACKUP_RECIPIENT and BACKUP_SSH_KEY.
# To also keep an unencrypted copy on an NFS share, add BACKUP_NFS_SHARE, like
# server:/share, plus optional BACKUP_NFS_DIR and BACKUP_NFS_OWNER (uid:gid).

set -u

# cron starts this with almost nothing set.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME="${HOME:-/root}"

STATE_DIR="${UPDOG_DIR:-/root/updog}"
CONF="${BACKUP_CONF:-$STATE_DIR/backup.conf}"
WORK="${BACKUP_WORK:-/tmp/updog-backup}"
LOG="$STATE_DIR/backup.log"
LAST_SUCCESS="$STATE_DIR/last-backup"
LAST_SHARE_COPY="$STATE_DIR/last-share-copy"
# Outside $WORK, so cleaning that up can't touch anything on the share.
NFS_MOUNT="${BACKUP_NFS_MOUNT:-/tmp/updog-backup-share}"
NFS_MOUNT_TIMEOUT=30
share_note=""
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

warn() {
    log "WARNING: $1"
    logger -t updog-backup -p daemon.warn "WARNING: $1"
    echo "Warning: $1" >&2
}

fail() {
    log "FAILED: $1"
    logger -t updog-backup -p daemon.err "FAILED: $1"
    echo "Backup failed: $1" >&2
    exit 1
}

unmount_share() {
    if grep -q " $NFS_MOUNT " /proc/mounts; then
        umount "$NFS_MOUNT" || umount -f "$NFS_MOUNT" || umount -l "$NFS_MOUNT"
    fi
    # rmdir, not rm -rf, so nothing can ever delete files on a share still mounted.
    rmdir "$NFS_MOUNT" 2>/dev/null
}

cleanup() {
    unmount_share
    rm -rf "$WORK"
}

mount_share() {
    mkdir -p "$NFS_MOUNT" || return 1
    mount -t nfs -o vers=3,soft,nolock,timeo=50,retrans=2,noexec,nosuid,nodev "$BACKUP_NFS_SHARE" "$NFS_MOUNT" &
    mounting=$!
    waited=0
    while kill -0 "$mounting" 2>/dev/null; do
        if [ "$waited" -ge "$NFS_MOUNT_TIMEOUT" ]; then
            kill "$mounting" 2>/dev/null
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    wait "$mounting" && grep -q " $NFS_MOUNT " /proc/mounts
}

# Saves the backup unencrypted on the share. A failure here is only a warning,
# because it shouldn't stop the encrypted copy from going off-site.
copy_to_share() {
    source_file="$1"
    folder="$NFS_MOUNT/${BACKUP_NFS_DIR:-router-backups}"
    name="router-backup-$(date -u +%Y-%m-%d).tar.gz"

    mount_share || { warn "couldn't mount $BACKUP_NFS_SHARE"; return 1; }
    if [ ! -d "$folder" ]; then
        warn "no folder $folder on the share"
        unmount_share
        return 1
    fi
    if cp "$source_file" "$folder/.incoming" &&
        gzip -t "$folder/.incoming" &&
        chmod 600 "$folder/.incoming" &&
        { [ -z "${BACKUP_NFS_OWNER:-}" ] || chown "$BACKUP_NFS_OWNER" "$folder/.incoming"; } &&
        mv "$folder/.incoming" "$folder/$name" &&
        ln -sf "$name" "$folder/router-backup-latest.tar.gz"; then
        date -u +%Y-%m-%dT%H:%M:%SZ > "$LAST_SHARE_COPY"
        share_note=", copied to the share"
    else
        rm -f "$folder/.incoming"
        warn "couldn't write the copy to the share"
    fi
    unmount_share
}

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

if [ -n "${BACKUP_NFS_SHARE:-}" ]; then
    copy_to_share "$WORK/backup.tar.gz"
fi

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
log "OK: $count files, $size bytes$share_note"
