# The bundled database: the official postgres image, unchanged except for
# the tool its entrypoint uses to drop from root to the postgres user.
#
# Upstream ships gosu, a Go binary whose embedded Go runtime carries known
# high/critical CVEs until upstream rebuilds it. su-exec (Alpine's package,
# a few hundred lines of C) does the same job with the same argument form,
# `<user> <command> [args...]`, so a two-line shim at gosu's path lets the
# upstream docker-entrypoint.sh run byte-for-byte as shipped: same initdb
# on an empty folder, same start on an existing one, same PGDATA handling,
# same postgres binary — a data folder moves between this image and the
# official one in either direction.
#
# The base is pinned by digest so a rebuild is reproducible. Bump it when
# the scanner reports a fix in the base image:
#   podman pull docker.io/library/postgres:18-alpine
#   podman image inspect docker.io/library/postgres:18-alpine --format '{{.RepoDigests}}'
# (use the image-index digest — the one that is not a per-architecture
# manifest — so every platform resolves). If the bump changes the
# upstream's runtime settings, the check in the last stage fails the build
# and names the line to update below. A new MAJOR also changes base_image
# in compose.yaml (scripts/pg-major-migrate.sh reads it there and moves the
# data across); the build refuses a digest whose major differs from it.
ARG base_image=docker.io/library/postgres:18-alpine
FROM ${base_image}@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395 AS base
ARG base_image
RUN case "$base_image" in \
      *:"$PG_MAJOR"-alpine) ;; \
      *) echo "pinned digest is Postgres $PG_MAJOR but base_image says $base_image" >&2; exit 1 ;; \
    esac \
 && apk add --no-cache su-exec \
 && rm -f /usr/local/bin/gosu \
 && printf '#!/bin/sh\n# su-exec in place of gosu: same "<user> <command> [args...]" form\nexec su-exec "$@"\n' \
      > /usr/local/bin/gosu \
 && chmod 0755 /usr/local/bin/gosu \
 && su-exec postgres true \
 && gosu postgres true \
 && env | grep -E '^(PATH|LANG|PG_MAJOR|PG_VERSION|PGDATA)=' | sort > /tmp/upstream.env

# A file deleted in a later layer still sits in the layer below it, where
# every image scanner finds it — so the result is one flat layer copied
# from the stage above, with the upstream runtime settings declared again.
FROM scratch
COPY --from=base / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=en_US.utf8 \
    PG_MAJOR=18 \
    PG_VERSION=18.6 \
    PGDATA=/var/lib/postgresql/18/docker
RUN env | grep -E '^(PATH|LANG|PG_MAJOR|PG_VERSION|PGDATA)=' | sort > /tmp/ours.env \
 && if ! cmp -s /tmp/upstream.env /tmp/ours.env; then \
      echo "the base image's runtime settings changed — update the ENV above to match:" >&2; \
      diff /tmp/upstream.env /tmp/ours.env >&2; exit 1; \
    fi \
 && rm -f /tmp/upstream.env /tmp/ours.env \
 && ! grep -q 'go1\.' /usr/local/bin/gosu
VOLUME /var/lib/postgresql
WORKDIR /
ENTRYPOINT ["docker-entrypoint.sh"]
STOPSIGNAL SIGINT
EXPOSE 5432
CMD ["postgres"]
