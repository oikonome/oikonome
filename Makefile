# Canonical dev commands — see DEVELOPMENT.md for the workflow they serve.
PY := server/.venv/bin/python

.PHONY: test lint dev spa container-check install-test version-history worktree test-long \
        test-db-prune lock sync api-types api-types-check api-types-tool \
        forecast-baseline forecast-scoreboard \
        api-types-compat

# Bake the v0.1.N build history into the package for the admin console
# (the image carries no .git). Generated, never committed — install.sh
# refreshes it on every install/upgrade; run by hand for `make dev`.
version-history:
	./scripts/gen-version-history.sh

# server/requirements.txt is the ONE resolution of the server's dependencies:
# the image installs exactly it (hash-checked) and the suite refuses to run
# on anything else, so a release upstream reaches production only through a
# deliberate `make lock`, a green suite and a commit. `lock` re-resolves
# server/pyproject.toml to the newest versions it allows (needs uv);
# `sync` installs the committed resolution into the test venv.
LOCK_CMD := uv pip compile server/pyproject.toml --extra model \
	--generate-hashes --universal --python-version 3.14 --upgrade \
	--custom-compile-command "make lock" -q -o server/requirements.txt
# The suite's test-only tools (the property tests' library) resolve the same
# way, against the server's pins, into their own hashed file the image never
# installs (specs/invariants-and-fuzzing.md).
LOCK_TEST_CMD := uv pip compile server/requirements-test.in \
	-c server/requirements.txt --generate-hashes --universal \
	--python-version 3.14 --custom-compile-command "make lock" -q \
	-o server/requirements-test.txt
lock:
	$(LOCK_CMD)
	$(LOCK_TEST_CMD)
	@$(MAKE) --no-print-directory sync

sync:
	server/.venv/bin/python -m pip install -q --require-hashes -r server/requirements.txt
	server/.venv/bin/python -m pip install -q --require-hashes --no-deps -r server/requirements-test.txt
	server/.venv/bin/python -m pip install -q --no-deps -e server

# The clients' API types, generated from the server's documented shapes
# (specs/api-types.md): specs/openapi.json, then the same TypeScript into
# webapp/ and mobile/. The pinned generator lives in tools/api-types;
# `api-types-tool` installs it from its lockfile.
api-types-tool:
	cd tools/api-types && npm ci --ignore-scripts --no-audit --no-fund

api-types:
	$(PY) scripts/gen-api-types.py

api-types-check:
	$(PY) scripts/gen-api-types.py --check

# The schema against the copy every supported release carried: fails when
# a release would remove or narrow what an installed client reads. The
# oldest supported release is set in specs/api-compat.json.
api-types-compat:
	$(PY) scripts/check-api-compat.py

# Security + correctness lint (ruff: flake8-bandit S rules, pyflakes F).
# NOT a formatter — config and rationale in server/pyproject.toml. Runs
# first because it takes a second and the suite takes four minutes.
# Skipped with a warning if ruff isn't installed, so a fresh checkout that
# has not installed ruff (`pip install --no-deps 'ruff>=0.16'`) still gets a usable `make test`.
lint:
	@if [ -x server/.venv/bin/ruff ]; then \
	  server/.venv/bin/ruff check server/oikonome server/tests; \
	else \
	  echo "! ruff not installed — skipping lint gate."; \
	  echo "  install it with: server/.venv/bin/pip install --no-deps 'ruff>=0.16'"; \
	fi
	@# A hook declared below an early return passes the typecheck and the
	@# build, then drops the whole page the first time the branch flips.
	@# Only the linter sees it, so its errors gate both clients here.
	@if [ -x webapp/node_modules/.bin/oxlint ]; then \
	  (cd webapp && node_modules/.bin/oxlint --quiet src) && \
	  webapp/node_modules/.bin/oxlint --quiet --react-plugin -A all -D react/rules-of-hooks mobile/src; \
	else \
	  echo "! oxlint not installed (cd webapp && npm ci) — skipping the client lint gate."; \
	fi

# Refuses to start if another suite is running against the same test
# database — see scripts/test-db-guard.sh for why this is mechanical.
test: lint
	@./scripts/test-db-guard.sh
	@./scripts/test-db-fresh.sh
	cd server && .venv/bin/python -m unittest discover -t . -s tests

# The property, fuzz, restore-invariant and concurrency modules in long
# mode (DEVELOPMENT.md, "Property tests"). Never part of `make test`.
# JOBS=n deals them over n test databases, ONLY=<regex> picks modules,
# SEED=<n> replays a run.
test-long:
	@JOBS=$(JOBS) ONLY=$(ONLY) SEED=$(SEED) ./scripts/test-long.py

# The cash forecast benchmark (specs/forecast-benchmark.md). The baseline
# moves only here; a worse value needs WHY="..." and the reason is kept.
forecast-baseline:
	cd server && $(abspath $(PY)) -W ignore -m tests.forecast_bench.bench \
	  --write-baseline $(if $(WHY),--why "$(WHY)")

# The same simulated households against the engine at BASE and this
# checkout's: a per-persona table of the change. ONLY=<persona regex>,
# SEEDS=n, MODE=default|long, JOBS=n.
forecast-scoreboard:
	@test -n "$(BASE)" || { echo "usage: make forecast-scoreboard BASE=<git ref>"; exit 2; }
	$(PY) scripts/forecast-scoreboard.py --base "$(BASE)" \
	  $(if $(ONLY),--only '$(ONLY)') $(if $(SEEDS),--seeds $(SEEDS)) \
	  $(if $(MODE),--mode $(MODE)) $(if $(JOBS),--jobs $(JOBS))

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
