# oikonome.sh manager menu (spec)

One plain-bash entry point at repo root (zero deps beyond what install.sh
already requires). Menu:
1) Install / Upgrade — current install.sh behavior (idempotent)
2) Status — containers up/down, host port, git version (short hash/tag),
   last backup file+age, quick /readyz probe
3) Logs — tail -f app + worker (Ctrl-C returns to menu)
4) Backup now — scripts/backup.sh into ./backups inside the install
   folder (the install-local backups directory, not a shared one, so
   every artifact belongs to its install; external schedulers pass
   their own destination to scripts/backup.sh)
5) Restore from backup — list dumps (date+size), pick one, CONFIRM-GATED
   (type 'restore'): stack up if needed, drop+recreate db from dump,
   migrate, health-check
6) Reset — CONFIRM-GATED (type 'reset'): stack down, delete ./data,
   reinstall → fresh setup wizard (secrets regenerate)
7) Uninstall — delegates to uninstall.sh (type 'delete')

Non-interactive: `./oikonome.sh <install|status|logs|backup|restore <file>|reset|uninstall>`
also works (CI/scripts). install.sh and uninstall.sh remain as thin
wrappers/back-compat entry points. Docs: quickstart switches its primary
instructions to ./oikonome.sh.

## Multi-instance amendment

Several instances can share one machine (e.g. a household instance plus
a demo), so the interactive menu now opens with an **instance picker**:
every running Oikonome compose project on the box (docker or podman,
found via compose labels) plus the script's own folder even when
stopped. Pick one and the whole menu manages it, no matter where the
script was run from; `s` switches instances. Non-interactive
subcommands still manage only the folder the script lives in.

`n` creates a **new instance**: a never-installed checkout installs in
place (the git-clone-then-run flow — the picker lists it as "not
installed"); from an already-installed folder it asks for a target
folder, clones the checkout there, and installs. Either way the
installer runs immediately and auto-picks the first free port (8042
upward) unless OIKONOME_PORT is exported.

Port fixes that fell out of testing this: install.sh now honors an
exported `OIKONOME_PORT` (compose gives the environment precedence over
.env, so before this the container published on the env port while
.env/project-name/health-wait used an auto-picked one — installs hung
at "waiting for the app to come up"); an explicitly requested busy port
dies instead of walking; and `reset` re-exports the instance's port so
a reset never silently moves it.
