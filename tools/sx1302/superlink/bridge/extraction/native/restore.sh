#!/bin/sh
# Argument is a locally generated, private job directory, never user input.
set -u
job=$1
# External recovery first prevents a still-running worker from launching later.
if [ "${2:-}" != normal ] && [ -f "$job/worker" ]; then
    read -r worker < "$job/worker"
    case "$worker" in ''|*[!0-9]*) exit 1;; esac
    if [ -r "/proc/$worker/cmdline" ] && [ "$(tr '\000' ' ' < /proc/$worker/cmdline)" = "sh $job/run.sh $job " ]; then
        kill -KILL "$worker" 2>/dev/null || :
        # A previously launched timeout/stop command has its own 15-second bound.
        sleep 16
    fi
fi
[ -f "$job/restored" ] && exit 0
if ! mkdir "$job/restoring" 2>/dev/null; then
    # Recover an abandoned lock, but never race another live restoration.
    [ -f "$job/restoring/pid" ] || sleep 1
    owner=$(cat "$job/restoring/pid" 2>/dev/null || :)
    case "$owner" in
        *[!0-9]*) exit 1;;
        '') ;;
        *) if [ -r "/proc/$owner/cmdline" ]; then
               case "$(tr '\000' ' ' < /proc/$owner/cmdline)" in
                   "sh $job/restore.sh $job "*) exit 1;;
               esac
           fi;;
    esac
    rm -f "$job/restoring/pid"
    rmdir "$job/restoring" 2>/dev/null || exit 1
    mkdir "$job/restoring" 2>/dev/null || exit 1
fi
echo "$$" > "$job/restoring/pid"
trap 'rm -f "$job/restoring/pid"; rmdir "$job/restoring" 2>/dev/null' EXIT
if [ -f "$job/changed" ]; then
    # Scan only lorabrd PIDs, then match this job's random environment marker.
    # This also handles a worker killed between process creation and PID recording.
    for child in $(pidof lorabrd 2>/dev/null || :); do
        case "$child" in ''|*[!0-9]*) exit 1;; esac
        if [ -r "/proc/$child/environ" ] && tr '\000' '\n' < "/proc/$child/environ" | grep -Fxq "SUPERLINK_CAPTURE_PATH=$job/capture.bin"; then
            kill -TERM "$child" 2>/dev/null || :
            sleep 2
            if [ -r "/proc/$child/environ" ] && tr '\000' '\n' < "/proc/$child/environ" | grep -Fxq "SUPERLINK_CAPTURE_PATH=$job/capture.bin"; then
                kill -KILL "$child" 2>/dev/null || :
                sleep 1
            fi
        fi
    done
    timeout -s KILL 15 /etc/init.d/lorabr start >/dev/null 2>&1 || exit 1
    tries=0
    while [ "$tries" -lt 10 ]; do
        pids=$(pidof lorabrd 2>/dev/null || :)
        case "$pids" in
            ''|*[!0-9]*) ;;
            *) if [ -r "/proc/$pids/environ" ] && ! tr '\000' '\n' < "/proc/$pids/environ" | grep -q '^LD_PRELOAD='; then
                   touch "$job/restored"
                   exit 0
               fi;;
        esac
        sleep 1
        tries=$((tries + 1))
    done
    exit 1
fi
touch "$job/restored"
