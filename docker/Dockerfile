# Oikonome server image — app and worker share it (worker overrides CMD).
# Stage 1 builds the React SPA; stage 2 is the Python server that serves it.
FROM node:22-slim AS webapp
WORKDIR /build
COPY webapp/package*.json ./
RUN npm ci --silent
COPY webapp .
# An operator's client add-on, if one sits in extras/webapp-ext/ (empty in
# the public tree): it replaces the slot directory src/ext/ before the build.
COPY extras /extras
RUN if [ -d /extras/webapp-ext ]; then rm -rf src/ext && cp -r /extras/webapp-ext src/ext; fi
RUN npm run build

# 3.14 because that is the interpreter the test suite runs on: a container
# a minor version behind the tests means production runs code nothing
# exercised. Bump the two together.
FROM python:3.14-slim
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/oikonome/oikonome"
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OIKONOME_SPA_DIST=/app/webapp/dist
# glibc gives every thread that allocates its own malloc arena, and an
# arena rarely hands memory back to the system once a burst is over. The
# server answers requests from a pool of threads, so one large download or
# import leaves its peak resident in whichever arena served it, and the
# process creeps up across many of them. Two arenas keep that bounded; the
# interpreter's own small-object allocator does most allocation under one
# lock anyway, so little contention is traded for it.
ENV MALLOC_ARENA_MAX=2
# pg_dump 18 for the in-app database-dump download — the base
# distro's own postgresql-client can lag the PG18 server and refuse it
# The base image is rebuilt upstream on its own schedule; Debian's security
# fixes (zlib, openssl, …) land in the archive first, so the image takes them
# from there rather than waiting for the next base rebuild.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-18 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
# Dependencies first, from the committed resolution and nothing else: every
# package is pinned to one version and checked against its hash, so the image
# runs the versions the test suite ran. Resolving afresh here would let a
# release published yesterday into production with no test ever having
# imported it. `make lock` moves the pins; the suite refuses a venv that
# disagrees with them. The file carries the optional local-categorizer stack
# too, so mounting a model file is all an operator has to do to enable it.
# The base image's bundled pip/setuptools lag their security fixes by months;
# upgrade them before anything is built on them (an image scanner reads the
# bundled copies as the image's packages otherwise)
COPY server/requirements.txt server/
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --require-hashes -r server/requirements.txt
COPY server/pyproject.toml server/
COPY server/oikonome server/oikonome
RUN pip install --no-deps ./server && pip check
# An operator's add-on, if one sits in extras/ (empty in the public tree):
# installed after the product so its entry points register in this image.
# Its own requirements.txt, when it ships one, is held to the same hash
# check; without one its dependencies resolve under the product's pins, so
# an add-on can add packages but never move one the product was tested on.
COPY extras extras
RUN if [ -f extras/requirements.txt ]; then \
      pip install --require-hashes -r extras/requirements.txt \
      && pip install --no-deps ./extras; \
    elif [ -f extras/pyproject.toml ]; then \
      grep -oE '^[A-Za-z0-9_.-]+==[^ ;\\]+' server/requirements.txt > /tmp/pins.txt \
      && pip install -c /tmp/pins.txt ./extras && rm /tmp/pins.txt; \
    fi
# pip is a build tool: nothing in the running image installs anything, and
# pip carries its own vendored copies of urllib3, msgpack and setuptools that
# lag their fixes and that a scanner reads as the image's. Out it goes.
RUN python -m pip uninstall -y pip wheel >/dev/null
COPY --from=webapp /build/dist webapp/dist
# In-app Help serves these (single source with the GitHub copies)
COPY docs docs
EXPOSE 8080
# Nothing in this image needs root at runtime — code and SPA are
# root-owned read-only, scratch goes to /tmp, the healthcheck is python
# urllib. A container escape or in-process bug starts from uid 10001.
RUN useradd --system --uid 10001 --create-home oikonome
USER oikonome
# Migrations do not run from this CMD: they are the one-shot `migrate`
# compose service, the only place the superuser DSN exists. App and worker
# start straight into their jobs with the least-privilege admin role.
# `oikonome migrate` stays advisory-locked for anyone running it by hand.
CMD ["oikonome", "serve", "--host", "0.0.0.0", "--port", "8080"]
