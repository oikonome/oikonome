# Canonical dev commands — see DEVELOPMENT.md for the workflow they serve.
PY := server/.venv/bin/python

.PHONY: test lint dev spa container-check install-test version-history worktree \
        test-db-prune

# Bake the v0.1.N build history into the package for the admin console
# (the image carries no .git). Generated, never committed — install.sh
# refreshes it on every install/upgrade; run by hand for `make dev`.
version-history:
	./scripts/gen-version-history.sh

# Security + correctness lint (ruff: flake8-bandit S rules, pyflakes F).
# NOT a formatter — config and rationale in server/pyproject.toml. Runs
# first because it takes a second and the suite takes four minutes.
# Skipped with a warning if ruff isn't installed, so a fresh checkout that
# has not run `pip install -e 'server[dev]'` still gets a usable `make test`.
lint:
	@if [ -x server/.venv/bin/ruff ]; then \
	  server/.venv/bin/ruff check server/oikonome server/tests; \
	else \
	  echo "! ruff not installed — skipping lint gate."; \
	  echo "  install it with: server/.venv/bin/pip install -e 'server[dev]'"; \
	fi

# Refuses to start if another suite is running against the same test
# database — see scripts/test-db-guard.sh for why this is mechanical.
test: lint
	@./scripts/test-db-guard.sh
	@./scripts/test-db-fresh.sh
	cd server && .venv/bin/python -m unittest discover -t . -s tests

# Isolated checkout + test database for a parallel session
worktree:
	@./scripts/new-worktree.sh $(name)

# Drop the test databases of worktrees that are gone (dry run without --yes;
# pass ARGS=--yes to drop). Removing a worktree never removed its database.
test-db-prune:
	@./scripts/test-db-prune.sh $(ARGS)

dev:
	cd server && OIKONOME_DEV=1 .venv/bin/python -m uvicorn oikonome.web.app:app --port 8129 --reload

spa:
	cd webapp && npm run build

container-check:
	podman build -q -t oikonome:dev-loop -f docker/Dockerfile .
	podman run -d --replace --name oik-devloop --network host \
	  -e OIKONOME_DSN="postgresql://postgres:devpass@127.0.0.1:5433/oikonome" \
	  -e OIKONOME_DEV=1 oikonome:dev-loop \
	  sh -c "oikonome migrate && exec oikonome serve --host 127.0.0.1 --port 8134"
	sleep 7
	for p in /healthz /readyz /login /app/; do \
	  curl -s -o /dev/null -w "$$p → %{http_code}\n" http://127.0.0.1:8134$$p; done
	podman rm -f oik-devloop

install-test:
	rm -rf /tmp/oikonome-install-test
	git clone -q . /tmp/oikonome-install-test
	@echo "now run: cd /tmp/oikonome-install-test && ./install.sh"
