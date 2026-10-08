#!/bin/sh
# MOS runs /boot/optional/scripts/shutdown.sh before shutdown: stop PE cleanly, dashboard first.
[ -x /etc/init.d/casa-dashboard ] && /etc/init.d/casa-dashboard stop
[ -x /etc/init.d/casa-planetexpress ] && /etc/init.d/casa-planetexpress stop
exit 0
