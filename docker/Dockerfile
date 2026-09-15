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
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OIKONOME_SPA_DIST=/app/webapp/dist
# pg_dump 18 for the in-app database-dump download — the base
# distro's own postgresql-client can lag the PG18 server and refuse it
RUN apt-get update \
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
COPY server/pyproject.toml server/
COPY server/oikonome server/oikonome
# [model] = the optional local-categorizer stack; the image carries it so
# mounting a model file is all an operator has to do to enable the feature
RUN pip install './server[model]'
# An operator's add-on, if one sits in extras/ (empty in the public tree):
# installed after the product so its entry points register in this image.
COPY extras extras
RUN if [ -f extras/pyproject.toml ]; then pip install ./extras; fi
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
