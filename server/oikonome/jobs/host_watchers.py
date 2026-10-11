"""Which host watchers are installed, as far as the container can tell.

Two separate root path-units read the one console drop-box (/state/cmd):
scripts/install-host-command-watcher.sh consumes `command-requested`, and
scripts/install-host-reboot-watcher.sh consumes `reboot-requested`. Both
installers make the drop-box writable for the container, so "the drop-box
is writable" only says that ONE of them ran. Judging both levers by it
lets either watcher alone make both look ready: a reboot request nothing
reads (with its countdown broadcast repeated every night), or a command
queued for no one that then blocks every later command.

So each installer also leaves a marker, `<install>/ops-state/watchers/
<kind>`, which the container reads through the read-only /state/ops bind.
Both installers share that step (scripts/watcher-marker.sh), which also
marks the OTHER watcher when systemd has it enabled: the first re-run on a
host whose watchers predate the markers would otherwise create the
directory holding one marker, and the other watcher, still running, would
read as absent from then on.
"""

from __future__ import annotations

import os
from pathlib import Path

KINDS = ("command", "reboot")


def _state() -> Path:
    return Path(os.environ.get("OIKONOME_STATE_DIR", "/state"))


def drop_box_writable() -> bool:
    cmd = _state() / "cmd"
    return cmd.is_dir() and os.access(cmd, os.W_OK)


def ready(kind: str) -> bool:
    """True when the drop-box is writable AND the watcher for `kind` says
    it is installed.

    A host whose watchers were installed before the markers existed has no
    `watchers` directory at all. There the command watcher still shows
    itself by the results folder only its installer creates; the reboot
    watcher leaves nothing to see until its first reboot, so it is taken
    on the drop-box alone until its installer is run again."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not drop_box_writable():
        return False
    ops = _state() / "ops"
    marks = ops / "watchers"
    if marks.is_dir():
        return (marks / kind).is_file()
    if kind == "command":
        return (ops / "cmd-results").is_dir()
    return True
