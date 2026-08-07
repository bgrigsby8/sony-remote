#!/bin/sh
# One-time host setup, executed by viam-server when the module is first
# installed on a machine (meta.json `first_run`).
#
# Linux caps userspace USB transfer buffers (usbfs) at 16MB by default. Live
# view fits under that; a 60-120MB RAW does not, and the failure is nasty to
# diagnose: the transfer kills the USB session ~0.5s after the shutter with no
# kernel USB event. Raise the cap now and persist it across reboots.
# session.py also warns at startup if the cap is still too low, so a machine
# where this script couldn't run (no root) still tells the operator what to do.

CAP=/sys/module/usbcore/parameters/usbfs_memory_mb
WANT=1000

current=$(cat "$CAP" 2>/dev/null || echo 0)
if [ "$current" -ge "$WANT" ] 2>/dev/null; then
    echo "sony-remote first_run: usbfs_memory_mb already $current, nothing to do"
else
    if echo "$WANT" > "$CAP" 2>/dev/null; then
        echo "sony-remote first_run: usbfs_memory_mb $current -> $WANT"
    else
        echo "sony-remote first_run: cannot write $CAP (not root?); RAW capture" \
             "will fail until it is >= 256. Fix: echo $WANT | sudo tee $CAP"
    fi
fi

# Persist across reboots. tmpfiles.d applies sysfs writes at boot on any
# systemd host, which covers every machine this rig targets.
if [ -d /etc/tmpfiles.d ] || mkdir -p /etc/tmpfiles.d 2>/dev/null; then
    if echo "w $CAP - - - - $WANT" > /etc/tmpfiles.d/sony-crsdk.conf 2>/dev/null; then
        echo "sony-remote first_run: persisted via /etc/tmpfiles.d/sony-crsdk.conf"
    fi
fi

exit 0
