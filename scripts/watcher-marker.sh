# Sourced by scripts/install-host-command-watcher.sh and
# scripts/install-host-reboot-watcher.sh (POSIX sh). Not run on its own.
#
# watcher_mark <install-root> <kind> <tag>
#
# The container cannot see host units, so each installer tells it that ITS
# watcher is installed with a marker, <root>/ops-state/watchers/<kind>, read
# through the read-only /state/ops bind (server/oikonome/jobs/host_watchers.py).
# The two watchers share one drop-box, so the drop-box alone must not make
# either lever look ready.
#
# Once the watchers directory exists, a missing marker reads as "not
# installed". A host whose watchers were both installed before markers
# existed has no directory — so the first installer re-run there would make
# the OTHER, still running, watcher look absent (a reboot lever that quietly
# stops working, an auto-reboot that never fires). Systemd knows better
# than the directory does, so when the other kind's marker is missing and
# its path unit is enabled for this install, that is recorded too. Nothing
# here ever removes a marker.
#
# The installers run as root, but uninstall runs as the install's owner
# (from a terminal, or from the console through the command watcher), and
# it cannot delete files inside a root-owned directory. So everything this
# step creates is handed to whoever owns the install root.
watcher_mark() {
    _wm_root=$1 _wm_kind=$2 _wm_tag=$3
    _wm_dir="$_wm_root/ops-state/watchers"
    mkdir -p "$_wm_dir"
    chmod u=rwx,go=rx "$_wm_root/ops-state" "$_wm_dir" 2>/dev/null || true
    date -u +%Y-%m-%dT%H:%M:%SZ > "$_wm_dir/$_wm_kind"
    chmod u=rw,go=r "$_wm_dir/$_wm_kind"
    case $_wm_kind in
        command) _wm_other=reboot ;;
        *) _wm_other=command ;;
    esac
    if [ -e "$_wm_dir/$_wm_other" ]; then
        _wm_chown "$_wm_root"
        return 0
    fi
    # the per-install unit, or the shared-name one an older installer left
    if systemctl is-enabled --quiet \
            "oikonome-$_wm_other-watcher-$_wm_tag.path" 2>/dev/null \
       || systemctl is-enabled --quiet \
            "oikonome-$_wm_other-watcher.path" 2>/dev/null; then
        date -u +%Y-%m-%dT%H:%M:%SZ > "$_wm_dir/$_wm_other"
        # A command watcher found this way was installed without a marker,
        # so the marker can only say what revision its checker is if the
        # checker says so itself; one that does not predates revisions and
        # is left at revision 1, which keeps the console's "behind" notice.
        [ "$_wm_other" = command ] && _wm_rev=$(sed -n \
            's/^# oikonome-command-run revision=\([0-9][0-9]*\)$/\1/p' \
            "${WATCHER_COMMAND_RUN:-/usr/local/bin/oikonome-command-run}" \
            2>/dev/null | head -n 1) && [ -n "$_wm_rev" ] \
            && echo "revision=$_wm_rev" >> "$_wm_dir/$_wm_other"
        chmod u=rw,go=r "$_wm_dir/$_wm_other"
        echo "found the $_wm_other watcher already enabled; marked it too"
    fi
    _wm_chown "$_wm_root"
    return 0
}

_wm_chown() {
    _wm_owner=$(stat -c %U "$1" 2>/dev/null) || return 0
    chown "$_wm_owner" "$1/ops-state" 2>/dev/null || true
    chown -R "$_wm_owner" "$1/ops-state/watchers" 2>/dev/null || true
}
