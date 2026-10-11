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
6) Reset — CONFIRM-GATED (type 'reset'): checks the master key first
   (nothing is touched if docker/.env's value is not a key), saves it
   beside the backups, releases bank links, stack down, deletes ./data —
   or, on an external database, drops and recreates that database —
   then reinstalls → fresh setup wizard. The generated secrets (database
   and queue passwords, setup token) regenerate; every other docker/.env
   setting is carried into the reinstall through the environment
   (OIKONOME_RESET_SETTINGS), and the key through OIKONOME_KEEP_MASTER_KEY
   unless `--new-key`.
7) Uninstall — delegates to uninstall.sh (type the instance name).
   Releases bank links when the database goes with the folder; a
   database that outlives it (OIKONOME_DATA_DIR outside the folder, or an
   external database, which uninstall never drops) keeps its links, and
   its docker/.env is saved owner-only before anything is removed.

Non-interactive: `./oikonome.sh <install|status|logs|backup|restore <file>|reset|uninstall|start-at-boot>`
also works (CI/scripts). install.sh and uninstall.sh remain as thin
wrappers/back-compat entry points. Docs: quickstart switches its primary
instructions use ./oikonome.sh.

## Multiple instances

Several instances can share one machine (e.g. a household instance plus
a demo), so the interactive menu opens with an **instance picker**:
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

Ports: install.sh honors an exported `OIKONOME_PORT` (compose gives the
environment precedence over .env, so .env, the project name and the
health wait must use the same port); an explicitly requested busy port
fails instead of walking; and `reset` keeps the instance's port.
