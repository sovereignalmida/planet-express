# Planet Express block for MOS's /boot/optional/scripts/post-start.sh (setup merges it between
# BEGIN/END markers; it never replaces the file, so the operator's own commands survive).
#
# MOS keeps / in RAM, so the init scripts and /etc/default files are reinstalled from the persistent
# install on every boot. The block is a function and returns instead of exiting: an `exit` here would
# skip whatever else the operator's post-start.sh does after it.
pe_post_start() {
    PE_HOME=/mnt/data/pe
    CHECKOUT=planet-express   # the checkout's directory name under PE_HOME

    _pe_i=0
    while [ ! -d "$PE_HOME/$CHECKOUT/scripts" ] && [ "$_pe_i" -lt 60 ]; do
        sleep 2
        _pe_i=$((_pe_i + 1))
    done
    if [ ! -d "$PE_HOME/$CHECKOUT/scripts" ]; then
        echo "Planet Express install not found at $PE_HOME; not starting" >&2
        return 0
    fi

    for _pe_name in casa-planetexpress casa-dashboard; do
        cp "$PE_HOME/$CHECKOUT/scripts/$_pe_name.init.d" "/etc/init.d/$_pe_name"
        chmod +x "/etc/init.d/$_pe_name"
        [ -r "$PE_HOME/default/$_pe_name" ] && cp "$PE_HOME/default/$_pe_name" "/etc/default/$_pe_name"
    done

    /etc/init.d/casa-planetexpress start
    sleep 10
    /etc/init.d/casa-dashboard start

    # Optional soak monitor: runs only if the install carries it (see soak.sh).
    if [ -f "$PE_HOME/$CHECKOUT/scripts/mos-boot/soak.sh" ] && [ -d "$PE_HOME/soak" ]; then
        setsid sh "$PE_HOME/$CHECKOUT/scripts/mos-boot/soak.sh" > /dev/null 2>&1 < /dev/null &
    fi
    return 0
}
pe_post_start
