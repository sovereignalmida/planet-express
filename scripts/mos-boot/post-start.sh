#!/bin/sh
# MOS runs /boot/optional/scripts/post-start.sh at the end of boot. MOS keeps / in RAM, so the init
# scripts and /etc/default files are reinstalled from the persistent install on every boot.
# Install: cp scripts/mos-boot/*.sh /boot/optional/scripts/ ; the host's /etc/default content lives
# in $PE_HOME/default/casa-planetexpress and $PE_HOME/default/casa-dashboard.
PE_HOME=/mnt/data/pe
CHECKOUT=planet-express   # the checkout's directory name under PE_HOME

i=0
while [ ! -d "$PE_HOME/$CHECKOUT/scripts" ] && [ "$i" -lt 60 ]; do
    sleep 2
    i=$((i + 1))
done
if [ ! -d "$PE_HOME/$CHECKOUT/scripts" ]; then
    echo "Planet Express install not found at $PE_HOME; not starting" >&2
    exit 0
fi

for name in casa-planetexpress casa-dashboard; do
    cp "$PE_HOME/$CHECKOUT/scripts/$name.init.d" "/etc/init.d/$name"
    chmod +x "/etc/init.d/$name"
    [ -r "$PE_HOME/default/$name" ] && cp "$PE_HOME/default/$name" "/etc/default/$name"
done

/etc/init.d/casa-planetexpress start
sleep 10
/etc/init.d/casa-dashboard start

# Optional soak monitor: runs only if the install carries it (see soak.sh).
if [ -f "$PE_HOME/$CHECKOUT/scripts/mos-boot/soak.sh" ] && [ -d "$PE_HOME/soak" ]; then
    setsid sh "$PE_HOME/$CHECKOUT/scripts/mos-boot/soak.sh" > /dev/null 2>&1 < /dev/null &
fi
exit 0
