#!/bin/sh
set -eu
job=$1
finish() {
    trap - EXIT HUP INT TERM
    if sh "$job/restore.sh" "$job" normal; then
        if [ -f "$job/captured" ]; then echo ready > "$job/status";
        else echo failed > "$job/status"; fi
    else
        rm -f "$job/capture.bin"
        echo recovery_required > "$job/status"
    fi
}
echo "$$" > "$job/worker"
trap finish EXIT
trap 'exit 1' HUP INT TERM
# Mark before stop, so a disconnect at any point still triggers restoration.
touch "$job/changed"
echo stopping > "$job/status"
timeout -s KILL 15 /etc/init.d/lorabr stop >/dev/null 2>&1
[ -z "$(pidof lorabrd 2>/dev/null || :)" ]
echo capturing > "$job/status"
SUPERLINK_CAPTURE_PATH="$job/capture.bin" LD_PRELOAD="$job/capture.so" \
    chrt -r 50 /usr/sbin/lorabrd --syslog /etc/persistent/cfg/lorabr.json </dev/null >/dev/null 2>&1 &
echo "$!" > "$job/pid"
sleep 45
echo restoring > "$job/status"
touch "$job/captured"
