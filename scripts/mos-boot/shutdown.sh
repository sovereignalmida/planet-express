# Planet Express block for MOS's /boot/optional/scripts/shutdown.sh (merged between BEGIN/END markers).
# Stops PE cleanly, dashboard first. Returns rather than exits so the operator's own commands still run.
pe_shutdown() {
    [ -x /etc/init.d/casa-dashboard ] && /etc/init.d/casa-dashboard stop
    [ -x /etc/init.d/casa-planetexpress ] && /etc/init.d/casa-planetexpress stop
    return 0
}
pe_shutdown
