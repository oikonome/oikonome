# extras/

Anything an operator drops here is installed into the image at build time:
a Python distribution (`pyproject.toml` at this level) that registers
through the entry-point groups in `server/oikonome/ext.py`, and an optional
`compose.hosted.yaml` the operator lists in `COMPOSE_FILE`, and an optional
`webapp-ext/` that replaces the SPA's slot directory `webapp/src/ext/`
before the client build (the tracked stub there renders nothing).

Empty in this repository by design. A household running its own instance
needs nothing here; an operator running the product for other people adds
their own add-on (gating, extra routes and jobs, extra console panes).
Nothing in the product imports such a package by name —
see `DEVELOPMENT.md` → Extension points.
