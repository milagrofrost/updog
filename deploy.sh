#!/bin/sh
# Deploys the router side of UpDog and shows whether the router matches the repo.
#
#   ROUTER=root@192.168.1.1 ./deploy.sh status   Compare the router with the repo
#   ROUTER=root@192.168.1.1 ./deploy.sh deploy   Update anything that's different
#
# It only touches files that changed, and checks new files on the router before
# anything live changes. The status page and Worker deploy with wrangler instead.

set -eu

: "${ROUTER:?Set ROUTER, like ROUTER=root@192.168.1.1 ./deploy.sh status}"
# Installs under this folder and leaves services alone. Used for testing the script.
PREFIX="${DEPLOY_PREFIX:-}"
REPO="$(cd "$(dirname "$0")" && pwd)"

# Repo file and where it goes on the router.
FILES="router/log-to-html.py:/root/log-to-html.py
router/updog-probe.sh:/root/updog-probe.sh
router/updog-probe.init:/etc/init.d/updog-probe
router/nginx.conf:/etc/nginx/nginx.conf"

send_to_router() { ssh -o BatchMode=yes -o ConnectTimeout=6 -o ServerAliveInterval=5 "$ROUTER" "$@"; }

# Without /dev/null, ssh eats the input of the loops that call it, skipping files.
remote() { send_to_router "$@" < /dev/null; }

# Windows checkouts can have CRLF line endings, which would break these on the router.
clean() { tr -d '\r' < "$REPO/$1"; }

repo_sum() { clean "$1" | md5sum | cut -d' ' -f1; }

router_sum() { remote "md5sum '$PREFIX$1' 2>/dev/null" | cut -d' ' -f1 || true; }

changed_files() {
    echo "$FILES" | while IFS=: read -r source destination; do
        [ "$(repo_sum "$source")" = "$(router_sum "$destination")" ] || echo "$source:$destination"
    done
}

check_router_setup() {
    remote 'grep -q log-to-html /etc/crontabs/root' || echo "Warning: no cron job runs log-to-html.py"
    remote 'python3 -c "import pytz"' 2>/dev/null || echo "Warning: pytz isn't installed on the router"
}

status() {
    changes="$(changed_files)"
    check_router_setup
    if [ -z "$changes" ]; then
        echo "The router matches the repo."
        return 0
    fi
    echo "Different from the repo:"
    echo "$changes" | sed 's/^[^:]*:/  /'
    return 1
}

# Removes uploaded files, so a failed check leaves the router as it was.
discard_uploads() {
    echo "$1" | while IFS=: read -r _ destination; do
        remote "rm -f '$PREFIX$destination.new'"
    done
}

upload() {
    source="$1"
    target="$PREFIX$2"
    clean "$source" | send_to_router "mkdir -p \"\$(dirname '$target')\" && cat > '$target.new'"
}

validate() {
    source="$1"
    new="$PREFIX$2.new"
    case "$source" in
        *.py)
            # Runs it on copies of the real logs, without touching the live page or history.
            remote "set -e
                dir=\$(mktemp -d)
                trap 'rm -rf \"\$dir\"' EXIT
                cp '$new' \"\$dir/script.py\"
                mkdir \"\$dir/state\"
                cp /root/updog/history.json /root/updog/probe.log \"\$dir/state/\" 2>/dev/null || true
                python3 \"\$dir/script.py\" /var/log/cloudflared.log \"\$dir/out/index.html\" \"\$dir/state\""
            ;;
        *.sh | *.init)
            remote "sh -n '$new'"
            ;;
        *.conf)
            remote "nginx -t -c '$new' 2>&1 | tail -1 | grep -q successful"
            ;;
    esac
}

install() {
    target="$PREFIX$1"
    remote "if [ -e '$target' ]; then cp -p '$target' '$target.prev'; fi
        mv '$target.new' '$target'
        case '$target' in *.sh | */init.d/*) chmod +x '$target' ;; esac"
}

restart_services() {
    case "$1" in
        *updog-probe*)
            remote '/etc/init.d/updog-probe enable; /etc/init.d/updog-probe restart'
            ;;
    esac
    case "$1" in
        *nginx.conf*)
            # A reload can't change which addresses nginx listens on, so restart it.
            remote '/etc/init.d/nginx restart'
            ;;
    esac
}

deploy() {
    changes="$(changed_files)"
    if [ -z "$changes" ]; then
        echo "The router already matches the repo, so there's nothing to do."
        check_router_setup
        return 0
    fi

    echo "Checking on the router:"
    echo "$changes" | while IFS=: read -r source destination; do
        upload "$source" "$destination"
        if ! validate "$source" "$destination"; then
            echo "  $source failed its check" >&2
            exit 1
        fi
        echo "  $source is good"
    done || {
        discard_uploads "$changes"
        echo "Nothing was changed on the router." >&2
        exit 1
    }

    echo "Installing:"
    echo "$changes" | while IFS=: read -r _ destination; do
        install "$destination"
        echo "  $destination"
    done

    if [ -z "$PREFIX" ]; then
        restart_services "$changes"
        remote '/usr/bin/python3 /root/log-to-html.py'
        sleep 2
        remote '/etc/init.d/updog-probe running' || { echo "The probe isn't running" >&2; exit 1; }
        remote '/etc/init.d/nginx running' || { echo "nginx isn't running" >&2; exit 1; }
    fi

    status
    echo "To undo this, each file's previous version is saved next to it as .prev."
}

case "${1:-status}" in
    status) status ;;
    deploy) deploy ;;
    *)
        echo "Usage: ROUTER=root@192.168.1.1 $0 [status|deploy]" >&2
        exit 2
        ;;
esac
