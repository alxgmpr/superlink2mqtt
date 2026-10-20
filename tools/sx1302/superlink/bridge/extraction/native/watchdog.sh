#!/bin/sh
# Independent of SSH and the capture worker. Bounds retained plaintext lifetime.
job=$1
sleep 110
[ -d "$job" ] || exit 0
if sh "$job/restore.sh" "$job"; then
    rm -f "$job/capture.bin" "$job/capture.so" "$job/pid" "$job/worker" "$job/captured" "$job/changed" "$job/status" "$job/run.sh" "$job/watchdog.sh" "$job/restore.sh" "$job/restored"
    rmdir "$job" 2>/dev/null || :
    if [ "$(cat /tmp/superlink-extraction.lock/owner 2>/dev/null)" = "$job" ]; then
        rm -f /tmp/superlink-extraction.lock/owner
        rmdir /tmp/superlink-extraction.lock 2>/dev/null || :
    fi
else
    rm -f "$job/capture.bin"
    echo recovery_required > "$job/status"
fi
